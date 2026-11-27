import asyncio
import os
import shutil
import signal
from typing import Optional

from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request

from plugins.base_plugin import BasePlugin, Namespace
from plugins.poomsae.external_screen.settings import PoomsaeExternalScreenSettings
from plugins.tkstrike.external_screen.display_manager import DisplayManager


BROWSER_CANDIDATES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                      "chrome", "brave-browser", "microsoft-edge"]


class PoomsaeExternalScreenPlugin(BasePlugin):
    """Shows the Poomsae external (audience) screen on the second display.

    Same idea as the TkStrike external screen — xrandr turns the output on,
    i3 puts the window on the external workspace fullscreen — but the content
    is the Poomsae Control Panel's /screen page, shown in a chromium app
    window of its own (no GStreamer involved).

    Bus events consumed:
        poomsae.screen.cmd.enable / .disable / .toggle
    Bus events emitted:
        poomsae.screen.activated   {"display": "HDMI-1"}
        poomsae.screen.deactivated {}

    Dashboard: a toggle in "Quick Controls" (see get_dashboard_controls).
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.is_active = False
        self.last_error: Optional[str] = None
        self._subscriptions: list[tuple] = []
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._origin: Optional[str] = None   # address this server was reached on (set by the routes)
        self._move_task: Optional[asyncio.Task] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._screen_lock = asyncio.Lock()

    @property
    def name(self) -> str: return "Poomsae External Screen"

    @property
    def requires_internet(self) -> bool: return False

    @property
    def settings_schema(self): return PoomsaeExternalScreenSettings

    namespace: str = Namespace.BOUT_CNTRL

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                ("poomsae.screen.cmd.enable", self._cmd_enable),
                ("poomsae.screen.cmd.disable", self._cmd_disable),
                ("poomsae.screen.cmd.toggle", self._cmd_toggle),
            ]
            bus.subscribe_many(self._subscriptions)
        self.logger.info("Poomsae External Screen ready.")

    async def stop(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []
        await self.set_active(False)

    async def on_settings_changed(self, new_settings: PoomsaeExternalScreenSettings) -> None:
        self.logger.info("Poomsae external screen settings updated (applied on next enable).")

    # ------------------------------------------------------------------ public state API

    async def set_active(self, active: bool) -> bool:
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

    async def _cmd_enable(self, _: dict): await self.set_active(True)
    async def _cmd_disable(self, _: dict): await self.set_active(False)
    async def _cmd_toggle(self, _: dict): await self.toggle()

    # ------------------------------------------------------------------ screen lifecycle (call with lock held)

    @staticmethod
    def _find_browser(configured: str):
        """Configured command first, then the usual Chromium-family names. Returns a full path or None."""
        for name in ([configured] if configured and configured != "auto" else []) + BROWSER_CANDIDATES:
            path = shutil.which(name)
            if path:
                return path
        return None

    async def _enable(self) -> None:
        s = self._settings()
        self.last_error = None
        browser = self._find_browser(s.browser_command)
        if browser is None:
            self.last_error = ("No Chromium-based browser found (tried "
                               f"{s.browser_command or 'auto'}, {', '.join(BROWSER_CANDIDATES)}); "
                               "set browser_command in the plugin settings")
            self.logger.error(self.last_error)
            return
        self.logger.info(f"Enabling Poomsae external screen on {s.external_display}...")

        if not await DisplayManager.async_setup_extended(s.main_display, s.external_display):
            self.last_error = f"xrandr could not enable {s.external_display} (is it connected?)"
            self.logger.error(self.last_error)
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                browser, f"--app={self._screen_url(s)}", f"--user-data-dir={s.profile_dir}",
                "--no-first-run", "--noerrdialogs", "--disable-infobars", "--disable-session-crashed-bubble",
                "--autoplay-policy=no-user-gesture-required",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,            # own process group → closed as a whole
            )
        except Exception as exc:
            self.last_error = f"Could not start the browser: {exc}"
            self.logger.error(self.last_error)
            await DisplayManager.async_reset(s.external_display)
            return

        # A browser that dies right away (bad profile dir, no display…) is a failed enable.
        await asyncio.sleep(1.0)
        if proc.returncode is not None:
            self.last_error = f"Browser exited immediately (code {proc.returncode})"
            self.logger.error(self.last_error)
            await DisplayManager.async_reset(s.external_display)
            return

        self._proc = proc
        self.is_active = True
        self._schedule_move(s)
        self._watch_task = asyncio.create_task(self._watch(proc))
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("poomsae.screen.activated", {"display": s.external_display})

    async def _disable(self) -> None:
        s = self._settings()
        self.logger.info(f"Disabling Poomsae external screen on {s.external_display}...")
        self._cancel_move()
        if self._watch_task and self._watch_task is not asyncio.current_task():
            self._watch_task.cancel()
        self._watch_task = None

        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        await DisplayManager.async_reset(s.external_display)
        self.is_active = False
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("poomsae.screen.deactivated", {})

    async def _watch(self, proc) -> None:
        """The operator closed the window by hand → the toggle goes back to OFF."""
        await proc.wait()
        if self._proc is proc and self.is_active:
            self.logger.info("Poomsae external screen window closed.")
            await self.set_active(False)

    # ------------------------------------------------------------------ helpers

    def _schedule_move(self, s: PoomsaeExternalScreenSettings) -> None:
        self._cancel_move()
        self._move_task = asyncio.create_task(
            DisplayManager.move_window_after_delay(s.window_title, s.target_workspace, s.window_delay)
        )

    def _cancel_move(self) -> None:
        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
        self._move_task = None

    SCREEN_PATH = "/api/poomsae-control/screen"

    def _remember_origin(self, request: Request) -> None:
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        self._origin = f"{request.url.scheme}://127.0.0.1:{port}"

    def _screen_url(self, s: PoomsaeExternalScreenSettings) -> str:
        """'auto' (or a stale local URL with the wrong port) → the address this server is really on."""
        url = (s.screen_url or "").strip()
        if self._origin and (not url or url == "auto"):
            return self._origin + self.SCREEN_PATH
        if self._origin and url:
            u, o = urlsplit(url), urlsplit(self._origin)
            if u.hostname in ("localhost", "127.0.0.1") and u.port != o.port:
                self.logger.warning(f"screen_url port {u.port} differs from the server port {o.port}; using {self._origin}")
                return self._origin + (u.path or self.SCREEN_PATH)
        return url if url and url != "auto" else f"http://127.0.0.1:8000{self.SCREEN_PATH}"

    def _settings(self) -> PoomsaeExternalScreenSettings:
        return self.core_context["settings_manager"].get_settings(self.name)

    def _state_dict(self) -> dict:
        return {"active": self.is_active, "error": self.last_error}

    def get_dashboard_controls(self) -> list[dict]:
        return [{
            "id": "poomsae_panel",
            "label": "Poomsae panel",
            "type": "link",
            "url": "/api/poomsae-control/panel",
        }, {
            "id": "poomsae_external_screen",
            "label": "Poomsae screen",
            "type": "toggle",
            "state": self.is_active,
            "endpoint": "/api/poomsae-external-screen/toggle",
            "error": self.last_error,
        }]

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/poomsae-external-screen", tags=["Poomsae External Screen"])

        @router.get("/status")
        async def status():
            return self._state_dict()

        @router.post("/enable")
        async def enable(request: Request):
            self._remember_origin(request)
            await self.set_active(True)
            if not self.is_active:
                raise HTTPException(status_code=500, detail=self.last_error or "Enable failed")
            return self._state_dict()

        @router.post("/disable")
        async def disable(request: Request):
            self._remember_origin(request)
            await self.set_active(False)
            return self._state_dict()

        @router.post("/toggle")
        async def toggle(request: Request):
            self._remember_origin(request)
            want = not self.is_active
            await self.toggle()
            if want and not self.is_active:
                raise HTTPException(status_code=500, detail=self.last_error or "Enable failed")
            return self._state_dict()

        return router