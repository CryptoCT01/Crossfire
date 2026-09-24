"""CMC Witness feeds for Rule-2 + dashboard.

Uses CMC_API_KEY from env (also loaded from Desktop/cmc-witness/.env as fallback).
Never invents numbers. Cache TTL keeps credit use sane.
Live sentiment pulse is computed from Pro Fear&Greed history (not a stale MCP seed).
Optional logs/cmc_skills_cache.json can overlay extra skill packs; stale extras are labeled.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CACHE: dict[str, Any] = {"ts": 0.0, "pack": None}
_CREDITS: dict[str, Any] = {"ts": 0.0, "credits": None}
_TTL = 90.0  # seconds — quotes / global / F&G
_CREDITS_TTL = 900.0  # key/info is observational; don't spend every pack
_SKILLS_CACHE = _ROOT / "logs" / "cmc_skills_cache.json"
_BAY_SEED = _ROOT / "crossfire" / "cmc_bay_seed.json"
_SKILLS_STALE_SEC = 2 * 3600
_NEWS_TTL = 300.0
_SLOW_TTL = 600.0
_FAST_TTL = 300.0
_TA_TTL = 1800.0
_SLOW: dict[str, Any] = {
    "news_ts": 0.0, "news": None,
    "alt_ts": 0.0, "alt": None,
    "liq_ts": 0.0, "liq": None,
    "ta_ts": 0.0, "ta": None,
    "narr_ts": 0.0, "narr": None,
    "deriv_ts": 0.0, "deriv": None,
}
_BASE = "https://pro-api.coinmarketcap.com"


def _api_key() -> str:
    k = (os.environ.get("CMC_API_KEY") or os.environ.get("COINMARKETCAP_API_KEY") or "").strip()
    if k:
        return k
    # fallback: witness project env (not committed)
    for p in (
        Path.home() / "Desktop" / "cmc-witness" / ".env",
        _ROOT.parent / "cmc-witness" / ".env",
    ):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("CMC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _ssl():
    try:
        import certifi
        import ssl

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        import ssl

        return ssl.create_default_context()


def _get(path: str, params: dict[str, str] | None = None, timeout: float = 18.0) -> dict[str, Any]:
    key = _api_key()
    if not key:
        return {"ok": False, "error": "CMC_API_KEY missing"}
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{_BASE}{path}{q}"
    req = urllib.request.Request(
        url,
        headers={
            "X-CMC_PRO_API_KEY": key,
            "Accept": "application/json",
            "User-Agent": "Crossfire/0.3 (+Bitget AI Hackathon S2; CMC Witness)",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        st = raw.get("status") or {}
        if st.get("error_code") not in (0, None, "0"):
            return {
                "ok": False,
                "error": st.get("error_message") or f"cmc error {st.get('error_code')}",
                "status": st,
            }
        return {"ok": True, "data": raw.get("data"), "status": st}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _fmt_usd(n: float | None) -> str | None:
    if n is None:
        return None
    v = float(n)
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${a/1e12:.2f}T"
    if a >= 1e9:
        return f"{sign}${a/1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:.2f}M"
    if a >= 1e3:
        return f"{sign}${a/1e3:.1f}K"
    return f"{sign}${a:.0f}"


def _live_headlines(now: float) -> dict[str, Any]:
    """CoinDesk RSS. CMC /v1/content/latest is not on this Pro plan."""
    cached = _SLOW.get("news")
    if cached and now - float(_SLOW["news_ts"]) < _NEWS_TTL:
        return cached
    items: list[dict[str, Any]] = []
    err = None
    try:
        req = urllib.request.Request(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            headers={"User-Agent": "Crossfire/0.3", "Accept": "application/rss+xml"},
        )
        with urllib.request.urlopen(req, timeout=12, context=_ssl()) as resp:
            root = ET.fromstring(resp.read())
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "url": (item.findtext("link") or "").strip() or None,
                    "published": (item.findtext("pubDate") or "").strip() or None,
                }
            )
            if len(items) >= 12:
                break
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    btc = [n for n in items if any(w in n["title"].lower() for w in ("bitcoin", "btc"))]
    scope = "btc" if len(btc) >= 2 else "crypto"
    rows = (btc if scope == "btc" else items)[:5]
    pack = {
        "items": rows,
        "scope": scope,
        "source": "coindesk_rss",
        "note": "CMC content/latest is not on this plan. Headlines are CoinDesk, live.",
        "error": err,
        "fetched_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    if rows:
        _SLOW["news"] = pack
        _SLOW["news_ts"] = now
    return pack


def _altcoin_season(now: float) -> dict[str, Any] | None:
    cached = _SLOW.get("alt")
    if cached and now - float(_SLOW["alt_ts"]) < _SLOW_TTL:
        return cached
    req = urllib.request.Request(
        f"{_BASE}/public-api/v1/altcoin-season-index/latest",
        headers={"Accept": "application/json", "User-Agent": "Crossfire/0.3"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12, context=_ssl()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return cached if isinstance(cached, dict) else None
    d = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    idx = d.get("altcoin_index")
    if idx is None:
        return cached if isinstance(cached, dict) else None
    pack = {
        "altcoin_index": idx,
        "altcoin_marketcap": _f(d.get("altcoin_marketcap")),
        "snapshot_time": d.get("snapshot_time"),
        "source": "cmc_altcoin_season_index",
    }
    _SLOW["alt"] = pack
    _SLOW["alt_ts"] = now
    return pack


def _btc_liquidations(now: float) -> dict[str, Any] | None:
    """1 credit per call. Cached 10 minutes — do not attach this to the 90s quote pack."""
    cached = _SLOW.get("liq")
    if cached and now - float(_SLOW["liq_ts"]) < _FAST_TTL:
        return cached
    res = _get(
        "/v5/derivatives/liquidations/cryptocurrency/list/latest",
        {"crypto_id": "1", "limit": "1"},
    )
    if not res.get("ok"):
        return cached if isinstance(cached, dict) else None
    raw_data = res.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    coins = data.get("cryptocurrencies") or []
    if not coins:
        return cached if isinstance(cached, dict) else None
    quotes = coins[0].get("quotes") or []
    q = quotes[0] if quotes else {}
    total = _f(q.get("total_liquidations_24h"))
    long_v = _f(q.get("long_liquidations_24h"))
    short_v = _f(q.get("short_liquidations_24h"))
    pack = {
        "total_24h": total,
        "long_24h": long_v,
        "short_24h": short_v,
        "total_24h_label": _fmt_usd(total),
        "long_24h_label": _fmt_usd(long_v),
        "short_24h_label": _fmt_usd(short_v),
        "last_updated": q.get("last_updated"),
        "source": "cmc_pro_liquidations_btc",
    }
    _SLOW["liq"] = pack
    _SLOW["liq_ts"] = now
    return pack


def _closes_from_ohlcv(node: Any) -> list[float]:
    if not isinstance(node, dict):
        return []
    out: list[float] = []
    for q in node.get("quotes") or []:
        if not isinstance(q, dict):
            continue
        usd = ((q.get("quote") or {}).get("USD")) or {}
        c = _f(usd.get("close"))
        if c is not None:
            out.append(c)
    return out


def _rsi(vals: list[float], period: int = 14) -> float | None:
    if len(vals) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = vals[i] - vals[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _ema_last(vals: list[float], period: int) -> float | None:
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = v * k + e * (1.0 - k)
    return e


def _macd_hist(vals: list[float]) -> float | None:
    if len(vals) < 40:
        return None
    k12 = 2.0 / 13
    k26 = 2.0 / 27
    e12 = sum(vals[:12]) / 12
    e26 = sum(vals[:26]) / 26
    macd: list[float] = []
    for i, v in enumerate(vals):
        if i >= 12:
            e12 = v * k12 + e12 * (1.0 - k12)
        if i >= 26:
            e26 = v * k26 + e26 * (1.0 - k26)
        if i >= 26:
            macd.append(e12 - e26)
    sig = _ema_last(macd, 9)
    if sig is None or not macd:
        return None
    return macd[-1] - sig


def _sma(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _corr(a: list[float], b: list[float], n: int = 30) -> float | None:
    if len(a) < n or len(b) < n:
        return None
    xs, ys = a[-n:], b[-n:]
    rx = [xs[i] / xs[i - 1] - 1 for i in range(1, n) if xs[i - 1]]
    ry = [ys[i] / ys[i - 1] - 1 for i in range(1, n) if ys[i - 1]]
    m = min(len(rx), len(ry))
    if m < 8:
        return None
    rx, ry = rx[-m:], ry[-m:]
    mx = sum(rx) / m
    my = sum(ry) / m
    num = sum((x - mx) * (y - my) for x, y in zip(rx, ry))
    dx = sum((x - mx) ** 2 for x in rx) ** 0.5
    dy = sum((y - my) ** 2 for y in ry) ** 0.5
    if not dx or not dy:
        return None
    return num / (dx * dy)


def _ta_read(rsi: float | None, macd: float | None, price: float | None, sma30: float | None) -> str:
    bits = []
    if rsi is None:
        bits.append("RSI —")
    elif rsi >= 70:
        bits.append(f"RSI14 {rsi:.0f} hot")
    elif rsi <= 30:
        bits.append(f"RSI14 {rsi:.0f} washed")
    else:
        bits.append(f"RSI14 {rsi:.0f} neutral")
    if macd is not None:
        bits.append("MACD hist " + ("positive" if macd > 0 else "negative"))
    if price is not None and sma30 is not None:
        bits.append("price " + ("above" if price > sma30 else "below") + " SMA30")
    return "; ".join(bits)


def _ta_leg(closes: list[float]) -> dict[str, Any]:
    rsi = _rsi(closes, 14)
    macd = _macd_hist(closes)
    sma30 = _sma(closes, 30)
    sma200 = _sma(closes, 200)
    price = closes[-1] if closes else None
    return {
        "rsi14": round(rsi, 1) if rsi is not None else None,
        "macd": round(macd, 2) if macd is not None else None,
        "sma30": round(sma30, 2) if sma30 is not None else None,
        "sma200": round(sma200, 2) if sma200 is not None else None,
        "read": _ta_read(rsi, macd, price, sma30),
    }


def _live_ta(now: float) -> dict[str, Any] | None:
    cached = _SLOW.get("ta")
    if isinstance(cached, dict) and now - float(_SLOW["ta_ts"]) < _TA_TTL:
        return cached
    res = _get(
        "/v2/cryptocurrency/ohlcv/historical",
        {"id": "1,1027", "time_period": "daily", "count": "210", "convert": "USD"},
    )
    if not res.get("ok") or not isinstance(res.get("data"), dict):
        return cached if isinstance(cached, dict) else None
    data = res["data"]
    btc = _closes_from_ohlcv(data.get("1") or data.get(1))
    eth = _closes_from_ohlcv(data.get("1027") or data.get(1027))
    if len(btc) < 30:
        return cached if isinstance(cached, dict) else None
    pack = {
        "BTC": _ta_leg(btc),
        "ETH": _ta_leg(eth),
        "btc_eth_corr_30d": round(c, 2) if (c := _corr(btc, eth, 30)) is not None else None,
        "bars": len(btc),
        "source": "cmc_ohlcv_daily",
        "fetched_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    _SLOW["ta"] = pack
    _SLOW["ta_ts"] = now
    return pack


def _live_narratives(now: float) -> list[dict[str, Any]] | None:
    cached = _SLOW.get("narr")
    if isinstance(cached, list) and now - float(_SLOW["narr_ts"]) < _SLOW_TTL:
        return cached
    res = _get("/v1/cryptocurrency/categories", {"limit": "100"})
    if not res.get("ok") or not isinstance(res.get("data"), list):
        return cached if isinstance(cached, list) else None
    rows = []
    for r in res["data"]:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "")
        if "Portfolio" in name:
            continue
        cap = _f(r.get("market_cap")) or 0.0
        chg = _f(r.get("market_cap_change"))
        if chg is None or cap < 2e9 or cap > 8e11:
            continue
        rows.append((abs(chg), name, cap, chg))
    rows.sort(reverse=True)
    if len(rows) < 3:
        rows = []
        for r in res["data"]:
            if not isinstance(r, dict) or "Portfolio" in str(r.get("name") or ""):
                continue
            chg = _f(r.get("market_cap_change"))
            cap = _f(r.get("market_cap")) or 0.0
            if chg is None:
                continue
            rows.append((abs(chg), str(r.get("name") or ""), cap, chg))
        rows.sort(reverse=True)
    out = []
    for i, (_a, name, cap, chg) in enumerate(rows[:5], start=1):
        sign = "+" if chg >= 0 else ""
        out.append(
            {
                "rank": i,
                "name": name,
                "mcap": _fmt_usd(cap),
                "chg_24h": f"{sign}{chg:.2f}%",
            }
        )
    if not out:
        return cached if isinstance(cached, list) else None
    _SLOW["narr"] = out
    _SLOW["narr_ts"] = now
    return out


def _live_derivatives(now: float) -> dict[str, Any] | None:
    cached = _SLOW.get("deriv")
    if isinstance(cached, dict) and now - float(_SLOW["deriv_ts"]) < _FAST_TTL:
        return cached
    res = _get(
        "/v5/cryptocurrency/derivatives/market-pairs/list/latest",
        {
            "crypto_id": "1",
            "category": "perpetual",
            "limit": "200",
            "sort": "volume_24h_strict",
            "sort_dir": "desc",
        },
    )
    if not res.get("ok") or not isinstance(res.get("data"), dict):
        return cached if isinstance(cached, dict) else None
    pairs = res["data"].get("market_pairs") or []
    oi = 0.0
    wfund = 0.0
    wvol = 0.0
    n = 0
    for p in pairs:
        if not isinstance(p, dict):
            continue
        reported = p.get("exchange_reported_quotes") or []
        er = reported[0] if reported else {}
        if not isinstance(er, dict):
            continue
        oi_raw = float(er.get("open_interest") or 0)
        # One venue has reported ~$10T. That is not Bitcoin open interest.
        if 0 < oi_raw < 80e9:
            oi += oi_raw
        vol = float(er.get("volume_24h_quote") or 0)
        fr = _f(er.get("funding_rate"))
        if fr is not None and vol > 0:
            wfund += fr * vol
            wvol += vol
        n += 1
    if not n:
        return cached if isinstance(cached, dict) else None
    fund = (wfund / wvol) if wvol else None
    pack = {
        "oi_label": _fmt_usd(oi),
        "funding_label": None if fund is None else f"{fund * 100:.4f}%",
        "pairs": n,
        "tracked": res["data"].get("num_market_pairs"),
        "source": "cmc_btc_perps",
        "fetched_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    _SLOW["deriv"] = pack
    _SLOW["deriv_ts"] = now
    return pack


def _bay_content_stamp(bay: Any, fallback: str | None) -> str | None:
    """The snapshot's own clock. Never the file rewrite time."""
    if isinstance(bay, dict) and bay.get("as_of"):
        return str(bay["as_of"])
    return fallback


