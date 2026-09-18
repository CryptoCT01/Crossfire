"""Rule-2 perception pack — Dual Book + session + Bitget toolkit mirrors.

build_context() is called every heartbeat / event wake and injected as
`context` on the LLM user payload (and stored on the tick/decision record).

Determinism (documented):
  - macro / us_tape / sentiment are derived from:
      * books.us / books.crypto marks & change24h_pct & funding_rate
      * session.us_cash_status / us_cash_rth_open
      * optional derived Mag7/BTC averages (or recomputed here)
      * optional public Bitget open-interest for BTC (best-effort; fail → omit)
      * bitget_feeds toolkit pack (Fear&Greed, CoinDesk RSS, Yahoo NDX/DXY/Mag7)
        — public mirrors of Bitget S2 MCP/signal skills; never invented
      * cmc_feeds Witness pack (BTC/ETH quotes, global dominance, F&G pulse)
        — CoinMarketCap Pro; Mag7 is Dual Book, not CMC
  - news comes from CoinDesk RSS when reachable; else []
  - geopolitics stays level=unknown (no geopolitics feed wired)
  - If books are empty / not ok / insufficient marks → macro/sentiment/us_tape
    become "unknown" so Rule 2 biases HOLD / no new US risk.
"""
from __future__ import annotations

from typing import Any

from . import bitget_feeds
from . import cmc_feeds
from . import config

# Sentiment thresholds (deterministic)
_GREEN_MAJORITY = 55.0  # % symbols with change24h_pct > 0
_RED_MAJORITY = 45.0  # below this = majority red
_FUNDING_HOT = 0.0005  # ~0.05% per 8h — crowded long tilt
_FUNDING_COLD = -0.0005  # crowded short tilt
_DIV_NOTE_PCT = 1.5  # mention Mag7 vs BTC split above this


def _avg_change(rows: list[dict[str, Any]], symbols: list[str] | None = None) -> float | None:
    want = set(symbols) if symbols is not None else None
    vals: list[float] = []
    for r in rows:
        if want is not None and r.get("symbol") not in want:
            continue
        if r.get("change24h_pct") is not None:
            try:
                vals.append(float(r["change24h_pct"]))
            except (TypeError, ValueError):
                continue
    if not vals:
        return None
    return sum(vals) / len(vals)


def _avg_funding(rows: list[dict[str, Any]], symbols: list[str] | None = None) -> float | None:
    want = set(symbols) if symbols is not None else None
    vals: list[float] = []
    for r in rows:
        if want is not None and r.get("symbol") not in want:
            continue
        fr = r.get("funding_rate")
        if fr is not None:
            try:
                vals.append(float(fr))
            except (TypeError, ValueError):
                continue
    if not vals:
        return None
    return sum(vals) / len(vals)


def _breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """% green / red among available rows with a real 24h %. Unknown if none."""
    avail = [r for r in rows if r.get("available") and r.get("change24h_pct") is not None]
    n = len(avail)
    if n == 0:
        return {"n": 0, "green_pct": None, "red_pct": None, "avg_24h_pct": None}
    green = sum(1 for r in avail if float(r["change24h_pct"]) > 0)
    red = sum(1 for r in avail if float(r["change24h_pct"]) < 0)
    avg = sum(float(r["change24h_pct"]) for r in avail) / n
    return {
        "n": n,
        "green_pct": round(100.0 * green / n, 1),
        "red_pct": round(100.0 * red / n, 1),
        "avg_24h_pct": round(avg, 3),
    }


def _fmt_pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "?"
    if signed:
        return f"{v:+.2f}%"
    return f"{v:.2f}%"


def _fmt_funding(v: float | None) -> str:
    if v is None:
        return "?"
    # Bitget fundingRate is typically decimal per period (e.g. 0.0001 = 0.01%)
    return f"{v * 100:.4f}%"


