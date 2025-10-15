# salida_forzada.py — TOKEN -> SOL forzado (firma local con solana-py; asLegacyTransaction)
# Uso:
#   python salida_forzada.py <TOKEN_MINT> --max-intentos 6 --delay 3 --op-id 123 --debug --slippage-plan 30,60,100,150,200,300
# Notas:
#   - No cambia la columna 'estado' en DB.
#   - Guarda siempre los txids aunque la confirmación demore.
#   - Firma la transacción de Jupiter localmente (legacy).

import os, sys, time, json, argparse, sqlite3, subprocess, random, traceback
from base64 import b64decode
import requests
from decimal import Decimal, getcontext
from time import perf_counter

from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts, TxOpts
from solana.keypair import Keypair as PyKeypair
from solana.transaction import Transaction

# === Telemetry (no-op fallback) ===
try:
    from telemetry import emit  # type: ignore
except Exception:
    def emit(event, **kw):  # no-op
        pass

# === Config centralizada ===
from config import (
    client,              # solana.rpc.api.Client(...)
    wallet_pubkey,       # str o PublicKey
    JUPITER_API_KEY,
    QUOTE_URL,           # "https://quote-api.jup.ag/v6/quote"
    SWAP_URL,            # "https://quote-api.jup.ag/v6/swap"
    DB_NAME              # p.ej. "goodt.db"
)

# (opcional) si tienes keypair ya cargado en config, se usa
CFG_KP = None
try:
    from config import keypair as _cfg_kp
    _ = str(_cfg_kp.public_key)
    CFG_KP = _cfg_kp
except Exception:
    CFG_KP = None

try:
    from config import API_KEY as BIRDEYE_API_KEY
except Exception:
    try:
        from config import BIRDEYE_API_KEY
    except Exception:
        BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

SOL_MINT = "So11111111111111111111111111111111111111112"
getcontext().prec = 50

# ---------- HTTP helpers (con telemetría) ----------
def hget(url, headers=None, params=None, timeout=10):
    t0 = perf_counter()
    try:
        r = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
        emit("http_get", url=url, status=getattr(r, "status_code", None), dt_ms=int((perf_counter()-t0)*1000))
        return r
    except requests.RequestException as e:
        emit("http_get_error", url=url, err=str(e))
        return None

def hpost(url, headers=None, json_body=None, timeout=20):
    t0 = perf_counter()
    try:
        r = requests.post(url, headers=headers or {}, json=json_body or {}, timeout=timeout)
        emit("http_post", url=url, status=getattr(r, "status_code", None), dt_ms=int((perf_counter()-t0)*1000))
        return r
    except requests.RequestException as e:
        emit("http_post_error", url=url, err=str(e))
        return None

# ---------- RPC utils ----------
def _value_list(resp):
    if hasattr(resp, "value"): return resp.value
    if isinstance(resp, dict): return (resp.get("result", {}) or {}).get("value", [])
    return []

def _item_pubkey(it):
    if isinstance(it, dict): return it.get("pubkey") or ((it.get("account", {}) or {}).get("pubkey"))
    return str(getattr(it, "pubkey", "")) or None

def _safe_get_token_account_balance(cli: Client, pubkey_str: str):
    try:
        bal = cli.get_token_account_balance(PublicKey(pubkey_str))
        if isinstance(bal, dict):
            v = (bal.get("result", {}) or {}).get("value", {}) or {}
            ui = v.get("uiAmount") ; dec = v.get("decimals") ; amt = v.get("amount")
        else:
            v = getattr(bal, "value", None)
            ui = getattr(v, "ui_amount", None)
            dec = getattr(v, "decimals", None)
            amt = getattr(v, "amount", None)
        ui_f = float(ui) if ui is not None else 0.0
        amt_i = int(amt) if amt is not None else 0
        return ui_f, amt_i, int(dec) if dec is not None else None
    except Exception:
        return 0.0, 0, None

