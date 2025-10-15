# insert_vip_interactive.py — Interactivo: pide un mint y completa TODOS los campos en vip_tokens (incluye route_base USDC|SOL).
# Requisitos: pip install requests
import os, sys, time, json, sqlite3, requests, base64
from datetime import datetime, timezone

DB_NAME = "goodt.db"
TABLE   = "vip_tokens"

# Endpoints / claves
USDC_MINT        = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT         = "So11111111111111111111111111111111111111112"
HELIUS_RPC_URL   = os.getenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=06d471af-62a0-40d9-867a-50d9aad046e6").strip()
RAYDIUM_POOLS_URL= "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"
JUP_TOKENLIST_URL= "https://token.jup.ag/all"
BIRDEYE_API_KEY  = os.getenv("BIRDEYE_API_KEY", "e7417163ac494dffa19c3caeecd76629").strip()

HEADERS_BIRDEYE  = {"accept": "application/json", "X-API-KEY": BIRDEYE_API_KEY} if BIRDEYE_API_KEY else None

# Blacklist de mints bloqueados
BLACKLIST = {"7eMJmn1bYWSQEwxAX7CyngBzGNGu1cT582asKxxRpump"}

# Parámetros de severidad automática (si hay datos de Birdeye)
MIN_HOLDERS   = 500_000
MIN_AGE_DAYS  = 60
MIN_LIQUIDITY = 500_000

