# swap_real.py — Venta TOKEN -> (USDC|SOL) con fallback:
# 1) Si falla la preferida, intenta la alternativa (USDC<->SOL).
# 2) Si no hay ruta directa (NO_ROUTES_FOUND), reintenta sin onlyDirectRoutes.
#    Mantiene minOut estricto. Cambios acotados a cotización/selección de ruta.

import sys, os, json, sqlite3, time, argparse, logging, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from base64 import b64decode
from solana.transaction import Transaction
from solana.rpc.types import TxOpts
from solana.rpc.api import Client  # noqa: F401 (client real viene de config)
from time import perf_counter

# === Telemetry (no-op fallback) ===
try:
    from telemetry import emit  # type: ignore
except Exception:
    def emit(event, **kw):  # no-op
        pass

# === Config centralizada ===
from config import (
    client, keypair, wallet_pubkey,
    JUPITER_API_KEY, QUOTE_URL, SWAP_URL,
    DB_NAME, AMOUNT_LAMPORTS as CFG_AMOUNT_LAMPORTS  # noqa: F401
)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT  = "So11111111111111111111111111111111111111112"

# --- logging ---
DEBUG = os.getenv("DEBUG_SWAP_REAL", "0") == "1"
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# --- sesión HTTP reutilizable ---
_SES = requests.Session()
_SES.headers.update({"Accept": "application/json", "Connection": "keep-alive"})

def http_get(url, headers=None, params=None, timeout=5, attempts=2):
    last_exc = None
    for i in range(attempts):
        try:
            t0 = perf_counter()
            r = _SES.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
            dt = int((perf_counter() - t0) * 1000)
            emit("http_get", url=url, status=r.status_code, dt_ms=dt)
            if r.status_code >= 400:
                logging.warning(f"GET {url} {r.status_code}: {r.text[:300]}")
            return r
        except Exception as e:
            last_exc = e
            emit("http_get_error", url=url, err=str(e), attempt=i+1)
            logging.error(f"GET {url} error: {e}\n{traceback.format_exc()}")
            if i + 1 < attempts:
                time.sleep(0.2 * (2 ** i))
    if last_exc:
        raise last_exc

def http_post(url, headers=None, json_body=None, timeout=10, attempts=2):
    last_exc = None
    for i in range(attempts):
        try:
            t0 = perf_counter()
            r = _SES.post(url, headers=headers or {}, json=json_body or {}, timeout=timeout)
            dt = int((perf_counter() - t0) * 1000)
            emit("http_post", url=url, status=r.status_code, dt_ms=dt)
            if r.status_code >= 400:
                logging.warning(f"POST {url} {r.status_code}: {r.text[:300]}")
            return r
        except Exception as e:
            last_exc = e
            emit("http_post_error", url=url, err=str(e), attempt=i+1)
            logging.error(f"POST {url} error: {e}\n{traceback.format_exc()}")
            if i + 1 < attempts:
                time.sleep(0.2 * (2 ** i))
    if last_exc:
        raise last_exc

# --- preparse --motivo sin romper argv actual ---
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--motivo", default="", help="Texto para guardar en operaciones.note")
_known, _rest = _pre.parse_known_args()
MOTIVO = _known.motivo or ""
sys.argv = [sys.argv[0]] + _rest

# Console encoding friendly (Windows)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def ts():
    return datetime.now().strftime("%H:%M:%S")