def _list_token_acc_pubkeys_by_mint(cli: Client, owner_pub: PublicKey, mint: PublicKey):
    try:
        resp = cli.get_token_accounts_by_owner_json_parsed(owner_pub, TokenAccountOpts(mint=mint))
        vals = _value_list(resp)
        pubs = []
        for it in (vals or []):
            pk = _item_pubkey(it)
            if pk: pubs.append(pk)
        return pubs
    except Exception:
        return []

# ---------- Balances ----------
def get_token_balance_ui(cli: Client, owner_pub: PublicKey, mint: PublicKey):
    total = 0.0
    for pk in _list_token_acc_pubkeys_by_mint(cli, owner_pub, mint):
        ui, _, _ = _safe_get_token_account_balance(cli, pk)
        total += ui
    return total

def get_token_balance_raw(cli: Client, owner_pub: PublicKey, mint: PublicKey, decimals_hint: int | None = None) -> int:
    pubs = _list_token_acc_pubkeys_by_mint(cli, owner_pub, mint)
    if pubs:
        total = 0
        for pk in pubs:
            _, amt, _ = _safe_get_token_account_balance(cli, pk)
            total += int(amt)
        return total
    d = decimals_hint if (decimals_hint is not None) else 9
    ui = get_token_balance_ui(cli, owner_pub, mint)
    try:
        return int(Decimal(str(ui)) * (Decimal(10) ** d))
    except Exception:
        return 0

# ---------- Confirmación ----------
def confirm_tx_sig(cli: Client, sig: str, max_wait_s=90, dbg=False):
    emit("confirm_start", sig=sig, max_wait_s=max_wait_s)
    start = time.time()
    backoff = 0.5
    while time.time() - start < max_wait_s:
        try:
            resp = cli.get_signature_statuses([sig])
            vlist = _value_list(resp)
            v = vlist[0] if vlist else None
        except Exception:
            v = None
        if isinstance(v, dict):
            err = v.get("err")
            cstat = v.get("confirmationStatus")
            confs = v.get("confirmations", 0) or 0
            if dbg: print(f"[DEBUG] confirm: err={err} status={cstat} confs={confs}")
            if err is None and (cstat in ("confirmed","finalized") or confs >= 1):
                emit("confirm_ok", sig=sig, status=cstat, confirmations=confs)
                return True
            if err is not None:
                emit("confirm_err", sig=sig, err=str(err))
                return False
        time.sleep(backoff)
        backoff = min(backoff*1.7, 3.0)
    emit("confirm_timeout", sig=sig)
    return False

# ---------- Keypair ----------
def load_key_bytes():
    path = os.getenv("SOLANA_KEYPAIR")
    if not path:
        try:
            from config import WALLET_KEYPAIR_PATH as _p
            path = _p
        except Exception:
            path = None
    if path and os.path.exists(path):
        arr = json.loads(open(path, "r", encoding="utf-8").read())
        return bytes(arr)

    pk = os.getenv("SOLANA_PRIVKEY", "").strip()
    if pk:
        try:
            arr = json.loads(pk)
            return bytes(arr)
        except Exception:
            try:
                from base58 import b58decode
                return b58decode(pk)
            except Exception as e:
                raise RuntimeError("SOLANA_PRIVKEY no es JSON ni base58 válido.") from e

    raise RuntimeError("No se pudo cargar el keypair. Define SOLANA_KEYPAIR (ruta JSON) o SOLANA_PRIVKEY.")

def load_keypair():
    if CFG_KP is not None:
        return CFG_KP
    sk = load_key_bytes()
    return PyKeypair.from_secret_key(sk)

