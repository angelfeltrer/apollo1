# compra_swap_usdc.py — VIP-only: USDC->TOKEN hop=1 (ExactIn), optimizado + observabilidad
# Mantiene:
# - Intentos ma={64,56,48}, hop=1 real, filtro label, minOut estricto, dynamicSlippage=False
# - quote timeout=6s, swap timeout=12s, fee ≤45 bps, TX ≤1300B/≤1800b64
# - HTTP keep-alive, retries + RPC fallback
# Añadido:
# - Telemetría granular: inicio, quote_ok/err, filtros, swap_req/err, tx_sent, excepción, métricas de tiempo

import sys, os, json, time, sqlite3
import requests
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor, as_completed
from solana.transaction import Transaction
from solana.rpc.types import TxOpts
from solana.rpc.api import Client

from config import (
    client, keypair, wallet_pubkey,
    JUPITER_API_KEY, QUOTE_URL, SWAP_URL,
    DB_NAME, AMOUNT_LAMPORTS as CFG_AMOUNT_LAMPORTS
)

# ===== Telemetry (no-op si no está disponible) =====
try:
    from telemetry import emit  # def emit(event: str, **fields): ...
except Exception:
    def emit(event: str, **fields):  # fallback
        pass

# ===== DEBUG =====
DEBUG = True
def dbg(*a):
    if DEBUG:
        print("🧪", *a)

# ===== HTTP session (keep-alive) =====
_SES = requests.Session()
try:
    from requests.adapters import HTTPAdapter
    _SES.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=12, max_retries=0))
    _SES.mount("http://",  HTTPAdapter(pool_connections=4, pool_maxsize=12, max_retries=0))
except Exception:
    pass
_SES.headers.update({"Accept": "application/json", "Connection": "keep-alive"})

def http_get(url, headers=None, params=None, timeout=10):
    r = _SES.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
    dbg("GET", r.status_code, url)
    if r.status_code >= 400:
        dbg("RESP", (r.text or "")[:700])
    return r

def http_post(url, headers=None, json_body=None, timeout=15):
    r = _SES.post(url, headers=headers or {}, json=json_body or {}, timeout=timeout)
    dbg("POST", r.status_code, url)
    if r.status_code >= 400:
        dbg("RESP", (r.text or "")[:700])
    return r

# --- Envío robusto con retries y RPCs de respaldo ---
FALLBACK_RPC_URLS = ["https://api.mainnet-beta.solana.com"]

def _rpc_name(cli: Client):
    for attr in ("endpoint_uri", "endpoint"):
        v = getattr(cli, attr, None)
        if v: return v
    prov = getattr(cli, "_provider", None)
        # pragma: no cover
    if prov is not None:
        return getattr(prov, "endpoint_uri", "unknown")
    return "unknown"

def send_with_retries(tx_bytes, primary_client: Client, label="[SEND]"):
    opts_list = [
        TxOpts(skip_preflight=False, preflight_commitment="confirmed", max_retries=2),
        TxOpts(skip_preflight=True,  preflight_commitment="processed", max_retries=4),
    ]
    clients = [primary_client] + [Client(u) for u in FALLBACK_RPC_URLS]
    last_err = None
    for cli in clients:
        for opts in opts_list:
            try:
                print(f"{label} rpc={_rpc_name(cli)} skip_preflight={opts.skip_preflight}")
                return cli.send_raw_transaction(tx_bytes, opts=opts)
            except Exception as e:
                last_err = e
                emit("tx_send_error_try", rpc=_rpc_name(cli), skip_preflight=bool(opts.skip_preflight), err=str(e))
                time.sleep(0.20)
    if last_err:
        raise last_err

# --- Guard-rails tamaño/fees ---
MAX_RAW_BYTES = 1300
MAX_B64_LEN   = 1800
MAX_FEE_BPS   = 45

# --- Filtro de labels no deseados (defensivo) ---
_BANNED_LABEL_SUBSTR = (
    "meteora", "dlmm", "stable", "openbook",
    "creon", "vault", "stake", "lend", "margin"
)

def _label_ok(quote_json: dict) -> bool:
    try:
        rp = quote_json.get("routePlan") or []
        if not rp: return False
        if len(rp) != 1: return False
        lbl = (rp[0].get("label") or "").lower()
        return not any(s in lbl for s in _BANNED_LABEL_SUBSTR)
    except Exception:
        return False

