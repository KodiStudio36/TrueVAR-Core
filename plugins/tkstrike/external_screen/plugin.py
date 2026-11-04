import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException

from plugins.base_plugin import BasePlugin, Namespace
from plugins.core.gstreamer_core.extensions.tkstrike_external_screen_extension import TkStrikeExternalScreenExtension
from plugins.tkstrike.external_screen.display_manager import DisplayManager
from plugins.tkstrike.external_screen.settings import TkStrikeExternalScreenSettings


class TkStrikeExternalScreenPlugin(BasePlugin):
    """Manages the external screen for TkStrike discipline.

    Owns xrandr / i3 lifecycle.  Delegates all GStreamer work to PipelineManager
    by registering / unregistering TkStrikeExternalScreenExtension.

    Bus events consumed:
        ivr.toggle_external_screen — IVR's toggle_external_screen_key → toggle mirroring
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

    Dashboard:
        get_dashboard_controls() → toggles rendered in the dashboard's
        "Quick Controls" panel.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.is_active = False
        self.is_mirror = False
        self.last_error: Optional[str] = None
        self._subscriptions: list[tuple] = []
        self._extension = TkStrikeExternalScreenExtension()
        self._move_task: Optional[asyncio.Task] = None
        # Serialises enable/disable/toggle: HTTP, bus and stop() can all race
        # (double-click on the dashboard, a bus command mid-transition…).
        self._screen_lock = asyncio.Lock()

    @property
    def name(self) -> str: return "TkStrike External Screen"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return TkStrikeExternalScreenSettings

    namespace: str = Namespace.TKSTRIKE

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                ("screen.cmd.enable",        self._cmd_enable),
                ("screen.cmd.disable",       self._cmd_disable),
                ("screen.cmd.toggle",        self._cmd_toggle),
                ("screen.cmd.toggle_mirror", self._cmd_toggle_mirror),
                # IVR app: operator pressed toggle_external_screen_key
                ("ivr.toggle_external_screen", self._cmd_toggle_mirror),
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

        self._cancel_move()
        await self.set_active(False)

    async def on_settings_changed(self, new_settings: TkStrikeExternalScreenSettings) -> None:
        self.logger.info("External screen settings updated.")

    # ------------------------------------------------------------------ public state API

    async def set_active(self, active: bool) -> bool:
        """Idempotent: enable/disable only when the state actually changes."""
        async with self._screen_lock:
            if active and not self.is_active:
                await self._enable()
            elif not active and self.is_active:
                await self._disable()
        return self.is_active

    async def toggle(self) -> bool:
        async with self._screen_lock:
            if self.is_active:
                await self._disable()
            else:
                await self._enable()
        return self.is_active

    async def toggle_mirror(self) -> bool:
        async with self._screen_lock:
            if not self.is_active:
                self.logger.info("Mirror toggle ignored — external screen is off.")
                return self.is_mirror
            s = self._settings()
            self.is_mirror = await DisplayManager.async_toggle_mirror(s.main_display, s.external_display)
            self.logger.info(f"Display mode toggled — mirrored: {self.is_mirror}")
            if not self.is_mirror:
                # Back to extended: the window has to go to the external workspace again.
                self._schedule_move(s)
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("screen.mirror_changed", {"is_mirror": self.is_mirror})
        return self.is_mirror

    # ------------------------------------------------------------------ bus command handlers

    async def _cmd_enable(self, _: dict):
        await self.set_active(True)

    async def _cmd_disable(self, _: dict):
        await self.set_active(False)

    async def _cmd_toggle(self, _: dict):
        await self.toggle()

    async def _cmd_toggle_mirror(self, _: dict):
        await self.toggle_mirror()

    async def _on_pipeline_rebuilt(self, data: dict):
        """Schedule window move only when our extension is present in the rebuilt pipeline."""
        if not self.is_active or self.is_mirror:
            return
        if self._extension.name not in data.get("extensions", []):
            return
        self._schedule_move(self._settings())

    # ------------------------------------------------------------------ screen lifecycle (call with _lock held)

    async def _enable(self) -> None:
        s = self._settings()
        self.logger.info(f"Enabling external screen on {s.external_display}...")
        self.last_error = None

        pm = self.core_context.get("pipeline_manager")
        if pm is None:
            self.last_error = "GStreamer pipeline manager not available"
            self.logger.error(f"{self.last_error} — external screen not enabled.")
            return

        ok = await DisplayManager.async_setup_extended(s.main_display, s.external_display)
        if not ok:
            self.last_error = f"xrandr could not enable {s.external_display} (is it connected?)"
            self.logger.error(self.last_error)
            return

        self.is_active = True
        self.is_mirror = False
        # Window placement happens on gstreamer.pipeline.rebuilt (see _on_pipeline_rebuilt).
        pm.register_extension(self._extension)

        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("screen.external.activated", {"display": s.external_display})

    async def _disable(self) -> None:
        s = self._settings()
        self.logger.info(f"Disabling external screen on {s.external_display}...")
        self._cancel_move()

        # Unregister AND wait for the rebuild, so the xvimagesink is really gone
        # before xrandr turns the output off (previously the rebuild was only
        # scheduled, so xrandr --off raced a live sink).
        pm = self.core_context.get("pipeline_manager")
        if pm:
            pm.unregister_extension(self._extension.name)
            if not await pm.wait_for_rebuild():
                self.logger.warning("Pipeline rebuild still pending — turning output off anyway.")

        await DisplayManager.async_reset(s.external_display)

        was_mirror = self.is_mirror
        self.is_active = False
        self.is_mirror = False

        bus = self.core_context.get("event_bus")
        if bus:
            if was_mirror:
                # Listeners (IVR badge / closeup) must learn mirroring ended too.
                await bus.publish("screen.mirror_changed", {"is_mirror": False})
            await bus.publish("screen.external.deactivated", {})

    # ------------------------------------------------------------------ helpers

    def _schedule_move(self, s: TkStrikeExternalScreenSettings) -> None:
        self.logger.info(
            f"Scheduling window move: '{s.window_title}' → workspace {s.target_workspace}"
        )
        self._cancel_move()
        self._move_task = asyncio.create_task(
            DisplayManager.move_window_after_delay(s.window_title, s.target_workspace, s.window_delay)
        )

    def _cancel_move(self) -> None:
        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
        self._move_task = None

    def _settings(self) -> TkStrikeExternalScreenSettings:
        return self.core_context["settings_manager"].get_settings(self.name)

    def _screen_status(self) -> dict:
        return {"active": self.is_active, "mirror": self.is_mirror, "error": self.last_error}

    def get_dashboard_controls(self) -> list[dict]:
        """Rendered by the dashboard "Quick Controls" panel (see orchestrator)."""
        return [
            {
                "id": "external_screen",
                "label": "External screen",
                "type": "toggle",
                "state": self.is_active,
                "endpoint": "/api/external-screen/toggle",
                "error": self.last_error,
            },
            {
                "id": "external_screen_mirror",
                "label": "Mirror displays",
                "type": "toggle",
                "state": self.is_mirror,
                "endpoint": "/api/external-screen/toggle-mirror",
                "disabled": not self.is_active,
            },
        ]

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/external-screen", tags=["External Screen"])

        @router.get("/status")
        async def status():
            return self._screen_status()

        @router.post("/enable")
        async def enable():
            await self.set_active(True)
            if not self.is_active:
                raise HTTPException(status_code=500, detail=self.last_error or "Enable failed")
            return self._screen_status()

        @router.post("/disable")
        async def disable():
            await self.set_active(False)
            return self._screen_status()

        @router.post("/toggle")
        async def toggle():
            want = not self.is_active
            await self.toggle()
            if want and not self.is_active:
                raise HTTPException(status_code=500, detail=self.last_error or "Enable failed")
            return self._screen_status()

        @router.post("/toggle-mirror")
        async def toggle_mirror():
            await self.toggle_mirror()
            return self._screen_status()

        return router