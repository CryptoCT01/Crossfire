"""Equity-aware notional → contract size for Crossfire sleeve."""
from __future__ import annotations

import json
import math
import time
import urllib.request
from typing import Any

from . import config

_contract_cache: dict[str, Any] = {"ts": 0.0, "by_sym": {}}


def _ssl_context():
    try:
        import certifi
        import ssl

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        import ssl

        return ssl.create_default_context()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def equity_use_pct() -> float:
    try:
        p = float(os_environ_get("CROSSFIRE_EQUITY_USE_PCT", "0.80"))
    except ValueError:
        p = 0.80
    return max(0.1, min(0.95, p))


def os_environ_get(k: str, default: str = "") -> str:
    import os

    return (os.environ.get(k) or default).strip()


def default_leverage(book: str | None = None) -> int:
    try:
        lev = int(float(os_environ_get("CROSSFIRE_DEFAULT_LEV", "20")))
    except ValueError:
        lev = 20
    # Exchange-aligned ceilings from Risk Cage (US 20 / crypto 50)
    risk = config.active_risk()
    book_l = (book or "").lower()
    if book_l == "us":
        ceiling = int(risk.get("max_lev_us") or 20)
    elif book_l == "crypto":
        ceiling = int(risk.get("max_lev_crypto") or 50)
    else:
        ceiling = max(int(risk.get("max_lev_us") or 20), int(risk.get("max_lev_crypto") or 50))
    return max(1, min(ceiling, lev, 50))


def effective_caps(available_usdt: float | None) -> dict[str, float]:
    """Clamp configured Risk Cage caps to what this wallet can actually carry."""
    risk = config.active_risk()
    sleeve_cfg = float(risk["sleeve_notional_cap_usdt"])
    leg_cfg = float(risk["per_leg_notional_cap_usdt"])
    avail = float(available_usdt or 0.0)
    budget = avail * equity_use_pct() if avail > 0 else 0.0
    sleeve = min(sleeve_cfg, budget) if budget > 0 else sleeve_cfg
    # Keep at least Bitget minTradeUSDT (~5) headroom when possible
    leg = min(leg_cfg, sleeve, max(5.0, budget * 0.33) if budget > 0 else leg_cfg)
    if budget > 0:
        leg = min(leg, budget)
        sleeve = min(sleeve, budget)
    return {
        "sleeve_notional_cap_usdt": round(sleeve, 4),
        "per_leg_notional_cap_usdt": round(leg, 4),
        "available_usdt": round(avail, 6),
        "equity_budget_usdt": round(budget, 4),
        "default_leverage": float(default_leverage(None)),
    }


def fetch_contracts(force: bool = False) -> dict[str, dict[str, Any]]:
    now = time.time()
    if not force and _contract_cache["by_sym"] and now - float(_contract_cache["ts"]) < 600:
        return _contract_cache["by_sym"]
    url = f"{config.BITGET_BASE}/api/v2/mix/market/contracts?productType={config.PRODUCT_TYPE}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Crossfire/0.3"})
    with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if str(payload.get("code")) != "00000":
        raise RuntimeError(f"contracts error: {payload.get('msg')}")
    by: dict[str, dict[str, Any]] = {}
    for row in payload.get("data") or []:
        sym = str(row.get("symbol") or "")
        if sym:
            by[sym] = row
    _contract_cache["by_sym"] = by
    _contract_cache["ts"] = now
    return by


def round_size(size: float, multiplier: float, volume_place: int) -> float:
    if multiplier <= 0:
        multiplier = 10 ** (-max(0, volume_place))
    steps = math.floor(size / multiplier + 1e-12)
    sized = steps * multiplier
    return round(sized, max(0, volume_place))


