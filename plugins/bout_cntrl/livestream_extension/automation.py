import asyncio
from typing import Optional

from core.domain.ports.overlay_broadcaster_port import OverlayBroadcaster
from plugins.bout_cntrl.livestream_extension.settings import BoutCntrlSettings
from plugins.livestream.base_automation import BaseAutomationEngine


class BoutCntrlAutomation(BaseAutomationEngine):
    """
    All OBS scene switches and Socket.IO overlay broadcasts for Kyorugi.

    Every public on_* method is an async EventBus handler:
        async def on_*(self, data: dict) -> None

    Flows are called via trigger_* wrappers so the caller stays synchronous
    while the flow runs in a managed asyncio task.
    """

    def __init__(
        self,
        obs_adapter,
        broadcaster: OverlayBroadcaster,
        settings: BoutCntrlSettings,
        logger,
    ):
        super().__init__(logger)
        self.obs         = obs_adapter
        self.broadcaster = broadcaster
        self.settings    = settings

        self._replay_active = False
        self._youtube_loop_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ settings shorthands

    def _scene(self, key: str) -> str:
        return getattr(self.settings, f"obs_scene_{key}")

    def _transition(self, key: str) -> str:
        return getattr(self.settings, f"obs_transition_{key}")

    # ================================================================== scoreboard handlers

    async def on_stable_update(self, data: dict):
        """Pushes full scoreboard state to the browser overlay."""
        await self.broadcaster.emit_to_namespace(
            "stable_update", {"event": "update", "data": data}
        )

    async def on_clock_update(self, data: dict):
        await self.broadcaster.emit_to_namespace(
            "clock_update", {"event": "update", "data": data}
        )

    # ================================================================== fight lifecycle handlers

    async def on_new_fight(self, data: dict):
        """Both mch + at1 received — new fighters loaded."""
        await self.on_stable_update(data)
        self.trigger_next_fight()

    async def on_walk_in(self, data: dict):
        """Fight clock has started."""
        self.trigger_walkin()

    async def on_start_round(self, data: dict):
        """New round started (clk packet while not in fight/init)."""
        self.trigger_next_round()
        # This is importnat to be after!
        await self.on_stable_update(data)

    async def on_win(self, data: dict):
        """Winner declared (win packet)."""
        await self.on_stable_update(data)
        self.trigger_win()

    async def on_livestream_start(self, data: dict):
        """Fired when this extension finishes setup (livestream infrastructure online)."""
        # self.trigger_pre_tournament()

    async def on_tournament_start(self, data: dict):
        """Fired manually by the operator to kick off a session."""
        # self.trigger_pre_pre_fight()

    # ================================================================== flows

    async def _next_fight_flow(self):
        self.logger.info("Flow: next-fight")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets",
            {"event": "reset", "data": None},
        )
        # self.obs.set_scene(self._scene("main"))
        await asyncio.sleep(0.5)
        await self.broadcaster.emit_to_namespace("show_next_round", {})

        if not self._youtube_loop_task or self._youtube_loop_task.done():
            self._youtube_loop_task = asyncio.create_task(
                self._youtube_loop_manager(self.settings.youtube_subscribe_interval_mins)
            )

    async def _walkin_flow(self):
        self.logger.info("Flow: walkin")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets", {"event": "reset", "data": None}
        )

        await self.broadcaster.emit_to_namespace("show_fighter_bars", {})
        await asyncio.sleep(4)
        await self.broadcaster.emit_to_namespace("hide_fighter_bars", {})

        await self.broadcaster.emit_to_namespace("show_scoreboard", {})

    async def _next_round_flow(self):
        self.logger.info("Flow: start-round")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets",
            {"event": "reset", "data": None},
        )

    async def _win_flow(self):
        self.logger.info("Flow: post-fight")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets", {"event": "reset", "data": None}
        )

        await asyncio.sleep(2)
        await self.broadcaster.emit_to_namespace("show_win", {})
        await asyncio.sleep(8)
        await self.broadcaster.emit_to_namespace("hide_win", {})

    async def _youtube_subscribe_flow(self):
        await self.broadcaster.emit_to_namespace("show_yt", {})
        await asyncio.sleep(10)
        await self.broadcaster.emit_to_namespace("hide_yt", {})

    async def _youtube_loop_manager(self, interval_mins: int):
        """Fires the subscribe widget on a fixed interval until cancelled."""
        interval_secs = interval_mins * 60
        try:
            while True:
                await asyncio.sleep(interval_secs)
                self.logger.info("Triggering periodic YouTube subscribe flow.")
                self.trigger_youtube_subscribe()
        except asyncio.CancelledError:
            self.logger.info("YouTube subscribe loop cancelled.")

    # ================================================================== trigger wrappers

    def trigger_next_fight(self):
        self.start_killable_flow(self._next_fight_flow())

    def trigger_walkin(self):
        self.start_async_flow(self._walkin_flow())

    def trigger_next_round(self):
        self.start_killable_flow(self._next_round_flow())

    def trigger_win(self):
        self.start_killable_flow(self._win_flow())

    def trigger_youtube_subscribe(self):
        self.start_async_flow(self._youtube_subscribe_flow())