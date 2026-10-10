import asyncio, httpx
from datetime import datetime

from typing import Optional

from fastapi import APIRouter

from plugins.base_plugin import BasePlugin
from plugins.core.firebase_sync.settings import FirebaseSyncSettings

MATCH_NOTIFY_TIMEOUT_SECONDS = 5.0
MATCH_PROCESS_TIMEOUT_SECONDS = 10.0

# TODO: replace with the actual deployed URL of the processMatchResult
# function once you deploy it (see functions_index.js) — this is NOT a
# real endpoint, just following the same naming/host pattern as the
# existing notifymatchstart URL below.
PROCESS_MATCH_RESULT_URL = "https://processmatchresult-fv4r3klxxa-ey.a.run.app"

# Same header value already used for notifyMatchStart — kept identical
# so both calls authenticate against the same INTERNAL_SECRET on the
# Cloud Functions side. Worth eventually moving out of source into
# settings/env rather than hardcoding, same as the existing one below.
INTERNAL_SECRET_HEADER_VALUE = "ov0Xpo0IJaZ2scWTt6Q40sov0Xpo0IJaZ2scWTt6Q40sxtA5cnAGhB0CAB4i0AEIM="


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _prune(value):
    """Drops None / [] / {} recursively — fields that don't apply are left
    out of the match doc rather than written as null."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = _prune(v)
            if v is None or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [_prune(v) for v in value if v is not None]
    return value


class FirebaseSyncPlugin(BasePlugin):
    """
    Every scoreboard.stable_update is pushed to Firebase Realtime Database
    via DatabasePort.push_live_state() at
        tournaments/{tournament_id}/courts/{court_id}/live/state

    Additionally, this plugin subscribes to scoreboard.WIN — the instant a
    fight is decided, not whenever (if ever) the console happens to load
    the next one. parser.py's on_win() passes a full, already-isolated
    snapshot of the winning state directly on that event, so archiving
    here never depends on cross-event ordering with stable_update, and
    a match still archives correctly even if it's the very last fight of
    the day with no "next match" to trigger off of. Archived as:
        sports/{sport_id}/disciplines/{discipline_id}/matches/{tournamentId}_{match_id}

    After archiving, this plugin explicitly POSTs to the
    processMatchResult Cloud Function (see functions_index.js) to run
    medal/athlete-stat/bracket-propagation logic. That used to happen
    automatically via a Firestore onDocumentWritten trigger, but that
    trigger fired on EVERY write to a match doc — including the
    scheduler's initial "ready" doc creation and this very function's
    own resultsProcessed write-back — the large majority of which did
    no real work but were still billed invocations. Calling it directly,
    exactly once per real completion, avoids that overhead. The
    trade-off: there is no more automatic catch-all — any OTHER system
    that completes a match (an admin dashboard override, a walkover set
    outside this listener's flow) must call that same endpoint itself.

    POOMSAE: the Poomsae Control Panel plugin publishes
    poomsae.FINAL_RESULTS when the operator clicks Final Results, with
    the full result already computed from RTDB (see
    poomsae_control/results.py). _on_poomsae_result merges it into the
    match doc the bracket builder created — results only, identity
    fields untouched, same rule as a bracket-managed kyorugi match — and
    publishes firebase_sync.match_archived back so the operator panel can
    show whether the save worked. The same event carries one analysis doc
    per athlete per poomsae (with the silent timeline), written to
        sports/{sport_id}/disciplines/{discipline_id}/performances/{matchId}_{n}_{corner}
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._settings: Optional[FirebaseSyncSettings] = None
        self._subscriptions: list[tuple] = []

        self._tournament = None
        self._tournament_id: Optional[str] = None
        self._court_id: Optional[str] = None
        self._sport_id: Optional[str] = None
        self._discipline_id: Optional[str] = None

        self._last_match_id: Optional[str] = None

        self._push_count = 0
        self._history_count = 0
        self._process_trigger_count = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return "Firebase Database Sync"

    @property
    def requires_internet(self) -> bool:
        return True

    @property
    def settings_schema(self):
        return FirebaseSyncSettings

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._settings = self.core_context["settings_manager"].get_settings(self.name)

        tournament = self.core_context.get("tournament")
        if tournament is None:
            self.logger.warning(
                "FirebaseSync: no tournament in context (offline mode) — plugin idle."
            )
            return

        self._tournament = tournament
        self._tournament_id = tournament.id
        self._sport_id = tournament.sport
        self._discipline_id = tournament.discipline
        self._court_id = self.core_context.get("court_id")

        if not self._court_id:
            self.logger.warning(
                "FirebaseSync: no court_id in context — RTDB path will be incomplete."
            )

        if not self._settings.enabled:
            self.logger.info("FirebaseSync disabled via settings — plugin idle.")
            return

        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [
                ("scoreboard.stable_update", self._on_stable_update),
                ("scoreboard.WIN", self._on_win),
                ("poomsae.FINAL_RESULTS", self._on_poomsae_result),
            ]
            bus.subscribe_many(self._subscriptions)

        self.logger.info(
            "FirebaseSync started",
            tournament_id=self._tournament_id,
            court_id=self._court_id,
            sport_id=self._sport_id,
            discipline_id=self._discipline_id,
        )

    async def stop(self) -> None:
        bus = self.core_context.get("event_bus")
        if bus and self._subscriptions:
            bus.unsubscribe_many(self._subscriptions)
            self._subscriptions = []

        self.logger.warning("FirebaseSync plugin stopped.")

    async def on_settings_changed(self, new_settings: FirebaseSyncSettings) -> None:
        self.logger.info("FirebaseSync settings changed — restarting.")
        await self.stop()
        self._settings = new_settings
        await self.start()

    # ------------------------------------------------------------------ event handler

    async def _on_stable_update(self, data: dict) -> None:
        if not self._tournament_id or not data:
            return

        match_id = data.get("match_id")
        is_new_match = match_id and match_id != self._last_match_id

        if is_new_match:
            # Archiving used to happen right here, keyed off detecting
            # the NEXT match starting — moved to _on_win below, which
            # fires the instant a fight is actually decided instead of
            # whenever (if ever) the console happens to load a next one.
            # This condition still matters for the "match starting"
            # notification, which is a genuinely separate concern from
            # archiving a finished result.
            asyncio.create_task(self._notify_match_start(match_id, data))

        self._last_match_id = match_id

        try:
            await self.core_context["db"].push_live_state(
                self._tournament_id, self._court_id, _json_safe(data)
            )
            self._push_count += 1
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("FirebaseSync: push_live_state failed", exception=exc)

    async def _on_win(self, data: dict) -> None:
        """
        Archives the match the INSTANT it's decided. `data` here is the
        deep-copied snapshot parser.py's on_win() passes directly on the
        event — not something read back off self._last_match_state or
        any other separately-tracked state — so this has no dependency
        on _on_stable_update having already run for this same match, and
        no dependency on a next match ever being detected at all.
        """
        if not self._tournament_id or not data:
            return

        match_id = data.get("match_id")
        if not match_id:
            self.logger.warning("FirebaseSync: WIN event with no match_id — skipping archive.")
            return

        await self._archive_match(match_id, data)

    # ------------------------------------------------------------------ poomsae

    async def _on_poomsae_result(self, data: dict) -> None:
        """
        Archives a poomsae match when the operator clicks Final Results.
        `data` is the event payload from PoomsaeControlPlugin._publish_result:
            {match_id, tournament_id, court_id, sport_id, discipline_id,
             category_code, round_phase, completed_at, result}
        match_id is already the full Firestore doc id (the poomsae plugin
        reads matches straight from Firestore), so no tournament prefix.

        Written like a bracket-managed kyorugi match (merge — names, entries
        and bracket fields stay untouched), into the same maps:
            round       number of poomsae performed (1, or 2 in a final)
            win         "blue" / "red" side by side; None for a solo
                        performance or an undecided tie
            win_score   "8.533-7.966" side by side, "8.533" solo
            win_type    PTS (score) · PRS (tie → presentation) · ALL (tie →
                        all judges' total); None when undecided
            status: "completed", resultsProcessed: False, completedAt
            video       {id, t}: YouTube video id + second where the first
                        poomsae starts (each performance doc has its own)
            blue / red:
                points      one entry per poomsae, like kyorugi's per round:
                            {round, poomsae: {key, name}, points (round
                             score), accuracy, presentation, total (judges'
                             averages), restart, penalties, judges: {"1":
                             {accuracy, presentation, total, submitted,
                              trimmed, presentationCategories, technical}},
                             trimmedJudges, totalAllJudges,
                             allJudgesSubmitted}
                finalScore  the match score (average of the poomsae)
        """
        match_id = (data or {}).get("match_id")
        if not match_id:
            self.logger.warning("FirebaseSync: poomsae result with no match_id — skipping archive.")
            return
        result = data.get("result") or {}
        sport_id = data.get("sport_id") or self._sport_id
        discipline_id = data.get("discipline_id") or self._discipline_id
        if not sport_id or not discipline_id:
            await self._report_archived(match_id, False, "No sport/discipline context.")
            return

        merge_payload = {
            "round": result.get("round"),
            "win": result.get("win"),
            "win_score": result.get("win_score"),
            "win_type": result.get("win_type"),
            "status": "completed",
            "resultsProcessed": False,
            "completedAt": data.get("completed_at"),
            # YouTube video at the first poomsae: {id, t (seconds)}.
            "video": result.get("video"),
        }
        for color in ("blue", "red"):
            corner = result.get(color)
            if corner:
                merge_payload[color] = {
                    "points": corner.get("points"),
                    "finalScore": corner.get("finalScore"),
                }
        # Fields that don't apply (no winner, no win_type…) aren't written.
        merge_payload = _prune(merge_payload)

        db = self.core_context["db"]
        try:
            await db.save_match_state(sport_id, discipline_id, match_id, _json_safe(merge_payload))
            self._history_count += 1
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("FirebaseSync: save_match_state (poomsae result) failed", exception=exc, doc_id=match_id)
            await self._report_archived(match_id, False, str(exc))
            return

        # Analysis docs — one per athlete per poomsae, with the timeline.
        performances = data.get("performances") or []
        if performances:
            try:
                await self._save_performances(sport_id, discipline_id, performances)
            except Exception as exc:
                self._last_error = str(exc)
                self.logger.error("FirebaseSync: saving poomsae performances failed", exception=exc, doc_id=match_id)
                await self._report_archived(match_id, False, f"Match saved, performances failed: {exc}")
                return

        await self._report_archived(match_id, True, None)

        # Same post-processing call as kyorugi — but only for a side-by-side
        # match with a decided winner, the case bracket propagation needs.
        # A solo cut-off performance has no per-match winner; its ranking
        # comes from the whole round's scores, not from one match doc.
        if result.get("format") == "duel" and result.get("win"):
            await self._trigger_result_processing(match_id, sport_id, discipline_id)

    async def _save_performances(self, sport_id: str, discipline_id: str, performances: list) -> None:
        """
        sports/{sport}/disciplines/{discipline}/performances/{performanceId}
        One doc per athlete per poomsae — {matchId}_{poomsaeNumber}_{corner}
        — fully replaced on every save (a re-saved result rewrites them).
        Written straight through the adapter's Firestore client in one
        batch; DatabasePort has no generic "save document" method.
        """
        firestore_db = self.core_context["db"].firestore_db
        collection = (
            firestore_db.collection("sports").document(sport_id)
            .collection("disciplines").document(discipline_id)
            .collection("performances")
        )

        def _write():
            batch = firestore_db.batch()
            for doc in performances:
                batch.set(collection.document(doc["performanceId"]), _json_safe(doc))
            batch.commit()

        await asyncio.to_thread(_write)

    async def _report_archived(self, match_id: str, ok: bool, error) -> None:
        bus = self.core_context.get("event_bus")
        if bus:
            await bus.publish("firebase_sync.match_archived", {"match_id": match_id, "ok": ok, "error": error})

    async def _notify_match_start(self, match_id: str, data: dict) -> None:
        athletes = [
            name
            for name in (
                data.get("blue", {}).get("name"),
                data.get("red", {}).get("name"),
            )
            if name
        ]
        if not athletes:
            self.logger.warning(
                "FirebaseSync: match start with no athlete names — skipping notify",
                match_id=match_id,
            )
            return

        try:
            async with httpx.AsyncClient(timeout=MATCH_NOTIFY_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    "https://notifymatchstart-fv4r3klxxa-ey.a.run.app",
                    json={"match_id": match_id, "athletes": athletes, "court_id": self._court_id, "tournament_id": self._tournament_id},
                    headers={"X-Internal-Secret": INTERNAL_SECRET_HEADER_VALUE},
                )
                resp.raise_for_status()
            self._notify_count = getattr(self, "_notify_count", 0) + 1
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error(
                "FirebaseSync: match start notify failed",
                exception=exc,
                match_id=match_id,
            )

    async def _archive_match(self, match_id: str, state: dict) -> None:
        if not self._sport_id or not self._discipline_id:
            self.logger.warning(
                "FirebaseSync: no sport/discipline context — skipping history save."
            )
            return

        doc_id = f"{self._tournament_id}_{match_id}" if self._tournament_id else match_id
        db = self.core_context["db"]

        # Look at what's already there before deciding what we're allowed
        # to touch. If blue/red already carry entryId, this match came
        # from the bracket/roster system — that's the only place entryId
        # can come from, since the UDP scoreboard feed never produces it.
        existing = None
        try:
            existing = await db.fetch_match_state(self._sport_id, self._discipline_id, doc_id)
        except Exception as exc:
            self.logger.warning(
                "FirebaseSync: could not read existing match doc before archiving — "
                "falling back to full write",
                exception=exc, doc_id=doc_id,
            )

        is_bracket_managed = bool(
            existing
            and (existing.get("blue", {}).get("entryId") or existing.get("red", {}).get("entryId"))
        )

        payload = dict(state)
        payload.pop("clk", None)
        payload.pop("state", None)
        payload.pop("fight_started", None)

        if is_bracket_managed:
            # This match's identity — names, category, roster links,
            # country, bracket position — is authoritative and came from
            # the bracket system. The scoreboard hardware only ever
            # transcribes a short, operator-typed version of some of
            # these (abbreviated names, a re-derived category string in
            # a different format entirely) and must never overwrite the
            # real thing. Write ONLY what the listener is actually the
            # source of truth for: the live scoring result.
            merge_payload = {
                "round": payload.get("round"),
                "win": payload.get("win"),
                "win_score": payload.get("win_score"),
                "win_type": payload.get("win_type"),
                "status": "completed",
                "resultsProcessed": False,
                "blue": {"points": payload.get("blue", {}).get("points")},
                "red": {"points": payload.get("red", {}).get("points")},
            }
            self.logger.info(
                "FirebaseSync: bracket-managed match — writing results only, "
                "preserving existing identity/roster fields",
                doc_id=doc_id,
            )
        else:
            # No pre-existing bracket record for this match — we're the
            # only source of truth (livestream/analytics-only
            # tournament), so archive everything the listener observed,
            # same as before.
            merge_payload = payload
            merge_payload["status"] = "completed"
            merge_payload["resultsProcessed"] = False
            merge_payload["tournamentId"] = self._tournament_id
            merge_payload["tournamentName"] = getattr(self._tournament, "title", None)
            merge_payload["athletes"] = [
                state.get("blue", {}).get("name"),
                state.get("red", {}).get("name"),
            ]

        try:
            await db.save_match_state(self._sport_id, self._discipline_id, doc_id, merge_payload)
            self._history_count += 1
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("FirebaseSync: save_match_state (history) failed", exception=exc)
            return  # don't trigger processing on a write we know failed

        await self._trigger_result_processing(doc_id)

    async def _trigger_result_processing(self, doc_id: str, sport_id: Optional[str] = None,
                                         discipline_id: Optional[str] = None) -> None:
        """
        Tells the processMatchResult Cloud Function to run medal/athlete-
        stat/bracket-propagation logic for this match NOW, via direct
        HTTP call — replacing the old onDocumentWritten Firestore
        trigger, which fired on every write to a match doc (initial
        bracket scheduling, the result merge, AND the function's own
        resultsProcessed write-back), most of which did no real work but
        still counted as a billed invocation. This call only happens
        once, right after we know a result was actually just archived.

        The function re-reads the match doc itself and claims it via a
        Firestore transaction before processing (see
        claimMatchForProcessing in functions_index.js), so a retried or
        duplicate call here is safe — it can't double-run the medal/
        propagation logic; a duplicate just gets back
        {"processed": false, "reason": "already_processed"} or similar.
        """
        try:
            async with httpx.AsyncClient(timeout=MATCH_PROCESS_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    PROCESS_MATCH_RESULT_URL,
                    json={
                        "sport_id": sport_id or self._sport_id,
                        "discipline_id": discipline_id or self._discipline_id,
                        "match_id": doc_id,
                    },
                    headers={"X-Internal-Secret": INTERNAL_SECRET_HEADER_VALUE},
                )
                resp.raise_for_status()
            self._process_trigger_count += 1
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error(
                "FirebaseSync: processMatchResult trigger failed — match was archived "
                "but medals/athlete-stats/bracket-propagation will NOT run until this "
                "is retried",
                exception=exc, doc_id=doc_id,
            )

    # ------------------------------------------------------------------ HTTP router

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/firebase_sync", tags=["FirebaseSync"])

        return router