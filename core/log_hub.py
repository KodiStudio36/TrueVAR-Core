"""Shared, thread-safe ring buffer of dashboard log lines.

Any plugin can push a line from any thread (asyncio loop, GStreamer
streaming thread, GLib bus watch):

    from core.log_hub import log_hub
    log_hub.push("udp", "[192.168.1.5] clk;0130")
    log_hub.push("diag", "STALL Camera 2 ...", level="error", camera=2)

The dashboard polls  GET /api/system/logs?since=<seq>  (see get_log_router()).
While a session file is open (Pipeline Diagnostics plugin), every line is
also appended to it as JSON — one timeline of UDP packets, scoreboard
events, IVR commands/logs, pipeline rebuilds and stalls per session.
"""
import json
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

LEVELS = ("debug", "info", "warning", "error")


class LogHub:
    def __init__(self, maxlen: int = 5000):
        self._lines: deque = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._file = None
        self._file_path: Optional[Path] = None
        # Changes on every process start, so a dashboard left open across a
        # core restart knows its `since` cursor is meaningless.
        self.boot_id = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------------ write

    def push(self, source: str, message: str, level: str = "info", **data) -> dict:
        entry = {"ts": time.time(), "source": source, "level": level, "msg": str(message)}
        if data:
            entry["data"] = data
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._lines.append(entry)
            if self._file is not None:
                try:
                    self._file.write(json.dumps(entry, default=str) + "\n")
                except Exception:
                    pass
        return entry

    # ------------------------------------------------------------------ read

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        with self._lock:
            if seq > self._seq:          # cursor from a previous process
                seq = 0
            out = [e for e in self._lines if e["seq"] > seq]
        return out[-limit:]

    @property
    def last_seq(self) -> int:
        return self._seq

    # ------------------------------------------------------------------ session file

    def open_file(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._file is not None:
                self._file.close()
            is_new = not path.exists() or path.stat().st_size == 0
            # line-buffered: survives a hard crash of the core up to the last line
            self._file = open(path, "a", buffering=1, encoding="utf-8")
            self._file_path = path
            if is_new:   # include what happened before the file was opened (core start-up)
                for entry in self._lines:
                    self._file.write(json.dumps(entry, default=str) + "\n")
        return path

    def close_file(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
            self._file = None
            self._file_path = None

    @property
    def file_path(self) -> Optional[Path]:
        return self._file_path


log_hub = LogHub()


def get_log_router():
    """Mount once in the FastAPI app:  app.include_router(get_log_router())"""
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/system/logs", tags=["System Logs"])

    @router.get("")
    async def get_logs(since: int = 0, limit: int = 500):
        return {
            "boot_id": log_hub.boot_id,
            "last_seq": log_hub.last_seq,
            "file": str(log_hub.file_path) if log_hub.file_path else None,
            "entries": log_hub.since(since, min(max(limit, 1), 2000)),
        }

    return router