# ----- DB helpers -----
def _db_connect():
    con = sqlite3.connect(DB_NAME, timeout=8, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-8000;")
    con.row_factory = sqlite3.Row
    return con

def db_row(q, p=()):
    with _db_connect() as con:
        return con.execute(q, p).fetchone()

def db_exec(q, p=()):
    with _db_connect() as con:
        con.execute(q, p)
        con.commit()

# ----- Precios Jupiter (USD) -----
JUP_PRICE_URL = "https://price.jup.ag/v6/price?ids={mint}"

def precio_jupiter_usd(mint, timeout=5):
    try:
        t0 = perf_counter()
        r = http_get(JUP_PRICE_URL.format(mint=mint), timeout=timeout)
        r.raise_for_status()
        data = r.json()
        entry = (data or {}).get("data", {}).get(mint)
        px = (entry or {}).get("price")
        if px is None:
            emit("jup_price_none", mint=mint, dt_ms=int((perf_counter()-t0)*1000))
            return None
        f = float(px)
        if f > 0:
            emit("jup_price_ok", mint=mint, price=f, dt_ms=int((perf_counter()-t0)*1000))
            return f
        emit("jup_price_nonpos", mint=mint, dt_ms=int((perf_counter()-t0)*1000))
        return None
    except Exception as e:
        emit("jup_price_error", mint=mint, err=str(e))
        logging.debug(f"precio_jupiter_usd fail: {e}")
        return None

# ----- Precio “vivo” del trailing (DB) -----
def precio_trailing_db(token_mint: str, fresh_secs: int = 10):
    try:
        with _db_connect() as con:
            row = con.execute("""
                SELECT price_usd, updated_at
                FROM precios_live
                WHERE address=?
            """, (token_mint,)).fetchone()
            if not row:
                return None
            px = float(row["price_usd"] or 0.0)
            if px <= 0:
                return None
            try:
                t = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
            age = (datetime.now() - t).total_seconds()
            return px if age <= fresh_secs else None
    except Exception as e:
        logging.debug(f"precio_trailing_db fail: {e}")
        return None

# ----- Balance SPL (raw + ui) -----
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=06d471af-62a0-40d9-867a-50d9aad046e6").strip()

def consultar_balance_raw_and_ui(token_mint: str, timeout=8):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [
            str(wallet_pubkey),
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed", "commitment": "processed"}
        ]
    }
    try:
        t0 = perf_counter()
        r = http_post(HELIUS_RPC_URL, json_body=payload, timeout=timeout)
        emit("helius_balance_http", status=r.status_code, dt_ms=int((perf_counter()-t0)*1000))
        r.raise_for_status()
        vals = (r.json().get("result") or {}).get("value") or []
        for acc in vals:
            info = ((acc.get("account") or {}).get("data") or {}).get("parsed", {}).get("info", {})
            if (info.get("mint") or "") != token_mint:
                continue
            ta = info.get("tokenAmount") or {}
            raw = int(ta.get("amount") or "0")
            dec = int(ta.get("decimals") or 0)
            if raw > 0:
                ui = raw / (10 ** dec if dec else 1)
                emit("balance_found", mint=token_mint, raw=raw, ui=ui, dec=dec)
                return raw, ui, dec
        emit("balance_zero", mint=token_mint)
        return 0, 0.0, None
    except Exception as e:
        emit("helius_balance_error", err=str(e))
        logging.error(f"❌ Error consultando balance: {e}\n{traceback.format_exc()}")
        return 0, 0.0, None

# ===== Cotización hop=1 con fallback a no-direct =====
def _quote_request(input_mint: str, output_mint: str, amount_raw: int,
                   slippage_bps: int, max_accounts: int, timeout_quote: int,
                   direct_only: bool):
    # Importante: restrictIntermediateTokens debe quedar EN TRUE en free tier
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_raw,
        "swapMode": "ExactIn",
        "slippageBps": slippage_bps,
        "onlyDirectRoutes": "true" if direct_only else "false",
        "restrictIntermediateTokens": "true",   # nunca false en free tier
        "maxAccounts": max_accounts
    }
    headers = {"accept": "application/json", "Authorization": f"Bearer {JUPITER_API_KEY}"}
    t0 = perf_counter()
    qr = http_get(QUOTE_URL, headers=headers, params=params, timeout=timeout_quote)
    emit("quote_http", input=input_mint, output=output_mint, status=qr.status_code,
         dt_ms=int((perf_counter()-t0)*1000), max_accounts=max_accounts, direct_only=direct_only)

    if qr.status_code != 200:
        try:
            errj = qr.json()
            ec = (errj or {}).get("errorCode") or (errj or {}).get("error")
            return None, str(ec or f"HTTP_{qr.status_code}")
        except Exception:
            return None, f"HTTP_{qr.status_code}"

    try:
        q = qr.json()
    except Exception as e:
        emit("quote_json_error", err=str(e))
        return None, f"JSON_{e}"

    if "errorCode" in q:
        return None, str(q.get("errorCode") or "NO_ROUTE")

    rp = q.get("routePlan") or []
    if direct_only and len(rp) != 1:
        return None, "NO_ROUTE"
    return q, None

