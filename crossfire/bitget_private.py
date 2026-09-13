"""Bitget private mix endpoints — stub hooks; skipped when keys missing."""
from __future__ import annotations

from typing import Any

from . import config


def positions() -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "connected": False,
            "positions": [],
            "message": "SLEEVE DISCONNECTED — set BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE in env",
        }
    # Stub: private signing not enabled in this build until user wires live keys.
    return {
        "connected": True,
        "positions": [],
        "message": "Sleeve keys present; private position fetch hook not yet enabled — no simulated positions",
        "stub": True,
    }


def open_orders() -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "connected": False,
            "orders": [],
            "message": "SLEEVE DISCONNECTED",
        }
    return {
        "connected": True,
        "orders": [],
        "message": "Sleeve keys present; private orders hook not yet enabled",
        "stub": True,
    }


def account_equity() -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "connected": False,
            "equity": None,
            "message": "SLEEVE DISCONNECTED — equity curve empty until sleeve connected",
        }
    return {
        "connected": True,
        "equity": None,
        "message": "Sleeve keys present; equity snapshot hook not yet enabled",
        "stub": True,
    }


def place_order(_payload: dict[str, Any]) -> dict[str, Any]:
    if not config.sleeve_connected():
        return {"ok": False, "error": "SLEEVE DISCONNECTED — cannot place orders"}
    return {
        "ok": False,
        "error": "Order placement disabled until live sleeve hook is explicitly enabled",
        "stub": True,
    }


def kill_all() -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "ok": False,
            "status": 409,
            "error": "SLEEVE DISCONNECTED — kill switch requires connected sleeve",
        }
    return {
        "ok": False,
        "status": 409,
        "error": "Kill switch wired but private flatten hook not yet enabled — no fake cancel",
        "stub": True,
    }
