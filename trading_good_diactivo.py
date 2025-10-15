# trading_diactivo.py — VIP-only: compra según route_base (USDC o SOL, hop=1) + trailing 1.6/-0.8 + SL fijo -0.9 + token dormido
# Perfil: DÍA ACTIVO (24hs) — parcheado para menor latencia, manteniendo lógica y DB
# Punto 4: telemetría mínima sin alterar la lógica

import os, sys, time, json, sqlite3, threading, asyncio, subprocess, random, requests
from datetime import datetime
import websockets
from decimal import Decimal, ROUND_HALF_UP
from telemetry import Timer, jlog              # ← observabilidad
from config import WS_URL, WS_PING_INTERVAL, WS_PING_TIMEOUT  # <<< WS centralizado

DB_NAME = "goodt.db"

# ===== Parámetros (no tocar) =====
MONTO_USDC        = 2.0
SLIPPAGE_INFO     = "0.5%"
ACTIVA_TRAIL_EN   = 0.020    # +2.0%
TRAILING_STOP     = 0.007    # -0.7%
STOP_LOSS_FIJO    = 0.010    # -1.0%

# ⚡ Acelerados (día activo = más agresivo)
STALE_WS_SECS     = 0.80
WAIT_FIRST_WS     = 2.0
POLL_SECONDS      = 0.40
DORMIDO_WINDOW    = 360      # 6 minutos
DORMIDO_BANDA     = 0.005    # ±0.5%

# --- Anti-429 / HTTP control ---
PRICE_TTL         = 2.0
POLL_SECONDS_HTTP = 2.00
COOLDOWN_429      = 60
JITTER_MAX        = 0.20

# ===== Overrides opcionales por entorno (punto 3) =====
def _env_bps(name, default_frac):
    try:
        v = os.getenv(name)
        if not v: return default_frac
        bps = int(str(v).strip())
        return max(0.0, bps / 10_000.0)
    except Exception:
        return default_frac

def _env_secs(name, default_secs):
    try:
        v = os.getenv(name)
        return int(v) if v is not None else default_secs
    except Exception:
        return default_secs

# Mantiene valores por defecto, pero permite override sin editar archivo
ACTIVA_TRAIL_EN = _env_bps("APOLLO_TRAIL_ACTIVATE_BPS", ACTIVA_TRAIL_EN)
TRAILING_STOP   = _env_bps("APOLLO_TRAIL_STOP_BPS",     TRAILING_STOP)
HOLD_SECS       = _env_secs("APOLLO_HOLD_SECS",         0)     # 0 = sin hold si no viene del orquestador

_price_cache = {"mint": None, "t": 0.0, "v": None}
_http_cooldown_until = 0.0
_last_price_status = None
_last_price_status_ts = 0.0

def _cache_fresh(mint: str) -> bool:
    return (_price_cache["mint"] == mint) and (time.monotonic() - _price_cache["t"] < PRICE_TTL)

# ===== Endpoints / mints =====
USDC_MINT    = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT     = "So11111111111111111111111111111111111111112"
PRICE_BASE   = "https://lite-api.jup.ag/price/v3"  # Lite

# ===== Debug =====
DEBUG = True
TICK_PRINT_SECS = 1.0

# ===== Color consola =====
ANSI_RED = "\x1b[31m"
ANSI_GREEN = "\x1b[32m"
ANSI_RESET = "\x1b[0m"

# PATCH: métricas estables de delta/bps y formateo sin "-0.00%"
_EPS = 5e-6                # 0.0005% → evita -0.00%
def _pct(px, anchor):
    try:
        if anchor <= 0: return 0.0
        return (float(px)/float(anchor)) - 1.0
    except Exception:
        return 0.0

def _bps(px, anchor):
    return int(round(_pct(px, anchor) * 10_000))

def fmt_delta(frac):
    try:
        if abs(frac) < _EPS:  # zona muerta
            s = "±0.00%"
            return s
        s = f"{frac*100:+.2f}%"
        return f"{ANSI_GREEN}{s}{ANSI_RESET}" if frac > 0 else f"{ANSI_RED}{s}{ANSI_RESET}"
    except:
        return f"{frac*100:+.2f}%"

