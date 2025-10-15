# config.py

from solana.rpc.api import Client
from solana.keypair import Keypair
import json
import os

# === 🔐 Wallet desde keypair.json ===
with open("keypair.json", "r") as f:
    secret = json.load(f)
keypair = Keypair.from_secret_key(bytes(secret))
wallet_pubkey = keypair.public_key

# ********** LATENCIA **************
BIRDEYE_API_KEY = "e7417163ac494dffa19c3caeecd76629"  # <- tu key
HELIUS_RPC_URL  = "https://mainnet.helius-rpc.com/?api-key=06d471af-62a0-40d9-867a-50d9aad046e6"
SOLANA_RPC_URL  = "https://api.mainnet-beta.solana.com"
DEFAULT_HEADERS = {"Accept": "application/json", "User-Agent": "apollo1-latency/1.0"}
# ********** LATENCIA **************

# === 🔔 WebSocket (Helius) ===
WS_URL = os.getenv(
    "HELIUS_WS",
    "wss://mainnet.helius-rpc.com/?api-key=06d471af-62a0-40d9-867a-50d9aad046e6"
)
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT  = 20

# === 🌐 Conexión RPC ===
RPC_URL = HELIUS_RPC_URL
client = Client(RPC_URL)

# === 🔑 API KEYS ===
API_KEY = BIRDEYE_API_KEY
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()  # no usado en Lite

# === 🌐 URLs Jupiter (Lite) ===
QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SWAP_URL  = "https://lite-api.jup.ag/swap/v1/swap"

# === ⚙️ Parámetros generales ===
DB_NAME = "goodt.db"
AMOUNT_USDC = 2.0
AMOUNT_LAMPORTS = int(AMOUNT_USDC * 1_000_000)  # 1 USDC = 1e6 lamports
TRAILING_STOP = 0.007  # -0.7%

# === 🧾 Headers para Birdeye ===
HEADERS = {
    "accept": "application/json",
    "x-chain": "solana",
    "X-API-KEY": API_KEY,
}
TIMEOUT = 10  # segundos

