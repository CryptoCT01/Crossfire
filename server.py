#!/usr/bin/env python3
"""Crossfire HTTP server — real Bitget public marks + agent heartbeat + dashboard."""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from crossfire import config
from crossfire import cmc_feeds  # noqa: E402

config.load_dotenv_if_present()
# reload bindings after dotenv
from crossfire import agent_engine, bitget_private, bitget_public, paper_sleeve  # noqa: E402
from crossfire import logging_store as store  # noqa: E402
from crossfire import session_info  # noqa: E402

DASHBOARD = ROOT / "dashboard.html"


def _json(handler: BaseHTTPRequestHandler, code: int, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


class Handler(BaseHTTPRequestHandler):
    server_version = "Crossfire/0.2"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", "/dashboard", "/dashboard.html"):
                self._serve_dashboard()
                return
            if path == "/api/health":
                st = agent_engine.get_state()
                paper = config.paper_mode()
                sleeve = True if paper else config.sleeve_connected()
                hooks = True if paper else bitget_private.hooks_enabled()
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "crossfire",
                        "version": "0.3.0",
                        "mode": config.MODE,
                        "mode_pill": config.mode_pill(),
                        "agent_mode": config.get_agent_mode(),
                        "agent_profile": config.active_profile(),
                        "risk": config.active_risk(),
                        "sleeve_connected": sleeve,
                        "llm_available": config.llm_available(),
                        "agent_enabled": config.agent_enabled(),
                        "paper": paper,
                        "private_hooks_enabled": hooks,
                        "kill_armed": bool(paper or (sleeve and hooks)),
                        "capabilities": bitget_private.capability_matrix(),
                        "heartbeat_sec": config.HEARTBEAT_SEC,
                        "event_move_pct": config.EVENT_MOVE_PCT,
                        "last_heartbeat_ts": st.get("last_heartbeat_ts"),
                        "next_heartbeat_ts": st.get("next_heartbeat_ts"),
                        "port": config.PORT,
                        "ts": time.time(),
                    },
                )
                return
            if path == "/api/books":
                _json(self, 200, bitget_public.get_books())
                return
            if path == "/api/cmc":
                pack = cmc_feeds.fetch_cmc_pack()
                _json(self, 200 if pack.get("ok") else 503, pack)
                return
            if path == "/api/session":
                st = agent_engine.get_state()
                sess = session_info.us_cash_session()
                sess.update(
                    {
                        "last_heartbeat_ts": st.get("last_heartbeat_ts"),
                        "next_heartbeat_ts": st.get("next_heartbeat_ts"),
                        "heartbeat_sec": config.HEARTBEAT_SEC,
                        "mode_pill": config.mode_pill(),
                        "sleeve_connected": config.sleeve_connected(),
                    }
                )
                _json(self, 200, sess)
                return
            if path == "/api/agent/state":
                st = agent_engine.get_state()
                sleeve = config.sleeve_connected()
                hooks = bitget_private.hooks_enabled()
                st["sleeve_connected"] = sleeve
                st["llm_available"] = config.llm_available()
                st["private_hooks_enabled"] = hooks
                st["kill_armed"] = bool(sleeve and hooks)
                st["capabilities"] = bitget_private.capability_matrix()
                _json(self, 200, st)
                return
            if path == "/api/agent/cinema":
                n = 40
                qs = urlparse(self.path).query
                for part in qs.split("&"):
                    if part.startswith("n="):
                        try:
                            n = max(1, min(200, int(part[2:])))
                        except ValueError:
                            pass
                ticks = store.read_jsonl_tail("ticks.jsonl", n)
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "count": len(ticks),
                        "ticks": ticks,
                        "empty_message": "Waiting for first heartbeat…" if not ticks else None,
                    },
                )
                return
            if path == "/api/blotter":
                pos = bitget_private.positions()
                orders = bitget_private.open_orders()
                fills_total = store.count_jsonl("fills.jsonl")
                fills = store.read_jsonl_tail("fills.jsonl", 2000)
                paper = config.paper_mode()
                sleeve = True if paper else config.sleeve_connected()
                hooks = True if paper else bitget_private.hooks_enabled()
                stub = False if paper else bool(pos.get("stub") or orders.get("stub") or (sleeve and not hooks))
                _json(
                    self,
                    200,
                    {
                        "sleeve_connected": sleeve,
                        "hooks_enabled": hooks,
                        "stub": stub,
                        "paper": paper,
                        "positions": pos.get("positions") or [],
                        "orders": orders.get("orders") or [],
                        "fills": fills,
                        "fills_total": fills_total,
                        "fills_returned": len(fills),
                        "message": pos.get("message"),
                        "disconnected": not sleeve,
                    },
                )
                return
            if path == "/api/equity":
                paper = config.paper_mode()
                eq_log = "paper_equity.jsonl" if paper else "equity.jsonl"
                if paper:
                    paper_sleeve.ensure_curve_seed()
                    live = paper_sleeve.account_equity()
                    series = store.read_jsonl_tail(eq_log, 500)
                    _json(
                        self,
                        200,
                        {
                            "ok": True,
                            "paper": True,
                            "connected": True,
                            "hooks_enabled": True,
                            "stub": False,
                            "live": {
                                "equity": live.get("equity"),
                                "available": live.get("available"),
                                "paper": True,
                            },
                            "series": series,
                            "message": live.get("message") or "PAPER sleeve — not live Bitget",
                        },
                    )
                    return
                series = store.read_jsonl_tail(eq_log, 500)
                sleeve = config.sleeve_connected()
                hooks = bitget_private.hooks_enabled()
                if not sleeve:
                    _json(
                        self,
                        200,
                        {
                            "ok": True,
                            "connected": False,
                            "hooks_enabled": False,
                            "stub": True,
                            "series": [],
                            "message": ("PAPER sleeve — seeding virtual equity" if config.paper_mode() else "Equity curve empty — sleeve DISCONNECTED (connect BITGET_* keys to start snapshots)"),
                        },
                    )
                    return
                live = None
                if hooks:
                    live = bitget_private.account_equity()
                    eqv = live.get("equity")
                    if eqv is not None:
                        # Throttle snapshots: append if empty or last point is >25s old / moved
                        should = True
                        if series:
                            last = series[-1]
                            age = time.time() - float(last.get("ts") or 0)
                            try:
                                moved = abs(float(last.get("equity") or 0) - float(eqv)) > 1e-6
                            except (TypeError, ValueError):
                                moved = True
                            should = age >= 25.0 or moved
                        if should:
                            store.append_jsonl(
                                eq_log,
                                {
                                    "ts": time.time(),
                                    "tick_id": "poll",
                                    "equity": eqv,
                                    "available": live.get("available"),
                                },
                            )
                            series = store.read_jsonl_tail("equity.jsonl", 500)
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "connected": True,
                        "hooks_enabled": hooks,
                        "stub": not hooks,
                        "live": {
                            "equity": (live or {}).get("equity"),
                            "available": (live or {}).get("available"),
                        }
                        if hooks
                        else None,
                        "series": series if hooks else [],
                        "message": None
                        if (hooks and series)
                        else (
                            "Sleeve keys present; equity hook stub — no snapshots"
                            if not hooks
                            else "Sleeve connected but no equity snapshots yet"
                        ),
                    },
                )
                return
            if path.startswith("/api/explain/"):
                tick_id = path[len("/api/explain/") :].strip()
                if not tick_id:
                    _json(self, 400, {"ok": False, "error": "missing tickId"})
                    return
                tick = store.read_jsonl_by_id("ticks.jsonl", tick_id)
                decision = store.read_jsonl_by_id("decisions.jsonl", tick_id)
                if not tick and not decision:
                    _json(self, 404, {"ok": False, "error": "tick not found", "tick_id": tick_id})
                    return
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "tick_id": tick_id,
                        "tick": tick,
                        "decision": decision,
                    },
                )
                return
            if path == "/api/risk":
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "agent_mode": config.get_agent_mode(),
                        "agent_profile": config.active_profile(),
                        "risk": config.active_risk(),
                        "sleeve_connected": config.sleeve_connected(),
                        "policy": config.active_policy(),
                    },
                )
                return
            if path == "/api/candles":
                qs = parse_qs(urlparse(self.path).query)
                sym = (qs.get("symbol") or [""])[0]
                gran = (qs.get("granularity") or qs.get("tf") or ["15m"])[0]
                lim_raw = (qs.get("limit") or ["100"])[0]
                try:
                    lim = int(lim_raw)
                except ValueError:
                    lim = 100
                # Only allow known universe symbols (public, but avoid abuse)
                allowed = set(config.ALL_SYMBOLS)
                sym_u = str(sym).strip().upper()
                if sym_u and not sym_u.endswith("USDT"):
                    sym_u = sym_u + "USDT"
                if sym_u not in allowed:
                    _json(
                        self,
                        400,
                        {
                            "ok": False,
                            "error": "symbol not in Crossfire universe",
                            "symbol": sym_u or None,
                            "candles": [],
                        },
                    )
                    return
                _json(self, 200, bitget_public.fetch_candles(sym_u, gran, lim))
                return
            if path == "/api/fills":
                qs = parse_qs(urlparse(self.path).query)
                try:
                    n = max(1, min(5000, int((qs.get("n") or ["2000"])[0])))
                except ValueError:
                    n = 2000
                fills_total = store.count_jsonl("fills.jsonl")
                fills = store.read_jsonl_tail("fills.jsonl", n)
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "count": len(fills),
                        "total": fills_total,
                        "fills": fills,
                        "sleeve_connected": config.sleeve_connected(),
                        "message": None
                        if fills
                        else (
                            "No fills in logs/fills.jsonl — empty until sleeve executes"
                            if config.sleeve_connected()
                            else "No fills — sleeve DISCONNECTED (not simulated)"
                        ),
                    },
                )
                return
            _json(self, 404, {"ok": False, "error": "not found", "path": path})
        except Exception as e:
            traceback.print_exc()
            _json(self, 500, {"ok": False, "error": str(e)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/kill":
                body = _read_json(self)
                if not body.get("confirm"):
                    _json(
                        self,
                        400,
                        {
                            "ok": False,
                            "error": "confirmation required — POST {\"confirm\": true}",
                        },
                    )
                    return
                if not config.sleeve_connected():
                    _json(
                        self,
                        409,
                        {
                            "ok": False,
                            "hooks_enabled": False,
                            "stub": True,
                            "error": "SLEEVE DISCONNECTED — kill switch disabled until BITGET_* keys are set",
                        },
                    )
                    return
                if not bitget_private.hooks_enabled():
                    _json(
                        self,
                        409,
                        {
                            "ok": False,
                            "hooks_enabled": False,
                            "stub": True,
                            "error": "Kill switch wired but private flatten hook not yet enabled — no fake cancel",
                        },
                    )
                    return
                result = bitget_private.kill_all()
                code = int(result.get("status") or (200 if result.get("ok") else 409))
                _json(self, code, result)
                return
            if path == "/api/agent/tick":
                if not config.agent_enabled():
                    _json(
                        self,
                        409,
                        {
                            "ok": False,
                            "error": "AGENT_OFF — turn agent on first",
                            "agent_enabled": False,
                        },
                    )
                    return
                result = agent_engine.run_tick(trigger="force_api")
                if result.get("ok") and result.get("tick"):
                    agent_engine.schedule_next(result["tick"]["ts"])
                _json(self, 200 if result.get("ok") else 409, result)
                return

            if path == "/api/agent/power":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    body = {}
                if "enabled" in body:
                    on = bool(body.get("enabled"))
                elif "on" in body:
                    on = bool(body.get("on"))
                else:
                    on = str(body.get("power") or body.get("state") or "").lower() in (
                        "on", "1", "true", "enable", "enabled",
                    )
                val = config.set_agent_enabled(on)
                if not val:
                    try:
                        agent_engine.schedule_next(time.time() + 3650 * 24 * 3600)
                    except Exception:
                        pass
                st = agent_engine.get_state()
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "agent_enabled": val,
                        "message": (
                            "AGENT ON — heartbeats + event wakes armed"
                            if val
                            else "AGENT OFF — no auto ticks, no LLM spend"
                        ),
                        "next_heartbeat_ts": st.get("next_heartbeat_ts"),
                    },
                )
                return

            if path == "/api/agent/mode":
                body = _read_json(self)
                mode = body.get("mode")
                try:
                    prof = config.set_agent_mode(str(mode or ""))
                except ValueError as e:
                    _json(
                        self,
                        400,
                        {
                            "ok": False,
                            "error": str(e),
                            "allowed": ["normal", "aggressive"],
                        },
                    )
                    return
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "agent_mode": config.get_agent_mode(),
                        "agent_profile": prof,
                        "risk": config.active_risk(),
                        "policy": config.active_policy(),
                        "heartbeat_sec": config.HEARTBEAT_SEC,
                        "message": (
                            f"Agent mode → {prof['label']}: slots={prof['max_slots']}, "
                            f"div±{prof['divergence_threshold_pct']}%, "
                            f"dd halt {prof['daily_dd_halt_pct']}% · heartbeat still "
                            f"{config.HEARTBEAT_SEC}s"
                        ),
                    },
                )
                return
            _json(self, 404, {"ok": False, "error": "not found", "path": path})
        except Exception as e:
            traceback.print_exc()
            _json(self, 500, {"ok": False, "error": str(e)})

    def _serve_dashboard(self) -> None:
        if not DASHBOARD.is_file():
            _json(self, 500, {"ok": False, "error": "dashboard.html missing"})
            return
        data = DASHBOARD.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class HeartbeatThread(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="crossfire-heartbeat")
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # Initial schedule only — no startup tick if agent is off
        agent_engine.schedule_next(time.time())
        if config.agent_enabled():
            try:
                r = agent_engine.run_tick(trigger="startup")
                if r.get("ok") and r.get("tick"):
                    agent_engine.schedule_next(r["tick"]["ts"])
            except Exception:
                traceback.print_exc()
        else:
            # Park far ahead so UI countdown isn't fake-urgent while off
            agent_engine.schedule_next(time.time() + 3650 * 24 * 3600)

        while not self._stop.is_set():
            if not config.agent_enabled():
                self._stop.wait(5.0)
                continue

            st = agent_engine.get_state()
            now = time.time()
            nxt = float(st.get("next_heartbeat_ts") or (now + config.HEARTBEAT_SEC))

            # Event wake check between heartbeats (poll marks lightly)
            try:
                books = bitget_public.get_books(force=False)
                marks = bitget_public.marks_map(books)
                woke = agent_engine.maybe_event_wake(marks)
                if woke and woke.get("ok") and woke.get("tick"):
                    agent_engine.schedule_next(woke["tick"]["ts"])
            except Exception:
                traceback.print_exc()

            st = agent_engine.get_state()
            nxt = float(st.get("next_heartbeat_ts") or (now + config.HEARTBEAT_SEC))
            now = time.time()
            if now >= nxt:
                try:
                    r = agent_engine.run_tick(trigger="heartbeat")
                    if r.get("ok") and r.get("tick"):
                        agent_engine.schedule_next(r["tick"]["ts"])
                    else:
                        agent_engine.schedule_next(now)
                except Exception:
                    traceback.print_exc()
                    agent_engine.schedule_next(now)

            self._stop.wait(5.0)


def main() -> None:
    store.ensure_logs_dir()
    host = config.HOST
    port = config.PORT
    httpd = ThreadingHTTPServer((host, port), Handler)
    hb = HeartbeatThread()
    hb.start()
    print(
        json.dumps(
            {
                "event": "crossfire_start",
                "url": f"http://{host}:{port}/",
                "mode": config.MODE,
                "mode_pill": config.mode_pill(),
                "sleeve_connected": config.sleeve_connected(),
                "llm_available": config.llm_available(),
                "agent_mode": config.get_agent_mode(),
                "heartbeat_sec": config.HEARTBEAT_SEC,
            }
        ),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…", flush=True)
    finally:
        hb.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