def ts():
    return datetime.now().strftime("%H:%M:%S")

# Forzar UTF-8 y line buffering
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

# ===== Helpers DB =====
def db_exec(q, p=()):
    with sqlite3.connect(DB_NAME) as con:
        con.execute(q, p)
        con.commit()

def db_row(q, p=()):
    with sqlite3.connect(DB_NAME) as con:
        con.row_factory = sqlite3.Row
        cur = con.execute(q, p)
        return cur.fetchone()

# ===== Guardar precio vivo para swap_real* =====
def save_live_price(token_mint: str, price_usd: float):
    try:
        if price_usd is None or float(price_usd) <= 0:
            return
    except:
        return
    try:
        with sqlite3.connect(DB_NAME, timeout=1.7) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS precios_live(
                  address TEXT PRIMARY KEY,
                  price_usd REAL NOT NULL,
                  updated_at TEXT NOT NULL
                )
            """)
            con.execute("""
                INSERT INTO precios_live(address, price_usd, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                  price_usd=excluded.price_usd,
                  updated_at=excluded.updated_at
            """, (token_mint, float(price_usd), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
    except Exception as e:
        if DEBUG: print(f"[{ts()}] ⚠️ save_live_price error: {e}", flush=True)

# ===== HTTP session keep-alive =====
_SES = None
def _http():
    global _SES
    if _SES is None:
        s = requests.Session()
        from requests.adapters import HTTPAdapter
        a = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        s.mount("https://", a); s.mount("http://", a)
        s.headers.update({"Connection":"keep-alive","Accept":"application/json"})
        _SES = s
    return _SES

# ===== Precio (Jupiter Price API v3) con anti-429 =====
def _precio_v3_single(mint: str, base: str = PRICE_BASE, timeout=1.5):
    global _last_price_status, _last_price_status_ts
    url = f"{base}?ids={mint}"
    try:
        r = _http().get(url, timeout=timeout)
        _last_price_status = r.status_code
        _last_price_status_ts = time.monotonic()
        r.raise_for_status()
        j = r.json()
        data = j.get("data", j) or {}
        info = data.get(mint) or {}
        p = info.get("usdPrice", info.get("price"))
        return float(p) if (p is not None and float(p) > 0) else None
    except Exception as e:
        if DEBUG: print(f"[{ts()}] ⚠️ precio_v3 error {base}: {e}", flush=True)
        return None

def precio_jupiter(mint: str):
    p = _precio_v3_single(mint, PRICE_BASE, timeout=1.5)
    if p is not None:
        return p
    pro_base = PRICE_BASE.replace("lite-api.jup.ag", "api.jup.ag")
    return _precio_v3_single(mint, pro_base, timeout=1.5)

def precio_jupiter_safe(mint: str):
    global _price_cache, _http_cooldown_until, _last_price_status
    now = time.monotonic()
    if now < _http_cooldown_until:
        return _price_cache["v"] if _cache_fresh(mint) else None
    if _cache_fresh(mint):
        return _price_cache["v"]
    v = precio_jupiter(mint)
    if v is not None and v > 0:
        _price_cache = {"mint": mint, "t": now, "v": v}
        _http_cooldown_until = 0.0
        return v
    if _last_price_status == 429:
        _http_cooldown_until = now + COOLDOWN_429 + random.uniform(0, 5)
    else:
        _http_cooldown_until = 0.0
    return _price_cache["v"] if _cache_fresh(mint) else None

# Cache corto para precio de SOL
_SOL_CACHE = {"ts": 0.0, "px": None}
def precio_sol_usd(ttl=3.0):
    now = time.monotonic()
    if _SOL_CACHE["px"] and (now - _SOL_CACHE["ts"] <= ttl):
        return _SOL_CACHE["px"]
    p = _precio_v3_single(SOL_MINT, PRICE_BASE, timeout=1.5)
    if p:
        _SOL_CACHE["px"] = p
        _SOL_CACHE["ts"] = now
    return _SOL_CACHE["px"]

# ===== WS feed de precio (Raydium CPMM via vaults) =====
class PriceFeed:
    def __init__(self, vault_quote: str, vault_token: str):
        self.vq = vault_quote
        self.vt = vault_token
        self._last = None   # (price_raw_quote_per_token, ts_monotonic)
        self._stop = threading.Event()
        self._th = None

    def start(self):
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            try: self._th.join(timeout=2)
            except: pass

    def last(self):
        return self._last

    def _set_price(self, quote_ui, tok_ui):
        if quote_ui and tok_ui and tok_ui > 0:
            self._last = (quote_ui / tok_ui, time.monotonic())

    async def _loop(self):
        amt_quote = None
        amt_tok   = None
        async with websockets.connect(WS_URL, ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 101, "method": "accountSubscribe",
                "params": [self.vq, {"encoding": "jsonParsed", "commitment": "processed"}]
            }))
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 102, "method": "accountSubscribe",
                "params": [self.vt, {"encoding": "jsonParsed", "commitment": "processed"}]
            }))

            ack_q = json.loads(await ws.recv()); sub_q = ack_q.get("result")
            ack_t = json.loads(await ws.recv()); sub_t = ack_t.get("result")

            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                data = json.loads(msg)
                params = data.get("params") or {}
                sub_id = params.get("subscription")
                val = params.get("result", {}).get("value", {}) if params else {}
                parsed = (val.get("data") or {}).get("parsed", {})
                info = parsed.get("info", {})
                token_amount = info.get("tokenAmount", {}) or {}
                uiAmt = token_amount.get("uiAmount")
                if uiAmt is None:
                    continue
                try:
                    ui = float(uiAmt)
                except:
                    continue

                if sub_id == sub_q:
                    amt_quote = ui
                elif sub_id == sub_t:
                    amt_tok = ui

                if amt_quote is not None and amt_tok is not None:
                    self._set_price(amt_quote, amt_tok)

    def _run(self):
        while not self._stop.is_set():
            try:
                asyncio.run(self._loop())
            except Exception:
                time.sleep(2)

# ===== Compra / Venta =====
def _run_script(cmd, label, timeout_sec=60):
    try:
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUNBUFFERED"] = "1"
        if cmd and cmd[0] == sys.executable and "-u" not in cmd:
            cmd.insert(1, "-u")

        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            bufsize=1
        )
        print(f"[{ts()}] ▶ {label} iniciado: {' '.join(cmd)}", flush=True)

        assert p.stdout is not None
        t0 = time.perf_counter()
        for line in iter(p.stdout.readline, ''):
            print(f"[{label}] {line.rstrip()}", flush=True)
            if timeout_sec and (time.perf_counter() - t0) > timeout_sec:
                print(f"[{ts()}] ⏳ {label} TIMEOUT {timeout_sec}s → killing", flush=True)
                p.kill()
                return False

        rc = p.wait(timeout=2)
        if rc == 0:
            print(f"[{ts()}] ✓ {label} OK", flush=True)
            return True
        else:
            print(f"[{ts()}] ❌ {label} RC={rc}", flush=True)
            return False

    except subprocess.TimeoutExpired:
        print(f"[{ts()}] ⏳ {label} TIMEOUT wait()", flush=True)
        try: p.kill()
        except: pass
        return False
    except Exception as e:
        print(f"[{ts()}] ❌ {label} error: {e}", flush=True)
        try: p.kill()
        except: pass
        return False

def comprar_usdc(address, monto_usdc_ui):
    with Timer("buy_usdc", mint=address, amt=monto_usdc_ui):
        ok = _run_script([sys.executable, "compra_swap_usdc.py", address, str(monto_usdc_ui)], "compra_swap_usdc.py")
    jlog("buy_result", base="USDC", mint=address, ok=bool(ok))
    return ok

def comprar_sol(address, monto_usdc_ui):
    with Timer("buy_sol", mint=address, amt=monto_usdc_ui):
        ok = _run_script([sys.executable, "compra_swap_sol.py", address, str(monto_usdc_ui)], "compra_swap_sol.py")
    jlog("buy_result", base="SOL", mint=address, ok=bool(ok))
    return ok

def vender_a_usdc(address, motivo=""):
    with Timer("sell", mint=address, cause=motivo):
        ok = _run_script([sys.executable, "swap_real.py", address, "sell", "--motivo", motivo], "swap_real.py")
    jlog("sell_result", mint=address, cause=motivo, ok=bool(ok))
    return ok

def vender_seguro(address: str, motivo: str):
    ok = vender_a_usdc(address, motivo)
    return ok

# ===== MAIN =====
def main():
    if len(sys.argv) < 2:
        print("Uso: python trading_diactivo.py <token_mint>", flush=True)
        sys.exit(2)
    address = sys.argv[1].strip()

    vip = db_row("""
        SELECT address, name, decimals, vault_usdc, vault_token, route_base
        FROM vip_tokens WHERE address=?
    """, (address,))
    if not vip:
        print("⛔ No existe en vip_tokens.", flush=True)
        return

    name = vip["name"] or address[:6]
    dec  = vip["decimals"]
    v_quote = vip["vault_usdc"]
    v_token = vip["vault_token"]
    route_base = (vip["route_base"] or "").upper()

    if dec is None or not v_quote or not v_token or route_base not in ("USDC","SOL"):
        print("⛔ Faltan decimals/vaults/route_base en vip_tokens.", flush=True)
        return

    print(f"▶️ Iniciando trading: {name} ({address[:6]}…) | monto={MONTO_USDC} USDC | slippage={SLIPPAGE_INFO} | base={route_base} (hop=1)", flush=True)
    if HOLD_SECS > 0:
        print(f"⏳ HOLD armado: {HOLD_SECS}s tras compra antes de evaluar SL/TS", flush=True)
    jlog("trade_start", mint=address, name=name, base=route_base, amt=MONTO_USDC, hold=HOLD_SECS)

    # Lanzar WS y obtener primer precio
    feed = PriceFeed(v_quote, v_token)
    feed.start()
    price_in = None

    t0_mono = time.monotonic()
    fallback_done = False
    while (time.monotonic() - t0_mono) < WAIT_FIRST_WS:
        last = feed.last()
        if last:
            raw, _ = last  # QUOTE_per_TOKEN
            if raw and raw > 0:
                if route_base == "USDC":
                    price_in = raw
                else:
                    sol_px = precio_sol_usd(ttl=2.0)
                    if sol_px:
                        price_in = raw * sol_px
            if price_in:
                break
        if not fallback_done and (time.monotonic() - t0_mono) >= 1.2 and not price_in:
            p = precio_jupiter_safe(address)
            if p:
                price_in = p
            fallback_done = True
        time.sleep(0.2)

    if not price_in:
        if DEBUG: print(f"[{ts()}] 🔁 Fallback precio Jupiter V3…", flush=True)
        price_in = precio_jupiter_safe(address)

    if not price_in:
        print("⛔ No se obtuvo precio de entrada (WS/Jupiter).", flush=True)
        jlog("trade_abort_no_price", mint=address)
        feed.stop()
        return

    try:
        save_live_price(address, float(price_in))
    except Exception:
        pass

    # COMPRA
    if route_base == "USDC":
        buy_ok = comprar_usdc(address, MONTO_USDC) or comprar_sol(address, MONTO_USDC)
    else:
        buy_ok = comprar_sol(address, MONTO_USDC) or comprar_usdc(address, MONTO_USDC)

    if not buy_ok:
        print(f"[{ts()}] ❌ Error de compra.", flush=True)
        jlog("trade_abort_buy_fail", mint=address)
        feed.stop()
        return

    # Marca inicio de HOLD tras compra
    hold_until_mono = time.monotonic() + max(0, HOLD_SECS)

    # INSERT operación (exacto)
    fecha = datetime.now().strftime("%Y-%m-%d")
    hora_entrada = datetime.now().strftime("%H:%M:%S")
    price_entrada = float(Decimal(str(price_in)).quantize(Decimal("0.00000000"), rounding=ROUND_HALF_UP))
    with sqlite3.connect(DB_NAME) as con:
        cur = con.execute("""
            INSERT INTO operaciones (fecha, address, name, hora_entrada, price_entrada, score)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (fecha, address, name, hora_entrada, price_entrada, 0))
        op_id = cur.lastrowid
    jlog("op_open", mint=address, op_id=op_id, entry=price_entrada)

    # Trailing
    trailing_activo = False
    precio_max = price_in
    stop_dinamico = price_in * (1 - TRAILING_STOP)

    dormido_start_mono = time.monotonic()
    dormido_min = price_in * (1 - DORMIDO_BANDA)
    dormido_max = price_in * (1 + DORMIDO_BANDA)

    print(f"⏱️  Trailing: activa {ACTIVA_TRAIL_EN:.1%} | stop -{TRAILING_STOP:.1%} | SL fijo -{STOP_LOSS_FIJO:.1%}", flush=True)

    last_tick_print_mono = 0.0

    # PATCH: agregador de ticks con prioridad WS y debounce por bps/tiempo
    agg_last_ts = 0.0
    agg_last_src = None
    agg_last_px = None
    MIN_TICK_BPS = 3                 # ignora cambios < 3 bps
    MIN_TICK_GAP_MS = 300            # ignora rebotes < 300 ms

    try:
        while True:
            with Timer("tick", mint=address):
                # 1) Obtener candidatos
                last = feed.last()
                ws_ok = False
                ws_px = None
                ws_ts = None
                if last:
                    raw, ts_ws = last
                    ws_ok = (time.monotonic() - ts_ws) <= STALE_WS_SECS
                    if ws_ok and raw:
                        if route_base == "USDC":
                            ws_px = raw
                        else:
                            sol_px = precio_sol_usd(ttl=2.0)
                            if sol_px:
                                ws_px = raw * sol_px
                        ws_ts = ts_ws

                http_px = None
                http_ts = None
                if not ws_px:
                    hp = precio_jupiter_safe(address)
                    if hp:
                        http_px = hp
                        http_ts = time.monotonic()

                # 2) Selección por prioridad/recencia
                cand_px = None
                cand_ts = None
                cand_src = None
                if ws_px is not None:
                    cand_px, cand_ts, cand_src = ws_px, ws_ts, "ws"
                elif http_px is not None:
                    cand_px, cand_ts, cand_src = http_px, http_ts, "http"
                else:
                    time.sleep(POLL_SECONDS_HTTP + random.uniform(0, JITTER_MAX))
                    continue

                # 3) Debounce: solo aceptar si es más nuevo y movimiento suficiente
                accept = False
                now_mono = time.monotonic()
                if cand_ts is None: cand_ts = now_mono
                if (cand_ts > agg_last_ts) or (cand_ts == agg_last_ts and (agg_last_src, cand_src) == ("http","ws")):
                    if agg_last_px is None:
                        accept = True
                    else:
                        d_bps = abs(_bps(cand_px, agg_last_px))
                        gap_ms = (now_mono - agg_last_ts) * 1000.0
                        accept = (d_bps >= MIN_TICK_BPS) or (gap_ms >= MIN_TICK_GAP_MS)
                # aplicar aceptación
                if accept:
                    agg_last_px = cand_px
                    agg_last_ts = cand_ts
                    agg_last_src = cand_src

                # Si no se aceptó, usa el último aceptado para lógica
                precio = agg_last_px
                src = agg_last_src

                if precio is None:
                    time.sleep(POLL_SECONDS if ws_ok else (POLL_SECONDS_HTTP + random.uniform(0, JITTER_MAX)))
                    continue

                try:
                    save_live_price(address, float(precio))
                except Exception:
                    pass

                if precio < dormido_min or precio > dormido_max:
                    dormido_start_mono = time.monotonic()
                    dormido_min = precio * (1 - DORMIDO_BANDA)
                    dormido_max = precio * (1 + DORMIDO_BANDA)

                gan = _pct(precio, price_in)
                if precio > precio_max:
                    precio_max = precio
                    if trailing_activo:
                        stop_dinamico = max(stop_dinamico, precio_max * (1 - TRAILING_STOP))

                if (time.monotonic() - last_tick_print_mono) >= TICK_PRINT_SECS:
                    try:
                        hold_rem = max(0.0, hold_until_mono - time.monotonic())
                        hold_txt = f" | hold {hold_rem:.1f}s" if hold_rem > 0 else ""
                        if trailing_activo:
                            print(f"[{ts()}] tick {name} now={precio:.8f} Δ={fmt_delta(gan)} max={precio_max:.8f} stop={stop_dinamico:.8f}{hold_txt}", flush=True)
                        else:
                            print(f"[{ts()}] tick {name} now={precio:.8f} Δ={fmt_delta(gan)} (pre-trailing){hold_txt}", flush=True)
                    except Exception:
                        print(f"[{ts()}] tick {name} now=? Δ=?", flush=True)
                    last_tick_print_mono = time.monotonic()
                    jlog("price_tick", mint=address, src=src or "n/a", px=round(float(precio),8), gain_bp=_bps(precio, price_in))

                # === HOLD: no salir antes de HOLD_SECS ===
                if time.monotonic() < hold_until_mono:
                    time.sleep(POLL_SECONDS if (src == "ws") else (POLL_SECONDS_HTTP + random.uniform(0, JITTER_MAX)))
                    continue

                # 1) Stop-loss fijo antes de activar
                if not trailing_activo and gan <= -STOP_LOSS_FIJO:
                    print(f"[{ts()}] 🔻 STOP LOSS | {name} | {gan*100:.2f}%", flush=True)
                    jlog("exit_signal", mint=address, cause="stop_loss_fijo", pnl_bp=int(gan*1e4))
                    ok = vender_seguro(address, "stop_loss_fijo")
                    if not ok:
                        subprocess.run([sys.executable, "salida_forzada.py", address,
                                        "--max-intentos","6","--delay","3",
                                        "--op-id", str(op_id)], check=False)
                    break

                # 2) Activación de trailing
                if not trailing_activo and gan >= ACTIVA_TRAIL_EN:
                    trailing_activo = True
                    stop_dinamico = max(stop_dinamico, precio_max * (1 - TRAILING_STOP))
                    print(f"[{ts()}] 🟠 TRAILING ON | {name} | entry={price_in:.8f} now={precio:.8f} max={precio_max:.8f} stop={stop_dinamico:.8f}", flush=True)
                    jlog("trailing_on", mint=address, entry=round(float(price_in),8), now=round(float(precio),8))

                # 3) Salida por trailing
                if trailing_activo and precio <= stop_dinamico:
                    print(f"[{ts()}] 🔴 TRAILING HIT | {name} | px={precio:.8f} stop={stop_dinamico:.8f}", flush=True)
                    jlog("exit_signal", mint=address, cause="trailing_hit")
                    ok = vender_seguro(address, "trailing_hit")
                    if not ok:
                        subprocess.run([sys.executable, "salida_forzada.py", address,
                                        "--max-intentos","6","--delay","3",
                                        "--op-id", str(op_id)], check=False)
                    break

                # 4) Token dormido
                if (time.monotonic() - dormido_start_mono) >= DORMIDO_WINDOW:
                    print(f"[{ts()}] 😴 TOKEN DORMIDO | {name} | {DORMIDO_WINDOW}s", flush=True)
                    jlog("exit_signal", mint=address, cause="token_dormido")
                    ok = vender_seguro(address, "token_dormido")
                    if not ok:
                        subprocess.run([sys.executable, "salida_forzada.py", address,
                                        "--max-intentos","6","--delay","3",
                                        "--op-id", str(op_id)], check=False)
                    break

                time.sleep(POLL_SECONDS if (src == "ws") else (POLL_SECONDS_HTTP + random.uniform(0, JITTER_MAX)))

    finally:
        try:
            feed.stop()
        except Exception:
            pass
        jlog("trade_end", mint=address)

if __name__ == "__main__":
    main()