def cotizar_hop1_con_fallback(input_mint: str, output_mint: str, amount_raw: int,
                              slippage_bps: int, max_accounts: int, timeout_quote: int):
    # 1) directo hop=1
    q, err = _quote_request(input_mint, output_mint, amount_raw, slippage_bps, max_accounts, timeout_quote,
                            direct_only=True)
    if q is not None:
        emit("quote_ok_hop1", output=output_mint)
        return q, None

    # 2) no-direct si el error fue ruta inexistente o HTTP 400
    if err and ("NO_ROUTE" in err or "NO_ROUTES_FOUND" in err or "HTTP_400" in err):
        emit("quote_retry_non_direct", output=output_mint, reason=err)
        q2, err2 = _quote_request(input_mint, output_mint, amount_raw, slippage_bps, max_accounts, timeout_quote,
                                  direct_only=False)
        if q2 is not None:
            q2["_non_direct"] = True
            emit("quote_ok_non_direct", output=output_mint, legs=len(q2.get("routePlan") or []))
            return q2, None
        return None, err2 or err

    return None, err or "NO_ROUTE"

# ----- Filtro de ruta -----
def ruta_valida(quote):
    rp = quote.get("routePlan") or []
    # Acepta multi-hop solo si fue obtenido en fallback no-direct
    if len(rp) != 1 and not quote.get("_non_direct"):
        emit("route_invalid", reason="legs", legs=len(rp))
        return False, f"legs={len(rp)}"

    step = (rp[0] or {}) if rp else {}
    swap_info = step.get("swapInfo") or {}
    label = (swap_info.get("label") or "").strip()

    # Directo: permitir más labels. Fallback: no filtrar por label.
    if not quote.get("_non_direct"):
        ok_labels = ("RaydiumCPMM", "Raydium", "RaydiumCLMM", "OrcaWhirlpool", "Whirlpool", "MeteoraDLMM")
        if label not in ok_labels:
            emit("route_invalid", reason="amm", amm=label or "unknown")
            return False, f"amm={label or 'unknown'}"

    try:
        fees_bps = int(step.get("percentFeeBps", 0))
    except Exception:
        fees_bps = 0

    if (not quote.get("_non_direct") and fees_bps > 35) or (quote.get("_non_direct") and fees_bps > 100):
        emit("route_invalid", reason="fees_bps", fees_bps=fees_bps)
        return False, f"fees_bps={fees_bps}"

    try:
        pi = float(quote.get("priceImpactPct") or 0.0)
    except Exception:
        pi = 0.0

    if (not quote.get("_non_direct") and pi > 0.004) or (quote.get("_non_direct") and pi > 0.015):
        emit("route_invalid", reason="price_impact", price_impact=pi)
        return False, f"pi={pi:.5f}"

    try:
        out_amt = int(quote.get("outAmount") or 0)
    except Exception:
        out_amt = 0
    if out_amt <= 0:
        emit("route_invalid", reason="outAmount<=0")
        return False, "outAmount<=0"

    emit("route_valid", amm=label or "n/a", legs=len(rp), fees_bps=fees_bps, price_impact=pi, out_amt=out_amt, non_direct=bool(quote.get("_non_direct")))
    return True, "ok"

