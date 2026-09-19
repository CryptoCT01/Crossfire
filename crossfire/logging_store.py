"""Append-only JSONL logs for decisions, ticks, fills, equity."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from . import config

_lock = threading.Lock()


def ensure_logs_dir() -> Path:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return config.LOGS_DIR


def _path(name: str) -> Path:
    ensure_logs_dir()
    return config.LOGS_DIR / name


def append_jsonl(name: str, record: dict[str, Any]) -> None:
    path = _path(name)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def count_jsonl(name: str) -> int:
    """Total non-empty lines in a JSONL log (full history length)."""
    path = _path(name)
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def read_jsonl_tail(name: str, n: int = 50) -> list[dict[str, Any]]:
    path = _path(name)
    if not path.is_file():
        return []
    with _lock:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, n) :]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_jsonl_span(name: str, max_points: int = 800) -> list[dict[str, Any]]:
    """Full-history series, evenly downsampled so charts keep the true start."""
    path = _path(name)
    if not path.is_file():
        return []
    with _lock:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    n = len(rows)
    cap = max(2, int(max_points))
    if n <= cap:
        return rows
    # always keep first + last; fill evenly between
    idxs = [0]
    for i in range(1, cap - 1):
        idxs.append(int(round(i * (n - 1) / (cap - 1))))
    idxs.append(n - 1)
    # de-dupe while preserving order
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for i in idxs:
        if i in seen:
            continue
        seen.add(i)
        out.append(rows[i])
    return out


def read_jsonl_by_id(name: str, tick_id: str) -> dict[str, Any] | None:
    path = _path(name)
    if not path.is_file():
        return None
    with _lock:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(obj.get("tick_id") or obj.get("id") or "") == tick_id:
            return obj
    return None


def now_ms() -> int:
    return int(time.time() * 1000)
