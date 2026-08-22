import asyncio
import os
from typing import Optional

from fastapi import APIRouter

from plugins.base_plugin import BasePlugin, Namespace, PluginState
from plugins.shuttle_xpress.settings import ShuttleXpressSettings


class ShuttleXpressPlugin(BasePlugin):
    """
    Supervises the `shuttle-go` driver process for a Contour Design
    ShuttleXpress jog/shuttle controller.

    shuttle-go is a headless daemon — it opens the HID event node itself
    and translates jog/button input into keystrokes/OSC/exec commands.
    It doesn't emit anything structured on stdout, so there's nothing
    here to relay onto the event bus; this plugin's job is purely to:

      1. wait for the device to be present at `device_path`,
      2. keep `shuttle-go` running against it,
      3. surface connection/process state on the dashboard via status,
         and restart the subprocess with backoff if it exits (e.g. the
         device gets unplugged mid-session).

    Unlike TkStrikePlugin's SerialDeviceResolver check, a missing device
    here does NOT fail plugin load — it's a "waiting for hardware" state,
    not a fatal error, since the ShuttleXpress can be plugged in later.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._settings: Optional[ShuttleXpressSettings] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._connected: bool = False

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return "shuttle_xpress"

    @property
    def requires_internet(self) -> bool:
        return False

    @property
    def settings_schema(self):
        return ShuttleXpressSettings

    namespace: str = Namespace.CORE

    # ------------------------------------------------------------------ device check

    def _device_present(self) -> bool:
        """
        True if the by-id symlink exists and resolves to a real node.
        os.path.exists() follows symlinks, so a dangling symlink (device
        unplugged, udev hasn't cleaned up yet) correctly reports False.
        Cheap and doesn't require root, unlike inspecting sysfs directly.
        """
        return os.path.exists(self._settings.device_path)

    def _describe_missing_device(self) -> str:
        return (
            f"ShuttleXpress not found at '{self._settings.device_path}'. "
            f"Check it's plugged in (lsusb should show "
            f"{self._settings.vendor_id}:{self._settings.product_id}) and that "
            f"udev has created the by-id symlink."
        )

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._settings = self.core_context["settings_manager"].get_settings(self.name)
        self.core_context["shuttle_xpress_plugin"] = self

        self.logger.info(
            "Starting ShuttleXpress Plugin",
            device_path=self._settings.device_path,
        )
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())

    async def stop(self) -> None:
        self.logger.info("Stopping ShuttleXpress Plugin...")

        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
            self._supervisor_task = None

        await self._terminate_process()

        self._connected = False
        self.logger.info("ShuttleXpress Plugin stopped.")

    async def on_settings_changed(self, new_settings: ShuttleXpressSettings) -> None:
        self.logger.info("ShuttleXpress settings changed — restarting supervisor.")
        await self.stop()
        self._settings = new_settings
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())

    # ------------------------------------------------------------------ supervisor

    async def _supervisor_loop(self) -> None:
        """
        Two nested loops:
          - outer: wait until the device node exists
          - inner: run shuttle-go against it, with restart backoff if it
            exits on its own (crash, or device unplugged out from under it)
        """
        try:
            while True:
                if not self._device_present():
                    self._connected = False
                    self.set_status(PluginState.WAITING, self._describe_missing_device())
                    await asyncio.sleep(self._settings.poll_interval_seconds)
                    continue

                self._connected = True
                await self._run_until_exit_or_unplugged()
        except asyncio.CancelledError:
            return

    async def _run_until_exit_or_unplugged(self) -> None:
        """
        Runs shuttle-go while the device stays present, restarting with
        backoff if the process dies. Returns (back to the outer loop) as
        soon as the device disappears.
        """
        delay = self._settings.restart_backoff_min_seconds

        while self._device_present():
            self.set_status(PluginState.STARTING, "Launching shuttle-go...")
            try:
                await self._launch_process()
            except FileNotFoundError:
                self.set_status(
                    PluginState.ERROR,
                    f"shuttle-go binary not found at '{self._settings.shuttle_go_path}'.",
                )
                self.logger.error(
                    f"shuttle-go binary not found: {self._settings.shuttle_go_path}"
                )
                return  # not a device problem — don't spin the outer loop
            except Exception as e:
                self.set_status(PluginState.ERROR, f"Failed to launch shuttle-go: {e}")
                self.logger.error("Failed to launch shuttle-go", error=str(e))
                return

            self.set_status(PluginState.ACTIVE, "shuttle-go running.")
            self.logger.info("shuttle-go started", pid=self._process.pid)

            return_code = await self._process.wait()
            self._process = None

            if not self._device_present():
                self.logger.warning("ShuttleXpress disconnected.")
                return

            self.logger.warning(
                f"shuttle-go exited unexpectedly (code {return_code}). "
                f"Retrying in {delay:.0f}s."
            )
            self.set_status(
                PluginState.ERROR,
                f"shuttle-go exited (code {return_code}); retrying in {delay:.0f}s.",
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._settings.restart_backoff_max_seconds)

        self._connected = False

    async def _launch_process(self) -> None:
        cmd = [self._settings.shuttle_go_path, self._settings.device_path]
        if self._settings.config_path:
            cmd += ["-config", self._settings.config_path]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _terminate_process(self) -> None:
        if not self._process:
            return
        try:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.warning("shuttle-go didn't exit cleanly — killing it.")
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass  # already gone
        finally:
            self._process = None

    # ------------------------------------------------------------------ HTTP router

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/shuttle-xpress", tags=["ShuttleXpress"])

        @router.get("/status")
        async def status():
            return {
                "plugin": self.name,
                "connected": self._connected,
                "process_running": self._process is not None,
                "device_path": self._settings.device_path if self._settings else None,
                "status": self.get_status().state.value,
                "message": self.get_status().message,
            }

        return router