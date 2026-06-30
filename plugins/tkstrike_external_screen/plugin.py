import asyncio
from typing import Optional
from fastapi import APIRouter

from plugins.base_plugin import BasePlugin
from plugins.gstreamer_core.extensions.tkstrike_external_screen_extension import TkStrikeExternalScreenExtension
from plugins.tkstrike_external_screen.display_manager import DisplayManager
from plugins.tkstrike_external_screen.settings import TkStrikeExternalScreenSettings


class TkStrikeExternalScreenPlugin(BasePlugin):
    """Manages the external screen for TkStrike discipline.

    Owns xrandr / i3 lifecycle.  Delegates all GStreamer work to PipelineManager
    by registering / unregistering ExternalScreenExtension.

    Bus events consumed:
        screen.cmd.enable         — activate the screen
        screen.cmd.disable        — deactivate the screen
        screen.cmd.toggle         — flip active state
        screen.cmd.toggle_mirror  — switch mirror ↔ extended

    Bus events emitted:
        screen.external.activated   {"display": "HDMI-1"}
        screen.external.deactivated {}
        screen.mirror_changed       {"is_mirror": bool}

    Reacts to:
        gstreamer.pipeline.rebuilt  — triggers window move when xvimagesink is live
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.is_active = False
        self.is_mirror = False
        self._subscriptions: list[tuple] = []
        self._extension = TkStrikeExternalScreenExtension()
        self._move_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str: return "tkstrike_external_screen"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return TkStrikeExternalScreenSettings

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                ("screen.cmd.enable",        self._cmd_enable),
                ("screen.cmd.disable",       self._cmd_disable),
                ("screen.cmd.toggle",        self._cmd_toggle),
                ("screen.cmd.toggle_mirror", self._cmd_toggle_mirror),
                # After GStreamer rebuilds with the screen extension, move the window
                ("gstreamer.pipeline.rebuilt", self._on_pipeline_rebuilt),
            ]
            bus.subscribe_many(self._subscriptions)
        self.logger.info("TkStrike External Screen Manager ready.")

    async def stop(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []

        if self._move_task and not self._move_task.done():
            self._move_task.cancel()

        if self.is_active:
            await self._disable()

    async def on_settings_changed(self, new_settings: TkStrikeExternalScreenSettings) -> None:
        self.logger.info("External screen settings updated.")

    # ------------------------------------------------------------------ bus command handlers

    async def _cmd_enable(self, _: dict):
        if not self.is_active:
            await self._enable()

    async def _cmd_disable(self, _: dict):
        if self.is_active:
            await self._disable()

    async def _cmd_toggle(self, _: dict):
        if self.is_active:
            await self._disable()
        else:
            await self._enable()

    async def _cmd_toggle_mirror(self, _: dict):
        if not self.is_active:
            return
        s = self._settings()
        self.is_mirror = await DisplayManager.async_toggle_mirror(s.main_display, s.external_display)
        self.logger.info(f"Display mode toggled — mirrored: {self.is_mirror}")
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("screen.mirror_changed", {"is_mirror": self.is_mirror})

    async def _on_pipeline_rebuilt(self, data: dict):
        """Schedule window move only when our extension is present in the rebuilt pipeline."""
        if not self.is_active:
            return
        if "tkstrike_external_screen" not in data.get("extensions", []):
            return
        
        s = self._settings()
        self.logger.info(
            f"Pipeline ready — scheduling window move: "
            f"'{s.window_title}' → workspace {s.target_workspace}"
        )

        if self._move_task and not self._move_task.done():
            self._move_task.cancel()

        self._move_task = asyncio.create_task(
            DisplayManager.move_window_after_delay(s.window_title, s.target_workspace, s.window_delay)
        )

    # ------------------------------------------------------------------ screen lifecycle

    async def _enable(self) -> None:
        s = self._settings()
        self.logger.info(f"Enabling external screen on {s.external_display}...")

        ok = await DisplayManager.async_setup_extended(s.main_display, s.external_display)
        if not ok:
            self.logger.error("xrandr setup failed — aborting.")
            return

        # Hand off GStreamer work to PipelineManager
        pm = self.core_context.get("pipeline_manager")
        if pm:
            pm.register_extension(self._extension)
        else:
            self.logger.warning("pipeline_manager not found in context — GStreamer branch skipped.")

        self.is_active = True
        self.is_mirror = False

        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("screen.external.activated", {"display": s.external_display})

    async def _disable(self) -> None:
        s = self._settings()
        self.logger.info(f"Disabling external screen on {s.external_display}...")

        # Unregister first so GStreamer drops the xvimagesink before xrandr turns off
        pm = self.core_context.get("pipeline_manager")
        if pm:
            pm.unregister_extension(self._extension.name)

        await DisplayManager.async_reset(s.external_display)

        self.is_active = False
        self.is_mirror = False

        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("screen.external.deactivated", {})

    # ------------------------------------------------------------------ helpers

    def _settings(self) -> TkStrikeExternalScreenSettings:
        return self.core_context["settings_manager"].get_settings(self.name)

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/external-screen", tags=["External Screen"])

        @router.get("/status")
        async def status():
            return {"active": self.is_active, "mirror": self.is_mirror}

        @router.post("/enable")
        async def enable():
            await self._enable()
            return {"active": self.is_active}

        @router.post("/disable")
        async def disable():
            await self._disable()
            return {"active": self.is_active}

        @router.post("/toggle")
        async def toggle():
            await (self._disable() if self.is_active else self._enable())
            return {"active": self.is_active}

        @router.post("/toggle-mirror")
        async def toggle_mirror():
            await self._cmd_toggle_mirror({})
            return {"mirror": self.is_mirror}

        return router