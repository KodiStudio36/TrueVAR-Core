import asyncio
import os
from typing import Optional
from fastapi import APIRouter

from core.use_cases.application_bootstrapper import ApplicationBootstrapper
from plugins.base_plugin import BasePlugin
from plugins.tkstrike.tkstrike.settings import TkStrikeSettings


class TkStrikePlugin(BasePlugin):
    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._settings: Optional[TkStrikeSettings] = None
        self._bootstrapper = ApplicationBootstrapper(logger)

    @property
    def name(self) -> str: return "tkstrike"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return TkStrikeSettings

    # ------------------------------------------------------------------ i3 Workspace Transitions

    async def _switch_active_workspace(self, workspace_num: int) -> None:
        """Helper to command i3 to physically shift view focus to another workspace."""
        try:
            self.logger.info(f"Shifting i3 focus to workspace {workspace_num}")
            process = await asyncio.create_subprocess_exec(
                "i3-msg", "workspace", str(workspace_num),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await process.wait()
        except Exception as e:
            self.logger.error(f"Failed to switch workspace to {workspace_num}", error=str(e))

    # ------------------------------------------------------------------ Event Bus Handlers

    async def _on_replay_triggered(self, data: dict):
        """Triggered when event bus signals video replay start."""
        self.logger.info("Replay event received. Transitioning screen to IVR workspace...")
        await self._switch_active_workspace(self._settings.ivr_workspace)

    async def _on_replay_stopped(self, data: dict):
        """Triggered when event bus signals video replay finish."""
        self.logger.info("Replay finished event received. Returning to TkStrike workspace...")
        await self._switch_active_workspace(self._settings.workspace)

    # ------------------------------------------------------------------ Lifecycle

    async def start(self) -> None:
        self._settings = self.core_context["settings_manager"].get_settings(self.name)
        self.core_context["tk_strike_plugin"] = self

        self.logger.info("Starting TkStrike Plugin (Wine Sandbox)...")

        # 2. Subscribe to Video Replay Event Hooks on the EventBus
        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                (self._settings.replay_start_event, self._on_replay_triggered),
                (self._settings.replay_stop_event,  self._on_replay_stopped),
            ]
            bus.subscribe_many(self._subscriptions)
            self.logger.info("TkStrike subscribed to workspace replay event sequence.")

        # 1. Define command (without 'start' to keep process attached)
        cmd = ["wine", "start", "/d", self._settings.app_dir, self._settings.exe_name]

        env_vars = {
            "WINEPREFIX": self._settings.wine_prefix,
            "WINEDEBUG": "err+all,warn+all,+loaddll",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            "XAUTHORITY": os.environ.get("XAUTHORITY", "")
        }

        """
        WINEPREFIX=/home/kodi/Documents/programs/var/TKStrike/pfx wine
        start /d /home/kodi/Documents/programs/var/TKStrike/pfx/drive_c/users/kodi/AppData/Local/tkStrikeGen2 tkStrikeGen2.exe
        """

        # 3. Boot via our reusable bootstrapper
        asyncio.create_task(
            self._bootstrapper.boot(
                cmd=cmd,
                window_class=self._settings.window_class,
                workspace=self._settings.workspace,
                startup_delay=self._settings.startup_delay,
                env=env_vars,
            )
        )

        # TODO: reconsider this maybe
        # await self._switch_active_workspace(self._settings.workspace)

    async def stop(self) -> None:
        self.logger.info("Stopping TkStrike Plugin...")
        
        # 1. Let the bootstrapper clean up its initial process handle if any exists
        self._bootstrapper.terminate()

        print("here")

        # 2. Drop the hammer on the specific Wine Sandbox environment
        if self._settings:
            try:
                self.logger.info(f"Killing all processes in Wine Prefix: {self._settings.wine_prefix}")
                
                # Execute wineserver -k under the isolated environment variables
                process = await asyncio.create_subprocess_exec(
                    "wineserver", "-k",
                    env={"WINEPREFIX": self._settings.wine_prefix},
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                await process.wait() # Ensure it completes before proceeding
                
            except Exception as e:
                self.logger.error("Failed to cleanly shut down wineserver", error=str(e))

        self.logger.info("TkStrike Plugin stopped and Wine sandbox wiped.")

    async def on_settings_changed(self, new_settings: TkStrikeSettings) -> None:
        self.logger.info("TkStrike settings changed — rebooting application.")
        await self.stop()
        
        # Give Wine a second to completely release the file locks before restarting
        await asyncio.sleep(1.0)
        
        self._settings = new_settings
        await self.start()

    def get_router(self) -> APIRouter:
        """Fulfills the BasePlugin abstract contract."""
        router = APIRouter(prefix="/api/tkstrike", tags=["TkStrike"])

        @router.get("/status")
        async def status():
            return {
                "plugin": self.name,
                "workspace": self._settings.workspace if self._settings else None
            }

        return router