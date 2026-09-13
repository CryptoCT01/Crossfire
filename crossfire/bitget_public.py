"""Bitget public USDT-M market data — no API key required."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from . import config

_cache: dict[str, Any] = {"ts": 0.0, "books": None, "error": None}


def _ssl_context():
    try:
        import certifi
        import ssl

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        import ssl

        return ssl.create_default_context()


def _get_urllib(url: str, timeout: float = 12.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Crossfire/0.2 (+Bitget AI Hackathon S2)",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _get_curl(url: str, timeout: float = 12.0) -> dict[str, Any]:
    import subprocess

    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            str(int(timeout)),
            "-H",
            "Accept: application/json",
            "-H",
            "User-Agent: Crossfire/0.2 (+Bitget AI Hackathon S2)",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"curl failed ({proc.returncode})")
    return json.loads(proc.stdout)


def _get(url: str, timeout: float = 12.0) -> dict[str, Any]:
    try:
        return _get_urllib(url, timeout=timeout)
    except Exception:
        return _get_curl(url, timeout=timeout)


def fetch_all_tickers() -> list[dict[str, Any]]:
    url = f"{config.BITGET_BASE}/api/v2/mix/market/tickers?productType={config.PRODUCT_TYPE}"
    payload = _get(url)
    if str(payload.get("code")) != "00000":
        raise RuntimeError(f"Bitget tickers error: {payload.get('msg') or payload}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("Bitget tickers: unexpected payload")
    return data


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _book_row(sym: str, side: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    max_lev = config.EXCHANGE_MAX_LEV.get(sym)
    base = {
        "symbol": sym,
        "side": side,  # us | crypto
        "mark": None,
        "bid": None,
        "ask": None,
        "change24h": None,
        "change24h_pct": None,
        "high24h": None,
        "low24h": None,
        "index_price": None,
        "funding_rate": None,
        "ts_ms": None,
        "exchange_max_lev": max_lev,
        "position_lev": None,  # never invent
        "available": False,
        "error": None,
    }
    if not raw:
        base["error"] = "symbol not in Bitget USDT-FUTURES ticker set"
        return base
    last = _f(raw.get("lastPr"))
    ch = _f(raw.get("change24h"))
    base.update(
        {
            "mark": last,
            "bid": _f(raw.get("bidPr")),
            "ask": _f(raw.get("askPr")),
            "change24h": ch,
            "change24h_pct": (ch * 100.0) if ch is not None else None,
            "high24h": _f(raw.get("high24h")),
            "low24h": _f(raw.get("low24h")),
            "index_price": _f(raw.get("indexPrice")),
            "funding_rate": _f(raw.get("fundingRate")),
            "ts_ms": int(raw.get("ts") or 0) or None,
            "available": last is not None,
        }
    )
    return base


def get_books(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if (
        not force
        and _cache["books"] is not None
        and (now - float(_cache["ts"])) < config.BOOKS_CACHE_SEC
    ):
        out = dict(_cache["books"])
        out["cached"] = True
        out["cache_age_sec"] = round(now - float(_cache["ts"]), 3)
        return out

    fetched_at = time.time()
    try:
        all_t = fetch_all_tickers()
        by_sym = {str(x.get("symbol")): x for x in all_t if x.get("symbol")}
        us = [_book_row(s, "us", by_sym.get(s)) for s in config.US_UNIVERSE]
        crypto = [_book_row(s, "crypto", by_sym.get(s)) for s in config.CRYPTO_UNIVERSE]
        books = {
            "ok": True,
            "source": "bitget_public_usdt_futures",
            "productType": config.PRODUCT_TYPE,
            "fetched_at": fetched_at,
            "cached": False,
            "cache_age_sec": 0.0,
            "us": us,
            "crypto": crypto,
            "error": None,
        }
        _cache["ts"] = fetched_at
        _cache["books"] = books
        _cache["error"] = None
        return books
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, RuntimeError) as e:
        err = str(e)
        _cache["error"] = err
        if _cache["books"] is not None:
            stale = dict(_cache["books"])
            stale["ok"] = False
            stale["cached"] = True
            stale["cache_age_sec"] = round(now - float(_cache["ts"]), 3)
            stale["error"] = f"stale cache; fetch failed: {err}"
            return stale
        return {
            "ok": False,
            "source": "bitget_public_usdt_futures",
            "productType": config.PRODUCT_TYPE,
            "fetched_at": fetched_at,
            "cached": False,
            "cache_age_sec": 0.0,
            "us": [_book_row(s, "us", None) for s in config.US_UNIVERSE],
            "crypto": [_book_row(s, "crypto", None) for s in config.CRYPTO_UNIVERSE],
            "error": err,
        }


def marks_map(books: dict[str, Any] | None = None) -> dict[str, float]:
    b = books or get_books()
    out: dict[str, float] = {}
    for row in (b.get("us") or []) + (b.get("crypto") or []):
        m = row.get("mark")
        if m is not None:
            out[row["symbol"]] = float(m)
    return out