# ----- minOut estricto -----
def _calc_min_out(out_amount: int, slippage_bps: int) -> int:
    if out_amount is None or out_amount <= 0:
        return 0
    m = int(out_amount * (1 - slippage_bps / 10_000.0))
    return 1 if m < 1 else m

def construir_y_enviar_swap(quote, wrap_unwrap: bool, timeout_swap: int, slippage_bps: int):
    out_amt = int(quote.get("outAmount") or 0)
    if out_amt <= 0:
        return None, "outAmount<=0"
    min_out = _calc_min_out(out_amt, slippage_bps)
    emit("build_swap", min_out=min_out, slippage_bps=slippage_bps, wrap_unwrap=bool(wrap_unwrap))

    headers = {"accept": "application/json", "Authorization": f"Bearer {JUPITER_API_KEY}"}
    body = {
        "quoteResponse": quote,
        "userPublicKey": str(wallet_pubkey),
        "wrapAndUnwrapSol": wrap_unwrap,
        "dynamicSlippage": False,
        "minOut": min_out,
        "asLegacyTransaction": True,
        "computeUnitPriceMicroLamports": 9000
    }
    t0 = perf_counter()
    sr = http_post(SWAP_URL, headers=headers, json_body=body, timeout=timeout_swap)
    emit("swap_http", status=sr.status_code, dt_ms=int((perf_counter()-t0)*1000))
    if sr.status_code != 200:
        emit("swap_bad_status", status=sr.status_code)
        return None, f"/swap HTTP {sr.status_code}: {(sr.text or '')[:180]}"
    try:
        sj = sr.json()
    except Exception as e:
        emit("swap_json_error", err=str(e))
        return None, f"SWAP_JSON_{e}"
    raw_b64 = sj.get("swapTransaction")
    if not raw_b64:
        emit("swap_missing_tx")
        return None, "swapTransaction vacío"
    try:
        tx_decoded = b64decode(raw_b64)
        tx = Transaction.deserialize(tx_decoded)
        tx.sign(keypair)
    except Exception as e:
        emit("tx_build_error", err=str(e))
        return None, f"tx_build {e}"
    try:
        t0s = perf_counter()
        sent = client.send_raw_transaction(
            tx.serialize(),
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed", max_retries=5)
        )
        dt = int((perf_counter() - t0s) * 1000)
        sig = sent["result"] if isinstance(sent, dict) and "result" in sent else getattr(sent, "value", sent)
        emit("tx_sent_confirmed", sig=str(sig), dt_ms=dt)
        return str(sig), None
    except Exception as e:
        emit("tx_send_error", err=str(e))
        return None, f"send_tx {e}"

def estimar_px_usd_por_token(out_raw: int, output_mint: str, qty_in_ui: float):
    if out_raw <= 0 or qty_in_ui <= 0:
        return 0.0
    out_dec = 6 if output_mint == USDC_MINT else 9
    out_ui = out_raw / (10 ** out_dec)
    if output_mint == USDC_MINT:
        out_usd_value = out_ui
    else:
        sol_px = precio_jupiter_usd(SOL_MINT) or 0.0
        out_usd_value = out_ui * sol_px if sol_px > 0 else 0.0
    return (out_usd_value / qty_in_ui) if out_usd_value > 0 else 0.0

