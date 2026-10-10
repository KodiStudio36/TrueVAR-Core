"""Supervises the AI cameraman renderer process from inside the main app.

Pure stdlib: the main app never imports OpenCV, numpy, torch or OpenVINO for the
AI cameraman, and no per-frame work happens here. If the renderer dies it is
restarted (with backoff); its log lines are forwarded to the app's logger.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_RENDERER = Path(__file__).with_name("engine.py")


class RendererProcess:

    def __init__(self, settings_json: str, raw_socket: str, output_socket: str, logger):
        self._args = [sys.executable, str(_RENDERER), settings_json, raw_socket, output_socket]
        self.logger = logger
        self.output_socket = output_socket
        self.status: dict = {"state": "starting"}
        self.restarts = 0
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="aicam-renderer-supervisor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write('{"type": "quit"}\n')
                proc.stdin.flush()
                proc.stdin.close()
                proc.wait(timeout=timeout)
            except Exception:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------ supervisor

    def _run(self) -> None:
        while not self._stop.is_set():
            r, w = os.pipe()
            env = dict(os.environ, AICAM_STATUS_FD=str(w))
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    self._args, stdin=subprocess.PIPE, pass_fds=(w,), env=env,
                    text=True, bufsize=1, cwd=os.getcwd(),
                    # own session: a terminal Ctrl+C only hits the main app, which then
                    # shuts us down in order (renderer -> detector) via stop()
                    start_new_session=True,
                )
            except Exception as exc:
                os.close(r)
                os.close(w)
                self.status = {"state": "error", "error": f"cannot start renderer: {exc!r}"}
                self.logger.error(f"AI cameraman: cannot start renderer: {exc!r}")
                return
            os.close(w)
            self._read(r)
            os.close(r)
            code = self._proc.wait()
            if self._stop.is_set():
                return
            self.restarts += 1
            self.status = {"state": "error", "error": f"renderer process stopped (exit code {code})",
                           "renderer_restarts": self.restarts}
            self.logger.error(f"AI cameraman: renderer process stopped (exit code {code}) — restarting.")
            quick = time.monotonic() - started < 10.0
            self._stop.wait(min(30.0, 2.0 * self.restarts) if quick else 1.0)

    def _read(self, fd: int) -> None:
        buf = b""
        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                if self._proc.poll() is not None:
                    return
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "status":
                    msg.pop("type")
                    msg["renderer_pid"] = self._proc.pid
                    msg["renderer_restarts"] = self.restarts
                    self.status = msg
                elif msg.get("type") == "log":
                    level = msg.get("level", "info")
                    getattr(self.logger, level if level in ("info", "warning", "error") else "info")(msg.get("msg", ""))