#!/usr/bin/env python3
"""AI cameraman RENDERER process (started by plugin.py; never imported by the main app).

    main app (core pipeline, pure C):  raw_src1 ─► vapostproc ─► 1080p NV12 ─► shmsink <raw socket>
                                                                     │
    THIS PROCESS ───────────────────────────────────────────────────┼─────────────────────
    input thread (live edge, every frame):                           ▼
        shmsrc ─► camera.step(dt) ─► record camera path ─► copy frame into delay buffer
              └► every 1/detect_fps, only if the detector is idle:
                 2x downscale + NV12→BGR straight into shared memory (960x540)
                                                                     │
    detector PROCESS (worker.py, CPU only, niced, 2 threads):        ▼
        YOLO+ByteTrack ─► AthleteSelector ─► boxes back ─► camera.observe()
                                                                     │
    pacer thread (output_delay_s behind live):                       ▼
        Gaussian-smoothed camera path (look-ahead, zero lag) ─► subpixel,
        anti-aliased crop ─► 720p ─► stamped with its due time ─► shmsink (sync on clock)

Why three processes:
  * main app   : its Python threads (web server, OBS control, ...) can never steal
                 GIL time from rendering — that would cause dropped/late frames.
  * renderer   : GStreamer + OpenCV only, with its own GIL and only its own threads.
  * detector   : torch / OpenVINO, isolated because their native libraries clash
                 with GStreamer's in one process (that was the free() crash).
  Any of them can die without taking the others down; each parent restarts its child.

Anti-flicker (livestream grade):
  * Sub-pixel window movement (no whole-pixel snapping) + low-pass before
    downscaling (no twinkling detail).
  * Output delayed by `output_delay_s` (default 1 s): zero-phase smoothing of the
    camera path with look-ahead, `anticipation_s` lead, and a jitter buffer.
  * Frame times are run through a PLL, so the output cadence is perfectly regular;
    every frame is rendered ahead and released by shmsink on the GStreamer clock.

Protocol with plugin.py:
  argv  : <settings json> <raw socket> <output socket>
  stdin : EOF or {"type": "quit"} -> clean shutdown
  fd N  : JSON lines {"type": "status", ...} / {"type": "log", ...}   (N = $AICAM_STATUS_FD)
"""
from __future__ import annotations

import json
import math
import os
import select
import subprocess
import sys
import threading
import time
from collections import deque
from multiprocessing import shared_memory
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[3]          # project root (folder that holds plugins/)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from numpy.lib.stride_tricks import as_strided  # noqa: E402

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402

from plugins.universal.ai_cameraman import autocam  # noqa: E402
from plugins.universal.ai_cameraman.thermal import ThermalGovernor, efficiency_cores, parse_cpu_list  # noqa: E402

