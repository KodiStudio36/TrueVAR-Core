from abc import ABC, abstractmethod
from core.domain.ports.logger_port import LoggerPort
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Type

class BasePlugin(ABC):
    def __init__(self, core_context: dict, logger: LoggerPort):
        self.core_context = core_context
        self.logger = logger

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