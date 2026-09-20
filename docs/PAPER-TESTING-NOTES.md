# Crossfire — paper testing notes (Base Camp S2)

**Last updated:** 2026-09-20  
**Mode:** PAPER SLEEVE · LIVE MARKS (virtual book, real Bitget marks — not live Bitget orders)

## Live sleeve → paper sleeve (honest timeline)

We **started on a small live / connected sleeve** to prove the real connect path, marks, and Risk Cage wiring end-to-end. For the longer demo window we **moved to a paper sleeve** (live Bitget marks, virtual fills) so we could age the competition log without putting more capital at risk.

**We did not rewrite or scrub historical logs.** Every decision and fill from both phases remains in the append-only JSONL for judges to inspect.

## What happened after the paper switch

Once on paper, an early **same-asset re-entry / same-side ADD bug** let the Agent open and close (and VWAP-add) **multiple legs in the same name** — most visibly **MSFTUSDT short**. That churn **dragged paper win rate** and inflated notional on the Dual Book.

Important for judges:

- The **Risk Cage still held** — this did **not** wipe the paper book or “kill” the account.
- The noisy MSFT phase is **real test history**, not hidden.
- We later shipped production controls (below) and a **managed FLAT** for Demo optics; pre-fix rows stay in the log.

## What we changed (production paper rules)

1. **No same-side ADD / average** — paper sleeve rejects duplicate same-side opens.
2. **Engine hard-gate** — duplicate same-side legs dropped before execution.
3. **Tick rules 6 / 7 / 10** — one position per symbol; open symbols may only HOLD / REDUCE / FLAT.
4. **Prompt mandate** — removed “MUST trade every tick”; default HOLD when thesis already expressed.
5. **Positions always injected** into the LLM payload in paper mode.

## Managed risk action (Demo optics + Risk Cage)

On 2026-09-18 we issued a **managed FLAT** on the oversized MSFT paper short (`t_msft_risk_flat_*`) so the dashboard reads exchange-ready for Demo review.

- **Fill/decision history was not wiped** — pre-fix adds remain in `logs/fills.jsonl` / `logs/decisions.jsonl` as the test phase.
- Post-fix ticks run under the hardened rules.

## How to read the paper log for judges

| Phase | Meaning |
|---|---|
| Early live / connect path | Real sleeve wiring proven; capital preservation prioritized |
| Pre-fix paper fills (many `note=add`, MSFT churn) | Test regime — same-side re-entry gap under observation; win rate impacted |
| `t_msft_risk_flat_*` | Managed FLAT — risk sanitation for Demo |
| Post-fix | Production paper controls (no same-side repeat opens) |

Ambassador standard: real marks, real Agent loop, honest paper sleeve, no fabricated dashboard state, **no log rewriting**.