# ----- Swap Jupiter -----
def ejecutar_swap_salida(token_mint: str, slippage_bps: int, max_accounts: int,
                         timeout_quote: int, timeout_swap: int):
    """
    Devuelve dict {'txid': str, 'qty_ui': float, 'route': 'USDC'|'SOL', 'price_out_est': float}
    """
    emit("sell_start", token_mint=token_mint, wallet=str(wallet_pubkey))
    # 1) route_base desde vip_tokens
    row = db_row("SELECT route_base FROM vip_tokens WHERE address=?", (token_mint,))
    prefer = (row["route_base"] if row else None) or "USDC"

    # 2) Balance
    raw, ui, dec = consultar_balance_raw_and_ui(token_mint)
    if raw <= 0:
        logging.error("❌ No hay balance para vender.")
        emit("sell_abort", reason="no_balance")
        return False
    emit("sell_balance", raw=raw, ui=ui, dec=dec, prefer=prefer)

    logging.info(f"[SELL] {token_mint[:6]}… qty={ui:.6f} (raw={raw}) prefer={prefer}")

    # 3) Intento paralelo con fallback interno
    targets = [("USDC", USDC_MINT), ("SOL", SOL_MINT)]
    targets.sort(key=lambda t: 0 if t[0] == prefer else 1)

    futs = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        for name, omint in targets:
            futs[ex.submit(cotizar_hop1_con_fallback, token_mint, omint, raw,
                           slippage_bps, max_accounts, timeout_quote)] = (name, omint)

        best = None
        errs = {}
        for f in as_completed(futs):
            name, omint = futs[f]
            q, err = f.result()
            if q is not None:
                emit("quote_ok", route=name, output_mint=omint, non_direct=bool(q.get("_non_direct")))
                if name == prefer and best is None:
                    best = (name, omint, q)
                elif best is None:
                    best = (name, omint, q)
            else:
                errs[name] = err or "err"
                emit("quote_fail", route=name, error=str(err))

    # Fallback explícito a la alternativa si nada elegido
    if best is None and errs:
        alt = [t for t in targets if t[0] != prefer]
        if alt:
            name, omint = alt[0]
            q, err = cotizar_hop1_con_fallback(token_mint, omint, raw, slippage_bps, max_accounts, timeout_quote)
            if q is not None:
                best = (name, omint, q)

    if best is None:
        logging.error(f"⚠️ Sin ruta (incluyendo fallback). Detalle: {errs or 'n/a'}")
        emit("sell_abort", reason="no_route_all", detail=str(errs or "n/a"))
        return False

    route, output_mint, quote = best

    # 3.1) Filtro de ruta
    ok, why = ruta_valida(quote)
    if not ok:
        logging.error(f"⚠️ Ruta inválida en salida: {why}")
        emit("sell_abort", reason="route_invalid", detail=why)
        return False
    emit("route_selected", route=route, output_mint=output_mint, non_direct=bool(quote.get("_non_direct")))

    out_raw = int(quote.get("outAmount") or 0)
    est_price_out_usd_per_token = estimar_px_usd_por_token(out_raw, output_mint, ui)
    emit("sell_estimation", out_raw=out_raw, price_out_est=est_price_out_usd_per_token)

    # 4) Construir swap y enviar
    wrap_unwrap = True
    txid, err = construir_y_enviar_swap(quote, wrap_unwrap, timeout_swap, slippage_bps)
    if err:
        logging.error(f"❌ swap error: {err}")
        emit("sell_abort", reason="swap_error", detail=err)
        return False

    logging.info(f"✅ TXID: {txid}")
    emit("sell_done", txid=str(txid), route=route)
    return {
        "txid": txid,
        "qty_ui": float(ui),
        "route": route,
        "price_out_est": float(est_price_out_usd_per_token)
    }

