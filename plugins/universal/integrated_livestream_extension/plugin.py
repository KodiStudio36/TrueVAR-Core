import os
from sqlite3 import dbapi2
from fastapi import APIRouter
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as PydanticBody

from plugins.base_plugin import Namespace
from plugins.core.livestream.base_extension import BaseLivestreamExtension
from plugins.universal.integrated_livestream_extension.settings import UniversalIntegratedLivestreamSettings


class UniversalIntegratedLivestreamExtension(BaseLivestreamExtension):
    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._subscriptions: list[tuple] = []

    @property
    def name(self) -> str: return "Universal Integrated Livestream Extension"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return UniversalIntegratedLivestreamSettings

    dependencies = []

    namespace: str = Namespace.UNIVERSAL

    @property
    def obs_collection_name(self) -> str:
        return self.core_context["settings_manager"].get_settings(self.name).obs_collection

    @property
    def i3_workspace(self) -> int:
        return self.core_context["settings_manager"].get_settings(self.name).i3_workspace

    async def on_settings_changed(self, new_settings: UniversalIntegratedLivestreamSettings) -> None:
        self.logger.info("Taekwondo livestream config changed — restarting.")
        await self.stop()
        await self.start()

    async def setup_discipline(self) -> None:
        obs_adapter  = self.core_context.get("obs_adapter")
        broadcaster  = self.core_context.get("overlay_broadcaster")
        bus          = self.core_context.get("event_bus")
        settings     = self.core_context["settings_manager"].get_settings(self.name)

        await bus.publish("system.livestream_start", {})

    async def teardown_discipline(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []
            self.logger.info("Taekwondo automation unsubscribed from event bus.")

    def get_router(self) -> APIRouter:
        return