"""Agent tick pipeline: Dual Book → policy/LLM → Risk Cage → JSONL (no fake fills)."""
from __future__ import annotations

import json
import threading
import time
import uuid
import urllib.error
import urllib.request
from typing import Any

from . import bitget_private, bitget_public, config
from . import logging_store as store
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


def risk_cage_check(decision: dict[str, Any], sleeve: dict[str, Any]) -> dict[str, Any]:
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

    # Daily DD: without real equity, cannot trip — report UNAVAILABLE honestly
    checks.append(
        {
            "rule": "daily_dd_halt_pct",
            "limit": risk["daily_dd_halt_pct"],
            "current": None,
            "pass": True,
            "detail": "equity UNAVAILABLE until sleeve connected",
        }
    )

    for c in checks:
        if c.get("pass") is False and c.get("rule") != "sleeve_connected":
            ok = False
            reason = f"blocked by {c['rule']}"

    # Sleeve disconnect blocks execution but does not invalidate the thesis record
    exec_allowed = ok and config.sleeve_connected() and decision.get("action") not in ("HOLD",)
    if decision.get("action") in ("HOLD",):
        exec_allowed = False
        reason = "HOLD — no execution"

    return {
        "ok": ok,
        "exec_allowed": exec_allowed,
        "reason": reason,
        "risk": risk,
        "agent_mode": config.get_agent_mode(),
        "checks": checks,
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

    return {
        "engine": "policy",
        "model": None,
        "action": action,
        "thesis": thesis,
        "mag7_24h_pct": mag7,
        "btc_24h_pct": btc,
        "divergence_pct": divergence,
        "threshold_pct": thr,
        "agent_mode": mode,
        "legs": legs,
        "confidence": 0.55 if action in ("HEDGE", "ROTATE") else 0.7,
        "profile_label": prof.get("label"),
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
    """Compact tick JSON for the LLM — never secrets."""
    pol = config.active_policy()
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

    positions_summary: list[dict[str, Any]] = []
    if config.sleeve_connected():
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
                        "unrealized_pnl": p.get("unrealizedPL") or p.get("unrealized_pnl"),
                    }
                )
        except Exception:
            positions_summary = []

    payload: dict[str, Any] = {
        "us": [
            {"s": r["symbol"], "m": r.get("mark"), "c24": r.get("change24h_pct")}
            for r in us
            if r.get("available")
        ],
        "crypto": [
            {"s": r["symbol"], "m": r.get("mark"), "c24": r.get("change24h_pct")}
            for r in crypto
            if r.get("available")
        ],
        "session": session_info.us_cash_session(),
        "risk": config.active_risk(),
        "agent_mode": config.get_agent_mode(),
        "sleeve_connected": config.sleeve_connected(),
        "positions": positions_summary,
        "derived": {
            "mag7_24h_pct": mag7,
            "btc_24h_pct": btc,
            "divergence_pct": divergence,
            "threshold_pct": float(pol["divergence_threshold_pct"]),
        },
        "reply_schema": (
            "Reply JSON only: "
            '{"action":"HOLD|HEDGE|ROTATE|REDUCE|FLAT","thesis":"...","legs":[],'
            '"confidence":0-1,"rules_fired":[]}'
        ),
    }
    if context:
        # Drop any accidental secret-looking keys
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
        cage = risk_cage_check(decision, sleeve)

        # Execution: only if cage allows AND sleeve connected — currently stub, no fake fills
        execution = {
            "attempted": False,
            "filled": False,
            "fills": [],
            "message": "no execution — HOLD or sleeve disconnected / stub",
        }
        if cage.get("exec_allowed") and decision.get("action") not in ("HOLD",):
            order = bitget_private.place_order(
                {"action": decision.get("action"), "legs": decision.get("legs"), "tick_id": tick_id}
            )
            execution = {
                "attempted": True,
                "filled": False,
                "fills": [],
                "order_result": order,
                "message": order.get("error") or order.get("message") or "order hook returned",
            }
            # Only write fills.jsonl when real fills exist
            if order.get("fills"):
                for fl in order["fills"]:
                    fl = dict(fl)
                    fl["tick_id"] = tick_id
                    fl["ts"] = time.time()
                    store.append_jsonl("fills.jsonl", fl)
                    execution["filled"] = True
                    execution["fills"].append(fl)

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

        # Equity snapshot only when sleeve connected and we have a real number
        eq = bitget_private.account_equity()
        if eq.get("connected") and eq.get("equity") is not None:
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
