import asyncio
from fastapi import APIRouter
from pydantic import BaseModel, Field
from plugins.base_plugin import BasePlugin, Namespace
from plugins.tkstrike.listener.gen3.listener import TkStrikeListener
from plugins.tkstrike.listener.gen3.settings import TkStrikeListenerSettings
from plugins.tkstrike.listener.gen3.history import ScoreboardHistory


class TkStrikeListenerGen2Plugin(BasePlugin):
    """Owns the hardware UDP socket and publishes all scoreboard events to the bus.

    No other plugin needs to know the listener exists — they subscribe to
    scoreboard.* events on the bus and receive data regardless of transport.

    It also builds a per-fight ScoreboardHistory timeline (see history.py) so
    the score/penalty/round/clock state at any point in a fight can be
    reconstructed later — this is what lets the IVR app show the correct
    scoreboard while replaying a video, instead of trying to capture it live.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.listener: TkStrikeListener | None = None
        self.history: ScoreboardHistory | None = None

    @property
    def name(self) -> str: return "TkStrike Gen2 Listener"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return TkStrikeListenerSettings

    namespace: str = Namespace.TKSTRIKE

    def get_history(self) -> ScoreboardHistory | None:
        """Other plugins (e.g. IVRPlugin) call this to grab the live timeline."""
        return self.history

    async def start(self) -> None:
        settings: TkStrikeListenerSettings = (
            self.core_context["settings_manager"].get_settings(self.name)
        )
        bus = self.core_context["event_bus"]
        history_dir = getattr(settings, "history_dir", "data/scoreboard_history")

        # Make this plugin discoverable in-process (mirrors IVRPlugin's pattern
        # of stashing itself on core_context so other plugins can reach it
        # without going through the bus).
        self.core_context["tkstrike_listener_plugin"] = self

        # Bridge: DaedoStateEngine calls these synchronously from an asyncio
        # DatagramProtocol, so asyncio.create_task is the safe dispatch path.
        def on_stable(data: dict):
            if self.history is not None:
                score_event = self.history.record(data)
                if score_event:
                    asyncio.create_task(bus.publish("scoreboard.score_event", score_event))
            asyncio.create_task(bus.publish("scoreboard.stable_update", data))

        def on_clock(clk: str):
            # Every clock tick (~1/sec while running, frozen while stopped)
            # is fed into the SAME timeline as score/penalty changes. This
            # is what lets replay reconstruct the exact displayed clock at
            # any moment — see history.record()'s docstring for why no
            # start/stop interpolation is needed: a stopped clock simply
            # stops producing new ticks, so the last recorded value just
            # keeps being the answer until it changes again.
            if self.history is not None:
                score_event = self.history.record(self.listener.parser.data)
                if score_event:
                    asyncio.create_task(bus.publish("scoreboard.score_event", score_event))
            asyncio.create_task(bus.publish("scoreboard.clock_update", {"clk": clk}))

        def on_event(event_name: str, data: dict={}):
            if event_name == "NEW_FIGHT":
                match_id = self.listener.parser.data["match_id"]
                self.history = ScoreboardHistory(match_id)
                self.logger.info(f"Scoreboard history timeline created for match #{match_id}")

            elif event_name == "START_FIGHT" and self.history is not None:
                self.history.start()

            elif event_name == "WIN" and self.history is not None:
                path = self.history.save(history_dir)
                self.logger.info(f"Scoreboard history saved -> {path}")
                asyncio.create_task(bus.publish("scoreboard.history_saved", {
                    "match_id": self.history.match_id,
                    "path": str(path),
                }))

            asyncio.create_task(bus.publish(f"scoreboard.{event_name}", data))

        self.listener = TkStrikeListener(
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

    async def on_settings_changed(self, new_settings: TkStrikeListenerSettings) -> None:
        self.logger.info("Listener settings changed — rebinding.")
        await self.stop()
        await self.start()

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/kyorugi-listener", tags=["Kyorugi Listener"])

        @router.get("/history/current")
        async def current_history():
            if not self.history:
                return {"match_id": None, "events": []}
            return {"match_id": self.history.match_id, "events": self.history.to_list()}

        return router