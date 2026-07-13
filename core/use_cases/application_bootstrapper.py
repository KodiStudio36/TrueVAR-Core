import asyncio
import shlex
from typing import List, Optional

class ApplicationBootstrapper:
    def __init__(self, logger):
        self.logger = logger
        self._process: Optional[asyncio.subprocess.Process] = None

    async def boot(
        self, 
        cmd: List[str], 
        window_class: str, 
        workspace: int, 
        startup_delay: float = 4.0
    ) -> bool:
        """
        Launches any application, waits for its window, and pins it to an i3 workspace.
        """
        self.logger.info(f"Booting application process: {' '.join(cmd)}")
        try:
            # 1. Launch the process safely
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            
            # 2. Wait for the window to render completely
            await asyncio.sleep(startup_delay)
            
            # 3. Move it to the specified i3 workspace
            self.logger.info(f"Moving window class '{window_class}' to i3 workspace {workspace}")
            await asyncio.create_subprocess_exec(
                "i3-msg", f'[class="{window_class}"] move container to workspace {workspace}'
            )
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to boot process: {' '.join(cmd)}", exception=str(e))
            return False

    def terminate(self):
        """Clean closure of the OS process."""
        if self._process:
            try:
                self._process.terminate()
                self.logger.info("Application process terminated.")
            except ProcessLookupError:
                pass  # Already dead
            self._process = None