# compra_swap_sol.py — optimizado sin Jupiter Pro (latencia baja)
# - Birdeye 3s + fallback Jupiter Lite 1.2s con backoff
# - Reuso de conexión HTTP (keep-alive)
# - Cotizaciones en paralelo: (direct,ma) = (True,64),(True,56),(False,64) con timeout 6s
# - /swap timeout 12s
# - Envío con retries reducidos y espera 0.2s
# - Observabilidad: telemetry.emit (no-op si no existe)

import sys
import os
import json
import time
import sqlite3
import requests
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter

from solana.transaction import Transaction
from solana.rpc.types import TxOpts
from solana.rpc.api import Client

from config import (
    client, keypair, wallet_pubkey,
    JUPITER_API_KEY, QUOTE_URL, SWAP_URL,
    DB_NAME, API_KEY
)

# --- Telemetry (no-op fallback) ---
try:
    from telemetry import emit  # type: ignore
except Exception:
    def emit(event, **kw):  # no-op
        pass

# --- Fix encoding Windows ---
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
PRICE_FALLBACK_URL = "https://lite-api.jup.ag/price/v3"

# ===== DEBUG =====
DEBUG = True
def dbg(*a):
    if DEBUG:
        print("🧪", *a)

# ===== HTTP session (keep-alive) =====
_SES = requests.Session()
_SES.headers.update({"Accept": "application/json", "Connection": "keep-alive"})

def http_get(url, headers=None, params=None, timeout=10):
    t0 = perf_counter()
    r = _SES.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
    dt = int((perf_counter() - t0) * 1000)
    emit("http_get", url=url, status=r.status_code, dt_ms=dt)
    dbg("GET", r.status_code, url)
    if r.status_code >= 400:
        dbg("RESP", (r.text or "")[:700])
    return r

def http_post(url, headers=None, json_body=None, timeout=20):
    t0 = perf_counter()
    r = _SES.post(url, headers=headers or {}, json=json_body or {}, timeout=timeout)
    dt = int((perf_counter() - t0) * 1000)
    emit("http_post", url=url, status=r.status_code, dt_ms=dt)
    dbg("POST", r.status_code, url)
    if r.status_code >= 400:
        dbg("RESP", (r.text or "")[:700])
    return r

# ===== Precio SOL USD =====
def get_sol_price_usd(timeout=3):
    """Precio de SOL en USD desde Birdeye con timeout reducido."""
    url = "https://public-api.birdeye.so/defi/v3/token/market-data"
    headers = {"accept": "application/json", "x-chain": "solana", "X-API-KEY": API_KEY}
    t0 = perf_counter()
    r = http_get(url, headers=headers, params={"address": SOL_MINT}, timeout=timeout)
    dt = int((perf_counter() - t0) * 1000)
    try:
        data = r.json()
        p = float((data or {}).get("data", {}).get("price", 0))
        if p and p > 0:
            emit("sol_price_ok", source="birdeye", price=p, dt_ms=dt, status=r.status_code)
            return p
        emit("sol_price_zero", source="birdeye", dt_ms=dt, status=r.status_code)
        return None
    except Exception as e:
        emit("sol_price_json_error", source="birdeye", err=str(e), dt_ms=dt, status=r.status_code)
        dbg("SOL price json err:", e, "| body:", (r.text or "")[:400])
        return None

def get_sol_price_usd_fallback(timeout=1.2):
    """Fallback rápido a Jupiter Price API v3 lite con backoff corto."""
    url = f"{PRICE_FALLBACK_URL}?ids={SOL_MINT}"
    try:
        t0 = perf_counter()
        r = http_get(url, timeout=timeout)
        if r.status_code == 429:
            emit("price_lite_429", url=url)
            time.sleep(0.6)
            r = http_get(url, timeout=timeout)
        j = r.json()
        p = (j.get("data", {}) or {}).get(SOL_MINT, {}).get("usdPrice")
        p = float(p) if p is not None else None
        if p and p > 0:
            emit("sol_price_ok", source="jup_lite", price=p, status=r.status_code, dt_ms=int((perf_counter()-t0)*1000))
            return p
        emit("sol_price_zero", source="jup_lite", status=r.status_code)
        return None
    except Exception as e:
        emit("sol_price_error", source="jup_lite", err=str(e))
        dbg("Jupiter fallback err:", e)
        return None

# ===== Envío robusto con retries y RPCs de respaldo =====
FALLBACK_RPC_URLS = ["https://api.mainnet-beta.solana.com"]

def _rpc_name(cli: Client):
    for attr in ("endpoint_uri", "endpoint"):
        v = getattr(cli, attr, None)
        if v:
            return v
    prov = getattr(cli, "_provider", None)
    if prov is not None:
        return getattr(prov, "endpoint_uri", "unknown")
    return "unknown"

