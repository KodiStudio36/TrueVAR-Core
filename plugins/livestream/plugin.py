from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.adapters.obs_adapter import OBSAdapter
from core.domain.ports.broadcaster_port import BroadcasterPort
from core.domain.ports.overlay_broadcaster_port import OverlayBroadcaster
from plugins.base_plugin import BasePlugin
from plugins.livestream.settings import LivestreamCoreSettings

class LivestreamCorePlugin(BasePlugin):
    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.obs = None
        self.broadcaster = None
        self.templates = Jinja2Templates(directory="ui/templates")

    @property
    def name(self) -> str: return "livestream_core"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return LivestreamCoreSettings

    async def start(self) -> None:
        settings: LivestreamCoreSettings = self.core_context["settings_manager"].get_settings(self.name)
        self.logger.info("Starting Livestream Core Infrastructure...")
        
        # 1. Initialize OBS
        self.obs = OBSAdapter(
            host=settings.obs_host, port=settings.obs_port,
            password=settings.obs_password, logger=self.logger
        )

        sio = self.core_context["sio"]
        self.broadcaster = OverlayBroadcaster(sio)
        
        # 2. Expose Core Ports
        self.core_context["obs_adapter"] = self.obs
        self.core_context["overlay_broadcaster"] = self.broadcaster

        # 4. Resolve tournament + court from context
        tournament = self.core_context.get("tournament")
        court_id   = self.core_context.get("court_id")

        if not tournament or not court_id:
            self.logger.warning(
                "No tournament/court bound to this device — skipping stream key setup."
            )
            return

        # 5. Fetch the matching scheduled broadcast from Firebase
        db = self.core_context["db"]
        broadcast = await db.fetch_scheduled_broadcast(
            tournament_id=tournament.id,
            court_number=court_id,
        )

        if not broadcast:
            self.logger.warning(
                f"No scheduled_broadcast found for tournament={tournament.id} "
                f"court={court_id} — stream key NOT set."
            )
            return
        
        self.core_context["scheduled_broadcast"] = broadcast


    async def stop(self) -> None:
        if self.obs:
            self.obs.disconnect()
        self.logger.warning("Livestream Core Infrastructure stopped.")

    async def on_settings_changed(self, new_settings: LivestreamCoreSettings) -> None:
        self.logger.info("Livestream configuration modified. Reconnecting components...")
        await self.stop()
        await self.start()

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/livestream", tags=["Livestream Infrastructure"])
        return router