def _load_skills_cache() -> dict[str, Any]:
    try:
        if _SKILLS_CACHE.is_file():
            return json.loads(_SKILLS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_skills_cache(obj: dict[str, Any]) -> None:
    try:
        _SKILLS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _SKILLS_CACHE.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def _f(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_ts(row: dict[str, Any]) -> float:
    t = row.get("timestamp") or row.get("last_updated") or row.get("time_until_update")
    if isinstance(t, str) and t.strip().isdigit():
        t = float(t.strip())
    if isinstance(t, (int, float)):
        v = float(t)
        return v / 1000.0 if v > 1e12 else v
    if isinstance(t, str) and t:
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _fng_row(data: Any) -> dict[str, Any] | None:
    row = None
    if isinstance(data, list) and data:
        row = data[0] if isinstance(data[0], dict) else None
    elif isinstance(data, dict):
        inner = data.get("data") if isinstance(data.get("data"), list) else None
        if inner:
            row = inner[0] if isinstance(inner[0], dict) else None
        else:
            row = data
    return row if isinstance(row, dict) else None


def _fear_greed_from_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    v = _f(row.get("value") or row.get("score"))
    if v is None:
        return None
    return {
        "value": int(round(v)),
        "label": row.get("value_classification") or row.get("classification") or row.get("name"),
        "source": "cmc_pro_fear_greed",
    }


def _pulse_from_history(data: Any, latest: dict[str, Any] | None) -> dict[str, Any] | None:
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        rows = [r for r in data["data"] if isinstance(r, dict)]
    parsed: list[dict[str, Any]] = []
    for r in rows:
        v = _f(r.get("value") or r.get("score"))
        if v is None:
            continue
        parsed.append(
            {
                "value": float(v),
                "label": r.get("value_classification") or r.get("classification"),
                "ts": _parse_ts(r),
            }
        )
    if not parsed and latest and latest.get("value") is not None:
        return {
            "fear_greed_value": int(latest["value"]),
            "fear_greed_label": (latest.get("label") or "").lower() or None,
            "change_7d_points": None,
            "avg_30d": None,
            "n_hist": 0,
            "source": "cmc_pro_fear_greed_latest_only",
            "summary": f"Crypto Fear & Greed is {latest['value']} ({latest.get('label') or 'unlabeled'}); 7d/30d history unavailable.",
        }
    if not parsed:
        return None
    parsed.sort(key=lambda x: x["ts"] or 0.0)
    last = parsed[-1]
    # Prefer /latest as current F&G when it disagrees with a mis-ordered hist row
    if latest and latest.get("value") is not None:
        last_val = float(latest["value"])
        last_label = latest.get("label")
        now_ts = last["ts"] or time.time()
    else:
        last_val = float(last["value"])
        last_label = last.get("label")
        now_ts = last["ts"] or time.time()
    target_7d = now_ts - 7 * 86400
    dated = [p for p in parsed if (p["ts"] or 0) > 0]
    if dated:
        base_7 = min(dated, key=lambda x: abs((x["ts"] or 0.0) - target_7d))
        # Guard: baseline should be older than ~3d when we have a week of points
        if abs((base_7["ts"] or 0) - now_ts) < 3 * 86400 and len(dated) >= 8:
            older = [p for p in dated if now_ts - (p["ts"] or 0) >= 6 * 86400]
            if older:
                base_7 = min(older, key=lambda x: abs((x["ts"] or 0.0) - target_7d))
    else:
        base_7 = parsed[-8] if len(parsed) >= 8 else parsed[0]
    window30 = parsed[-30:] if len(parsed) >= 2 else parsed
    avg30 = sum(p["value"] for p in window30) / len(window30) if window30 else None
    ch7 = last_val - float(base_7["value"])
    label = (last_label or "").lower() or None
    ch7_i = int(round(ch7))
    summary = (
        f"Crypto Fear & Greed is {int(round(last_val))}"
        + (f" ({label})" if label else "")
        + f", {abs(ch7_i)} points {'down' if ch7_i < 0 else 'up' if ch7_i > 0 else 'unchanged'} over ~7 days."
    )
    return {
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts if now_ts > 1e9 else time.time())),
        "fear_greed_value": int(round(last_val)),
        "fear_greed_label": label,
        "change_7d_points": ch7_i,
        "baseline_7d_value": int(round(base_7["value"])),
        "avg_30d": round(avg30, 1) if avg30 is not None else None,
        "n_hist": len(parsed),
        "source": "cmc_pro_fear_greed_historical",
        "summary": summary,
    }


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:+.2f}%"