_WORKER = Path(__file__).with_name("worker.py")
_READY_TIMEOUT = 300.0      # first start exports the model
_RESULT_TIMEOUT = 15.0
_RENDER_AHEAD = 0.120       # render this far ahead; shmsink waits on the clock (absorbs stalls)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _nv12_layout(buf: Gst.Buffer, info: GstVideo.VideoInfo, size: int):
    """(y_offset, uv_offset, y_stride, uv_stride) — buffers may be padded."""
    W, H = info.width, info.height
    meta = None
    try:
        meta = GstVideo.buffer_get_video_meta(buf)
    except Exception:
        pass
    candidates = []
    if meta is not None:
        try:
            candidates.append((meta.offset[0], meta.offset[1], meta.stride[0], meta.stride[1]))
        except Exception:
            pass
    try:
        candidates.append((info.offset[0], info.offset[1], info.stride[0], info.stride[1]))
    except Exception:
        pass
    rows = max(H, size * 2 // (3 * W)) if W else H     # last resort: tight rows
    candidates.append((0, W * rows, W, W))
    for o0, o1, s0, s1 in candidates:
        if s0 >= W and s1 >= W and o0 + s0 * (H - 1) + W <= o1 and o1 + s1 * (H // 2 - 1) + W <= size:
            return o0, o1, s0, s1
    return None


def _resolve_cores(spec: str) -> Optional[set[int]]:
    spec = (spec or "").strip().lower()
    if not spec:
        return None
    if spec == "auto":
        return efficiency_cores()
    return parse_cpu_list(spec) & os.sched_getaffinity(0) or None


def _settings_json(cfg) -> str:
    dump = getattr(cfg, "model_dump_json", None) or cfg.json        # pydantic v2 / v1
    return dump()


class _LineReader:
    """Line reader over a raw fd with a timeout (never blocks on partial lines)."""

    def __init__(self, fd: int):
        self.fd, self.buf = fd, b""

    def readline(self, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], min(left, 0.5))
            if not r:
                continue
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode()


class _FrameClock:
    """PLL over frame arrival times: a perfectly regular timeline that still
    tracks the camera's true rate (and its drift against the PC clock)."""

    def __init__(self, fps: float):
        self.nominal = 1.0 / fps
        self.reset()

    def reset(self) -> None:
        self.t: Optional[float] = None
        self.period = self.nominal

    def __call__(self, arrival: float) -> tuple[float, bool]:
        """Returns (smoothed frame time, discontinuity)."""
        if self.t is None:
            self.t = arrival
            return self.t, True
        pred = self.t + self.period
        err = arrival - pred
        if abs(err) > 0.25:                       # gap / restart / jump: start over
            self.reset()
            self.t = arrival
            return self.t, True
        self.t = pred + 0.05 * err                # phase correction (gentle)
        self.period += 0.002 * err                # frequency correction (very gentle)
        self.period = min(max(self.period, self.nominal * 0.9), self.nominal * 1.1)
        return self.t, False


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class AICameramanEngine:

    def __init__(self, cfg, raw_socket: str, output_socket: str, log):
        self.cfg = cfg
        self.raw_socket = raw_socket
        self.socket = output_socket
        self.log = log

        self.W, self.H = cfg.input_width & ~1, cfg.input_height & ~1
        self.aw, self.ah = cfg.analysis_width & ~1, cfg.analysis_height & ~1
        self.ow, self.oh = cfg.output_width & ~1, cfg.output_height & ~1
        self.cam = autocam.make_camera(cfg, self.aw, self.ah, cfg.detect_fps)
        self.governor = ThermalGovernor(cfg.thermal_hot_c, cfg.thermal_cool_c)
        self.cores = _resolve_cores(cfg.cpu_cores)

        # analysis frame lives in shared memory: written here, read by the detector process
        self._shm = shared_memory.SharedMemory(create=True, size=self.aw * self.ah * 3)
        self._frame = np.ndarray((self.ah, self.aw, 3), dtype=np.uint8, buffer=self._shm.buf)

        self._cam_lock = threading.Lock()
        self._stop = threading.Event()
        self._job_ready = threading.Event()
        self._job_dt = 0.1
        self._busy = True                       # until the worker is ready
        self._threads: list[threading.Thread] = []
        self._proc: Optional[subprocess.Popen] = None

        n = self.ow * self.oh
        self._out = np.zeros(n * 3 // 2, np.uint8)
        self._out[n:] = 128
        self._out_y = self._out[:n].reshape(self.oh, self.ow)
        self._out_uv = self._out[n:].reshape(self.oh // 2, self.ow // 2, 2)
        self._frame_ns = int(1e9 / cfg.output_fps)

        self._out_pipeline: Optional[Gst.Pipeline] = None
        self._appsrc = None

        # ---- delay buffer + camera path (anti-flicker)
        self.delay = float(cfg.output_delay_s)
        self.sigma = max(float(cfg.path_smoothing_s), 1e-3)
        # look-ahead must fit inside the delay (minus what is rendered ahead)
        self.lead = max(0.0, min(float(cfg.anticipation_s), self.delay - _RENDER_AHEAD - 3 * self.sigma))
        self.antialias = bool(cfg.antialias)
        self._clock = _FrameClock(cfg.output_fps)
        self._buf_lock = threading.Lock()
        self._frame_ready = threading.Event()
        self._ring: deque = deque()                 # (frame_t, (Y, UV)) waiting for output
        self._free: list = []                       # recycled frame buffers
        self._path: deque = deque()                 # (frame_t, cx, cy, w) camera at the live edge
        self._anchor: Optional[float] = None        # monotonic due = anchor + frame_t
        self._slot_shape = None
        self._n_alloc = 0
        self._n_slots = int(math.ceil((self.delay + 0.5) * 60)) + 4     # cap, covers up to 60 fps
        self._last_pts: Optional[int] = None

        self.state = "starting"
        self.error: Optional[str] = None
        self.device: Optional[str] = None
        self.backend: Optional[str] = None
        self.restarts = 0
        self.input_connected = False
        self.stats = {"input_fps": 0.0, "render_fps": 0.0, "detect_fps": 0.0, "infer_ms": 0.0,
                      "render_ms": 0.0, "zoom": 1.0, "athletes": [], "dropped": 0, "buffered_s": 0.0,
                      # stutter diagnostics
                      "source_fps": 0.0,        # camera rate as tracked by the frame clock
                      "input_gaps": 0,          # frames missing BEFORE the renderer (camera/decoder/core)
                      "input_gaps_last_10s": 0,
                      "max_input_gap_ms": 0.0,
                      "late_frames": 0,         # frames the renderer finished after their slot
                      "max_render_ms": 0.0}
        self._gap_times: deque = deque()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._start_output()
        for fn, name in ((self._input_loop, "aicam-input"), (self._pacer_loop, "aicam-pacer"),
                         (self._supervise, "aicam-supervisor")):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._job_ready.set()
        self._frame_ready.set()
        self._kill_worker()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()
        if self._out_pipeline is not None:
            self._appsrc.emit("end-of-stream")
            self._out_pipeline.set_state(Gst.State.NULL)
            self._out_pipeline = None
            self._appsrc = None
        try:
            self._shm.close()
            self._shm.unlink()
        except Exception:
            pass
        try:
            os.remove(self.socket)
        except OSError:
            pass

    def _start_output(self) -> None:
        try:
            os.remove(self.socket)
        except OSError:
            pass
        frame = self.ow * self.oh * 3 // 2
        desc = (
            f"appsrc name=aicam_src is-live=true format=time do-timestamp=false block=false "
            f"max-bytes={frame * 16} "
            f"caps=video/x-raw,format=NV12,width={self.ow},height={self.oh},framerate={self.cfg.output_fps}/1 ! "
            f"queue max-size-buffers=10 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
            f"shmsink socket-path={self.socket} wait-for-connection=false "
            f"shm-size=200000000 sync=true async=false qos=false max-lateness=-1"
        )
        self._out_pipeline = Gst.parse_launch(desc)
        self._appsrc = self._out_pipeline.get_by_name("aicam_src")
        self._out_pipeline.set_state(Gst.State.PLAYING)
        self.log.info(f"AI cameraman output → {self.socket} ({self.ow}x{self.oh}@{self.cfg.output_fps}, "
                      f"{self.delay:.1f}s delay)")

    # ------------------------------------------------------------------ input thread (live edge)

    def _open_input(self):
        desc = (
            f"shmsrc socket-path={self.raw_socket} is-live=true do-timestamp=true ! "
            f"video/x-raw,format=NV12,width={self.W},height={self.H},"
            f"framerate={self.cfg.output_fps}/1 ! "
            f"appsink name=in emit-signals=false sync=false drop=true max-buffers=8 "
            f"enable-last-sample=false"
        )
        pipe = Gst.parse_launch(desc)
        pipe.set_state(Gst.State.PLAYING)
        return pipe, pipe.get_by_name("in"), pipe.get_bus()

    def _input_loop(self) -> None:
        """Live edge: pulls every frame, advances the camera simulation, records the
        camera path, parks the raw frame in the delay buffer and feeds the detector.
        It never renders — the pacer does that, `output_delay_s` later."""
        cv2.setNumThreads(1)
        while not self._stop.is_set():
            if not os.path.exists(self.raw_socket):          # core pipeline not (re)built yet
                self.input_connected = False
                self._stop.wait(0.25)
                continue
            try:
                pipe, sink, bus = self._open_input()
            except Exception as exc:
                self.log.warning(f"AI cameraman: cannot open input: {exc}")
                self._stop.wait(1.0)
                continue
            try:
                self._pull_frames(sink, bus)
            finally:
                pipe.set_state(Gst.State.NULL)
                self.input_connected = False
                self._reset_timeline()
            self._stop.wait(0.25)                 # core pipeline rebuilt: reconnect

    def _pull_frames(self, sink, bus) -> None:
        info, info_caps = None, None
        last_t = None
        last_job_t = 0.0
        last_det_t = None
        n_frames, t_stat = 0, time.monotonic()
        idle_since = time.monotonic()
        last_arrival = None

        while not self._stop.is_set():
            if self.governor.poll():
                self.log.info(f"AI cameraman: CPU {self.governor.temp_c:.0f}°C → "
                              f"detection at {self.governor.scale:.0%} rate")
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is not None:
                return                                         # socket closed: reconnect
            sample = sink.emit("try-pull-sample", 250 * Gst.MSECOND)
            if sample is None:
                if time.monotonic() - idle_since > 2.0 and not os.path.exists(self.raw_socket):
                    return
                continue
            now = time.monotonic()
            idle_since = now
            self.input_connected = True
            if last_arrival is not None:                  # a frame went missing upstream?
                gap = now - last_arrival
                if gap > 1.6 * self._clock.period and gap < 1.0:
                    self.stats["input_gaps"] += 1
                    self._gap_times.append(now)
                    self.stats["max_input_gap_ms"] = round(max(self.stats["max_input_gap_ms"], gap * 1000), 1)
            last_arrival = now

            caps = sample.get_caps()
            if caps is not info_caps:
                info_caps = caps
                info = GstVideo.VideoInfo.new_from_caps(caps)
            buf = sample.get_buffer()

            # regular frame timeline (PLL over arrival) — the pacer reproduces it exactly
            t, discontinuity = self._clock(now)
            if discontinuity and last_t is not None:
                self._reset_timeline(keep_clock=True)
                last_det_t = None
            dt = min(max(t - last_t, 1e-3), 0.2) if (last_t is not None and not discontinuity) \
                else 1.0 / self.cfg.output_fps
            last_t = t

            with self._cam_lock:
                self.cam.step(dt)
                cx, cy, w = self.cam.cx, self.cam.cy, self.cam.w
            with self._buf_lock:
                self._path.append((t, cx, cy, w))

            want_job = (not self._busy and
                        now - last_job_t >= 1.0 / (self.cfg.detect_fps * self.governor.scale))

            ok, mi = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            made_job = False
            try:
                arr = np.frombuffer(mi.data, dtype=np.uint8)
                layout = _nv12_layout(buf, info, arr.size)
                if layout is None:
                    continue
                W, H = info.width & ~1, info.height & ~1
                o0, o1, s0, s1 = layout
                y = as_strided(arr[o0:], shape=(H, W), strides=(s0, 1))
                uv = as_strided(arr[o1:], shape=(H // 2, W // 2, 2), strides=(s1, 2, 1))

                slot = self._take_slot(W, H)
                np.copyto(slot[0], y)
                np.copyto(slot[1], uv)

                if want_job:
                    small_y = cv2.resize(y, (self.aw, self.ah), interpolation=cv2.INTER_AREA)
                    small_uv = cv2.resize(uv, (self.aw // 2, self.ah // 2), interpolation=cv2.INTER_AREA)
                    cv2.cvtColorTwoPlane(small_y, small_uv, cv2.COLOR_YUV2BGR_NV12, dst=self._frame)
                    made_job = True
            finally:
                buf.unmap(mi)
            del sample, buf                       # hand the shm block back to the sender now

            with self._buf_lock:
                if self._anchor is None:
                    self._anchor = now - t + self.delay       # output = input + delay
                self._ring.append((t, slot))
            self._frame_ready.set()

            if made_job:
                det_dt = (t - last_det_t) if last_det_t is not None else now - last_job_t
                self._job_dt = min(max(det_dt, 1e-3), 2.0)
                self._busy = True
                last_job_t, last_det_t = now, t
                self._job_ready.set()

            n_frames += 1
            if now - t_stat >= 2.0:
                self.stats["input_fps"] = n_frames / (now - t_stat)
                self.stats["source_fps"] = round(1.0 / self._clock.period, 2)
                while self._gap_times and now - self._gap_times[0] > 10.0:
                    self._gap_times.popleft()
                self.stats["input_gaps_last_10s"] = len(self._gap_times)
                self.stats["zoom"] = self.cam.zoom
                n_frames, t_stat = 0, now

    # ------------------------------------------------------------------ delay buffer

    def _take_slot(self, W: int, H: int):
        """A free (Y, UV) frame buffer. If the pacer is stalled, recycle the oldest frame."""
        with self._buf_lock:
            if self._slot_shape != (H, W):
                self._slot_shape = (H, W)
                self._free.clear()
                self._ring.clear()
                self._n_alloc = 0
            if self._free:
                return self._free.pop()
            if self._n_alloc < self._n_slots:        # grow on demand (~delay x fps buffers)
                self._n_alloc += 1
                return np.empty((H, W), np.uint8), np.empty((H // 2, W // 2, 2), np.uint8)
            self.stats["dropped"] += 1
            return self._ring.popleft()[1]

    def _reset_timeline(self, keep_clock: bool = False) -> None:
        with self._buf_lock:
            self._free.extend(slot for _, slot in self._ring)
            self._ring.clear()
            self._path.clear()
            self._anchor = None
        if not keep_clock:
            self._clock.reset()

    def _smoothed_rect(self, t: float):
        """Camera window for the frame at time t, from the recorded path.

        Zero-phase Gaussian over [t+lead-3σ, t+lead+3σ]: because output is
        delayed, the 'future' half of the window already exists — so the path
        is smoothed without adding any lag, and `lead` lets the camera start
        moving slightly *before* the action (like a human operator)."""
        c = t + self.lead
        lo, hi = c - 3 * self.sigma, c + 3 * self.sigma
        with self._buf_lock:
            while len(self._path) > 2 and self._path[1][0] < lo - 1.0:
                self._path.popleft()
            pts = [p for p in self._path if lo <= p[0] <= hi]
            if not pts and self._path:
                pts = [min(self._path, key=lambda p: abs(p[0] - c))]
        if not pts:
            return None
        a = np.array(pts, dtype=np.float64)
        wts = np.exp(-0.5 * ((a[:, 0] - c) / self.sigma) ** 2)
        cx, cy, w = (a[:, 1:] * wts[:, None]).sum(axis=0) / wts.sum()
        w = min(max(w, self.cam.full_w() / self.cam.max_zoom), self.cam.full_w())
        h = w / self.cam.aspect
        cx = min(max(cx, w / 2), self.aw - w / 2)
        cy = min(max(cy, h / 2), self.ah - h / 2)
        return cx - w / 2, cy - h / 2, w, h

    # ------------------------------------------------------------------ pacer thread (output)

    def _pacer_loop(self) -> None:
        """Renders each frame ~_RENDER_AHEAD before it is due and stamps it with its
        due time; shmsink (sync=true) releases it on the GStreamer clock from C, so
        neither render-time variation nor Python scheduling reaches OBS."""
        cv2.setNumThreads(1)
        n_frames, t_stat, render_acc = 0, time.monotonic(), 0.0
        last_rect = None

        while not self._stop.is_set():
            with self._buf_lock:
                head = self._ring[0] if self._ring else None
                anchor = self._anchor
            if head is None or anchor is None:
                self._frame_ready.clear()
                self._frame_ready.wait(0.05)
                continue
            t, slot = head
            due = anchor + t
            now = time.monotonic()
            if due - now > _RENDER_AHEAD:
                self._stop.wait(min(due - now - _RENDER_AHEAD, 0.02))
                continue
            with self._buf_lock:
                if not self._ring or self._ring[0][1] is not slot:
                    continue
                self._ring.popleft()
                skip = now - due > 0.5 and len(self._ring) > 1     # badly behind: catch up
            if skip:
                with self._buf_lock:
                    self._free.append(slot)
                self.stats["dropped"] += 1
                continue

            t0 = time.perf_counter()
            rect = self._smoothed_rect(t) or last_rect
            if rect is not None:
                self._render(slot[0], slot[1], rect)
                last_rect = rect
            with self._buf_lock:
                self._free.append(slot)
            out = Gst.Buffer.new_wrapped(self._out.tobytes())
            out.duration = self._frame_ns
            render_acc += time.perf_counter() - t0

            took = time.perf_counter() - t0
            if took * 1000 > self.stats["max_render_ms"]:
                self.stats["max_render_ms"] = round(took * 1000, 1)
            if time.monotonic() > due:                      # missed its slot: visible hitch
                self.stats["late_frames"] += 1
            pts = self._due_to_pts(due)
            if pts is not None:
                out.pts = pts
            else:                                   # no clock yet: fall back to sleeping
                while (left := due - time.monotonic()) > 0 and not self._stop.is_set():
                    time.sleep(max(left - 0.0015, 0) if left > 0.002 else 0)
            self._appsrc.emit("push-buffer", out)

            n_frames += 1
            now = time.monotonic()
            if now - t_stat >= 2.0:
                self.stats["render_fps"] = n_frames / (now - t_stat)
                self.stats["render_ms"] = 1000.0 * render_acc / max(n_frames, 1)
                with self._buf_lock:
                    self.stats["buffered_s"] = round(len(self._ring) / max(self.stats["render_fps"], 1), 2)
                n_frames, render_acc, t_stat = 0, 0.0, now

    def _due_to_pts(self, due: float) -> Optional[int]:
        """Monotonic due time -> running-time PTS of the output pipeline (strictly increasing)."""
        pl = self._out_pipeline
        clock = pl.get_clock() if pl is not None else None
        if clock is None:
            return None
        running_now = clock.get_time() - pl.get_base_time()
        pts = running_now + int((due - time.monotonic()) * 1e9)
        if self._last_pts is not None and pts <= self._last_pts:
            pts = self._last_pts + 1_000_000            # keep it monotonic (+1 ms)
        self._last_pts = pts
        return max(pts, 0)

    def _render(self, y, uv, rect) -> None:
        """Subpixel crop+scale of the 1080p NV12 frame into the 720p output.

        * warpAffine with float offsets: the window moves in 1/32-pixel steps,
          never in whole-pixel jumps (whole-pixel snapping is what shimmers).
        * When scaling down, a matched low-pass first, so fine detail (crowd,
          mat lines) doesn't alias/twinkle while the camera moves.
        """
        H, W = y.shape
        fx, fy, fw, _fh = rect
        sx, sy = W / self.aw, H / self.ah
        x0, y0, w = fx * sx, fy * sy, fw * sx
        s = w / self.ow                                   # source px per output px
        if self.antialias and s > 1.15:
            m = 2                                         # blur only the region the window reads
            rx0, ry0 = max(int(x0) - m, 0) & ~1, max(int(y0) - m, 0) & ~1
            rx1 = min(int(x0 + w) + m + 1, W)
            ry1 = min(int(y0 + w * self.oh / self.ow) + m + 1, H)
            sigma = 0.5 * math.sqrt(s * s - 1.0)
            ys = cv2.GaussianBlur(y[ry0:ry1, rx0:rx1], (3, 3), sigma)
            uvs = cv2.GaussianBlur(uv[ry0 // 2:(ry1 + 1) // 2, rx0 // 2:(rx1 + 1) // 2], (3, 3), sigma)
            x0, y0 = x0 - rx0, y0 - ry0
        else:
            ys, uvs = y, uv
        # pixel-centre-correct mapping: src = x0 + (dst + 0.5) * s - 0.5
        m_y = np.array([[s, 0, x0 + 0.5 * s - 0.5], [0, s, y0 + 0.5 * s - 0.5]], np.float32)
        m_c = np.array([[s, 0, x0 / 2 + 0.5 * s - 0.5], [0, s, y0 / 2 + 0.5 * s - 0.5]], np.float32)
        flags = cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP
        cv2.warpAffine(ys, m_y, (self.ow, self.oh), dst=self._out_y, flags=flags,
                       borderMode=cv2.BORDER_REPLICATE)
        cv2.warpAffine(uvs, m_c, (self.ow // 2, self.oh // 2), dst=self._out_uv, flags=flags,
                       borderMode=cv2.BORDER_REPLICATE)

    # ------------------------------------------------------------------ detector process

    def _spawn_worker(self, backend: str):
        r, w = os.pipe()
        env = dict(os.environ)
        env["AICAM_REPLY_FD"] = str(w)
        env["AICAM_THREADS"] = str(self.cfg.cpu_threads)
        env.pop("AICAM_STATUS_FD", None)
        if self.cores:
            env["AICAM_CORES"] = ",".join(map(str, sorted(self.cores)))
        proc = subprocess.Popen(
            [sys.executable, str(_WORKER), _settings_json(self.cfg), self._shm.name, backend],
            stdin=subprocess.PIPE, pass_fds=(w,), env=env, text=True, bufsize=1,
            cwd=os.getcwd(), start_new_session=True,
        )
        os.close(w)
        return proc, r

    def _kill_worker(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write('{"type": "quit"}\n')
            proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    def _supervise(self) -> None:
        backend = self.cfg.backend
        early_failures = 0
        while not self._stop.is_set():
            self._busy = True
            proc, rfd = self._spawn_worker(backend)
            self._proc = proc
            reader = _LineReader(rfd)
            ready = False
            why = None
            try:
                self.state = "starting"
                line = reader.readline(_READY_TIMEOUT)
                if line is None:
                    raise RuntimeError("detector did not become ready in time")
                msg = json.loads(line)
                if msg.get("type") == "error":
                    self.log.error(f"AI cameraman: detector failed to load: {msg.get('error')}\n"
                                   f"{msg.get('trace', '')}")
                    raise RuntimeError(msg.get("error"))
                ready = True
                early_failures = 0
                self.device, self.backend = msg.get("device"), msg.get("backend")
                self.state, self.error = "active", None
                self.log.info(f"AI cameraman: detector ready ({self.backend} on CPU, "
                              f"{msg['shape'][1]}x{msg['shape'][0]})")
                self._detect_loop(proc, reader)
            except EOFError:
                pass
            except Exception as exc:
                why = f"{exc}"
            finally:
                os.close(rfd)
                if self._proc is proc:
                    self._kill_worker()
                if proc.poll() is None:
                    proc.kill()
                code = proc.wait()

            if self._stop.is_set():
                return
            self.restarts += 1
            with self._cam_lock:
                self.cam.observe([], 10.0)                       # -> wide shot
            if not ready:
                early_failures += 1
            why = why or f"exit code {code}"
            self.state = "error"
            self.error = f"detector process stopped ({why})"
            self.log.error(f"AI cameraman: {self.error} — restarting.")
            if not ready and backend == "openvino" and early_failures >= 2:
                backend = "pytorch"
                early_failures = 0
                self.log.warning("AI cameraman: OpenVINO keeps failing here — switching to the PyTorch backend.")
            elif early_failures >= 4:
                self.log.error("AI cameraman: detector cannot start — giving up (output stays on the wide shot).")
                return
            self._stop.wait(min(30.0, 2.0 * self.restarts))

    def _detect_loop(self, proc, reader: _LineReader) -> None:
        self._busy = False
        n_det, t_stat, infer_acc = 0, time.monotonic(), 0.0
        while not self._stop.is_set():
            if not self._job_ready.wait(0.5):
                if proc.poll() is not None:
                    raise EOFError
                continue
            self._job_ready.clear()
            if self._stop.is_set():
                return
            proc.stdin.write(json.dumps({"type": "frame", "dt": self._job_dt}) + "\n")
            line = reader.readline(_RESULT_TIMEOUT)
            if line is None:
                raise RuntimeError("detector stopped answering")
            msg = json.loads(line)
            if msg.get("error"):
                self.log.error(f"AI cameraman: detection step failed: {msg['error']}")
            with self._cam_lock:
                self.cam.observe(msg.get("boxes") or [], self._job_dt, wide_box=msg.get("wide"))
            self.stats["athletes"] = msg.get("chosen", [])
            if msg.get("mat"):
                self.stats["mat"] = msg["mat"]
            infer_acc += msg.get("infer_ms", 0.0)
            n_det += 1
            self._busy = False

            now = time.monotonic()
            if now - t_stat >= 2.0:
                self.stats["detect_fps"] = n_det / (now - t_stat)
                self.stats["infer_ms"] = infer_acc / max(n_det, 1)
                n_det, infer_acc, t_stat = 0, 0.0, now

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        return {
            "discipline": getattr(self.cfg, "discipline", "kyorugi"),
            "state": self.state,
            "error": self.error,
            "input_connected": self.input_connected,
            "device": self.device,
            "backend": self.backend,
            "detector_pid": self._proc.pid if self._proc else None,
            "detector_restarts": self.restarts,
            "output_socket": self.socket,
            "output_delay_s": self.delay,
            "cpu_temp_c": self.governor.temp_c,
            "detect_rate_scale": self.governor.scale,
            "pinned_cores": sorted(self.cores) if self.cores else None,
            **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.stats.items()},
        }


# --------------------------------------------------------------------------
# process entry point
# --------------------------------------------------------------------------
class _PipeLog:
    """Logger that forwards to the main app (and to stderr as a fallback)."""

    def __init__(self, send):
        self._send = send

    def _emit(self, level: str, msg: str) -> None:
        print(f"[aicam-render] {msg}", file=sys.stderr, flush=True)
        self._send({"type": "log", "level": level, "msg": msg})

    def info(self, msg):    self._emit("info", msg)
    def warning(self, msg): self._emit("warning", msg)
    def error(self, msg):   self._emit("error", msg)


def main() -> int:
    from plugins.universal.ai_cameraman.settings import settings_from_json

    cfg = settings_from_json(sys.argv[1])
    raw_socket, out_socket = sys.argv[2], sys.argv[3]
    status_fd = os.environ.get("AICAM_STATUS_FD")
    out = os.fdopen(int(status_fd), "w", buffering=1) if status_fd else None
    out_lock = threading.Lock()

    def send(obj) -> None:
        if out is None:
            return
        try:
            with out_lock:
                out.write(json.dumps(obj) + "\n")
        except (BrokenPipeError, OSError, ValueError):
            pass

    import signal
    stop = threading.Event()
    # Shutdown comes from the main app (stdin "quit"/EOF). Ignore terminal Ctrl+C;
    # treat SIGTERM as a clean shutdown request.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    sys.setswitchinterval(0.001)          # own process: snappy thread hand-offs are free
    # NOT niced: output timing must win over busy processes. Only the detector is niced.
    Gst.init(None)
    engine = AICameramanEngine(cfg, raw_socket, out_socket, _PipeLog(send))
    engine.start()

    def watch_stdin():                    # parent gone (EOF) or "quit" -> shut down
        for line in sys.stdin:
            if '"quit"' in line:
                break
        stop.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    while not stop.wait(1.0):
        send({"type": "status", **engine.status()})
    engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())