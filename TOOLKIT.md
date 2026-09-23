# Bitget AI Hackathon S2 — Developer Toolkit (Crossfire)

Official guide: https://bitget-ai.gitbook.io/bitgetai_hackathons2#v.-developer-toolkit

## Cursor MCPs (builder account — connected)

| MCP | Transport | Role |
|-----|-----------|------|
| `bitget-mcp-server` | `https://agent.bitget.com/mcp` | US equity / ETF / news / sentiment catalog (`guide`, `do_query`) |
| `bitget-signal` | `https://datahub.noxiaohao.com/mcp` | Market signal tools (news sources, sentiment, macro, cross-asset) |
| `bitget-agentic` | `npx -y @bitget-ai/bitget-agent-mcp` | Agent Hub trading MCP — **OAuth required** (`authorized: false` until Allow) |

Validated: `do_query` → `equity_price_quote` for **NVDA** returns live quotes.

## Crossfire runtime (Python)

Cursor MCP sessions are not callable from the Crossfire process. Rule-2 uses
`crossfire/bitget_feeds.py` public mirrors of the same toolkit:

- Fear & Greed → `api.alternative.me/fng` (signal `sentiment_index` mirror)
- News → CoinDesk RSS (signal `news_feed` mirror)
- Cross-asset / Mag7 cash → Yahoo Finance chart API

Wired into `perception.build_context()` as `news`, `fear_greed`, `cross_asset`, `mag7_cash`.

## Optional next: Agent Hub OAuth

For paper/live via Bitget Agent Hub (not required for Dual Book sleeve):

1. New Cursor chat so MCP tools load
2. Call `authorize_start` on `bitget-agentic`
3. Browser Allow once
4. Confirm `get_auth_status` → `authorized: true`

## Submission checklist (track)

- Demo / walkthrough video
- Paper logs ≥ ~2 weeks preferred
- X post with `#BitgetHackathon` + `@Bitget_AI`
- Form: https://forms.gle/GyWZCMCPocgJdJon6