def _build_brief(
    btc: dict[str, Any] | None,
    eth: dict[str, Any] | None,
    gm: dict[str, Any] | None,
    fg: dict[str, Any] | None,
    pulse: dict[str, Any] | None,
) -> str:
    bits: list[str] = []
    if btc and btc.get("pct_24h") is not None:
        bits.append(f"CMC BTC {_fmt_pct(btc['pct_24h'])}")
        if btc.get("price") is not None:
            bits.append(f"${btc['price']:,.0f}")
    if eth and eth.get("pct_24h") is not None:
        bits.append(f"ETH {_fmt_pct(eth['pct_24h'])}")
    if gm:
        if gm.get("btc_dominance") is not None:
            bits.append(f"BTC.D {gm['btc_dominance']:.2f}%")
        if gm.get("eth_dominance") is not None:
            bits.append(f"ETH.D {gm['eth_dominance']:.2f}%")
        if gm.get("total_market_cap_yesterday_percentage_change") is not None:
            bits.append(f"mcap {_fmt_pct(gm['total_market_cap_yesterday_percentage_change'])}")
    if fg and fg.get("value") is not None:
        lab = fg.get("label") or ""
        bits.append(f"F&G {fg['value']} {lab}".strip())
    if pulse and pulse.get("change_7d_points") is not None:
        bits.append(f"F&G 7d {pulse['change_7d_points']:+d}")
    if pulse and pulse.get("avg_30d") is not None:
        bits.append(f"F&G 30d avg {pulse['avg_30d']}")
    return "; ".join(bits) if bits else "cmc:unknown"


