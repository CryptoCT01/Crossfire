"""Paper sleeve — virtual book for CROSSFIRE_PAPER=1.

Honest paper fills at live Bitget marks. Never talks to private Bitget.
Equity starts at CROSSFIRE_PAPER_EQUITY (default 10000) and moves on fills + MTM.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

from . import config
from . import logging_store as store

_DEFAULT = 10_000.0
_PAPER_EQUITY_LOG = "paper_equity.jsonl"
_FILLS_LOG = "fills.jsonl"
_BOOK = config.LOGS_DIR / "paper_book.json"
_lock = threading.Lock()

SL_PCT = float(os.environ.get("CROSSFIRE_PAPER_SL_PCT") or "0.015")
TP_PCT = float(os.environ.get("CROSSFIRE_PAPER_TP_PCT") or "0.025")


def starting_equity() -> float:
    raw = (os.environ.get("CROSSFIRE_PAPER_EQUITY") or "").strip()
    try:
        v = float(raw) if raw else _DEFAULT
    except ValueError:
        v = _DEFAULT
    return max(100.0, v)


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or _now()))


def _empty_book() -> dict[str, Any]:
    eq = starting_equity()
    return {
        "cash": eq,
        "equity": eq,
        "available": eq,
        "realized_pnl": 0.0,
        "positions": [],
        "updated_ts": _now(),
    }


def _load() -> dict[str, Any]:
    try:
        raw = json.loads(_BOOK.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("positions", [])
            raw.setdefault("cash", starting_equity())
            raw.setdefault("realized_pnl", 0.0)
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return _empty_book()


def _save(book: dict[str, Any]) -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    book["updated_ts"] = _now()
    tmp = _BOOK.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(book, indent=2), encoding="utf-8")
    os.replace(tmp, _BOOK)


def _sign(side: str) -> int:
    s = (side or "").lower()
    if s in ("long", "buy"):
        return 1
    return -1


def _norm_side(side: str) -> str:
    s = (side or "").lower()
    if s in ("long", "buy"):
        return "long"
    return "short"


def _revalue(book: dict[str, Any], marks: dict[str, float] | None = None) -> None:
    upnl = 0.0
    margin = 0.0
    for p in book.get("positions") or []:
        sym = str(p.get("symbol") or "")
        if marks and marks.get(sym):
            p["mark"] = float(marks[sym])
        mark = float(p.get("mark") or p.get("entry") or 0)
        entry = float(p.get("entry") or mark)
        size = float(p.get("size") or 0)
        sgn = _sign(str(p.get("side") or "long"))
        u = (mark - entry) * size * sgn
        p["unrealized_pnl"] = round(u, 6)
        p["notional_usdt"] = round(abs(size * mark), 4)
        lev = max(1.0, float(p.get("leverage") or 10))
        margin += abs(size * mark) / lev
        upnl += u
    cash = float(book.get("cash") or starting_equity())
    book["equity"] = round(cash + upnl, 6)
    book["available"] = round(max(0.0, cash - margin + min(0.0, upnl)), 6)
    book["unrealized_pnl"] = round(upnl, 6)


def _append_equity(book: dict[str, Any], tick_id: str | None = None) -> None:
    store.append_jsonl(
        _PAPER_EQUITY_LOG,
        {
            "ts": _now(),
            "ts_iso": _iso(),
            "tick_id": tick_id or "paper",
            "equity": book.get("equity"),
            "available": book.get("available"),
            "cash": book.get("cash"),
            "unrealized_pnl": book.get("unrealized_pnl") or 0.0,
            "source": "paper_sleeve",
            "paper": True,
        },
    )


def ensure_curve_seed() -> None:
    if not config.paper_mode():
        return
    with _lock:
        book = _load()
        _revalue(book)
        _save(book)
        series = store.read_jsonl_tail(_PAPER_EQUITY_LOG, 1)
        if not series:
            _append_equity(book, "paper_seed")


def _public_marks() -> dict[str, float]:
    try:
        from . import bitget_public

        return bitget_public.marks_map()
    except Exception:
        return {}


def account_equity() -> dict[str, Any]:
    ensure_curve_seed()
    marks = _public_marks()
    if marks:
        mark_to_market(marks)
    with _lock:
        book = _load()
        _revalue(book, marks or None)
        _save(book)
        eq = float(book.get("equity") or starting_equity())
        avail = float(book.get("available") or eq)
        return {
            "ok": True,
            "stub": False,
            "paper": True,
            "connected": True,
            "hooks_enabled": True,
            "equity": eq,
            "available": avail,
            "wallet_available": avail,
            "unrealized_pnl": float(book.get("unrealized_pnl") or 0.0),
            "message": f"PAPER sleeve — {eq:.2f} USDT (not your live Bitget balance)",
            "source": "paper_sleeve",
        }


def positions() -> dict[str, Any]:
    ensure_curve_seed()
    marks = _public_marks()
    if marks:
        mark_to_market(marks)
    with _lock:
        book = _load()
        _revalue(book, marks or None)
        _save(book)
        rows = list(book.get("positions") or [])
        return {
            "ok": True,
            "stub": False,
            "paper": True,
            "connected": True,
            "hooks_enabled": True,
            "positions": rows,
            "message": None if rows else "PAPER sleeve — no open positions",
            "source": "paper_sleeve",
        }


def orders() -> dict[str, Any]:
    return {
        "ok": True,
        "stub": False,
        "paper": True,
        "connected": True,
        "hooks_enabled": True,
        "orders": [],
        "message": "PAPER sleeve — market fills, no resting orders",
        "source": "paper_sleeve",
    }


def mark_to_market(marks: dict[str, float] | None = None) -> dict[str, Any]:
    with _lock:
        book = _load()
        prev = float(book.get("equity") or 0)
        _revalue(book, marks)
        _save(book)
        if abs(float(book.get("equity") or 0) - prev) > 1e-6:
            _append_equity(book, "mtm")
        return dict(book)


def _close_leg(book: dict[str, Any], pos: dict[str, Any], mark: float, reason: str, tick_id: str) -> dict[str, Any]:
    size = float(pos.get("size") or 0)
    entry = float(pos.get("entry") or mark)
    sgn = _sign(str(pos.get("side") or "long"))
    pnl = (mark - entry) * size * sgn
    equity_before = float(book.get("equity") or starting_equity())
    book["cash"] = float(book.get("cash") or 0) + pnl
    book["realized_pnl"] = float(book.get("realized_pnl") or 0) + pnl
    close_side = "sell" if _norm_side(str(pos.get("side"))) == "long" else "buy"
    fill = {
        "ts": _now(),
        "ts_iso": _iso(),
        "tick_id": tick_id,
        "symbol": pos.get("symbol"),
        "side": close_side,
        "direction": close_side,
        "size": size,
        "quantity": size,
        "price": mark,
        "notional_usdt": round(abs(size * mark), 4),
        "pnl": round(pnl, 6),
        "equity_before": round(equity_before, 6),
        "reason": reason,
        "paper": True,
        "source": "paper_fill",
        "order_id": f"paper-{uuid.uuid4().hex[:12]}",
        "reduce_only": True,
    }
    return fill


def _apply_leg(book: dict[str, Any], leg: dict[str, Any], tick_id: str) -> dict[str, Any] | None:
    sym = str(leg.get("symbol") or "").upper()
    if sym and not sym.endswith("USDT"):
        sym = sym + "USDT"
    raw_side = str(leg.get("side") or "").lower()
    if raw_side in ("long", "open_long", "buy"):
        want = "long"
        fill_side = "buy"
    elif raw_side in ("short", "open_short", "sell"):
        want = "short"
        fill_side = "sell"
    else:
        return None
    try:
        size = float(leg.get("size") or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0 or not sym:
        return None
    mark = float(leg.get("mark") or 0)
    if mark <= 0:
        return None
    lev = float(leg.get("leverage") or 10)
    equity_before = float(book.get("equity") or starting_equity())
    reduce_only = bool(leg.get("reduceOnly") or leg.get("reduce_only"))

    existing = next((p for p in book["positions"] if p.get("symbol") == sym), None)
    fills_note = ""
    if existing:
        have = _norm_side(str(existing.get("side")))
        if have != want:
            # reduce / flip
            have_size = float(existing.get("size") or 0)
            close_sz = min(have_size, size)
            fill = _close_leg(book, {**existing, "size": close_sz}, mark, "reduce_or_flip", tick_id)
            remain = have_size - close_sz
            if remain <= 1e-12:
                book["positions"] = [p for p in book["positions"] if p.get("symbol") != sym]
            else:
                existing["size"] = remain
            leftover = size - close_sz
            if leftover > 1e-12 and not reduce_only:
                book["positions"].append(
                    {
                        "symbol": sym,
                        "side": want,
                        "size": leftover,
                        "entry": mark,
                        "mark": mark,
                        "leverage": lev,
                        "opened_ts": _now(),
                        "tick_id": tick_id,
                        "paper": True,
                    }
                )
                fills_note = "flip"
            else:
                fills_note = "reduce"
            fill["equity_after"] = None
            fill["note"] = fills_note
            _revalue(book)
            fill["equity_after"] = round(float(book.get("equity") or 0), 6)
            fill["equity_change"] = round(fill["equity_after"] - equity_before, 6)
            return fill
        # same side — REJECT add/VWAP (do not stack). One open per symbol.
        return None
    else:
        if reduce_only:
            return None
        book["positions"].append(
            {
                "symbol": sym,
                "side": want,
                "size": size,
                "entry": mark,
                "mark": mark,
                "leverage": lev,
                "opened_ts": _now(),
                "tick_id": tick_id,
                "paper": True,
            }
        )
        fills_note = "open"

    _revalue(book)
    equity_after = float(book.get("equity") or 0)
    return {
        "ts": _now(),
        "ts_iso": _iso(),
        "tick_id": tick_id,
        "symbol": sym,
        "side": fill_side,
        "direction": fill_side,
        "size": size,
        "quantity": size,
        "price": mark,
        "notional_usdt": round(abs(size * mark), 4),
        "pnl": 0.0,
        "equity_before": round(equity_before, 6),
        "equity_after": round(equity_after, 6),
        "equity_change": round(equity_after - equity_before, 6),
        "paper": True,
        "source": "paper_fill",
        "order_id": f"paper-{uuid.uuid4().hex[:12]}",
        "note": fills_note,
        "leverage": lev,
    }


def place_order(payload: dict[str, Any]) -> dict[str, Any]:
    """Virtual market fills at provided marks. Never hits Bitget."""
    legs = list(payload.get("legs") or [])
    tick_id = str(payload.get("tick_id") or f"paper-{int(_now()*1000)}")
    results: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    with _lock:
        book = _load()
        for leg in legs:
            fill = _apply_leg(book, leg, tick_id)
            if not fill:
                results.append({"ok": False, "symbol": leg.get("symbol"), "error": "paper fill refused"})
                continue
            results.append({"ok": True, "symbol": fill.get("symbol"), "side": fill.get("side"), "size": fill.get("size")})
            fills.append(fill)
            store.append_jsonl(_FILLS_LOG, fill)
        _revalue(book)
        _save(book)
        if fills:
            _append_equity(book, tick_id)
        any_ok = bool(fills)
        return {
            "ok": any_ok,
            "paper": True,
            "stub": False,
            "hooks_enabled": True,
            "results": results,
            "fills": fills,
            "equity": book.get("equity"),
            "message": f"PAPER fills {len(fills)}/{len(legs)}" if any_ok else "no paper legs filled",
            "error": None if any_ok else "all paper legs failed",
        }


def manage_exits(marks: dict[str, float] | None, tick_id: str) -> list[dict[str, Any]]:
    """Hard SL / TP on paper legs. Price % vs entry."""
    fills: list[dict[str, Any]] = []
    with _lock:
        book = _load()
        _revalue(book, marks)
        keep: list[dict[str, Any]] = []
        for pos in list(book.get("positions") or []):
            sym = str(pos.get("symbol") or "")
            mark = float((marks or {}).get(sym) or pos.get("mark") or pos.get("entry") or 0)
            entry = float(pos.get("entry") or 0)
            if entry <= 0 or mark <= 0:
                keep.append(pos)
                continue
            sgn = _sign(str(pos.get("side") or "long"))
            move = (mark - entry) / entry * sgn
            reason = None
            if move <= -SL_PCT:
                reason = f"paper_sl_{SL_PCT*100:.1f}pct"
            elif move >= TP_PCT:
                reason = f"paper_tp_{TP_PCT*100:.1f}pct"
            if reason:
                fill = _close_leg(book, pos, mark, reason, tick_id)
                _revalue(book)
                fill["equity_after"] = round(float(book.get("equity") or 0), 6)
                fill["equity_change"] = round(fill["equity_after"] - float(fill.get("equity_before") or 0), 6)
                store.append_jsonl(_FILLS_LOG, fill)
                fills.append(fill)
            else:
                keep.append(pos)
        book["positions"] = keep
        _revalue(book, marks)
        _save(book)
        if fills:
            _append_equity(book, tick_id)
    return fills


def flatten_all(marks: dict[str, float] | None = None) -> dict[str, Any]:
    fills: list[dict[str, Any]] = []
    with _lock:
        book = _load()
        _revalue(book, marks)
        tick_id = f"paper-kill-{int(_now()*1000)}"
        for pos in list(book.get("positions") or []):
            sym = str(pos.get("symbol") or "")
            mark = float((marks or {}).get(sym) or pos.get("mark") or pos.get("entry") or 0)
            fill = _close_leg(book, pos, mark, "paper_kill", tick_id)
            _revalue(book)
            fill["equity_after"] = round(float(book.get("equity") or 0), 6)
            fill["equity_change"] = round(fill["equity_after"] - float(fill.get("equity_before") or 0), 6)
            store.append_jsonl(_FILLS_LOG, fill)
            fills.append(fill)
        book["positions"] = []
        _revalue(book)
        _save(book)
        if fills:
            _append_equity(book, tick_id)
    return {
        "ok": True,
        "paper": True,
        "flattened": fills,
        "cancelled": [],
        "message": f"PAPER kill — flattened {len(fills)} legs (live Bitget untouched)",
    }
