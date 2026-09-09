from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Optional, Type

from core.domain.ports.logger_port import LoggerPort
from fastapi import APIRouter
from pydantic import BaseModel


class Namespace:
    """Category labels for organizing plugins on the dashboard."""
    CORE = "Core"
    TKSTRIKE = "TkStrike"
    BOUT_CNTRL = "Bout Cntrl"
    UNIVERSAL = "Universal"

class PluginState(str, Enum):
    IDLE = "idle"           # instantiated, not started yet
    STARTING = "starting"   # start() in progress
    WAITING = "waiting"     # e.g. waiting for a connection, waiting for data
    ACTIVE = "active"       # healthy and doing its job
    ERROR = "error"         # something is wrong — see message
    STOPPED = "stopped"     # stop() has completed


class PluginStatus(BaseModel):
    state: PluginState = PluginState.IDLE
    message: Optional[str] = None


class BasePlugin(ABC):
    """
    dependencies: class names (as registered in PluginRegistry) of plugins
    that must already be attached before this one can be attached.
    Override at the class level:

        class TkStrikePlugin(BasePlugin):
            dependencies = ["TkStrikeListenerPlugin"]

    Deliberately a plain list, not an abstract property — it must be
    inspectable on the class itself, before any instance exists, so the
    dashboard can show dependency status for plugins that aren't attached.
    """

    def __init__(self, core_context: dict, logger: LoggerPort):
        self.core_context = core_context
        self.logger = logger
        self._status = PluginStatus(state=PluginState.IDLE, message=None)

    @property
    @abstractmethod
    def name(self) -> str: pass

    @property
    @abstractmethod
    def requires_internet(self) -> bool: pass

    @property
    @abstractmethod
    def settings_schema(self) -> Type[BaseModel]:
        """Defines the Pydantic model for this plugin's settings."""
        pass

    dependencies: List[str] = []
    
    namespace: str = Namespace.CORE

    # None → dashboard auto-generates a form from settings_schema's JSON
    # schema. Set to a string (e.g. a static asset path or route name)
    # once a plugin needs a hand-built settings UI instead — the
    # dashboard checks this and skips the auto-form when it's set.
    settings_ui: Optional[str] = None

    @abstractmethod
    async def on_settings_changed(self, new_settings: BaseModel) -> None:
        """Triggered automatically when the user updates settings via the UI."""
        pass

    @abstractmethod
    async def start(self) -> None: pass

    @abstractmethod
    async def stop(self) -> None: pass

    @abstractmethod
    def get_router(self) -> APIRouter: pass

    # ------------------------------------------------------------------ status

    def set_status(self, state: PluginState, message: Optional[str] = None) -> None:
        """
        Call this from anywhere in the plugin's lifecycle to report what
        it's currently doing — waiting for a connection, an error, active,
        etc. The dashboard reads this via get_status().
        """
        self._status = PluginStatus(state=state, message=message)
        if self.logger:
            self.logger.info(f"[{self.name}] status -> {state.value}", message=message)

    def get_status(self) -> PluginStatus:
        return self._status