def _quote_entry(coin: dict[str, Any], cid: str, sym: str) -> dict[str, Any]:
    q = (coin.get("quote") or {}).get("USD") or {}
    return {
        "id": int(cid),
        "symbol": coin.get("symbol") or sym,
        "name": coin.get("name"),
        "price": _f(q.get("price")),
        "pct_1h": _f(q.get("percent_change_1h")),
        "pct_24h": _f(q.get("percent_change_24h")),
        "pct_7d": _f(q.get("percent_change_7d")),
        "market_cap": _f(q.get("market_cap")),
        "volume_24h": _f(q.get("volume_24h")),
        "dominance": _f(q.get("market_cap_dominance")),
        "source": "cmc_pro_quotes_latest",
    }


def _skills_overlay(now: float) -> dict[str, Any] | None:
    """Keep extra MCP skill fields only when fresh; never treat stale numbers as live."""
    raw = _load_skills_cache()
    if not raw:
        return None
    updated = raw.get("updated_at") or raw.get("as_of")
    ts = 0.0
    if isinstance(updated, str):
        try:
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0
    age = (now - ts) if ts else None
    stale = age is None or age > _SKILLS_STALE_SEC
    extra = {
        k: v
        for k, v in raw.items()
        if k
        not in (
            "updated_at",
            "bay_as_of",
            "source",
            "note",
            "quotes",
            "crypto_sentiment_pulse",
            "global_metrics_skill",
            "stale",
            "age_sec",
        )
    }
    if not extra:
        return None
    return {
        "updated_at": updated,
        "source": raw.get("source") or "cmc_skills_cache",
        "stale": stale,
        "age_sec": int(age) if age is not None else None,
        "extra": extra if not stale else None,
        "note": "stale MCP extras omitted" if stale else "fresh MCP extras",
    }


