import asyncio
from typing import Optional

from core.domain.ports.overlay_broadcaster_port import OverlayBroadcaster
from plugins.livestream.base_automation import BaseAutomationEngine
from plugins.tkstrike.livestream_extension.settings import TaekwondoSettings


class TaekwondoStreamAutomation(BaseAutomationEngine):
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
        settings: TaekwondoSettings,
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
            "listener_update", {"event": "update", "data": data}
        )

    async def on_clock_update(self, data: dict):
        # data = {"clk": "01:45"} — extend if the overlay needs live clock ticks
        pass

    # ================================================================== fight lifecycle handlers

    async def on_livestream_start(self, data: dict):
        """Fired when this extension finishes setup (livestream infrastructure online)."""
        self.trigger_pre_tournament()

    async def on_tournament_start(self, data: dict):
        """Fired manually by the operator to kick off a session."""
        self.trigger_pre_pre_fight()

    async def on_new_fight(self, data: dict):
        """Both mch + at1 received — new fighters loaded."""
        self.trigger_pre_fight()

    async def on_start_fight(self, data: dict):
        """Fight clock has started."""
        self.trigger_start_fight()

    async def on_start_round(self, data: dict):
        """New round started (clk packet while not in fight/init)."""
        self.trigger_start_round()

    async def on_start_break(self, data: dict):
        """Round ended (brk packet)."""
        self.trigger_post_round()

    async def on_win(self, data: dict):
        """Winner declared (win packet)."""
        self.trigger_post_fight()

    # ================================================================== IVR handlers

    async def on_ivr_replay_opened(self, data: dict):
        self._replay_active = True
        self.logger.info("IVR replay opened → IVR broadcast flow.")
        self.trigger_start_ivr()

    async def on_ivr_replay_closed(self, data: dict):
        self._replay_active = False
        self.logger.info("IVR replay closed → post-IVR flow.")
        self.trigger_post_ivr()

    async def on_ivr_recording_started(self, data: dict):
        self.logger.info(f"IVR recording started for fight {data.get('fight_num')}.")

    async def on_screen_mirror_changed(self, data: dict):
        """External screen switched to mirror while replay is open → closeup view."""
        if data.get("is_mirror") and self._replay_active:
            self.logger.info("Screen mirrored during IVR replay → IVR closeup flow.")
            self.trigger_start_ivr_closeup()

    # ================================================================== operator handlers

    async def on_message_broadcast(self, data: dict):
        """Show a ticker message on the overlay."""
        self.trigger_message_broadcast(data.get("message", ""))

    async def on_troubleshoot(self, data: dict):
        """Switch OBS to the troubleshoot scene."""
        self.trigger_troubleshooting()

    # ================================================================== flows

    async def _pre_tournament_flow(self):
        self.logger.info("Flow: pre-tournament")
        self.obs.set_scene(self._scene("starting"))
        self.obs.set_transition(self._transition("move"))

    async def _pre_pre_fight_flow(self):
        """Between tournaments — clean UI and (re)start the YouTube reminder loop."""
        self.logger.info("Flow: pre-pre-fight")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets",
            {"event": "reset", "data": ["widget-winner", "widget-round-results"]},
        )
        self.obs.set_scene(self._scene("main"))

        if self._youtube_loop_task and not self._youtube_loop_task.done():
            self._youtube_loop_task.cancel()
        self._youtube_loop_task = asyncio.create_task(
            self._youtube_loop_manager(self.settings.youtube_subscribe_interval_mins)
        )

    async def _pre_fight_flow(self):
        self.logger.info("Flow: pre-fight")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets",
            {"event": "reset", "data": ["widget-winner", "widget-round-results"]},
        )
        self.obs.set_scene(self._scene("main"))
        await asyncio.sleep(0.5)
        await self.broadcaster.emit_to_namespace("show_next_round", {})

    async def _start_fight_flow(self):
        self.logger.info("Flow: start-fight")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets", {"event": "reset", "data": None}
        )
        await self.broadcaster.emit_to_namespace("show_fighter_bars", {})
        await asyncio.sleep(8)
        await self.broadcaster.emit_to_namespace("hide_fighter_bars", {})

    async def _start_round_flow(self):
        self.logger.info("Flow: start-round")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets",
            {"event": "reset", "data": ["widget-winner", "widget-round-results"]},
        )
        self.obs.set_scene(self._scene("main_scoreboard"))
        await asyncio.sleep(0.2)
        await self.broadcaster.emit_to_namespace("hide_next_round", {})

    async def _post_round_flow(self):
        self.logger.info("Flow: post-round")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets", {"event": "reset", "data": None}
        )
        self.obs.set_scene(self._scene("main"))
        await asyncio.sleep(4)
        await self.broadcaster.emit_to_namespace("show_round_results", {})
        await asyncio.sleep(10)
        await self.broadcaster.emit_to_namespace("hide_round_results", {})

    async def _post_fight_flow(self):
        self.logger.info("Flow: post-fight")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets", {"event": "reset", "data": None}
        )
        self.obs.set_scene(self._scene("main"))
        await asyncio.sleep(2)
        await self.broadcaster.emit_to_namespace("show_win", {})
        await asyncio.sleep(8)
        await self.broadcaster.emit_to_namespace("hide_win", {})
        await self._post_round_flow()

    async def _start_ivr_flow(self):
        self.logger.info("Flow: start-IVR")
        self.obs.set_transition(self._transition("stinger"))
        await asyncio.sleep(2)
        await self.broadcaster.emit_to_namespace("show_ivr", {})
        await asyncio.sleep(5)
        await self.broadcaster.emit_to_namespace("hide_ivr", {})
        self.obs.set_scene(self._scene("ivr"))
        await asyncio.sleep(3)
        self.obs.set_transition(self._transition("stinger"))

    async def _start_ivr_closeup_flow(self):
        self.logger.info("Flow: start-IVR-closeup")
        self.obs.set_transition(self._transition("move"))
        self.obs.set_scene(self._scene("ivr_closeup"))
        await asyncio.sleep(3)
        self.obs.set_transition(self._transition("stinger"))

    async def _post_ivr_flow(self):
        self.logger.info("Flow: post-IVR")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets", {"event": "reset", "data": ["widget-ivr"]}
        )
        self.obs.set_scene(self._scene("main_scoreboard"))
        await asyncio.sleep(3)
        self.obs.set_transition(self._transition("move"))

    async def _message_broadcast_flow(self, message: str):
        self.logger.info(f"Flow: message-broadcast → '{message}'")
        await self.broadcaster.emit_to_namespace(
            "show_ticker", {"event": "show", "message": message}
        )

    async def _troubleshooting_flow(self):
        self.logger.info("Flow: troubleshooting")
        await self.broadcaster.emit_to_namespace(
            "reset_widgets", {"event": "reset", "data": None}
        )
        self.obs.set_transition(self._transition("stinger"))
        await asyncio.sleep(0.5)
        self.obs.set_scene(self._scene("troubleshoot"))

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

    def trigger_pre_tournament(self):
        self.start_killable_flow(self._pre_tournament_flow())

    def trigger_pre_pre_fight(self):
        self.start_killable_flow(self._pre_pre_fight_flow())

    def trigger_pre_fight(self):
        self.start_killable_flow(self._pre_fight_flow())

    def trigger_start_fight(self):
        self.start_async_flow(self._start_fight_flow())

    def trigger_start_round(self):
        self.start_killable_flow(self._start_round_flow())

    def trigger_post_round(self):
        self.start_killable_flow(self._post_round_flow())

    def trigger_post_fight(self):
        self.start_killable_flow(self._post_fight_flow())

    def trigger_start_ivr(self):
        self.start_killable_flow(self._start_ivr_flow())

    def trigger_start_ivr_closeup(self):
        self.start_killable_flow(self._start_ivr_closeup_flow())

    def trigger_post_ivr(self):
        self.start_killable_flow(self._post_ivr_flow())

    def trigger_message_broadcast(self, message: str):
        self.start_async_flow(self._message_broadcast_flow(message))

    def trigger_troubleshooting(self):
        self.start_nuke_flow(self._troubleshooting_flow())

    def trigger_youtube_subscribe(self):
        self.start_async_flow(self._youtube_subscribe_flow())