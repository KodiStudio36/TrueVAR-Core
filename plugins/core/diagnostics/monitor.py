"""Per-stage frame monitoring of the core GStreamer pipeline (+ IVR stats).

For every source the video path is split into stages, each measured by a
pad probe (core) or by the IVR app itself (reported over WS every second):

    camera      camN_in src pad      encoded frames arriving from the camera
    processed   tee_srcN sink pad    decoded + scaled 720p NV12 frames
    shm_out     camN_shm sink pad    frames written into the IVR shm socket
    ivr_in      (IVR app)            frames the IVR's shmsrc received
    ivr_encoded (IVR app)            frames the IVR encoder produced (-> file)

The first stage in that chain that stops delivering is where the video is
lost; everything after it stalls as a consequence. evaluate() turns that
into one verdict per source and reports every change of verdict.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

CORE_STAGES = ("camera", "processed", "shm_out")
IVR_STAGES = ("ivr_in", "ivr_encoded")
ALL_STAGES = CORE_STAGES + IVR_STAGES

STAGE_LABELS = {
    "camera": "camera in",
    "processed": "decoded 720p",
    "shm_out": "-> shm",
    "ivr_in": "IVR <- shm",
    "ivr_encoded": "IVR encoded",
}

# element name template, pad name
_STAGE_PADS = {
    "camera": ("cam{i}_in", "src"),
    "processed": ("tee_src{i}", "sink"),
    "shm_out": ("cam{i}_shm", "sink"),
}

# What it means when THIS stage is the first one that stopped.
_BREAK_EXPLANATION = {
    "camera": "no frames from the camera (network / camera / RTSP session or USB capture)",
    "processed": "camera delivers, but decoding/scaling stopped (decoder / vapostproc / jpegdec)",
    "shm_out": "decoded frames OK, but nothing is written to shm (shmsink blocked: a client is not releasing buffers)",
    "ivr_in": "core writes to shm, but the IVR is not receiving (IVR shmsrc errored/disconnected — pipeline rebuild?)",
    "ivr_encoded": "IVR receives frames, but encoding/writing stopped (vah264enc / jpegenc / avimux / disk)",
}

# A stage that never produced a frame is only "dead" after this grace period.
STARTUP_GRACE_S = 6.0
IVR_STATS_STALE_S = 3.5


class FrameCounter:
    """Updated from GStreamer streaming threads, read from the asyncio loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self.frames = 0
        self.last: float | None = None
        self.max_gap = 0.0
        self.hiccups = 0                 # gaps > HICCUP_S since last read
        self._tick_frames = 0
        self._tick_time = time.monotonic()

    HICCUP_S = 0.15                      # > ~4 frames missing at 30 fps: visible stutter

    def hit(self, n: int = 1):
        now = time.monotonic()
        with self._lock:
            if self.last is not None:
                gap = now - self.last
                if gap > self.max_gap:
                    self.max_gap = gap
                if gap > self.HICCUP_S:
                    self.hiccups += 1
            self.last = now
            self.frames += n

    def read(self) -> dict:
        now = time.monotonic()
        with self._lock:
            dt = max(now - self._tick_time, 1e-6)
            fps = (self.frames - self._tick_frames) / dt
            self._tick_frames, self._tick_time = self.frames, now
            gap = self.max_gap
            if self.last is not None:
                gap = max(gap, now - self.last)
            self.max_gap = 0.0
            hiccups, self.hiccups = self.hiccups, 0
            since = None if self.last is None else int((now - self.last) * 1000)
        return {"frames": self.frames, "fps": round(fps, 1), "ms_since_last": since,
                "max_gap_ms": int(gap * 1000), "hiccups": hiccups}