def compact_for_context(pack: dict[str, Any]) -> dict[str, Any]:
    """Slim CMC object for the LLM payload — no skills dump, no credits."""
    if not pack:
        return {"ok": False, "error": "cmc pack empty"}
    if not pack.get("ok"):
        return {
            "ok": False,
            "error": pack.get("error") or "cmc unavailable",
            "sources": pack.get("sources") or ["cmc:unavailable"],
        }
    btc = pack.get("btc") if isinstance(pack.get("btc"), dict) else None
    eth = pack.get("eth") if isinstance(pack.get("eth"), dict) else None

    def _slim_q(q: dict[str, Any] | None) -> dict[str, Any] | None:
        if not q:
            return None
        return {
            "symbol": q.get("symbol"),
            "price": q.get("price"),
            "pct_1h": q.get("pct_1h"),
            "pct_24h": q.get("pct_24h"),
            "pct_7d": q.get("pct_7d"),
            "dominance": q.get("dominance"),
        }

    gm = pack.get("global") if isinstance(pack.get("global"), dict) else None
    gm_slim = None
    if gm:
        gm_slim = {
            "btc_dominance": gm.get("btc_dominance"),
            "eth_dominance": gm.get("eth_dominance"),
            "total_market_cap": gm.get("total_market_cap"),
            "total_volume_24h": gm.get("total_volume_24h"),
            "mcap_24h_pct": gm.get("total_market_cap_yesterday_percentage_change"),
        }
    return {
        "ok": True,
        "brief": pack.get("brief"),
        "btc": _slim_q(btc),
        "eth": _slim_q(eth),
        "global": gm_slim,
        "fear_greed": pack.get("fear_greed"),
        "sentiment_pulse": pack.get("sentiment_pulse"),
        "mag7_note": "Mag7/US names are Dual Book (Bitget), not CMC — CMC is crypto tape only",
        "sources": pack.get("sources") or [],
        "fetched_at_iso": pack.get("fetched_at_iso"),
    }


