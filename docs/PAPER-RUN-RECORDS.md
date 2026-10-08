# Crossfire — paper trading run records (Agentic Trading)

**Mode:** paper sleeve · live Bitget marks (`CROSSFIRE_PAPER=1`)  
**Period:** 2026-09-13 → 2026-10-08 (decisions from 13 Sep; fills 17 Sep → 8 Oct)  
**Fills exported:** **209** (synced from live paper journal; append-only, not rewritten)  
**Decisions exported:** **5971** (synced from live decision journal; append-only, not rewritten)  
**Full fills log:** [paper-trading-fills.jsonl](./paper-trading-fills.jsonl)  
**Full decisions log:** [paper-trading-decisions.jsonl](./paper-trading-decisions.jsonl)

Each fill line includes: `timestamp`, `instrument`, `direction`, `price`, `quantity`, `account_balance_change` (plus equity before/after).

Each decision line includes: `tick_id`, `timestamp`, `engine`/`model`, `action`, `thesis`, Risk Cage checks, and Rule-2 context snapshot when present.

These are **observed paper** fills and decisions — not live Bitget orders.

For the live→paper switch and early MSFT re-entry context, see [PAPER-TESTING-NOTES.md](./PAPER-TESTING-NOTES.md).
