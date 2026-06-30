import asyncio
from fastapi import APIRouter
from pydantic import BaseModel, Field
from plugins.base_plugin import BasePlugin
from plugins.tkstrike_listener.listener import TaekwondoScoreListener


class KyorugiListenerSettings(BaseModel):
    bind_host: str = Field(default="0.0.0.0", description="UDP bind address")
    bind_port: int = Field(default=8080,      description="Daedo UDP port")


class KyorugiListenerPlugin(BasePlugin):
    """Owns the hardware UDP socket and publishes all scoreboard events to the bus.

    No other plugin needs to know the listener exists — they subscribe to
    scoreboard.* events on the bus and receive data regardless of transport.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.listener: TaekwondoScoreListener | None = None

    @property
    def name(self) -> str: return "kyorugi_listener"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return KyorugiListenerSettings

    async def start(self) -> None:
        settings: KyorugiListenerSettings = (
            self.core_context["settings_manager"].get_settings(self.name)
        )
        bus = self.core_context["event_bus"]

        # Bridge: DaedoStateEngine calls these synchronously from an asyncio
        # DatagramProtocol, so asyncio.create_task is the safe dispatch path.
        def on_stable(data: dict):
            asyncio.create_task(bus.publish("scoreboard.stable_update", data))

        def on_clock(clk: str):
            asyncio.create_task(bus.publish("scoreboard.clock_update", {"clk": clk}))

        def on_event(event_name: str):
            asyncio.create_task(bus.publish(f"scoreboard.{event_name}", {}))

        self.listener = TaekwondoScoreListener(
            logger=self.logger,
            on_stable=on_stable,
            on_clock=on_clock,
            on_event=on_event,
        )
        await self.listener.connect_to_hardware(settings.bind_host, settings.bind_port)
        self.logger.info(f"Kyorugi listener active on {settings.bind_host}:{settings.bind_port}")

    async def stop(self) -> None:
        if self.listener:
            await self.listener.disconnect()
            self.listener = None

    async def on_settings_changed(self, new_settings: KyorugiListenerSettings) -> None:
        self.logger.info("Listener settings changed — rebinding.")
        await self.stop()
        await self.start()

    def get_router(self) -> APIRouter:
        return APIRouter(prefix="/api/kyorugi-listener", tags=["Kyorugi Listener"])