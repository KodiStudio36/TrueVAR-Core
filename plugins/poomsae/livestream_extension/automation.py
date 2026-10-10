import asyncio
from typing import Optional

from core.domain.ports.overlay_broadcaster_port import OverlayBroadcaster
from plugins.core.livestream.base_automation import BaseAutomationEngine
from plugins.poomsae.livestream_extension.deductions import MajorityDeductions
from plugins.poomsae.livestream_extension.settings import PoomsaeLivestreamSettings

POOMSAE_STAGES = ("poomsae_1", "poomsae_2")


class PoomsaeStreamAutomation(BaseAutomationEngine):
    """
    OBS scene switches and Socket.IO overlay broadcasts for Poomsae.

    Data comes from the Poomsae Control Panel plugin over the event bus:
        poomsae.live_update    full scoreboard payload (see live.py)
        poomsae.stage_changed  live match / stage changed

    What the overlay shows, per stage:
        Preparation   scoreboard header only (number + category)
        Poomsae 1/2   full scoreboard; names shorten compact_after_seconds
                      after the rows appear; clock folds in after the timer
                      starts and comes back on pause
        Results 1/2   scoreboard back to the header + Results widget
                      (accuracy / presentation per judge)
        anything else nothing

    Every public on_* method is an async EventBus handler, same contract
    as TkStrikeStreamAutomation.
    """

    def __init__(self, obs_adapter, broadcaster: OverlayBroadcaster, settings: PoomsaeLivestreamSettings, logger):
        super().__init__(logger)
        self.obs = obs_adapter
        self.broadcaster = broadcaster
        self.settings = settings
        self._deductions = MajorityDeductions()
        # Short names: a timer of its own (not a killable flow, so scene
        # flows can never cancel it), restarted whenever the rows appear.
        self._compact = False
        self._rows_visible = False
        self._compact_task: Optional[asyncio.Task] = None
        # Athlete intro at Poomsae 1 — once per match.
        self._last_data: dict = {}
        self._intro_shown_for: set = set()
        self._intro_task: Optional[asyncio.Task] = None
        # While the intro is up, the scoreboard and the poomsae name wait.
        self._intro_active = False
        self._warned_empty_category = None

    def _scene(self, key: str) -> str:
        return getattr(self.settings, f"obs_scene_{key}")

    def _transition(self, key: str) -> str:
        return getattr(self.settings, f"obs_transition_{key}")

    # ================================================================== compact (short names)

    async def _emit_compact(self, compact: bool):
        self._compact = compact
        await self.broadcaster.emit_to_namespace(
            "compact_scoreboard" if compact else "uncompact_scoreboard", {}
        )

    async def _compact_after_delay(self, delay: float):
        try:
            await asyncio.sleep(delay)
            if self._rows_visible:
                await self._emit_compact(True)
        except asyncio.CancelledError:
            pass

    async def _track_rows(self, rows_visible: bool):
        if rows_visible and not self._rows_visible:
            # Rows just appeared (scoreboard shown, or Preparation → Poomsae):
            # full names now, short names after the delay.
            if self._compact_task and not self._compact_task.done():
                self._compact_task.cancel()
            self._rows_visible = True
            await self._emit_compact(False)
            delay = self.settings.compact_after_seconds
            if delay > 0:
                self._compact_task = asyncio.create_task(self._compact_after_delay(delay))
        elif not rows_visible and self._rows_visible:
            self._rows_visible = False
            if self._compact_task and not self._compact_task.done():
                self._compact_task.cancel()
            await self._emit_compact(False)

    def shutdown(self):
        for task in (self._compact_task, self._intro_task):
            if task and not task.done():
                task.cancel()

    # ================================================================== athlete intro

    async def _athlete_intro(self, match_id, seconds: int):
        """Name / flag / club card, bottom-center, at the start of Poomsae 1."""
        try:
            # stage_changed is published just before the live update of the
            # same tick — give the payload a moment to arrive.
            await asyncio.sleep(0.6)
            data = self._last_data
            if data.get("match_id") != match_id:
                return
            athletes = [
                {"corner": color, **{k: c.get(k) for k in ("name", "club", "flag2", "flag3")}}
                for color in ("blue", "red")
                if (c := data.get(color))
            ]
            if not athletes:
                return
            await self.broadcaster.emit_to_namespace("athlete_intro", {"event": "update", "data": {
                "match_id": match_id,
                "is_solo": bool(data.get("is_solo")),
                "category": data.get("category", ""),
                "phase": data.get("phase", ""),
                "athletes": athletes,
            }})
            await self.broadcaster.emit_to_namespace("show_athlete_intro", {})
            await asyncio.sleep(seconds)
            await self.broadcaster.emit_to_namespace("hide_athlete_intro", {})
            # Let the intro slide out before the scoreboard comes in.
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            await self.broadcaster.emit_to_namespace("hide_athlete_intro", {})
        finally:
            if self._intro_task is asyncio.current_task():
                self._intro_active = False
                if self._last_data:
                    # Bring the scoreboard + poomsae name in right away rather
                    # than on the next update.
                    await self.on_live_update(self._last_data)

    # ================================================================== scoreboard

    async def on_live_update(self, data: dict):
        """One bus event → the overlay events. Order matters: data is bound
        before anything is shown."""
        data = data or {}
        cat = data.get("category_results")
        if cat is not None:
            rows = cat.get("rows") or []
            if not rows and self._warned_empty_category != data.get("match_id"):
                self._warned_empty_category = data.get("match_id")
                self.logger.warning("Category Results: no ranking rows found in match_data.categoryResults — "
                                    "check live.shape_category against standings.build_category_view.")
            data = {**data, "category_results": {**cat, "rows": rows[: self.settings.category_max_rows]}}
        self._last_data = data
        in_results = bool(data.get("result"))
        # Results and Final Score carry the number / category themselves →
        # no top-left scoreboard then. During the athlete intro it waits too.
        visible = bool(data.get("visible")) and not in_results and not self._intro_active
        header_only = bool(data.get("header_only"))

        await self._track_rows(visible and not header_only)

        # Clock folds into the scoreboard N s after the timer starts; back on pause.
        collapse_after = self.settings.clock_collapse_after_seconds
        run = data.get("run_seconds")
        overtime = str(data.get("clk", "")).startswith("-")
        presentation = bool(data.get("presentation_mode"))
        clock_hidden = presentation or bool(
            collapse_after > 0 and data.get("timer_running") and run is not None and run >= collapse_after
            # Overtime (-0:01 …) always shows the clock, in red.
            and not overtime
        )

        await self.broadcaster.emit_to_namespace(
            "listener_update", {"event": "update", "data": data}
        )
        await self.broadcaster.emit_to_namespace(
            "clock_update", {"event": "update", "data": {"clk": data.get("clk", "")}}
        )
        await self.broadcaster.emit_to_namespace(
            "scoreboard_sync", {"event": "update", "data": {
                "visible": visible,
                "header_only": header_only,
                "result": in_results,
                "compact": self._compact,
                "clock_hidden": clock_hidden,
                # Clock stopped + most judges on presentation marks: clock,
                # round and accuracy hide, a PRESENTATION chip shows.
                "presentation": presentation,
                # Drawn poomsae, bottom-left — while performing, if one was drawn.
                "show_poomsae_name": visible and not header_only and bool(data.get("poomsae_name")),
                # Accuracy / presentation per judge, bottom-center.
                "show_results": in_results and self.settings.show_results_widget,
                # Final Results: P1 / P2 totals + overall, kyorugi winner style.
                "show_final": bool(data.get("final_visible")) and self.settings.show_final_widget,
                # Category Results: ranked list (bottom ≤5 rows, centred above).
                "show_category": bool((data.get("category_results") or {}).get("rows"))
                and self.settings.show_category_widget,
            }}
        )

        # "-0.3" only when most judges made that call together — and never for
        # para / freestyle, which have no live accuracy score.
        flashes = self._deductions.update(data, self.settings.deduction_window_seconds)
        if data.get("live_score") is False:
            flashes = []
        for flash in flashes:
            await self.broadcaster.emit_to_namespace(
                "deduction_flash", {"event": "update", "data": flash}
            )

    async def on_stage_changed(self, data: dict):
        stage = (data or {}).get("stage")
        self.logger.info(f"Poomsae stage → {stage} (match {data.get('match_id')})")
        if stage in POOMSAE_STAGES:
            self.trigger_start_poomsae()

        match_id = (data or {}).get("match_id")
        seconds = self.settings.athlete_intro_seconds
        if stage == "poomsae_1" and match_id and seconds > 0 and match_id not in self._intro_shown_for:
            # Only the first time this match reaches Poomsae 1 (not after a
            # Reset, not on Poomsae 2).
            self._intro_shown_for.add(match_id)
            if self._intro_task and not self._intro_task.done():
                self._intro_task.cancel()
            self._intro_active = True
            self._intro_task = asyncio.create_task(self._athlete_intro(match_id, seconds))
        elif stage != "poomsae_1" and self._intro_task and not self._intro_task.done():
            self._intro_task.cancel()

    # ================================================================== system / operator

    async def on_livestream_start(self, data: dict):
        self.trigger_pre_tournament()

    async def on_message_broadcast(self, data: dict):
        self.start_async_flow(self._message_broadcast_flow((data or {}).get("message", "")))

    # ================================================================== flows

    async def _pre_tournament_flow(self):
        self.logger.info("Flow: pre-tournament")
        if self.obs:
            self.obs.set_scene(self._scene("starting"))
            self.obs.set_transition(self._transition("move"))

    async def _start_poomsae_flow(self):
        self.logger.info("Flow: start-poomsae")
        if self.obs:
            self.obs.set_scene(self._scene("main"))

    async def _message_broadcast_flow(self, message: str):
        await self.broadcaster.emit_to_namespace("show_ticker", {"event": "show", "message": message})

    # ================================================================== trigger wrappers

    def trigger_pre_tournament(self):
        self.start_killable_flow(self._pre_tournament_flow())

    def trigger_start_poomsae(self):
        self.start_killable_flow(self._start_poomsae_flow())