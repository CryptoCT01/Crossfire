# Crossfire — paper trading run records (Agentic Trading)

**Mode:** paper sleeve · live Bitget marks (`CROSSFIRE_PAPER=1`)  
**Period:** 2026-09-17 → 2026-09-20 (aging continues)  
**Fills exported:** **120** (synced from live paper journal; append-only, not rewritten)  
**Full fills log:** [paper-trading-fills.jsonl](./paper-trading-fills.jsonl)

Each fill line includes: `timestamp`, `instrument`, `direction`, `price`, `quantity`, `account_balance_change` (plus equity before/after).

These are **observed paper** fills — not live Bitget orders.

For the live→paper switch and early MSFT re-entry context, see [PAPER-TESTING-NOTES.md](./PAPER-TESTING-NOTES.md).