def size_from_notional(
    symbol: str,
    notional_usdt: float,
    mark: float | None,
    contracts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert USDT notional to exchange contract size; refuse if below mins."""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym = sym + "USDT"
    meta = (contracts or fetch_contracts()).get(sym) or {}
    min_num = _f(meta.get("minTradeNum")) or 0.0
    mult = _f(meta.get("sizeMultiplier")) or min_num or 0.0001
    vol_place = int(_f(meta.get("volumePlace")) or 4)
    min_usdt = _f(meta.get("minTradeUSDT")) or 5.0
    px = _f(mark)
    if px is None or px <= 0:
        return {"ok": False, "symbol": sym, "error": "missing mark price", "size": None}
    if notional_usdt < min_usdt:
        return {
            "ok": False,
            "symbol": sym,
            "error": f"notional {notional_usdt:.4f} < minTradeUSDT {min_usdt}",
            "size": None,
            "min_trade_usdt": min_usdt,
        }
    raw_size = notional_usdt / px
    size = round_size(raw_size, mult, vol_place)
    if size < min_num:
        # bump to minimum contract if min notional still within leg budget * 1.15
        min_notional = min_num * px
        if min_notional <= notional_usdt * 1.15 and min_notional <= (notional_usdt + 2):
            size = round_size(min_num, mult, vol_place)
        else:
            return {
                "ok": False,
                "symbol": sym,
                "error": f"size {size} < minTradeNum {min_num} (need ~{min_notional:.2f} USDT)",
                "size": None,
                "min_trade_num": min_num,
                "min_notional_usdt": min_notional,
            }
    actual_notional = size * px
    if actual_notional < min_usdt * 0.98:
        return {
            "ok": False,
            "symbol": sym,
            "error": f"actual notional {actual_notional:.4f} < minTradeUSDT {min_usdt}",
            "size": None,
        }
    return {
        "ok": True,
        "symbol": sym,
        "size": size,
        "mark": px,
        "notional_usdt": round(actual_notional, 4),
        "min_trade_num": min_num,
        "min_trade_usdt": min_usdt,
        "size_multiplier": mult,
    }


def intent_to_side(intent: str, action: str | None = None) -> str | None:
    i = (intent or "").upper()
    if any(x in i for x in ("LONG", "BUY", "ADD_LONG")):
        return "buy"
    if any(x in i for x in ("SHORT", "SELL", "REDUCE", "TRIM")):
        # REDUCE_OR_SHORT on empty book → open short (sell in one-way)
        return "sell"
    a = (action or "").upper()
    if a in ("FLAT",):
        return None
    return None


def normalize_executable_legs(
    decision: dict[str, Any],
    marks: dict[str, float],
    available_usdt: float,
) -> dict[str, Any]:
    """Turn thesis legs into sized, executable Bitget one-way legs — or refuse."""
    caps = effective_caps(available_usdt)
    leg_cap = caps["per_leg_notional_cap_usdt"]
    sleeve_cap = caps["sleeve_notional_cap_usdt"]
    action = str(decision.get("action") or "HOLD").upper()
    raw_legs = list(decision.get("legs") or [])
    if action in ("HOLD",) or not raw_legs:
        return {
            "ok": True,
            "executable": False,
            "legs": [],
            "caps": caps,
            "message": "no executable legs (HOLD or empty)",
        }
    if sleeve_cap < 5 or available_usdt < 5:
        return {
            "ok": False,
            "executable": False,
            "legs": [],
            "caps": caps,
            "error": f"available {available_usdt:.2f} too small for Bitget minTradeUSDT",
        }

    contracts = fetch_contracts()
    max_legs = int(config.active_risk().get("max_slots") or 3)
    max_legs = max(1, min(8, max_legs))
    chosen: list[dict[str, Any]] = []
    errors: list[str] = []
    spent = 0.0

    for leg in raw_legs:
        if len(chosen) >= max_legs:
            break
        sym = str(leg.get("symbol") or "").upper()
        if not sym and str(leg.get("basket") or "").upper() in ("MAG7", "NVDA"):
            # Single liquid US proxy for small sleeve — never spray Mag7 basket
            sym = "NVDAUSDT"
        if not sym and str(leg.get("book") or "").lower() == "crypto":
            sym = "BTCUSDT"
        if not sym:
            errors.append("leg missing symbol")
            continue
        if not sym.endswith("USDT"):
            sym = sym + "USDT"

        side = str(leg.get("side") or "").lower()
        if side in ("long", "buy"):
            side = "buy"
        elif side in ("short", "sell"):
            side = "sell"
        else:
            side = intent_to_side(str(leg.get("intent") or ""), action) or ""
        if side not in ("buy", "sell"):
            errors.append(f"{sym}: cannot map intent to side")
            continue

        # Remaining budget
        remain = max(0.0, sleeve_cap - spent)
        target = min(leg_cap, remain)
        if target < 5:
            errors.append(f"{sym}: sleeve budget exhausted")
            break

        # Explicit size wins only if notional fits; else size from notional
        mark = marks.get(sym)
        if mark is None and leg.get("mark") is not None:
            mark = _f(leg.get("mark"))

        sized = size_from_notional(sym, target, mark, contracts)
        if not sized.get("ok"):
            errors.append(f"{sym}: {sized.get('error')}")
            continue

        notional = float(sized["notional_usdt"])
        book = str(leg.get("book") or ("crypto" if "BTC" in sym or "ETH" in sym else "us"))
        leg_lev = default_leverage(book)
        margin_needed = notional / max(1, leg_lev)
        if margin_needed > available_usdt * 0.9:
            shrink_notional = min(target, available_usdt * 0.85 * leg_lev)
            sized2 = size_from_notional(sym, shrink_notional, mark, contracts)
            if not sized2.get("ok"):
                errors.append(f"{sym}: cannot fit margin at {leg_lev}x ({sized2.get('error')})")
                continue
            sized = sized2
            notional = float(sized["notional_usdt"])
            margin_needed = notional / max(1, leg_lev)
        chosen.append(
            {
                "symbol": sym,
                "side": side,
                "size": sized["size"],
                "notional_usdt": notional,
                "mark": sized.get("mark"),
                "leverage": leg_lev,
                "marginMode": "crossed",
                "margin_est_usdt": round(margin_needed, 4),
                "intent": leg.get("intent"),
                "book": book,
                "note": leg.get("note") or "sized_for_sleeve",
            }
        )
        spent += notional

    return {
        "ok": bool(chosen),
        "executable": bool(chosen),
        "legs": chosen,
        "caps": caps,
        "spent_notional_usdt": round(spent, 4),
        "errors": errors,
        "message": None if chosen else ("sizing failed: " + "; ".join(errors[:3])),
    }
