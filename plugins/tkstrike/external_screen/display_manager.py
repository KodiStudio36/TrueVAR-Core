"""Python replacement for manage_display.sh.

All subprocess calls are blocking.  Call them via asyncio.run_in_executor
when inside the async event loop.
"""

import asyncio
import subprocess
import logging

log = logging.getLogger(__name__)


class DisplayManager:

    # ------------------------------------------------------------------ xrandr

    @staticmethod
    def setup_extended(main: str, external: str) -> bool:
        """Enable the external display in extended (right-of) mode."""
        try:
            subprocess.run(
                ["xrandr",
                 "--output", main,     "--auto", "--primary",
                 "--output", external, "--auto", "--right-of", main],
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            log.error(f"xrandr setup_extended failed: {exc}")
            return False

    @staticmethod
    def is_mirrored(external: str) -> bool:
        """Return True when the external display is in same-as (mirror) mode."""
        try:
            result = subprocess.run(["xrandr"], capture_output=True, text=True, check=True)
            for line in result.stdout.splitlines():
                # mirrored → external sits at position +0+0
                if external in line and "connected" in line and "+0+0" in line:
                    return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return False

    @classmethod
    def toggle_mirror(cls, main: str, external: str) -> bool:
        """Switch between extended and mirror. Returns True when now mirrored."""
        if cls.is_mirrored(external):
            subprocess.run(
                ["xrandr",
                 "--output", main,     "--auto", "--primary",
                 "--output", external, "--auto", "--right-of", main],
                check=False,
            )
            return False
        else:
            subprocess.run(
                ["xrandr",
                 "--output", main,     "--auto", "--primary",
                 "--output", external, "--auto", "--same-as", main],
                check=False,
            )
            return True

    @staticmethod
    def reset(external: str) -> bool:
        """Turn the external display off."""
        try:
            subprocess.run(["xrandr", "--output", external, "--off"], check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            log.error(f"xrandr reset failed: {exc}")
            return False

    # ------------------------------------------------------------------ i3 window management

    @staticmethod
    def move_window(title: str, workspace: int) -> bool:
        """Move an X11 window (matched by title) to an i3 workspace and fullscreen it."""
        try:
            subprocess.run(
                ["i3-msg", f'[title="{title}"] move container to workspace {workspace}'],
                check=True,
            )
            subprocess.run(
                ["i3-msg", f'[title="{title}"] fullscreen enable'],
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            log.error(f"i3-msg move_window failed: {exc}")
            return False

    # ------------------------------------------------------------------ async helpers

    @classmethod
    async def async_setup_extended(cls, main: str, external: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, cls.setup_extended, main, external)

    @classmethod
    async def async_reset(cls, external: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, cls.reset, external)

    @classmethod
    async def async_toggle_mirror(cls, main: str, external: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, cls.toggle_mirror, main, external)

    @classmethod
    async def move_window_after_delay(cls, title: str, workspace: int, delay: float) -> None:
        """Wait for the xvimagesink X11 window to appear, then move it."""
        await asyncio.sleep(delay)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, cls.move_window, title, workspace)