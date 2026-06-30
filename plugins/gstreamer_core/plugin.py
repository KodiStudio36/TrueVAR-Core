import os
import gi
from fastapi import APIRouter
from plugins.base_plugin import BasePlugin
from plugins.gstreamer_core.settings import CameraSettings
from plugins.gstreamer_core.pipeline_manager import PipelineManager

gi.require_version("Gst", "1.0")
from gi.repository import Gst


class GStreamerCorePlugin(BasePlugin):
    """Owns the physical GStreamer pipeline.

    Pipeline structure per source:

        <source> → ... → tee name=tee_srcN
            tee_srcN. ! queue ! shmsink ...          ← fixed, always present
            tee_srcN. ! <extension branch A> ...     ← registered at runtime
            tee_srcN. ! <extension branch B> ...

    Other plugins add/remove branches via core_context["pipeline_manager"].
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.pipeline = None
        self._pipeline_manager: PipelineManager | None = None

        if not Gst.is_initialized():
            Gst.init(None)

    @property
    def name(self) -> str: return "gstreamer_core"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return CameraSettings

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        bus = self.core_context.get("event_bus")
        self._pipeline_manager = PipelineManager(self, bus, self.logger)
        self.core_context["pipeline_manager"] = self._pipeline_manager
        await self._start_pipeline()

    async def stop(self) -> None:
        await self._stop_pipeline()

    async def on_settings_changed(self, new_settings: CameraSettings) -> None:
        self.logger.info("Camera settings changed — rebuilding pipeline.")
        await self._stop_pipeline()
        await self._start_pipeline()

    # ------------------------------------------------------------------ called by PipelineManager

    async def rebuild_pipeline(self) -> None:
        ext_names = list(self._pipeline_manager._extensions.keys())
        self.logger.info(f"Rebuilding pipeline. Active extensions: {ext_names}")
        await self._stop_pipeline()
        await self._start_pipeline()

    # ------------------------------------------------------------------ internal pipeline control

    async def _start_pipeline(self) -> None:
        settings: CameraSettings = (
            self.core_context["settings_manager"].get_settings(self.name)
        )
        camera_count = len(settings.camera_ips)
        ext_count = len(self._pipeline_manager._extensions)

        self.logger.info(
            f"Starting GStreamer pipeline",
            cameras=camera_count,
            extensions=ext_count,
        )

        self._clean_sockets(camera_count, settings.shm_base_dir)
        pipeline_string = self._build_pipeline_string(settings)
        self.logger.info("Compiled pipeline", pipeline=pipeline_string)

        try:
            self.pipeline = Gst.parse_launch(pipeline_string)
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus_message)
            self.pipeline.set_state(Gst.State.PLAYING)
            self.logger.info("Pipeline PLAYING.")
        except Exception as exc:
            self.logger.error("Failed to start pipeline", exception=exc)

    async def _stop_pipeline(self) -> None:
        if self.pipeline:
            self.logger.warning("Stopping pipeline...")
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # ------------------------------------------------------------------ pipeline string builder

    def _build_pipeline_string(self, settings: CameraSettings) -> str:
        base_dir = settings.shm_base_dir
        parts: list[str] = []

        # ---- Source 0: scoreboard / master ----
        if settings.is_scoreboard:
            source = (
                f"v4l2src device=/dev/video{settings.scoreboard_device} ! "
                f"image/jpeg,width=1280,height=720,framerate=30/1"
            )
        else:
            source = "videotestsrc"

        parts.append(
            f"{source} ! jpegdec ! videoconvert ! "
            f"video/x-raw,width=1280,height=720,framerate=30/1,format=NV12 ! "
            f"tee name=tee_src0"
        )
        # Fixed branch: IVR shared memory
        parts.append(
            f"tee_src0. ! "
            f"queue max-size-buffers=30 max-size-bytes=0 max-size-time=0 leaky=upstream ! "
            f"shmsink socket-path={base_dir}/camera0_shm_socket "
            f"wait-for-connection=false shm-size=200000000 buffer-time=0"
        )
        # Extension branches for source 0
        parts.extend(self._pipeline_manager.get_branches_for_source(0, settings))

        # ---- Sources 1..n: IP cameras ----
        for idx, ip in enumerate(settings.camera_ips, start=1):
            cam_src = (
                "videotestsrc" if settings.debug else
                f"rtspsrc location=rtsp://admin:TaekwondoVAR@{ip}:554 latency=800 ! "
                f"rtph264depay ! h264parse ! vah264dec"
            )
            parts.append(
                f"{cam_src} ! vapostproc ! "
                f"video/x-raw,width=1280,height=720,framerate=30/1,format=NV12 ! "
                f"queue ! tee name=tee_src{idx}"
            )
            parts.append(
                f"tee_src{idx}. ! queue ! "
                f"shmsink socket-path={base_dir}/camera{idx}_shm_socket "
                f"wait-for-connection=false shm-size=200000000"
            )
            parts.extend(self._pipeline_manager.get_branches_for_source(idx, settings))

        return " ".join(parts)

    # ------------------------------------------------------------------ helpers

    def _clean_sockets(self, count: int, base_dir: str) -> None:
        for i in range(count + 1):
            path = f"{base_dir}/camera{i}_shm_socket"
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _on_bus_message(self, bus, message) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            self.logger.error(f"GStreamer error: {err.message}", debug=dbg)
        elif t == Gst.MessageType.EOS:
            self.logger.info("GStreamer EOS.")

    def get_router(self) -> APIRouter:
        return APIRouter(prefix="/api/gstreamer", tags=["GStreamer Control"])