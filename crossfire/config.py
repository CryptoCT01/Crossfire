"""Crossfire configuration — env-driven, no hardcoded secrets."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

# Mode: public (marks only) | demo | live
MODE = (os.environ.get("CROSSFIRE_MODE") or "public").strip().lower()
if MODE not in ("public", "demo", "live"):
    MODE = "public"

PORT = int(os.environ.get("CROSSFIRE_PORT") or "8780")
HOST = os.environ.get("CROSSFIRE_HOST") or "127.0.0.1"

# Heartbeat / event wake — same in every agent mode
HEARTBEAT_SEC = int(os.environ.get("CROSSFIRE_HEARTBEAT_SEC") or "300")

# Agent loop — False = no auto ticks / event wakes / LLM spend
def _env_bool(name: str, default: bool = True) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")

AGENT_ENABLED = _env_bool("CROSSFIRE_AGENT_ENABLED", True)
PAPER = _env_bool("CROSSFIRE_PAPER", False)  # paper fills on virtual $10k book; never Bitget private orders
_agent_enabled_lock = __import__("threading").Lock()

EVENT_MOVE_PCT = float(os.environ.get("CROSSFIRE_EVENT_MOVE_PCT") or "1.5")
BOOKS_CACHE_SEC = float(os.environ.get("CROSSFIRE_BOOKS_CACHE_SEC") or "4.0")

# Bitget public
BITGET_BASE = os.environ.get("BITGET_BASE") or "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

# Private sleeve credentials — ONLY from env
BITGET_API_KEY = (os.environ.get("BITGET_API_KEY") or "").strip()
BITGET_SECRET_KEY = (os.environ.get("BITGET_SECRET_KEY") or "").strip()
BITGET_PASSPHRASE = (os.environ.get("BITGET_PASSPHRASE") or "").strip()

# Optional LLM (OpenRouter preferred; OpenAI/Anthropic as fallbacks)
OPENROUTER_API_KEY = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
_or_base = (os.environ.get("OPENROUTER_BASE") or "https://openrouter.ai/api/v1").strip().rstrip("/")
OPENROUTER_BASE = _or_base or "https://openrouter.ai/api/v1"
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
LLM_MODEL = (os.environ.get("CROSSFIRE_LLM_MODEL") or "").strip()

# Shared notional caps (both agent modes)
_SLEEVE_CAP = float(os.environ.get("CROSSFIRE_SLEEVE_CAP") or "2000")
_LEG_CAP = float(os.environ.get("CROSSFIRE_LEG_CAP") or "500")
_BASE_DIV = float(os.environ.get("CROSSFIRE_DIV_PCT") or "2.5")

# Agent mode profiles — Risk Cage never removed; Aggressive only loosens gates
AGENT_MODE_PROFILES: dict[str, dict[str, Any]] = {
    "normal": {
        "max_slots": int(os.environ.get("CROSSFIRE_MAX_SLOTS") or "2"),
        "max_lev_us": 20,
        "max_lev_crypto": 50,
        "daily_dd_halt_pct": 5.0,
        "sleeve_notional_cap_usdt": _SLEEVE_CAP,
        "per_leg_notional_cap_usdt": _LEG_CAP,
        "divergence_threshold_pct": _BASE_DIV,
        "single_hedge_pair_bias": True,
        "prefer_hold": True,
        "label": "Normal",
        "blurb": "Risk Cage spirit — prefer HOLD unless Mag7 vs BTC divergence is clear; single hedge-pair bias; tighter halt.",
    },
    "aggressive": {
        "max_slots": int(os.environ.get("CROSSFIRE_MAX_SLOTS_AGG") or "3"),
        "max_lev_us": 20,  # exchange ceiling
        "max_lev_crypto": 50,  # exchange ceiling
        "daily_dd_halt_pct": 8.0,
        "sleeve_notional_cap_usdt": _SLEEVE_CAP,
        "per_leg_notional_cap_usdt": _LEG_CAP,
        # Looser gate → HEDGE/ROTATE more often; still a real threshold
        "divergence_threshold_pct": max(0.8, round(_BASE_DIV * 0.6, 2)),
        "single_hedge_pair_bias": False,
        "prefer_hold": False,
        "label": "Aggressive",
        "blurb": "More slots, looser divergence gates, slightly looser daily halt — same 5m heartbeat. Not no-risk.",
    },
}

_agent_lock = threading.Lock()
_agent_mode = (os.environ.get("CROSSFIRE_AGENT_MODE") or "normal").strip().lower()
if _agent_mode not in AGENT_MODE_PROFILES:
    _agent_mode = "normal"


def get_agent_mode() -> str:
    with _agent_lock:
        return _agent_mode


def set_agent_mode(mode: str) -> dict[str, Any]:
    """Switch live agent mode without restart. Returns active profile snapshot."""
    m = (mode or "").strip().lower()
    if m not in AGENT_MODE_PROFILES:
        raise ValueError("mode must be 'normal' or 'aggressive'")
    global _agent_mode
    with _agent_lock:
        _agent_mode = m
        prof = dict(AGENT_MODE_PROFILES[m])
    prof["mode"] = m
    return prof


def active_profile() -> dict[str, Any]:
    with _agent_lock:
        m = _agent_mode
        prof = dict(AGENT_MODE_PROFILES[m])
    prof["mode"] = m
    if paper_mode():
        # Live 60/20 caps starve Bitget minTradeNum. Paper book is $10k.
        try:
            sleeve = float(os.environ.get("CROSSFIRE_PAPER_SLEEVE_CAP") or "1500")
        except ValueError:
            sleeve = 1500.0
        try:
            leg = float(os.environ.get("CROSSFIRE_PAPER_LEG_CAP") or "400")
        except ValueError:
            leg = 400.0
        try:
            dd = float(os.environ.get("CROSSFIRE_PAPER_DD_HALT") or "8")
        except ValueError:
            dd = 8.0
        try:
            div = float(os.environ.get("CROSSFIRE_PAPER_DIV_PCT") or "0.4")
        except ValueError:
            div = 0.4
        try:
            slots = int(os.environ.get("CROSSFIRE_PAPER_MAX_SLOTS") or "5")
        except ValueError:
            slots = 5
        prof["prefer_hold"] = False
        prof["single_hedge_pair_bias"] = False
        prof["max_slots"] = max(2, min(8, slots))
        prof["sleeve_notional_cap_usdt"] = sleeve
        prof["per_leg_notional_cap_usdt"] = leg
        prof["daily_dd_halt_pct"] = dd
        prof["divergence_threshold_pct"] = div
        prof["label"] = "Paper"
        prof["blurb"] = "Paper $10k book — up to 5 slots, live marks, virtual fills, live Bitget untouched."
    return prof


def active_risk() -> dict[str, Any]:
    """Risk Cage params for the active agent mode (API + checks)."""
    p = active_profile()
    return {
        "max_slots": p["max_slots"],
        "max_lev_us": p["max_lev_us"],
        "max_lev_crypto": p["max_lev_crypto"],
        "daily_dd_halt_pct": p["daily_dd_halt_pct"],
        "sleeve_notional_cap_usdt": p["sleeve_notional_cap_usdt"],
        "per_leg_notional_cap_usdt": p["per_leg_notional_cap_usdt"],
        "agent_mode": p["mode"],
    }


def active_policy() -> dict[str, Any]:
    p = active_profile()
    return {
        "divergence_threshold_pct": p["divergence_threshold_pct"],
        "mag7_symbols": list(POLICY["mag7_symbols"]),
        "btc_symbol": POLICY["btc_symbol"],
        "single_hedge_pair_bias": p["single_hedge_pair_bias"],
        "prefer_hold": p["prefer_hold"],
        "agent_mode": p["mode"],
    }


# Back-compat: RISK / POLICY dicts — prefer active_*() for live values
# Prefer active_risk() — this dict is back-compat only.
# Paper mode overrides max_slots to 5 via active_profile().
RISK = {
    "max_slots": 5,
    "max_lev_us": 20,
    "max_lev_crypto": 50,
    "daily_dd_halt_pct": 5.0,
    "sleeve_notional_cap_usdt": _SLEEVE_CAP,
    "per_leg_notional_cap_usdt": _LEG_CAP,
}

POLICY = {
    "divergence_threshold_pct": _BASE_DIV,
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
    **{s: 20 for s in US_UNIVERSE},
    **{s: 50 for s in CRYPTO_UNIVERSE},
}



def paper_mode() -> bool:
    return bool(PAPER)


def sleeve_connected() -> bool:
    return bool(BITGET_API_KEY and BITGET_SECRET_KEY and BITGET_PASSPHRASE)



def agent_enabled() -> bool:
    with _agent_enabled_lock:
        return bool(AGENT_ENABLED)


def set_agent_enabled(on: bool) -> bool:
    global AGENT_ENABLED
    with _agent_enabled_lock:
        AGENT_ENABLED = bool(on)
        return AGENT_ENABLED

def llm_available() -> bool:
    return bool(OPENROUTER_API_KEY or OPENAI_API_KEY or ANTHROPIC_API_KEY)


def connect_mode_label() -> str:
    if sleeve_connected():
        if MODE == "live":
            return "LIVE"
        if MODE == "demo":
            return "DEMO"
        return "SLEEVE CONNECTED"
    return "SLEEVE DISCONNECTED"


def mode_pill() -> str:
    if paper_mode():
        return "PAPER SLEEVE · LIVE MARKS"
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
    global OPENROUTER_API_KEY, OPENROUTER_BASE, OPENAI_API_KEY, ANTHROPIC_API_KEY, LLM_MODEL, PORT, HOST, AGENT_ENABLED, PAPER
    global _agent_mode, _SLEEVE_CAP, _LEG_CAP, _BASE_DIV, RISK, POLICY
    MODE = (os.environ.get("CROSSFIRE_MODE") or "public").strip().lower()
    if MODE not in ("public", "demo", "live"):
        MODE = "public"
    BITGET_API_KEY = (os.environ.get("BITGET_API_KEY") or "").strip()
    BITGET_SECRET_KEY = (os.environ.get("BITGET_SECRET_KEY") or "").strip()
    BITGET_PASSPHRASE = (os.environ.get("BITGET_PASSPHRASE") or "").strip()
    OPENROUTER_API_KEY = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    _or_base = (os.environ.get("OPENROUTER_BASE") or "https://openrouter.ai/api/v1").strip().rstrip("/")
    OPENROUTER_BASE = _or_base or "https://openrouter.ai/api/v1"
    OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
    ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    LLM_MODEL = (os.environ.get("CROSSFIRE_LLM_MODEL") or "").strip()
    AGENT_ENABLED = _env_bool("CROSSFIRE_AGENT_ENABLED", True)
    PAPER = _env_bool("CROSSFIRE_PAPER", False)
    PORT = int(os.environ.get("CROSSFIRE_PORT") or "8780")
    HOST = os.environ.get("CROSSFIRE_HOST") or "127.0.0.1"
    _SLEEVE_CAP = float(os.environ.get("CROSSFIRE_SLEEVE_CAP") or "2000")
    _LEG_CAP = float(os.environ.get("CROSSFIRE_LEG_CAP") or "500")
    _BASE_DIV = float(os.environ.get("CROSSFIRE_DIV_PCT") or "2.5")
    # Refresh profile notional/div from env
    AGENT_MODE_PROFILES["normal"]["sleeve_notional_cap_usdt"] = _SLEEVE_CAP
    AGENT_MODE_PROFILES["normal"]["per_leg_notional_cap_usdt"] = _LEG_CAP
    AGENT_MODE_PROFILES["normal"]["divergence_threshold_pct"] = _BASE_DIV
    AGENT_MODE_PROFILES["aggressive"]["sleeve_notional_cap_usdt"] = _SLEEVE_CAP
    AGENT_MODE_PROFILES["aggressive"]["per_leg_notional_cap_usdt"] = _LEG_CAP
    AGENT_MODE_PROFILES["aggressive"]["divergence_threshold_pct"] = max(
        0.8, round(_BASE_DIV * 0.6, 2)
    )
    RISK["sleeve_notional_cap_usdt"] = _SLEEVE_CAP
    RISK["per_leg_notional_cap_usdt"] = _LEG_CAP
    AGENT_MODE_PROFILES["normal"]["max_slots"] = int(os.environ.get("CROSSFIRE_MAX_SLOTS") or "2")
    AGENT_MODE_PROFILES["aggressive"]["max_slots"] = int(os.environ.get("CROSSFIRE_MAX_SLOTS_AGG") or "3")
    RISK["max_slots"] = AGENT_MODE_PROFILES["normal"]["max_slots"]
    try:
        _dd = float(os.environ.get("CROSSFIRE_DAILY_DD_HALT") or AGENT_MODE_PROFILES["normal"]["daily_dd_halt_pct"])
    except ValueError:
        _dd = float(AGENT_MODE_PROFILES["normal"]["daily_dd_halt_pct"])
    AGENT_MODE_PROFILES["normal"]["daily_dd_halt_pct"] = _dd
    if (os.environ.get("CROSSFIRE_CAPITAL_PRESERVE") or "").strip().lower() in ("1", "true", "yes", "on"):
        AGENT_MODE_PROFILES["aggressive"]["daily_dd_halt_pct"] = _dd
        AGENT_MODE_PROFILES["normal"]["prefer_hold"] = True
        AGENT_MODE_PROFILES["aggressive"]["prefer_hold"] = True
        AGENT_MODE_PROFILES["aggressive"]["single_hedge_pair_bias"] = True
    else:
        AGENT_MODE_PROFILES["aggressive"]["daily_dd_halt_pct"] = max(_dd, 8.0)
    with _agent_lock:
        _m = _agent_mode
    RISK["daily_dd_halt_pct"] = float(AGENT_MODE_PROFILES[_m]["daily_dd_halt_pct"])
    POLICY["divergence_threshold_pct"] = _BASE_DIV
    am = (os.environ.get("CROSSFIRE_AGENT_MODE") or "normal").strip().lower()
    if am in AGENT_MODE_PROFILES:
        with _agent_lock:
            _agent_mode = am
