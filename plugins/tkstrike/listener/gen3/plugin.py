import asyncio
from fastapi import APIRouter
from pydantic import BaseModel, Field
from core.log_hub import log_hub
from plugins.base_plugin import BasePlugin, Namespace
from plugins.tkstrike.listener.gen3.listener import TkStrikeListener
from plugins.tkstrike.listener.gen3.settings import TkStrikeListenerSettings


def _event_summary(event_name: str, data) -> str:
    """One short line per scoreboard event for the dashboard log."""
    d = data if isinstance(data, dict) else {}
    if event_name == "NEW_FIGHT":
        return (f"NEW_FIGHT #{d.get('match_id')} {d.get('blue', {}).get('name', '?')} vs "
                f"{d.get('red', {}).get('name', '?')} ({d.get('category', '')})")
    if event_name in ("BLUE_SCORE", "RED_SCORE"):
        return f"{event_name} type {d.get('p_type')}"
    if event_name == "WIN":
        return f"WIN {str(d.get('win', '')).upper()} {d.get('win_score', '')} {d.get('win_type', '')}".strip()
    return event_name


class TkStrikeListenerGen3Plugin(BasePlugin):
    """Owns the hardware UDP socket and publishes all scoreboard events to the bus.

    No other plugin needs to know the listener exists — they subscribe to
    scoreboard.* events on the bus and receive data regardless of transport.

    Every raw UDP packet goes to the dashboard log (source "udp") and every
    parsed event (START_FIGHT, IVR, WIN, ...) as source "scoreboard", so IVR
    commands can be matched against what the scoring system actually sent.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.listener: TkStrikeListener | None = None

    @property
    def name(self) -> str: return "TkStrike Gen3 Listener"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return TkStrikeListenerSettings

    namespace: str = Namespace.TKSTRIKE

    async def start(self) -> None:
        settings: TkStrikeListenerSettings = (
            self.core_context["settings_manager"].get_settings(self.name)
        )
        bus = self.core_context["event_bus"]

        # Make this plugin discoverable in-process (mirrors IVRPlugin's pattern
        # of stashing itself on core_context so other plugins can reach it
        # without going through the bus).
        self.core_context["tkstrike_listener_plugin"] = self

        # Bridge: DaedoStateEngine calls these synchronously from an asyncio
        # DatagramProtocol, so asyncio.create_task is the safe dispatch path.
        def on_stable(data: dict):
            asyncio.create_task(bus.publish("scoreboard.stable_update", data))

        def on_clock(clk: str):
            asyncio.create_task(bus.publish("scoreboard.clock_update", {"clk": clk}))

        def on_event(event_name: str, data: dict | None = None):
            data = {} if data is None else data
            log_hub.push("scoreboard", _event_summary(event_name, data), event=event_name)
            asyncio.create_task(bus.publish(f"scoreboard.{event_name}", data))

        def on_raw(line: str):
            # Runs BEFORE the packet is parsed: it must never raise, or the
            # packet is lost (no START_FIGHT / IVR / WIN events).
            try:
                log_hub.push("udp", line)
            except Exception:
                pass

        def on_error(message: str):
            log_hub.push("udp", message, level="error")

        self.listener = TkStrikeListener(
            logger=self.logger,
            on_stable=on_stable,
            on_clock=on_clock,
            on_event=on_event,
        )
        ok = await self.listener.connect_to_hardware(
            settings.bind_host, settings.bind_port, log_callback=on_raw, error_callback=on_error)
        if ok:
            self.logger.info(f"Kyorugi listener active on {settings.bind_host}:{settings.bind_port}")
            log_hub.push("udp", f"Listening for TkStrike Gen3 on UDP {settings.bind_host}:{settings.bind_port}")

    async def stop(self) -> None:
        if self.listener:
            await self.listener.disconnect()
            self.listener = None
            log_hub.push("udp", "TkStrike Gen3 listener stopped", level="warning")

    async def on_settings_changed(self, new_settings: TkStrikeListenerSettings) -> None:
        self.logger.info("Listener settings changed — rebinding.")
        await self.stop()
        await self.start()

    def get_router(self) -> APIRouter:
        return APIRouter(prefix="/api/kyorugi-listener", tags=["Kyorugi Listener"])