# ---------- Jupiter ----------
# PATCH: quote hop=1 estricto y manejo de errores/NO_ROUTES_FOUND
def jup_quote(token_in: str, amount_raw: int, slippage_bps=30, dbg=False):
    params = {
        "inputMint": token_in,
        "outputMint": SOL_MINT,
        "amount": str(amount_raw),
        "slippageBps": slippage_bps,
        "swapMode": "ExactIn",
        "onlyDirectRoutes": "true",
        "direct": "true",                        # forzar hop=1
        "restrictIntermediateTokens": "true",    # sin intermedios
        "asLegacyTransaction": "true",
        "platformFeeBps": "0"
    }
    hdr = {"Accept":"application/json"}
    if JUPITER_API_KEY: hdr["X-API-KEY"] = JUPITER_API_KEY
    t0 = perf_counter()
    r = hget(QUOTE_URL, headers=hdr, params=params, timeout=12)
    emit("jup_quote_http", status=getattr(r,"status_code",None), dt_ms=int((perf_counter()-t0)*1000), amount_raw=amount_raw, slippage_bps=slippage_bps)

    # HTTP != 200: intenta extraer errorCode
    if not r or r.status_code != 200:
        try:
            ec = (r.json() or {}).get("errorCode")
            return None, str(ec or f"quote_http_{getattr(r,'status_code','exc')}")
        except Exception:
            return None, f"quote_http_{getattr(r,'status_code','exc')}"

    try:
        q = r.json()
    except Exception as e:
        emit("jup_quote_json_err", err=str(e))
        if dbg: print("[DEBUG] quote_json_err")
        return None, "quote_json_err"

    # 200 con errorCode en payload
    if isinstance(q, dict) and "errorCode" in q:
        emit("jup_quote_errcode", code=q.get("errorCode"))
        return None, str(q.get("errorCode") or "NO_ROUTES_FOUND")

    if not q or "inAmount" not in q:
        emit("jup_quote_empty")
        if dbg: print("[DEBUG] quote_empty")
        return None, "quote_empty"

    # validar hop=1 real (v6: routePlan; compat: marketInfos)
    rp = q.get("routePlan") or []
    mi = q.get("marketInfos") or []
    legs = rp if rp else mi
    if len(legs) != 1:
        emit("jup_quote_not_hop1", hops=len(legs))
        return None, "NO_ROUTES_FOUND"
    try:
        first = legs[0] or {}
        swap = first.get("swapInfo") or {}
        inp = swap.get("inputMint")  or first.get("inputMint")
        out = swap.get("outputMint") or first.get("outputMint")
        if inp != token_in or out != SOL_MINT:
            emit("jup_quote_wrong_pair", inp=inp, out=out)
            return None, "NO_ROUTES_FOUND"
    except Exception:
        return None, "NO_ROUTES_FOUND"

    emit("jup_quote_ok", outAmount=q.get("outAmount"), inAmount=q.get("inAmount"))
    return q, None

# PATCH: debug_print_quote tolerante a v6 (routePlan) y v4/v5 (marketInfos)
def debug_print_quote(q: dict):
    try:
        if not isinstance(q, dict):
            print(f"[DEBUG] quote tipo inválido: {type(q)}"); return
        ia = q.get("inAmount"); oa = q.get("outAmount"); sbps = q.get("slippageBps")
        rp = q.get("routePlan") or []
        mi = q.get("marketInfos") or []
        legs = rp if rp else mi
        print(f"[DEBUG] quote: in={ia} out={oa} slippageBps={sbps} hops={len(legs)}")
        for i, leg in enumerate(legs):
            try:
                swap = leg.get("swapInfo") or {}
                lbl  = swap.get("label") or leg.get("label") or (leg.get("amm") or {}).get("label") or leg.get("marketId") or "?"
                inp  = swap.get("inputMint")  or leg.get("inputMint")
                out  = swap.get("outputMint") or leg.get("outputMint")
                impact = leg.get("priceImpactPct") or q.get("priceImpactPct")
                print(f"[DEBUG]  - hop[{i}] {lbl} {inp}->{out} impact={impact}")
            except Exception as e:
                print(f"[DEBUG]  - hop[{i}] parse err: {e}")
    except Exception as e:
        print(f"[DEBUG] debug_print_quote err: {e}")

