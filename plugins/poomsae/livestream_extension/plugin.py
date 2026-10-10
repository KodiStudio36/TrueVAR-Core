from fastapi import APIRouter
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as PydanticBody

from plugins.base_plugin import Namespace
from plugins.core.livestream.base_extension import BaseLivestreamExtension
from plugins.poomsae.livestream_extension.automation import PoomsaeStreamAutomation
from plugins.poomsae.livestream_extension.settings import PoomsaeLivestreamSettings

# Published by the Poomsae Control Panel plugin (plugins/poomsae_control).
LIVE_UPDATE_EVENT = "poomsae.live_update"
STAGE_CHANGED_EVENT = "poomsae.stage_changed"
LIVE_RESYNC_EVENT = "poomsae.live_resync"


class _BroadcastRequest(PydanticBody):
    message: str


class PoomsaeLivestreamExtension(BaseLivestreamExtension):
    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.automation: PoomsaeStreamAutomation | None = None
        self._subscriptions: list[tuple] = []

    @property
    def name(self) -> str: return "Poomsae Livestream Extension"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return PoomsaeLivestreamSettings

    dependencies = []

    # Same overlay socket namespace as kyorugi, so the overlay page connects
    # to the same /truevar endpoint. Swap if you add a dedicated value.
    namespace: str = Namespace.TKSTRIKE

    @property
    def obs_collection_name(self) -> str:
        return self.core_context["settings_manager"].get_settings(self.name).obs_collection

    @property
    def i3_workspace(self) -> int:
        return self.core_context["settings_manager"].get_settings(self.name).i3_workspace

    async def on_settings_changed(self, new_settings: PoomsaeLivestreamSettings) -> None:
        self.logger.info("Poomsae livestream config changed — restarting.")
        await self.stop()
        await self.start()

    async def setup_discipline(self) -> None:
        obs_adapter = self.core_context.get("obs_adapter")
        broadcaster = self.core_context.get("overlay_broadcaster")
        bus = self.core_context.get("event_bus")
        settings = self.core_context["settings_manager"].get_settings(self.name)

        self.automation = PoomsaeStreamAutomation(obs_adapter, broadcaster, settings, self.logger)

        if bus:
            self._subscriptions = [
                # Scoreboard data (from the Poomsae Control Panel plugin)
                (LIVE_UPDATE_EVENT,          self.automation.on_live_update),
                (STAGE_CHANGED_EVENT,        self.automation.on_stage_changed),
                # System lifecycle / operator
                ("system.livestream_start",  self.automation.on_livestream_start),
                ("system.message_broadcast", self.automation.on_message_broadcast),
            ]
            bus.subscribe_many(self._subscriptions)
            self.logger.info("Poomsae automation subscribed to event bus.")

            await bus.publish("system.livestream_start", {})
            # Ask the control panel for the current state right away.
            await bus.publish(LIVE_RESYNC_EVENT, {})

    async def teardown_discipline(self) -> None:
        if self.automation:
            self.automation.shutdown()
        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []
            self.logger.info("Poomsae automation unsubscribed from event bus.")

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/poomsae", tags=["Poomsae Livestream"])

        @router.post("/resync", summary="Operator: re-send the scoreboard to the overlay")
        async def resync():
            bus = self.core_context.get("event_bus")
            if bus:
                await bus.publish(LIVE_RESYNC_EVENT, {})
            return {"triggered": True}

        @router.post("/broadcast-message", summary="Operator: show ticker message on overlay")
        async def broadcast_message(body: _BroadcastRequest):
            bus = self.core_context.get("event_bus")
            if bus:
                await bus.publish("system.message_broadcast", {"message": body.message})
            return {"triggered": True, "message": body.message}

        # Same overlay folder as kyorugi — open /api/poomsae/overlay/poomsae.html
        router.mount("/overlay", StaticFiles(directory="ui/livestream", html=True), name="pms_overlay")
        return router