# Monto por defecto (override por argv)
AMOUNT_LAMPORTS = CFG_AMOUNT_LAMPORTS
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def ejecutar_compra_usdc(output_mint: str):
    t0 = time.perf_counter()
    emit("buy_usdc_start", wallet=str(wallet_pubkey), output_mint=output_mint, amount_lamports=int(AMOUNT_LAMPORTS))
    try:
        print(f"\n[BUY][USDC] Wallet: {wallet_pubkey}")
        print(f"[BUY][USDC] USDC -> {output_mint} por {AMOUNT_LAMPORTS} lamports (USDC 6 dec)")
        dbg("PY:", sys.executable, "| CWD:", os.getcwd())

        def obtener_cotizacion(max_accounts: int):
            params = {
                "inputMint": USDC_MINT,
                "outputMint": output_mint,
                "amount": AMOUNT_LAMPORTS,       # entero on-chain (USDC*10^6)
                "swapMode": "ExactIn",
                "slippageBps": 30,               # 0.3%
                "onlyDirectRoutes": "true",      # hop=1
                "restrictIntermediateTokens": "true",
                "maxAccounts": max_accounts,
            }
            headers = {
                "accept": "application/json",
                "Authorization": f"Bearer {JUPITER_API_KEY}"
            }
            tq0 = time.perf_counter()
            r = http_get(QUOTE_URL, headers=headers, params=params, timeout=6)  # quote 6s
            emit("quote_http", ma=max_accounts, status=int(r.status_code), dt_ms=int((time.perf_counter()-tq0)*1000))
            return r

        headers_swap = {
            "accept": "application/json",
            "Authorization": f"Bearer {JUPITER_API_KEY}"
        }

        # Intentos reducidos y priorizados
        candidatos_ma = [64, 56, 48]
        swap_json = None
        ruta_info = None  # (ma, raw_len, b64_len)

        # Paraleliza y toma la primera ruta válida
        with ThreadPoolExecutor(max_workers=len(candidatos_ma)) as ex:
            fut_map = {ex.submit(obtener_cotizacion, ma): ma for ma in candidatos_ma}
            for fut in as_completed(fut_map):
                ma = fut_map[fut]
                try:
                    quote_res = fut.result()
                except Exception as e:
                    emit("quote_exc", ma=ma, err=str(e))
                    continue

                if quote_res.status_code != 200:
                    print(f"[BUY][USDC] Quote HTTP {quote_res.status_code} (ma={ma})")
                    emit("quote_bad_status", ma=ma, status=int(quote_res.status_code))
                    continue

                try:
                    quote = quote_res.json()
                except Exception as je:
                    emit("quote_json_error", ma=ma, err=str(je))
                    dbg("QUOTE JSON error:", je, "| body:", (quote_res.text or "")[:400])
                    continue

                route = quote.get("routePlan") or []
                if not route:
                    emit("route_missing", ma=ma)
                    dbg("SIN RUTA en QUOTE (routePlan vacío).")
                    continue

                # hop=1 real
                if len(route) != 1:
                    emit("route_filtered_hop", ma=ma, legs=len(route))
                    dbg(f"Ruta con {len(route)} legs. Saltando…")
                    continue

                # filtro label
                if not _label_ok(quote):
                    lbl = (route[0].get("label") or "")
                    emit("route_filtered_label", ma=ma, label=lbl)
                    dbg("Ruta filtrada por label.")
                    continue

                # fees guard-rail
                try:
                    fees = sum(int(step.get("percentFeeBps", 0)) for step in route)
                except Exception:
                    fees = 0
                if fees > MAX_FEE_BPS:
                    emit("route_filtered_fee", ma=ma, fee_bps=int(fees))
                    print(f"[BUY][USDC] Ruta cara: {fees} bps. Probando otra…")
                    continue

                # min_out estricto desde quote
                min_out_raw = int(quote.get("otherAmountThreshold") or quote.get("outAmountWithSlippage") or 0)
                if min_out_raw <= 0:
                    emit("route_filtered_minout_missing", ma=ma)
                    dbg("Quote sin otherAmountThreshold/outAmountWithSlippage. Skip.")
                    continue

                emit("route_selected", ma=ma, fee_bps=int(fees), min_out_raw=int(min_out_raw),
                     label=(route[0].get("label") or ""))

                # Construir swap (timeout 12s, sin dynamicSlippage)
                swap_body = {
                    "quoteResponse": quote,
                    "userPublicKey": str(wallet_pubkey),
                    "wrapAndUnwrapSol": False,               # USDC-only
                    "useSharedAccounts": True,
                    "asLegacyTransaction": True,
                    "dynamicSlippage": False,                # ← clave
                    "computeUnitPriceMicroLamports": 9000
                }
                ts0 = time.perf_counter()
                swap_res = http_post(SWAP_URL, headers=headers_swap, json_body=swap_body, timeout=12)
                emit("swap_http", ma=ma, status=int(swap_res.status_code), dt_ms=int((time.perf_counter()-ts0)*1000))
                if swap_res.status_code != 200:
                    print(f"[BUY][USDC] /swap {swap_res.status_code}: {swap_res.text[:160]}")
                    emit("swap_bad_status", ma=ma, status=int(swap_res.status_code))
                    continue

                try:
                    sj = swap_res.json()
                except Exception as je:
                    emit("swap_json_error", ma=ma, err=str(je))
                    dbg("SWAP JSON error:", je, "| body:", (swap_res.text or "")[:400])
                    continue

                if "error" in sj:
                    emit("swap_sim_error", ma=ma, detail=json.dumps(sj)[:600])
                    dbg("SWAP error obj:", json.dumps(sj)[:700])
                    continue

                raw_b64 = sj.get("swapTransaction")
                if not raw_b64:
                    emit("swap_missing_tx", ma=ma)
                    dbg("SWAP sin 'swapTransaction'")
                    continue

                # Tamaño TX
                try:
                    tx_decoded = b64decode(raw_b64)
                    raw_len = len(tx_decoded)
                    b64_len = len(raw_b64)
                    dbg(f"TX size raw={raw_len}B b64={b64_len}")
                except Exception:
                    raw_len = None
                    b64_len = None

                if (raw_len is not None and raw_len > MAX_RAW_BYTES) or (b64_len is not None and b64_len > MAX_B64_LEN):
                    emit("route_filtered_size", ma=ma, raw_len=raw_len or -1, b64_len=b64_len or -1)
                    print(f"[BUY][USDC] TX grande (raw≈{raw_len}B, b64≈{b64_len}). Buscando ruta más compacta…")
                    continue

                # Primer resultado válido
                swap_json = sj
                ruta_info = (ma, raw_len, b64_len)
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pass
                break

        if not swap_json:
            emit("buy_usdc_abort", reason="no_valid_route")
            print("[BUY][USDC] SIN RUTAS válidas (hop=1/label/fees/size/min_out).")
            return False

        ma, raw_len, b64_len = ruta_info
        print(f"[BUY][USDC] Ruta OK (ma={ma}) | raw≈{raw_len}B b64≈{b64_len}")
        emit("route_final", ma=ma, raw_len=int(raw_len or -1), b64_len=int(b64_len or -1))

        # Firmar y enviar con retries/fallback
        print("[BUY][USDC] Firmando y enviando…")
        tx_decoded = b64decode(swap_json["swapTransaction"])
        transaction = Transaction.deserialize(tx_decoded)
        transaction.sign(keypair)
        raw = transaction.serialize()

        try:
            t_send0 = time.perf_counter()
            txid = send_with_retries(raw, client, label="[BUY][USDC] SEND")
            sig = getattr(txid, "value", txid)
            print(f"[BUY][USDC] TXID: {sig}")
            emit("tx_sent", sig=str(sig), dt_ms=int((time.perf_counter()-t_send0)*1000))
            emit("buy_usdc_done", ok=True, total_ms=int((time.perf_counter()-t0)*1000))
            return str(sig)
        except Exception as e:
            emit("tx_send_failed", err=str(e))
            print(f"[BUY][USDC] Error de envío (reintentos agotados): {e}")
            emit("buy_usdc_done", ok=False, total_ms=int((time.perf_counter()-t0)*1000))
            return False

    except Exception as e:
        emit("buy_usdc_exception", err=str(e))
        print(f"[BUY][USDC] Excepción: {e}")
        emit("buy_usdc_done", ok=False, total_ms=int((time.perf_counter()-t0)*1000))
        return False


if __name__ == "__main__":
    # Uso: python compra_swap_usdc.py <output_mint> [monto_usdc]
    if len(sys.argv) < 2:
        print("Uso: python compra_swap_usdc.py <output_mint> [monto_usdc]")
        sys.exit(2)

    output_mint = sys.argv[1].strip()

    # Monto opcional en USDC -> lamports (6 dec)
    AMOUNT_LAMPORTS = CFG_AMOUNT_LAMPORTS
    if len(sys.argv) >= 3:
        try:
            amount_usdc = float(sys.argv[2])
            AMOUNT_LAMPORTS = int(round(amount_usdc * 1_000_000))
            print(f"[BUY][USDC] Override de monto: {amount_usdc} USDC -> {AMOUNT_LAMPORTS} lamports")
        except Exception as e:
            print(f"No se pudo interpretar monto_usdc '{sys.argv[2]}': {e}")
            sys.exit(2)

    ok = ejecutar_compra_usdc(output_mint)
    sys.exit(0 if ok else 1)
# telemetry.py — Telemetría ligera con SQLite + contexto Timer/span