def _try_btc_open_interest() -> dict[str, Any] | None:
    """Best-effort public OI for BTC. Returns None on any failure (no invent)."""
    try:
        from . import bitget_public

        url = (
            f"{config.BITGET_BASE}/api/v2/mix/market/open-interest"
            f"?symbol=BTCUSDT&productType={config.PRODUCT_TYPE}"
        )
        payload = bitget_public._get(url, timeout=6.0)  # noqa: SLF001 — thin reuse
        if str(payload.get("code")) != "00000":
            return None
        data = payload.get("data") or {}
        # Bitget v2: {"openInterestList":[{"symbol":"BTCUSDT","size":"..."}], "ts":"..."}
        row = None
        if isinstance(data, dict):
            lst = data.get("openInterestList")
            if isinstance(lst, list) and lst and isinstance(lst[0], dict):
                row = lst[0]
            else:
                row = data
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            row = data[0]
        if not isinstance(row, dict):
            return None
        oi = row.get("size") or row.get("openInterest") or row.get("holdingAmount")
        if oi is None or oi == "":
            return None
        return {"symbol": str(row.get("symbol") or "BTCUSDT"), "open_interest": float(oi)}
    except Exception:
        return None


def _session_label(session: dict[str, Any] | None) -> str:
    if not session:
        return "unknown"
    return str(session.get("us_cash_status") or "unknown")


def _build_us_tape(
    session: dict[str, Any] | None,
    us_b: dict[str, Any],
    mag7: float | None,
) -> str:
    """Session character from real session + US breadth — never geopolitics."""
    status = _session_label(session)
    if status == "unknown" and us_b.get("n", 0) == 0:
        return "unknown"

    parts: list[str] = []
    if status == "RTH_OPEN":
        parts.append("US cash RTH open")
    elif status == "PRE_MARKET":
        parts.append("US pre-market (gap risk; cash closed)")
    elif status == "AFTER_HOURS":
        parts.append("US after-hours (gap risk; cash closed)")
    elif status == "WEEKEND":
        parts.append("US weekend (gap risk; cash closed)")
    else:
        parts.append(f"US session={status}")

    parts.append("Bitget US contracts 24/7")

    gp = us_b.get("green_pct")
    if gp is not None and us_b.get("n", 0) > 0:
        if gp >= 70:
            parts.append(f"US book trend-up breadth {gp:.0f}% green")
        elif gp <= 30:
            parts.append(f"US book trend-down breadth {gp:.0f}% green")
        else:
            parts.append(f"US book chop/mixed breadth {gp:.0f}% green")
    else:
        parts.append("US breadth unavailable")

    if mag7 is not None:
        parts.append(f"Mag7 24h {_fmt_pct(mag7)}")

    return "; ".join(parts)


def _build_macro(
    session: dict[str, Any] | None,
    mag7: float | None,
    btc: float | None,
    div: float | None,
    btc_fund: float | None,
    mag7_fund: float | None,
    us_b: dict[str, Any],
    cr_b: dict[str, Any],
    oi: dict[str, Any] | None,
) -> str:
    """Short macro string from marks/funding/session — not Fed minutes."""
    if us_b.get("n", 0) == 0 and cr_b.get("n", 0) == 0:
        return "unknown"

    bits: list[str] = []
    bits.append(f"session={_session_label(session)}")
    if not (session or {}).get("us_cash_rth_open"):
        bits.append("cash closed → overnight/gap risk for US names")

    bits.append(f"Mag7 {_fmt_pct(mag7)} vs BTC {_fmt_pct(btc)}")
    if div is not None:
        bits.append(f"div {_fmt_pct(div)}")

    bits.append(f"BTC funding {_fmt_funding(btc_fund)}")
    if mag7_fund is not None:
        bits.append(f"Mag7 avg funding {_fmt_funding(mag7_fund)}")

    if us_b.get("green_pct") is not None:
        bits.append(f"breadth US {us_b['green_pct']:.0f}%/{us_b['n']}")
    if cr_b.get("green_pct") is not None:
        bits.append(f"crypto {cr_b['green_pct']:.0f}%/{cr_b['n']}")

    if oi and oi.get("open_interest") is not None:
        bits.append(f"BTC OI {oi['open_interest']:.0f}")

    bits.append("no rates/USD feed wired")
    return "; ".join(bits)


def _cmc_bits(cmc: dict[str, Any]) -> str:
    """One honest CMC line for Rule-2. Empty string if pack is down."""
    if not isinstance(cmc, dict) or not cmc.get("ok"):
        return ""
    brief = cmc.get("brief")
    if isinstance(brief, str) and brief and brief not in ("cmc:unknown", "cmc:unavailable", "cmc:no_key"):
        return brief
    return ""