# PATCH: swap siempre retorna firma como str
def jup_swap(qresp: dict, user_pubkey: str, dbg=False):
    body = {
        "quoteResponse": qresp,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "asLegacyTransaction": True,   # firmamos legacy
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": None
    }
    hdr = {"Content-Type":"application/json"}
    if JUPITER_API_KEY: hdr["X-API-KEY"] = JUPITER_API_KEY
    t0 = perf_counter()
    r = hpost(SWAP_URL, headers=hdr, json_body=body, timeout=20)
    emit("jup_swap_http", status=getattr(r,"status_code",None), dt_ms=int((perf_counter()-t0)*1000))
    if not r or r.status_code != 200:
        if dbg: print(f"[DEBUG] swap_http status={getattr(r,'status_code','exc')}")
        return None, f"swap_http_{getattr(r,'status_code','exc')}", None
    try:
        j = r.json()
        tx_b64 = j.get("swapTransaction")
        if not tx_b64:
            emit("jup_swap_no_tx")
            if dbg: print("[DEBUG] swap_no_tx en respuesta")
            return None, "swap_no_tx", None

        tx_bytes = b64decode(tx_b64)
        kp = load_keypair()
        if str(getattr(kp, "public_key", "")) != user_pubkey and dbg:
            print(f"[WARN] keypair pubkey {getattr(kp,'public_key','?')} != user_pubkey {user_pubkey}")

        tx = Transaction.deserialize(tx_bytes)
        tx.sign(kp)
        raw = tx.serialize()
        emit("tx_built", raw_len=len(raw))

        t1 = perf_counter()
        resp = client.send_raw_transaction(raw, opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed", max_retries=3))
        dt_ms = int((perf_counter()-t1)*1000)
        sigstr = resp.value if hasattr(resp, "value") else (resp.get("result") if isinstance(resp, dict) else None)
        sigstr = str(sigstr) if sigstr is not None else None   # forzar string
        emit("tx_submitted", sig=sigstr, dt_ms=dt_ms)
        if dbg: print(f"[DEBUG] tx sig={sigstr}")
        return True, None, sigstr

    except Exception as e:
        emit("tx_send_error", err=str(e))
        if dbg:
            print(f"[DEBUG] send_err: {e}")
            traceback.print_exc()
        return None, f"send_err:{e}", None

# ---------- DB helpers ----------
def _table_has_column(conn, table, col):
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        return col in cols
    except Exception:
        return False

def db_mark_forced_close_success(db_path: str, op_id: int, txids: list):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        tx_join = ",".join(txids) if txids else ""
        sets = ["note = 'Salida forzada por SOL'"]
        params = []
        if _table_has_column(conn, "operaciones", "tx_salida"):
            sets.insert(0, "tx_salida = ?"); params.append(tx_join)
        sql = f"UPDATE operaciones SET {', '.join(sets)} WHERE id = ?"
        params.append(op_id)
        cur.execute(sql, tuple(params))
        conn.commit()
        emit("db_mark_success", op_id=op_id, tx_count=len(txids))
    finally:
        conn.close()

def db_mark_forced_close_failure(db_path: str, op_id: int):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE operaciones
            SET note = 'Salida forzada/swap manual'
            WHERE id = ?
        """, (op_id,))
        conn.commit()
        emit("db_mark_failure", op_id=op_id)
    finally:
        conn.close()

# ---------- Utilidades varias ----------
def vip_name(mint: str) -> str:
    try:
        con = sqlite3.connect(DB_NAME, timeout=5)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT COALESCE(NULLIF(TRIM(name),''), substr(address,1,6)) AS n FROM vip_tokens WHERE address=?",
            (mint,)
        ).fetchone()
        return row["n"] if row else mint[:6]
    except Exception:
        return mint[:6]
    finally:
        try: con.close()
        except: pass

def email_salida(token: str, name: str, subject: str, amount_tokens: float | None = None, dbg=False):
    if dbg: print(f"[DEBUG] email_salida: subj='{subject}' amount_tokens={amount_tokens}")
    emit("notify_email", token=token, subject=subject)
    cmd = [sys.executable, "mesenger.py", "--token", token, "--name", name]
    if isinstance(amount_tokens, (int, float)):
        cmd += ["--amount", f"{amount_tokens:.8f}"]
    cmd += ["--subject", subject]
    subprocess.run(cmd, check=False)

def debug_list_accounts(cli: Client, owner_pub: PublicKey, mint: PublicKey):
    try:
        resp = cli.get_token_accounts_by_owner_json_parsed(owner_pub, TokenAccountOpts(mint=mint))
        vals = _value_list(resp)
        print(f"[DEBUG] owner={owner_pub}, mint={mint}, cuentas={len(vals or [])}")
        for it in (vals or []):
            pk = _item_pubkey(it)
            ui, amt, dec = _safe_get_token_account_balance(cli, pk) if pk else (0.0, 0, None)
            print(f"[DEBUG]  - acc={pk} ui={ui} raw={amt} dec={dec}")
    except Exception as e:
        print(f"[DEBUG] get_token_accounts_by_owner_json_parsed err: {e}")

# ---------- Slippage plan ----------
def build_slippage_plan(plan_str: str, base_bps: int, max_len: int = 6) -> list[int]:
    if plan_str:
        xs = []
        for tok in plan_str.split(","):
            tok = tok.strip()
            if tok.isdigit():
                xs.append(int(tok))
        if xs:
            return xs[:max_len]
    return [base_bps, max(base_bps, 50), 80, 100, 200, 300][:max_len]

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("token_mint", nargs="?", help="Mint del token a liquidar")
    ap.add_argument("--max-intentos", type=int, default=6)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--slippage-bps", type=int, default=30)  # 0.3%
    ap.add_argument("--slippage-plan", default="", help="Lista de slippages por intento en bps, ej: 30,60,100,150,200,300")
    ap.add_argument("--op-id", "--op_id", dest="op_id", type=int, default=None, help="ID en operaciones para marcar cerrado forzado")
    ap.add_argument("--db", default=DB_NAME, help="Ruta DB SQLite (default: config.DB_NAME)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    # token_mint opcional: pedir si falta
    if not args.token_mint:
        args.token_mint = input("Mint del token a liquidar: ").strip()

    emit("forced_exit_start", token_mint=args.token_mint, max_intentos=args.max_intentos, delay=args.delay, slippage_bps=args.slippage_bps)

    # owner desde keypair (si está disponible) para evitar desalineación en la firma
    try:
        kp = load_keypair()
        owner_pubkey_str = str(kp.public_key)
    except Exception:
        owner_pubkey_str = str(wallet_pubkey)

    name = vip_name(args.token_mint)
    owner = PublicKey(owner_pubkey_str)
    mint  = PublicKey(args.token_mint)

    # Email de inicio
    bal_ui0 = get_token_balance_ui(client, owner, mint)
    emit("forced_exit_init_balance", ui=bal_ui0)
    email_salida(args.token_mint, name, f"ALERTA: salida forzada iniciada – {name}", amount_tokens=bal_ui0, dbg=args.debug)

    plan = build_slippage_plan(args.slippage_plan, args.slippage_bps, max_len=args.max_intentos)
    emit("forced_exit_plan", plan_bps=plan)

    txids = []

    for intento in range(1, args.max_intentos + 1):
        emit("attempt_start", intento=intento)
        if args.debug: print(f"[DEBUG] -------- intento={intento} --------")

        bal_raw = get_token_balance_raw(client, owner, mint)
        bal_ui  = get_token_balance_ui(client, owner, mint)
        emit("attempt_balance", intento=intento, raw=bal_raw, ui=bal_ui)

        # Éxito solo si raw == 0
        if bal_raw == 0:
            out = {"ok": True, "txids": txids, "final_balance_raw": bal_raw, "final_balance_ui": bal_ui, "final_usd": "n/a", "intentos": intento - 1}
            print(json.dumps(out, ensure_ascii=False))
            emit("forced_exit_done", ok=True, tx_count=len(txids), attempts=intento-1, final_raw=bal_raw, final_ui=bal_ui)
            if args.op_id is not None:
                try:
                    db_mark_forced_close_success(args.db, args.op_id, txids)
                except Exception as e:
                    emit("db_mark_success_error", err=str(e))
            email_salida(args.token_mint, name, f"Salida forzada COMPLETADA – {name} (remanente: {bal_ui:.8f} tokens)", amount_tokens=bal_ui, dbg=args.debug)
            return

        # Intentar liquidar TODO el saldo disponible
        amt_raw = bal_raw
        if amt_raw <= 0:
            time.sleep(args.delay + random.uniform(0, 0.6))
            continue

        # Slippage dinámico por intento
        slip_bps = plan[min(intento-1, len(plan)-1)]
        emit("attempt_quote", intento=intento, slip_bps=slip_bps, amt_raw=amt_raw)

        q, qerr = jup_quote(args.token_mint, amt_raw, slippage_bps=slip_bps, dbg=args.debug)
        if qerr:
            emit("attempt_quote_error", intento=intento, error=qerr)
            time.sleep(args.delay * (1 + 0.25*(intento-1)) + random.uniform(0, 0.7))
            continue

        if args.debug: debug_print_quote(q)

        ok, serr, sig = jup_swap(q, owner_pubkey_str, dbg=args.debug)
        if serr or not ok or not sig:
            emit("attempt_swap_error", intento=intento, error=serr or "unknown")
            time.sleep(args.delay * (1 + 0.25*(intento-1)) + random.uniform(0, 0.7))
            continue

        # Guardar siempre txid como string (evita TypeError en json.dumps)
        txids.append(str(sig))
        emit("attempt_swap_ok", intento=intento, sig=str(sig))

        # Confirmación con espera mayor
        conf = confirm_tx_sig(client, sig, max_wait_s=90, dbg=args.debug)
        emit("attempt_confirm", intento=intento, sig=sig, confirmed=bool(conf))

        # respirar para que la DB/RPC refleje balances post-swap
        time.sleep(args.delay * (1 + 0.15*(intento-1)) + random.uniform(0, 0.5))

    # Revisión final tras agotar intentos
    bal_raw = get_token_balance_raw(client, owner, mint)
    bal_ui  = get_token_balance_ui(client, owner, mint)
    success = (bal_raw == 0)

    print(json.dumps({
        "ok": success,
        "txids": txids,
        "final_balance_raw": bal_raw,
        "final_balance_ui": bal_ui,
        "final_usd": "n/a",
        "intentos": args.max_intentos
    }, ensure_ascii=False))

    if success:
        emit("forced_exit_done", ok=True, tx_count=len(txids), attempts=args.max_intentos, final_raw=bal_raw, final_ui=bal_ui)
        email_salida(args.token_mint, name, f"Salida forzada COMPLETADA – {name} (remanente: {bal_ui:.8f} tokens)", amount_tokens=bal_ui, dbg=args.debug)
        if args.op_id is not None:
            try:
                db_mark_forced_close_success(args.db, args.op_id, txids)
            except Exception as e:
                emit("db_mark_success_error", err=str(e))
    else:
        emit("forced_exit_done", ok=False, tx_count=len(txids), attempts=args.max_intentos, final_raw=bal_raw, final_ui=bal_ui)
        # Correo especial de fallo y nota en DB
        email_salida(args.token_mint, name, f"❌ Salida forzada NO completada – {name} (remanente: {bal_ui:.8f} tokens)", amount_tokens=bal_ui, dbg=args.debug)
        if args.op_id is not None:
            try:
                db_mark_forced_close_failure(args.db, args.op_id)
            except Exception as e:
                emit("db_mark_failure_error", err=str(e))

if __name__ == "__main__":
    main()








