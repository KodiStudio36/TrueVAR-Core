import asyncio
import json
from typing import Optional

import websockets
import websockets.exceptions
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.use_cases.application_bootstrapper import ApplicationBootstrapper
from plugins.base_plugin import BasePlugin
from plugins.ivr.settings import IVRSettings


class IVRPlugin(BasePlugin):
    _RECONNECT_DELAY_MIN = 1.0
    _RECONNECT_DELAY_MAX = 30.0

    _CMD_EVENTS = {
        "ivr.cmd.start_recording":  "start_recording",
        "ivr.cmd.stop_recording":   "stop_recording",
        "ivr.cmd.toggle_recording": "toggle_recording",
        "ivr.cmd.show_replay":      "show_replay",
        "ivr.cmd.hide_replay":      "hide_replay",
        "ivr.cmd.show_settings":    "show_settings",
        "ivr.cmd.hide_settings":    "hide_settings",
    }

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

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str: return "instant_video_replay"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return IVRSettings

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
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
                # Cross-plugin: auto-start recording when a fight begins
                ("scoreboard.START_FIGHT", self._on_fight_started),
            ]
            bus.subscribe_many(self._subscriptions)

        self.logger.info(
            "IVR plugin starting",
            target=f"ws://{self._settings.ws_host}:{self._settings.ws_port}",
        )
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        self._bootstrapper.terminate()
        
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

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._connected = False
        self.logger.warning("IVR plugin stopped.")

    async def on_settings_changed(self, new_settings: IVRSettings) -> None:
        self.logger.info("IVR settings changed — reconnecting.")
        await self.stop()
        self._settings = new_settings
        self.core_context["ivr_plugin"] = self
        self._subscriptions = []
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

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
                    await self._push_settings()
                    await self._listen(ws)

            except (ConnectionRefusedError, OSError,
                    websockets.exceptions.WebSocketException) as exc:
                self._connected = False
                self._ws = None
                self.logger.warning(f"IVR unreachable ({exc}). Retry in {delay:.0f}s.")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._RECONNECT_DELAY_MAX)

            except asyncio.CancelledError:
                return

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
        self.logger.info(f"IVR → core: {event}", data=data)

        # Mirror state locally
        if event == "recording_started":
            self.is_recording = True
            self.fight_num = data.get("fight_num")
        elif event == "recording_stopped":
            self.is_recording = False
        elif event == "error":
            self.logger.error("IVR application error", message=data.get("message"))

        # Publish to the bus so every other plugin can react
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish(f"ivr.{event}", data)

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

    async def _on_fight_started(self, data: dict):
        """Auto-start IVR recording when the scoreboard signals a fight has begun."""
        if not self.is_recording:
            self.logger.info("Fight started — triggering IVR recording.")
            await self.send_event("start_recording")

    # ------------------------------------------------------------------ outbound

    async def send_event(self, event: str, data: Optional[dict] = None) -> bool:
        if not self._ws or not self._connected:
            self.logger.warning(f"IVR not connected — dropped event '{event}'")
            return False
        try:
            await self._ws.send(json.dumps({"type": "event", "event": event, "data": data or {}}))
            return True
        except Exception as exc:
            self.logger.error(f"Failed to send IVR event '{event}'", exception=exc)
            self._connected = False
            return False

    async def _push_settings(self):
        if not self._settings:
            return
        payload = {
            "fps":            self._settings.fps,
            "res_height":     self._settings.res_height,
            "camera_count":   self._settings.camera_count,
            "delete_records": self._settings.delete_records,
            "auto_record":    self._settings.auto_record,
            "key_binds":      self._settings.key_binds.model_dump(),
        }
        try:
            await self._ws.send(json.dumps({"type": "settings", "data": payload}))
            self.logger.info("Settings pushed to IVR.")
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