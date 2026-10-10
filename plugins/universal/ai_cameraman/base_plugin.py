import asyncio
import os
import tempfile
from typing import ClassVar, Optional

from fastapi import APIRouter

from plugins.base_plugin import BasePlugin, Namespace, PluginState
from plugins.core.gstreamer_core.extensions.ai_cameraman_extension import AICameramanExtension
from plugins.universal.ai_cameraman.renderer_process import RendererProcess
from plugins.universal.ai_cameraman.settings import AICameramanBaseSettings


class AICameramanBasePlugin(BasePlugin):
    """Shared AI cameraman plugin. Subclassed per discipline:

        kyorugi/plugin.py  AICameramanKyorugiPlugin  — 2 fighters by kit, referee ignored
        poomsae/plugin.py  AICameramanPoomsaePlugin  — everyone on the square mat

    A subclass only sets `discipline`, `name` and `settings_schema`; the selection
    logic is picked in the detector process from settings.discipline.
    Only ONE AI cameraman (either discipline) can own the camera at a time.

    The core pipeline hands the camera's decoded 1080p stream (raw_srcN, before
    the 720p scale) to a separate renderer process over shared memory
    (AICameramanExtension). The renderer (engine.py) and its detector (worker.py)
    run outside the main app, so they can neither slow it down nor crash it.
    The framed 1280x720 NV12 output goes to its own shmsink socket
    (default <shm_base_dir>/camera_ai_shm_socket), same caps as the camera sockets.

    Bus events consumed:
        aicam.cmd.enable / aicam.cmd.disable / aicam.cmd.toggle

    Bus events emitted:
        aicam.activated   {"source_index": int, "socket": str}
        aicam.deactivated {}
    """

    dependencies = ["GStreamerCorePlugin"]
    namespace: str = Namespace.UNIVERSAL
    discipline: ClassVar[str] = "kyorugi"
    _owner: ClassVar[Optional[str]] = None          # name of the plugin currently owning the camera

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.is_active = False
        self._engine = None
        self._extension: Optional[AICameramanExtension] = None
        self._subscriptions: list[tuple] = []
        self._lock = asyncio.Lock()

    @property
    def requires_internet(self) -> bool: return False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                ("aicam.cmd.enable",  self._cmd_enable),
                ("aicam.cmd.disable", self._cmd_disable),
                ("aicam.cmd.toggle",  self._cmd_toggle),
            ]
            bus.subscribe_many(self._subscriptions)
        if self._settings().enabled:
            await self._enable()
        else:
            self.set_status(PluginState.IDLE, "Disabled")

    async def stop(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []
        await self._disable()
        self.set_status(PluginState.STOPPED)

    async def on_settings_changed(self, new_settings: AICameramanBaseSettings) -> None:
        self.logger.info("AI cameraman settings changed — restarting.")
        await self._disable()
        if new_settings.enabled:
            await self._enable()

    # ------------------------------------------------------------------ bus commands

    async def _cmd_enable(self, _: dict):
        await self._enable()

    async def _cmd_disable(self, _: dict):
        await self._disable()

    async def _cmd_toggle(self, _: dict):
        await (self._disable() if self.is_active else self._enable())

    # ------------------------------------------------------------------ enable / disable

    async def _enable(self) -> None:
        async with self._lock:
            if self.is_active:
                return
            owner = AICameramanBasePlugin._owner
            if owner and owner != self.name:
                self.set_status(PluginState.ERROR, f"'{owner}' already owns the camera — only one AI cameraman can run")
                self.logger.error(f"AI cameraman: '{owner}' is already running; '{self.name}' not started.")
                return
            s = self._settings()
            pm = self.core_context.get("pipeline_manager")
            if pm is None:
                self.set_status(PluginState.ERROR, "pipeline_manager not in context (GStreamer core not started)")
                return

            cams = len(getattr(self._core_settings(), "camera_ips", []) or [])
            if s.source_index > cams:
                self.logger.warning(
                    f"AI cameraman: source {s.source_index} requested but only {cams} IP camera(s) configured."
                )

            self.set_status(PluginState.STARTING, "Loading detector…")
            raw_socket = self._raw_socket()
            try:
                os.remove(raw_socket)                 # stale socket from a crash would block shmsink
            except OSError:
                pass
            out_socket = self._output_socket(s)

            # 1) Branch first. When we take over camera<N>_shm_socket, the OLD pipeline's
            #    shmsink must be gone before we bind it: shmsink deletes its socket file
            #    when it stops, which would otherwise delete ours.
            old_pipeline = pm.pipeline
            self._extension = AICameramanExtension(raw_socket, s.source_index, s.input_width,
                                                   s.input_height, replace_output=s.replace_camera_output,
                                                   decoder=s.decoder)
            if self._extension.decoder != self._extension.requested_decoder:
                self.logger.warning("AI cameraman: CPU decoder (avdec_h264) not installed — using the GPU "
                                    "decoder. Install gst-libav for the smoothest picture.")
            pm.register_extension(self._extension)
            await self._wait_rebuilt(pm, old_pipeline)

            # 2) Then the renderer (it publishes on out_socket and waits for the raw socket).
            dump = getattr(s, "model_dump_json", None) or s.json
            self._engine = RendererProcess(dump(), raw_socket, out_socket, self.logger)
            self._engine.start()
            if s.replace_camera_output:
                self.logger.info(f"AI cameraman: framed output replaces camera{s.source_index}_shm_socket")
            self.is_active = True
            AICameramanBasePlugin._owner = self.name

            asyncio.create_task(self._watch_ready())
            bus = self.core_context.get("event_bus")
            if bus:
                await bus.publish("aicam.activated",
                                  {"source_index": s.source_index, "socket": self._engine.output_socket,
                                   "discipline": self.discipline})

    async def _disable(self) -> None:
        async with self._lock:
            if not self.is_active:
                return
            # Renderer first: it releases camera<N>_shm_socket; then the core gets its
            # normal branch back (no-op if the core is already shutting down).
            if self._engine:
                engine, self._engine = self._engine, None
                await asyncio.get_running_loop().run_in_executor(None, engine.stop)
            pm = self.core_context.get("pipeline_manager")
            if pm and self._extension:
                pm.unregister_extension(self._extension.name)
            self._extension = None
            self.is_active = False
            if AICameramanBasePlugin._owner == self.name:
                AICameramanBasePlugin._owner = None
            self.set_status(PluginState.IDLE, "Disabled")
            bus = self.core_context.get("event_bus")
            if bus:
                await bus.publish("aicam.deactivated", {})

    async def _wait_rebuilt(self, pm, old_pipeline, timeout: float = 8.0) -> None:
        """Wait until the core has swapped in a new pipeline (debounced rebuild)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            current = pm.pipeline
            if current is not None and current is not old_pipeline:
                return
            await asyncio.sleep(0.05)
        self.logger.warning("AI cameraman: pipeline rebuild not observed in time — continuing.")

    async def _watch_ready(self) -> None:
        """Mirror the engine/detector state into the dashboard status (the detector
        runs in its own process and may be restarted, so keep watching)."""
        engine = self._engine
        last = None
        while engine is not None and engine is self._engine:
            st = engine.status
            state, error = st.get("state"), st.get("error")
            if state == "active" and not st.get("input_connected"):
                state, error = "waiting", "Waiting for camera frames…"
            now = (state, error)
            if now != last:
                last = now
                if state == "active":
                    self.set_status(PluginState.ACTIVE, f"Framing {self.discipline} ({st.get('backend')} on CPU)")
                elif state == "error":
                    self.set_status(PluginState.ERROR, error)
                elif state == "waiting":
                    self.set_status(PluginState.WAITING, error)
                else:
                    self.set_status(PluginState.STARTING, "Loading detector…")
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ helpers

    def _settings(self) -> AICameramanBaseSettings:
        return self.core_context["settings_manager"].get_settings(self.name)

    def _core_settings(self):
        try:
            return self.core_context["settings_manager"].get_settings("GStreamer Core Plugin")
        except Exception:
            return None

    def _shm_base(self) -> str:
        return getattr(self._core_settings(), "shm_base_dir", None) or "/tmp"

    def _output_socket(self, s: AICameramanBaseSettings) -> str:
        if s.replace_camera_output:
            return f"{self._shm_base()}/camera{s.source_index}_shm_socket"
        return s.output_socket or f"{self._shm_base()}/camera_ai_shm_socket"

    def _raw_socket(self) -> str:
        return f"{self._shm_base()}/aicam_raw_shm_socket"

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix=f"/api/ai-cameraman/{self.discipline}",
                           tags=[f"AI Cameraman ({self.discipline})"])

        @router.get("/status")
        async def status():
            return {"active": self.is_active, "discipline": self.discipline,
                    **(dict(self._engine.status) if self._engine else {})}

        @router.get("/debug.jpg")
        async def debug_image():
            """What the AI sees: mat outline (yellow), everyone detected, framed people (green)."""
            from fastapi import HTTPException
            from fastapi.responses import FileResponse
            path = os.path.join(tempfile.gettempdir(), f"aicam_{self.discipline}_debug.jpg")
            if not os.path.exists(path):
                raise HTTPException(404, "No debug image yet — is the AI cameraman running?")
            return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        @router.get("/debug")
        async def debug_page():
            from fastapi.responses import HTMLResponse
            return HTMLResponse(
                "<!doctype html><title>AI cameraman debug</title>"
                "<body style='margin:0;background:#111;color:#ddd;font:14px sans-serif'>"
                "<img id=i style='width:100%;display:block'><pre id=s style='padding:8px'></pre><script>"
                "async function t(){document.getElementById('i').src='debug.jpg?'+Date.now();"
                "try{const r=await fetch('status');document.getElementById('s').textContent="
                "JSON.stringify(await r.json(),null,1)}catch(e){}}t();setInterval(t,2000)</script>")

        @router.post("/enable")
        async def enable():
            await self._enable()
            return {"active": self.is_active}

        @router.post("/disable")
        async def disable():
            await self._disable()
            return {"active": self.is_active}

        @router.post("/toggle")
        async def toggle():
            await (self._disable() if self.is_active else self._enable())
            return {"active": self.is_active}

        return router