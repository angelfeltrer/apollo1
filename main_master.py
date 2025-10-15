# main_master_vip.py — Low-latency scan (score ponderado R210→R15, sin horarios, 1×1)
import time, json, sqlite3, subprocess, sys, os, shutil, atexit, ctypes, threading, random
from collections import deque, defaultdict
from datetime import datetime, timedelta
from reporte import tick_reporte_diario           # ← 🔔 REPORTE DIARIO 23:59
from telemetry import Timer, jlog                 # ← punto 4: observabilidad mínima

# =================== CONST ===================
DB_NAME   = "goodt.db"
STOP_FILE = "stop.txt"
LOCK_FILE = "trading.lock"

POLL_SECS_BASE = 1.2
PRINT_LIST_LIMIT = 20
DEBUG = True

# Jupiter Price
PRICE_BASE = "https://lite-api.jup.ag/price/v3"
BATCH = 60
SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_API_KEY = (os.getenv("JUPITER_API_KEY") or "").strip()

# Tunings red
PRICE_TTL = 2.0
_MIN_REQ_SPACING = 0.9
_HTTP_TIMEOUT = 1.5

# ---- Señal ponderada (R210→R15)
SCORE_WEIGHTS = {
    'r210': 1,
    'r180': 1,
    'r120': 2,
    'r90':  2,
    'r60':  4,
    'r30':  5,
    'r15':  6,
}
TICK_MIN_RET = 0.008       # 0.8% mínimo para contar “tick”
SCORE_THRESHOLD = 17       # score mínimo
CONF_TICKS = 2             # confirmaciones

# ---- Punto 3: trailing y anti round-trip (vía orquestador)
ACTIVA_TRAIL_EN  = 0.020   # +2.0% activa trailing
TRAILING_STOP    = 0.007   # 0.7% trail
HOLD_SECS        = 8       # cooldown post-compra antes de permitir venta
REENTRY_BLOCK    = 30      # veto reentrada por token tras cerrar trade (s)

# ================== CONSOLA ==================
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def ts(): return datetime.now().strftime("%H:%M:%S")

# =================== NO SLEEP ===================
KEEP_SCREEN_ON = False
_KEEP_AWAKE = True
ES_CONTINUOUS        = 0x80000000
ES_SYSTEM_REQUIRED   = 0x00000001
ES_DISPLAY_REQUIRED  = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040

def _apply_exec_state():
    if os.name != "nt": return
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
    if KEEP_SCREEN_ON: flags |= ES_DISPLAY_REQUIRED
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass

def _win_keep_awake_on():
    if os.name != "nt": return
    _apply_exec_state()
    print(f"[{ts()}] 🛡️  No-sleep activo (refresco periódico). Pantalla={'ON' if KEEP_SCREEN_ON else 'permite OFF'}.")
    def _worker():
        while _KEEP_AWAKE:
            _apply_exec_state()
            time.sleep(50)
    threading.Thread(target=_worker, daemon=True).start()

def _win_keep_awake_off():
    if os.name != "nt": return
    global _KEEP_AWAKE
    _KEEP_AWAKE = False
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass

atexit.register(_win_keep_awake_off)

# =================== SQLITE ===================
_SQL_CON = None

