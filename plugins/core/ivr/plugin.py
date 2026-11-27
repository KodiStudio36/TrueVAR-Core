import asyncio
import json
from typing import Optional

import websockets
import websockets.exceptions
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.log_hub import log_hub
from core.use_cases.application_bootstrapper import ApplicationBootstrapper
from plugins.base_plugin import BasePlugin
from plugins.core.gstreamer_core.plugin import GStreamerCorePlugin, SHM_HEIGHT, SHM_FPS
from plugins.core.ivr.settings import IVRSettings


class IVRPlugin(BasePlugin):
    """Bridge to the IVR (instant video replay) app over WebSocket.

    Recording lifecycle, driven by the scoreboard:
        scoreboard.NEW_FIGHT   match loaded      -> start_recording {match_id}
        scoreboard.START_FIGHT fallback only: start if the IVR is not recording
                               a match that is still on (core/IVR restarted
                               mid-match, NEW_FIGHT missed)
        scoreboard.WIN         winner decided    -> stop_recording {delay_s, match_id}
                               (the IVR app runs the delay itself)

    Recording protection: before the GStreamer core stops its pipeline (rebuild,
    settings change, restart) it awaits gstreamer.pipeline.stopping; we tell the
    IVR to finalize its files (suspend_recording) and wait for its ack. On
    gstreamer.pipeline.started the recording resumes as a new segment.
    """

    _RECONNECT_DELAY_MIN = 1.0
    _RECONNECT_DELAY_MAX = 30.0

    # Settings key of GStreamerCorePlugin (its `name` property). The IVR app
    # needs CameraSettings.is_scoreboard to know whether a scoreboard
    # capture source (camera0 shm socket) exists to record from.
    _GSTREAMER_SETTINGS_NAME = "GStreamer Core Plugin"

    _SUSPEND_ACK_TIMEOUT = 6.0   # IVR waits up to 3 s for EOS per stop
    _TERMINATE_TIMEOUT = 8.0     # IVR finalizes its recording on SIGTERM

    _CMD_EVENTS = {
        "ivr.cmd.start_recording":  "start_recording",
        "ivr.cmd.stop_recording":   "stop_recording",
        "ivr.cmd.toggle_recording": "toggle_recording",
        "ivr.cmd.show_replay":      "show_replay",
        "ivr.cmd.hide_replay":      "hide_replay",
        "ivr.cmd.show_settings":    "show_settings",
        "ivr.cmd.hide_settings":    "hide_settings",
    }

    # Every inbound IVR event is published as ivr.<event> (e.g.
    # ivr.toggle_external_screen, listened to by the external screen plugin).
    # Some are additionally translated into another plugin's bus command:
    #   livestream_closeup   → livestream plugin, data {"active": bool}
    _INBOUND_COMMANDS = {
        "livestream_closeup": "livestream.cmd.closeup",
    }

    # High-rate IVR -> core events: not written to the core log / dashboard as events.
    #   diagnostics → per-source frame stats (Pipeline Diagnostics plugin)
    #   log         → IVR app log records, shown on the dashboard as source "ivr-app"
    _QUIET_INBOUND = frozenset({"diagnostics", "log"})

    _HTTP_ALLOWED = frozenset({
        "start_recording", "stop_recording", "toggle_recording",
        "show_replay", "hide_replay", "show_settings", "hide_settings",
    })

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._settings: Optional[IVRSettings] = None
        self._ws = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._connected = False
        self._subscriptions: list[tuple] = []
        self._bootstrapper = ApplicationBootstrapper(logger)

        self.is_recording = False
        self.fight_num: Optional[str] = None
        # Scoreboard match currently loaded, and the last one that got a winner.
        self._match_id: Optional[str] = None
        self._ended_match_id: Optional[str] = None
        # Pipeline-rebuild protection (see _on_pipeline_stopping)
        self._suspended_for_rebuild = False
        self._suspend_ack: Optional[asyncio.Event] = None
        self._stopping = False
        # Last known external-screen mirror state, re-sent on every (re)connect
        # so the IVR "Mirroring the screen" badge is right after an IVR restart.
        self._is_mirror = False

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str: return "Instant Video Replay"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return IVRSettings

    dependencies = ["GStreamerCorePlugin"]

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._stopping = False
        self._settings = self.core_context["settings_manager"].get_settings(self.name)
        self.core_context["ivr_plugin"] = self

        cmd = ["python", self._settings.app_path]
        asyncio.create_task(
            self._bootstrapper.boot(
                cmd=cmd,
                window_class=self._settings.window_class,
                workspace=self._settings.workspace,
                startup_delay=3.0
            )
        )

        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                # Wire up every ivr.cmd.* event to the matching WS send
                *[(bus_ev, self._make_cmd_handler(ws_ev))
                  for bus_ev, ws_ev in self._CMD_EVENTS.items()],
                # Recording lifecycle: match load -> start, winner (+delay) -> stop
                ("scoreboard.NEW_FIGHT", self._on_new_fight),
                ("scoreboard.START_FIGHT", self._on_fight_started),
                ("scoreboard.WIN", self._on_win),
                ("scoreboard.IVR", self._on_ivr_requested),
                # Finalize the recording before the core pipeline (shm) goes down
                ("gstreamer.pipeline.stopping", self._on_pipeline_stopping),
                ("gstreamer.pipeline.started", self._on_pipeline_started),
                # Scoreboard source toggled in camera settings — re-push so the
                # IVR app starts/stops recording the scoreboard accordingly.
                ("gstreamer.settings_changed", self._on_gstreamer_settings_changed),
                # External screen mirror state → IVR (badge + closeup logic).
                ("screen.mirror_changed", self._on_mirror_changed),
            ]
            bus.subscribe_many(self._subscriptions)

        self.logger.info(
            "IVR plugin starting",
            target=f"ws://{self._settings.ws_host}:{self._settings.ws_port}",
        )
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        self._stopping = True
        # Wait for the app to exit: on SIGTERM it finalizes the running
        # recording (EOS -> AVI index), and a restart must not start a second
        # instance while the old one still holds the port and the records.
        await self._bootstrapper.terminate_and_wait(self._TERMINATE_TIMEOUT)

        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        ws = self._ws
        await self._mark_disconnected("plugin stopped")
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        self.logger.warning("IVR plugin stopped.")

    async def on_settings_changed(self, new_settings: IVRSettings) -> None:
        # stop() terminates the IVR app and drops every bus subscription, so a
        # plain reconnect left the plugin deaf to all ivr.cmd.* / screen events.
        # Do a full restart instead (start() re-reads the new settings).
        self.logger.info("IVR settings changed — restarting IVR.")
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------ connection

    async def _reconnect_loop(self):
        delay = self._RECONNECT_DELAY_MIN
        uri = f"ws://{self._settings.ws_host}:{self._settings.ws_port}"

        while True:
            try:
                self.logger.info(f"Connecting to IVR at {uri} …")
                async with websockets.connect(uri) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = self._RECONNECT_DELAY_MIN

                    self.logger.info("IVR connection established.")
                    log_hub.push("ivr", f"Connected to IVR app at {uri}")
                    await self._push_settings()
                    await self.send_event("mirror_changed", {"is_mirror": self._is_mirror})
                    await self._listen(ws)

                # IVR closed the connection cleanly (app quit / restarted).
                # Previously the loop reconnected straight away while still
                # marked connected, and nobody learned the replay was gone.
                await self._mark_disconnected("connection closed")
                await asyncio.sleep(delay)

            except (ConnectionRefusedError, OSError,
                    websockets.exceptions.WebSocketException) as exc:
                await self._mark_disconnected(str(exc))
                self.logger.warning(f"IVR unreachable ({exc}). Retry in {delay:.0f}s.")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._RECONNECT_DELAY_MAX)

            except asyncio.CancelledError:
                return

    async def _mark_disconnected(self, reason: str) -> None:
        """Publishes ivr.disconnected once per lost connection, so plugins
        that follow the IVR state (livestream!) never stay stuck in a replay
        the IVR app can no longer close."""
        was_connected = self._connected
        self._connected = False
        self._ws = None
        if not was_connected:
            return
        was_recording = self.is_recording
        self.is_recording = False
        if self._suspend_ack is not None:
            self._suspend_ack.set()          # don't hold up a pipeline rebuild
        unexpected = was_recording and not self._stopping
        self.logger.warning(f"IVR disconnected ({reason}).")
        log_hub.push("ivr", f"IVR app disconnected ({reason})"
                     + (" WHILE RECORDING — app crashed/closed?" if unexpected else ""),
                     level="error" if unexpected else "warning")
        bus = self.core_context.get("event_bus")
        if bus:
            try:
                await bus.publish("ivr.disconnected", {"reason": reason})
            except Exception as exc:
                self.logger.error("Failed to publish ivr.disconnected", exception=exc)

    async def _listen(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self.logger.warning("IVR sent non-JSON", raw=repr(raw))
                continue

            if msg.get("type") == "event":
                await self._handle_inbound_event(msg.get("event", ""), msg.get("data", {}))

    # ------------------------------------------------------------------ inbound events

    async def _handle_inbound_event(self, event: str, data: dict):
        if event == "log":
            d = data or {}
            log_hub.push("ivr-app", d.get("msg", ""), level=d.get("level", "info"),
                         logger=d.get("logger"))
            return
        if event not in self._QUIET_INBOUND:
            self.logger.info(f"IVR → core: {event}", data=data)
            log_hub.push("ivr", f"IVR → core: {event}" + (f" {data}" if data else ""),
                         level="error" if event == "error" else "info")

        # Mirror state locally
        if event == "recording_started":
            self.is_recording = True
            self.fight_num = data.get("fight_num")
        elif event == "recording_stopped":
            self.is_recording = False
        elif event == "recording_suspended":
            if self._suspend_ack is not None:
                self._suspend_ack.set()
        elif event == "state":
            await self._on_ivr_state(data or {})
        elif event == "error":
            self.logger.error("IVR application error", message=data.get("message"))

        # Publish to the bus so every other plugin can react
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish(f"ivr.{event}", data)

            # Commands for other plugins (external screen, livestream…)
            command = self._INBOUND_COMMANDS.get(event)
            if command:
                await bus.publish(command, data or {})

        # Also forward to Socket.IO frontend if the broadcaster is up
        # broadcaster = self.core_context.get("overlay_broadcaster")
        # if broadcaster:
        #     await broadcaster.broadcast({"source": "ivr", "event": event, "data": data})

    # ------------------------------------------------------------------ bus-driven command handlers

    def _make_cmd_handler(self, ws_event: str):
        """Factory that creates a typed async handler for each ivr.cmd.* bus event."""
        async def _handler(data: dict):
            await self.send_event(ws_event)
        _handler.__name__ = f"_cmd_{ws_event}"
        return _handler

    # ------------------------------------------------------------------ recording lifecycle

    @staticmethod
    def _match_id_of(data: dict) -> Optional[str]:
        mid = (data or {}).get("match_id")
        return str(mid) if mid not in (None, "", 0, "0") else None

    def _match_is_on(self) -> bool:
        return self._match_id is not None and self._match_id != self._ended_match_id

    async def _on_new_fight(self, data: dict):
        """Match loaded on the scoreboard -> fresh recording for it. The IVR
        ignores a repeated NEW_FIGHT for the match it is already recording."""
        self._match_id = self._match_id_of(data)
        self.logger.info(f"Match {self._match_id} loaded — starting IVR recording.")
        log_hub.push("ivr", f"NEW_FIGHT #{self._match_id} -> start_recording")
        await self.send_event("start_recording", {"match_id": self._match_id})

    async def _on_fight_started(self, data: dict):
        """Fallback only: NEW_FIGHT normally started the recording already."""
        if self.is_recording or self._suspended_for_rebuild:
            return
        if self._match_id is not None and not self._match_is_on():
            log_hub.push("ivr", f"START_FIGHT: match {self._match_id} already ended, not recording",
                         level="warning")
            return
        log_hub.push("ivr", f"START_FIGHT while IVR not recording (NEW_FIGHT missed?) "
                            f"-> start_recording #{self._match_id}", level="warning")
        await self.send_event("start_recording", {"match_id": self._match_id})

    async def _on_win(self, data: dict):
        """Winner decided -> stop after stop_delay_s (timer runs in the IVR app).
        Sent even when not recording: a replay open at that moment must not
        resume recording when it closes."""
        match_id = self._match_id_of(data) or self._match_id
        self._ended_match_id = match_id
        delay = float(self._settings.stop_delay_s) if self._settings else 10.0
        log_hub.push("ivr", f"WIN #{match_id} -> stop_recording in {delay:.0f}s")
        await self.send_event("stop_recording", {"delay_s": delay, "match_id": match_id})

    async def _on_ivr_state(self, state: dict):
        """IVR (re)connected and reported what it is doing."""
        self.is_recording = bool(state.get("is_recording"))
        self.fight_num = state.get("fight_num") or self.fight_num
        ivr_match = state.get("match_id")
        # IVR restarted / was down at NEW_FIGHT while a match is on: record it.
        if (not self.is_recording and self._match_is_on()
                and state.get("screen") == "main" and not state.get("suspended")
                and ivr_match != self._match_id):
            log_hub.push("ivr", f"IVR connected idle during match #{self._match_id} -> start_recording",
                         level="warning")
            await self.send_event("start_recording", {"match_id": self._match_id})

    async def _on_pipeline_stopping(self, data: dict):
        """Awaited by the GStreamer core before its pipeline goes down: the IVR
        finalizes its files while the shm sockets still deliver frames."""
        if not self._connected or not self.is_recording:
            return
        self._suspend_ack = asyncio.Event()
        self._suspended_for_rebuild = True
        log_hub.push("ivr", "Core pipeline stopping -> suspend_recording (finalizing segment)",
                     level="warning")
        try:
            if await self.send_event("suspend_recording"):
                await asyncio.wait_for(self._suspend_ack.wait(), self._SUSPEND_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            log_hub.push("ivr", f"IVR did not confirm suspend within {self._SUSPEND_ACK_TIMEOUT:.0f}s "
                                f"— pipeline stops anyway", level="error")
        finally:
            self._suspend_ack = None

    async def _on_pipeline_started(self, data: dict):
        if not self._suspended_for_rebuild:
            return
        self._suspended_for_rebuild = False
        if not self._connected:
            return
        # Settings first (is_scoreboard may have changed with the rebuild), so
        # the new segment is built with them.
        await self._push_settings()
        log_hub.push("ivr", "Core pipeline started -> resume_recording (new segment)")
        await self.send_event("resume_recording")

    async def _on_ivr_requested(self, data: dict):
        """Open the IVR replay when the scoreboard signals a video review request.
        The IVR decides: it also opens the finished recording after the match
        ended, and refuses (with a toast) when there is nothing recorded."""
        if not self.is_recording:
            log_hub.push("ivr", "Scoreboard IVR request while not recording -> show_replay "
                                "(IVR opens the last recording if there is one)", level="warning")
        else:
            self.logger.info("IVR Requested — triggering IVR replay.")
            log_hub.push("ivr", "Scoreboard IVR request -> show_replay")
        await self.send_event("show_replay")

    async def _on_mirror_changed(self, data: dict):
        self._is_mirror = bool(data.get("is_mirror", False))
        if self._connected:
            await self.send_event("mirror_changed", {"is_mirror": self._is_mirror})

    async def _on_gstreamer_settings_changed(self, data: dict):
        if self._connected:
            await self._push_settings()

    # ------------------------------------------------------------------ outbound

    async def send_event(self, event: str, data: Optional[dict] = None) -> bool:
        if not self._ws or not self._connected:
            self.logger.warning(f"IVR not connected — dropped event '{event}'")
            log_hub.push("ivr", f"core → IVR: '{event}' DROPPED (IVR not connected)", level="error")
            return False
        try:
            await self._ws.send(json.dumps({"type": "event", "event": event, "data": data or {}}))
            log_hub.push("ivr", f"core → IVR: {event}" + (f" {data}" if data else ""))
            return True
        except Exception as exc:
            self.logger.error(f"Failed to send IVR event '{event}'", exception=exc)
            log_hub.push("ivr", f"core → IVR: '{event}' FAILED: {exc}", level="error")
            self._connected = False
            return False

    def _camera_count(self) -> int:
        """IVR camera_count, capped at the cameras the core actually streams
        (more would make the replay look for files that are never written)."""
        wanted = self._settings.camera_count
        try:
            cam_settings = self.core_context["settings_manager"].get_settings(
                self._GSTREAMER_SETTINGS_NAME
            )
            available = len(getattr(cam_settings, "camera_ips", []) or [])
        except Exception:
            return wanted
        if available and wanted > available:
            log_hub.push("ivr", f"IVR camera_count {wanted} > {available} core cameras — using {available}",
                         level="warning")
            return available
        return wanted

    def _is_scoreboard_enabled(self) -> bool:
        try:
            cam_settings = self.core_context["settings_manager"].get_settings(
                self._GSTREAMER_SETTINGS_NAME
            )
            return bool(getattr(cam_settings, "is_scoreboard", False))
        except Exception as exc:
            self.logger.warning("Could not read GStreamer is_scoreboard — assuming off", error=str(exc))
            return False

    async def _push_settings(self):
        if not self._settings or not self._ws:
            return
        payload = {
            # Must describe what the core writes to shm (not a preference).
            "fps":            SHM_FPS,
            "res_height":     SHM_HEIGHT,
            "camera_count":   self._camera_count(),
            "delete_records": self._settings.delete_records,
            "auto_record":    self._settings.auto_record,
            "is_scoreboard":  self._is_scoreboard_enabled(),
            "key_binds":      self._settings.key_binds.model_dump(),
        }
        try:
            await self._ws.send(json.dumps({"type": "settings", "data": payload}))
            self.logger.info("Settings pushed to IVR.", is_scoreboard=payload["is_scoreboard"])
            log_hub.push("ivr", f"core → IVR: settings (cameras={payload['camera_count']}, "
                                f"scoreboard={payload['is_scoreboard']}, fps={payload['fps']})")
        except Exception as exc:
            self.logger.error("Failed to push settings to IVR", exception=exc)

    # ------------------------------------------------------------------ HTTP router

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/ivr", tags=["IVR"])

        @router.get("/status")
        async def status():
            return {
                "connected":    self._connected,
                "is_recording": self.is_recording,
                "fight_num":    self.fight_num,
            }

        @router.post("/event/{event_name}")
        async def send_http_event(event_name: str):
            if event_name not in self._HTTP_ALLOWED:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown event '{event_name}'. Allowed: {sorted(self._HTTP_ALLOWED)}",
                )
            sent = await self.send_event(event_name)
            return {"event": event_name, "sent": sent}

        return router