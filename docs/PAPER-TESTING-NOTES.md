# Crossfire — paper testing notes (Base Camp S2)

**Date locked:** 2026-09-18  
**Mode:** PAPER SLEEVE · LIVE MARKS (virtual book, real Bitget marks — not live Bitget orders)

## What happened during testing
While aging the competition paper log, the Agent repeatedly re-emitted **same-side HEDGE legs** (notably **MSFTUSDT short**). The paper sleeve originally **VWAP-added** those legs, which inflated notional and made the Dual Book / Risk Cage panels look unhealthy for a $10k paper sleeve.

This was a **test-regime control gap**, not live-funded trading.

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
| Pre-fix fills (many `note=add`) | Test regime — stacking under observation |
| `t_msft_risk_flat_*` | Managed FLAT — risk sanitation for Demo |
| Post-fix | Production paper controls |

Ambassador standard: real marks, real Agent loop, honest paper sleeve, no fabricated dashboard state.