# Fix consola Windows
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------- utilidades ----------
def nowts():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    con = sqlite3.connect(DB_NAME, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con

def ensure_schema():
    base_create = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
      address    TEXT PRIMARY KEY,
      name       TEXT,
      symbol     TEXT,
      reason     TEXT,
      source     TEXT DEFAULT 'manual',
      severity   INTEGER NOT NULL DEFAULT 2,
      active     INTEGER NOT NULL DEFAULT 1,
      expires_at TEXT,
      note       TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """
    extra_cols = [
        ("decimals",   "INTEGER"),
        ("pool_id",    "TEXT"),
        ("vault_usdc", "TEXT"),
        ("vault_token","TEXT"),
        ("estado",     "TEXT"),
        ("prioridad",  "INTEGER"),
        ("price_entry","REAL"),
        ("route_base", "TEXT"),   # USDC | SOL
    ]
    indexes = [
        (f"CREATE INDEX IF NOT EXISTS idx_vip_estado      ON {TABLE}(estado)"),
        (f"CREATE INDEX IF NOT EXISTS idx_vip_prioridad   ON {TABLE}(prioridad)"),
        (f"CREATE INDEX IF NOT EXISTS idx_vip_updated     ON {TABLE}(updated_at)"),
        (f"CREATE INDEX IF NOT EXISTS idx_vip_route_base  ON {TABLE}(route_base)"),
    ]
    with db() as con:
        con.execute(base_create)
        have = {r[1] for r in con.execute(f"PRAGMA table_info({TABLE})").fetchall()}
        for col, ctype in extra_cols:
            if col not in have:
                con.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col} {ctype}")
                print(f"➕ columna añadida: {col} {ctype}")
        for ddl in indexes:
            con.execute(ddl)

def exists_in_vip(mint: str) -> bool:
    with db() as con:
        return con.execute(f"SELECT 1 FROM {TABLE} WHERE address=?", (mint,)).fetchone() is not None

def sanitize(s: str | None) -> str | None:
    if s is None: return None
    s = s.replace("\x00", "").strip()
    return s or None

def insert_base_row(con, mint: str, name: str, symbol: str, reason: str, source: str, severity: int, active: int, expires_at: str, note: str):
    # Inserta solo si NO existe (sin updates)
    con.execute(
        f"""INSERT INTO {TABLE}
        (address, name, symbol, reason, source, severity, active, expires_at, note, created_at, updated_at,
         decimals, pool_id, vault_usdc, vault_token, estado, prioridad, price_entry, route_base)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                NULL, NULL, NULL, NULL, '', NULL, NULL, NULL)""",
        (mint, name, symbol, reason, source, severity, active, expires_at, note, nowts(), nowts())
    )

# ---------- Jupiter token list (name/symbol) ----------
_TOKENS_CACHE = None
def get_name_symbol_jup(mint: str):
    global _TOKENS_CACHE
    try:
        if _TOKENS_CACHE is None:
            r = requests.get(JUP_TOKENLIST_URL, timeout=15)
            r.raise_for_status()
            arr = r.json()
            _TOKENS_CACHE = {it.get("address"): {"name": it.get("name"), "symbol": it.get("symbol")} for it in arr if isinstance(it, dict) and it.get("address")}
        meta = _TOKENS_CACHE.get(mint)
        if meta:
            n = sanitize(meta.get("name"))
            s = sanitize(meta.get("symbol"))
            if n or s:
                return n, s
    except Exception:
        pass
    return None, None

# ---------- On-chain: Metaplex Metadata vía Helius RPC ----------
# Programa de Metadatos (Token Metadata Program)
METAPLEX_PID = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

def get_name_symbol_onchain(mint: str) -> tuple[str|None, str|None]:
    """
    Busca la cuenta de metadata (getProgramAccounts con memcmp al campo mint en offset 33),
    trae un slice inicial y decodifica Borsh: name y symbol (len LE + bytes).
    """
    if not HELIUS_RPC_URL:
        return None, None
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getProgramAccounts",
        "params": [
            METAPLEX_PID,
            {
                "encoding": "base64",
                "dataSlice": {"offset": 0, "length": 256},
                "filters": [
                    {"memcmp": {"offset": 33, "bytes": mint}}
                ]
            }
        ]
    }
    try:
        r = requests.post(HELIUS_RPC_URL, json=payload, timeout=12)
        r.raise_for_status()
        res = r.json().get("result") or []
        if not res:
            return None, None
        data_b64 = (res[0].get("account") or {}).get("data") or []
        if isinstance(data_b64, list):
            data_b64 = data_b64[0]
        if not data_b64:
            return None, None
        raw = base64.b64decode(data_b64)
        # Layout básico:
        # 0: key(1) | 1..32: updateAuth(32) | 33..64: mint(32) | luego: name(str) | symbol(str) | uri(str) ...
        i = 1 + 32 + 32
        if len(raw) < i + 8:
            return None, None
        name_len = int.from_bytes(raw[i:i+4], "little", signed=False); i += 4
        name = raw[i:i+name_len].decode("utf-8", "ignore"); i += name_len
        if len(raw) < i + 4:
            return sanitize(name), None
        sym_len = int.from_bytes(raw[i:i+4], "little", signed=False); i += 4
        symbol = raw[i:i+sym_len].decode("utf-8", "ignore"); i += sym_len
        return sanitize(name), sanitize(symbol)
    except Exception:
        return None, None

# ---------- Birdeye (opcional para enriquecer) ----------
def birdeye_market_data(mint: str):
    if not HEADERS_BIRDEYE: return {}
    try:
        url = f"https://public-api.birdeye.so/defi/v3/token/market-data?address={mint}"
        r = requests.get(url, headers=HEADERS_BIRDEYE, timeout=10)
        r.raise_for_status()
        return r.json().get("data", {}) or {}
    except Exception:
        return {}

def birdeye_trade_data(mint: str):
    if not HEADERS_BIRDEYE: return {}
    try:
        url = f"https://public-api.birdeye.so/defi/v3/token/trade-data/single?address={mint}"
        r = requests.get(url, headers=HEADERS_BIRDEYE, timeout=10)
        r.raise_for_status()
        return r.json().get("data", {}) or {}
    except Exception:
        return {}

def calc_age_days(created_field):
    if not created_field:
        return None
    try:
        ts = int(created_field)
        if ts > 10_000_000_000: ts = ts / 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(str(created_field).replace("Z","+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return None

def enrich_and_classify_with_birdeye(mint: str):
    md = birdeye_market_data(mint)
    td = birdeye_trade_data(mint)
    liq = float(md.get("liquidity") or md.get("liquidityUSD") or 0.0)
    holders = int(md.get("holders") or 0)
    created = td.get("firstTradeUnixTime") or td.get("first_trade_unix_time") or md.get("createdAt") or md.get("createdTime")
    age_days = calc_age_days(created) or 0

    reasons = []
    if holders < MIN_HOLDERS: reasons.append(f"holders<{MIN_HOLDERS}")
    if age_days < MIN_AGE_DAYS: reasons.append(f"edad<{MIN_AGE_DAYS}d")
    if liq < MIN_LIQUIDITY: reasons.append(f"liq<${MIN_LIQUIDITY}")

    if reasons:
        reason = "manual"
        severity = 2
    else:
        reason = f"holders≥{MIN_HOLDERS}, edad≥{MIN_AGE_DAYS}d, liq≥{MIN_LIQUIDITY}"
        severity = 1 if (holders >= 1_000_000 and liq >= 2_000_000 and age_days >= 180) else 2

    note = f"liq=${liq:,.0f}, holders≈{holders}, edad={age_days}d"
    return reason, severity, note

# ---------- Helius: decimals ----------
def rpc_get_mint_decimals(mint: str, timeout=7):
    if not HELIUS_RPC_URL:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
               "params": [mint, {"encoding": "jsonParsed", "commitment": "processed"}]}
    try:
        r = requests.post(HELIUS_RPC_URL, json=payload, timeout=timeout)
        r.raise_for_status()
        v = r.json().get("result", {}).get("value") or {}
        dec = ((v.get("data") or {}).get("parsed") or {}).get("info", {}).get("decimals")
        return int(dec) if dec is not None else None
    except Exception as e:
        print(f"   ↳ Helius decimals error ({mint[:6]}…): {e}")
        return None

# ---------- Raydium: pools / vaults ----------
_POOLS_CACHE = None
def fetch_raydium_pools(timeout=10):
    global _POOLS_CACHE
    if _POOLS_CACHE is not None:
        return _POOLS_CACHE
    try:
        r = requests.get(RAYDIUM_POOLS_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        pools = []
        def collect(x):
            if isinstance(x, dict):
                if "baseMint" in x and "quoteMint" in x:
                    pools.append(x)
                else:
                    for v in x.values(): collect(v)
            elif isinstance(x, list):
                for it in x: collect(it)
        collect(data)
        _POOLS_CACHE = pools
        return pools
    except Exception as e:
        print(f"⚠️ Raydium pools: {e}")
        _POOLS_CACHE = []
        return []

def pick_direct_pool(token_mint: str, base_mint: str, pools: list[dict]):
    """
    Devuelve (pool_id, quote_vault, token_vault) si hay pool hop=1 entre token_mint y base_mint.
    El 'quote_vault' es el vault del base_mint (USDC o SOL).
    """
    for p in pools:
        base = p.get("baseMint"); quote = p.get("quoteMint")
        if not base or not quote: continue
        if (base == token_mint and quote == base_mint) or (quote == token_mint and base == base_mint):
            pool_id = p.get("id") or p.get("ammId") or p.get("poolId") or p.get("address")
            base_vault  = p.get("baseVault")  or p.get("baseVaultAddress")
            quote_vault = p.get("quoteVault") or p.get("quoteVaultAddress")
            if not pool_id or not base_vault or not quote_vault:
                continue
            if quote == base_mint:
                return pool_id, quote_vault, base_vault
            else:
                return pool_id, base_vault, quote_vault
    return None, None, None

# ---------- flujo principal ----------
def resolve_name_symbol(mint: str) -> tuple[str, str]:
    # 1) Jupiter
    n, s = get_name_symbol_jup(mint)
    # 2) On-chain (Metaplex)
    if not n or not s:
        n2, s2 = get_name_symbol_onchain(mint)
        n = n or n2
        s = s or s2
    # 3) Birdeye
    if (not n or not s) and HEADERS_BIRDEYE:
        try:
            url = f"https://public-api.birdeye.so/defi/token_overview?address={mint}"
            r = requests.get(url, headers=HEADERS_BIRDEYE, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {}) or {}
            n = n or sanitize(data.get("name"))
            s = s or sanitize(data.get("symbol"))
        except Exception:
            pass
    # 4) Fallback amigable (sin None)
    if not n: n = f"{mint[:6]}"
    if not s: s = f"{mint[:3]}".upper()
    return n, s

def insert_or_update_full(mint: str):
    if mint in BLACKLIST:
        print("🚫 Mint en blacklist. Saltando.")
        return False

    # name / symbol (mejorados)
    name, symbol = resolve_name_symbol(mint)

    # reason / severity / note
    if HEADERS_BIRDEYE:
        reason, severity, note = enrich_and_classify_with_birdeye(mint)
        source = "interactive+birdeye"
    else:
        reason, severity, note = "manual", 2, "insert_vip_interactive"
        source = "interactive"

    # active / expires
    active = 1
    expires_at = ""  # vacío = sin expiración

    with db() as con:
        # base fields (insert-only)
        insert_base_row(con, mint, name, symbol, reason, source, severity, active, expires_at, note)

        # decimals
        row = con.execute(f"SELECT decimals FROM {TABLE} WHERE address=?", (mint,)).fetchone()
        if row and row["decimals"] is None:
            d = rpc_get_mint_decimals(mint)
            if d is not None:
                con.execute(f"UPDATE {TABLE} SET decimals=?, updated_at=? WHERE address=?", (int(d), nowts(), mint))
                print(f"   ➕ decimals={d}")

        # pools/vaults + route_base (preferir USDC; si no, SOL)
        pools = fetch_raydium_pools()
        route_base = None
        pool_id = v_quote = v_token = None

        pid, vu, vt = pick_direct_pool(mint, USDC_MINT, pools)
        if pid and vu and vt:
            route_base = "USDC"; pool_id, v_quote, v_token = pid, vu, vt
        else:
            pid, vu, vt = pick_direct_pool(mint, SOL_MINT, pools)
            if pid and vu and vt:
                route_base = "SOL"; pool_id, v_quote, v_token = pid, vu, vt

        if pool_id and v_quote and v_token and route_base:
            con.execute(
                f"UPDATE {TABLE} SET pool_id=?, vault_usdc=?, vault_token=?, route_base=?, updated_at=? WHERE address=?",
                (pool_id, v_quote, v_token, route_base, nowts(), mint)
            )
            print(f"   🔗 pool/vaults OK (base={route_base})")
        else:
            print("   ⚠️ sin pool directo USDC ni SOL en Raydium (queda incompleto).")

        # marcar listo si completo (incluye route_base)
        row2 = con.execute(
            f"SELECT decimals,pool_id,vault_usdc,vault_token,route_base,active,expires_at,estado,prioridad FROM {TABLE} WHERE address=?",
            (mint,)
        ).fetchone()

        ok = bool(
            (row2["decimals"] is not None) and row2["pool_id"] and row2["vault_usdc"] and row2["vault_token"]
            and (row2["route_base"] in ("USDC","SOL"))
        )
        if ok and int(row2["active"] or 1) != 0:
            if not (row2["estado"] or "").strip():
                con.execute(f"UPDATE {TABLE} SET estado=?, updated_at=? WHERE address=?", ("🚀 comprar", nowts(), mint))
            if row2["prioridad"] is None:
                con.execute(f"UPDATE {TABLE} SET prioridad=?, updated_at=? WHERE address=?", (100, nowts(), mint))

        # asegurar price_entry inicial (NULL si prefieres)
        con.execute(f"UPDATE {TABLE} SET price_entry=price_entry WHERE address=?", (mint,))

    # resumen visual
    with db() as con:
        r = con.execute(f"SELECT * FROM {TABLE} WHERE address=?", (mint,)).fetchone()
        if r:
            print("   ── RESUMEN VIP ──")
            print(f"   name:        {r['name']}")
            print(f"   symbol:      {r['symbol']}")
            print(f"   decimals:    {r['decimals']}")
            print(f"   pool_id:     {r['pool_id']}")
            print(f"   vault_usdc:  {r['vault_usdc']}  (quote vault: USDC o SOL)")
            print(f"   vault_token: {r['vault_token']}")
            print(f"   route_base:  {r['route_base']}")
            print(f"   estado:      {r['estado']}")
            print(f"   prioridad:   {r['prioridad']}")
            print(f"   reason:      {r['reason']}")
            print(f"   severity:    {r['severity']}")
            print(f"   active:      {r['active']}")
            print(f"   expires_at:  {r['expires_at']}")
            print(f"   note:        {r['note']}")
            print("   ────────────────")

    return True

def main():
    ensure_schema()
    print("\n=== INSERT VIP (interactivo, USDC o SOL hop=1; guarda route_base) ===")
    print("Ingresa un mint SPL por línea. Vacío o 'q' para salir.\n")

    ok_cnt = fail_cnt = 0
    while True:
        try:
            mint = input("Mint > ").strip()
        except EOFError:
            break
        if not mint or mint.lower() in ("q", "quit", "exit"):
            break

        # si existe, NO graba ni actualiza
        if exists_in_vip(mint):
            print("🔁 token ya existe en vip_tokens\n")
            continue

        print(f"\n==> {mint}")
        try:
            ok = insert_or_update_full(mint)
            if ok:
                print("✅ Insert completo (si hay pool USDC o SOL quedó habilitado para operar).")
                ok_cnt += 1
            else:
                print("❌ Falló inserción.")
                fail_cnt += 1
        except sqlite3.IntegrityError:
            print("🔁 token ya existe en vip_tokens")
        except Exception as e:
            print(f"⛔ Error: {e}")
            fail_cnt += 1
        print("")

    print(f"\nResumen: OK={ok_cnt} | Fallos={fail_cnt}")
    print("Listo.\n")

if __name__ == "__main__":
    main()












