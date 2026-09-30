import asyncio
import logging

class BaseAutomationEngine:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.current_flow_task: asyncio.Task | None = None
        self.background_tasks: set[asyncio.Task] = set()

    def start_killable_flow(self, coro):
        """Starts an exclusive flow. Kills the currently running killable flow (if any)."""
        if self.current_flow_task and not self.current_flow_task.done():
            self.logger.info(f"Cancelling current flow to start new one: {coro.__name__}")
            self.current_flow_task.cancel()

        loop = asyncio.get_running_loop()
        self.current_flow_task = loop.create_task(self._run_safe(coro, is_killable=True))

    def start_async_flow(self, coro):
        """Starts a fully independent background flow."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_safe(coro, is_killable=False))
        
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    def start_nuke_flow(self, coro):
        """Kills EVERYTHING, then runs the new flow."""
        if self.current_flow_task and not self.current_flow_task.done():
            self.current_flow_task.cancel()
        
        for task in list(self.background_tasks):
            if not task.done():
                task.cancel()
        
        self.start_killable_flow(coro)

    async def _run_safe(self, coro, is_killable: bool):
        try:
            await coro
        except asyncio.CancelledError:
            flow_type = "Killable" if is_killable else "Background"
            self.logger.warning(f"{flow_type} flow cancelled: {coro.__name__}")
        except Exception as e:
            self.logger.error(f"Error in flow {coro.__name__}: {e}", exc_info=True)
        finally:
            if is_killable and self.current_flow_task and self.current_flow_task.get_name() == asyncio.current_task().get_name():
                self.current_flow_task = None