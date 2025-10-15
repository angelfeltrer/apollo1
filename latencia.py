# latencia.py — Latencia total de APIs usando config.py
# Reqs: pip install httpx
import time, json, argparse, statistics as stats
import httpx

try:
    import config  # BIRDEYE_API_KEY, HELIUS_RPC_URL, SOLANA_RPC_URL, DEFAULT_HEADERS
except Exception as e:
    raise SystemExit(f"Falta config.py: {e}")

DEFAULT_HEADERS = getattr(
    config,
    "DEFAULT_HEADERS",
    {"Accept": "application/json", "User-Agent": "apollo1-latency/1.0"},
)

TARGETS = [
    # --- Jupiter: SWAP (latencia del endpoint, NO ejecuta swap real) ---
    {
        "name": "jupiter_swap_api",
        "method": "POST",
        "url": "https://quote-api.jup.ag/v6/swap",
        "headers": {"Content-Type": "application/json"},
        "json": {
            "userPublicKey": "11111111111111111111111111111111",  # dummy
            "wrapAndUnwrapSol": False,
            "asLegacyTransaction": True
        },
        "kind": "post",
        "needs": [],
    },
    # --- Jupiter: QUOTE ---
    {
        "name": "jupiter_quote",
        "method": "GET",
        "url": (
            "https://quote-api.jup.ag/v6/quote"
            "?inputMint=So11111111111111111111111111111111111111112"   # SOL
            "&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" # USDC
            "&amount=1000000&slippageBps=50&onlyDirectRoutes=true"
        ),
        "headers": {},
        "kind": "get",
        "needs": [],
    },

    # --- Birdeye (path correcto /defi/v3 y x-chain) ---
    {
        "name": "birdeye_market_data",
        "method": "GET",
        "url": "https://public-api.birdeye.so/defi/v3/token/market-data?address=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&x-chain=solana",
        "headers": lambda: {"X-API-KEY": getattr(config, "BIRDEYE_API_KEY", "")},
        "kind": "get",
        "needs": ["BIRDEYE_API_KEY"],
    },

    # --- Helius / RPC ---
    {
        "name": "helius_getLatestBlockhash",
        "method": "POST",
        "url": lambda: getattr(config, "HELIUS_RPC_URL", ""),
        "headers": {"Content-Type": "application/json"},
        "json": {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash"},
        "kind": "post",
        "needs": ["HELIUS_RPC_URL"],
    },

    # --- Solana RPC público (opcional) ---
    {
        "name": "solana_rpc_getHealth",
        "method": "POST",
        "url": lambda: getattr(config, "SOLANA_RPC_URL", ""),
        "headers": {"Content-Type": "application/json"},
        "json": {"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        "kind": "post",
        "needs": [],
        "optional_url": True,
    },
]

def pct(arr, q):
    if not arr: return float("nan")
    arr = sorted(arr); k = (len(arr)-1)*q; f = int(k); c = min(f+1, len(arr)-1)
    return arr[f] if f==c else arr[f]+(arr[c]-arr[f])*(k-f)

def ok_resp(resp, name):
    # Para medir latencia del endpoint swap aceptamos 200/400/422/429
    if name == "jupiter_swap_api":
        return resp.status_code in (200, 400, 422, 429)
    if resp.status_code != 200:
        return False
    if name == "helius_getLatestBlockhash":
        try: return "result" in resp.json()
        except Exception: return False
    if name == "birdeye_market_data":
        try:
            j = resp.json()
            return bool(j.get("success"))
        except Exception:
            return False
    return True

def measure_once(client, t, timeout):
    url = t["url"]() if callable(t["url"]) else t["url"]
    if not url and t.get("optional_url"): return {"skip": True, "reason": "URL vacía"}
    hdrs = t["headers"]() if callable(t.get("headers")) else (t.get("headers") or {})
    base = DEFAULT_HEADERS.copy(); base.update(hdrs); hdrs = base
    for v in t.get("needs", []):
        if not getattr(config, v, ""): return {"skip": True, "reason": f"falta {v}"}
    try:
        start = time.perf_counter()
        if t["kind"] == "get":
            resp = client.get(url, headers=hdrs, timeout=timeout)
        else:
            resp = client.post(url, headers=hdrs, json=t.get("json"), timeout=timeout)
        total = time.perf_counter() - start
        ok = ok_resp(resp, t["name"])
        if not ok:
            try: body = resp.text[:200]
            except Exception: body = ""
            return {"status": resp.status_code, "total": total, "ok": False, "err": body or f"HTTP {resp.status_code}"}
        return {"status": resp.status_code, "total": total, "ok": True}
    except httpx.ReadTimeout:
        return {"status": None, "total": None, "ok": False, "err": "timeout"}
    except Exception as e:
        return {"status": None, "total": None, "ok": False, "err": str(e)}

def run(repeats, timeout, warmups):
    out = []
    # No seguir redirecciones para detectar 301/302 de Birdeye antiguos
    with httpx.Client(http2=False, follow_redirects=False) as client:
        for t in TARGETS:
            for _ in range(max(0, warmups)):
                _ = measure_once(client, t, timeout)
            samples = [measure_once(client, t, timeout) for _ in range(repeats)]
            oks = [s for s in samples if s.get("ok")]
            totals = [s["total"] for s in oks if s.get("total") is not None]
            out.append({
                "name": t["name"],
                "url": (t["url"]() if callable(t["url"]) else t["url"]),
                "needs": t.get("needs", []),
                "repeats": repeats,
                "success_rate": len(oks)/repeats if repeats else 0.0,
                "p50_total": pct(totals, 0.50),
                "p95_total": pct(totals, 0.95),
                "avg_total": (stats.fmean(totals) if totals else float("nan")),
                "last_status": next((s["status"] for s in reversed(samples) if s.get("status") is not None), None),
                "errors": [s.get("err") for s in samples if s.get("err")][:5],
                "skips": [s for s in samples if s.get("skip")],
            })
    return out

def fmt_ms(x):
    if x is None or isinstance(x, float) and x != x: return "-"
    return f"{x*1000:.1f} ms"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--warmups", type=int, default=0)
    ap.add_argument("--json-out", type=str)
    args = ap.parse_args()

    report = run(args.repeats, args.timeout, args.warmups)

    print("\n== Latencia (total) por API ==")
    print(f"{'target':24} {'ok%':>6} {'p50':>10} {'p95':>10} {'last':>6} {'needs'}")
    for r in report:
        print(f"{r['name'][:24]:24} "
              f"{r['success_rate']*100:6.1f} "
              f"{fmt_ms(r['p50_total']):>10} {fmt_ms(r['p95_total']):>10} "
              f"{str(r['last_status'])[:6]:>6} "
              f"{','.join(r['needs']) if r['needs'] else '-'}")
    for r in report:
        if r["errors"]:
            print(f"- {r['name']} errors: {r['errors']}")
    for r in report:
        if r["skips"]:
            reasons = list({s.get("reason") for s in r["skips"] if s})
            print(f"- {r['name']} skipped: {reasons}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nGuardado: {args.json_out}")







