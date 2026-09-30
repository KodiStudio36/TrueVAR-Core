import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from plugins.base_plugin import BasePlugin, Namespace, PluginState

# The operator UI is a standalone HTML/JS file served straight off disk by
# this plugin's own router (see get_router) — this is NOT part of the main
# website, exactly as asked. Served, not templated: no Jinja, no server-
# rendered state — the page fetches its own state from this plugin's API
# on load and after every action.
PANEL_HTML_PATH = Path(__file__).parent / "static" / "panel.html"
# The audience-facing external screen — a SEPARATE static file from the
# operator panel, served at GET /screen. Polls the SAME /state endpoint
# the operator panel already uses (read-only from its perspective — it
# never calls any of the write endpoints below), so there's no second
# data-fetching path to keep in sync on the backend side.
SCREEN_HTML_PATH = Path(__file__).parent / "static" / "screen.html"

# ── Match lifecycle ─────────────────────────────────────────────────────
# Preparation -> Poomsae 1 -> Poomsae 2 -> Final Results. The operator
# manually advances through the first three (Next/Previous Stage buttons
# on the panel); Final Results is NEVER manually triggered — see
# _poll_loop, which watches for every judge having submitted a score and
# advances into it automatically once that's true. There is deliberately
# no "Show Results" button in this plugin at all.
STAGES = ["preparation", "poomsae_1", "poomsae_2", "final_results"]
STAGE_LABELS = {
    "preparation": "Preparation",
    "poomsae_1": "Poomsae 1",
    "poomsae_2": "Poomsae 2",
    "final_results": "Final Results",
}
# Maps this plugin's own stage strings onto the RTDB child key the judge
# app (judge_app/) actually writes scores under — MUST stay in lockstep
# with PoomsaeRound.rtdbKey in judge_app/lib/features/scoring/domain/
# entities/poomsae_round.dart. Only the two scoreable stages have an
# entry; "preparation"/"final_results" are intentionally absent — see
# _poll_loop, which uses membership in this dict to decide whether the
# current stage is even scoreable at all.
STAGE_TO_ROUND_KEY = {"poomsae_1": "poomsae1", "poomsae_2": "poomsae2"}


# Round-phase codes the bracket builder writes on poomsae matches, as
# shown to people. Round robin rounds are "RR1", "RR2", …; single
# elimination uses R{n}/QF/SF/F like kyorugi.
ROUND_PHASE_LABELS = {
    "PR": "Preliminary Round",
    "SF": "Semi-Final",
    "QF": "Quarter-Final",
    "F": "Final",
    "EXH": "Exhibition",
}


def round_phase_label(code: Optional[str]) -> str:
    if not code:
        return ""
    if code in ROUND_PHASE_LABELS:
        return ROUND_PHASE_LABELS[code]
    if code.startswith("RR") and code[2:].isdigit():
        return f"Round {code[2:]}"
    if code.startswith("R") and code[1:].isdigit():
        return f"Round of {code[1:]}"
    return code


def category_label_from_code(code: Optional[str]) -> str:
    """
    Readable category name from a category code, the same way kyorugi
    shows it: every "_" becomes a space and each word gets a capital first
    letter — "para_individual_poomsae_seniors_p72_male" →
    "Para Individual Poomsae Seniors P72 Male". Match docs pushed by the
    bracket builder carry only categoryCode; a match inserted by hand from
    the live queue also has a categoryLabel, which is used as-is.
    """
    if not code:
        return ""
    return " ".join(word[:1].upper() + word[1:] for word in str(code).split("_") if word)


def match_category_label(match: Optional[dict]) -> str:
    if not match:
        return ""
    return match.get("categoryLabel") or category_label_from_code(match.get("categoryCode"))


def format_match_number(value) -> str:
    """Poomsae match numbers print zero-padded (001, 002 …), the same as
    the bracket builder and printed sheets. Inserted matches can carry a
    fractional number (12.5) — those print as-is."""
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return f"{value:03d}"
    return str(value)


