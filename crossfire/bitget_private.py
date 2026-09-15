"""Bitget private mix endpoints — stub hooks; skipped when keys missing."""
from __future__ import annotations

from typing import Any

from . import config


def hooks_enabled() -> bool:
    """True only when live private signing / flatten hooks are wired.

    Currently a stub — always False. Separate sleeve task will flip this.
    """
    return False


def capability_matrix() -> dict[str, Any]:
    """Honest bool + message matrix for UI gating (no fake live claims)."""
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


def positions() -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "connected": False,
            "hooks_enabled": False,
            "stub": True,
            "positions": [],
            "message": "SLEEVE DISCONNECTED — set BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE in env",
        }
    # Stub: private signing not enabled in this build until user wires live keys.
    return {
        "connected": True,
        "hooks_enabled": False,
        "positions": [],
        "message": "Sleeve keys present; private position fetch hook not yet enabled — no simulated positions",
        "stub": True,
    }


def open_orders() -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "connected": False,
            "hooks_enabled": False,
            "stub": True,
            "orders": [],
            "message": "SLEEVE DISCONNECTED",
        }
    return {
        "connected": True,
        "hooks_enabled": False,
        "orders": [],
        "message": "Sleeve keys present; private orders hook not yet enabled",
        "stub": True,
    }


def account_equity() -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "connected": False,
            "hooks_enabled": False,
            "stub": True,
            "equity": None,
            "message": "SLEEVE DISCONNECTED — equity curve empty until sleeve connected",
        }
    return {
        "connected": True,
        "hooks_enabled": False,
        "equity": None,
        "message": "Sleeve keys present; equity snapshot hook not yet enabled",
        "stub": True,
    }


def place_order(_payload: dict[str, Any]) -> dict[str, Any]:
    if not config.sleeve_connected():
        return {
            "ok": False,
            "hooks_enabled": False,
            "stub": True,
            "error": "SLEEVE DISCONNECTED — cannot place orders",
        }
    return {
        "ok": False,
        "error": "Order placement disabled until live sleeve hook is explicitly enabled",
        "stub": True,
        "hooks_enabled": False,
    }


def kill_all() -> dict[str, Any]:
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
    # Live path reserved for when hooks_enabled() flips True
    return {
        "ok": False,
        "status": 409,
        "error": "Kill flatten not implemented",
        "stub": True,
        "hooks_enabled": True,
    }
