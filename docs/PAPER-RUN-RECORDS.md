# Crossfire — paper trading run records (Agentic Trading)

**Mode:** paper sleeve · live Bitget marks (`CROSSFIRE_PAPER=1`)  
**Period:** 2026-09-17 → 2026-09-25 (aging continues; fills through 24 Sep)  
**Fills exported:** **146** (synced from live paper journal; append-only, not rewritten)  
**Decisions exported:** **2462** (synced from live decision journal; append-only, not rewritten)  
**Full fills log:** [paper-trading-fills.jsonl](./paper-trading-fills.jsonl)  
**Full decisions log:** [paper-trading-decisions.jsonl](./paper-trading-decisions.jsonl)

Each fill line includes: `timestamp`, `instrument`, `direction`, `price`, `quantity`, `account_balance_change` (plus equity before/after).

Each decision line includes: `tick_id`, `timestamp`, `engine`/`model`, `action`, `thesis`, Risk Cage checks, and Rule-2 context snapshot when present.

These are **observed paper** fills and decisions — not live Bitget orders.

For the live→paper switch and early MSFT re-entry context, see [PAPER-TESTING-NOTES.md](./PAPER-TESTING-NOTES.md).