def _normalize_firebase_map(value) -> dict:
    """
    Firebase Realtime Database has a well-known quirk: a node whose
    children are ALL small, sequential integer-LIKE keys (exactly what
    judge numbers are — "1", "2", "3", ...) can come back from a read
    as a JSON ARRAY instead of an object, not a dict — the exact shape
    depends on which numbers happen to be present, and isn't something
    this plugin controls or can rely on being stable. firebase_admin's
    Python SDK passes whichever shape Firebase decided on straight
    through, so `.get("judges")` / fetch_judges_registry can just as
    easily hand back a `list` as a `dict`.

    Every place in this file that reads one of these maps normalizes it
    through here FIRST, rather than assuming either shape — a list gets
    turned into a dict keyed by its own (stringified) index, skipping
    None entries (Firebase pads a sparse array with nulls for missing
    indices, e.g. no "judges/0" slot existing at all still produces
    index 0 = null in the array form).
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(i): v for i, v in enumerate(value) if v is not None}
    return {}


class PoomsaeControlSettings(BaseModel):
    # Poomsae round duration ONLY — Preparation has no timer at all
    # (see STAGE_DURATION_SETTING / _duration_for_stage), so there's
    # nothing to configure for it. "The time resets on every poomsae"
    # still holds: this is the value the countdown resets TO whenever a
    # poomsae round is (re)entered, before the operator manually starts
    # it — see the module comment on TIMER MODEL below.
    poomsae_seconds: int = Field(90, ge=5)
    # How many judge slots this court has — the operator no longer adds
    # them one at a time via a "+ Add Judge" button; changing this
    # number is what creates/removes numbered slots (1..judge_count),
    # see _reconcile_judge_slots. 1-7 per the real range of judges an
    # event would ever actually run.
    judge_count: int = Field(3, ge=1, le=7)
    # How the judges' individual Total scores combine into the panel's
    # own Total/Avg/Final Score summary rows — see buildJudgesTable on
    # the panel side, which reads this to pick the aggregation. Real WT
    # competition judging commonly drops the highest and lowest score
    # before averaging (to reduce the effect of one outlier judge);
    # "sum_all" is the simpler alternative that counts everyone.
    scoring_method: str = Field("sum_all", pattern="^(sum_all|trim_high_low)$")
    # Automatic point deduction applied when the operator hits Restart
    # (see reset_round) — real WT poomsae rules apply a standard penalty
    # when a performer has to restart their pattern. Negative by
    # convention (a deduction), -0.6 is the standard WT amount; kept
    # configurable since exact competition rules can vary.
    restart_deduction: float = Field(-0.6)
    # Penalty button (see /penalty) — each tap adds this to the athlete's
    # penalty total for the current round. Unlike the restart deduction it
    # can be given more than once, and in a side-by-side match the
    # operator picks which athlete gets it. -0.3 is the standard amount.
    penalty_deduction: float = Field(-0.3)


class PenaltyPayload(BaseModel):
    corner: str = Field("blue", pattern="^(blue|red)$")


class TimerPausePayload(BaseModel):
    # The panel computes this client-side (full duration minus elapsed
    # time since the timer was last started) and sends it up so the
    # PAUSED value stored in match_data is exact — this plugin has no
    # way to independently verify a browser's own elapsed-time math, and
    # doesn't try to; it just persists whatever the operator's panel
    # says the remaining time was at the moment of the pause tap.
    remainingSeconds: float


class PoomsaeControlPlugin(BasePlugin):
    """
    Operator control panel for poomsae matches. Deliberately NOT part of
    the main tournament website — this is its own plugin, with its own
    UI (served as a static file, see PANEL_HTML_PATH) and its own API,
    following the same BasePlugin contract as e.g.
    TkStrikeListenerGen3Plugin.

    Everything this plugin writes goes to Firebase REALTIME DATABASE at
    /tournaments/{tournament_id}/courts/{court_id}/match_data — see
    push_live_state / update_live_state on FirebaseAdapter (the latter
    is a new addition this plugin needed; see
    firebase_adapter_additions.py). Firestore is never written to by
    this plugin, on purpose — only read, to pull the match schedule
    the bracket builder already pushed via commit_brackets_endpoint.

    tournament_id / court_id / sport_id / discipline_id are NOT plugin
    settings — SystemOrchestrator.boot_tournament already builds
    shared_context with "tournament" (a Tournament entity, .id/.sport/
    .discipline) and "court_id" scoped to this exact boot session before
    any plugin is even constructed, so re-asking the operator to type
    them into settings would just be a second, driftable source of
    truth for the same thing. This plugin reads them straight off
    core_context in start() instead — see _bind_context.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self.tournament_id: Optional[str] = None
        self.court_id: Optional[str] = None
        self.sport_id: Optional[str] = None
        self.discipline_id: Optional[str] = None
        self.matches: list[dict] = []   # every match scheduled on this court, this tournament, queuePosition order
        # SELECTED vs LIVE. Paging through matches (Previous/Next match or
        # category, clicking the queue) only changes active_index — the
        # operator browsing. Nothing reaches RTDB / the external screen
        # until they click Preparation, which makes the selected match the
        # LIVE one (live_match_id). self.stage is always the LIVE match's
        # stage, or None when nothing is live on this court.
        self.active_index: int = 0
        self.live_match_id: Optional[str] = None
        self.stage: Optional[str] = None
        self._poll_task: Optional[asyncio.Task] = None
        # Guards judge-slot reconciliation against overlapping calls
        # (e.g. a settings save landing while startup reconciliation is
        # still running) — this plugin is the single writer of judge
        # slots (see create_judge_slot's own doc comment), so a plain
        # asyncio.Lock here is enough; no need for anything RTDB-side.
        self._judges_lock = asyncio.Lock()
        # Settings changed through THIS panel's own Settings modal (see
        # the /settings endpoint) — layered on top of whatever the
        # framework's own settings store returns, in _settings() below.
        # KNOWN LIMITATION, stated plainly: this is in-memory only and
        # does NOT persist across a plugin restart, unlike settings
        # changed through the framework's own auto-generated settings
        # form elsewhere in the dashboard (see settings_ui's own
        # comment). I don't have visibility into SettingsManager's own
        # write API — only .get_settings() appears anywhere in the code
        # I was given — so layering an override at READ time here is the
        # self-contained choice I can actually verify, rather than
        # guessing a setter method name that might not exist.
        self._settings_override: dict = {}

    @property
    def name(self) -> str:
        return "Poomsae Control Panel"

    @property
    def requires_internet(self) -> bool:
        return True  # talks to Firebase RTDB

    @property
    def settings_schema(self):
        return PoomsaeControlSettings

    namespace: str = Namespace.BOUT_CNTRL
    # Few enough fields (tournament/court ids, two durations, judge
    # count) that the dashboard's auto-generated settings form is fine —
    # no need for a hand-built settings_ui here.
    settings_ui: Optional[str] = None

    def _settings(self) -> PoomsaeControlSettings:
        base = self.core_context["settings_manager"].get_settings(self.name)
        if not self._settings_override:
            return base
        return base.model_copy(update=self._settings_override)

    @property
    def db(self):
        # SystemOrchestrator wires the DatabasePort adapter in under
        # "db" (see boot_tournament's shared_context) — not "database".
        return self.core_context["db"]

    def _bind_context(self) -> bool:
        """
        Pulls tournament/court identity straight off core_context, set
        once by SystemOrchestrator.boot_tournament before this plugin is
        even constructed. Returns False (and leaves the plugin in
        WAITING) if either piece is missing — e.g. offline mode, where
        boot_offline's shared_context sets "tournament": None and
        "court_id": None on purpose.
        """
        tournament = self.core_context.get("tournament")
        court_id = self.core_context.get("court_id")
        if tournament is None or not court_id:
            return False
        self.tournament_id = tournament.id
        self.court_id = str(court_id)
        self.sport_id = tournament.sport
        self.discipline_id = tournament.discipline
        return True

    # ── Lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        self.set_status(PluginState.STARTING)
        if not self._bind_context():
            self.set_status(PluginState.WAITING, "No tournament/court bound yet (offline mode, or booting).")
            return
        await self._reload_matches()
        await self._reconcile_judge_slots()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.set_status(PluginState.ACTIVE)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        self.set_status(PluginState.STOPPED)

    async def on_settings_changed(self, new_settings: PoomsaeControlSettings) -> None:
        self.logger.info(f"[{self.name}] settings changed — reloading match schedule.")
        await self.stop()
        await self.start()

    # ── Match schedule ────────────────────────────────────────────────
    async def _reload_matches(self) -> None:
        """
        (Re)loads this court's schedule without disturbing what's live.

        The live match and its stage come from match_data in RTDB (so a
        device restart picks up exactly where the floor is); the selection
        stays on whatever the operator had selected, or the live match on a
        fresh boot, or the first match. Nothing is pushed to RTDB except a
        refresh of the live match's descriptive fields (names, clubs,
        category, number) — never its stage, timer or judge scores.
        """
        selected_id = self.current_match.get("id") if self.current_match else None
        self.matches = await self._fetch_all_matches_for_court()
        self.active_index = 0

        try:
            live = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        except Exception as exc:
            self.logger.info(f"[{self.name}] couldn't read match_data on reload (non-fatal): {exc}")
            live = {}

        live_id = live.get("matchId")
        if self._index_of(live_id) is not None and live.get("stage") in STAGES:
            self.live_match_id = live_id
            self.stage = live["stage"]
        else:
            self.live_match_id = None
            self.stage = None

        index = self._index_of(selected_id or self.live_match_id)
        self.active_index = index if index is not None else 0
        if self.live_match_id:
            await self._refresh_live_identity()

    def _index_of(self, match_id: Optional[str]) -> Optional[int]:
        if not match_id:
            return None
        return next((i for i, m in enumerate(self.matches) if m.get("id") == match_id), None)

    @property
    def live_match(self) -> Optional[dict]:
        index = self._index_of(self.live_match_id)
        return self.matches[index] if index is not None else None

    @property
    def selected_is_live(self) -> bool:
        return bool(self.live_match_id) and self.current_match is not None and self.current_match.get("id") == self.live_match_id

    async def _refresh_live_identity(self) -> None:
        """Rewrites only the descriptive fields of match_data for the live
        match — never stage, timer, penalties or judge scores."""
        m = self.live_match
        if m is None:
            return
        updates = {
            "categoryLabel": match_category_label(m),
            "roundPhase": m.get("roundPhase"),
            "roundPhaseLabel": round_phase_label(m.get("roundPhase")),
            "displayNumber": m.get("displayNumber"),
            "displayNumberLabel": format_match_number(m.get("displayNumber")),
            "isSolo": m.get("red") is None,
        }
        for color in ("blue", "red"):
            corner = m.get(color)
            if corner is None:
                updates[f"corners/{color}"] = None
                continue
            updates[f"corners/{color}/entryId"] = corner.get("entryId")
            updates[f"corners/{color}/name"] = self._corner_name(corner)
            updates[f"corners/{color}/clubName"] = corner.get("clubName") or corner.get("club")
        await self.db.update_live_state(self.tournament_id, self.court_id, updates)

    # ── Category navigation ─────────────────────────────────────────────
    # The bracket builder schedules a whole category as one block on one
    # court, so a category is a contiguous run of self.matches.
    def _category_bounds(self, index: int) -> tuple[int, int]:
        code = self.matches[index].get("categoryCode")
        start = end = index
        while start > 0 and self.matches[start - 1].get("categoryCode") == code:
            start -= 1
        while end + 1 < len(self.matches) and self.matches[end + 1].get("categoryCode") == code:
            end += 1
        return start, end

    def _corner_name(self, corner: Optional[dict]) -> Optional[str]:
        """
        What to show for a corner. Real entries have a name. Later-round
        slots don't yet: a single-elimination slot points at its feeder
        match (commit_brackets_endpoint rewrites source.matchId to the real
        doc id, which is on this same court), and a cut-off Semi-Final /
        Final slot is flagged pendingAdvancement until someone advances.
        """
        if not corner:
            return None
        if corner.get("name"):
            return corner["name"]
        source = corner.get("source") or {}
        if source.get("matchId"):
            feeder = next((m for m in self.matches if m.get("id") == source["matchId"]), None)
            if feeder is not None and feeder.get("displayNumber") is not None:
                return f"Winner of №{format_match_number(feeder.get('displayNumber'))}"
            return "Winner of previous match"
        if corner.get("pendingAdvancement"):
            return "To be decided"
        return None

    async def _fetch_all_matches_for_court(self) -> list[dict]:
        """
        DatabasePort's fetch_next_matches_for_court only returns matches
        with status "ready" — this plugin needs the FULL schedule on this
        court (already-fought, current, and upcoming) so Previous/Next
        Match can page through the whole day, not just what's currently
        playable. Queries Firestore directly through
        FirebaseAdapter.firestore_db rather than adding a new
        DatabasePort method I can't see the abstract contract for — this
        really belongs on the port properly once that's confirmed safe.
        """
        from google.cloud.firestore_v1.base_query import FieldFilter

        def _fetch():
            query = (
                self.db.firestore_db.collection("sports").document(self.sport_id)
                .collection("disciplines").document(self.discipline_id)
                .collection("matches")
                .where(filter=FieldFilter("tournamentId", "==", self.tournament_id))
                .where(filter=FieldFilter("courtId", "==", self.court_id))
                .order_by("queuePosition")
            )
            results = []
            for doc in query.stream():
                data = doc.to_dict()
                data["id"] = doc.id
                results.append(data)
            return results

        return await asyncio.to_thread(_fetch)

    @property
    def current_match(self) -> Optional[dict]:
        if not self.matches or not (0 <= self.active_index < len(self.matches)):
            return None
        return self.matches[self.active_index]

    # Every match sharing the CURRENT match's categoryCode, in their
    # existing order (already queuePosition-sorted, since self.matches
    # is — see _fetch_all_matches_for_court) — "positioned after each
    # other" is already true of the underlying data; this just filters
    # the court's full mixed-category schedule down to one category's
    # own sequence for the admin queue panel / external screen to show,
    # rather than requiring a second Firestore query. Summary fields
    # only (not the full match doc) — this is a lightweight list for a
    # sidebar/queue, not the source of truth for scoring.
    def _category_matches(self) -> list[dict]:
        current = self.current_match
        if not current:
            return []
        code = current.get("categoryCode")
        result = []
        for m in self.matches:
            if m.get("categoryCode") != code:
                continue
            blue = m.get("blue") or {}
            red = m.get("red") or {}
            result.append({
                "id": m.get("id"),
                "displayNumber": m.get("displayNumber"),
                "displayNumberLabel": format_match_number(m.get("displayNumber")),
                "roundPhase": m.get("roundPhase"),
                "roundPhaseLabel": round_phase_label(m.get("roundPhase")),
                "blueName": self._corner_name(blue),
                "redName": self._corner_name(red) if m.get("red") is not None else None,
                "isActive": m.get("id") == current.get("id"),
                "isLive": m.get("id") == self.live_match_id,
            })
        return result

    def _blank_corner(self, corner: Optional[dict]) -> Optional[dict]:
        """
        A corner as pushed to match_data on match selection — identity
        fields only (name/clubName/entryId, denormalized onto the match doc
        by commit_brackets_endpoint; a not-yet-decided slot gets a
        placeholder name, see _corner_name). `judges` starts empty;
        the future scoring app is expected to write into it per judge
        (see the module comment on STAGES for how completeness is
        detected). None in, None out — a solo cut-off match's red corner
        stays genuinely absent, same convention used throughout the
        bracket builder / commit endpoint.
        """
        if corner is None:
            return None
        return {
            "entryId": corner.get("entryId"),
            "name": self._corner_name(corner),
            "clubName": corner.get("clubName") or corner.get("club"),
            "judges": {},
            "deduction": 0,
            "finalScore": None,
            # Sum of every Penalty tap this round (see _add_penalty) —
            # reset on every stage change, like roundDeduction.
            "roundPenalty": None,
            # Reset on every fresh match selection — see _reset_round,
            # which is the only thing that ever sets this to something
            # nonzero. A new match should never inherit a deduction from
            # whatever the PREVIOUS match's last round happened to have.
            "roundDeduction": None,
        }

    # ── Judge slots (settings-driven count, not one-at-a-time anymore) ──
    # judge_count in settings is now the SOURCE OF TRUTH for how many
    # judge slots exist on this court — this replaces the old "+ Add
    # Judge" button, which created exactly one slot per click. Called on
    # every start() (so a fresh boot always matches the configured
    # count) and from the /settings endpoint (so changing the count
    # through the panel's own Settings modal takes effect immediately,
    # without needing a plugin restart).
    #
    # Slots are always exactly {1, 2, ..., judge_count} — no gap-filling
    # logic anymore (that mattered when judges were added/removed
    # individually; now the whole set is just recomputed from the
    # target count each time). Missing numbers get created; any number
    # ABOVE judge_count gets deleted — e.g. dropping from 5 judges to 3
    # removes slots 4 and 5.
    async def _reconcile_judge_slots(self) -> None:
        if not self.tournament_id or not self.court_id:
            return
        async with self._judges_lock:
            target_count = self._settings().judge_count
            registry = _normalize_firebase_map(await self.db.fetch_judges_registry(self.tournament_id, self.court_id))
            existing_numbers = set()
            for k in registry.keys():
                try:
                    existing_numbers.add(int(k))
                except (TypeError, ValueError):
                    continue  # leftover non-numeric key from an earlier design — ignore, see buildJudgesTable's own comment on the panel side

            for number in range(1, target_count + 1):
                if number not in existing_numbers:
                    await self.db.create_judge_slot(self.tournament_id, self.court_id, number)
            for number in existing_numbers:
                if number > target_count:
                    await self.db.delete_judge_slot(self.tournament_id, self.court_id, number)

    # ── TIMER MODEL ───────────────────────────────────────────────────
    # No stage auto-starts a countdown anymore. Preparation has no timer
    # at all (None everywhere below). A poomsae round, once entered, sits
    # PAUSED at its full duration until the operator taps the timer —
    # see the /timer/start and /timer/pause endpoints. This function is
    # the one place that decides what a stage's FULL duration is; the
    # actual running/paused/remaining state lives only in match_data,
    # toggled by those two endpoints, never auto-managed here.
    def _duration_for_stage(self, stage: str) -> Optional[int]:
        if stage in ("poomsae_1", "poomsae_2"):
            return self._settings().poomsae_seconds
        return None  # preparation, final_results — no timer

    async def _activate_selected_match(self) -> None:
        """
        The operator clicked Preparation on a match that isn't live yet:
        it becomes the LIVE match. Does a FULL replace of match_data via
        push_live_state — a new match on the floor is a clean slate, so
        whatever the previous match left there (judge scores included) is
        wiped, unlike _advance_stage below. This is the ONLY thing that
        puts a match on the external screen; selecting one doesn't.
        """
        m = self.current_match
        if m is None:
            raise HTTPException(status_code=400, detail="No match selected.")
        self.stage = "preparation"
        self.live_match_id = m.get("id")
        payload = {
            "matchId": m.get("id"),
            "categoryLabel": match_category_label(m),
            "roundPhase": m.get("roundPhase"),
            "roundPhaseLabel": round_phase_label(m.get("roundPhase")),
            "displayNumber": m.get("displayNumber"),
            "displayNumberLabel": format_match_number(m.get("displayNumber")),
            "isSolo": m.get("red") is None,
            "activatedAt": datetime.now(timezone.utc).isoformat(),
            "stage": self.stage,
            "stageLabel": STAGE_LABELS[self.stage],
            # Preparation has no timer — see _duration_for_stage. No
            # stageStartedAt either: that concept is gone entirely, only
            # the paused/running fields below matter now.
            "stageDurationSeconds": None,
            "timerRunning": False,
            "timerRemainingSeconds": None,
            "allJudgesSubmitted": False,
            "corners": {
                "blue": self._blank_corner(m.get("blue")),
                "red": self._blank_corner(m.get("red")),
            },
        }
        await self.db.push_live_state(self.tournament_id, self.court_id, payload)

    async def _advance_stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise HTTPException(status_code=400, detail=f"Unknown stage: {stage}")
        self.stage = stage
        duration = self._duration_for_stage(stage)
        # update_live_state (RTDB .update()), NOT push_live_state — see
        # that method's own docstring. A stage change must never wipe
        # out judge scores a scoring app may have already written under
        # corners/*/judges since this match was selected.
        #
        # timerRunning is ALWAYS False here, even for a poomsae round —
        # the countdown never auto-starts on stage entry anymore, only
        # on an explicit tap of the timer (see /timer/start). Jumping
        # INTO a round the operator already timed earlier (e.g. clicking
        # back to Poomsae 1's pill) resets the remaining time to the
        # full duration again, same as a fresh entry — this plugin
        # doesn't try to remember a partially-elapsed countdown across a
        # stage jump.
        await self.db.update_live_state(self.tournament_id, self.court_id, {
            "stage": stage,
            "stageLabel": STAGE_LABELS[stage],
            "stageDurationSeconds": duration,
            "timerRunning": False,
            "timerRemainingSeconds": duration,
            # Fresh per-round — see _poll_loop, which sets this True the
            # moment it detects every required judge has submitted.
            "allJudgesSubmitted": False,
            # A restart deduction (see _reset_round) is specific to the
            # round it was applied in — moving to a different stage
            # (even back to the SAME round number, e.g. re-entering
            # Poomsae 1 after jumping away) starts that round fresh
            # again, with no lingering penalty from before.
            "corners/blue/roundDeduction": None,
            "corners/red/roundDeduction": None,
            "corners/blue/roundPenalty": None,
            "corners/red/roundPenalty": None,
        })

    async def _poll_loop(self) -> None:
        """
        The "automatic once all judges have provided data" half of the
        lifecycle — no button anywhere in this plugin triggers Final
        Results. Every few seconds, reads match_data back and checks
        whether every relevant corner (blue always; red too, unless this
        is a solo match) has at least judge_count judges who have
        explicitly SUBMITTED (not just written partial data) for THIS
        SPECIFIC round — see STAGE_TO_ROUND_KEY.

        Checking `submitted: true` specifically (not just "does this
        round key exist at all") matters: the judge app's own scoring
        screens push live updates continuously while a judge is still
        adjusting values (see judge_app's SubmitJudgeScore usage), so a
        round key existing is not the same thing as that judge being
        DONE — only their explicit Submit action sets `submitted`.

        Once every required corner has enough submitted judges, this
        writes an explicit `allJudgesSubmitted: true` flag onto
        match_data (see _advance_stage, which resets it back to false on
        every stage entry) — a clean, self-documenting event a future
        external-screen consumer can watch for, rather than having to
        infer "results are ready" indirectly from `stage ==
        "final_results"`. The stage IS still advanced to final_results
        too, same as before; this flag is additive, not a replacement.

        A plain poll, not a persistent RTDB listener — noticing
        completeness within a few seconds is plenty for this, and it
        avoids a second long-lived connection stacked on top of the
        judge app's own writes.
        """
        while True:
            try:
                await asyncio.sleep(3)
                # Read every tick, not once — the panel's Settings modal can
                # change judge_count at any time (see /settings), and a stale
                # count would advance to Final Results too early or never.
                settings = self._settings()
                if self.stage not in STAGE_TO_ROUND_KEY:
                    continue
                round_key = STAGE_TO_ROUND_KEY[self.stage]
                state = await self.db.fetch_live_state(self.tournament_id, self.court_id)
                if not state:
                    continue
                corners = state.get("corners") or {}
                required = [c for c in (corners.get("blue"), corners.get("red")) if c]
                if not required:
                    continue
                all_judged = all(
                    sum(
                        1 for j in _normalize_firebase_map(c.get("judges")).values()
                        if isinstance(j, dict) and isinstance(j.get(round_key), dict)
                        and j[round_key].get("submitted") is True
                    ) >= settings.judge_count
                    for c in required
                )
                if all_judged:
                    await self.db.update_live_state(self.tournament_id, self.court_id, {
                        "allJudgesSubmitted": True,
                    })
                    next_stage = "poomsae_2" if self.stage == "poomsae_1" else "final_results"
                    await self._advance_stage(next_stage)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Non-fatal on purpose — a transient RTDB hiccup shouldn't
                # kill the poll loop for the rest of the match.
                self.logger.info(f"[{self.name}] poll loop error (non-fatal): {exc}")

    # ── Restart / round reset ───────────────────────────────────────────
    # "Reset" in real WT poomsae terms: a performer has to restart their
    # pattern. This clears every judge's score for the CURRENTLY ACTIVE
    # round only (leaves the OTHER round's data alone, and never touches
    # the timer — see _duration_for_stage / the /timer endpoints, which
    # this does not call at all), and records the standard automatic
    # restart penalty (settings.restart_deduction, -0.6 by default) onto
    # each active corner so the panel's Final Score row can show and
    # subtract it — see buildJudgesTable on the panel side.
    #
    # Uses RTDB's own multi-location update capability: passing a dict
    # of SLASH-SEPARATED PATHS (relative to match_data) to
    # update_live_state, with None as a path's value meaning "delete
    # whatever is there" — this is what lets one single write clear
    # every judge's {roundKey} entry across both corners atomically,
    # without needing a separate call per judge.
    async def _reset_round(self) -> None:
        live = self.live_match
        if live is not None and live.get("red") is not None:
            raise HTTPException(status_code=400, detail="Restart only applies to a solo performance, not a side-by-side match.")
        round_key = STAGE_TO_ROUND_KEY.get(self.stage)
        if not round_key:
            return  # Preparation / Final Results — nothing to reset

        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        corners = state.get("corners") or {}
        deduction = self._settings().restart_deduction

        updates: dict = {}
        for color in ("blue", "red"):
            corner = corners.get(color)
            if not corner:
                continue
            judges = _normalize_firebase_map(corner.get("judges"))
            for judge_key, judge_data in judges.items():
                if isinstance(judge_data, dict) and round_key in judge_data:
                    updates[f"corners/{color}/judges/{judge_key}/{round_key}"] = None
            updates[f"corners/{color}/roundDeduction"] = deduction

        if updates:
            await self.db.update_live_state(self.tournament_id, self.court_id, updates)

    # ── Penalty ─────────────────────────────────────────────────────────
    # Adds settings.penalty_deduction (-0.3 by default) to one corner's
    # penalty total for the CURRENT round. Can be given repeatedly; in a
    # side-by-side match the panel sends which corner. Stored separately
    # from roundDeduction (restart) so the two never overwrite each other.
    async def _add_penalty(self, corner: str) -> float:
        if STAGE_TO_ROUND_KEY.get(self.stage) is None:
            raise HTTPException(status_code=400, detail="Penalties can only be given during Poomsae 1 or Poomsae 2.")
        live = self.live_match
        if corner == "red" and (live is None or live.get("red") is None):
            raise HTTPException(status_code=400, detail="This is a solo performance — there is no red athlete.")
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        current = ((state.get("corners") or {}).get(corner) or {}).get("roundPenalty") or 0
        total = round(current + self._settings().penalty_deduction, 3)
        await self.db.update_live_state(self.tournament_id, self.court_id, {f"corners/{corner}/roundPenalty": total})
        return total

    # ── HTTP API + static panel ─────────────────────────────────────────
    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/poomsae-control", tags=["Poomsae Control"])

        @router.get("/panel")
        async def panel():
            return FileResponse(str(PANEL_HTML_PATH))

        @router.get("/screen")
        async def screen():
            return FileResponse(str(SCREEN_HTML_PATH))

        @router.get("/state")
        async def get_state():
            live = None
            judges_registry = None
            if self.tournament_id and self.court_id:
                live = await self.db.fetch_live_state(self.tournament_id, self.court_id)
                judges_registry = _normalize_firebase_map(await self.db.fetch_judges_registry(self.tournament_id, self.court_id))
            selected = self.current_match
            live_match = self.live_match
            bounds = self._category_bounds(self.active_index) if selected else (0, 0)
            return {
                "index": self.active_index,
                "total": len(self.matches),
                # The LIVE match's stage (None when nothing is live) — see
                # the SELECTED vs LIVE note in __init__.
                "stage": self.stage,
                "stageLabel": STAGE_LABELS.get(self.stage, ""),
                # The SELECTED match (what the operator is looking at).
                "match": selected,
                "matchLabels": {
                    "category": match_category_label(selected),
                    "roundPhase": round_phase_label(selected.get("roundPhase")),
                    "displayNumber": format_match_number(selected.get("displayNumber")),
                } if selected else None,
                # Names/clubs for the selected match's corners, for showing
                # it before it's live (live data only exists once activated).
                "selectedCorners": {
                    "blue": self._blank_corner(selected.get("blue")),
                    "red": self._blank_corner(selected.get("red")),
                } if selected else None,
                "isLive": self.selected_is_live,
                "liveMatch": {
                    "id": live_match.get("id"),
                    "index": self._index_of(live_match.get("id")),
                    "category": match_category_label(live_match),
                    "displayNumber": format_match_number(live_match.get("displayNumber")),
                    "isSolo": live_match.get("red") is None,
                } if live_match else None,
                "categoryNav": {"hasPrevious": bounds[0] > 0, "hasNext": bounds[1] + 1 < len(self.matches)},
                # match_data in RTDB — what the external screen shows.
                "live": live,
                "categoryMatches": self._category_matches(),
                "judgesRegistry": judges_registry,
                "settings": self._settings().model_dump(),
                # For the "Connect Judge" QR payload.
                "tournamentId": self.tournament_id,
                "courtId": self.court_id,
            }

        # ── Selection (browsing only — never touches RTDB) ──────────────
        @router.post("/next-match")
        async def next_match():
            if self.active_index + 1 >= len(self.matches):
                raise HTTPException(status_code=400, detail="Already at the last match on this court.")
            self.active_index += 1
            return {"index": self.active_index}

        @router.post("/previous-match")
        async def previous_match():
            if self.active_index <= 0:
                raise HTTPException(status_code=400, detail="Already at the first match on this court.")
            self.active_index -= 1
            return {"index": self.active_index}

        @router.post("/next-category")
        async def next_category():
            # First match of the category after the selected one's.
            if not self.matches:
                raise HTTPException(status_code=400, detail="No matches on this court.")
            _, end = self._category_bounds(self.active_index)
            if end + 1 >= len(self.matches):
                raise HTTPException(status_code=400, detail="Already in the last category on this court.")
            self.active_index = end + 1
            return {"index": self.active_index}

        @router.post("/previous-category")
        async def previous_category():
            # First match of the category before the selected one's.
            if not self.matches:
                raise HTTPException(status_code=400, detail="No matches on this court.")
            start, _ = self._category_bounds(self.active_index)
            if start == 0:
                raise HTTPException(status_code=400, detail="Already in the first category on this court.")
            self.active_index, _ = self._category_bounds(start - 1)
            return {"index": self.active_index}

        @router.post("/jump-to-match/{match_id}")
        async def jump_to_match(match_id: str):
            index = self._index_of(match_id)
            if index is None:
                raise HTTPException(status_code=404, detail="Match not found in this court's schedule.")
            self.active_index = index
            return {"index": self.active_index}

        # ── Live match ──────────────────────────────────────────────────
        @router.post("/stage/{stage}")
        async def set_stage(stage: str):
            # Final Results is set only by _poll_loop, once judges are done.
            if stage == "final_results":
                raise HTTPException(status_code=400, detail="final_results is set automatically once all judges submit — it can't be triggered manually.")
            if stage not in STAGES:
                raise HTTPException(status_code=400, detail=f"Unknown stage: {stage}")
            if not self.selected_is_live:
                # Preparation on a match that isn't live = put it on the
                # floor (and the external screen). Rounds need that first.
                if stage != "preparation":
                    raise HTTPException(status_code=400, detail="Click Preparation first to put this match on the floor.")
                await self._activate_selected_match()
            else:
                await self._advance_stage(stage)
            return {"stage": self.stage, "liveMatchId": self.live_match_id}

        def _require_live():
            if not self.live_match_id:
                raise HTTPException(status_code=400, detail="No match is live — click Preparation on a match first.")

        @router.post("/timer/start")
        async def start_timer():
            _require_live()
            await self.db.update_live_state(self.tournament_id, self.court_id, {
                "timerRunning": True,
                "timerStartedAt": datetime.now(timezone.utc).isoformat(),
            })
            return {"ok": True}

        @router.post("/timer/pause")
        async def pause_timer(payload: TimerPausePayload):
            _require_live()
            await self.db.update_live_state(self.tournament_id, self.court_id, {
                "timerRunning": False,
                "timerRemainingSeconds": payload.remainingSeconds,
            })
            return {"ok": True}

        @router.post("/round/reset")
        async def reset_round():
            _require_live()
            await self._reset_round()
            return {"ok": True}

        @router.post("/penalty")
        async def penalty(payload: PenaltyPayload):
            _require_live()
            total = await self._add_penalty(payload.corner)
            return {"ok": True, "corner": payload.corner, "roundPenalty": total}

        @router.post("/penalty/clear")
        async def clear_penalty(payload: PenaltyPayload):
            # Undo for a penalty given by mistake — clears that corner's
            # penalty total for the current round.
            _require_live()
            await self.db.update_live_state(self.tournament_id, self.court_id, {f"corners/{payload.corner}/roundPenalty": None})
            return {"ok": True}

        @router.post("/settings")
        async def update_settings(payload: PoomsaeControlSettings):
            # In-memory layer — see _settings_override's comment in __init__.
            self._settings_override = payload.model_dump()
            await self._reconcile_judge_slots()
            return {"ok": True, "settings": self._settings().model_dump()}

        @router.post("/reload")
        async def reload():
            # Re-reads the schedule (e.g. after a re-push from the bracket
            # builder); keeps the selection and whatever is live.
            await self._reload_matches()
            return {"total": len(self.matches)}

        return router