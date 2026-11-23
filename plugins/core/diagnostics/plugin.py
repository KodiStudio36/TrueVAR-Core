import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from core.log_hub import log_hub
from plugins.base_plugin import BasePlugin
from plugins.core.diagnostics.monitor import PipelineMonitor
from plugins.core.diagnostics.settings import DiagnosticsSettings


class PipelineDiagnosticsPlugin(BasePlugin):
    """Debug mode for "where does the video get lost?".

    Probes the core pipeline at three points per source (camera in ->
    decoded 720p -> written to shm) and merges the IVR app's own numbers
    (received from shm -> encoded to file). Reports, per source, the first
    stage that stops delivering frames, every shm client (dis)connect, shm
    back-pressure and every pipeline rebuild — on the dashboard and in a
    per-session .jsonl file together with the UDP / scoreboard / IVR feed.

    No pipeline rebuild is needed to turn it on or off: the probes attach to
    named elements of the running pipeline.
    """

    TICK_S = 0.5
    _GSTREAMER_SETTINGS_NAME = "GStreamer Core Plugin"

    dependencies = ["GStreamerCorePlugin"]

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._settings: Optional[DiagnosticsSettings] = None
        self.monitor = PipelineMonitor()
        self.enabled = True
        self._task: Optional[asyncio.Task] = None
        self._subscriptions: list[tuple] = []
        self._ivr_stats: Optional[dict] = None
        self._ivr_stats_at: Optional[float] = None
        self._attached_generation: Optional[int] = None
        self._last_summary = 0.0

    @property
    def name(self) -> str: return "Pipeline Diagnostics"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return DiagnosticsSettings

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._settings = self.core_context["settings_manager"].get_settings(self.name)
        self.enabled = self._settings.enabled
        self.monitor.stall_ms = self._settings.stall_ms

        if self._settings.session_file:
            path = Path(self._settings.log_dir) / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
            log_hub.open_file(path)
            log_hub.push("diag", f"Session timeline file: {path.resolve()}")

        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                ("ivr.diagnostics", self._on_ivr_stats),
                ("ivr.disconnected", self._on_ivr_disconnected),
            ]
            bus.subscribe_many(self._subscriptions)

        self._last_summary = time.monotonic()
        self._task = asyncio.create_task(self._run())
        log_hub.push("diag", f"Pipeline diagnostics {'ON' if self.enabled else 'OFF'} "
                             f"(stall threshold {self._settings.stall_ms} ms)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.monitor.detach()
        self._attached_generation = None
        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []
        log_hub.push("diag", "Pipeline diagnostics stopped")
        log_hub.close_file()

    async def on_settings_changed(self, new_settings: DiagnosticsSettings) -> None:
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------ bus handlers

    async def _on_ivr_stats(self, data: dict):
        self._ivr_stats = data or {}
        self._ivr_stats_at = time.monotonic()

    async def _on_ivr_disconnected(self, data: dict):
        if self._ivr_stats and self._ivr_stats.get("recording"):
            log_hub.push("diag", "IVR disconnected while recording — IVR stages unknown from now",
                         level="error")
        self._ivr_stats = None
        self._ivr_stats_at = None

    # ------------------------------------------------------------------ loop

    async def _run(self):
        while True:
            try:
                self._tick()
            except Exception as exc:
                self.logger.error("Diagnostics tick failed", exception=exc)
            await asyncio.sleep(self.TICK_S)

    def _core(self):
        pm = self.core_context.get("pipeline_manager")
        return pm, (getattr(pm, "_plugin", None) if pm else None)

    def _sources(self) -> dict[int, str]:
        try:
            cam = self.core_context["settings_manager"].get_settings(self._GSTREAMER_SETTINGS_NAME)
        except Exception:
            return {}
        sources = {0: "Scoreboard"} if getattr(cam, "is_scoreboard", False) else {}
        for i in range(1, len(getattr(cam, "camera_ips", [])) + 1):
            sources[i] = f"Camera {i}"
        return sources

    def _tick(self):
        if not self.enabled:
            if self.monitor.pipeline is not None:
                self.monitor.detach()
                self._attached_generation = None
            return

        pm, core = self._core()
        pipeline = pm.pipeline if pm else None
        generation = getattr(core, "pipeline_generation", None)

        if pipeline is None:
            if self.monitor.pipeline is not None:
                self.monitor.detach()
                self._attached_generation = None
                log_hub.push("diag", "Pipeline is not running — nothing to monitor", level="warning")
            return

        if pipeline is not self.monitor.pipeline or generation != self._attached_generation:
            notes = self.monitor.attach(pipeline, self._sources())
            self._attached_generation = generation
            log_hub.push("diag", f"Probes attached to pipeline #{generation} "
                                 f"({', '.join(st.label for st in self.monitor.sources.values()) or 'no sources'})")
            for note in notes:
                log_hub.push("diag", note, level="debug")

        age = None if self._ivr_stats_at is None else time.monotonic() - self._ivr_stats_at
        for level, msg in self.monitor.evaluate(self._ivr_stats, age):
            log_hub.push("diag", msg, level=level)
            if level == "error":
                self.logger.error(msg)
            elif level == "warning":
                self.logger.warning(msg)

        interval = self._settings.summary_interval_s if self._settings else 0
        now = time.monotonic()
        if interval and now - self._last_summary >= interval:
            self._last_summary = now
            for line in self.monitor.summary_lines():
                log_hub.push("stats", line)

    # ------------------------------------------------------------------ API

    def status(self) -> dict:
        pm, core = self._core()
        started = getattr(core, "pipeline_started_at", None)
        age = None if self._ivr_stats_at is None else round(time.monotonic() - self._ivr_stats_at, 1)
        return {
            "enabled": self.enabled,
            "running": bool(pm and pm.pipeline is not None),
            "generation": getattr(core, "pipeline_generation", None),
            "pipeline_age_s": round(time.time() - started, 1) if started else None,
            "stall_ms": self.monitor.stall_ms,
            "ivr": {
                "connected": bool(getattr(self.core_context.get("ivr_plugin"), "_connected", False)),
                "recording": bool(self._ivr_stats and self._ivr_stats.get("recording")),
                "stats_age_s": age,
            },
            "file": str(log_hub.file_path) if log_hub.file_path else None,
            "sources": self.monitor.status() if self.enabled else [],
        }

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/diagnostics", tags=["Diagnostics"])

        @router.get("/status")
        async def status():
            return self.status()

        @router.post("/enable")
        async def enable(on: bool = True):
            """Runtime toggle (not persisted; the setting decides the state at start)."""
            if on != self.enabled:
                self.enabled = on
                log_hub.push("diag", f"Pipeline diagnostics turned {'ON' if on else 'OFF'} from the dashboard")
            return {"enabled": self.enabled}

        return router