def fetch_cmc_pack(force: bool = False) -> dict[str, Any]:
    """Cached CMC Witness pack for perception + /api/cmc."""
    now = time.time()
    if not force and _CACHE["pack"] and now - float(_CACHE["ts"]) < _TTL:
        return _CACHE["pack"]

    sources: list[str] = []
    key_ok = bool(_api_key())
    if not key_ok:
        pack = {
            "ok": False,
            "fetched_at": now,
            "fetched_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "error": "CMC_API_KEY not configured",
            "sources": ["cmc:no_key"],
            "credits_note": "Set CMC_API_KEY (cmc-witness/.env)",
            "brief": "cmc:no_key",
        }
        _CACHE["ts"] = now
        _CACHE["pack"] = pack
        return pack

    global_m = _get("/v1/global-metrics/quotes/latest")
    quotes = _get("/v1/cryptocurrency/quotes/latest", {"id": "1,1027", "convert": "USD"})
    fng = _get("/v3/fear-and-greed/latest")
    fng_hist = _get("/v3/fear-and-greed/historical", {"limit": "30"})

    fear_greed = None
    if fng.get("ok"):
        fear_greed = _fear_greed_from_row(_fng_row(fng.get("data")))
        if fear_greed:
            sources.append("cmc.fear_greed")
    if fear_greed is None and fng_hist.get("ok"):
        fear_greed = _fear_greed_from_row(_fng_row(fng_hist.get("data")))
        if fear_greed:
            fear_greed["source"] = "cmc_pro_fear_greed_historical"
            sources.append("cmc.fear_greed")

    pulse = _pulse_from_history(fng_hist.get("data") if fng_hist.get("ok") else None, fear_greed)
    if pulse:
        sources.append("cmc.sentiment_pulse")

    btc = eth = None
    if quotes.get("ok") and isinstance(quotes.get("data"), dict):
        sources.append("cmc.quotes_btc_eth")
        for cid, sym in (("1", "BTC"), ("1027", "ETH")):
            coin = quotes["data"].get(cid) or {}
            entry = _quote_entry(coin, cid, sym)
            if sym == "BTC":
                btc = entry
            else:
                eth = entry

    gm = None
    if global_m.get("ok") and isinstance(global_m.get("data"), dict):
        sources.append("cmc.global_metrics")
        d = global_m["data"]
        q = (d.get("quote") or {}).get("USD") or {}
        gm = {
            "total_market_cap": _f(q.get("total_market_cap")),
            "total_volume_24h": _f(q.get("total_volume_24h")),
            "btc_dominance": _f(d.get("btc_dominance")),
            "eth_dominance": _f(d.get("eth_dominance")),
            "defi_market_cap": _f(q.get("defi_market_cap")),
            "stablecoin_market_cap": _f(q.get("stablecoin_market_cap")),
            "total_market_cap_yesterday_percentage_change": _f(
                q.get("total_market_cap_yesterday_percentage_change")
            ),
            "last_updated": d.get("last_updated") or q.get("last_updated"),
            "source": "cmc_pro_global_metrics",
        }

    overlay = _skills_overlay(now)
    if overlay and overlay.get("extra"):
        sources.append("cmc.skills_cache")

    credits = None
    if _CREDITS["credits"] and now - float(_CREDITS["ts"]) < _CREDITS_TTL:
        credits = _CREDITS["credits"]
        sources.append("cmc.key_info_cached")
    else:
        key_info = _get("/v1/key/info")
        if key_info.get("ok") and isinstance(key_info.get("data"), dict):
            sources.append("cmc.key_info")
            usage = (key_info["data"].get("usage") or {}).get("current_month") or {}
            plan = key_info["data"].get("plan") or {}
            credits = {
                "credits_left": usage.get("credits_left"),
                "credits_used": usage.get("credits_used"),
                "credit_limit_monthly": plan.get("credit_limit_monthly"),
                "source": "cmc_pro_key_info",
            }
            _CREDITS["ts"] = now
            _CREDITS["credits"] = credits

    headlines = _live_headlines(now)
    if headlines.get("items"):
        sources.append("coindesk_rss")
    alt = _altcoin_season(now)
    if alt:
        sources.append("cmc.altcoin_season")
    liq = _btc_liquidations(now)
    if liq:
        sources.append("cmc.liquidations_btc")
    ta = _live_ta(now)
    if ta:
        sources.append("cmc.ohlcv_ta")
    narr_live = _live_narratives(now)
    if narr_live:
        sources.append("cmc.categories")
    deriv = _live_derivatives(now)
    if deriv:
        sources.append("cmc.btc_perps")

    brief = _build_brief(btc, eth, gm, fear_greed, pulse)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    # Refresh on-disk pulse from live Pro API (honest; extras kept only if not stale)
    cache_out: dict[str, Any] = {
        "updated_at": iso,
        "source": "crossfire.cmc_feeds live Pro API",
        "quotes": {
            "BTC": {
                "price": (btc or {}).get("price"),
                "pct_24h": (btc or {}).get("pct_24h"),
            },
            "ETH": {
                "price": (eth or {}).get("price"),
                "pct_24h": (eth or {}).get("pct_24h"),
            },
        },
        "crypto_sentiment_pulse": pulse,
        "global_metrics_skill": {
            "btc_dominance": (gm or {}).get("btc_dominance"),
            "eth_dominance": (gm or {}).get("eth_dominance"),
            "market_cap": (gm or {}).get("total_market_cap"),
            "mcap_24h_pct": (gm or {}).get("total_market_cap_yesterday_percentage_change"),
            "fear_greed": fear_greed,
        },
        "note": "Live Pro API pack; Mag7 is Dual Book not CMC",
    }
    # Always load previous cache BEFORE write so bay survives Pro-API refresh
    try:
        prev_cache = _load_skills_cache()
    except Exception:
        prev_cache = {}
    if not isinstance(prev_cache, dict):
        prev_cache = {}

    bay_keep = None
    bay_as_of_keep = None
    if isinstance(prev_cache.get("bay"), dict):
        bay_keep = prev_cache["bay"]
        bay_as_of_keep = prev_cache.get("bay_as_of") or prev_cache.get("updated_at")
    if bay_keep is None and _BAY_SEED.is_file():
        try:
            seed = json.loads(_BAY_SEED.read_text(encoding="utf-8"))
            if isinstance(seed.get("bay"), dict):
                bay_keep = seed["bay"]
                bay_as_of_keep = seed.get("bay_as_of") or seed.get("updated_at")
        except Exception:
            pass

    if overlay and isinstance(overlay.get("extra"), dict):
        extra = dict(overlay["extra"])
        if isinstance(extra.get("bay"), dict):
            bay_keep = extra.pop("bay")
            bay_as_of_keep = overlay.get("updated_at") or bay_as_of_keep
        # strip nested mcp_extras noise
        extra.pop("mcp_extras", None)
        extra.pop("mcp_extras_as_of", None)
        if extra:
            cache_out["mcp_extras"] = extra
            cache_out["mcp_extras_as_of"] = overlay.get("updated_at")

    if bay_keep is not None:
        cache_out["bay"] = bay_keep
        cache_out["bay_as_of"] = _bay_content_stamp(bay_keep, bay_as_of_keep)

    _write_skills_cache(cache_out)

    bay = cache_out.get("bay")
    bay_as_of = cache_out.get("bay_as_of")

    pack = {
        "ok": bool(btc or eth or gm or fear_greed),
        "fetched_at": now,
        "fetched_at_iso": iso,
        "btc": btc,
        "eth": eth,
        "global": gm,
        "fear_greed": fear_greed,
        "sentiment_pulse": pulse,
        "brief": brief,
        "skills": overlay,
        "bay": bay,
        "bay_as_of": _bay_content_stamp(bay, bay_as_of),
        "news": headlines,
        "altcoin_season": alt,
        "liquidations_btc": liq,
        "ta": ta,
        "narratives": narr_live,
        "derivatives_live": deriv,
        "credits": credits,
        "sources": sources or ["cmc:unavailable"],
        "witness": "CMC Witness · Pro API",
        "mag7_note": "Mag7/US names are Dual Book (Bitget), not CMC",
        "equities_note": (bay or {}).get("equities_note")
        if isinstance(bay, dict)
        else "CMC research skills can cover US equities; Mag7 marks stay on Dual Book.",
    }
    if not pack["ok"]:
        pack["error"] = "CMC Pro calls returned no usable quotes/global/F&G"
        pack["brief"] = "cmc:unavailable"
    _CACHE["ts"] = now
    _CACHE["pack"] = pack
    return pack
