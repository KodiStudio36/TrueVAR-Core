import os
import gi
from fastapi import APIRouter
from plugins.base_plugin import BasePlugin
from plugins.core.gstreamer_core.settings import CameraSettings
from plugins.core.gstreamer_core.pipeline_manager import PipelineManager

gi.require_version("Gst", "1.0")
from gi.repository import Gst


class GStreamerCorePlugin(BasePlugin):
    """Owns the physical GStreamer pipeline.

    Pipeline structure per source:

        <source> → ... → tee name=tee_srcN
            tee_srcN. ! queue ! shmsink ...          ← fixed, always present
            tee_srcN. ! <extension branch A> ...     ← registered at runtime
            tee_srcN. ! <extension branch B> ...

    IP cameras only, and only when an extension asks for it (get_raw_branches):

        rtspsrc ! ... ! vah264dec ! tee name=raw_srcN    ← native res, VAMemory
            raw_srcN. ! queue ! vapostproc ! 720p ! queue ! tee name=tee_srcN
            raw_srcN. ! <raw extension branch> ...

    Other plugins add/remove branches via core_context["pipeline_manager"].
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.pipeline = None
        self._pipeline_manager: PipelineManager | None = None

        if not Gst.is_initialized():
            Gst.init(None)

    @property
    def name(self) -> str: return "GStreamer Core Plugin"

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
        if self._pipeline_manager:
            self._pipeline_manager.deactivate()
        await self._stop_pipeline()

    async def on_settings_changed(self, new_settings: CameraSettings) -> None:
        self.logger.info("Camera settings changed — rebuilding pipeline.")
        await self._stop_pipeline()
        await self._start_pipeline()

        # Lets IVRPlugin re-push is_scoreboard to the IVR app, which decides
        # from it whether to record the scoreboard (camera0) source.
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("gstreamer.settings_changed", {
                "is_scoreboard": bool(getattr(new_settings, "is_scoreboard", False)),
            })

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

        replaced = self._pipeline_manager.get_replaced_outputs(settings)
        self._clean_sockets(camera_count, settings.shm_base_dir, keep=replaced)
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
        replaced = self._pipeline_manager.get_replaced_outputs(settings)
        for idx, ip in enumerate(settings.camera_ips, start=1):
            # A full URL (e.g. a test stream "rtsp://127.0.0.1:8554/cam1") is used as-is;
            # a plain IP keeps the real-camera URL exactly as before.
            url = ip if ip.startswith("rtsp://") else f"rtsp://admin:TaekwondoVAR@{ip}:554"
            decoder = self._pipeline_manager.get_decoder(idx, settings, "vah264dec")
            cam_src = (
                "videotestsrc" if settings.debug else
                f"rtspsrc location={url} latency=800 ! "
                f"rtph264depay ! h264parse ! {decoder}"
            )
            scaled = (
                f"vapostproc ! "
                f"video/x-raw,width=1280,height=720,framerate=30/1,format=NV12 ! "
                f"queue ! tee name=tee_src{idx}"
            )
            raw_branches = self._pipeline_manager.get_raw_branches_for_source(idx, settings)
            ext_branches = self._pipeline_manager.get_branches_for_source(idx, settings)
            is_replaced = idx in replaced
            # 720p path is needed for the fixed shm output and/or 720p extension branches
            need_scaled = not is_replaced or bool(ext_branches) or not raw_branches
            if raw_branches:
                # Split the decoded native-resolution stream BEFORE the 720p scale.
                # The 720p path keeps back-pressure (non-leaky); raw extension
                # branches are expected to be leaky so they can never stall it.
                parts.append(f"{cam_src} ! tee name=raw_src{idx}")
                if need_scaled:
                    parts.append(
                        f"raw_src{idx}. ! "
                        f"queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 ! "
                        f"{scaled}"
                    )
                parts.extend(raw_branches)
            else:
                # Unchanged from before: identical string when no raw branch exists.
                parts.append(f"{cam_src} ! {scaled}")
            if not is_replaced:
                parts.append(
                    f"tee_src{idx}. ! queue ! "
                    f"shmsink socket-path={base_dir}/camera{idx}_shm_socket "
                    f"wait-for-connection=false shm-size=200000000"
                )
            elif need_scaled and not ext_branches:
                # replaced but nothing consumes the 720p tee: terminate it cleanly
                parts.append(f"tee_src{idx}. ! queue leaky=downstream max-size-buffers=1 ! "
                             f"fakesink sync=false async=false")
            parts.extend(ext_branches)

        return " ".join(parts)

    # ------------------------------------------------------------------ helpers

    def _clean_sockets(self, count: int, base_dir: str, keep: set[int] = frozenset()) -> None:
        for i in range(count + 1):
            if i in keep:          # owned by an extension's process (e.g. AI cameraman): live socket
                continue
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