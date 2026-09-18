"""Bitget private mix endpoints — signed REST when BITGET_* keys are present."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any

from . import config
from . import paper_sleeve

PRODUCT = getattr(config, "PRODUCT_TYPE", "USDT-FUTURES") or "USDT-FUTURES"
MARGIN_COIN = "USDT"


def hooks_enabled() -> bool:
    """True when sleeve keys exist — private signing / flatten are live."""
    return config.sleeve_connected()


def capability_matrix() -> dict[str, Any]:
    """Honest bool + message matrix for UI gating (no fake live claims)."""
    if config.paper_mode():
        def cell(ok: bool, msg: str) -> dict[str, Any]:
            return {"ok": bool(ok), "message": msg}

        return {
            "connected": cell(True, "PAPER sleeve — virtual $10k book"),
            "hooks_enabled": cell(True, "Paper fills at live marks (Bitget private unused)"),
            "positions": cell(True, "Paper positions ledger"),
            "orders": cell(True, "Paper market fills — no resting orders"),
            "equity": cell(True, "Paper equity curve (not live Bitget)"),
            "place_order": cell(True, "Paper place_order — never sent to Bitget"),
            "kill": cell(True, "Paper kill flattens virtual book only"),
        }
    connected = config.sleeve_connected()
    hooks = hooks_enabled()

    def cell(ok: bool, msg: str) -> dict[str, Any]:
        return {"ok": bool(ok), "message": msg}

    return {
        "connected": cell(
            connected,
            "BITGET_* keys present" if connected else "SLEEVE DISCONNECTED — set BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE",
        ),
        "hooks_enabled": cell(
            hooks,
            "Private signing / flatten hooks live" if hooks else "Private hooks stub — no signed REST yet",
        ),
        "positions": cell(
            connected and hooks,
            "Live positions hook"
            if (connected and hooks)
            else ("Keys present; positions hook stub" if connected else "SLEEVE DISCONNECTED"),
        ),
        "orders": cell(
            connected and hooks,
            "Live orders hook"
            if (connected and hooks)
            else ("Keys present; orders hook stub" if connected else "SLEEVE DISCONNECTED"),
        ),
        "equity": cell(
            connected and hooks,
            "Live equity snapshot hook"
            if (connected and hooks)
            else ("Keys present; equity hook stub" if connected else "SLEEVE DISCONNECTED — equity curve empty until sleeve connected"),
        ),
        "place_order": cell(
            connected and hooks,
            "Order placement enabled"
            if (connected and hooks)
            else ("Order placement disabled until live sleeve hook is explicitly enabled" if connected else "SLEEVE DISCONNECTED — cannot place orders"),
        ),
        "kill": cell(
            connected and hooks,
            "Kill / flatten armed"
            if (connected and hooks)
            else ("Kill wired but private flatten hook not yet enabled — no fake cancel" if connected else "SLEEVE DISCONNECTED — kill switch requires connected sleeve"),
        ),
    }


def _ssl_context():
    try:
        import certifi
        import ssl

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        import ssl

        return ssl.create_default_context()


def _sign(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    msg = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        config.BITGET_SECRET_KEY.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _request(
    method: str,
    path_with_query: str,
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    if not config.sleeve_connected():
        raise RuntimeError("SLEEVE DISCONNECTED")
    method_u = method.upper()
    body_str = ""
    data: bytes | None = None
    if body is not None:
        body_str = json.dumps(body, separators=(",", ":"))
        data = body_str.encode("utf-8")
    ts = str(int(time.time() * 1000))
    sign = _sign(ts, method_u, path_with_query, body_str)
    url = f"{config.BITGET_BASE}{path_with_query}"
    req = urllib.request.Request(
        url,
        data=data,
        method=method_u,
        headers={
            "ACCESS-KEY": config.BITGET_API_KEY,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": config.BITGET_PASSPHRASE,
            "Content-Type": "application/json",
            "locale": "en-US",
            "User-Agent": "Crossfire/0.3 (+Bitget AI Hackathon S2)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"code": str(e.code), "msg": raw[:300], "data": None}


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def positions() -> dict[str, Any]:
    if config.paper_mode():
        return paper_sleeve.positions()
    if not config.sleeve_connected():
        return {
            "connected": False,
            "hooks_enabled": False,
            "stub": True,
            "positions": [],
            "message": "SLEEVE DISCONNECTED — set BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE in env",
        }
    try:
        payload = _request(
            "GET",
            f"/api/v2/mix/position/all-position?productType={PRODUCT}&marginCoin={MARGIN_COIN}",
        )
    except Exception as e:
        return {
            "connected": True,
            "hooks_enabled": True,
            "stub": False,
            "ok": False,
            "positions": [],
            "error": str(e),
            "message": f"positions fetch failed: {e}",
        }
    if str(payload.get("code")) != "00000":
        return {
            "connected": True,
            "hooks_enabled": True,
            "stub": False,
            "ok": False,
            "positions": [],
            "error": payload.get("msg") or payload,
            "message": f"Bitget positions error: {payload.get('msg')}",
        }
    rows: list[dict[str, Any]] = []
    for raw in payload.get("data") or []:
        size = _f(raw.get("total") or raw.get("available") or raw.get("holdSide"))
        # Bitget v2: total is position size; holdSide long/short
        total = _f(raw.get("total")) or 0.0
        if abs(total) < 1e-12:
            continue
        hold = str(raw.get("holdSide") or "").lower()
        side = "long" if hold in ("long", "buy") else "short" if hold in ("short", "sell") else hold
        rows.append(
            {
                "symbol": raw.get("symbol"),
                "side": side,
                "size": total,
                "available": _f(raw.get("available")),
                "entry": _f(raw.get("openPriceAvg") or raw.get("averageOpenPrice")),
                "mark": _f(raw.get("markPrice")),
                "unrealized_pnl": _f(raw.get("unrealizedPL") or raw.get("upl")),
                "leverage": _f(raw.get("leverage")),
                "margin_mode": raw.get("marginMode"),
                "pos_mode": raw.get("posMode"),
                "margin_coin": raw.get("marginCoin"),
            }
        )
    return {
        "connected": True,
        "hooks_enabled": True,
        "stub": False,
        "ok": True,
        "positions": rows,
        "message": None if rows else "No open USDT-M positions",
    }


def open_orders() -> dict[str, Any]:
    if config.paper_mode():
        return paper_sleeve.orders()
    if not config.sleeve_connected():
        return {
            "connected": False,
            "hooks_enabled": False,
            "stub": True,
            "orders": [],
            "message": "SLEEVE DISCONNECTED",
        }
    try:
        payload = _request(
            "GET",
            f"/api/v2/mix/order/orders-pending?productType={PRODUCT}",
        )
    except Exception as e:
        return {
            "connected": True,
            "hooks_enabled": True,
            "stub": False,
            "ok": False,
            "orders": [],
            "error": str(e),
            "message": f"orders fetch failed: {e}",
        }
    if str(payload.get("code")) != "00000":
        return {
            "connected": True,
            "hooks_enabled": True,
            "stub": False,
            "ok": False,
            "orders": [],
            "error": payload.get("msg") or payload,
            "message": f"Bitget orders error: {payload.get('msg')}",
        }
    data = payload.get("data") or {}
    # v2 may return {entrustedList: [...]} or a list
    raw_list = data if isinstance(data, list) else (data.get("entrustedList") or data.get("orderList") or [])
    orders: list[dict[str, Any]] = []
    for raw in raw_list or []:
        orders.append(
            {
                "order_id": raw.get("orderId") or raw.get("order_id"),
                "client_oid": raw.get("clientOid"),
                "symbol": raw.get("symbol"),
                "side": raw.get("side"),
                "size": _f(raw.get("size") or raw.get("baseVolume")),
                "price": _f(raw.get("price")),
                "order_type": raw.get("orderType"),
                "status": raw.get("status") or raw.get("state"),
                "reduce_only": raw.get("reduceOnly"),
            }
        )
    return {
        "connected": True,
        "hooks_enabled": True,
        "stub": False,
        "ok": True,
        "orders": orders,
        "message": None if orders else "No pending orders",
    }


def account_equity() -> dict[str, Any]:
    if config.paper_mode():
        return paper_sleeve.account_equity()
    if not config.sleeve_connected():
        return {
            "connected": False,
            "hooks_enabled": False,
            "stub": True,
            "equity": None,
            "message": "SLEEVE DISCONNECTED — equity curve empty until sleeve connected",
        }
    try:
        payload = _request(
            "GET",
            f"/api/v2/mix/account/accounts?productType={PRODUCT}",
        )
    except Exception as e:
        return {
            "connected": True,
            "hooks_enabled": True,
            "stub": False,
            "ok": False,
            "equity": None,
            "error": str(e),
            "message": f"equity fetch failed: {e}",
        }
    if str(payload.get("code")) != "00000":
        return {
            "connected": True,
            "hooks_enabled": True,
            "stub": False,
            "ok": False,
            "equity": None,
            "error": payload.get("msg") or payload,
            "message": f"Bitget account error: {payload.get('msg')}",
        }
    acct = next(
        (x for x in (payload.get("data") or []) if str(x.get("marginCoin") or "").upper() == MARGIN_COIN),
        None,
    )
    if not acct and (payload.get("data") or []):
        acct = (payload.get("data") or [None])[0]
    if not acct:
        return {
            "connected": True,
            "hooks_enabled": True,
            "stub": False,
            "ok": False,
            "equity": None,
            "message": "No USDT futures account row returned",
        }
    equity = _f(acct.get("accountEquity") or acct.get("usdtEquity"))
    available = _f(acct.get("unionAvailable") or acct.get("crossedMaxAvailable") or acct.get("available"))
    return {
        "connected": True,
        "hooks_enabled": True,
        "stub": False,
        "ok": True,
        "equity": equity,
        "available": available,
        "wallet_available": _f(acct.get("available")),
        "locked": _f(acct.get("locked")),
        "crossed_margin": _f(acct.get("crossedMargin")),
        "unrealized_pnl": _f(acct.get("unrealizedPL") or acct.get("upl")),
        "message": None,
    }


def place_order(payload: dict[str, Any]) -> dict[str, Any]:
    """Place market legs — one-way buy/sell, crossed margin, pre-sized only.

    Refuses legs without symbol/side/size. Does not invent Mag7 basket sprays.
    PAPER: virtual fills only — never Bitget private.
    """
    if config.paper_mode():
        return paper_sleeve.place_order(payload)
    if not config.sleeve_connected():
        return {
            "ok": False,
            "hooks_enabled": False,
            "stub": True,
            "error": "SLEEVE DISCONNECTED — cannot place orders",
        }
    if not hooks_enabled():
        return {
            "ok": False,
            "error": "Order placement disabled until live sleeve hook is explicitly enabled",
            "stub": True,
            "hooks_enabled": False,
        }

    legs = list(payload.get("legs") or [])
    if not legs:
        return {"ok": False, "hooks_enabled": True, "stub": False, "error": "no legs in payload", "fills": []}

    results: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    for leg in legs:
        sym = str(leg.get("symbol") or "").upper()
        if sym and not sym.endswith("USDT"):
            sym = sym + "USDT"
        raw_side = str(leg.get("side") or "").lower()
        if raw_side in ("long", "open_long", "buy"):
            side = "buy"
        elif raw_side in ("short", "open_short", "sell"):
            side = "sell"
        else:
            results.append({"ok": False, "symbol": sym or None, "error": f"bad side {raw_side!r} — need buy/sell"})
            continue
        size = leg.get("size") or leg.get("qty") or leg.get("amount")
        if size is None or not sym:
            results.append({"ok": False, "symbol": sym or None, "error": "missing symbol or size — run sizing first"})
            continue
        try:
            size_f = float(size)
        except (TypeError, ValueError):
            results.append({"ok": False, "symbol": sym, "error": f"bad size {size!r}"})
            continue
        if size_f <= 0:
            results.append({"ok": False, "symbol": sym, "error": "size must be > 0"})
            continue

        lev_i = None
        if leg.get("leverage") is not None:
            try:
                lev_i = int(float(leg.get("leverage")))
            except (TypeError, ValueError):
                lev_i = None
        if lev_i:
            try:
                _request(
                    "POST",
                    "/api/v2/mix/account/set-leverage",
                    body={
                        "symbol": sym,
                        "productType": PRODUCT,
                        "marginCoin": MARGIN_COIN,
                        "leverage": str(lev_i),
                    },
                )
            except Exception:
                pass

        body = {
            "symbol": sym,
            "productType": PRODUCT,
            "marginMode": str(leg.get("marginMode") or "crossed"),
            "marginCoin": MARGIN_COIN,
            "size": str(size_f),
            "side": side,
            "orderType": str(leg.get("orderType") or "market"),
            "clientOid": str(leg.get("clientOid") or f"cf{int(time.time()*1000)}")[:32],
        }
        if leg.get("reduceOnly") is True or str(leg.get("reduceOnly") or "").upper() in ("YES", "TRUE", "1"):
            body["reduceOnly"] = "YES"
        try:
            resp = _request("POST", "/api/v2/mix/order/place-order", body=body)
        except Exception as e:
            results.append({"ok": False, "symbol": sym, "error": str(e)})
            continue
        ok = str(resp.get("code")) == "00000"
        entry = {
            "ok": ok,
            "symbol": sym,
            "side": side,
            "size": size_f,
            "notional_usdt": leg.get("notional_usdt"),
            "leverage": lev_i,
            "code": resp.get("code"),
            "msg": resp.get("msg"),
            "data": resp.get("data"),
        }
        results.append(entry)
        if ok:
            data = resp.get("data") or {}
            fills.append(
                {
                    "symbol": sym,
                    "side": side,
                    "size": size_f,
                    "notional_usdt": leg.get("notional_usdt"),
                    "order_id": data.get("orderId") or data.get("order_id"),
                    "client_oid": data.get("clientOid") or body["clientOid"],
                    "source": "place_order",
                }
            )

    any_ok = any(r.get("ok") for r in results)
    return {
        "ok": any_ok,
        "hooks_enabled": True,
        "stub": False,
        "results": results,
        "fills": fills,
        "message": "orders submitted" if any_ok else "no legs filled",
        "error": None if any_ok else "all legs failed",
    }


def _cancel_pending() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending = open_orders()
    for od in pending.get("orders") or []:
        oid = od.get("order_id")
        sym = od.get("symbol")
        if not oid or not sym:
            continue
        body = {
            "orderId": str(oid),
            "symbol": str(sym),
            "productType": PRODUCT,
            "marginCoin": MARGIN_COIN,
        }
        try:
            resp = _request("POST", "/api/v2/mix/order/cancel-order", body=body)
            out.append({"order_id": oid, "symbol": sym, "code": resp.get("code"), "msg": resp.get("msg")})
        except Exception as e:
            out.append({"order_id": oid, "symbol": sym, "error": str(e)})
    return out


def _flatten_positions() -> list[dict[str, Any]]:
    """Market-close all open USDT-M positions (one-way: sell closes long, buy closes short)."""
    out: list[dict[str, Any]] = []
    pos = positions()
    for p in pos.get("positions") or []:
        sym = str(p.get("symbol") or "")
        size = p.get("size")
        side = str(p.get("side") or "").lower()
        if not sym or size is None or abs(float(size)) < 1e-12:
            continue
        close_side = "sell" if side == "long" else "buy"
        body = {
            "symbol": sym,
            "productType": PRODUCT,
            "marginMode": str((p.get("margin_mode") or "crossed")),
            "marginCoin": MARGIN_COIN,
            "size": str(size),
            "side": close_side,
            "orderType": "market",
            "reduceOnly": "YES",
            "clientOid": f"cfkill{int(time.time()*1000)}"[:32],
        }
        try:
            resp = _request("POST", "/api/v2/mix/order/place-order", body=body)
            out.append(
                {
                    "symbol": sym,
                    "close_side": close_side,
                    "size": size,
                    "code": resp.get("code"),
                    "msg": resp.get("msg"),
                    "data": resp.get("data"),
                    "ok": str(resp.get("code")) == "00000",
                }
            )
        except Exception as e:
            out.append({"symbol": sym, "error": str(e), "ok": False})
    return out


def kill_all() -> dict[str, Any]:
    if config.paper_mode():
        return paper_sleeve.flatten_all()
    if not config.sleeve_connected():
        return {
            "ok": False,
            "status": 409,
            "hooks_enabled": False,
            "stub": True,
            "error": "SLEEVE DISCONNECTED — kill switch requires connected sleeve",
        }
    if not hooks_enabled():
        return {
            "ok": False,
            "status": 409,
            "error": "Kill switch wired but private flatten hook not yet enabled — no fake cancel",
            "stub": True,
            "hooks_enabled": False,
        }
    cancelled = _cancel_pending()
    flattened = _flatten_positions()
    ok = True
    if flattened and not any(x.get("ok") for x in flattened):
        ok = False
    return {
        "ok": ok,
        "status": 200 if ok else 409,
        "hooks_enabled": True,
        "stub": False,
        "cancelled_orders": cancelled,
        "flattened": flattened,
        "message": "Kill executed — cancelled pending + flatten market closes"
        if ok
        else "Kill partially failed — check flattened/cancelled",
    }
