"""Agent tick pipeline: Dual Book → policy/LLM → Risk Cage → JSONL (no fake fills)."""
from __future__ import annotations

import json
import threading
import time
import uuid
import urllib.error
import urllib.request
from typing import Any

from . import bitget_private, bitget_public, config, sizing
from . import logging_store as store
from . import paper_sleeve
from . import perception
from . import session_info

def _ssl_context():
    try:
        import certifi
        import ssl
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        import ssl
        return ssl.create_default_context()


_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "IDLE",
    "last_decision": None,
    "last_context": None,
    "last_tick_id": None,
    "last_heartbeat_ts": None,
    "next_heartbeat_ts": None,
    "connect_mode": None,
    "engine": None,
    "kill_armed": False,
    "last_marks": {},
    "running_tick": False,
}


def _avg_change(rows: list[dict[str, Any]], symbols: list[str]) -> float | None:
    vals = []
    want = set(symbols)
    for r in rows:
        if r.get("symbol") in want and r.get("change24h_pct") is not None:
            vals.append(float(r["change24h_pct"]))
    if not vals:
        return None
    return sum(vals) / len(vals)


def _min_confidence() -> float:
    import os
    if config.paper_mode():
        try:
            return max(0.2, min(0.95, float(os.environ.get("CROSSFIRE_PAPER_MIN_CONF") or "0.35")))
        except ValueError:
            return 0.35
    try:
        return max(0.5, min(0.95, float(os.environ.get("CROSSFIRE_MIN_CONFIDENCE") or "0.75")))
    except ValueError:
        return 0.75


