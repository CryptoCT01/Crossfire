"""Crossfire configuration — env-driven, no hardcoded secrets."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

# Mode: public (marks only) | demo | live
MODE = (os.environ.get("CROSSFIRE_MODE") or "public").strip().lower()
if MODE not in ("public", "demo", "live"):
    MODE = "public"

PORT = int(os.environ.get("CROSSFIRE_PORT") or "8770")
HOST = os.environ.get("CROSSFIRE_HOST") or "127.0.0.1"

# Heartbeat / event wake
HEARTBEAT_SEC = int(os.environ.get("CROSSFIRE_HEARTBEAT_SEC") or "300")
EVENT_MOVE_PCT = float(os.environ.get("CROSSFIRE_EVENT_MOVE_PCT") or "1.5")
BOOKS_CACHE_SEC = float(os.environ.get("CROSSFIRE_BOOKS_CACHE_SEC") or "4.0")

# Bitget public
BITGET_BASE = os.environ.get("BITGET_BASE") or "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

# Private sleeve credentials — ONLY from env
BITGET_API_KEY = (os.environ.get("BITGET_API_KEY") or "").strip()
BITGET_SECRET_KEY = (os.environ.get("BITGET_SECRET_KEY") or "").strip()
BITGET_PASSPHRASE = (os.environ.get("BITGET_PASSPHRASE") or "").strip()

# Optional LLM
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
LLM_MODEL = (os.environ.get("CROSSFIRE_LLM_MODEL") or "").strip()

# Risk Cage — hard constants
RISK = {
    "max_slots": 3,
    "max_lev_us": 20,
    "max_lev_crypto": 50,
    "daily_dd_halt_pct": 5.0,
    "sleeve_notional_cap_usdt": float(os.environ.get("CROSSFIRE_SLEEVE_CAP") or "2000"),
    "per_leg_notional_cap_usdt": float(os.environ.get("CROSSFIRE_LEG_CAP") or "500"),
}

# Policy thresholds (rule engine when no LLM)
POLICY = {
    "divergence_threshold_pct": float(os.environ.get("CROSSFIRE_DIV_PCT") or "2.5"),
    "mag7_symbols": [
        "AAPLUSDT",
        "TSLAUSDT",
        "NVDAUSDT",
        "METAUSDT",
        "AMZNUSDT",
        "MSFTUSDT",
        "GOOGLUSDT",
    ],
    "btc_symbol": "BTCUSDT",
}

US_UNIVERSE = [
    "AAPLUSDT",
    "TSLAUSDT",
    "NVDAUSDT",
    "METAUSDT",
    "AMZNUSDT",
    "MSFTUSDT",
    "GOOGLUSDT",
    "NFLXUSDT",
    "AMDUSDT",
    "COINUSDT",
    "MSTRUSDT",
    "SPYUSDT",
]

CRYPTO_UNIVERSE = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "BNBUSDT",
    "SUIUSDT",
    "DOTUSDT",
    "NEARUSDT",
    "PEPEUSDT",
]

ALL_SYMBOLS = US_UNIVERSE + CRYPTO_UNIVERSE

# Exchange max leverage hints for watchlist display (not live position lev)
EXCHANGE_MAX_LEV = {
    **{s: RISK["max_lev_us"] for s in US_UNIVERSE},
    **{s: RISK["max_lev_crypto"] for s in CRYPTO_UNIVERSE},
}


def sleeve_connected() -> bool:
    return bool(BITGET_API_KEY and BITGET_SECRET_KEY and BITGET_PASSPHRASE)


def llm_available() -> bool:
    return bool(OPENAI_API_KEY or ANTHROPIC_API_KEY)


def connect_mode_label() -> str:
    if sleeve_connected():
        if MODE == "live":
            return "LIVE"
        if MODE == "demo":
            return "DEMO"
        return "SLEEVE CONNECTED"
    return "SLEEVE DISCONNECTED"


def mode_pill() -> str:
    if not sleeve_connected():
        return "PUBLIC MARKS · SLEEVE DISCONNECTED"
    if MODE == "live":
        return "LIVE SLEEVE"
    if MODE == "demo":
        return "DEMO SLEEVE"
    return "PUBLIC MARKS · SLEEVE READY"


def load_dotenv_if_present() -> None:
    """Minimal .env loader (no dependency). Does not override existing env."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v
    # Re-bind module globals after load
    global MODE, BITGET_API_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE
    global OPENAI_API_KEY, ANTHROPIC_API_KEY, LLM_MODEL, PORT, HOST
    MODE = (os.environ.get("CROSSFIRE_MODE") or "public").strip().lower()
    if MODE not in ("public", "demo", "live"):
        MODE = "public"
    BITGET_API_KEY = (os.environ.get("BITGET_API_KEY") or "").strip()
    BITGET_SECRET_KEY = (os.environ.get("BITGET_SECRET_KEY") or "").strip()
    BITGET_PASSPHRASE = (os.environ.get("BITGET_PASSPHRASE") or "").strip()
    OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
    ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    LLM_MODEL = (os.environ.get("CROSSFIRE_LLM_MODEL") or "").strip()
    PORT = int(os.environ.get("CROSSFIRE_PORT") or "8770")
    HOST = os.environ.get("CROSSFIRE_HOST") or "127.0.0.1"
