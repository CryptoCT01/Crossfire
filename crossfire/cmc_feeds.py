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
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CACHE: dict[str, Any] = {"ts": 0.0, "pack": None}
_CREDITS: dict[str, Any] = {"ts": 0.0, "credits": None}
_TTL = 90.0  # seconds — quotes / global / F&G
_CREDITS_TTL = 900.0  # key/info is observational; don't spend every pack
_SKILLS_CACHE = _ROOT / "logs" / "cmc_skills_cache.json"
_SKILLS_STALE_SEC = 2 * 3600
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
    if overlay and overlay.get("extra"):
        cache_out["mcp_extras"] = overlay["extra"]
        cache_out["mcp_extras_as_of"] = overlay.get("updated_at")
    _write_skills_cache(cache_out)

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
        "credits": credits,
        "sources": sources or ["cmc:unavailable"],
        "witness": "CMC Witness · Pro API",
        "mag7_note": "Mag7/US names are Dual Book (Bitget), not CMC",
    }
    if not pack["ok"]:
        pack["error"] = "CMC Pro calls returned no usable quotes/global/F&G"
        pack["brief"] = "cmc:unavailable"
    _CACHE["ts"] = now
    _CACHE["pack"] = pack
    return pack