def _capital_preserve() -> bool:
    import os
    if config.paper_mode():
        return False
    return (os.environ.get("CROSSFIRE_CAPITAL_PRESERVE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _day_equity_baseline(current_equity: float | None) -> dict[str, Any]:
    """UTC-day equity baseline from equity.jsonl; seed with current if empty."""
    import datetime as _dt

    day = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    eq_log = "paper_equity.jsonl" if config.paper_mode() else "equity.jsonl"
    series = store.read_jsonl_tail(eq_log, 2000)
    day_points = []
    for row in series:
        ts = float(row.get("ts") or 0)
        if ts <= 0:
            continue
        d = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        if d == day and row.get("equity") is not None:
            try:
                day_points.append((ts, float(row["equity"])))
            except (TypeError, ValueError):
                pass
    if day_points:
        day_points.sort(key=lambda x: x[0])
        baseline = day_points[0][1]
        seeded = False
    elif current_equity is not None:
        baseline = float(current_equity)
        seeded = True
        store.append_jsonl(
            eq_log,
            {"ts": time.time(), "tick_id": "day_baseline", "equity": baseline, "day": day, "paper": config.paper_mode()},
        )
    else:
        return {"day": day, "baseline": None, "dd_pct": None, "seeded": False}
    dd_pct = None
    if current_equity is not None and baseline:
        dd_pct = (float(current_equity) - baseline) / baseline * 100.0
    return {"day": day, "baseline": baseline, "dd_pct": dd_pct, "seeded": seeded, "n_points": len(day_points)}


def risk_cage_check(decision: dict[str, Any], sleeve: dict[str, Any], sized: dict[str, Any] | None = None) -> dict[str, Any]:
    """Enforce Risk Cage. Never invent clearance for live orders without sleeve."""
    risk = config.active_risk()
    checks = []
    ok = True
    reason = "cleared"

    slots_used = len(sleeve.get("positions") or [])
    max_slots = risk["max_slots"]
    checks.append(
        {
            "rule": "max_slots",
            "limit": max_slots,
            "current": slots_used,
            "pass": slots_used < max_slots or decision.get("action") in ("HOLD", "FLAT", "REDUCE"),
        }
    )

    if decision.get("action") in ("HEDGE", "ROTATE") and not config.sleeve_connected():
        ok = False
        reason = "sleeve disconnected — decision logged as thesis only; no order"
        checks.append({"rule": "sleeve_connected", "pass": False, "detail": reason})
    else:
        checks.append({"rule": "sleeve_connected", "pass": config.sleeve_connected()})

    # Live equity / available for DD + micro-sleeve caps
    equity = None
    available = None
    try:
        eq = bitget_private.account_equity()
        if eq.get("ok"):
            equity = eq.get("equity")
            available = eq.get("available")
    except Exception:
        pass

    caps = sizing.effective_caps(float(available or 0.0))
    checks.append(
        {
            "rule": "per_leg_notional_cap_usdt",
            "limit": caps["per_leg_notional_cap_usdt"],
            "configured": risk["per_leg_notional_cap_usdt"],
            "pass": True,
            "detail": f"effective leg cap {caps['per_leg_notional_cap_usdt']} (wallet-aware)",
        }
    )
    checks.append(
        {
            "rule": "sleeve_notional_cap_usdt",
            "limit": caps["sleeve_notional_cap_usdt"],
            "configured": risk["sleeve_notional_cap_usdt"],
            "pass": True,
            "detail": f"effective sleeve cap {caps['sleeve_notional_cap_usdt']} (wallet-aware)",
        }
    )

    dd_pass = True
    dd_detail = "equity UNAVAILABLE until sleeve connected"
    day_info: dict[str, Any] = {}
    if equity is not None:
        day_info = _day_equity_baseline(float(equity))
        dd_pct = day_info.get("dd_pct")
        halt = float(risk["daily_dd_halt_pct"])
        if float(available or 0) < 5:
            dd_pass = False
            dd_detail = f"available {available} < Bitget minTradeUSDT"
        elif dd_pct is not None and dd_pct <= -halt:
            dd_pass = False
            dd_detail = f"day DD {dd_pct:.2f}% hit halt -{halt}% (baseline {day_info.get('baseline')})"
        else:
            dd_detail = (
                f"live equity {float(equity):.2f}; day DD {dd_pct if dd_pct is not None else 0:.2f}% "
                f"(halt -{halt}%); baseline {day_info.get('baseline')}"
            )
    checks.append(
        {
            "rule": "daily_dd_halt_pct",
            "limit": risk["daily_dd_halt_pct"],
            "current": day_info.get("dd_pct") if day_info else equity,
            "pass": dd_pass,
            "detail": dd_detail,
            "baseline": day_info.get("baseline") if day_info else None,
        }
    )

    # Sized legs must exist + fit caps before exec
    exec_legs_ok = True
    if decision.get("action") not in ("HOLD",) and (config.paper_mode() or config.sleeve_connected()):
        if not sized or not sized.get("executable"):
            exec_legs_ok = False
            checks.append(
                {
                    "rule": "sized_legs",
                    "pass": False,
                    "detail": (sized or {}).get("message") or (sized or {}).get("error") or "legs not sized for wallet",
                }
            )
        else:
            spent = float(sized.get("spent_notional_usdt") or 0)
            over = spent > float(caps["sleeve_notional_cap_usdt"]) + 1e-6
            checks.append(
                {
                    "rule": "sized_legs",
                    "pass": not over,
                    "spent": spent,
                    "limit": caps["sleeve_notional_cap_usdt"],
                    "legs": len(sized.get("legs") or []),
                }
            )
            if over:
                exec_legs_ok = False
            for leg in sized.get("legs") or []:
                n = float(leg.get("notional_usdt") or 0)
                if n > float(caps["per_leg_notional_cap_usdt"]) + 1e-6:
                    exec_legs_ok = False
                    checks.append({"rule": "per_leg_fit", "symbol": leg.get("symbol"), "pass": False, "notional": n})

    for c in checks:
        if c.get("pass") is False and c.get("rule") not in ("sleeve_connected",):
            ok = False
            reason = f"blocked by {c['rule']}"

    if not exec_legs_ok and decision.get("action") not in ("HOLD",):
        ok = False
        reason = "blocked by sized_legs"

    # Confidence floor (capital preservation)
    conf = None
    try:
        conf = float(decision.get("confidence")) if decision.get("confidence") is not None else None
    except (TypeError, ValueError):
        conf = None
    min_c = _min_confidence()
    conf_ok = True
    if decision.get("action") in ("HEDGE", "ROTATE") and (conf is None or conf < min_c):
        conf_ok = False
        checks.append(
            {
                "rule": "min_confidence",
                "limit": min_c,
                "current": conf,
                "pass": False,
                "detail": f"need confidence >= {min_c} to open risk",
            }
        )
        ok = False
        reason = "blocked by min_confidence"
    else:
        checks.append({"rule": "min_confidence", "limit": min_c, "current": conf, "pass": True})

    # Capital preserve: block ROTATE adds; force prefer HOLD on weak context
    if _capital_preserve() and decision.get("action") == "ROTATE":
        checks.append({"rule": "capital_preserve_no_rotate", "pass": False})
        ok = False
        reason = "blocked by capital_preserve_no_rotate"
        conf_ok = False

    if not dd_pass and decision.get("action") in ("HEDGE", "ROTATE"):
        ok = False
        reason = "blocked by daily_dd_halt_pct"

    # Sleeve disconnect blocks execution but does not invalidate the thesis record
    exec_allowed = (
        ok
        and conf_ok
        and dd_pass
        and (config.paper_mode() or config.sleeve_connected())
        and decision.get("action") not in ("HOLD",)
        and bool(sized and sized.get("executable"))
    )
    if config.paper_mode() and decision.get("action") not in ("HOLD",):
        checks.append({"rule": "paper_mode_fills", "pass": True, "detail": "paper — virtual fills at live marks, Bitget private unused"})
        if exec_allowed:
            reason = "paper_fill_cleared"
        else:
            reason = reason or "paper — blocked by cage"
    if decision.get("action") in ("HOLD",):
        exec_allowed = False
        reason = "HOLD — no execution"

    # Closing an open leg is management, not new risk. Caps, confidence, and
    # day-DD block HEDGE/ROTATE only — they must not trap a FLAT/REDUCE.
    if decision.get("management_close") and str(decision.get("action") or "").upper() in ("FLAT", "REDUCE"):
        can_close = bool(sized and sized.get("executable")) and (
            config.paper_mode() or config.sleeve_connected()
        )
        checks.append(
            {
                "rule": "management_close",
                "pass": can_close,
                "detail": "reduce-only close of open legs; empty model legs filled from the book",
            }
        )
        if can_close:
            exec_allowed = True
            ok = True
            reason = "management_close_cleared"
        else:
            exec_allowed = False
            reason = "FLAT/REDUCE — no open leg to close"

    return {
        "ok": ok,
        "exec_allowed": exec_allowed,
        "reason": reason,
        "risk": {**risk, **{k: caps[k] for k in ("sleeve_notional_cap_usdt", "per_leg_notional_cap_usdt", "equity_budget_usdt", "available_usdt", "default_leverage") if k in caps}},
        "agent_mode": config.get_agent_mode(),
        "checks": checks,
        "sizing": sized,
    }



def policy_decide(books: dict[str, Any]) -> dict[str, Any]:
    """Transparent rule path when no LLM key. Labels engine: policy."""
    pol = config.active_policy()
    prof = config.active_profile()
    mode = config.get_agent_mode()
    us = books.get("us") or []
    crypto = books.get("crypto") or []
    mag7 = _avg_change(us, pol["mag7_symbols"])
    btc_row = next((r for r in crypto if r.get("symbol") == pol["btc_symbol"]), None)
    btc = float(btc_row["change24h_pct"]) if btc_row and btc_row.get("change24h_pct") is not None else None

    thr = float(pol["divergence_threshold_pct"])
    divergence = None
    action = "HOLD"
    thesis = (
        f"POLICY HOLD [{mode.upper()}]: Mag7 24h avg vs BTC divergence below {thr:.2f}% threshold "
        f"(or insufficient marks)."
    )
    legs: list[dict[str, Any]] = []

    if mag7 is not None and btc is not None:
        divergence = mag7 - btc
        # Aggressive: also consider ROTATE when divergence is strong and multi-slot bias
        rotate_thr = thr * 1.35
        if abs(divergence) >= thr:
            if mode == "aggressive" and abs(divergence) >= rotate_thr and not pol.get("prefer_hold"):
                action = "ROTATE"
            else:
                action = "HEDGE"
            if divergence > 0:
                thesis = (
                    f"POLICY {action} [{mode.upper()}]: Mag7 24h avg {mag7:+.2f}% vs BTC {btc:+.2f}% "
                    f"(divergence {divergence:+.2f}% ≥ {thr:.2f}%). "
                    f"Rule: propose cross-hedge — reduce US-stock-contract risk / add BTC perp hedge. "
                    f"Thesis is rule-based, not LLM."
                )
                legs = [
                    {"book": "us", "intent": "REDUCE_OR_SHORT", "basket": "Mag7", "note": "policy"},
                    {"book": "crypto", "intent": "LONG_HEDGE", "symbol": "BTCUSDT", "note": "policy"},
                ]
                if mode == "aggressive" and not pol.get("single_hedge_pair_bias"):
                    # Extra sleeve candidate — still thesis-only until sleeve executes
                    eth = next((r for r in crypto if r.get("symbol") == "ETHUSDT"), None)
                    if eth and eth.get("available"):
                        legs.append(
                            {
                                "book": "crypto",
                                "intent": "SATELLITE_HEDGE",
                                "symbol": "ETHUSDT",
                                "note": "aggressive multi-slot bias",
                            }
                        )
            else:
                thesis = (
                    f"POLICY {action} [{mode.upper()}]: Mag7 24h avg {mag7:+.2f}% vs BTC {btc:+.2f}% "
                    f"(divergence {divergence:+.2f}% ≤ -{thr:.2f}%). "
                    f"Rule: propose cross-hedge — BTC weakness vs US strength flip: "
                    f"trim BTC risk / add Mag7 contract hedge. Thesis is rule-based, not LLM."
                )
                legs = [
                    {"book": "crypto", "intent": "REDUCE_OR_SHORT", "symbol": "BTCUSDT", "note": "policy"},
                    {"book": "us", "intent": "LONG_HEDGE", "basket": "Mag7", "note": "policy"},
                ]
                if mode == "aggressive" and not pol.get("single_hedge_pair_bias"):
                    legs.append(
                        {
                            "book": "us",
                            "intent": "SATELLITE_HEDGE",
                            "basket": "NVDA",
                            "symbol": "NVDAUSDT",
                            "note": "aggressive multi-slot bias",
                        }
                    )
        else:
            thesis = (
                f"POLICY HOLD [{mode.upper()}]: Mag7 24h avg {mag7:+.2f}% vs BTC {btc:+.2f}% "
                f"(divergence {divergence:+.2f}%, threshold ±{thr:.2f}%). No action."
            )

    # Policy observes divergence. It must not open. A 24h print is not an entry.
    suppressed = action if action in ("HEDGE", "ROTATE") else None
    if suppressed:
        thesis = (
            f"POLICY HOLD [{mode.upper()}]: Mag7 24h avg {mag7:+.2f}% vs BTC {btc:+.2f}% "
            f"(divergence {divergence:+.2f}%, threshold ±{thr:.2f}%). "
            f"Would have been {suppressed}; policy does not open risk. "
            f"LLM must pass Rules 2–10. Thesis is rule-based, not LLM."
        )
        action = "HOLD"
        legs = []
    return {
        "engine": "policy",
        "model": None,
        "action": "HOLD" if suppressed else action,
        "thesis": thesis,
        "mag7_24h_pct": mag7,
        "btc_24h_pct": btc,
        "divergence_pct": divergence,
        "threshold_pct": thr,
        "agent_mode": mode,
        "legs": [] if suppressed else legs,
        "confidence": 0.7,
        "profile_label": prof.get("label"),
        "note": "policy_open_blocked" if suppressed else None,
        "suppressed_action": suppressed,
    }


def paper_seed_hedge(books: dict[str, Any], sess: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    """Disabled. A flat book stays flat until the LLM passes Rules 2–10."""
    del books, sess
    mag7 = derived.get("mag7_24h_pct")
    btc = derived.get("btc_24h_pct")
    div = derived.get("divergence_pct")
    return {
        "engine": "policy",
        "model": None,
        "action": "HOLD",
        "thesis": (
            "POLICY HOLD: paper-seed hedge disabled. Flat book stays flat until the LLM "
            f"passes Rules 2–10 (Mag7 {mag7} vs BTC {btc}, div {div})."
        ),
        "legs": [],
        "confidence": 0.7,
        "paper_seed": False,
        "note": "paper_seed_disabled",
        "divergence_pct": div,
        "mag7_24h_pct": mag7,
        "btc_24h_pct": btc,
    }


_RULEBOOK_CACHE: str | None = None
_RULEBOOK_FALLBACK = (
    "You are Crossfire cross-asset agent. Follow Risk Cage. "
    "Reply JSON only: "
    '{"action":"HOLD|HEDGE|ROTATE|REDUCE|FLAT","thesis":"...","legs":[],'
    '"confidence":0-1,"rules_fired":[]}'
)


def _load_rulebook() -> str:
    """Load 13-rule tick rulebook once; short fallback if missing."""
    global _RULEBOOK_CACHE
    if _RULEBOOK_CACHE is not None:
        return _RULEBOOK_CACHE
    path = config.ROOT / "prompts" / "crossfire_tick_rules_v1.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            _RULEBOOK_CACHE = text
            return _RULEBOOK_CACHE
    except OSError:
        pass
    _RULEBOOK_CACHE = _RULEBOOK_FALLBACK
    return _RULEBOOK_CACHE


def _safe_err(exc: BaseException) -> str:
    """Format LLM errors without leaking secrets from exception strings."""
    msg = str(exc)
    for secret in (
        config.OPENROUTER_API_KEY,
        config.OPENAI_API_KEY,
        config.ANTHROPIC_API_KEY,
    ):
        if secret and secret in msg:
            msg = msg.replace(secret, "[redacted]")
    return f"LLM call failed: {type(exc).__name__}: {msg}"


def _tick_user_payload(books: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact tick JSON for the LLM — never secrets. Full Dual Book fields the agent needs."""
    pol = config.active_policy()
    risk = config.active_risk()
    us = books.get("us") or []
    crypto = books.get("crypto") or []
    mag7 = _avg_change(us, pol["mag7_symbols"])
    btc_row = next((r for r in crypto if r.get("symbol") == pol["btc_symbol"]), None)
    btc = (
        float(btc_row["change24h_pct"])
        if btc_row and btc_row.get("change24h_pct") is not None
        else None
    )
    divergence = (mag7 - btc) if mag7 is not None and btc is not None else None

    def _row(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "s": r.get("symbol"),
            "m": r.get("mark"),
            "c24": r.get("change24h_pct"),
            "bid": r.get("bid"),
            "ask": r.get("ask"),
            "hi": r.get("high24h"),
            "lo": r.get("low24h"),
            "fr": r.get("funding_rate"),
            "max_lev": r.get("exchange_max_lev"),
        }

    positions_summary: list[dict[str, Any]] = []
    equity_snap: dict[str, Any] = {}
    # Always load positions from active sleeve (paper routes to paper_sleeve).
    # Previously only ran when sleeve_connected — LLM often saw empty positions and re-opened.
    if config.paper_mode() or config.sleeve_connected():
        try:
            sleeve = bitget_private.positions()
            for p in sleeve.get("positions") or []:
                if not isinstance(p, dict):
                    continue
                positions_summary.append(
                    {
                        "symbol": p.get("symbol") or p.get("symbolName"),
                        "side": p.get("side") or p.get("holdSide"),
                        "size": p.get("size") or p.get("total"),
                        "entry": p.get("entry"),
                        "mark": p.get("mark"),
                        "unrealized_pnl": p.get("unrealized_pnl") or p.get("unrealizedPL"),
                        "leverage": p.get("leverage"),
                    }
                )
        except Exception:
            positions_summary = []
        try:
            eq = bitget_private.account_equity()
            if eq.get("ok"):
                equity_snap = {
                    "equity": eq.get("equity"),
                    "available": eq.get("available"),
                    "unrealized_pnl": eq.get("unrealized_pnl"),
                }
                day = _day_equity_baseline(eq.get("equity"))
                equity_snap["day_baseline"] = day.get("baseline")
                equity_snap["day_dd_pct"] = day.get("dd_pct")
                equity_snap["day"] = day.get("day")
                caps = sizing.effective_caps(float(eq.get("available") or 0))
                equity_snap["effective_caps"] = caps
        except Exception:
            equity_snap = {"error": "equity_fetch_failed"}

    capital = _capital_preserve()
    payload: dict[str, Any] = {
        "us": [_row(r) for r in us if r.get("available")],
        "crypto": [_row(r) for r in crypto if r.get("available")],
        "session": session_info.us_cash_session(),
        "risk": risk,
        "agent_mode": config.get_agent_mode(),
        "capital_preserve": capital,
        "min_confidence_to_execute": _min_confidence(),
        "sleeve_connected": config.sleeve_connected(),
        "wallet": equity_snap,
        "positions": positions_summary,
        "derived": {
            "mag7_24h_pct": mag7,
            "btc_24h_pct": btc,
            "divergence_pct": divergence,
            "threshold_pct": float(pol["divergence_threshold_pct"]),
        },
        "capital_mandate": (
            "PAPER SLEEVE: $10k virtual book at live marks (not live Bitget). "
            "Default HOLD when thesis already expressed. Max 5 slots. "
            "ONE position per symbol — never OPEN/ADD/average a symbol that is already open; "
            "only REDUCE or FLAT that symbol. No same-side stacking. "
            "HEDGE only for a NEW symbol pair when Rules 2–10 pass. "
            "When US cash is CLOSED/PRE_MARKET/WEEKEND, manage existing cross-hedge; do not churn."
            if config.paper_mode()
            else (
                "CAPITAL PRESERVATION ON: prefer HOLD. Do not open new risk unless confidence "
                f">= {_min_confidence():.2f}, divergence is clear, Rule 2/5 pass, and day DD is inside "
                f"{risk.get('daily_dd_halt_pct')}%. Never average down. Prefer REDUCE/FLAT over hope. "
                "Micro sleeve (~$90): small sized legs only; losing more money is unacceptable."
                if capital
                else "Follow Risk Cage; prefer capital safety over activity."
            )
        ),
        "data_honesty": (
            "context.news and geopolitics may be unknown — never invent headlines. "
            "Use Dual Book marks, funding, breadth, session, wallet, positions, and "
            "CMC Witness (context.cmc / context.cmc_brief) when present. "
            "If context.cmc.ok is false, treat CMC as unknown. Mag7 is Dual Book, not CMC."
        ),
        "reply_schema": (
            "Reply JSON only: "
            '{"action":"HOLD|HEDGE|ROTATE|REDUCE|FLAT","thesis":"...","legs":[],'
            '"confidence":0-1,"rules_fired":[]}'
        ),
    }
    if context:
        safe_ctx = {
            k: v
            for k, v in context.items()
            if not any(s in str(k).lower() for s in ("key", "secret", "pass", "token", "auth"))
        }
        if safe_ctx:
            payload["context"] = safe_ctx
    return payload



def _parse_llm_json(content: str) -> dict[str, Any]:
    content = (content or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def _llm_result(model: str, parsed: dict[str, Any]) -> dict[str, Any]:
    out = {
        "engine": "llm",
        "model": model,
        "action": str(parsed.get("action") or "HOLD").upper(),
        "thesis": str(parsed.get("thesis") or ""),
        "legs": parsed.get("legs") or [],
        "confidence": float(parsed.get("confidence") or 0.5),
        "raw_ok": True,
    }
    if "rules_fired" in parsed:
        out["rules_fired"] = parsed.get("rules_fired")
    return out


def llm_decide(
    books: dict[str, Any], context: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Call LLM if key present. Prefer OpenRouter; fall back OpenAI/Anthropic.
    Returns None if no key; on call failure returns fallback True with error.
    """
    if not config.llm_available():
        return None

    system = _load_rulebook()
    user_payload = _tick_user_payload(books, context)
    user_content = json.dumps(user_payload, separators=(",", ":"))

    # --- OpenRouter (preferred) ---
    if config.OPENROUTER_API_KEY:
        model = config.LLM_MODEL or "deepseek/deepseek-v4.1-flash"
        body = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        url = f"{config.OPENROUTER_BASE}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "User-Agent": "Crossfire/0.2",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45, context=_ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            parsed = _parse_llm_json(content)
            return _llm_result(model, parsed)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
            KeyError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as e:
            return {
                "engine": "llm",
                "model": model,
                "error": _safe_err(e),
                "fallback": True,
            }

    # --- OpenAI (secondary) ---
    if config.OPENAI_API_KEY:
        model = config.LLM_MODEL or "gpt-4o-mini"
        body = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                "User-Agent": "Crossfire/0.2",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            parsed = _parse_llm_json(content)
            return _llm_result(model, parsed)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
            KeyError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as e:
            return {
                "engine": "llm",
                "model": model,
                "error": _safe_err(e),
                "fallback": True,
            }

    # --- Anthropic (secondary) ---
    if config.ANTHROPIC_API_KEY:
        model = config.LLM_MODEL or "claude-3-5-haiku-latest"
        body = {
            "model": model,
            "max_tokens": 1200,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "User-Agent": "Crossfire/0.2",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            text = ""
            for block in payload.get("content") or []:
                if block.get("type") == "text":
                    text += block.get("text") or ""
            parsed = _parse_llm_json(text)
            return _llm_result(model, parsed)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
            KeyError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as e:
            return {
                "engine": "llm",
                "model": model,
                "error": _safe_err(e),
                "fallback": True,
            }

    return None


_US_BOOK = {
    "AAPLUSDT", "TSLAUSDT", "NVDAUSDT", "METAUSDT", "AMZNUSDT", "MSFTUSDT",
    "GOOGLUSDT", "NFLXUSDT", "AMDUSDT", "COINUSDT", "MSTRUSDT", "SPYUSDT",
}


def _sym_usdt(raw: str) -> str:
    sym = str(raw or "").upper()
    if sym and not sym.endswith("USDT"):
        sym += "USDT"
    return sym


def _management_close_legs(
    positions: list[dict[str, Any]],
    marks: dict[str, float],
    raw_legs: list[dict[str, Any]] | None,
    action: str,
) -> list[dict[str, Any]]:
    """Build reduce-only closes from the open book. Never size a new leg.

    FLAT, or REDUCE with no named symbol, closes every open leg at its real
    size. REDUCE that names symbols closes only those that are actually open.
    """
    open_rows: list[tuple[str, str, float, dict[str, Any]]] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        sym = _sym_usdt(str(pos.get("symbol") or pos.get("symbolName") or ""))
        try:
            size = float(pos.get("size") or pos.get("total") or 0)
        except (TypeError, ValueError):
            size = 0.0
        side = str(pos.get("side") or pos.get("holdSide") or "").lower()
        if side in ("buy", "long", "open_long"):
            side = "long"
        elif side in ("sell", "short", "open_short"):
            side = "short"
        if not sym or size <= 0 or side not in ("long", "short"):
            continue
        open_rows.append((sym, side, size, pos))

    named: set[str] = set()
    for leg in raw_legs or []:
        if not isinstance(leg, dict):
            continue
        sym = _sym_usdt(str(leg.get("symbol") or ""))
        if not sym and str(leg.get("basket") or "").upper() in ("MAG7", "NVDA"):
            sym = "NVDAUSDT"
        if sym:
            named.add(sym)

    if action == "REDUCE" and named:
        targets = [row for row in open_rows if row[0] in named]
    else:
        targets = open_rows

    legs: list[dict[str, Any]] = []
    for sym, side, size, pos in targets:
        mark = marks.get(sym)
        if mark is None:
            try:
                mark = float(pos.get("mark") or pos.get("entry") or 0)
            except (TypeError, ValueError):
                mark = 0.0
        if not mark or float(mark) <= 0:
            continue
        close_side = "sell" if side == "long" else "buy"
        legs.append(
            {
                "symbol": sym,
                "side": close_side,
                "size": size,
                "mark": float(mark),
                "notional_usdt": round(abs(size * float(mark)), 4),
                "leverage": float(pos.get("leverage") or 10),
                "reduceOnly": True,
                "reduce_only": True,
                "intent": "CLOSE" if action == "FLAT" else "REDUCE",
                "book": "us" if sym in _US_BOOK else "crypto",
                "note": "management_close",
            }
        )
    return legs


def run_tick(trigger: str = "heartbeat") -> dict[str, Any]:
    """Full pipeline tick. Always real marks; never fake fills/positions."""
    with _state_lock:
        if _state.get("running_tick"):
            return {"ok": False, "error": "tick already running"}
        _state["running_tick"] = True
        _state["status"] = "TICKING"

    tick_id = f"t_{store.now_ms()}_{uuid.uuid4().hex[:8]}"
    t0 = time.time()
    sess = session_info.us_cash_session()

    try:
        books = bitget_public.get_books(force=True)
        marks = bitget_public.marks_map(books)

        if config.paper_mode():
            paper_sleeve.mark_to_market(marks)
            paper_sleeve.manage_exits(marks, tick_id)

        # Rule-2 perception pack — real books+session only; never invent news
        pol = config.active_policy()
        mag7 = _avg_change(books.get("us") or [], pol["mag7_symbols"])
        btc_row = next(
            (r for r in (books.get("crypto") or []) if r.get("symbol") == pol["btc_symbol"]),
            None,
        )
        btc = (
            float(btc_row["change24h_pct"])
            if btc_row and btc_row.get("change24h_pct") is not None
            else None
        )
        derived = {
            "mag7_24h_pct": mag7,
            "btc_24h_pct": btc,
            "divergence_pct": (mag7 - btc) if mag7 is not None and btc is not None else None,
            "threshold_pct": float(pol["divergence_threshold_pct"]),
        }
        context = perception.build_context(books, sess, derived=derived)

        decision: dict[str, Any]
        if config.llm_available():
            llm_out = llm_decide(books, context=context)
            if llm_out and llm_out.get("raw_ok") and not llm_out.get("fallback"):
                decision = llm_out
            else:
                decision = policy_decide(books)
                decision["llm_error"] = (llm_out or {}).get("error")
                decision["note"] = "fell back to policy after LLM failure"
        else:
            decision = policy_decide(books)

        sleeve = bitget_private.positions()
        action_u = str(decision.get("action") or "HOLD").upper()
        if action_u in ("FLAT", "REDUCE"):
            close_legs = _management_close_legs(
                sleeve.get("positions") or [],
                marks,
                decision.get("legs"),
                action_u,
            )
            decision = dict(decision)
            decision["action"] = action_u
            decision["management_close"] = True
            decision["legs"] = close_legs
            if not close_legs:
                decision["note"] = (
                    (str(decision.get("note") or "") + " | no open leg to close").strip(" |")
                )

        # Wallet-aware sizing BEFORE Risk Cage — never send unsized Mag7 intents live.
        # FLAT/REDUCE skips the new-risk sizer so a close cannot flip into a fresh leg.
        eq_snap = bitget_private.account_equity()
        available = float(eq_snap.get("available") or 0.0) if eq_snap.get("ok") else 0.0
        if decision.get("management_close"):
            close_legs = list(decision.get("legs") or [])
            spent = round(sum(float(leg.get("notional_usdt") or 0) for leg in close_legs), 4)
            sized = {
                "ok": bool(close_legs),
                "executable": bool(close_legs),
                "legs": close_legs,
                "caps": sizing.effective_caps(available),
                "spent_notional_usdt": spent,
                "message": "reduce-only close of open legs" if close_legs else "no open leg to close",
            }
        else:
            marks_for_size = bitget_public.marks_map(books)
            sized = sizing.normalize_executable_legs(decision, marks_for_size, available)
        if sized.get("executable"):
            decision = dict(decision)
            decision["legs"] = sized["legs"]
            decision["sizing"] = {
                "caps": sized.get("caps"),
                "spent_notional_usdt": sized.get("spent_notional_usdt"),
            }
        cage = risk_cage_check(decision, sleeve, sized=sized)

        # Execution: paper virtual fills OR live Bitget — never mix
        execution = {
            "attempted": False,
            "filled": False,
            "fills": [],
            "message": "no execution — HOLD, blocked by Risk Cage, or legs not sized for wallet",
            "sizing": sized,
        }
        if cage.get("exec_allowed") and decision.get("action") not in ("HOLD",):
            # Hard gate: drop legs that would ADD same-side to an open symbol
            open_map = {}
            for p0 in (sleeve.get("positions") or []):
                if not isinstance(p0, dict):
                    continue
                sym0 = str(p0.get("symbol") or p0.get("symbolName") or "").upper()
                if sym0 and not sym0.endswith("USDT"):
                    sym0 += "USDT"
                side0 = str(p0.get("side") or p0.get("holdSide") or "").lower()
                if sym0:
                    open_map[sym0] = side0
            legs_in = list(sized.get("legs") or [])
            kept = []
            rejected = []
            for leg in legs_in:
                if not isinstance(leg, dict):
                    continue
                sym = str(leg.get("symbol") or "").upper()
                if sym and not sym.endswith("USDT"):
                    sym += "USDT"
                raw = str(leg.get("side") or "").lower()
                want = "long" if raw in ("long", "open_long", "buy") else "short" if raw in ("short", "open_short", "sell") else ""
                reduce_only = bool(leg.get("reduceOnly") or leg.get("reduce_only"))
                have = open_map.get(sym)
                if have and want and have == want and not reduce_only:
                    rejected.append({"symbol": sym, "side": want, "reason": "duplicate_same_side"})
                    continue
                kept.append(leg)
            if rejected:
                decision = dict(decision)
                decision["rejected_legs"] = rejected
                sized = dict(sized)
                sized["legs"] = kept
                if not kept and str(decision.get("action") or "").upper() in ("HEDGE", "ROTATE"):
                    decision["action"] = "HOLD"
                    decision["note"] = (str(decision.get("note") or "") + " | blocked duplicate same-side opens").strip(" |")
                    cage = dict(cage)
                    cage["exec_allowed"] = False
                    cage["detail"] = "all legs were duplicate same-side opens"
            order = bitget_private.place_order(
                {"action": decision.get("action"), "legs": sized.get("legs") or [], "tick_id": tick_id}
            )
            execution = {
                "attempted": True,
                "filled": False,
                "fills": [],
                "order_result": order,
                "sizing": sized,
                "paper": config.paper_mode(),
                "message": order.get("error") or order.get("message") or "order hook returned",
            }
            if order.get("fills"):
                for fl in order["fills"]:
                    fl = dict(fl)
                    fl["tick_id"] = tick_id
                    fl.setdefault("ts", time.time())
                    if not config.paper_mode():
                        store.append_jsonl("fills.jsonl", fl)
                    execution["filled"] = True
                    execution["fills"].append(fl)

        for _k in ("mag7_24h_pct", "btc_24h_pct", "divergence_pct", "threshold_pct"):
            if decision.get(_k) is None and derived.get(_k) is not None:
                decision[_k] = derived[_k]

        elapsed_ms = int((time.time() - t0) * 1000)
        record = {
            "tick_id": tick_id,
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "session": sess,
            "engine": decision.get("engine"),
            "model": decision.get("model"),
            "action": decision.get("action"),
            "thesis": decision.get("thesis"),
            "legs": decision.get("legs") or [],
            "confidence": decision.get("confidence"),
            "mag7_24h_pct": decision.get("mag7_24h_pct"),
            "btc_24h_pct": decision.get("btc_24h_pct"),
            "divergence_pct": decision.get("divergence_pct"),
            "threshold_pct": decision.get("threshold_pct"),
            "risk_cage": cage,
            "execution": execution,
            "sleeve_connected": config.sleeve_connected(),
            "connect_mode": config.connect_mode_label(),
            "paper": config.paper_mode(),
            "mode_pill": config.mode_pill(),
            "agent_mode": config.get_agent_mode(),
            "books_ok": bool(books.get("ok")),
            "marks_sample": {
                "AAPLUSDT": marks.get("AAPLUSDT"),
                "BTCUSDT": marks.get("BTCUSDT"),
                "ETHUSDT": marks.get("ETHUSDT"),
            },
            "context": context,
            "elapsed_ms": elapsed_ms,
            "status": decision.get("action") or "HOLD",
        }

        store.append_jsonl("ticks.jsonl", record)
        store.append_jsonl(
            "decisions.jsonl",
            {
                "tick_id": tick_id,
                "ts": record["ts"],
                "engine": record["engine"],
                "model": record["model"],
                "action": record["action"],
                "thesis": record["thesis"],
                "divergence_pct": record["divergence_pct"],
                "context": context,
                "risk_cage": cage,
                "trigger": trigger,
            },
        )

        # Equity snapshot: paper book OR live sleeve — never mix files
        eq = bitget_private.account_equity()
        if config.paper_mode():
            pass  # paper_sleeve already appends paper_equity.jsonl on fill/MTM
        elif eq.get("connected") and eq.get("equity") is not None:
            store.append_jsonl(
                "equity.jsonl",
                {"ts": time.time(), "tick_id": tick_id, "equity": eq["equity"]},
            )

        with _state_lock:
            _state["status"] = record["action"]
            _state["last_decision"] = record
            _state["last_context"] = context
            _state["last_tick_id"] = tick_id
            _state["last_heartbeat_ts"] = record["ts"]
            _state["next_heartbeat_ts"] = record["ts"] + config.HEARTBEAT_SEC
            _state["connect_mode"] = config.connect_mode_label()
            _state["engine"] = record["engine"]
            _state["last_marks"] = marks
            _state["running_tick"] = False

        return {"ok": True, "tick": record}
    except Exception as e:
        with _state_lock:
            _state["status"] = "ERROR"
            _state["running_tick"] = False
        err = {"ok": False, "tick_id": tick_id, "error": str(e), "ts": time.time(), "trigger": trigger}
        store.append_jsonl("ticks.jsonl", err)
        return err


def get_state() -> dict[str, Any]:
    with _state_lock:
        s = dict(_state)
    s["connect_mode"] = config.connect_mode_label()
    s["mode_pill"] = config.mode_pill()
    s["sleeve_connected"] = config.sleeve_connected()
    s["llm_available"] = config.llm_available()
    try:
        from . import bitget_private
        hooks = bitget_private.hooks_enabled()
        s["private_hooks_enabled"] = hooks
        s["kill_armed"] = bool(s["sleeve_connected"] and hooks)
        s["capabilities"] = bitget_private.capability_matrix()
    except Exception:
        s["private_hooks_enabled"] = False
        s["kill_armed"] = False
        s["capabilities"] = None
    s["heartbeat_sec"] = config.HEARTBEAT_SEC
    s["event_move_pct"] = config.EVENT_MOVE_PCT
    s["agent_mode"] = config.get_agent_mode()
    s["agent_profile"] = config.active_profile()
    s["risk"] = config.active_risk()
    s["policy"] = config.active_policy()
    # Rule-2 pack: prefer explicit last_context; fall back to last_decision.context
    if s.get("last_context") is None and isinstance(s.get("last_decision"), dict):
        s["last_context"] = s["last_decision"].get("context")
    if s.get("next_heartbeat_ts") is None:
        # schedule relative to now if never ticked
        now = time.time()
        s["next_heartbeat_ts"] = now + config.HEARTBEAT_SEC
        if s.get("last_heartbeat_ts") is None:
            # keep next as now+heartbeat for countdown honesty
            pass
    return s


def maybe_event_wake(marks: dict[str, float]) -> dict[str, Any] | None:
    """Wake if any watched mark moved > threshold since last tick marks."""
    with _state_lock:
        prev = dict(_state.get("last_marks") or {})
        if _state.get("running_tick"):
            return None
    if not prev or not marks:
        with _state_lock:
            _state["last_marks"] = marks
        return None
    thr = config.EVENT_MOVE_PCT
    movers = []
    for sym, px in marks.items():
        p0 = prev.get(sym)
        if p0 and p0 > 0 and px is not None:
            ch = abs((px - p0) / p0) * 100.0
            if ch >= thr:
                movers.append({"symbol": sym, "from": p0, "to": px, "move_pct": round(ch, 4)})
    if not movers:
        return None
    return run_tick(trigger=f"event_wake:{movers[0]['symbol']}:{movers[0]['move_pct']}%")


def schedule_next(from_ts: float | None = None) -> float:
    base = from_ts if from_ts is not None else time.time()
    nxt = base + config.HEARTBEAT_SEC
    with _state_lock:
        _state["next_heartbeat_ts"] = nxt
    return nxt