def _sql_connect():
    con = sqlite3.connect(DB_NAME, timeout=0.2, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-8000;")
    con.row_factory = sqlite3.Row
    return con

def _sql():
    global _SQL_CON
    if _SQL_CON is None:
        _SQL_CON = _sql_connect()
    return _SQL_CON

def db_rows(q, p=()):
    cur = _sql().execute(q, p)
    return cur.fetchall()

def db_row(q, p=()):
    cur = _sql().execute(q, p)
    return cur.fetchone()

def db_exec(q, p=()):
    _sql().execute(q, p)
    _sql().commit()

# =================== LOCK 1×1 ===================
def acquire_lock() -> bool:
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False

def release_lock():
    try: os.remove(LOCK_FILE)
    except FileNotFoundError: pass

def lock_is_stale(max_minutes=20):
    try:
        mtime = os.path.getmtime(LOCK_FILE)
        return (time.time() - mtime) > (max_minutes * 60)
    except FileNotFoundError:
        return False

# =================== HTTP SESSION ===================
import requests
_SES = None
_last_req = 0.0
_price_cache = {}

def _http():
    global _SES
    if _SES is None:
        s = requests.Session()
        from requests.adapters import HTTPAdapter
        a = HTTPAdapter(pool_connections=4, pool_maxsize=12, max_retries=0)
        s.mount("https://", a); s.mount("http://", a)
        s.headers.update({"Connection":"keep-alive","Accept":"application/json"})
        _SES = s
    return _SES

# =================== VIPS ===================
_SQL_GET_VIPS = """
SELECT address, name, route_base, decimals, pool_id, vault_usdc, vault_token
FROM vip_tokens
WHERE estado='🚀 comprar'
  AND prioridad = 100
  AND route_base IN ('USDC','SOL')
  AND decimals IS NOT NULL
"""

def get_vips_ready():
    return db_rows(_SQL_GET_VIPS)

# =================== PRECIOS ===================
def _short(m): return (m or "")[:6] + "…"

def fetch_prices_usd(mints):
    if not mints: return {}
    now = time.monotonic()
    out, needed = {}, []

    uniq = [m for m in dict.fromkeys(mints) if m]
    for m in uniq:
        ts_px = _price_cache.get(m)
        if ts_px and (now - ts_px[0] <= PRICE_TTL) and ts_px[1] > 0:
            out[m] = ts_px[1]
        else:
            needed.append(m)

    total = len(uniq)
    if DEBUG:
        chunks = (len(needed) + BATCH - 1) // BATCH if needed else 0
        print(f"[{ts()}] 🔎 Cotizando {total} ids en {chunks} chunk(s) (cache hit={total-len(needed)})")

    if not needed:
        if DEBUG: print(f"[{ts()}] ✅ Total precios válidos: {len(out)} / {total} (solo caché)")
        return out

    global _last_req
    dt = now - _last_req
    if dt < _MIN_REQ_SPACING:
        time.sleep((_MIN_REQ_SPACING - dt) + 0.02)

    ses = _http()
    for i in range(0, len(needed), BATCH):
        chunk = needed[i:i + BATCH]
        url = f"{PRICE_BASE}?ids={','.join(chunk)}"
        if DEBUG:
            det = ", ".join(_short(x) for x in chunk[:PRINT_LIST_LIMIT])
            more = "" if len(chunk) <= PRINT_LIST_LIMIT else "…"
            print(f"[{ts()}]   • Chunk {i//BATCH+1}/{(len(needed)+BATCH-1)//BATCH}: {len(chunk)} ids [{det}{more}]")
        data = {}
        backoff = 0.6 + random.random()*0.4

        with Timer("price_batch", size=len(chunk)):
            try:
                r = ses.get(url, timeout=_HTTP_TIMEOUT)
                _last_req = time.monotonic()
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    try: backoff = max(backoff, float(ra)) if ra else backoff
                    except Exception: pass
                    if DEBUG: print(f"[{ts()}] ⚠️ 429 lite. Backoff {backoff:.2f}s")
                    jlog("price_429", src="lite", backoff=round(backoff,2))
                    time.sleep(backoff)
                    r = ses.get(url, timeout=_HTTP_TIMEOUT)
                    _last_req = time.monotonic()
                r.raise_for_status()
                j = r.json()
                data = j.get("data", j) or {}

            except Exception as e:
                if DEBUG: print(f"[{ts()}] ⚠️ price lite error: {e}")
                jlog("price_err", src="lite", msg=str(e))
                if JUP_API_KEY:
                    try:
                        alt = url.replace("lite-api.jup.ag", "api.jup.ag")
                        rr = ses.get(alt, headers={"X-API-KEY": JUP_API_KEY}, timeout=_HTTP_TIMEOUT)
                        _last_req = time.monotonic()
                        if rr.status_code == 429:
                            ra = rr.headers.get("Retry-After")
                            try: backoff = max(backoff, float(ra)) if ra else backoff
                            except Exception: pass
                            if DEBUG: print(f"[{ts()}] ⚠️ 429 pro. Backoff {backoff:.2f}s")
                            jlog("price_429", src="pro", backoff=round(backoff,2))
                            time.sleep(backoff)
                            rr = ses.get(alt, headers={"X-API-KEY": JUP_API_KEY}, timeout=_HTTP_TIMEOUT)
                            _last_req = time.monotonic()
                        rr.raise_for_status()
                        jj = rr.json()
                        data = jj.get("data", jj) or {}
                        if DEBUG: print(f"[{ts()}]   ↪️ Fallback OK (api.jup.ag)")
                        jlog("price_fallback_ok", size=len(chunk))
                    except Exception as e2:
                        if DEBUG: print(f"[{ts()}] ⚠️ price pro error: {e2}")
                        jlog("price_err", src="pro", msg=str(e2))

        got = 0
        if isinstance(data, dict):
            now2 = time.monotonic()
            for m, v in data.items():
                try:
                    px = float(v.get("usdPrice") if isinstance(v, dict) else None)
                    if px and px > 0:
                        out[m] = px
                        _price_cache[m] = (now2, px)
                        got += 1
                except Exception:
                    pass
        if DEBUG: print(f"[{ts()}]   ✓ Precios recibidos: {got}")
        jlog("price_batch_result", size=len(chunk), got=got)

    if DEBUG:
        print(f"[{ts()}] ✅ Total precios válidos: {len(out)} / {total}")
        if len(out) == 0:
            print(f"[{ts()}] ⚠️ Ningún precio recibido.")
    return out

# =================== MÉTRICAS ===================
def _ret_secs(series, secs):
    if not series: return 0.0
    t_now, _ = series[-1]
    target = t_now - secs
    p_then = None
    for t, p in reversed(series):
        if t <= target: p_then = p; break
    if p_then is None: p_then = series[0][1]
    if not p_then or p_then <= 0: return 0.0
    return (series[-1][1] / p_then) - 1.0

def _weighted_score(series):
    r210 = _ret_secs(series, 210)
    r180 = _ret_secs(series, 180)
    r120 = _ret_secs(series, 120)
    r90  = _ret_secs(series, 90)
    r60  = _ret_secs(series, 60)
    r30  = _ret_secs(series, 30)
    r15  = _ret_secs(series, 15)

    vals = {
        'r210': r210, 'r180': r180, 'r120': r120, 'r90': r90,
        'r60': r60, 'r30': r30, 'r15': r15
    }
    ticks = {k: (v >= TICK_MIN_RET) for k, v in vals.items()}
    score = sum(SCORE_WEIGHTS[k] for k, ok in ticks.items() if ok)

    if DEBUG:
        try:
            dbg_vals = ", ".join(f"{k}={vals[k]:+.3%}" for k in ('r210','r180','r120','r90','r60','r30','r15'))
            print(f"[{ts()}]   ↪︎ ROC: {dbg_vals} | ticks={sum(ticks.values())} score={score}")
        except Exception:
            pass
    return score

def decide_signal(series):
    if len(series) < 20:
        return False
    score = _weighted_score(series)
    return score >= SCORE_THRESHOLD

# --- Filtro adicional: estructura bull r60 < r30 < r15 ---
def _rN(series, secs):
    return _ret_secs(series, secs)

def estructura_bull(series) -> bool:
    r60 = _rN(series, 60)
    r30 = _rN(series, 30)
    r15 = _rN(series, 15)
    return (r60 < r30) and (r30 < r15)

# =================== UI ===================
ANSI_RED = "\x1b[31m"
ANSI_GREEN = "\x1b[32m"
ANSI_RESET = "\x1b[0m"
def fmt_accel(val: float) -> str:
    s = f"{val:+.4%}"
    try:
        if val < 0:   return f"{ANSI_RED}{s}{ANSI_RESET}"
        if val > 0:   return f"{ANSI_GREEN}{s}{ANSI_RESET}"
        return s
    except Exception:
        return s

def _trading_script_for_now(_dt: datetime) -> str:
    return "trading_good_diactivo.py"

def abrir_trading_en_terminal(mint: str):
    cwd = os.getcwd()
    py = sys.executable
    script_name = _trading_script_for_now(datetime.now())
    script = os.path.join(cwd, script_name)
    wt = shutil.which("wt")
    dentro_de_wt = bool(os.environ.get("WT_SESSION"))
    if wt and dentro_de_wt:
        try:
            cmd = [wt, "-w", "0", "split-pane", "-V", "-d", cwd,
                   "powershell", "-NoExit", "-Command", f'& "{py}" "{script}" "{mint}"']
            subprocess.Popen(cmd); return True
        except Exception as e:
            print(f"[{ts()}] ⚠️ split-pane WT falló: {e}")
    if wt:
        try:
            cmd = [wt, "-d", cwd, "powershell", "-NoExit", "-Command",
                   f'& "{py}" "{script}" "{mint}"']
            subprocess.Popen(cmd); return True
        except Exception as e:
            print(f"[{ts()}] ⚠️ abrir WT nueva falló: {e}")
    try:
        ps = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        ps_cmd = f'& "{py}" "{script}" "{mint}"'
        subprocess.Popen([ps, "-NoExit", "-Command", ps_cmd], cwd=cwd)
        return True
    except Exception as e:
        print(f"[{ts()}] ❌ no pude lanzar {script_name} externamente: {e}")
        try:
            subprocess.Popen([py, script, mint], cwd=cwd, stdout=None, stderr=None)
            return False
        except Exception as ee:
            print(f"[{ts()}] ❌ fallback local también falló: {ee}")
            return False

# =================== LOOP ===================
def hay_stop():
    try:
        with open(STOP_FILE, "r", encoding="utf-8", errors="ignore") as f:
            return "STOP" in f.read().upper()
    except FileNotFoundError:
        return False

def main():
    _win_keep_awake_on()
    print("▶️  main_master_vip (low-latency): monitoreando VIPs y lanzando trading_good_diactivo.py …")

    price_hist = defaultdict(lambda: deque(maxlen=420))   # ~10 min si POLL≈1.5
    confirm = defaultdict(int)
    running = set()
    last_heartbeat = 0.0
    poll_secs = POLL_SECS_BASE

    # Punto 3: memoria de reentrada por token
    last_trade_ts = {}   # mint -> monotonic ts último cierre
    last_launch_ts = {}  # mint -> monotonic ts último lanzamiento trader

    # micro-cache VIPs
    vips_cache = []
    vips_cache_count = -1
    reuse_vips_cycles = 0
    MAX_REUSE_CYCLES = 2

    while True:
        if hay_stop():
            print("🛑 STOP detectado. Saliendo…")
            jlog("stop_detected")
            break

        tick_reporte_diario()

        # FREEZE 1×1
        if os.path.exists(LOCK_FILE):
            if lock_is_stale(20):
                print(f"[{ts()}] ⚠️ trading.lock viejo → limpio.")
                release_lock()
            else:
                time.sleep(1.0); continue

        try:
            # VIPs cache
            if reuse_vips_cycles > 0 and vips_cache:
                vips = vips_cache
                reuse_vips_cycles -= 1
            else:
                with Timer("get_vips"):
                    vips = get_vips_ready()
                cnt = len(vips)
                if cnt == vips_cache_count and cnt > 0:
                    vips_cache = vips
                    reuse_vips_cycles = MAX_REUSE_CYCLES
                else:
                    vips_cache = vips
                    vips_cache_count = cnt
                    reuse_vips_cycles = MAX_REUSE_CYCLES

            if not vips:
                if DEBUG: print(f"[{ts()}] 💤 0 VIPs '🚀 comprar'.")
                time.sleep(poll_secs); continue

            if DEBUG:
                names = [(row["name"] or _short(row["address"])) + f"({row['route_base']})" for row in vips]
                line = ", ".join(names[:PRINT_LIST_LIMIT]); suf = "" if len(names) <= PRINT_LIST_LIMIT else "…"
                print(f"[{ts()}] 🎯 VIPs listos: {len(vips)} → {line}{suf}")

            # Prepara mints
            mints = {SOL_MINT}
            mints.update(row["address"] for row in vips)
            with Timer("prices_fetch", uniq=len(mints)):
                prices = fetch_prices_usd(list(mints))
            if not prices:
                if DEBUG: print(f"[{ts()}] ⚠️ Sin precios.")
                time.sleep(poll_secs); continue

            sol_usd = prices.get(SOL_MINT, 0.0)
            if DEBUG:
                print(f"[{ts()}] 💰 SOL/USD = {sol_usd:.6f}" if sol_usd else f"[{ts()}] ⚠️ SOL/USD n/d")

            tnow = time.monotonic()

            # Actualiza series
            for row in vips:
                mint = row["address"]
                route_base = row["route_base"]
                name = (row["name"] or _short(mint))
                px_usd = prices.get(mint)
                if not px_usd or px_usd <= 0:
                    if DEBUG: print(f"[{ts()}]   ⛔ {name}({_short(mint)}) sin precio válido → skip")
                    continue
                ratio = px_usd if route_base == "USDC" else (px_usd / sol_usd if sol_usd > 0 else None)
                if ratio is None:
                    if DEBUG: print(f"[{ts()}]   ⛔ {name} base SOL pero SOL/USD n/d → skip")
                    continue
                price_hist[mint].append((tnow, ratio))
                if DEBUG and random.random() < 0.2:
                    try:
                        print(f"[{ts()}]   • tick {name} base={route_base} px_usd={px_usd:.8f} ratio={ratio:.8f}")
                    except Exception:
                        print(f"[{ts()}]   • tick {name} base={route_base}")

            # Señales
            for row in vips:
                mint = row["address"]
                if mint in running: continue
                series = price_hist[mint]
                if not series: continue

                # Debug corto
                if DEBUG and len(series) >= 5 and random.random() < 0.25:
                    r15 = _ret_secs(series, 15); r30 = _ret_secs(series, 30); r60 = _ret_secs(series, 60)
                    n = row["name"] or _short(mint)
                    ok_bull = estructura_bull(series)
                    print(f"[{ts()}]   ↪︎ {n} r15={r15:+.3%} r30={r30:+.3%} r60={r60:+.3%} bull={ok_bull} conf={confirm[mint]}")

                # Señal válida solo si score y estructura bull
                if decide_signal(series) and estructura_bull(series):
                    confirm[mint] += 1
                else:
                    if confirm[mint] != 0: confirm[mint] = 0

                # Anti reentrada
                last_t = max(last_trade_ts.get(mint, 0.0), last_launch_ts.get(mint, 0.0))
                cool_ok = (tnow - last_t) >= REENTRY_BLOCK

                if confirm[mint] >= CONF_TICKS and cool_ok:
                    if not acquire_lock():
                        if DEBUG: print(f"[{ts()}] ⏸️ Trade en curso. Omito señal de {_short(mint)}")
                        confirm[mint] = 0; continue
                    try:
                        script_name = _trading_script_for_now(datetime.now())
                        n = row["name"] or _short(mint)
                        print(f"[{ts()}] 🚀 Señal: {n} ({_short(mint)}) | lanzando {script_name} (1×1)")
                        confirm[mint] = 0
                        running.add(mint)
                        last_launch_ts[mint] = time.monotonic()

                        # Parámetros de trailing/hold al trader
                        env = os.environ.copy()
                        env["APOLLO_TRAIL_ACTIVATE_BPS"] = str(int(ACTIVA_TRAIL_EN * 10_000))  # 200
                        env["APOLLO_TRAIL_STOP_BPS"]     = str(int(TRAILING_STOP * 10_000))    # 70
                        env["APOLLO_HOLD_SECS"]          = str(int(HOLD_SECS))                 # 8
                        env["APOLLO_REENTRY_BLOCK_SECS"] = str(int(REENTRY_BLOCK))             # 30
                        py = sys.executable
                        script = os.path.join(os.getcwd(), script_name)
                        with Timer("launch_trader", mint=mint):
                            rc = subprocess.run([py, script, mint], cwd=os.getcwd(), check=False, env=env).returncode
                        jlog("launch_done", mint=mint, rc=rc)
                        print(f"[{ts()}] ✅ {script_name} finalizó (rc={rc})")
                        last_trade_ts[mint] = time.monotonic()
                        time.sleep(1.2)
                    except Exception as e:
                        print(f"[{ts()}] ❌ no pude lanzar trading_good: {e}")
                        jlog("launch_err", mint=mint, msg=str(e))
                        running.discard(mint)
                    finally:
                        release_lock()
                elif confirm[mint] >= CONF_TICKS and not cool_ok:
                    if DEBUG:
                        left = REENTRY_BLOCK - (tnow - last_t)
                        print(f"[{ts()}] 🧊 veto reentrada {_short(mint)} {left:.1f}s")
                        jlog("reentry_veto", mint=mint, left=round(left,1))

            # Limpia running sin query pesada
            if running:
                placeholders = ",".join("?" for _ in running)
                q = f"SELECT address, estado FROM vip_tokens WHERE address IN ({placeholders})"
                rows = db_rows(q, tuple(running))
                finished = {r["address"] for r in rows if (r["estado"] or "").strip() not in ("🚀 comprar","▶️ lanzando")}
                if DEBUG and finished:
                    print(f"[{ts()}] 🧹 running -= {len(finished)}")
                running -= finished

            if DEBUG and (time.monotonic() - last_heartbeat) > 30:
                print(f"[{ts()}] ❤️ loop vivo | running={len(running)} | hist_series={len(price_hist)}")
                last_heartbeat = time.monotonic()

            time.sleep(poll_secs)

        except KeyboardInterrupt:
            print("\n🛑 stop manual.")
            jlog("stop_manual")
            break
        except Exception as e:
            print(f"[{ts()}] ⚠️ loop error: {e}")
            jlog("loop_err", msg=str(e))
            time.sleep(1.2)

if __name__ == "__main__":
    main()




























