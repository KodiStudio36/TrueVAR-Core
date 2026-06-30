import asyncio
import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Type
from plugins.base_plugin import BasePlugin

class BoutControlSettings(BaseModel):
    allow_remote_control: bool = Field(default=True, description="Allows external web panels to override game states.")
    default_round_duration: int = Field(default=180, description="Standard round time limit in seconds.")

class EventPublishRequest(BaseModel):
    event_name: str = Field(..., description="The dot-namespaced event string identifier.")
    payload: dict = Field(default_factory=dict, description="Contextual state metadata payload.")

class BoutControlPlugin(BasePlugin):
    """Orchestrates manual match-control interfaces by capturing dashboard actions

    and funneling them securely into the core shared async EventBus environment.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._router = APIRouter(prefix="/api/bout-control", tags=["Bout Control Layout"])
        self._setup_routes()

    @property
    def name(self) -> str: 
        return "bout_control"

    @property
    def requires_internet(self) -> bool: 
        return False

    @property
    def settings_schema(self) -> Type[BaseModel]: 
        return BoutControlSettings

    async def start(self) -> None:
        self.logger.info("Bout Control engine successfully initialized and ready.")

    async def stop(self) -> None:
        self.logger.warning("Bout Control engine shut down safely.")

    async def on_settings_changed(self, new_settings: BoutControlSettings) -> None:
        self.logger.info(f"Bout Control configuration changed. Allowed remote inputs: {new_settings.allow_remote_control}")

    def get_router(self) -> APIRouter:
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        self._router.mount("/box_control", StaticFiles(directory=static_dir, html=True), name="box_control")
        return self._router

    def _setup_routes(self):
        # HTTP POST: Pipeline route to ingest front-end events and publish them directly to the bus
        @self._router.post("/publish")
        async def publish_ui_event(request: EventPublishRequest):
            settings: BoutControlSettings = self.core_context["settings_manager"].get_settings(self.name)
            if not settings.allow_remote_control:
                raise HTTPException(status_code=403, detail="Remote state manipulation is currently restricted.")
            
            bus = self.core_context["event_bus"]
            
            # Safe concurrent execution dispatch path to the internal async bus
            self.logger.info(f"{request.event_name}: {request.payload}")
            asyncio.create_task(bus.publish(request.event_name, request.payload))
            return {"status": "dispatched", "event": request.event_name}