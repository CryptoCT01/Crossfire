#!/usr/bin/env python3
"""Start Crossfire server in its own session so Cursor/agent shells cannot kill it."""
from __future__ import annotations
import os, signal, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path('/Users/cryptot/Desktop/s2-crossfire')
LOG = ROOT / 'logs' / 'server.daemon.log'
PIDF = ROOT / 'logs' / 'server.daemon.pid'
PYBIN = '/opt/homebrew/bin/python3'
URL = 'http://127.0.0.1:8780/api/health'

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def stop() -> None:
    if PIDF.exists():
        try:
            pid = int(PIDF.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.8)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        PIDF.unlink(missing_ok=True)
    # belt: free port
    try:
        out = subprocess.check_output(['lsof', '-nP', '-iTCP:8780', '-sTCP:LISTEN', '-t'], text=True)
        for line in out.split():
            try: os.kill(int(line), 9)
            except Exception: pass
    except Exception:
        pass

def health_ok() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False

def start() -> int:
    if health_ok():
        print('already up')
        return 0
    stop()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'ab') as f:
        f.write(b'\n--- daemonize start ---\n')
        f.flush()
        p = subprocess.Popen(
            [PYBIN, str(ROOT / 'server.py')],
            cwd=str(ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, 'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'HOME': str(Path.home())},
        )
    PIDF.write_text(str(p.pid))
    for _ in range(20):
        if health_ok():
            print(f'up pid={p.pid}')
            return 0
        if p.poll() is not None:
            print(f'exited early code={p.returncode}', file=sys.stderr)
            return 1
        time.sleep(0.25)
    print('started but health not ready', file=sys.stderr)
    return 1

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'start'
    if cmd == 'stop':
        stop(); print('stopped'); raise SystemExit(0)
    if cmd == 'status':
        print('health', health_ok(), 'pid', PIDF.read_text().strip() if PIDF.exists() else None)
        raise SystemExit(0 if health_ok() else 1)
    raise SystemExit(start())
