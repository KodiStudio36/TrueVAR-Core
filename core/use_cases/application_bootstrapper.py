import asyncio
import os
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
        startup_delay: float = 4.0,
        env: dict = None,
    ) -> bool:
        """
        Launches any application, waits for its window, and pins it to an i3 workspace.
        """
        self.logger.info(f"Booting application process: {' '.join(cmd)}")
        try:
            process_env = os.environ.copy()
            if env:
                process_env.update(env)

            # log_file_path = "/home/kodi/Documents/programs/var/TKStrike/wine_runtime.log"

            # # Ensure the directory exists
            # os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
                
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                env=process_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )

            # with open(log_file_path, "a") as log_file:
            #     self._process = await asyncio.create_subprocess_exec(
            #         *cmd,
            #         env=env,
            #         stdout=log_file, # Redirects stdout to file
            #         stderr=log_file  # Redirects stderr to file
            #     )
            
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