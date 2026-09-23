#!/usr/bin/env python3
"""
Offline DeepSeek vs Jev shadow bench — ZERO effect on Crossfire sleeve / dashboard.

Reads one frozen row from logs/decisions.jsonl, calls OpenRouter Decisions API
(typesafe/jev-1.13), prints a side-by-side vs the logged DeepSeek action, and
optionally appends to logs/shadow_jev.jsonl.

Usage:
  python3 scripts/shadow_jev_bench.py
  python3 scripts/shadow_jev_bench.py --tick t_...
  python3 scripts/shadow_jev_bench.py --smoke   # tiny canned state only (proves Jev access)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECISIONS = ROOT / "logs" / "decisions.jsonl"
SHADOW = ROOT / "logs" / "shadow_jev.jsonl"
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def read_decision(tick: str | None) -> dict:
    if not DECISIONS.is_file():
        raise SystemExit(f"missing {DECISIONS}")
    rows = [json.loads(l) for l in DECISIONS.read_text().splitlines() if l.strip()]
    if not rows:
        raise SystemExit("decisions.jsonl empty")
    if not tick:
        return rows[-1]
    for r in reversed(rows):
        if r.get("tick_id") == tick or r.get("id") == tick:
            return r
    raise SystemExit(f"tick not found: {tick}")


def compact_state(d: dict) -> dict:
    """Frozen state for Jev — no live API, no sleeve mutation."""
    ctx = d.get("context") if isinstance(d.get("context"), dict) else {}
    cage = d.get("risk_cage") if isinstance(d.get("risk_cage"), dict) else {}
    thesis = (d.get("thesis") or "")[:1200]
    return {
        "tick_id": d.get("tick_id"),
        "logged_action": d.get("action"),
        "logged_model": d.get("model"),
        "logged_engine": d.get("engine"),
        "trigger": d.get("trigger"),
        "divergence_pct": d.get("divergence_pct"),
        "thesis": thesis,
        "risk_cage": {
            "ok": cage.get("ok"),
            "exec_allowed": cage.get("exec_allowed"),
            "reason": cage.get("reason"),
            "agent_mode": cage.get("agent_mode"),
            "checks": cage.get("checks"),
        },
        "context_brief": {
            k: ctx.get(k)
            for k in (
                "macro",
                "sentiment",
                "us_tape",
                "breadth",
                "fear_greed",
                "cross_asset",
                "mag7_cash",
                "cmc_brief",
                "derived_snapshot",
            )
            if k in ctx
        },
    }


def trading_questions() -> dict:
    return {
        "action": {
            "type": "choice",
            "instructions": (
                "Given Crossfire Dual Book state + Risk Cage, which action should the paper agent take now? "
                "Prefer HOLD unless there is a clear, cage-safe edge."
            ),
            "criteria": {
                "HOLD": "Keep current book; no new risk",
                "REDUCE": "Cut size / risk on open leg(s)",
                "FLAT": "Close open leg(s) to flat",
                "HEDGE": "Add an opposing / cross-asset hedge leg",
                "ROTATE": "Swap or rebalance into a different name",
            },
        },
        "same_side_reentry_risk": {
            "type": "noul",
            "instructions": (
                "Would opening or adding the SAME side on a symbol that already has an open leg be unsafe "
                "(re-entry / churn risk) given this state?"
            ),
            "criteria": {
                "true": "Same-side add/re-entry looks unsafe or redundant",
                "false": "No meaningful same-side re-entry concern",
            },
        },
        "cage_stress": {
            "type": "noul",
            "instructions": "Is Risk Cage stress elevated enough that new risk should be blocked?",
            "criteria": {
                "true": "Cage / drawdown / slots / confidence stress elevated",
                "false": "Cage looks comfortable for standing pat or modest action",
            },
        },
        "change_risk_confidence": {
            "type": "score",
            "instructions": "How confident should we be to CHANGE risk (not HOLD) on this tick?",
            "criteria": [
                "No change — stay HOLD",
                "Weak lean to change",
                "Moderate case to change",
                "Strong case to change",
                "Urgent — change risk now",
            ],
        },
    }


def call_jev(api_key: str, state, questions: dict) -> dict:
    body = {
        "model": JEV_MODEL,
        "state": state,
        "questions": questions,
        "session_id": "crossfire-shadow-jev-bench",
        "trace": {"trace_name": "crossfire-shadow-jev", "span_name": "bench"},
    }
    req = urllib.request.Request(
        JEV_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/CryptoCT01/Crossfire",
            "X-Title": "Crossfire shadow Jev bench",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise SystemExit(f"Jev HTTP {e.code}: {err[:800]}") from e


def fmt_answers(answers: dict) -> str:
    lines = []
    for name, ans in (answers or {}).items():
        if not isinstance(ans, dict):
            lines.append(f"  {name}: {ans}")
            continue
        t = ans.get("type")
        if t == "noul" or "noul" in ans:
            lines.append(f"  {name}: noul={ans.get('noul')}")
        elif t == "choice" or "choice" in ans:
            lines.append(
                f"  {name}: choice={ans.get('choice')}  probs={ans.get('probabilities')}  conf={ans.get('confidence')}"
            )
        elif t == "score" or "score" in ans:
            lines.append(
                f"  {name}: score={ans.get('score')}  probs={ans.get('probabilities')}  conf={ans.get('confidence')}"
            )
        else:
            lines.append(f"  {name}: {json.dumps(ans)[:200]}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tick", help="decision tick_id (default: latest)")
    ap.add_argument("--smoke", action="store_true", help="canned urgency smoke test only")
    ap.add_argument("--no-log", action="store_true", help="do not append shadow_jev.jsonl")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY missing in env / .env")

    print("=== Crossfire shadow Jev bench (read-only) ===")
    print(f"model: {JEV_MODEL}")
    print(f"endpoint: {JEV_URL}")
    print("sleeve/dashboard: NOT touched\n")

    if args.smoke:
        state = "Help! My payouts have been failing for 3 days."
        questions = {
            "is_urgent": {
                "type": "noul",
                "instructions": "Does this message convey urgency?",
                "criteria": {
                    "true": "Explicitly time-sensitive",
                    "false": "No urgency expressed",
                },
            },
            "department": {
                "type": "choice",
                "instructions": "Which team should handle this?",
                "criteria": {
                    "billing": "Payments, invoicing, refunds",
                    "technical": "Bugs, outages, integrations",
                    "sales": "Pricing, upgrades, new accounts",
                },
            },
            "frustration": {
                "type": "score",
                "instructions": "How frustrated is the customer?",
                "criteria": ["Calm", "Frustrated", "Very angry"],
            },
        }
        deepseek_side = {"note": "smoke test — DeepSeek not called"}
        tick_id = "smoke"
    else:
        d = read_decision(args.tick)
        state = compact_state(d)
        questions = trading_questions()
        tick_id = d.get("tick_id")
        deepseek_side = {
            "source": "logs/decisions.jsonl (frozen)",
            "tick_id": tick_id,
            "action": d.get("action"),
            "model": d.get("model"),
            "engine": d.get("engine"),
            "trigger": d.get("trigger"),
            "thesis_preview": (d.get("thesis") or "")[:280],
        }

    t0 = time.time()
    raw = call_jev(key, state, questions)
    dt = time.time() - t0
    answers = raw.get("answers") or raw.get("decision", {}).get("answers") or {}

    print("--- DeepSeek (logged / frozen) ---")
    print(json.dumps(deepseek_side, indent=2)[:900])
    print("\n--- Jev (fresh Decisions call) ---")
    print(f"latency_s: {dt:.2f}")
    print(f"response_model: {raw.get('model')}")
    print(f"provider: {raw.get('provider')}")
    if raw.get("usage"):
        print(f"usage: {raw.get('usage')}")
    print("answers:")
    print(fmt_answers(answers) if answers else json.dumps(raw, indent=2)[:1200])

    row = {
        "ts": time.time(),
        "tick_id": tick_id,
        "mode": "smoke" if args.smoke else "shadow",
        "jev_model": JEV_MODEL,
        "deepseek": deepseek_side,
        "jev_answers": answers,
        "jev_raw_meta": {
            "id": raw.get("id"),
            "model": raw.get("model"),
            "provider": raw.get("provider"),
            "usage": raw.get("usage"),
            "latency_s": round(dt, 3),
        },
    }
    if not args.no_log:
        SHADOW.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        print(f"\nappended {SHADOW} (shadow only — not a fill)")
    print("\nDone. Paper sleeve untouched.")


if __name__ == "__main__":
    main()
