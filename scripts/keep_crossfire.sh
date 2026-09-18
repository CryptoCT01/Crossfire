#!/bin/bash
# Keep Crossfire dashboard (:8780) alive across shell/session kills.
ROOT="/Users/cryptot/Desktop/s2-crossfire"
cd "$ROOT" || exit 1
mkdir -p logs
PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi
LOG="$ROOT/logs/server.log"
WLOG="$ROOT/logs/watchdog.log"

while true; do
  # Free the port if a zombie holds it
  if lsof -nP -iTCP:8780 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] port 8780 busy — killing listeners" >> "$WLOG"
    PIDS=$(lsof -nP -iTCP:8780 -sTCP:LISTEN -t 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then kill -9 $PIDS 2>/dev/null || true; fi
    sleep 1
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting $PY server.py" >> "$WLOG"
  "$PY" server.py >> "$LOG" 2>&1
  code=$?
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] server exited code=$code — restart in 2s" >> "$WLOG"
  sleep 2
done
