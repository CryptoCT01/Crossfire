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
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from crossfire import config  # noqa: E402

config.load_dotenv_if_present()
# reload bindings after dotenv
from crossfire import agent_engine, bitget_private, bitget_public  # noqa: E402
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
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "crossfire",
                        "version": "0.2.0",
                        "mode": config.MODE,
                        "mode_pill": config.mode_pill(),
                        "sleeve_connected": config.sleeve_connected(),
                        "llm_available": config.llm_available(),
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
                _json(self, 200, agent_engine.get_state())
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
                fills = store.read_jsonl_tail("fills.jsonl", 100)
                _json(
                    self,
                    200,
                    {
                        "sleeve_connected": config.sleeve_connected(),
                        "positions": pos.get("positions") or [],
                        "orders": orders.get("orders") or [],
                        "fills": fills,
                        "message": pos.get("message"),
                        "disconnected": not config.sleeve_connected(),
                    },
                )
                return
            if path == "/api/equity":
                series = store.read_jsonl_tail("equity.jsonl", 500)
                if not config.sleeve_connected():
                    _json(
                        self,
                        200,
                        {
                            "ok": True,
                            "connected": False,
                            "series": [],
                            "message": "Equity curve empty — sleeve DISCONNECTED (connect BITGET_* keys to start snapshots)",
                        },
                    )
                    return
                _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "connected": True,
                        "series": series,
                        "message": None
                        if series
                        else "Sleeve connected but no equity snapshots yet (private hook stub or no data)",
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
                        "risk": dict(config.RISK),
                        "sleeve_connected": config.sleeve_connected(),
                        "policy": {
                            "divergence_threshold_pct": config.POLICY["divergence_threshold_pct"],
                            "mag7_symbols": config.POLICY["mag7_symbols"],
                            "btc_symbol": config.POLICY["btc_symbol"],
                        },
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
                            "error": "SLEEVE DISCONNECTED — kill switch disabled until BITGET_* keys are set",
                        },
                    )
                    return
                result = bitget_private.kill_all()
                code = int(result.get("status") or (200 if result.get("ok") else 409))
                _json(self, code, result)
                return
            if path == "/api/agent/tick":
                result = agent_engine.run_tick(trigger="force_api")
                # ensure next heartbeat scheduled from this tick
                if result.get("ok") and result.get("tick"):
                    agent_engine.schedule_next(result["tick"]["ts"])
                _json(self, 200 if result.get("ok") else 409, result)
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
        # Initial schedule
        agent_engine.schedule_next(time.time())
        # Optional: immediate first tick so cinema isn't empty on cold start
        try:
            r = agent_engine.run_tick(trigger="startup")
            if r.get("ok") and r.get("tick"):
                agent_engine.schedule_next(r["tick"]["ts"])
        except Exception:
            traceback.print_exc()

        while not self._stop.is_set():
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