# ----- Registrar salida en 'operaciones' -----
def registrar_salida(token_mint: str,
                     route: str,
                     qty_ui: float | None,
                     price_out_est: float | None = None,
                     motivo: str = ""):
    try:
        with _db_connect() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, price_entrada FROM operaciones
                WHERE address=? AND (hora_salida IS NULL OR TRIM(hora_salida)='')
                ORDER BY id DESC LIMIT 1
            """, (token_mint,))
            row = cur.fetchone()
            if not row:
                logging.info("ℹ️ No hay operación abierta para cerrar.")
                emit("reg_salida_skip", reason="no_open_operation")
                return
            op_id, price_in = row[0], float(row[1] or 0.0)

        fuente_price = None
        price_out = precio_trailing_db(token_mint, fresh_secs=10)
        if price_out and price_out > 0:
            fuente_price = "trailing"
        if not price_out or price_out <= 0:
            price_out = float(price_out_est or 0.0)
            if price_out > 0:
                fuente_price = "estimado_swap"
        if not price_out or price_out <= 0:
            jpx = precio_jupiter_usd(token_mint)
            if jpx and jpx > 0:
                price_out = jpx
                fuente_price = "jup_price"

        if price_in > 0 and price_out and price_out > 0:
            profit_pct = round((price_out / price_in - 1) * 100, 2)
            profit_usd = round((qty_ui or 0.0) * (price_out - price_in), 4) if (qty_ui and qty_ui > 0) else 0.0
        else:
            profit_pct = 0.0
            profit_usd = 0.0

        nota = f"swap automático ({route})"
        if motivo:
            nota += f" | {motivo}"
        if fuente_price:
            nota += f" | px={fuente_price}"

        with _db_connect() as con:
            cur = con.cursor()
            cur.execute("""
                UPDATE operaciones SET
                    hora_salida   = ?,
                    price_salida  = ?,
                    profit_percent= ?,
                    profit_usd    = ?,
                    note          = ?
                WHERE id = ?
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                round(price_out or 0.0, 6),
                profit_pct,
                profit_usd,
                nota,
                op_id
            ))
            con.commit()
        logging.info("📒 Operación actualizada en DB.")
        emit("reg_salida_ok", op_id=op_id, price_out=price_out, profit_pct=profit_pct, profit_usd=profit_usd, fuente=fuente_price or "n/a")
    except Exception as e:
        emit("reg_salida_error", err=str(e))
        logging.error(f"⚠️ Error registrando operación: {e}\n{traceback.format_exc()}")

# ----- CLI -----
def parse_args():
    p = argparse.ArgumentParser(description="swap_real — salida TOKEN -> (USDC|SOL) hop=1 + fallback")
    p.add_argument("token_mint", help="Mint del token a vender")
    p.add_argument("action", nargs="?", choices=["sell"], help="Compatibilidad: se ignora si se pasa 'sell'")
    p.add_argument("--motivo", default=MOTIVO, help="Nota para operaciones.note")
    p.add_argument("--slippage-bps", type=int, default=30, help="Slippage en bps (default 30 = 0.30%)")
    p.add_argument("--max-accounts", type=int, default=48, help="maxAccounts para Jupiter (default 48)")
    p.add_argument("--timeout-quote", type=int, default=5, help="timeout GET /quote (s)")
    p.add_argument("--timeout-swap", type=int, default=10, help="timeout POST /swap (s)")
    return p.parse_args()

def main():
    if len(sys.argv) < 2:
        print("Uso: python swap_real.py <token_mint> [sell] [--motivo TEXTO] [--slippage-bps 20] [--max-accounts 48] [--timeout-quote 5] [--timeout-swap 10]")
        sys.exit(2)
    args = parse_args()
    token_mint = args.token_mint.strip()
    emit("cli_start", token_mint=token_mint, motivo=args.motivo, slippage_bps=args.slippage_bps, max_accounts=args.max_accounts)

    try:
        txinfo = ejecutar_swap_salida(
            token_mint,
            slippage_bps=args.slippage_bps,
            max_accounts=args.max_accounts,
            timeout_quote=args.timeout_quote,
            timeout_swap=args.timeout_swap
        )
        if not txinfo:
            logging.error("❌ Swap no ejecutado.")
            emit("cli_end", ok=False)
            sys.exit(1)
        registrar_salida(
            token_mint,
            txinfo.get("route"),
            txinfo.get("qty_ui"),
            txinfo.get("price_out_est"),
            args.motivo or ""
        )
        emit("cli_end", ok=True)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        emit("cli_exception", err=str(e))
        logging.error(f"❌ Error fatal: {e}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    main()



























