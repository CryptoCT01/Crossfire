"""Bitget S2 toolkit feeds for Rule-2 perception.

Cursor-side MCPs (connected in account):
  - bitget-mcp-server  https://agent.bitget.com/mcp   (US equity / ETF catalog)
  - bitget-signal      https://datahub.noxiaohao.com/mcp
  - bitget-agentic     npx -y @bitget-ai/bitget-agent-mcp  (OAuth trading; authorize separately)

Crossfire cannot call Cursor MCP from the Python process, so this module pulls the
same public sources those skills wrap (Fear&Greed, CoinDesk RSS, Yahoo NDX/DXY)
and tags them honestly. Never invents headlines.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

_cache: dict[str, Any] = {"ts": 0.0, "pack": None}
_TTL = 120.0  # seconds


def _ssl():
    try:
        import certifi
        import ssl

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        import ssl

        return ssl.create_default_context()


def _get(url: str, timeout: float = 12.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Crossfire/0.3 (+Bitget AI Hackathon S2; toolkit feeds)",
            "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl()) as resp:
        return resp.read()


def _fear_greed() -> dict[str, Any] | None:
    try:
        raw = json.loads(_get("https://api.alternative.me/fng/?limit=1").decode("utf-8"))
        row = (raw.get("data") or [None])[0]
        if not row:
            return None
        return {
            "value": int(row.get("value")),
            "label": row.get("value_classification"),
            "source": "alternative.me/fng (bitget-signal sentiment_index mirror)",
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "source": "alternative.me/fng"}


def _coindesk_news(limit: int = 5) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        xml = _get("https://www.coindesk.com/arc/outboundfeeds/rss/").decode("utf-8", "replace")
        root = ET.fromstring(xml)
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if title:
                out.append(
                    {
                        "title": title,
                        "url": link or None,
                        "published": pub or None,
                        "source": "coindesk_rss (bitget-signal news_feed mirror)",
                    }
                )
    except Exception as e:
        out.append({"error": f"{type(e).__name__}: {e}", "source": "coindesk_rss"})
    return out


def _yahoo_last(symbol: str) -> dict[str, Any] | None:
    """Last + % change via Yahoo chart API (public)."""
    try:
        from urllib.parse import quote

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=5d&interval=1d"
        raw = json.loads(_get(url).decode("utf-8"))
        res = (raw.get("chart") or {}).get("result") or []
        if not res:
            return None
        meta = res[0].get("meta") or {}
        px = meta.get("regularMarketPrice")
        chg = meta.get("regularMarketChangePercent")
        if px is None:
            return None
        return {
            "symbol": symbol,
            "last": float(px),
            "change_pct": float(chg) if chg is not None else None,
            "source": "yahoo_finance (cross_asset / equity mirror)",
        }
    except Exception as e:
        return {"symbol": symbol, "error": f"{type(e).__name__}: {e}"}


def fetch_toolkit_pack(force: bool = False) -> dict[str, Any]:
    """Cached perception enrichment for Rule 2."""
    now = time.time()
    if not force and _cache["pack"] and now - float(_cache["ts"]) < _TTL:
        return _cache["pack"]

    sources: list[str] = []
    fng = _fear_greed()
    if fng and "value" in fng:
        sources.append("fear_greed")
    news = _coindesk_news(5)
    if any("title" in n for n in news):
        sources.append("coindesk_rss")

    cross = {
        "ndx": _yahoo_last("^NDX"),
        "spy": _yahoo_last("SPY"),
        "dxy": _yahoo_last("DX-Y.NYB"),
        "btc_usd": _yahoo_last("BTC-USD"),
    }
    if any(isinstance(v, dict) and v.get("last") is not None for v in cross.values()):
        sources.append("yahoo_cross_asset")

    # Mag7 cash equity last prints (complements Bitget USDT-M marks)
    mag7_cash = {}
    for sym in ("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"):
        q = _yahoo_last(sym)
        if q and q.get("last") is not None:
            mag7_cash[sym] = q
    if mag7_cash:
        sources.append("yahoo_mag7_cash")

    pack = {
        "ok": bool(sources),
        "fetched_at": now,
        "fear_greed": fng,
        "news": [n for n in news if n.get("title")],
        "cross_asset": cross,
        "mag7_cash": mag7_cash,
        "sources": sources or ["toolkit_feeds:unavailable"],
        "cursor_mcp": {
            "bitget_mcp_server": "https://agent.bitget.com/mcp",
            "bitget_signal": "https://datahub.noxiaohao.com/mcp",
            "bitget_agentic": "npx -y @bitget-ai/bitget-agent-mcp (OAuth required for live)",
            "note": "Cursor MCPs are connected for the builder agent; Crossfire runtime uses public mirrors tagged above.",
        },
    }
    _cache["ts"] = now
    _cache["pack"] = pack
    return pack
