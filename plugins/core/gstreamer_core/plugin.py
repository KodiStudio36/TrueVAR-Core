import asyncio
import os
import re
import time
import gi
from fastapi import APIRouter
from core.log_hub import log_hub
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

    Stable element names (used by the Pipeline Diagnostics plugin's probes):
        camN_in     first element carrying frames from the camera
                    (v4l2src for 0, h264parse for IP cameras, videotestsrc in debug)
        tee_srcN    decoded + scaled 720p NV12 frames (sink pad = "processed")
        camN_shmq   queue in front of the IVR shmsink (fill level = back-pressure)
        camN_shm    the shmsink itself (sink pad = frames written to shared memory)
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.pipeline = None
        self._pipeline_manager: PipelineManager | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.pipeline_started_at: float | None = None   # time.time() of the last PLAYING
        self.pipeline_generation = 0                     # +1 per (re)start

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
        self._loop = asyncio.get_running_loop()
        bus = self.core_context.get("event_bus")
        if self._pipeline_manager is None:
            self._pipeline_manager = PipelineManager(self, bus, self.logger)
        else:
            # Dashboard "Restart" reuses this instance: keep the manager so the
            # extensions other plugins registered (external screen, livestream,
            # AI cameraman…) survive the restart instead of silently vanishing.
            self._pipeline_manager.activate()
        self.core_context["pipeline_manager"] = self._pipeline_manager
        await self._start_pipeline()
        if bus and self._pipeline_manager._extensions:
            # Same notification a normal rebuild sends (window placement etc.).
            await bus.publish(
                "gstreamer.pipeline.rebuilt",
                {"extensions": list(self._pipeline_manager._extensions.keys())},
            )

    async def stop(self) -> None:
        if self._pipeline_manager:
            self._pipeline_manager.deactivate()
        await self._stop_pipeline()

    async def on_settings_changed(self, new_settings: CameraSettings) -> None:
        self.logger.info("Camera settings changed — rebuilding pipeline.")
        log_hub.push("pipeline", "Camera settings changed -> rebuilding pipeline", level="warning")
        await self._stop_pipeline()
        await self._start_pipeline()

        # Lets IVRPlugin re-push is_scoreboard to the IVR app, which decides
        # from it whether to record the scoreboard (camera0) source.
        bus = self.core_context.get("event_bus")
        if bus:
            # New pipeline = new xvimagesink window, so listeners must re-place it.
            await bus.publish(
                "gstreamer.pipeline.rebuilt",
                {"extensions": list(self._pipeline_manager._extensions.keys())},
            )
            await bus.publish("gstreamer.settings_changed", {
                "is_scoreboard": bool(getattr(new_settings, "is_scoreboard", False)),
            })

    # ------------------------------------------------------------------ called by PipelineManager

    async def rebuild_pipeline(self) -> None:
        ext_names = list(self._pipeline_manager._extensions.keys())
        self.logger.info(f"Rebuilding pipeline. Active extensions: {ext_names}")
        log_hub.push("pipeline", f"Rebuilding pipeline (extensions: {ext_names or 'none'})",
                     level="warning", extensions=ext_names)
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
            # Errors/warnings also synchronously: they reach the dashboard log even
            # when no GLib main loop is dispatching the signal watch above.
            bus.enable_sync_message_emission()
            bus.connect("sync-message", self._on_sync_message)
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            self.pipeline_started_at = time.time()
            self.pipeline_generation += 1
            if ret == Gst.StateChangeReturn.FAILURE:
                self.logger.error("Pipeline failed to go to PLAYING.")
                log_hub.push("pipeline", "Pipeline FAILED to start (see GStreamer error above)", level="error")
            else:
                self.logger.info("Pipeline PLAYING.")
                log_hub.push("pipeline", f"Pipeline started #{self.pipeline_generation}: "
                             f"{camera_count} cameras, scoreboard={'on' if settings.is_scoreboard else 'off'}, "
                             f"extensions={list(self._pipeline_manager._extensions.keys()) or 'none'}")
        except Exception as exc:
            self.logger.error("Failed to start pipeline", exception=exc)
            log_hub.push("pipeline", f"Failed to build pipeline: {exc}", level="error")

    async def _stop_pipeline(self) -> None:
        if self.pipeline:
            self.logger.warning("Stopping pipeline...")
            ivr = self.core_context.get("ivr_plugin")
            recording = bool(getattr(ivr, "is_recording", False))
            log_hub.push(
                "pipeline",
                "Stopping pipeline: ALL video outputs go down; the IVR shm sockets close"
                + (" -> the running IVR recording loses its cameras from here!" if recording else ""),
                level="error" if recording else "warning",
            )
            bus = self.pipeline.get_bus()
            try:
                bus.disable_sync_message_emission()
            except Exception:
                pass
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # ------------------------------------------------------------------ pipeline string builder

    def _build_pipeline_string(self, settings: CameraSettings) -> str:
        base_dir = settings.shm_base_dir
        parts: list[str] = []

        # ---- Source 0: scoreboard / master ----
        if settings.is_scoreboard:
            source = (
                f"v4l2src device=/dev/video{settings.scoreboard_device} name=cam0_in ! "
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
                f"queue name=cam0_shmq max-size-buffers=30 max-size-bytes=0 max-size-time=0 leaky=upstream ! "
                f"shmsink name=cam0_shm socket-path={base_dir}/camera0_shm_socket "
                f"wait-for-connection=false shm-size=200000000 buffer-time=0 sync=false"
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
            # Debug source must be live: a non-live videotestsrc runs as fast as the
            # CPU allows and was only held to 30 fps by back-pressure from the
            # (formerly blocking) shm queue. With the leaky queue it raced ahead,
            # its timestamps ran into the future and the shmsink starved.
            cam_src = (
                f"videotestsrc is-live=true name=cam{idx}_in" if settings.debug else
                f"rtspsrc name=cam{idx}_rtsp location={url} latency=800 ! "
                f"rtph264depay ! h264parse name=cam{idx}_in ! {decoder}"
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
                parts.append(f"{cam_src} ! {scaled}")
            if not is_replaced:
                # Leaky: shmsink blocks when its shm area is full, i.e. when a
                # client (IVR recording / IVR settings preview) stops releasing
                # buffers. A blocking queue here then stalled tee_srcN and froze
                # this camera on EVERY output (livestream, external screen, ...).
                # Now only the shm output drops frames (counted as overruns).
                #
                # sync=false on every shmsink: with sync=true each frame was held
                # until timestamp + pipeline latency (rtspsrc latency=800 -> ~0.8 s).
                # The 10-frame leaky queue holds only 0.33 s, so it dropped almost
                # everything and the shm got ~2 fps (OBS + IVR choppy). The readers
                # (IVR shmsrc do-timestamp=true, OBS) timestamp frames on arrival,
                # so syncing here only added delay. Bonus: 0.8 s less lag for them.
                parts.append(
                    f"tee_src{idx}. ! "
                    f"queue name=cam{idx}_shmq leaky=downstream max-size-buffers=10 "
                    f"max-size-bytes=0 max-size-time=0 ! "
                    f"shmsink name=cam{idx}_shm socket-path={base_dir}/camera{idx}_shm_socket "
                    f"wait-for-connection=false shm-size=200000000 sync=false"
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

    def _on_sync_message(self, bus, message) -> None:
        """Runs on the GStreamer thread that posted the message."""
        t = message.type
        if t not in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
            return
        src = message.src.get_name() if message.src else "?"
        err, dbg = message.parse_error() if t == Gst.MessageType.ERROR else message.parse_warning()
        level = "error" if t == Gst.MessageType.ERROR else "warning"
        cam = self._camera_of(message.src)
        where = f"{'Scoreboard' if cam == 0 else f'Camera {cam}'} " if cam is not None else ""
        log_hub.push("gst", f"{where}[{src}] {err.message}", level=level, debug=dbg, camera=cam)

        bus_ = self.core_context.get("event_bus")
        if bus_ and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                bus_.publish(f"gstreamer.{level}",
                             {"element": src, "camera": cam, "message": err.message, "debug": dbg}),
                self._loop,
            )

    _CAM_NAME = re.compile(r"(?:cam|tee_src|raw_src)(\d+)")

    @classmethod
    def _camera_of(cls, element):
        """Best effort: which source an element belongs to. Checks the element
        and its parents (rtspsrc internals), then walks upstream through the
        links (unnamed decoder / vapostproc / converters)."""
        def named(el):
            while isinstance(el, Gst.Element):
                m = cls._CAM_NAME.match(el.get_name() or "")
                if m:
                    return int(m.group(1))
                el = el.get_parent()
            return None

        el = element
        for _ in range(25):
            if not isinstance(el, Gst.Element):
                return None
            cam = named(el)
            if cam is not None:
                return cam
            sinks = el.sinkpads
            peer = sinks[0].get_peer() if sinks else None
            el = peer.get_parent_element() if peer else None
        return None

    def _on_bus_message(self, bus, message) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            self.logger.error(f"GStreamer error: {err.message}", debug=dbg)
        elif t == Gst.MessageType.EOS:
            self.logger.info("GStreamer EOS.")

    def get_router(self) -> APIRouter:
        return APIRouter(prefix="/api/gstreamer", tags=["GStreamer Control"])