@dataclass
class SourceState:
    idx: int
    label: str
    counters: dict = field(default_factory=dict)       # stage -> FrameCounter (core stages)
    snaps: dict = field(default_factory=dict)           # stage -> last snapshot (all stages)
    states: dict = field(default_factory=dict)          # stage -> ok/stalled/dead/waiting/missing/idle/unknown/error
    window_gap: dict = field(default_factory=dict)      # stage -> max gap (ms) in the summary window
    window_hiccups: dict = field(default_factory=dict)  # stage -> gaps > 150 ms in the summary window
    shm_clients: int = 0
    shm_overruns: int = 0
    overruns_reported: int = 0
    queue_level: int | None = None      # ms of video waiting in the shm queue
    queue_max: int | None = None        # ms the queue holds before it starts dropping
    verdict: str = "starting"
    verdict_level: str = "info"
    bad_stage: str | None = None
    bad_since: float | None = None
    candidate: str | None = "init"   # verdict seen on the previous tick (hysteresis)
    candidate_ticks: int = 0
    has_probes: bool = True


class PipelineMonitor:
    def __init__(self, stall_ms: int = 500):
        self.stall_ms = stall_ms
        self.pipeline = None
        self.attached_at = 0.0
        self.sources: dict[int, SourceState] = {}
        self._probes: list = []            # (pad, probe_id)
        self._signals: list = []           # (element, handler_id)
        self._events: list = []            # (level, msg) produced on streaming threads
        self._events_lock = threading.Lock()

    # ------------------------------------------------------------------ attach / detach

    def attach(self, pipeline, sources: dict[int, str]) -> list[str]:
        """sources: {index: label}. Returns human readable notes."""
        self.detach()
        self.pipeline = pipeline
        self.attached_at = time.monotonic()
        notes = []
        for idx, label in sources.items():
            st = SourceState(idx=idx, label=label)
            missing = []
            for stage, (tmpl, pad_name) in _STAGE_PADS.items():
                el = pipeline.get_by_name(tmpl.format(i=idx))
                pad = el.get_static_pad(pad_name) if el else None
                if pad is None:
                    st.states[stage] = "missing"
                    missing.append(STAGE_LABELS[stage])
                    continue
                counter = FrameCounter()
                st.counters[stage] = counter
                pid = pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                                    self._probe, counter)
                self._probes.append((pad, pid))

            shm = pipeline.get_by_name(f"cam{idx}_shm")
            if shm is not None:
                for sig, delta in (("client-connected", 1), ("client-disconnected", -1)):
                    hid = shm.connect(sig, self._on_shm_client, st, delta)
                    self._signals.append((shm, hid))
            queue = pipeline.get_by_name(f"cam{idx}_shmq")
            if queue is not None:
                st.queue_max = (queue.get_property("max-size-time") // 1_000_000) or None   # ms
                hid = queue.connect("overrun", self._on_overrun, st)
                self._signals.append((queue, hid))

            self.sources[idx] = st
            if not st.counters:
                st.has_probes = False
                st.verdict, st.verdict_level = "not in the pipeline (output replaced / source not built)", "info"
            if missing:
                notes.append(f"{label}: no probe point for {', '.join(missing)}")
        return notes

    def detach(self):
        for pad, pid in self._probes:
            try:
                pad.remove_probe(pid)
            except Exception:
                pass
        for el, hid in self._signals:
            try:
                el.disconnect(hid)
            except Exception:
                pass
        self._probes.clear()
        self._signals.clear()
        self.pipeline = None
        self.sources = {}

    # ------------------------------------------------------------------ callbacks (streaming threads)

    @staticmethod
    def _probe(pad, info, counter):
        if info.type & Gst.PadProbeType.BUFFER_LIST:
            lst = info.get_buffer_list()
            counter.hit(lst.length() if lst else 1)
        else:
            counter.hit()
        return Gst.PadProbeReturn.OK

    def _on_shm_client(self, element, fd, st: SourceState, delta: int):
        st.shm_clients = max(0, st.shm_clients + delta)
        what = "connected" if delta > 0 else "DISCONNECTED"
        with self._events_lock:
            self._events.append(("info" if delta > 0 else "warning",
                                 f"{st.label} shm: client {what} (fd {fd}) -> {st.shm_clients} client(s)"))

    @staticmethod
    def _on_overrun(queue, st: SourceState):
        st.shm_overruns += 1

    # ------------------------------------------------------------------ evaluation (asyncio loop)

    def evaluate(self, ivr_stats: dict | None, ivr_stats_age: float | None) -> list[tuple[str, str]]:
        with self._events_lock:
            events, self._events = self._events, []
        if self.pipeline is None:
            return events

        now = time.monotonic()
        age = now - self.attached_at
        ivr_recording = bool(ivr_stats and ivr_stats.get("recording")
                             and ivr_stats_age is not None and ivr_stats_age < IVR_STATS_STALE_S)
        ivr_streams = (ivr_stats or {}).get("streams", {}) if ivr_recording else {}

        for idx, st in self.sources.items():
            # --- core stages
            for stage, counter in st.counters.items():
                snap = counter.read()
                st.snaps[stage] = snap
                st.states[stage] = self._state(snap, age)
                st.window_gap[stage] = max(st.window_gap.get(stage, 0), snap["max_gap_ms"])
                st.window_hiccups[stage] = st.window_hiccups.get(stage, 0) + snap["hiccups"]

            # --- IVR stages (reported by the IVR app)
            ivr = ivr_streams.get(str(idx))
            if ivr_stats and ivr_stats.get("recording") and not ivr_recording:
                for stage in IVR_STAGES:
                    st.states[stage] = "unknown"      # IVR stopped reporting: frozen / disconnected
                    st.snaps.pop(stage, None)
            elif ivr is None:
                for stage in IVR_STAGES:
                    st.states[stage] = "idle"
                    st.snaps.pop(stage, None)
            else:
                ivr_age = float(ivr_stats.get("uptime_s") or 0)
                for stage, key in (("ivr_in", "shm_in"), ("ivr_encoded", "encoded")):
                    snap = ivr.get(key) or {}
                    st.snaps[stage] = snap
                    state = self._state(snap, ivr_age) if snap else "unknown"
                    if ivr.get("error") and state != "ok":
                        state = "error"
                    st.states[stage] = state
                    st.window_gap[stage] = max(st.window_gap.get(stage, 0), snap.get("max_gap_ms") or 0)

            # --- shm queue fill (back-pressure)
            if self.pipeline is not None:
                q = self.pipeline.get_by_name(f"cam{idx}_shmq")
                st.queue_level = q.get_property("current-level-time") // 1_000_000 if q else None   # ms

            if st.has_probes:
                events.extend(self._update_verdict(st, now, ivr_streams.get(str(idx))))
        return events

    def _state(self, snap: dict, age: float) -> str:
        since = snap.get("ms_since_last")
        if since is None:
            return "waiting" if age < STARTUP_GRACE_S else "dead"
        return "stalled" if since > self.stall_ms else "ok"

    def _update_verdict(self, st: SourceState, now: float, ivr: dict | None):
        events = []
        bad = next((s for s in ALL_STAGES if st.states.get(s) in ("stalled", "dead", "error", "unknown")), None)

        # Hysteresis: stages cross the stall threshold at slightly different
        # moments (and IVR stats arrive once a second), so a downstream stage
        # can look broken one tick before its upstream does. Only act on a
        # verdict that is the same on two consecutive ticks.
        if bad != st.candidate:
            st.candidate, st.candidate_ticks = bad, 1
        else:
            st.candidate_ticks += 1
        if bad is not None and st.bad_since is None:
            st.bad_since = now              # outage starts now, even if confirmed later
        elif bad is None and st.bad_stage is None:
            st.bad_since = None             # a one-tick blip that never got confirmed
        if bad is not None and self._upstream_suspect(st, bad):
            st.candidate_ticks = min(st.candidate_ticks, 1)   # upstream is about to stall too: wait
        if st.candidate_ticks < 2:
            return self._overrun_events(st)

        if bad is None:
            if st.bad_stage is not None:
                took = now - (st.bad_since or now)
                events.append(("info", f"✅ {st.label} recovered after {took:.1f}s "
                                       f"(was broken at '{STAGE_LABELS[st.bad_stage]}')"))
            waiting = [STAGE_LABELS[s] for s in ALL_STAGES if st.states.get(s) == "waiting"]
            st.verdict = "OK" if not waiting else f"waiting for first frame: {', '.join(waiting)}"
            st.verdict_level = "ok" if not waiting else "info"
            st.bad_stage, st.bad_since = None, None
        else:
            if bad != st.bad_stage:
                prev = ALL_STAGES[ALL_STAGES.index(bad) - 1] if bad != ALL_STAGES[0] else None
                flow = self._describe(st, prev) if prev else ""
                state = st.states[bad]
                snap = st.snaps.get(bad) or {}
                since = snap.get("ms_since_last")
                detail = ("never produced a frame" if state == "dead" else
                          "IVR stopped reporting stats (frozen / disconnected?)" if state == "unknown" else
                          f"IVR error: {ivr.get('error')}" if state == "error" and ivr else
                          f"no frame for {since} ms")
                extra = ""
                if bad == "shm_out" and st.queue_level is not None:
                    extra = f" [shm queue {st.queue_level}/{st.queue_max} ms, {st.shm_clients} client(s)]"
                events.append(("error", f"⛔ {st.label}: video lost at '{STAGE_LABELS[bad]}' — {detail}. "
                                        f"{flow}=> {_BREAK_EXPLANATION[bad]}{extra}"))
            st.bad_stage = bad
            st.verdict = f"broken at '{STAGE_LABELS[bad]}': {_BREAK_EXPLANATION[bad]}"
            st.verdict_level = "error"
        return events + self._overrun_events(st)

    def _upstream_suspect(self, st: SourceState, bad: str) -> bool:
        """True if a stage before `bad` is already slowing down (frames late but
        not yet past the stall threshold) — then it is probably the real break."""
        for stage in ALL_STAGES[:ALL_STAGES.index(bad)]:
            since = (st.snaps.get(stage) or {}).get("ms_since_last")
            if since is not None and since > self.stall_ms * 0.3:
                return True
        return False

    def _overrun_events(self, st: SourceState):
        events = []
        new_overruns = st.shm_overruns - st.overruns_reported
        if new_overruns >= 15:
            st.overruns_reported = st.shm_overruns
            events.append(("warning", f"{st.label}: shm queue full, {new_overruns} frames dropped for the "
                                      f"IVR (a shm client is slow or holding buffers; {st.shm_clients} client(s))"))
        return events

    def _describe(self, st: SourceState, stage: str) -> str:
        snap = st.snaps.get(stage) or {}
        if st.states.get(stage) == "ok":
            return f"'{STAGE_LABELS[stage]}' still flows ({snap.get('fps', 0)} fps) "
        return ""

    # ------------------------------------------------------------------ reporting

    def summary_lines(self) -> list[str]:
        lines = []
        for st in self.sources.values():
            if not st.has_probes:
                continue
            parts = []
            for stage in ALL_STAGES:
                state = st.states.get(stage)
                if state in (None, "missing", "idle"):
                    continue
                fps = (st.snaps.get(stage) or {}).get("fps")
                gap = st.window_gap.get(stage, 0)
                hic = st.window_hiccups.get(stage)
                parts.append(f"{STAGE_LABELS[stage]} {fps if fps is not None else '?'}"
                             + ("" if state == "ok" else f"({state})")
                             + (f" [gap {gap}ms" + (f", {hic}x>150ms" if hic else "") + "]" if gap > 100 else ""))
            lines.append(
                f"{st.label}: " + " → ".join(parts) + " fps"
                + (f" | shm q {st.queue_level}/{st.queue_max}ms overruns {st.shm_overruns} clients {st.shm_clients}"
                   if st.queue_level is not None else "")
            )
            st.window_gap.clear()
            st.window_hiccups.clear()
        return lines

    def status(self) -> list[dict]:
        out = []
        for st in self.sources.values():
            out.append({
                "idx": st.idx,
                "label": st.label,
                "verdict": st.verdict,
                "verdict_level": st.verdict_level,
                "stages": [
                    {"key": s, "label": STAGE_LABELS[s], "state": st.states.get(s, "missing"),
                     **{k: (st.snaps.get(s) or {}).get(k) for k in ("fps", "ms_since_last", "max_gap_ms")}}
                    for s in ALL_STAGES
                ],
                "shm": {"clients": st.shm_clients, "queue": st.queue_level,
                        "queue_max": st.queue_max, "overruns": st.shm_overruns},
            })
        return out