def _derive_sentiment(
    us_b: dict[str, Any],
    cr_b: dict[str, Any],
    btc_fund: float | None,
    div: float | None,
) -> str:
    """
    risk_on | risk_off | mixed | unknown

    Rules (deterministic):
      unknown  — either book has no usable marks
      risk_on  — both books green_pct >= 55 AND funding not cold (or funding unknown)
      risk_off — both books green_pct <= 45 OR (BTC funding <= cold AND US green_pct <= 45)
      mixed    — everything else (incl. Mag7/BTC divergence split with mixed breadth)
    """
    if us_b.get("n", 0) == 0 or cr_b.get("n", 0) == 0:
        return "unknown"

    us_g = us_b.get("green_pct")
    cr_g = cr_b.get("green_pct")
    if us_g is None or cr_g is None:
        return "unknown"

    funding_cold = btc_fund is not None and btc_fund <= _FUNDING_COLD
    funding_hot = btc_fund is not None and btc_fund >= _FUNDING_HOT

    both_green = us_g >= _GREEN_MAJORITY and cr_g >= _GREEN_MAJORITY
    both_red = us_g <= _RED_MAJORITY and cr_g <= _RED_MAJORITY

    if both_red or (funding_cold and us_g <= _RED_MAJORITY):
        return "risk_off"
    if both_green and not funding_cold:
        # Hot funding with green books is still risk_on (crowded long, not risk_off)
        _ = funding_hot  # documented; does not flip to risk_off alone
        return "risk_on"
    # Strong cross-asset split without clear breadth regime → mixed
    if div is not None and abs(div) >= _DIV_NOTE_PCT:
        return "mixed"
    return "mixed"


