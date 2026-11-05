import asyncio
from typing import Optional

from core.domain.ports.database_port import DatabasePort
from core.domain.ports.overlay_broadcaster_port import OverlayBroadcaster
from plugins.core.livestream.base_automation import BaseAutomationEngine
from plugins.tkstrike.livestream_extension.settings import TkStrikeSettings

# Where the fight is — used to decide whether IVR goes on air and to put the
# overlay back in the right state after a replay.
PHASE_IDLE      = "idle"        # between tournaments / nothing loaded
PHASE_PRE_FIGHT = "pre_fight"   # fighters loaded, clock not started
PHASE_ROUND     = "round"
PHASE_BREAK     = "break"
PHASE_WIN       = "win"         # winner declared, match over

# Temporary widgets: shown for a moment, must never outlive it.
_TEMP_WIDGET_IDS = ["widget-winner", "widget-round-results", "widget-next-round", "widget-ivr"]
_TEMP_HIDE_EVENTS = (
    "hide_ivr", "hide_win", "hide_round_results",
    "hide_previous_results", "hide_fighter_bars", "hide_yt",
)
_ROUND_RESET_IDS = ["widget-winner", "widget-round-results", "widget-next-round"]

_IVR_SCENES = ("ivr", "ivr_closeup")


class TkStrikeStreamAutomation(BaseAutomationEngine):
    """
    All OBS scene switches and Socket.IO overlay broadcasts for Kyorugi.

    Every public on_* method is an async EventBus handler:
        async def on_*(self, data: dict) -> None

    Flows are called via trigger_* wrappers so the caller stays synchronous
    while the flow runs in a managed asyncio task.

    Robustness rules (each one closes a "stuck widget" case):
      * every show_X has its hide_X in a `finally` (_show_for), so a flow that
        gets cancelled mid-sleep still hides what it put up;
      * the OBS transition is set right before each scene switch
        (_switch_scene), never "prepared" for a later one;
      * IVR goes on air only while a match is active, is tracked as state
        (_ivr_on_stream) and always ends: replay_closed, IVR disconnect, or
        the fight moving on (round / break / win / new fight);
      * after a replay the overlay is rebuilt from the fight phase instead of
        relying on whatever was on screen before.
    """

    def __init__(
        self,
        obs_adapter,
        broadcaster: OverlayBroadcaster,
        settings: TkStrikeSettings,
        logger,
        db=None, tournament_id=None,
    ):
        super().__init__(logger)
        self.obs         = obs_adapter
        self.broadcaster = broadcaster
        self.settings    = settings
        self.db: DatabasePort = db
        self.tournament_id = tournament_id

        self._youtube_loop_task: Optional[asyncio.Task] = None
        self._history_task: Optional[asyncio.Task] = None
        # Fighter bars / previous results at fight start. Own slot: it must
        # run alongside the start-round flow (both fire on the same packet),
        # but still be cancellable by a new fight, IVR, win or troubleshoot.
        self._intro_task: Optional[asyncio.Task] = None

        # Match / IVR state
        self._phase = PHASE_IDLE
        self._match_active = False     # START_FIGHT … WIN
        self._replay_open = False      # IVR app has its replay screen open
        self._ivr_on_stream = False    # … and the livestream is showing it
        self._is_mirror = False        # external screen mirrored (→ closeup)
        self._current_scene: Optional[str] = None

    # ------------------------------------------------------------------ settings shorthands

    def _scene(self, key: str) -> str:
        return getattr(self.settings, f"obs_scene_{key}")

    def _transition(self, key: str) -> str:
        return getattr(self.settings, f"obs_transition_{key}")

    # ------------------------------------------------------------------ safe output helpers

    async def _emit(self, event: str, payload: Optional[dict] = None):
        """Overlay emit that never raises — a failed emit must not abort the
        flow before its hide/cleanup steps."""
        try:
            await self.broadcaster.emit_to_namespace(event, payload if payload is not None else {})
        except Exception as exc:
            self.logger.error(f"Overlay emit '{event}' failed", exception=exc)

    async def _reset_widgets(self, ids: Optional[list]):
        await self._emit("reset_widgets", {"event": "reset", "data": ids})

    async def _show_for(self, show: str, hide: str, seconds: float, payload: Optional[dict] = None):
        """show → wait → hide, where the hide ALWAYS happens (also on cancel)."""
        await self._emit(show, payload)
        try:
            await asyncio.sleep(seconds)
        finally:
            await self._emit(hide)

    def _switch_scene(self, scene_key: str, transition_key: str):
        """Transition is chosen together with the switch it applies to."""
        if not self.obs:
            return
        try:
            self.obs.set_transition(self._transition(transition_key))
            self.obs.set_scene(self._scene(scene_key))
            self._current_scene = scene_key
        except Exception as exc:
            self.logger.error(f"OBS switch to '{scene_key}' failed", exception=exc)

    async def _clear_temp_widgets(self):
        await self._reset_widgets(_TEMP_WIDGET_IDS)
        for event in _TEMP_HIDE_EVENTS:
            await self._emit(event)

    # ------------------------------------------------------------------ state helpers

    def _set_phase(self, phase: str, active: bool):
        self._phase = phase
        self._match_active = active

    def _cancel_intro(self):
        if self._intro_task and not self._intro_task.done():
            self._intro_task.cancel()
        self._intro_task = None

    async def _drop_ivr_from_stream(self, reason: str):
        """The fight moved on while IVR was still on air (replay_closed missed,
        IVR crashed…): take IVR off the stream before the next flow runs."""
        if not self._ivr_on_stream:
            return
        self.logger.warning(f"IVR still on stream but {reason} — leaving the IVR scene.")
        self._ivr_on_stream = False
        await self._reset_widgets(["widget-ivr"])
        await self._emit("hide_ivr")
        self._switch_scene("main", "stinger")

    def shutdown(self):
        """Cancel every background task (called on extension teardown)."""
        for task in (self._youtube_loop_task, self._history_task, self._intro_task):
            if task and not task.done():
                task.cancel()

    # ================================================================== scoreboard handlers

    async def on_stable_update(self, data: dict):
        """Pushes full scoreboard state to the browser overlay."""
        await self._emit("listener_update", {"event": "update", "data": data})

    async def on_clock_update(self, data: dict):
        await self._emit("clock_update", {"event": "update", "data": data})

    async def on_blue_scored(self, data: dict):
        await self._emit("blue_scored", {"event": "update", "data": data})

    async def on_red_scored(self, data: dict):
        await self._emit("red_scored", {"event": "update", "data": data})

    async def _emit_previous(self, corner: str, athlete: dict, fights: list[dict]):
        await self._emit(
            "previous_results_update",
            {"event": "update", "data": {
                "corner": corner,
                "athlete_name": athlete.get("name", ""),
                "athlete_flag2": athlete.get("flag2", "un"),
                "athlete_flag3": athlete.get("flag3", ""),
                "fights": fights,
            }},
        )
        await self._emit("show_previous_results")

    # ================================================================== fight lifecycle handlers

    async def on_livestream_start(self, data: dict):
        """Fired when this extension finishes setup (livestream infrastructure online)."""
        self.trigger_pre_tournament()

    async def on_tournament_start(self, data: dict):
        """Fired manually by the operator to kick off a session."""
        await self._drop_ivr_from_stream("tournament (re)started")
        self._set_phase(PHASE_IDLE, active=False)
        self._cancel_intro()
        self.trigger_pre_pre_fight()

    async def on_new_fight(self, data: dict):
        """Both mch + at1 received — new fighters loaded."""
        await self._drop_ivr_from_stream("a new fight was loaded")
        self._set_phase(PHASE_PRE_FIGHT, active=False)
        self._cancel_intro()
        self.trigger_pre_fight(data)

    async def on_start_fight(self, data: dict):
        """Fight clock has started."""
        self._match_active = True
        self.trigger_start_fight()

    async def on_start_round(self, data: dict):
        """New round started (clk packet while not in fight/init)."""
        await self._drop_ivr_from_stream("the round started")
        self._set_phase(PHASE_ROUND, active=True)
        self.trigger_start_round()

    async def on_start_break(self, data: dict):
        """Round ended (brk packet)."""
        await self._drop_ivr_from_stream("the break started")
        self._set_phase(PHASE_BREAK, active=True)
        self.trigger_post_round()

    async def on_win(self, data: dict):
        """Winner declared (win packet)."""
        await self._drop_ivr_from_stream("the winner was declared")
        self._set_phase(PHASE_WIN, active=False)
        self._cancel_intro()
        self.trigger_post_fight()

    # ================================================================== IVR handlers

    async def on_ivr_replay_opened(self, data: dict):
        self._replay_open = True
        if self._ivr_on_stream:
            self.logger.info("IVR replay_opened repeated — already on stream, ignoring.")
            return
        if not self._match_active:
            self.logger.info(
                f"IVR replay opened outside an active match (phase: {self._phase}) "
                f"— not shown on the livestream."
            )
            return
        self._ivr_on_stream = True
        self._cancel_intro()
        self.logger.info("IVR replay opened → IVR broadcast flow.")
        self.trigger_start_ivr()

    async def on_ivr_replay_closed(self, data: dict):
        self._replay_open = False
        if not self._ivr_on_stream:
            return   # replay was never on air (no active match) or already dropped
        self._ivr_on_stream = False
        self.logger.info("IVR replay closed → post-IVR flow.")
        self.trigger_post_ivr()

    async def on_ivr_disconnected(self, data: dict):
        """IVR app crashed / quit / restarted: nothing will ever send replay_closed."""
        if self._ivr_on_stream:
            self.logger.warning("IVR disconnected with the replay on air — closing IVR on the livestream.")
        await self.on_ivr_replay_closed(data)

    async def on_ivr_recording_started(self, data: dict):
        self.logger.info(f"IVR recording started for fight {data.get('fight_num')}.")

    async def on_screen_mirror_changed(self, data: dict):
        """Mirror on → closeup of the replay; mirror off → back to the normal IVR scene.
        The state is remembered, so a replay opened while already mirrored
        goes straight to the closeup."""
        mirror = bool(data.get("is_mirror"))
        if mirror == self._is_mirror:
            return
        self._is_mirror = mirror
        if not self._ivr_on_stream:
            return
        self.logger.info(
            "Screen mirrored during IVR replay → IVR closeup flow." if mirror
            else "Screen un-mirrored during IVR replay → back to IVR scene."
        )
        self.trigger_ivr_view(closeup=mirror)

    # ================================================================== operator handlers

    async def on_message_broadcast(self, data: dict):
        """Show a ticker message on the overlay."""
        self.trigger_message_broadcast(data.get("message", ""))

    async def on_troubleshoot(self, data: dict):
        """Switch OBS to the troubleshoot scene."""
        self._ivr_on_stream = False   # the troubleshoot scene wins; a later replay_closed must not undo it
        self._cancel_intro()
        self.trigger_troubleshooting()

    # ================================================================== db

    async def _fetch_history(self, athlete_name: str | None) -> list[dict]:
        if not athlete_name:
            return []
        raw = await self.db.fetch_previous_fights("taekwondo", "kyorugi", self.tournament_id, athlete_name)
        return [self._shape_previous_fight(m, athlete_name) for m in raw]

    @staticmethod
    def _shape_previous_fight(match: dict, athlete_name: str) -> dict:
        is_blue = match.get("blue", {}).get("name") == athlete_name
        opponent = match.get("red" if is_blue else "blue") or {}
        return {
            "match_id": match.get("match_id"),
            "win_score": match.get("win_score"),
            "round_label": match.get("title", ""),
            "opponent_name": opponent.get("name", ""),
            "opponent_flag2": opponent.get("flag2", "un"),
            "opponent_flag3": opponent.get("flag3", ""),
        }

    # ================================================================== flows

    async def _pre_tournament_flow(self):
        self.logger.info("Flow: pre-tournament")
        self._switch_scene("starting", "move")

    async def _pre_pre_fight_flow(self):
        """Between tournaments — clean UI and (re)start the YouTube reminder loop."""
        self.logger.info("Flow: pre-pre-fight")
        await self._reset_widgets(["widget-winner", "widget-round-results"])
        self._switch_scene("main", "move")

        if self._youtube_loop_task and not self._youtube_loop_task.done():
            self._youtube_loop_task.cancel()
        self._youtube_loop_task = asyncio.create_task(
            self._youtube_loop_manager(self.settings.youtube_subscribe_interval_mins)
        )

    async def _pre_fight_flow(self, data):
        self.logger.info("Flow: pre-fight")
        await self._reset_widgets(None)
        self._switch_scene("main", "move")

        # Start the history fetch before any sleep, so a cancel of this flow
        # can't leave the new fight without (or with the old) history.
        if self._history_task and not self._history_task.done():
            self._history_task.cancel()
        blue, red = data.get("blue", {}), data.get("red", {})
        self._history_task = None
        if self.db and self.tournament_id and (blue.get("name") or red.get("name")):
            self._history_task = asyncio.create_task(self._load_history(blue, red))

        await asyncio.sleep(0.5)
        await self._emit("show_next_round")

    async def _show_previous_results(self, history: dict):
        blue, blue_history = history["blue"], history["blue_history"]
        red, red_history = history["red"], history["red_history"]
        try:
            if blue_history:
                await self._emit_previous("blue", blue, blue_history)
                await asyncio.sleep(5)
            if red_history:
                await self._emit_previous("red", red, red_history)
                await asyncio.sleep(5)
        finally:
            await self._emit("hide_previous_results")

    async def _start_fight_flow(self):
        self.logger.info("Flow: start-fight")
        # Only the temporary widgets — a full reset here raced the start-round
        # flow (same packet) and could hide the scoreboard it just showed.
        await self._reset_widgets(_ROUND_RESET_IDS)

        history = await self._resolve_history()
        has_previous = bool(history and (history["blue_history"] or history["red_history"]))

        if has_previous:
            self.logger.info("Previous fights found — showing previous-results instead of fighter bars.")
            await self._show_previous_results(history)
        else:
            await self._show_for("show_fighter_bars", "hide_fighter_bars", 6)

    async def _run_intro(self):
        try:
            await self._start_fight_flow()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.logger.error("Start-fight flow failed", exception=exc)

    async def _resolve_history(self, timeout: float = 5.0) -> Optional[dict]:
        task = self._history_task
        if not task:
            return None
        try:
            # shield: cancelling THIS flow must not cancel the shared fetch,
            # and must not be mistaken for a cancelled fetch either.
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.warning("Previous-fight fetch timed out — falling back to fighter bars.")
            return None
        except asyncio.CancelledError:
            if task.cancelled():
                return None   # the fetch was replaced by a newer fight
            raise             # the flow itself was cancelled → stop here
        except Exception as exc:
            self.logger.error("Previous-fight fetch failed", exception=exc)
            return None

    async def _load_history(self, blue: dict, red: dict) -> dict:
        try:
            blue_history, red_history = await asyncio.gather(
                self._fetch_history(blue.get("name")),
                self._fetch_history(red.get("name")),
            )
        except Exception as exc:
            self.logger.error("Failed to fetch previous fights", exception=exc)
            blue_history, red_history = [], []

        return {"blue": blue, "blue_history": blue_history, "red": red, "red_history": red_history}

    async def _start_round_flow(self):
        self.logger.info("Flow: start-round")
        await self._reset_widgets(_ROUND_RESET_IDS)
        await self._emit("hide_break")
        await self._emit("show_scoreboard")
        await asyncio.sleep(8)
        await self._emit("compact_scoreboard")

    async def _post_round_flow(self):
        self.logger.info("Flow: post-round")
        await self._reset_widgets(_ROUND_RESET_IDS)
        await self._emit("show_break")
        await asyncio.sleep(4)
        await self._emit("uncompact_scoreboard")
        await self._show_for("show_round_results", "hide_round_results", 10)

    async def _post_fight_flow(self):
        self.logger.info("Flow: post-fight")
        await self._reset_widgets(None)
        await asyncio.sleep(2)
        await self._show_for("show_win", "hide_win", 8)
        await self._post_round_flow()

    async def _start_ivr_flow(self):
        self.logger.info("Flow: start-IVR")
        await asyncio.sleep(2)
        await self._show_for("show_ivr", "hide_ivr", 5)
        # Mirrored already when the replay opened → straight to the closeup.
        self._switch_scene("ivr_closeup" if self._is_mirror else "ivr", "stinger")

    async def _ivr_view_flow(self, closeup: bool):
        target = "ivr_closeup" if closeup else "ivr"
        self.logger.info(f"Flow: IVR view → {target}")
        # Between the two IVR scenes: move. Coming from main (banner still
        # running, cancelled by this flow): stinger, like the normal entry.
        transition = "move" if self._current_scene in _IVR_SCENES else "stinger"
        self._switch_scene(target, transition)

    async def _post_ivr_flow(self):
        self.logger.info(f"Flow: post-IVR (back to {self._phase})")
        await self._clear_temp_widgets()
        self._switch_scene("main", "stinger")
        await self._restore_phase()

    async def _restore_phase(self):
        """Rebuild the overlay for the current fight phase — whatever flow the
        replay interrupted, the stream comes back in a consistent state."""
        if self._phase == PHASE_ROUND:
            await self._emit("hide_break")
            await self._emit("show_scoreboard")
            await self._emit("compact_scoreboard")
        elif self._phase == PHASE_BREAK:
            await self._emit("show_scoreboard")
            await self._emit("uncompact_scoreboard")
            await self._emit("show_break")

    async def _message_broadcast_flow(self, message: str):
        self.logger.info(f"Flow: message-broadcast → '{message}'")
        await self._emit("show_ticker", {"event": "show", "message": message})

    async def _troubleshooting_flow(self):
        self.logger.info("Flow: troubleshooting")
        await self._reset_widgets(None)
        await asyncio.sleep(0.5)
        self._switch_scene("troubleshoot", "stinger")

    async def _youtube_subscribe_flow(self):
        if self._ivr_on_stream or self._current_scene in (*_IVR_SCENES, "troubleshoot"):
            self.logger.info("YouTube subscribe skipped — IVR / troubleshoot on air.")
            return
        await self._show_for("show_yt", "hide_yt", 10)

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

    def trigger_pre_fight(self, data):
        self.start_killable_flow(self._pre_fight_flow(data))

    def trigger_start_fight(self):
        self._cancel_intro()
        self._intro_task = asyncio.create_task(self._run_intro())

    def trigger_start_round(self):
        self.start_killable_flow(self._start_round_flow())

    def trigger_post_round(self):
        self.start_killable_flow(self._post_round_flow())

    def trigger_post_fight(self):
        self.start_killable_flow(self._post_fight_flow())

    def trigger_start_ivr(self):
        self.start_killable_flow(self._start_ivr_flow())

    def trigger_ivr_view(self, closeup: bool):
        self.start_killable_flow(self._ivr_view_flow(closeup))

    def trigger_start_ivr_closeup(self):
        """Kept for compatibility (REST / other callers)."""
        self.trigger_ivr_view(closeup=True)

    def trigger_post_ivr(self):
        self.start_killable_flow(self._post_ivr_flow())

    def trigger_message_broadcast(self, message: str):
        self.start_async_flow(self._message_broadcast_flow(message))

    def trigger_troubleshooting(self):
        self.start_nuke_flow(self._troubleshooting_flow())

    def trigger_youtube_subscribe(self):
        self.start_async_flow(self._youtube_subscribe_flow())