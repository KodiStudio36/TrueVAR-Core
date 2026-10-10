import asyncio
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
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


# Pure helpers next to this file (no I/O), loaded by path so they work
# however the plugin loader imports this file.
def _load_local_module(module_name: str, file_name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(file_name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


poomsae_results = _load_local_module("poomsae_control_results", "results.py")
poomsae_standings = _load_local_module("poomsae_control_standings", "standings.py")
poomsae_live = _load_local_module("poomsae_control_live", "live.py")

# ── Livestream (event bus) ──────────────────────────────────────────────
# A light read-only loop (_live_loop) watches match_data and publishes on
# the core event bus — the Poomsae Livestream Extension subscribes, exactly
# like the kyorugi extension subscribes to scoreboard.*:
#   LIVE_UPDATE_EVENT   full scoreboard payload (live.shape_live_payload),
#                       on every change + a heartbeat so a reloaded
#                       browser source catches up
#   STAGE_CHANGED_EVENT {match_id, stage, previous_stage} whenever the live
#                       match or its stage changes (for scene automation)
LIVE_UPDATE_EVENT = "poomsae.live_update"
STAGE_CHANGED_EVENT = "poomsae.stage_changed"
LIVE_RESYNC_EVENT = "poomsae.live_resync"   # in: publish LIVE_UPDATE_EVENT now
LIVE_POLL_SECONDS = 0.4
LIVE_HEARTBEAT_SECONDS = 3.0

# ── Saving results to Firestore ─────────────────────────────────────────
# Final Results publishes RESULT_EVENT on the core event bus with the full
# computed result (see _publish_result). The Firebase Sync plugin
# subscribes to it and merges the result into the match doc —
#   sports/{sport}/disciplines/{discipline}/matches/{matchId}
# — the same way it archives a kyorugi match on scoreboard.WIN, then
# publishes ARCHIVED_EVENT back so the panel can show "Saved ✓".
RESULT_EVENT = "poomsae.FINAL_RESULTS"
ARCHIVED_EVENT = "firebase_sync.match_archived"

# ── Persisted panel settings ────────────────────────────────────────────
# The settings the operator changes in the panel (Settings modal and the
# Auto toggles on the stage pills) are saved per court in RTDB, next to
# match_data:
#   /tournaments/{tournament_id}/courts/{court_id}/poomsae_settings
# and read back on every start BEFORE anything that depends on them runs
# (judge slot reconciliation, the "all judges submitted" check). If that
# read fails, nothing is written anywhere until a later read succeeds —
# see _load_settings / _retry_settings_load.
#
# video_sync_url / video_sync_secret are deliberately NOT persisted here:
# the secret must never be written to RTDB. They come only from the
# framework's own settings store.
PERSISTED_SETTINGS = (
    "poomsae_seconds", "judge_count", "scoring_method", "restart_deduction", "penalty_deduction",
    "auto_results_1", "auto_results_2", "auto_final_results", "auto_category_results",
    "final_results_delay", "category_results_delay",
)
SETTINGS_RETRY_SECONDS = 10

# ── Category Results records ────────────────────────────────────────────
# Every match's computed scores are kept per category in RTDB on Final
# Results, so the category ranking survives plugin restarts:
#   /tournaments/{tid}/courts/{cid}/poomsae_category_results/{category}/{matchId}
# (see _record_category_result / standings.py).
CATEGORY_RESULTS_NODE = "poomsae_category_results"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rtdb_key(value) -> str:
    """RTDB keys may not contain . # $ [ ] /"""
    return re.sub(r"[.#$\[\]/]", "_", str(value or "")) or "uncategorized"


# ── Match lifecycle ─────────────────────────────────────────────────────
# Preparation → Poomsae 1 → Results 1 → Poomsae 2 → Results 2 →
# Final Results → Category Results.
#
# The operator starts Preparation and each Poomsae by hand. The four
# result stages each have an Auto toggle (settings auto_*):
#   Results 1 / 2     — auto: as soon as every judge submitted that poomsae
#   Final Results     — auto: final_results_delay s after the last Results
#   Category Results  — auto: category_results_delay s after Final Results
# With a toggle off, that stage waits for the operator (its pill is
# highlighted as the next step). With one poomsae in the round (cut-off
# preliminary / semi-final) Poomsae 2 and Results 2 are skipped.
STAGES = ["preparation", "poomsae_1", "results_1", "poomsae_2", "results_2", "final_results", "category_results"]
STAGE_LABELS = {
    "preparation": "Preparation",
    "poomsae_1": "Poomsae 1",
    "results_1": "Results 1",
    "poomsae_2": "Poomsae 2",
    "results_2": "Results 2",
    "final_results": "Final Results",
    "category_results": "Category Results",
}
# Maps the SCOREABLE stages onto the RTDB child key the judge app writes
# scores under — MUST stay in lockstep with PoomsaeRound.rtdbKey in
# judge_app/lib/features/scoring/domain/entities/poomsae_round.dart. Only
# these two stages are scoreable; _poll_loop uses membership in this dict
# to decide whether judges are being waited for.
STAGE_TO_ROUND_KEY = {"poomsae_1": "poomsae1", "poomsae_2": "poomsae2"}
# Results stages show (and accept penalties for) their poomsae.
STAGE_RESULTS_ROUND_KEY = {"results_1": "poomsae1", "results_2": "poomsae2"}
ROUND_TO_RESULTS_STAGE = {"poomsae_1": "results_1", "poomsae_2": "results_2"}
SECOND_POOMSAE_STAGES = ("poomsae_2", "results_2")
STAGE_POOMSAE_INDEX = {"poomsae_1": 0, "results_1": 0, "poomsae_2": 1, "results_2": 1}
AUTO_STAGES = ("results_1", "results_2", "final_results", "category_results")


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


# ── Scoring mode ────────────────────────────────────────────────────────
# The judge app shows a different screen for each:
#   "solo" — one athlete (cut-off): all four deduction buttons blue
#   "duel" — two athletes side by side: left buttons blue, right red
#   "para" — para poomsae: technical wheels instead of deduction buttons
# Para is recognised from the category code, the same way the website
# composes it (category-schema.js / _para_rules in api_router.py): a para
# entry type's code starts with "para_", and a class split adds a
# segment like "_p72_".
_PARA_CLASS_SEGMENT = re.compile(r"(^|_)p(11|12|21|22|23|31|32|33|34|35|41|42|43|44|45|51|52|53|60|72)(_|$)")


def is_para_category(code: Optional[str]) -> bool:
    code = str(code or "").lower()
    return code.startswith("para_") or bool(_PARA_CLASS_SEGMENT.search(code))


def scoring_mode(match: Optional[dict]) -> str:
    if not match:
        return "solo"
    if is_para_category(match.get("categoryCode")):
        return "para"
    return "solo" if match.get("red") is None else "duel"


def competition_system(match: Optional[dict]) -> str:
    """Which Category Results view a match's category gets: cut-off
    (solo performances), round robin (RR phases) or single elimination."""
    if not match or match.get("red") is None:
        return "cut_off"
    if str(match.get("roundPhase") or "").startswith("RR"):
        return "round_robin"
    return "single_elimination"


# ── Drawn poomsae ───────────────────────────────────────────────────────
# The bracket builder writes the poomsae drawn for a match's category and
# round onto the match doc: `poomsae: [{key, name}, …]` — one for the
# cut-off preliminary round and semi-final, two for the final. Matches
# pushed before the draw existed have none; see poomsae_count for the
# fallback.
# Cut-off (solo) rounds that perform a single poomsae. The cut-off final
# and every single-elimination / round-robin round perform two.
CUT_OFF_SINGLE_POOMSAE_PHASES = {"PR", "SF"}


def match_poomsae(match: Optional[dict]) -> list[dict]:
    result = []
    for item in (match or {}).get("poomsae") or []:
        if isinstance(item, dict) and (item.get("name") or item.get("key")):
            result.append({"key": item.get("key"), "name": item.get("name") or category_label_from_code(item.get("key"))})
        elif isinstance(item, str) and item:
            result.append({"key": item, "name": category_label_from_code(item)})
    return result


def poomsae_count(match: Optional[dict]) -> int:
    """How many performances (Poomsae 1 / Poomsae 2) this match has:
    the number drawn; with nothing drawn, 1 for a cut-off preliminary /
    semi-final and 2 otherwise."""
    drawn = match_poomsae(match)
    if drawn:
        return max(1, min(2, len(drawn)))
    if match and match.get("red") is None and match.get("roundPhase") in CUT_OFF_SINGLE_POOMSAE_PHASES:
        return 1
    return 2


def round_keys_for(match: Optional[dict]) -> list[str]:
    return ["poomsae1", "poomsae2"][:poomsae_count(match)]


def last_results_stage(match: Optional[dict]) -> str:
    return "results_2" if poomsae_count(match) >= 2 else "results_1"


def results_round_key_for(stage: Optional[str], match: Optional[dict]) -> Optional[str]:
    """Which poomsae's scores the panel / screen show in a stage."""
    if stage in STAGE_TO_ROUND_KEY:
        return STAGE_TO_ROUND_KEY[stage]
    if stage in STAGE_RESULTS_ROUND_KEY:
        return STAGE_RESULTS_ROUND_KEY[stage]
    if stage in ("final_results", "category_results"):
        return round_keys_for(match)[-1]
    return None


def poomsae_name_for_stage(match: Optional[dict], stage: Optional[str]) -> Optional[str]:
    index = STAGE_POOMSAE_INDEX.get(stage or "")
    if index is None:
        return None
    drawn = match_poomsae(match)
    return drawn[index]["name"] if index < len(drawn) else None


def stage_label(match: Optional[dict], stage: Optional[str]) -> str:
    """"Poomsae 1 · Koryo" / "Results 1 · Koryo" when a poomsae was drawn."""
    base = STAGE_LABELS.get(stage or "", "")
    name = poomsae_name_for_stage(match, stage)
    return f"{base} · {name}" if name else base


def new_round_token() -> str:
    """A fresh value written to match_data.roundToken whenever judges must
    start a round from scratch: a match goes live, a poomsae stage is
    (re-)entered, or Restart clears the round. Results / Final / Category
    stages keep the token, so a judge's "submitted" screen stays put. The
    judge app resets its screens whenever this changes."""
    return secrets.token_hex(6)


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


def corner_country(corner: Optional[dict]):
    """The athlete's country as stored on the match doc's corner — shown
    as flag + code on the external screen. Accepts the usual field names."""
    if not corner:
        return None
    for key in ("country", "countryCode", "nationality", "nation"):
        if corner.get(key):
            return corner[key]
    return None


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


# ── Timer ───────────────────────────────────────────────────────────────
# Only FACTS go to match_data — never a per-second tick:
#   start: timerRunning=True, timerRemainingSeconds (value AT this start),
#          timerStartedAt (ISO, this machine) and timerStartedAtMs
#          (Firebase SERVER timestamp — what the judge tablets count from)
#   pause: timerRunning=False, timerRemainingSeconds (frozen value)
# Everyone computes "now" as remaining − (now − startedAt).
def _remaining_now(state: dict, allow_negative: bool = False) -> Optional[float]:
    """Seconds left right now, from the last start/pause facts only.
    allow_negative=True keeps counting past zero (overtime) — the
    livestream clock shows -0:05 etc.; everything else stops at 0."""
    base = state.get("timerRemainingSeconds")
    if base is None:
        base = state.get("stageDurationSeconds")
    if base is None:
        return None
    if not state.get("timerRunning"):
        return float(base)
    try:
        started = datetime.fromisoformat(state["timerStartedAt"])
    except Exception:
        return None
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    remaining = round(float(base) - elapsed, 3)
    return remaining if allow_negative else max(0.0, remaining)


class PoomsaeControlSettings(BaseModel):
    # Poomsae round duration ONLY — Preparation and the result stages have
    # no timer (see _duration_for_stage). The countdown resets TO this
    # whenever a poomsae stage is (re)entered, before the operator starts it.
    poomsae_seconds: int = Field(90, ge=5)
    # How many judge slots this court has — changing this number is what
    # creates/removes numbered slots (1..judge_count), see
    # _reconcile_judge_slots.
    judge_count: int = Field(3, ge=1, le=7)
    # How the judges' Totals combine: "sum_all" counts everyone,
    # "trim_high_low" drops the highest and lowest before averaging.
    scoring_method: str = Field("sum_all", pattern="^(sum_all|trim_high_low)$")
    # Automatic deduction on Restart (see _reset_round). -0.6 = WT standard.
    restart_deduction: float = Field(-0.6)
    # Penalty button (see /penalty) — added per tap. -0.3 = WT standard.
    penalty_deduction: float = Field(-0.3)
    # ── Automatic result stages (the Auto toggles on the stage pills) ──
    # True = the stage runs by itself; False = it waits for the operator.
    auto_results_1: bool = Field(True)
    auto_results_2: bool = Field(True)
    auto_final_results: bool = Field(True)
    auto_category_results: bool = Field(True)
    # Seconds from the last Results to Final Results, and from Final
    # Results to Category Results, when those run automatically.
    final_results_delay: int = Field(5, ge=0, le=300)
    category_results_delay: int = Field(8, ge=0, le=300)
    # YouTube position of every poomsae (see _fetch_video_position): the
    # server's POST /api/youtube/stream-position, authenticated with the
    # same X-Internal-Secret as the Cloud Functions calls. Empty secret =
    # video sync off. Framework settings only — never persisted to RTDB.
    video_sync_url: str = Field("http://localhost:8001/api/youtube/stream-position")
    video_sync_secret: str = Field("")


class PenaltyPayload(BaseModel):
    corner: str = Field("blue", pattern="^(blue|red)$")


class TimerPausePayload(BaseModel):
    # Optional — the plugin computes the paused value itself from
    # timerStartedAt (see _remaining_now). Only used as a fallback when
    # match_data has no usable start time.
    remainingSeconds: Optional[float] = None


class JudgeScorePayload(BaseModel):
    # Operator correction of one judge's score for the poomsae on screen.
    corner: str = Field(pattern="^(blue|red)$")
    judge: int = Field(ge=1, le=99)
    accuracy: float = Field(ge=0, le=4)
    presentation: float = Field(ge=0, le=6)


class AutoTogglePayload(BaseModel):
    stage: str = Field(pattern="^(results_1|results_2|final_results|category_results)$")
    enabled: bool


class PoomsaeControlPlugin(BasePlugin):
    """
    Operator control panel for poomsae matches. Deliberately NOT part of
    the main tournament website — this is its own plugin, with its own
    UI (served as a static file, see PANEL_HTML_PATH) and its own API,
    following the same BasePlugin contract as e.g.
    TkStrikeListenerGen3Plugin.

    Everything this plugin writes goes to Firebase REALTIME DATABASE under
    /tournaments/{tournament_id}/courts/{court_id}/ — match_data (see
    push_live_state / update_live_state on FirebaseAdapter),
    poomsae_settings (see _settings_ref) and poomsae_category_results (see
    _category_results_ref). Firestore is never written to by this plugin
    directly — only read, to pull the match schedule the bracket builder
    already pushed. Final results reach Firestore through the Firebase
    Sync plugin (see RESULT_EVENT).

    tournament_id / court_id / sport_id / discipline_id are NOT plugin
    settings — SystemOrchestrator.boot_tournament already builds
    shared_context with "tournament" and "court_id" scoped to this exact
    boot session before any plugin is even constructed. This plugin reads
    them straight off core_context in start() — see _bind_context.
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
        # Guards judge-slot reconciliation against overlapping calls.
        self._judges_lock = asyncio.Lock()
        # Every stage change (operator click OR automatic) runs under this
        # lock, and so does each poll tick — an automatic step can never
        # land in the middle of an operator's click, or vice versa.
        self._stage_lock = asyncio.Lock()
        # The pending automatic step: {"stage", "to", "due" (monotonic)}.
        # Mirrored to match_data.autoAdvance for the panel's countdown.
        self._auto: Optional[dict] = None
        # Panel settings loaded from / saved to RTDB (see _load_settings
        # and the /settings endpoint), layered over the framework's own
        # settings in _settings(). Only PERSISTED_SETTINGS keys.
        self._settings_override: dict = {}
        # False until this court's settings were read from RTDB
        # successfully. While False, nothing that depends on them writes
        # anything: no judge slots are created or deleted, and
        # allJudgesSubmitted is never set — a default judge count must
        # never be acted on. _retry_settings_load keeps trying.
        self._settings_loaded = False
        self._settings_retry_task: Optional[asyncio.Task] = None
        # matchId -> {"state": "saving" | "saved" | "failed", "error", "at"}
        # — what the panel shows next to Final Results. See _publish_result
        # and _on_archived.
        self.archive_status: dict[str, dict] = {}
        # categoryKey -> {matchId: record} written this session — used
        # when an RTDB read of poomsae_category_results fails.
        self._category_records: dict[str, dict] = {}
        # Silent per-poomsae timeline of the LIVE match (results.Timeline):
        # stage entries, timer, restart, penalties, every judge score change
        # seen in RTDB. Saved with the performance docs on Final Results.
        self.timeline = None
        self._video_tasks: list[asyncio.Task] = []
        self._bus_subscriptions: list[tuple] = []
        # Livestream publishing (see _live_loop).
        self._live_task: Optional[asyncio.Task] = None
        self._live_last_dump: Optional[str] = None
        self._live_last_stage: tuple = (None, None)
        self._live_heartbeat_due: float = 0.0

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
    # Few enough fields that the dashboard's auto-generated settings form
    # is fine — no need for a hand-built settings_ui here.
    settings_ui: Optional[str] = None

    # ── Settings ──────────────────────────────────────────────────────
    def _base_settings(self) -> PoomsaeControlSettings:
        return self.core_context["settings_manager"].get_settings(self.name)

    def _settings(self) -> PoomsaeControlSettings:
        base = self._base_settings()
        if not self._settings_override:
            return base
        return base.model_copy(update=self._settings_override)

    def _rtdb_ref(self, path: str):
        from firebase_admin import db as rtdb
        # Same Firebase app the adapter uses, if it exposes one; else the
        # default app.
        return rtdb.reference(
            f"tournaments/{self.tournament_id}/courts/{self.court_id}/{path}",
            app=getattr(self.db, "app", None),
        )

    def _settings_ref(self):
        return self._rtdb_ref("poomsae_settings")

    def _category_results_ref(self, category_key: str):
        return self._rtdb_ref(f"{CATEGORY_RESULTS_NODE}/{category_key}")

    async def _load_settings(self) -> bool:
        """
        Reads this court's saved panel settings from RTDB.

        True  = the settings are known (the saved ones, or none were saved
                yet) and _settings_override now holds them.
        False = the read failed or returned something invalid. Nothing is
                applied and nothing is written; the caller must not act on
                settings until a later call returns True.
        """
        try:
            saved = await asyncio.to_thread(lambda: self._settings_ref().get())
        except Exception as exc:
            self.logger.warning(f"[{self.name}] couldn't read settings from RTDB — keeping everything as is: {exc}")
            return False

        if saved is None:
            # Nothing saved yet (first run on this court): keep the judge
            # slots that already exist instead of shrinking them to the
            # default count. Nothing is written until the operator saves.
            try:
                registry = _normalize_firebase_map(await self.db.fetch_judges_registry(self.tournament_id, self.court_id))
            except Exception as exc:
                self.logger.warning(f"[{self.name}] couldn't read judge slots — keeping everything as is: {exc}")
                return False
            numbers = [int(k) for k in registry if str(k).isdigit() and int(k) >= 1]
            self._settings_override = {"judge_count": min(7, max(numbers))} if numbers else {}
            return True

        if not isinstance(saved, dict):
            self.logger.warning(f"[{self.name}] settings in RTDB aren't an object — ignoring them, changing nothing.")
            return False
        try:
            values = {k: saved[k] for k in PERSISTED_SETTINGS if k in saved}
            validated = PoomsaeControlSettings.model_validate({**self._base_settings().model_dump(), **values})
        except Exception as exc:
            self.logger.warning(f"[{self.name}] settings in RTDB are invalid — changing nothing: {exc}")
            return False
        self._settings_override = {k: getattr(validated, k) for k in PERSISTED_SETTINGS}
        return True

    async def _retry_settings_load(self) -> None:
        """Keeps trying to read settings after a failed read at start.
        Only once they're known are judge slots reconciled."""
        while not self._settings_loaded:
            await asyncio.sleep(SETTINGS_RETRY_SECONDS)
            if await self._load_settings():
                self._settings_loaded = True
                self.logger.info(f"[{self.name}] settings loaded from RTDB.")
                try:
                    await self._reconcile_judge_slots()
                except Exception as exc:
                    self.logger.warning(f"[{self.name}] judge slot sync failed: {exc}")

    async def _save_persisted_settings(self, values: dict) -> None:
        """Writes the panel settings to RTDB first — if that fails, nothing
        changes anywhere (HTTP 502)."""
        try:
            await asyncio.to_thread(lambda: self._settings_ref().set(values))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Settings not saved — couldn't write to Firebase: {exc}")
        self._settings_override = values
        self._settings_loaded = True
        if self._settings_retry_task:
            self._settings_retry_task.cancel()
            self._settings_retry_task = None

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
        WAITING) if either piece is missing — e.g. offline mode.
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

        # Settings FIRST — judge slots are only touched once the saved
        # judge count is actually known. A failed read changes nothing.
        self._settings_loaded = await self._load_settings()
        if self._settings_loaded:
            try:
                await self._reconcile_judge_slots()
            except Exception as exc:
                self.logger.warning(f"[{self.name}] judge slot sync failed: {exc}")
        else:
            self._settings_retry_task = asyncio.create_task(self._retry_settings_load())

        self._poll_task = asyncio.create_task(self._poll_loop())
        self._live_task = asyncio.create_task(self._live_loop())
        bus = self.core_context.get("event_bus")
        if bus and not self._bus_subscriptions:
            self._bus_subscriptions = [
                (ARCHIVED_EVENT, self._on_archived),
                # The livestream extension asks for a fresh payload when it starts.
                (LIVE_RESYNC_EVENT, self._on_live_resync),
            ]
            bus.subscribe_many(self._bus_subscriptions)
        self.set_status(PluginState.ACTIVE)

    async def stop(self) -> None:
        if self._settings_retry_task:
            self._settings_retry_task.cancel()
            self._settings_retry_task = None
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._live_task:
            self._live_task.cancel()
            self._live_task = None
        self._live_last_dump = None
        self._live_last_stage = (None, None)
        self._auto = None
        bus = self.core_context.get("event_bus")
        if bus and self._bus_subscriptions:
            bus.unsubscribe_many(self._bus_subscriptions)
            self._bus_subscriptions = []
        self.set_status(PluginState.STOPPED)

    async def on_settings_changed(self, new_settings: PoomsaeControlSettings) -> None:
        # Note: the PERSISTED_SETTINGS fields come from RTDB (the panel);
        # a change to them in the dashboard form is superseded on restart.
        # video_sync_* take effect from here.
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
        country, category, number, position in category) — never its
        stage, timer or judge scores.
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
        position, total = self._category_progress(m.get("id"))
        updates = {
            "categoryLabel": match_category_label(m),
            "roundPhase": m.get("roundPhase"),
            "roundPhaseLabel": round_phase_label(m.get("roundPhase")),
            "displayNumber": m.get("displayNumber"),
            "displayNumberLabel": format_match_number(m.get("displayNumber")),
            "isSolo": m.get("red") is None,
            "isPara": is_para_category(m.get("categoryCode")),
            "scoringMode": scoring_mode(m),
            "competitionSystem": competition_system(m),
            # "2/7" on the judge app — which performance of the category.
            "categoryPosition": position,
            "categoryTotal": total,
        }
        for color in ("blue", "red"):
            corner = m.get(color)
            if corner is None:
                updates[f"corners/{color}"] = None
                continue
            updates[f"corners/{color}/entryId"] = corner.get("entryId")
            updates[f"corners/{color}/name"] = self._corner_name(corner)
            updates[f"corners/{color}/clubName"] = corner.get("clubName") or corner.get("club")
            updates[f"corners/{color}/country"] = corner_country(corner)
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

    def _category_progress(self, match_id: Optional[str]) -> tuple[Optional[int], Optional[int]]:
        """(position, total) of a match inside its category block — e.g.
        (2, 7). Matches inserted by hand count too."""
        index = self._index_of(match_id)
        if index is None:
            return None, None
        start, end = self._category_bounds(index)
        return index - start + 1, end - start + 1

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

    def _identity(self, corner: Optional[dict]) -> Optional[dict]:
        if corner is None:
            return None
        return {
            "entryId": corner.get("entryId"),
            "name": self._corner_name(corner),
            "club": corner.get("clubName") or corner.get("club"),
            "country": corner_country(corner),
        }

    async def _fetch_all_matches_for_court(self) -> list[dict]:
        """
        DatabasePort's fetch_next_matches_for_court only returns matches
        with status "ready" — this plugin needs the FULL schedule on this
        court (already-fought, current, and upcoming) so Previous/Next
        Match can page through the whole day. Queries Firestore directly
        through FirebaseAdapter.firestore_db.
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
    # existing (queuePosition) order — the admin queue panel. Summary
    # fields only.
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
        A corner as pushed to match_data on match activation — identity
        fields only (name/clubName/country/entryId, denormalized onto the
        match doc by commit_brackets_endpoint; a not-yet-decided slot gets
        a placeholder name, see _corner_name). `judges` starts empty; the
        judge app writes into it per judge. None in, None out — a solo
        cut-off match's red corner stays genuinely absent.
        """
        if corner is None:
            return None
        return {
            "entryId": corner.get("entryId"),
            "name": self._corner_name(corner),
            "clubName": corner.get("clubName") or corner.get("club"),
            # Flag + code on the external screen.
            "country": corner_country(corner),
            "judges": {},
            "deduction": 0,
            "finalScore": None,
            # Sum of every Penalty tap this round (see _add_penalty).
            "roundPenalty": None,
            # Only _reset_round ever sets this to something nonzero.
            "roundDeduction": None,
        }

    # ── Judge slots (settings-driven count) ──────────────────────────
    # judge_count is the SOURCE OF TRUTH for how many judge slots exist on
    # this court. Slots are always exactly {1, 2, ..., judge_count}:
    # missing numbers get created; any number ABOVE judge_count gets
    # deleted — e.g. dropping from 5 judges to 3 removes slots 4 and 5.
    #
    # Runs ONLY once the court's settings are known (_settings_loaded) —
    # never from a default count after a failed RTDB read, which would
    # delete real judge slots.
    async def _reconcile_judge_slots(self) -> None:
        if not self.tournament_id or not self.court_id or not self._settings_loaded:
            return
        async with self._judges_lock:
            target_count = self._settings().judge_count
            registry = _normalize_firebase_map(await self.db.fetch_judges_registry(self.tournament_id, self.court_id))
            existing_numbers = set()
            for k in registry.keys():
                try:
                    existing_numbers.add(int(k))
                except (TypeError, ValueError):
                    continue  # leftover non-numeric key from an earlier design — ignore

            for number in range(1, target_count + 1):
                if number not in existing_numbers:
                    await self.db.create_judge_slot(self.tournament_id, self.court_id, number)
            for number in existing_numbers:
                if number > target_count:
                    await self.db.delete_judge_slot(self.tournament_id, self.court_id, number)

    # ── TIMER MODEL ───────────────────────────────────────────────────
    # No stage auto-starts a countdown. Only the poomsae stages have a
    # timer; once entered it sits PAUSED at its full duration until the
    # operator taps it — see /timer/start and /timer/pause. The ticking
    # is computed by every reader (see _remaining_now) — nothing is
    # written per second.
    def _duration_for_stage(self, stage: str) -> Optional[int]:
        if stage in STAGE_TO_ROUND_KEY:
            return self._settings().poomsae_seconds
        return None

    def _round_key_for_events(self) -> Optional[str]:
        """The poomsae that penalties / timeline events belong to right now:
        the one being performed, or the one whose results are shown."""
        return STAGE_TO_ROUND_KEY.get(self.stage) or STAGE_RESULTS_ROUND_KEY.get(self.stage)

    async def _activate_selected_match(self) -> None:
        """
        The operator clicked Preparation on a match that isn't live yet:
        it becomes the LIVE match. Does a FULL replace of match_data via
        push_live_state — a new match on the floor is a clean slate. This
        is the ONLY thing that puts a match on the external screen;
        selecting one doesn't.
        """
        m = self.current_match
        if m is None:
            raise HTTPException(status_code=400, detail="No match selected.")
        self._auto = None
        self.stage = "preparation"
        self.live_match_id = m.get("id")
        self.timeline = poomsae_results.Timeline(self.live_match_id)
        position, total = self._category_progress(m.get("id"))
        payload = {
            "matchId": m.get("id"),
            "categoryLabel": match_category_label(m),
            "roundPhase": m.get("roundPhase"),
            "roundPhaseLabel": round_phase_label(m.get("roundPhase")),
            "displayNumber": m.get("displayNumber"),
            "displayNumberLabel": format_match_number(m.get("displayNumber")),
            "isSolo": m.get("red") is None,
            "isPara": is_para_category(m.get("categoryCode")),
            "scoringMode": scoring_mode(m),
            "competitionSystem": competition_system(m),
            # "2/7" on the judge app — which performance of the category.
            "categoryPosition": position,
            "categoryTotal": total,
            # Drawn poomsae in performance order ([] = none drawn) and how
            # many rounds this match runs (1 in a cut-off preliminary /
            # semi-final, 2 in the final and in single elimination).
            "poomsae": match_poomsae(m),
            "poomsaeCount": poomsae_count(m),
            "currentPoomsae": None,
            "roundToken": new_round_token(),
            "activatedAt": datetime.now(timezone.utc).isoformat(),
            "stage": self.stage,
            "stageLabel": STAGE_LABELS[self.stage],
            "stageDurationSeconds": None,
            "timerRunning": False,
            "timerRemainingSeconds": None,
            "allJudgesSubmitted": False,
            "autoAdvance": None,
            "categoryResults": None,
            "corners": {
                "blue": self._blank_corner(m.get("blue")),
                "red": self._blank_corner(m.get("red")),
            },
        }
        await self.db.push_live_state(self.tournament_id, self.court_id, payload)

    async def _advance_stage(self, stage: str) -> None:
        """Moves the LIVE match to `stage` — operator click or automatic.
        Callers hold self._stage_lock."""
        if stage not in STAGES:
            raise HTTPException(status_code=400, detail=f"Unknown stage: {stage}")
        live = self.live_match
        if live is None:
            raise HTTPException(status_code=400, detail="No live match.")
        has_red = live.get("red") is not None
        if stage in SECOND_POOMSAE_STAGES and poomsae_count(live) < 2:
            raise HTTPException(status_code=400, detail="Only one poomsae is performed in this round — there is no Poomsae 2.")

        # Leaving a round: one last read so judge changes made since the
        # last poll still land in that poomsae's timeline.
        leaving_round = STAGE_TO_ROUND_KEY.get(self.stage)
        if leaving_round and self.timeline is not None and self.timeline.match_id == self.live_match_id:
            try:
                last = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
                self.timeline.observe_judges(leaving_round, last.get("corners") or {}, _now_ms())
            except Exception as exc:
                self.logger.info(f"[{self.name}] timeline final read failed (non-fatal): {exc}")

        self._auto = None
        self.stage = stage
        duration = self._duration_for_stage(stage)
        results_round_key = results_round_key_for(stage, live)
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        corners = state.get("corners") or {}

        # update_live_state (RTDB .update()), NOT push_live_state — a stage
        # change never wipes out other poomsae's judge scores. timerRunning
        # is ALWAYS False here: the countdown only starts on a tap.
        updates: dict = {
            "stage": stage,
            "stageLabel": stage_label(live, stage),
            # Poomsae performed / shown in this stage — the judge app and
            # the screen show it.
            "currentPoomsae": poomsae_name_for_stage(live, stage),
            # Which poomsae's scores the panel / external screen show.
            "resultsRoundKey": results_round_key,
            "stageDurationSeconds": duration,
            "timerRunning": False,
            "timerRemainingSeconds": duration,
            "timerStartedAt": None,
            "timerStartedAtMs": None,
            "autoAdvance": None,
            "categoryResults": None,
        }

        if stage in STAGE_TO_ROUND_KEY:
            # (Re-)entering a poomsae starts it fresh: new token (judge apps
            # reset to full marks), no restart / penalties, and any scores
            # left from an earlier attempt at this poomsae are cleared —
            # otherwise they'd count as "submitted" straight away.
            round_key = STAGE_TO_ROUND_KEY[stage]
            updates["roundToken"] = new_round_token()
            updates["allJudgesSubmitted"] = False
            cleared = False
            for color in (("blue", "red") if has_red else ("blue",)):
                updates[f"corners/{color}/roundAdjustments/{round_key}"] = None
                updates[f"corners/{color}/roundDeduction"] = None
                updates[f"corners/{color}/roundPenalty"] = None
                for judge_key, judge_data in _normalize_firebase_map((corners.get(color) or {}).get("judges")).items():
                    if isinstance(judge_data, dict) and round_key in judge_data:
                        updates[f"corners/{color}/judges/{judge_key}/{round_key}"] = None
                        cleared = True
            if cleared and self.timeline is not None:
                self.timeline.forget_judges(round_key)
        else:
            if stage == "preparation":
                updates["roundToken"] = new_round_token()
            # Results / Final / Category: show that poomsae's restart and
            # penalty totals. allJudgesSubmitted and roundToken stay as
            # they are, so judges keep their submitted screen.
            for color in (("blue", "red") if has_red else ("blue",)):
                adj = {}
                if results_round_key:
                    adj = _normalize_firebase_map(_normalize_firebase_map(
                        (corners.get(color) or {}).get("roundAdjustments")).get(results_round_key))
                updates[f"corners/{color}/roundDeduction"] = adj.get("restart")
                updates[f"corners/{color}/roundPenalty"] = adj.get("penalty")

        await self.db.update_live_state(self.tournament_id, self.court_id, updates)

        if stage in STAGE_TO_ROUND_KEY and self.timeline is not None:
            started_iso = datetime.now(timezone.utc).isoformat()
            self.timeline.start_round(results_round_key, _now_ms(), started_iso)
            # Where this poomsae is in the court's YouTube video — asked in
            # the background so the stage click never waits on the network.
            self._video_tasks.append(asyncio.create_task(
                self._fetch_video_position(self.timeline, results_round_key, started_iso)))

        if stage == "final_results":
            await self._safe_record_category_result(live)
            await self._publish_result()
        elif stage == "category_results":
            await self._safe_record_category_result(live)
            await self._write_category_results(live)

        await self._sync_auto()

    # ── Automatic stages ────────────────────────────────────────────────
    def _auto_enabled(self, stage: str) -> bool:
        return bool(getattr(self._settings(), f"auto_{stage}", False))

    def _auto_target(self) -> Optional[tuple[str, int]]:
        """The timed automatic step from the current stage, if any:
        last Results → Final Results, Final Results → Category Results.
        (Results 1 / 2 follow the judges' submits — see _tick_round.)"""
        live = self.live_match
        if live is None or self.stage is None:
            return None
        settings = self._settings()
        if self.stage == last_results_stage(live) and settings.auto_final_results:
            return "final_results", settings.final_results_delay
        if self.stage == "final_results" and settings.auto_category_results:
            return "category_results", settings.category_results_delay
        return None

    async def _sync_auto(self) -> None:
        """Schedules / cancels the pending automatic step to match the
        current stage and toggles, and mirrors it to match_data.autoAdvance
        (the panel's countdown). The delay counts from when the step was
        scheduled — on stage entry, or when its toggle was switched on."""
        target = self._auto_target()
        if target is None:
            if self._auto is not None:
                self._auto = None
                await self.db.update_live_state(self.tournament_id, self.court_id, {"autoAdvance": None})
            return
        to, delay = target
        if self._auto and self._auto["stage"] == self.stage and self._auto["to"] == to:
            return
        self._auto = {"stage": self.stage, "to": to, "due": time.monotonic() + delay}
        await self.db.update_live_state(self.tournament_id, self.court_id, {"autoAdvance": {
            "to": to,
            "toLabel": STAGE_LABELS[to],
            "seconds": delay,
            "at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
        }})

    def _next_stage(self, live_state: Optional[dict]) -> Optional[str]:
        """The step the operator would normally take next — highlighted on
        the panel."""
        live = self.live_match
        if live is None:
            return None
        st = self.stage
        if st == "preparation":
            return "poomsae_1"
        if st in ROUND_TO_RESULTS_STAGE:
            return ROUND_TO_RESULTS_STAGE[st] if (live_state or {}).get("allJudgesSubmitted") is True else None
        if st == "results_1":
            return "poomsae_2" if poomsae_count(live) >= 2 else "final_results"
        if st == "results_2":
            return "final_results"
        if st == "final_results":
            return "category_results"
        return None

    # ── YouTube position of each poomsae ─────────────────────────────────
    async def _fetch_video_position(self, timeline, round_key: str, started_iso: str, retry: bool = False) -> None:
        """
        Asks the server where this court's YouTube live video is right
        now (POST /api/youtube/stream-position) and keeps {id, t} on the
        timeline round; saved on Final Results with the match (first
        poomsae) and each performance doc. If the server can't be reached
        the round is marked pending and asked again on Final Results with
        `at` = when the poomsae started.
        """
        settings = self._settings()
        round_ = timeline.rounds.get(round_key)
        if round_ is None or not settings.video_sync_secret or not settings.video_sync_url:
            return
        body = {"tournamentId": self.tournament_id, "courtId": self.court_id}
        if retry:
            body["at"] = started_iso
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(settings.video_sync_url, json=body,
                                         headers={"X-Internal-Secret": settings.video_sync_secret})
                resp.raise_for_status()
                data = resp.json()
            # Re-entering the stage started a newer attempt — keep the newest.
            if round_.get("startedAt") != started_iso and not retry:
                return
            if data.get("t") is not None and data.get("videoId"):
                round_["video"] = {"id": data["videoId"], "t": int(data["t"])}
                round_.pop("videoPending", None)
            else:
                round_["videoPending"] = started_iso  # broadcast not live yet
        except Exception as exc:
            round_["videoPending"] = started_iso
            self.logger.info(f"[{self.name}] stream position request failed (will retry on Final Results): {exc}")

    async def _settle_video_positions(self) -> None:
        """Before saving results: wait for in-flight requests, then re-ask
        for any poomsae whose request failed."""
        pending = [t for t in self._video_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=8)
        self._video_tasks = [t for t in self._video_tasks if not t.done()]
        if self.timeline is None:
            return
        retries = [self._fetch_video_position(self.timeline, key, r["videoPending"], retry=True)
                   for key, r in self.timeline.rounds.items() if r.get("videoPending") and not r.get("video")]
        if retries:
            await asyncio.wait([asyncio.create_task(c) for c in retries], timeout=10)

    def _track(self, code: str, corner: Optional[str] = None, value=None) -> None:
        """One operator event into the live poomsae's timeline (see
        results.Timeline for the codes). No-op outside a poomsae."""
        if self.timeline is not None:
            self.timeline.add(self._round_key_for_events(), _now_ms(), code, corner, None, value)

    # ── Final result → Firestore (via the Firebase Sync plugin) ──────────
    async def _publish_result(self) -> dict:
        """
        Computes the live match's full result from match_data (see
        results.py) and publishes it as RESULT_EVENT. The Firebase Sync
        plugin saves it to the match doc. Safe to call again: the save is
        a merge, the result just gets rewritten.
        """
        live = self.live_match
        if live is None:
            raise HTTPException(status_code=400, detail="No live match.")
        match_id = live.get("id")
        if self.timeline is not None and self.timeline.match_id == match_id:
            await self._settle_video_positions()
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        registry = _normalize_firebase_map(await self.db.fetch_judges_registry(self.tournament_id, self.court_id))
        settings = self._settings()
        result = poomsae_results.build_match_result(
            state, registry, settings.scoring_method, poomsae_count(live),
            match_poomsae(live), state.get("resultsRoundKey"),
        )
        payload = {
            "match_id": match_id,
            "tournament_id": self.tournament_id,
            "court_id": self.court_id,
            "sport_id": self.sport_id,
            "discipline_id": self.discipline_id,
            "category_code": live.get("categoryCode"),
            "round_phase": live.get("roundPhase"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        # The match doc links the video at its first poomsae.
        first_round = (self.timeline.rounds.get("poomsae1") or {}) if self.timeline and self.timeline.match_id == match_id else {}
        if first_round.get("video"):
            payload["result"]["video"] = first_round["video"]
        # One analysis doc per athlete per poomsae, with the timeline —
        # written to …/performances by the Firebase Sync plugin.
        tournament = self.core_context.get("tournament")
        payload["performances"] = poomsae_results.build_performances(
            live, result, self.timeline if self.timeline and self.timeline.match_id == match_id else None, {
                "tournament_id": self.tournament_id,
                "tournament_name": getattr(tournament, "title", None),
                "court_id": self.court_id,
                "completed_at": payload["completed_at"],
            },
        )
        bus = self.core_context.get("event_bus")
        if not bus:
            self.archive_status[match_id] = {"state": "failed", "error": "No event bus — results not saved.", "at": payload["completed_at"]}
            self.logger.warning(f"[{self.name}] no event bus — result for {match_id} NOT saved")
            return payload
        self.archive_status[match_id] = {"state": "saving", "error": None, "at": payload["completed_at"]}
        await bus.publish(RESULT_EVENT, payload)
        # Shown as done in this court's own schedule right away.
        for m in self.matches:
            if m.get("id") == match_id:
                m["status"] = "completed"
        return payload

    async def _on_archived(self, data: dict) -> None:
        match_id = (data or {}).get("match_id")
        if not match_id:
            return
        self.archive_status[match_id] = {
            "state": "saved" if data.get("ok") else "failed",
            "error": data.get("error"),
            "at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Category Results ────────────────────────────────────────────────
    async def _record_category_result(self, live: dict) -> dict:
        """Computes the live match's scores (standings.match_score — same
        maths as the panel's table) and stores them under
        poomsae_category_results/{category}/{matchId}."""
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        registry = poomsae_standings.registry_numbers(await self.db.fetch_judges_registry(self.tournament_id, self.court_id))
        method = self._settings().scoring_method
        keys = round_keys_for(live)
        corners_state = state.get("corners") or {}
        corners: dict = {}
        for color in ("blue", "red"):
            ident = self._identity(live.get(color))
            if ident is None:
                continue
            score = poomsae_standings.match_score(corners_state.get(color) or {}, keys, registry, method) or {}
            corners[color] = {**ident, **score}
        record = {
            "matchId": live.get("id"),
            "displayNumber": live.get("displayNumber"),
            "roundPhase": live.get("roundPhase"),
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "corners": corners,
            "winner": poomsae_standings.duel_winner(corners.get("blue"), corners.get("red")) if "red" in corners else None,
        }
        category_key = _rtdb_key(live.get("categoryCode"))
        self._category_records.setdefault(category_key, {})[live.get("id")] = record
        await asyncio.to_thread(lambda: self._category_results_ref(category_key).child(live.get("id")).set(record))
        return record

    async def _safe_record_category_result(self, live: dict) -> None:
        try:
            await self._record_category_result(live)
        except Exception as exc:
            self.logger.warning(f"[{self.name}] couldn't store category result (kept in memory): {exc}")

    async def _read_category_records(self, category_key: str) -> dict:
        try:
            remote = await asyncio.to_thread(lambda: self._category_results_ref(category_key).get())
        except Exception as exc:
            self.logger.info(f"[{self.name}] couldn't read category results (using this session's): {exc}")
            remote = None
        records = {k: v for k, v in _normalize_firebase_map(remote).items() if isinstance(v, dict)}
        records.update(self._category_records.get(category_key, {}))
        return records

    async def _build_category_results(self, live: dict) -> dict:
        code = live.get("categoryCode")
        records = await self._read_category_records(_rtdb_key(code))
        category = [m for m in self.matches if m.get("categoryCode") == code]
        info = [{
            "id": m.get("id"),
            "displayNumber": m.get("displayNumber"),
            "displayNumberLabel": format_match_number(m.get("displayNumber")),
            "roundPhase": m.get("roundPhase"),
            "blue": self._identity(m.get("blue")),
            "red": self._identity(m.get("red")),
        } for m in category]
        system = competition_system(live)
        phase = live.get("roundPhase")
        qualify_count = None
        if system == "cut_off":
            # How many go through = how many slots the next cut-off round
            # already has scheduled (bracket builder's pendingAdvancement).
            next_phase = poomsae_standings.NEXT_CUT_OFF_PHASE.get(phase)
            if next_phase:
                qualify_count = sum(1 for m in category if m.get("roundPhase") == next_phase) or None
        view = poomsae_standings.build_category_view(system, phase, info, records, live.get("id"), qualify_count)
        view.update({
            "matchId": live.get("id"),
            "categoryLabel": match_category_label(live),
            "roundPhase": phase,
            "roundPhaseLabel": round_phase_label(phase),
            "displayNumberLabel": format_match_number(live.get("displayNumber")),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        })
        return view

    async def _write_category_results(self, live: dict) -> None:
        try:
            view = await self._build_category_results(live)
        except Exception as exc:
            self.logger.warning(f"[{self.name}] couldn't build category results: {exc}")
            view = {"error": str(exc), "generatedAt": datetime.now(timezone.utc).isoformat()}
        await self.db.update_live_state(self.tournament_id, self.court_id, {"categoryResults": view})

    # ── Poll loop: judges + automatic stages ─────────────────────────────
    async def _poll_loop(self) -> None:
        """Once a second: judge submissions (during a poomsae) and the
        automatic result stages. Errors are non-fatal on purpose — a
        transient RTDB hiccup shouldn't stop the loop for the rest of the
        match."""
        while True:
            try:
                await asyncio.sleep(1)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.info(f"[{self.name}] poll loop error (non-fatal): {exc}")

    async def _tick(self) -> None:
        if not self.live_match_id or self.stage is None:
            return
        async with self._stage_lock:
            if self.stage in STAGE_TO_ROUND_KEY:
                await self._tick_round()
            await self._sync_auto()
            auto = self._auto
            if auto and auto["stage"] == self.stage and time.monotonic() >= auto["due"]:
                self._auto = None
                await self._advance_stage(auto["to"])

    async def _tick_round(self) -> None:
        """
        During a poomsae: checks whether every relevant corner (blue
        always; red too, unless solo) has at least judge_count judges who
        explicitly SUBMITTED this poomsae (`submitted: true` — the judge
        app pushes live values while a judge is still adjusting).

        Once all are in: sets allJudgesSubmitted, and — if that Results
        stage is on Auto — goes straight to Results 1 / Results 2.

        Never decides anything while the court's settings are unknown
        (_settings_loaded False) — a default judge count could flag a
        round complete too early.
        """
        settings = self._settings()
        round_key = STAGE_TO_ROUND_KEY[self.stage]
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id)
        if not state:
            return
        corners = state.get("corners") or {}
        if self.timeline is not None and self.timeline.match_id == self.live_match_id:
            self.timeline.observe_judges(round_key, corners, _now_ms())
        if not self._settings_loaded:
            return
        # Who must be judged comes from the match itself (blue always, red
        # only in a side-by-side match) — never from whatever nodes happen
        # to exist under corners/ in RTDB.
        live = self.live_match
        colors = ("blue", "red") if live is not None and live.get("red") is not None else ("blue",)
        required = [corners.get(color) or {} for color in colors]
        all_judged = all(
            sum(
                1 for j in _normalize_firebase_map(c.get("judges")).values()
                if isinstance(j, dict) and isinstance(j.get(round_key), dict)
                and j[round_key].get("submitted") is True
            ) >= settings.judge_count
            for c in required
        )
        if not all_judged:
            return
        if state.get("allJudgesSubmitted") is not True:
            await self.db.update_live_state(self.tournament_id, self.court_id, {"allJudgesSubmitted": True})
            self._track("as")
        results_stage = ROUND_TO_RESULTS_STAGE[self.stage]
        if self._auto_enabled(results_stage):
            await self._advance_stage(results_stage)

    # ── Livestream: publish the live scoreboard on the event bus ─────────
    async def _live_loop(self) -> None:
        """Read-only: never writes to RTDB and never takes _stage_lock, so it
        can't delay an operator click. Errors are non-fatal, like _poll_loop."""
        while True:
            try:
                await asyncio.sleep(LIVE_POLL_SECONDS)
                await self._publish_live()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.info(f"[{self.name}] live publish error (non-fatal): {exc}")

    async def _on_live_resync(self, data: dict) -> None:
        self._live_last_stage = (None, None)
        await self._publish_live(force=True)

    async def _publish_live(self, force: bool = False) -> None:
        bus = self.core_context.get("event_bus")
        if not bus or not self.tournament_id or not self.court_id:
            return
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        # Nothing on the floor → an empty, hidden scoreboard.
        if not self.live_match_id or state.get("matchId") != self.live_match_id:
            state = {}
        settings = self._settings()
        payload = poomsae_live.shape_live_payload(
            state,
            list(range(1, settings.judge_count + 1)),
            settings.scoring_method,
            _remaining_now(state, allow_negative=True) if state else None,
        )

        stage_key = (payload["match_id"], payload["stage"])
        if stage_key != self._live_last_stage:
            previous = self._live_last_stage[1]
            self._live_last_stage = stage_key
            await bus.publish(STAGE_CHANGED_EVENT, {
                "match_id": payload["match_id"], "stage": payload["stage"], "previous_stage": previous,
            })

        now = time.monotonic()
        heartbeat = force or now >= self._live_heartbeat_due
        dump = json.dumps(payload, sort_keys=True, default=str)
        if heartbeat or dump != self._live_last_dump:
            self._live_last_dump = dump
            self._live_heartbeat_due = now + LIVE_HEARTBEAT_SECONDS
            await bus.publish(LIVE_UPDATE_EVENT, payload)

    # ── Restart / round reset ───────────────────────────────────────────
    # A performer has to restart their pattern. Clears every judge's score
    # for the CURRENT poomsae only (never touches the timer), and records
    # the automatic restart deduction. One multi-location RTDB update:
    # slash-separated paths, None = delete.
    async def _reset_round(self) -> None:
        live = self.live_match
        if live is not None and live.get("red") is not None:
            raise HTTPException(status_code=400, detail="Restart only applies to a solo performance, not a side-by-side match.")
        round_key = STAGE_TO_ROUND_KEY.get(self.stage)
        if not round_key:
            raise HTTPException(status_code=400, detail="Restart is only available while a poomsae is being performed.")

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
            updates[f"corners/{color}/roundAdjustments/{round_key}/restart"] = deduction

        # The judges' scores for this round are gone — their apps start the
        # round again from full marks.
        updates["roundToken"] = new_round_token()
        updates["allJudgesSubmitted"] = False
        for color in ("blue", "red"):
            if corners.get(color):
                self._track("rs", color, deduction)
        if self.timeline is not None:
            self.timeline.forget_judges(round_key)
        await self.db.update_live_state(self.tournament_id, self.court_id, updates)

    # ── Operator score correction ───────────────────────────────────────
    # The operator clicks a judge's Accuracy / Presentation in the panel and
    # types the right value — e.g. a judge pressed the wrong button, or a
    # tablet died. Writes into that judge's own entry for the poomsae on
    # screen (field by field, so anything else the judge app stored stays),
    # marks it submitted and editedByOperator. On Final / Category Results
    # the saved result and the category ranking are rebuilt right away.
    async def _set_judge_score(self, payload: "JudgeScorePayload") -> dict:
        live = self.live_match
        if live is None:
            raise HTTPException(status_code=400, detail="No live match.")
        if payload.corner == "red" and live.get("red") is None:
            raise HTTPException(status_code=400, detail="This is a solo performance — there is no red athlete.")
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        round_key = state.get("resultsRoundKey") or results_round_key_for(self.stage, live)
        if not round_key:
            raise HTTPException(status_code=400, detail="No poomsae is shown right now — nothing to correct.")
        acc = round(payload.accuracy, 2)
        pres = round(payload.presentation, 2)
        path = f"corners/{payload.corner}/judges/{payload.judge}/{round_key}"
        await self.db.update_live_state(self.tournament_id, self.court_id, {
            f"{path}/accuracy": acc,
            f"{path}/presentation": pres,
            f"{path}/total": round(acc + pres, 2),
            f"{path}/submitted": True,
            f"{path}/editedByOperator": True,
            f"{path}/editedAt": datetime.now(timezone.utc).isoformat(),
        })
        if self.stage in ("final_results", "category_results"):
            await self._safe_record_category_result(live)
            await self._publish_result()
            if self.stage == "category_results":
                await self._write_category_results(live)
        return {"roundKey": round_key, "accuracy": acc, "presentation": pres, "total": round(acc + pres, 2)}

    # ── Penalty ─────────────────────────────────────────────────────────
    # Adds settings.penalty_deduction to one corner's penalty total for the
    # poomsae being performed, or the one whose Results are on screen.
    # Stored separately from roundDeduction (restart).
    async def _add_penalty(self, corner: str) -> float:
        round_key = self._round_key_for_events()
        if round_key is None:
            raise HTTPException(status_code=400, detail="Penalties can only be given during a poomsae or its results.")
        live = self.live_match
        if corner == "red" and (live is None or live.get("red") is None):
            raise HTTPException(status_code=400, detail="This is a solo performance — there is no red athlete.")
        state = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
        current = ((state.get("corners") or {}).get(corner) or {}).get("roundPenalty") or 0
        total = round(current + self._settings().penalty_deduction, 3)
        await self.db.update_live_state(self.tournament_id, self.court_id, {
            f"corners/{corner}/roundPenalty": total,
            f"corners/{corner}/roundAdjustments/{round_key}/penalty": total,
        })
        self._track("pn", corner, total)
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
                # The LIVE match's stage (None when nothing is live).
                "stage": self.stage,
                "stageLabel": STAGE_LABELS.get(self.stage, ""),
                # Next normal step for the live match — highlighted on the panel.
                "nextStage": self._next_stage(live),
                # The SELECTED match (what the operator is looking at).
                "match": selected,
                "matchLabels": {
                    "category": match_category_label(selected),
                    "scoringMode": scoring_mode(selected),
                    "system": competition_system(selected),
                    "roundPhase": round_phase_label(selected.get("roundPhase")),
                    "displayNumber": format_match_number(selected.get("displayNumber")),
                    "poomsae": match_poomsae(selected),
                    "poomsaeCount": poomsae_count(selected),
                } if selected else None,
                # Names/clubs for the selected match's corners, for showing
                # it before it's live.
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
                # Saving the live match's result to Firestore (Final Results).
                "archive": self.archive_status.get(self.live_match_id) if self.live_match_id else None,
                # match_data in RTDB — what the external screen shows
                # (includes autoAdvance and, in Category Results, categoryResults).
                "live": live,
                "categoryMatches": self._category_matches(),
                "judgesRegistry": judges_registry,
                "settings": self._settings().model_dump(exclude={"video_sync_secret"}),
                # False while the court's settings couldn't be read from
                # RTDB — the values in "settings" are defaults then.
                "settingsLoaded": self._settings_loaded,
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
            if stage not in STAGES:
                raise HTTPException(status_code=400, detail=f"Unknown stage: {stage}")
            async with self._stage_lock:
                if not self.selected_is_live:
                    # Preparation on a match that isn't live = put it on the
                    # floor (and the external screen). Rounds need that first.
                    if stage != "preparation":
                        raise HTTPException(status_code=400, detail="Click Preparation first to put this match on the floor.")
                    await self._activate_selected_match()
                else:
                    await self._advance_stage(stage)
            return {"stage": self.stage, "liveMatchId": self.live_match_id}

        @router.post("/auto")
        async def set_auto(payload: AutoTogglePayload):
            # The Auto toggle on a result stage's pill. Saved with the other
            # panel settings, so it survives restarts.
            if not self.tournament_id or not self.court_id:
                raise HTTPException(status_code=400, detail="Court isn't bound to a tournament yet — not saved.")
            if not self._settings_loaded:
                raise HTTPException(status_code=409, detail="This court's settings couldn't be read from Firebase yet — try again in a moment.")
            values = {k: getattr(self._settings(), k) for k in PERSISTED_SETTINGS}
            values[f"auto_{payload.stage}"] = payload.enabled
            async with self._stage_lock:
                await self._save_persisted_settings(values)
                if self.live_match_id:
                    await self._sync_auto()
            return {"ok": True, "settings": self._settings().model_dump(exclude={"video_sync_secret"})}

        def _require_live():
            if not self.live_match_id:
                raise HTTPException(status_code=400, detail="No match is live — click Preparation on a match first.")

        @router.post("/timer/start")
        async def start_timer():
            _require_live()
            current = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
            if current.get("stageDurationSeconds") is None:
                raise HTTPException(status_code=400, detail="No timer in this stage.")
            if current.get("timerRunning"):
                return {"ok": True}
            remaining = current.get("timerRemainingSeconds")
            if remaining is None:
                remaining = current["stageDurationSeconds"]
            self._track("ts", None, remaining)
            await self.db.update_live_state(self.tournament_id, self.court_id, {
                "timerRunning": True,
                # Value AT this start — readers subtract elapsed time.
                "timerRemainingSeconds": remaining,
                "timerStartedAt": datetime.now(timezone.utc).isoformat(),
                # Firebase SERVER time — what the judge tablets count from.
                "timerStartedAtMs": {".sv": "timestamp"},
            })
            return {"ok": True}

        @router.post("/timer/pause")
        async def pause_timer(payload: TimerPausePayload):
            _require_live()
            current = await self.db.fetch_live_state(self.tournament_id, self.court_id) or {}
            if not current.get("timerRunning"):
                return {"ok": True}
            remaining = _remaining_now(current)
            if remaining is None:
                remaining = max(0.0, payload.remainingSeconds or 0.0)
            self._track("tp", None, remaining)
            await self.db.update_live_state(self.tournament_id, self.court_id, {
                "timerRunning": False,
                "timerRemainingSeconds": remaining,
            })
            return {"ok": True}

        @router.post("/round/reset")
        async def reset_round():
            _require_live()
            async with self._stage_lock:
                await self._reset_round()
            return {"ok": True}

        @router.post("/round/reset-full")
        async def reset_full():
            # Start the poomsae on screen completely over: every judge's
            # score, restart deduction and penalties cleared, timer back to
            # full and stopped, judges' apps back to full marks. Unlike
            # Restart, NO deduction is applied. Works from the poomsae or its
            # Results (it goes back to the poomsae).
            _require_live()
            async with self._stage_lock:
                round_key = self._round_key_for_events()
                if round_key is None:
                    raise HTTPException(status_code=400, detail="Reset is available during a poomsae or its results.")
                await self._advance_stage("poomsae_1" if round_key == "poomsae1" else "poomsae_2")
            return {"ok": True, "stage": self.stage}

        @router.post("/judge-score")
        async def judge_score(payload: JudgeScorePayload):
            _require_live()
            async with self._stage_lock:
                result = await self._set_judge_score(payload)
            return {"ok": True, **result}

        @router.post("/penalty")
        async def penalty(payload: PenaltyPayload):
            _require_live()
            total = await self._add_penalty(payload.corner)
            return {"ok": True, "corner": payload.corner, "roundPenalty": total}

        @router.post("/penalty/clear")
        async def clear_penalty(payload: PenaltyPayload):
            # Undo for a penalty given by mistake — clears that corner's
            # penalty total for the current poomsae.
            _require_live()
            updates = {f"corners/{payload.corner}/roundPenalty": None}
            round_key = self._round_key_for_events()
            if round_key:
                updates[f"corners/{payload.corner}/roundAdjustments/{round_key}/penalty"] = None
            await self.db.update_live_state(self.tournament_id, self.court_id, updates)
            self._track("pc", payload.corner)
            return {"ok": True}

        @router.post("/results/save")
        async def save_results():
            # Retry for a failed save (or save again after a correction)
            # without leaving Final / Category Results. Also refreshes the
            # category ranking.
            _require_live()
            if self.stage not in ("final_results", "category_results"):
                raise HTTPException(status_code=400, detail="Results are saved from Final Results.")
            async with self._stage_lock:
                live = self.live_match
                await self._safe_record_category_result(live)
                payload = await self._publish_result()
                if self.stage == "category_results":
                    await self._write_category_results(live)
            return {"ok": True, "matchId": payload["match_id"], "result": payload["result"]}

        @router.post("/settings")
        async def update_settings(payload: PoomsaeControlSettings):
            # Only the panel-managed fields — video_sync_* (the panel never
            # sends them) keep coming from the framework settings.
            values = {k: getattr(payload, k) for k in PERSISTED_SETTINGS}
            if not self.tournament_id or not self.court_id:
                raise HTTPException(status_code=400, detail="Court isn't bound to a tournament yet — settings not saved.")
            async with self._stage_lock:
                await self._save_persisted_settings(values)
                if self.live_match_id:
                    await self._sync_auto()
            await self._reconcile_judge_slots()
            return {"ok": True, "settings": self._settings().model_dump(exclude={"video_sync_secret"})}

        @router.post("/reload")
        async def reload():
            # Re-reads the schedule (e.g. after a re-push from the bracket
            # builder); keeps the selection and whatever is live, and
            # refreshes the live match's names / clubs / countries.
            await self._reload_matches()
            return {"total": len(self.matches)}

        return router