def build_context(
    books: dict[str, Any] | None,
    session: dict[str, Any] | None,
    derived: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build Rule-2 perception pack. Always returns the schema; never invents news."""
    books = books or {}
    session = session or {}
    derived = derived or {}
    sources: list[str] = []

    us = list(books.get("us") or [])
    crypto = list(books.get("crypto") or [])
    books_ok = bool(books.get("ok"))
    if books_ok:
        sources.append("bitget_public_tickers")
    elif us or crypto:
        sources.append("bitget_public_tickers_stale_or_partial")
    if session.get("us_cash_status"):
        sources.append("session_info.us_cash_session")

    pol = config.active_policy()
    mag7_syms = list(pol.get("mag7_symbols") or config.POLICY["mag7_symbols"])
    btc_sym = pol.get("btc_symbol") or "BTCUSDT"

    mag7 = derived.get("mag7_24h_pct")
    if mag7 is None:
        mag7 = _avg_change(us, mag7_syms)
    btc = derived.get("btc_24h_pct")
    if btc is None:
        btc_row = next((r for r in crypto if r.get("symbol") == btc_sym), None)
        if btc_row and btc_row.get("change24h_pct") is not None:
            btc = float(btc_row["change24h_pct"])
    div = derived.get("divergence_pct")
    if div is None and mag7 is not None and btc is not None:
        div = mag7 - btc

    us_b = _breadth(us)
    cr_b = _breadth(crypto)
    btc_fund = _avg_funding(crypto, [btc_sym])
    mag7_fund = _avg_funding(us, mag7_syms)
    if btc_fund is not None or mag7_fund is not None:
        sources.append("bitget_ticker_funding_rate")

    oi = _try_btc_open_interest()
    if oi:
        sources.append("bitget_public_open_interest:BTCUSDT")

    # Toolkit feeds (Bitget S2 MCP/signal public mirrors) — never invent
    toolkit: dict[str, Any] = {}
    try:
        toolkit = bitget_feeds.fetch_toolkit_pack()
    except Exception as e:
        toolkit = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    news: list[Any] = list(toolkit.get("news") or [])
    if news:
        sources.append("bitget_feeds.coindesk_rss")
    else:
        sources.append("news:none")

    fng = toolkit.get("fear_greed") if isinstance(toolkit.get("fear_greed"), dict) else None
    if fng and fng.get("value") is not None:
        sources.append("bitget_feeds.fear_greed")
    cross = toolkit.get("cross_asset") if isinstance(toolkit.get("cross_asset"), dict) else {}
    mag7_cash = toolkit.get("mag7_cash") if isinstance(toolkit.get("mag7_cash"), dict) else {}
    if cross:
        sources.append("bitget_feeds.yahoo_cross_asset")
    if mag7_cash:
        sources.append("bitget_feeds.yahoo_mag7_cash")

    # CMC Witness (Pro API + optional skills cache) — never invent
    cmc: dict[str, Any] = {}
    try:
        cmc = cmc_feeds.fetch_cmc_pack()
    except Exception as e:
        cmc = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    cmc_brief = ""
    pulse = cmc.get("sentiment_pulse") if isinstance(cmc.get("sentiment_pulse"), dict) else None
    if cmc.get("ok"):
        sources.append("cmc_witness")
        for s in cmc.get("sources") or []:
            if isinstance(s, str) and s not in sources:
                sources.append(s)
        # Prefer CMC Fear&Greed when present (more authoritative than alt.me mirror)
        cfng = cmc.get("fear_greed") if isinstance(cmc.get("fear_greed"), dict) else None
        if cfng and cfng.get("value") is not None:
            fng = {
                "value": cfng.get("value"),
                "label": cfng.get("label"),
                "source": cfng.get("source") or "cmc_fear_greed",
            }
            if pulse and pulse.get("change_7d_points") is not None:
                fng["change_7d_points"] = pulse.get("change_7d_points")
                fng["avg_30d"] = pulse.get("avg_30d")
            if "bitget_feeds.fear_greed" in sources:
                sources = [x for x in sources if x != "bitget_feeds.fear_greed"]
            if "cmc.fear_greed" not in sources:
                sources.append("cmc.fear_greed")
        cmc_brief = _cmc_bits(cmc)
    else:
        sources.append("cmc:unavailable")
    cmc_ctx = cmc_feeds.compact_for_context(cmc)

    geopolitics = {"level": "unknown", "notes": "no geopolitics feed wired"}
    sources.append("geopolitics:none")

    usable = us_b.get("n", 0) > 0 or cr_b.get("n", 0) > 0
    if not usable:
        return {
            "macro": "unknown",
            "sentiment": "unknown",
            "news": news,
            "geopolitics": geopolitics,
            "us_tape": "unknown",
            "sources": sources or ["none"],
            "breadth": {"us": us_b, "crypto": cr_b},
            "funding": {"btc": btc_fund, "mag7_avg": mag7_fund},
            "open_interest": oi,
            "fear_greed": fng,
            "cross_asset": cross,
            "mag7_cash": mag7_cash,
            "toolkit_ok": bool(toolkit.get("ok")),
            "cmc": cmc_ctx,
            "cmc_brief": cmc_brief or "cmc:unavailable",
        }

    macro = _build_macro(session, mag7, btc, div, btc_fund, mag7_fund, us_b, cr_b, oi)
    if cmc_brief:
        macro = f"{macro}; {cmc_brief}"
    us_tape = _build_us_tape(session, us_b, mag7)
    sentiment = _derive_sentiment(us_b, cr_b, btc_fund, div)

    # Fold Fear&Greed into sentiment label when Dual Book already set risk_on/off
    if fng and fng.get("value") is not None and sentiment not in ("unknown",):
        try:
            fv = int(fng["value"])
            fl = fng.get("label") or ""
            if fv <= 25 and sentiment == "risk_on":
                sentiment = "mixed"  # extreme fear overrides soft risk_on
            elif fv >= 75 and sentiment == "risk_off":
                sentiment = "mixed"
            # attach for LLM without inventing a new regime string alone
            _ = fl
        except (TypeError, ValueError):
            pass

    return {
        "macro": macro,
        "sentiment": sentiment,
        "news": news[:5],
        "geopolitics": geopolitics,
        "us_tape": us_tape,
        "sources": sources,
        "breadth": {"us": us_b, "crypto": cr_b},
        "funding": {"btc": btc_fund, "mag7_avg": mag7_fund},
        "open_interest": oi,
        "fear_greed": fng,
        "cross_asset": cross,
        "mag7_cash": mag7_cash,
        "toolkit_ok": bool(toolkit.get("ok")),
        "cmc": cmc_ctx,
        "cmc_brief": cmc_brief or "cmc:unavailable",
        "derived_snapshot": {
            "mag7_24h_pct": mag7,
            "btc_24h_pct": btc,
            "divergence_pct": div,
        },
    }
