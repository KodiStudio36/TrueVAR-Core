import asyncio
import logging
from collections import defaultdict
from typing import Callable, Optional

log = logging.getLogger(__name__)


class EventBus:
    """
    Central async pub/sub bus injected into every plugin via core_context["event_bus"].

    Naming convention (dot-namespaced strings):

        scoreboard.stable_update    — full state dict from DaedoStateEngine
        scoreboard.clock_update     — {"clk": "01:45"}
        scoreboard.NEW_FIGHT
        scoreboard.START_FIGHT
        scoreboard.START_ROUND
        scoreboard.START_BREAK
        scoreboard.WIN
        scoreboard.BLUE_SCORE
        scoreboard.RED_SCORE

        ivr.recording_started       — {"fight_num": "..."}
        ivr.recording_stopped
        ivr.replay_opened
        ivr.replay_closed
        ivr.error                   — {"message": "..."}

        ivr.cmd.start_recording     — commands directed at the IVR app
        ivr.cmd.stop_recording
        ivr.cmd.toggle_recording
        ivr.cmd.show_replay / hide_replay
        ivr.cmd.show_settings / hide_settings

    Contract: every handler is   async def handler(data: dict) -> None
    """

    def __init__(self, logger=None):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._logger = logger

    # ------------------------------------------------------------------ sub / unsub

    def subscribe(self, event: str, handler: Callable) -> None:
        self._handlers[event].append(handler)

    def unsubscribe(self, event: str, handler: Callable) -> None:
        lst = self._handlers.get(event, [])
        if handler in lst:
            lst.remove(handler)

    def subscribe_many(self, pairs: list[tuple[str, Callable]]) -> None:
        for event, handler in pairs:
            self.subscribe(event, handler)

    def unsubscribe_many(self, pairs: list[tuple[str, Callable]]) -> None:
        for event, handler in pairs:
            self.unsubscribe(event, handler)

    # ------------------------------------------------------------------ publish

    async def publish(self, event: str, data: Optional[dict] = None) -> None:
        handlers = list(self._handlers.get(event, []))
        if not handlers:
            return

        results = await asyncio.gather(
            *[h(data or {}) for h in handlers],
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, Exception):
                msg = f"EventBus handler error [{event}]: {r}"
                if self._logger:
                    self._logger.error(msg, exception=r)
                else:
                    log.error(msg, exc_info=r)

    # ------------------------------------------------------------------ util

    def clear(self) -> None:
        self._handlers.clear()