def send_with_retries(tx_bytes, primary_client: Client, label="[SEND]"):
    """
    Primero con preflight, luego sin preflight.
    Retries reducidos y espera 0.2s.
    """
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
                t0 = perf_counter()
                sig = cli.send_raw_transaction(tx_bytes, opts=opts)
                dt = int((perf_counter() - t0) * 1000)
                emit("tx_sent", rpc=_rpc_name(cli), skip_preflight=bool(opts.skip_preflight), dt_ms=dt)
                return sig
            except Exception as e:
                last_err = e
                emit("tx_send_error_try", rpc=_rpc_name(cli), skip_preflight=bool(opts.skip_preflight), err=str(e))
                time.sleep(0.20)
    if last_err:
        emit("tx_send_failed", err=str(last_err))
        raise last_err

# ===== Lógica principal =====
def ejecutar_compra_sol(output_mint, amount_usdc: float):
    emit("buy_sol_start", output_mint=output_mint, amount_usdc=amount_usdc, wallet=str(wallet_pubkey))
    try:
        print(f"\n[BUY][SOL ] Wallet: {wallet_pubkey}")
        print(f"[BUY][SOL ] Iniciando swap SOL -> {output_mint} por {amount_usdc} USDC (convertido a SOL)")
        dbg("PY:", sys.executable, "| CWD:", os.getcwd())
        dbg("QUOTE_URL:", QUOTE_URL)
        dbg("SWAP_URL:", SWAP_URL)

        # Precio SOL: Birdeye 3s → fallback Jupiter Lite 1.2s
        p0 = perf_counter()
        precio_sol = get_sol_price_usd(timeout=3) or get_sol_price_usd_fallback(timeout=1.2)
        emit("sol_price_resolved", price=precio_sol, dt_ms=int((perf_counter()-p0)*1000))
        dbg("SOL/USD:", precio_sol)
        if not precio_sol or precio_sol <= 0:
            print("[BUY][SOL ] ❌ NO PRICE SOL")
            emit("buy_sol_abort", reason="no_price")
            return False

        sol_amount = float(amount_usdc) / float(precio_sol)
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        emit("amount_from_usd", amount_usdc=amount_usdc, sol_amount=sol_amount, lamports=lamports)
        dbg("sol_amount:", sol_amount, "lamports:", lamports)
        if lamports <= 0:
            print("[BUY][SOL ] ❌ MONTO SOL INVÁLIDO")
            emit("buy_sol_abort", reason="invalid_lamports")
            return False

        headers_quote = {"accept": "application/json", "Authorization": f"Bearer {JUPITER_API_KEY}"}
        headers_swap  = headers_quote

        def obtener_cotizacion(only_direct, max_accounts):
            params = {
                "inputMint": SOL_MINT,
                "outputMint": output_mint,
                "amount": lamports,                # SOL en lamports
                "slippageBps": 30,                 # 0.3%
                "onlyDirectRoutes": str(only_direct).lower(),
                "restrictIntermediateTokens": "true",
                "maxAccounts": max_accounts,
                "swapMode": "ExactIn",
            }
            dbg(f"QUOTE params: direct={only_direct} ma={max_accounts} amount(lamports)={lamports}")
            t0 = perf_counter()
            res = http_get(QUOTE_URL, headers=headers_quote, params=params, timeout=6)
            emit("quote_http", direct=bool(only_direct), max_accounts=max_accounts, status=res.status_code, dt_ms=int((perf_counter()-t0)*1000))
            return res

        # Intentos en paralelo: 3 combinaciones rápidas
        intentos = [(True, 64), (True, 56), (False, 64)]

        swap_json = None
        MAX_TX_BYTES = 1400

        with ThreadPoolExecutor(max_workers=len(intentos)) as ex:
            future_map = {ex.submit(obtener_cotizacion, od, ma): (od, ma) for od, ma in intentos}
            for fut in as_completed(future_map):
                only_direct, max_accounts = future_map[fut]
                try:
                    quote_res = fut.result()
                except Exception as e:
                    emit("quote_exc", direct=bool(only_direct), max_accounts=max_accounts, err=str(e))
                    continue

                if quote_res.status_code != 200:
                    print(f"[BUY][SOL ] Quote HTTP {quote_res.status_code} ({'direct' if only_direct else 'path'} ma={max_accounts})")
                    emit("quote_bad_status", direct=bool(only_direct), max_accounts=max_accounts, status=quote_res.status_code)
                    continue

                try:
                    quote = quote_res.json()
                except Exception as je:
                    emit("quote_json_error", direct=bool(only_direct), max_accounts=max_accounts, err=str(je))
                    dbg("QUOTE JSON error:", je, "| body:", (quote_res.text or "")[:400])
                    continue

                if not quote or not quote.get("routePlan"):
                    emit("route_missing", direct=bool(only_direct), max_accounts=max_accounts)
                    dbg("SIN RUTA en QUOTE (routePlan vacío).")
                    continue

                # Construir swap con fee prioridad; timeout 12s
                swap_body = {
                    "quoteResponse": quote,
                    "userPublicKey": str(wallet_pubkey),
                    "wrapAndUnwrapSol": True,
                    "dynamicSlippage": True,
                    "asLegacyTransaction": True,
                    "computeUnitPriceMicroLamports": 10000
                }
                dbg("POST /swap…")
                t0s = perf_counter()
                swap_res = http_post(SWAP_URL, headers=headers_swap, json_body=swap_body, timeout=12)
                emit("swap_http", direct=bool(only_direct), max_accounts=max_accounts, status=swap_res.status_code, dt_ms=int((perf_counter()-t0s)*1000))
                if swap_res.status_code != 200:
                    print(f"[BUY][SOL ] /swap {swap_res.status_code}: {swap_res.text[:160]}")
                    emit("swap_bad_status", status=swap_res.status_code)
                    continue

                try:
                    sj = swap_res.json()
                except Exception as je:
                    emit("swap_json_error", err=str(je))
                    dbg("SWAP JSON error:", je, "| body:", (swap_res.text or "")[:400])
                    continue

                if "error" in sj:
                    emit("swap_sim_error", error_obj=str(sj.get("error")))
                    dbg("SWAP error obj:", json.dumps(sj)[:700])
                    continue

                raw_b64 = sj.get("swapTransaction")
                if not raw_b64:
                    emit("swap_missing_tx")
                    dbg("SWAP sin 'swapTransaction'")
                    continue

                # Tamaño TX
                try:
                    approx_bytes = len(b64decode(raw_b64))
                except Exception:
                    approx_bytes = None

                if approx_bytes is not None and approx_bytes > MAX_TX_BYTES:
                    print(f"[BUY][SOL ] TX grande (~{approx_bytes} bytes). Probando otra ruta…")
                    emit("route_filtered_size", tx_bytes=approx_bytes, limit=MAX_TX_BYTES)
                    continue

                swap_json = sj
                emit("route_selected", direct=bool(only_direct), max_accounts=max_accounts, tx_bytes=approx_bytes)
                print(f"[BUY][SOL ] Ruta OK (direct={only_direct}, ma={max_accounts}) | tx≈{approx_bytes or '??'} bytes")
                break  # primera ruta válida

        if not swap_json:
            print("[BUY][SOL ] NO TIENE NINGUNA RUTA")
            emit("buy_sol_abort", reason="no_route")
            return False

        # Firmar y enviar con retries/fallback
        print("[BUY][SOL ] Firmando y enviando transacción…")
        try:
            tx_decoded = b64decode(swap_json["swapTransaction"])
            transaction = Transaction.deserialize(tx_decoded)
            transaction.sign(keypair)
            raw = transaction.serialize()
        except Exception as e:
            emit("tx_build_error", err=str(e))
            print(f"[BUY][SOL ] Error construyendo TX: {e}")
            return False

        try:
            t0 = perf_counter()
            txid = send_with_retries(raw, client, label="[BUY][SOL ] SEND")
            dt = int((perf_counter() - t0) * 1000)
            sig = getattr(txid, "value", txid)
            emit("tx_confirmed", sig=str(sig), dt_ms=dt)
            print(f"[BUY][SOL ] TXID: {sig}")
        except Exception as e:
            emit("tx_send_failed_final", err=str(e))
            print(f"[BUY][SOL ] Excepción en envío (reintentos agotados): {e}")
            return False

        # Marcar estado en DB
        try:
            with sqlite3.connect(DB_NAME) as conn:
                conn.execute("""
                    UPDATE premiun_tokens
                    SET estado = '🔂 operando'
                    WHERE address = ?
                """, (output_mint,))
                conn.commit()
            emit("db_update_estado", address=output_mint, estado="🔂 operando")
        except Exception as e:
            emit("db_update_error", err=str(e))

        emit("buy_sol_done", output_mint=output_mint)
        return str(sig)

    except Exception as e:
        emit("buy_sol_exception", err=str(e))
        print(f"[BUY][SOL ] Excepción: {e}")
        return False


if __name__ == "__main__":
    # Uso: python compra_swap_sol.py <output_mint> [monto_usdc]
    if len(sys.argv) < 2:
        print("Uso: python compra_swap_sol.py <output_mint> [monto_usdc]")
        sys.exit(2)

    output_mint = sys.argv[1].strip()
    amount_usdc = 0.25  # default de prueba
    if len(sys.argv) >= 3:
        try:
            amount_usdc = float(sys.argv[2])
            print(f"[BUY][SOL ] Override de monto: {amount_usdc} USDC")
        except Exception as e:
            print(f"No se pudo interpretar monto_usdc '{sys.argv[2]}': {e}")
            sys.exit(2)

    ok = ejecutar_compra_sol(output_mint, amount_usdc)
    sys.exit(0 if ok else 1)
# telemetry.py — Telemetría ligera con SQLite + contexto Timer/span












