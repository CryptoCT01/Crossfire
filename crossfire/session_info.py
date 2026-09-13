"""US cash equity session heuristic (America/New_York RTH)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# Regular trading hours (approx, no holiday calendar)
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def _now_ny() -> datetime:
    return datetime.now(NY)


def us_cash_session(now: datetime | None = None) -> dict[str, Any]:
    n = now or _now_ny()
    if n.tzinfo is None:
        n = n.replace(tzinfo=NY)
    else:
        n = n.astimezone(NY)

    weekday = n.weekday()  # 0=Mon
    weekend = weekday >= 5
    is_rth = (not weekend) and (RTH_OPEN <= n.time() < RTH_CLOSE)

    # Next open
    if weekend:
        days_ahead = (7 - weekday) % 7
        if days_ahead == 0:
            days_ahead = 1
        # Saturday→Monday = 2, Sunday→Monday = 1
        if weekday == 5:
            days_ahead = 2
        elif weekday == 6:
            days_ahead = 1
        next_open = datetime.combine(n.date() + timedelta(days=days_ahead), RTH_OPEN, tzinfo=NY)
        status = "WEEKEND"
    elif n.time() < RTH_OPEN:
        next_open = datetime.combine(n.date(), RTH_OPEN, tzinfo=NY)
        status = "PRE_MARKET"
    elif n.time() >= RTH_CLOSE:
        # next weekday
        d = n.date() + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        next_open = datetime.combine(d, RTH_OPEN, tzinfo=NY)
        status = "AFTER_HOURS"
    else:
        next_open = None
        status = "RTH_OPEN"

    return {
        "timezone": "America/New_York",
        "now_ny": n.isoformat(),
        "weekday": weekday,
        "weekend": weekend,
        "us_cash_rth_open": is_rth,
        "us_cash_status": status,
        "note": "Heuristic RTH 09:30–16:00 ET; no NYSE holiday calendar",
        "next_rth_open_ny": next_open.isoformat() if next_open else None,
        "bitget_us_contracts": "24/7 on Bitget USDT-FUTURES (subject to exchange maintenance)",
    }
