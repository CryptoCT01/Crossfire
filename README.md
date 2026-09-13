# Crossfire

**Bitget AI Base Camp Hackathon S2** · Agentic Trading · **Cross-Asset Execution Agent**

> When US cash equities sleep, Bitget US stock contracts and crypto still move. Crossfire is an LLM agent that senses a **leveraged Dual Book** (US stock contracts ↔ USDT-M perps), decides the hedge or rotation, and executes under a hard **Risk Cage**.

## Demo (mock — no live orders)

Open the command deck:

```bash
cd path/to/crossfire
python3 -m http.server 8765
# then visit http://127.0.0.1:8765/dashboard.html
# skip splash: http://127.0.0.1:8765/dashboard.html#floor
```

Or open `dashboard.html` directly in a browser (needs network once for fonts + lightweight-charts).

**Mock mode only in this build:** no private API keys, no order placement. Trade / kill controls toast *Mock only — live sleeve not connected*.

## What’s on the deck

| Panel | Role |
|-------|------|
| **Dual Book** | Leveraged US stock contracts (amber, 5–20× mock) · USDT-M perps (cyan, 10–50× mock) |
| **Bridge Core** | Active cross-hedge thesis, correlation, legs |
| **Decision Cinema** | event → LLM thesis → risk → order → fill |
| **Risk Cage** | sleeve equity, slots, daily DD halt, kill switch |

## Thesis (short)

Macro / weekend / after-hours shocks hit US stock contracts and crypto on different clocks. A single-asset bot leaves the other book stranded. Crossfire’s LLM owns the **cross-asset decision**; the Risk Cage owns blast radius (max slots, margin, daily DD, kill switch) on an **isolated sleeve**.

## Security

- No API keys or secrets in this repository
- Use `.env.example` as a template only; keep real credentials out of git and out of chat
- Prefer Bitget **Agentic** / Demo credentials for any future live wiring
- High-risk actions must require explicit confirmation

## Hackathon

- Track: Agentic Trading
- Sub-theme: Cross-Asset Execution Agent
- Builder: [@CryptoCTO1](https://github.com/CryptoCT01)
- Handbook: https://bitget-ai.gitbook.io/bitgetai_hackathons2

## License

MIT — see below.

```
MIT License

Copyright (c) 2026 CryptoCT01

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
```
