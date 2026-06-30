import os
from fastapi import APIRouter
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as PydanticBody

from plugins.livestream.core.base_extension import BaseLivestreamExtension
from plugins.livestream.kyorugi_tkstrike.automation import TaekwondoStreamAutomation
from plugins.livestream.kyorugi_tkstrike.settings import TaekwondoSettings


class _BroadcastRequest(PydanticBody):
    message: str


class TaekwondoLivestreamExtension(BaseLivestreamExtension):
    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.automation: TaekwondoStreamAutomation | None = None
        self._subscriptions: list[tuple] = []

    @property
    def name(self) -> str: return "livestream_taekwondo"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return TaekwondoSettings

    @property
    def obs_collection_name(self) -> str:
        return self.core_context["settings_manager"].get_settings(self.name).obs_collection

    @property
    def i3_workspace(self) -> int:
        return self.core_context["settings_manager"].get_settings(self.name).i3_workspace

    async def on_settings_changed(self, new_settings: TaekwondoSettings) -> None:
        self.logger.info("Taekwondo livestream config changed — restarting.")
        await self.stop()
        await self.start()

    async def setup_discipline(self) -> None:
        obs_adapter  = self.core_context.get("obs_adapter")
        broadcaster  = self.core_context.get("overlay_broadcaster")
        bus          = self.core_context.get("event_bus")
        settings     = self.core_context["settings_manager"].get_settings(self.name)

        self.automation = TaekwondoStreamAutomation(obs_adapter, broadcaster, settings, self.logger)

        if bus:
            self._subscriptions = [
                # Scoreboard data
                ("scoreboard.stable_update",  self.automation.on_stable_update),
                ("scoreboard.clock_update",   self.automation.on_clock_update),
                # Fight lifecycle (all Daedo state engine events)
                ("scoreboard.NEW_FIGHT",      self.automation.on_new_fight),
                ("scoreboard.START_FIGHT",    self.automation.on_start_fight),
                ("scoreboard.START_ROUND",    self.automation.on_start_round),
                ("scoreboard.START_BREAK",    self.automation.on_start_break),
                ("scoreboard.WIN",            self.automation.on_win),
                # IVR application state
                ("ivr.replay_opened",         self.automation.on_ivr_replay_opened),
                ("ivr.replay_closed",         self.automation.on_ivr_replay_closed),
                ("ivr.recording_started",     self.automation.on_ivr_recording_started),
                # External screen
                ("screen.mirror_changed",     self.automation.on_screen_mirror_changed),
                # System lifecycle — fired on plugin boot and via REST
                ("system.livestream_start",   self.automation.on_livestream_start),
                ("system.tournament_start",   self.automation.on_tournament_start),
                # Operator commands
                ("system.message_broadcast",  self.automation.on_message_broadcast),
                ("system.troubleshoot",       self.automation.on_troubleshoot),
            ]
            bus.subscribe_many(self._subscriptions)
            self.logger.info("Taekwondo automation subscribed to event bus.")

            # Subscriptions are in place — fire the initial OBS setup immediately
            await bus.publish("system.livestream_start", {})

    async def teardown_discipline(self) -> None:
        # Cancel the YouTube loop before unsubscribing so no ghost tasks remain
        if self.automation and self.automation._youtube_loop_task:
            if not self.automation._youtube_loop_task.done():
                self.automation._youtube_loop_task.cancel()

        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []
            self.logger.info("Taekwondo automation unsubscribed from event bus.")

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/taekwondo", tags=["Taekwondo Livestream"])

        @router.post("/tournament-start", summary="Operator: start session / pre-pre-fight sequence")
        async def tournament_start():
            bus = self.core_context.get("event_bus")
            if bus:
                await bus.publish("system.tournament_start", {})
            return {"triggered": True}

        @router.post("/broadcast-message", summary="Operator: show ticker message on overlay")
        async def broadcast_message(body: _BroadcastRequest):
            bus = self.core_context.get("event_bus")
            if bus:
                await bus.publish("system.message_broadcast", {"message": body.message})
            return {"triggered": True, "message": body.message}

        @router.post("/troubleshoot", summary="Operator: switch to troubleshoot scene")
        async def troubleshoot():
            bus = self.core_context.get("event_bus")
            if bus:
                await bus.publish("system.troubleshoot", {})
            return {"triggered": True}

        @router.post("/youtube-subscribe", summary="Operator: manually trigger subscribe widget")
        async def youtube_subscribe():
            if self.automation:
                self.automation.trigger_youtube_subscribe()
            return {"triggered": True}

        static_dir = os.path.join(os.path.dirname(__file__), "static")
        router.mount("/overlay", StaticFiles(directory=static_dir, html=True), name="tkd_overlay")
        return router