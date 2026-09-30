import asyncio
from datetime import datetime, time, timezone
import firebase_admin
# Import the Realtime Database module ('db') alongside firestore
from firebase_admin import credentials, firestore, db
from google.cloud.firestore_v1.base_query import FieldFilter
from core.domain.ports.database_port import DatabasePort
from core.domain.entities import Tournament

class FirebaseAdapter(DatabasePort):
    def __init__(self, 
                 service_account_path: str = "config/google_creds.json", 
                 database_url: str = "https://easytkd-default-rtdb.europe-west1.firebasedatabase.app/"): # <-- Add your RTDB URL here
        
        # Prevent re-initializing if hot-reloading
        if not firebase_admin._apps:
            cred = credentials.Certificate(service_account_path)
            # CRITICAL: Realtime Database requires the databaseURL options dictionary
            firebase_admin.initialize_app(cred, {
                'databaseURL': database_url
            })
            
        # Initialize both clients so you don't break your legacy tournament/license fetching
        self.firestore_db = firestore.client()

    async def verify_license(self, license_key: str) -> bool:
        if not license_key:
            return False
            
        def _verify():
            doc_ref = self.firestore_db.collection('cases').document(license_key)
            return doc_ref.get().exists
            
        return await asyncio.to_thread(_verify)

    async def fetch_today_tournaments(self) -> list[Tournament]:
        def _fetch():
            now = datetime.now(timezone.utc)
            start_of_day = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
            # end_of_day = datetime.combine(now.date(), time.max, tzinfo=timezone.utc)
            
            query = self.firestore_db.collection('tournaments')\
                           .where(filter=FieldFilter('dateTime', '>=', start_of_day))#\
                        #    .where(filter=FieldFilter('dateTime', '<=', end_of_day))
            
            tournaments = []
            for doc in query.stream():
                data = doc.to_dict()
                data['id'] = doc.id
                tournaments.append(Tournament(**data))
            return tournaments
            
        return await asyncio.to_thread(_fetch)
    
    async def push_live_state(self, tournament_id: str, court_id: str, data: dict) -> None:
        """
        Pushes a direct live scoring update to Firebase Realtime Database.
        Saves data to the path: /tournaments/{tournament_id}/courts/{court_id}/match_data
        """
        def _push():
            # Create a reference pointing to your nested JSON path in the RTDB tree
            ref = db.reference(f'tournaments/{tournament_id}/courts/{court_id}/match_data')
            # .set() replaces the data at this path completely, matching your firestore logic
            ref.set(data)
            
        await asyncio.to_thread(_push)

    # Append these methods to FirebaseAdapter (after push_live_state)
    async def fetch_case(self, license_key: str) -> dict | None:
        if not license_key:
            return None
        def _fetch():
            doc = self.firestore_db.collection('cases').document(license_key).get()
            if not doc.exists:
                return None
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return await asyncio.to_thread(_fetch)

    async def update_case_binding(self, license_key: str, tournament_id: str, court_id: int) -> None:
        def _update():
            self.firestore_db.collection('cases').document(license_key).update({
                'tournamentId': tournament_id,
                'courtId': court_id,
            })
        await asyncio.to_thread(_update)

    async def clear_case_binding(self, license_key: str) -> None:
        """
        Removes tournamentId/courtId from a case doc. Used when the bound
        tournament no longer exists or is stale, so the case falls back to
        the normal tournament-selection flow on next preflight.
        """
        def _clear():
            self.firestore_db.collection('cases').document(license_key).update({
                'tournamentId': firestore.DELETE_FIELD,
                'courtId': firestore.DELETE_FIELD,
            })
        await asyncio.to_thread(_clear)

    async def fetch_tournament_by_id(self, tournament_id: str) -> Tournament | None:
        def _fetch():
            doc = self.firestore_db.collection('tournaments').document(tournament_id).get()
            if not doc.exists:
                return None
            data = doc.to_dict()
            data['id'] = doc.id
            return Tournament(**data)
        return await asyncio.to_thread(_fetch)

    async def fetch_courts_for_tournament(self, tournament_id: str) -> list[str]:
        """
        Uses an explicit 'courts' array on the tournament doc if present,
        otherwise derives ["1", "2", ..., N] from the 'courtNum' field.
        """
        def _fetch():
            doc = self.firestore_db.collection('tournaments').document(tournament_id).get()
            if not doc.exists:
                return ["1"]
            data = doc.to_dict()
            if data.get('courts'):
                return [str(c) for c in data['courts']]
            court_num = int(data.get('courtNum', 1))
            return [str(i) for i in range(1, court_num + 1)]
        return await asyncio.to_thread(_fetch)
    
    async def fetch_scheduled_broadcast(self, tournament_id: str, court_number: str) -> dict | None:
        def _fetch():
            docs = list(
                self.firestore_db.collection("scheduled_broadcasts")
                .where(filter=FieldFilter("tournament_id", "==", tournament_id))
                .where(filter=FieldFilter("court_number", "==", court_number))
                .limit(1)
                .stream()
            )
            if not docs:
                return None
            data = docs[0].to_dict()
            data["id"] = docs[0].id
            return data
        return await asyncio.to_thread(_fetch)
    
    async def save_match_state(
        self, sport_id: str, discipline_id: str, match_id: str, data: dict
    ) -> None:
        """
        Archives/updates a match's result at:
            sports/{sport_id}/disciplines/{discipline_id}/matches/{match_id}

        match_id here is expected to ALREADY be the full, tournament-scoped
        id (e.g. "{tournamentId}_{101}") — see FirebaseSyncPlugin._archive_match,
        which composes it that way before calling this. Using it directly
        as the document id (not a fresh auto-id) is what makes this a
        MERGE onto whatever the web scheduler already pre-created for
        this exact fight, instead of an orphaned duplicate — see
        commit_brackets_endpoint on the web app side for the other half
        of that same id composition.

        merge=True is required, not optional: the target document
        already carries the scheduler's pre-fight fields (blue/red
        corners, courtId, categoryCode, ...). A plain .set() here would
        silently erase all of that.

        `end_time` is stamped with datetime.now() at the moment of the
        actual write, so it reflects when the match was archived.
        """
        def _save():
            payload = dict(data)
            payload["end_time"] = datetime.now()

            ref = (
                self.firestore_db.collection('sports').document(sport_id)
                .collection('disciplines').document(discipline_id)
                .collection('matches').document(match_id)
            )
            ref.set(payload, merge=True)

        await asyncio.to_thread(_save)

    async def fetch_match_state(
        self, sport_id: str, discipline_id: str, match_id: str
    ) -> dict | None:
        """
        Reads a single archived match doc at:
            sports/{sport_id}/disciplines/{discipline_id}/matches/{match_id}

        match_id is expected to already be the full, tournament-scoped id
        (same convention as save_match_state — e.g. "{tournamentId}_{101}").

        Used by FirebaseSyncPlugin._archive_match to check, before writing,
        whether this match already has bracket/roster-sourced identity data
        (blue/red.entryId) — that's the signal it uses to decide whether to
        merge just the live-scoring result on top, or do a full write. A
        plain read (not a watch) is intentional here: this only needs to
        run once per WIN event, not stay subscribed.

        Returns None if no doc exists yet at that id — a completely normal
        outcome for a guest/analytics-only fight the bracket system never
        pre-created, not an error.
        """
        def _fetch():
            doc = (
                self.firestore_db.collection('sports').document(sport_id)
                .collection('disciplines').document(discipline_id)
                .collection('matches').document(match_id)
                .get()
            )
            if not doc.exists:
                return None
            data = doc.to_dict()
            data['id'] = doc.id
            return data

        return await asyncio.to_thread(_fetch)

    async def fetch_previous_fights(
        self, sport_id: str, discipline_id: str, tournament_id: str, athlete_name: str, limit: int = 5
    ) -> list[dict]:
        def _fetch():
            query = (
                self.firestore_db.collection('sports').document(sport_id)
                .collection('disciplines').document(discipline_id)
                .collection('matches')
                .where(filter=FieldFilter('tournamentId', '==', tournament_id))
                .where(filter=FieldFilter('athletes', 'array_contains', athlete_name))
                # .order_by('end_time', direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            results = []
            for doc in query.stream():
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            return results

        return await asyncio.to_thread(_fetch)

    async def fetch_next_matches_for_court(
        self, sport_id: str, discipline_id: str, tournament_id: str, court_id: str, limit: int = 3
    ) -> list[dict]:
        """
        One-shot fetch of matches scheduled onto THIS court for THIS
        tournament, ready to fight (status "ready"), ordered by their
        position in the queue — exactly the documents
        commit_brackets_endpoint writes from the web app's bracket
        scheduler, blue/red corners already denormalized with name/
        country (see BracketCommitMatch), so no athlete join is needed
        here or by any caller.

        TkStrikeServerPlugin does NOT use this anymore — a one-time read
        can't tell you when the answer changes without being re-polled,
        which is exactly the "hammering Firestore every few seconds"
        problem that plugin used to have. See watch_next_matches_for_court
        below for the real-time version it actually uses. This one-shot
        version is kept for callers that genuinely just want a single
        current snapshot (e.g. an on-demand dashboard refresh).

        NOTE: combining the tournamentId/courtId/status equality filters
        with order_by(queuePosition) needs a composite index — Firestore
        returns a direct link to create it the first time this runs
        without one (see also firestore.indexes.json in this repo, which
        should already define it for a deployed environment instead of
        relying on someone clicking that link manually).
        """
        def _fetch():
            query = (
                self.firestore_db.collection('sports').document(sport_id)
                .collection('disciplines').document(discipline_id)
                .collection('matches')
                .where(filter=FieldFilter('tournamentId', '==', tournament_id))
                .where(filter=FieldFilter('courtId', '==', court_id))
                .where(filter=FieldFilter('status', '==', 'ready'))
                .order_by('queuePosition')
                .limit(limit)
            )
            results = []
            for doc in query.stream():
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            return results

        return await asyncio.to_thread(_fetch)

    def watch_next_matches_for_court(
        self, sport_id: str, discipline_id: str, tournament_id: str, court_id: str, callback, limit: int = 3
    ):
        """
        Real-time equivalent of fetch_next_matches_for_court — instead of
        being polled, calls `callback(matches: list[dict])` every time the
        underlying query's RESULTS actually change, via Firestore's own
        watch mechanism (one persistent gRPC stream, not repeated reads).
        This is what TkStrikeServerPlugin actually subscribes to; polling
        every few seconds was both wasteful (a read every poll whether or
        not anything changed) and, worse, meant a single missing-index
        error got retried and re-logged on every single poll forever
        instead of surfacing once.

        Same query/index shape as fetch_next_matches_for_court above —
        see its docstring for the composite index this needs.

        Firestore's on_snapshot callback fires on a watch thread it
        manages internally, NOT the asyncio event loop thread, so
        `callback` (an async function) is dispatched back onto the loop
        via run_coroutine_threadsafe rather than called directly — calling
        asyncio APIs straight from a non-loop thread is not safe.

        Returns the Watch object; call .unsubscribe() on it (typically in
        the plugin's stop()) to stop listening.
        """
        loop = asyncio.get_event_loop()

        def _on_snapshot(col_snapshot, changes, read_time):
            results = []
            for doc in col_snapshot:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(data)
            results.sort(key=lambda d: d.get('queuePosition', 0))
            asyncio.run_coroutine_threadsafe(callback(results), loop)

        query = (
            self.firestore_db.collection('sports').document(sport_id)
            .collection('disciplines').document(discipline_id)
            .collection('matches')
            .where(filter=FieldFilter('tournamentId', '==', tournament_id))
            .where(filter=FieldFilter('courtId', '==', str(court_id)))
            .where(filter=FieldFilter('status', '==', 'ready'))
            .order_by('queuePosition')
            .limit(limit)
        )
        return query.on_snapshot(_on_snapshot)

    async def fetch_athletes_by_ids(self, athlete_ids: list[str]) -> dict[str, dict]:
        """
        Batch-resolves athlete ids into display data (name, country).
        Not on TkStrikeServerPlugin's hot path anymore — match docs now
        carry blue/red corners with name/country already denormalized
        onto them at scheduling time (see BracketCommitMatch), so there's
        nothing left to join for that use case. Kept as a general-purpose
        utility for anything else that only has athleteIds and needs the
        rest. Uses get_all() (one round trip for the whole batch) instead
        of looping individual .get() calls.
        """
        if not athlete_ids:
            return {}

        def _fetch():
            refs = [self.firestore_db.collection('athletes').document(aid) for aid in athlete_ids]
            result = {}
            for snap in self.firestore_db.get_all(refs):
                if snap.exists:
                    result[snap.id] = snap.to_dict()
            return result

        return await asyncio.to_thread(_fetch)

     
    # ── ADD THESE TWO METHODS TO YOUR EXISTING FirebaseAdapter CLASS ───────────
    # Place them right after push_live_state — same RTDB path, same pattern.
    # Everything else in FirebaseAdapter is untouched; this is purely additive.

    async def update_live_state(self, tournament_id: str, court_id: str, data: dict) -> None:
        """
        Partial update (RTDB .update(), NOT .set()) of the SAME
        /tournaments/{tournament_id}/courts/{court_id}/match_data path
        push_live_state writes to.

        push_live_state's plain .set() REPLACES the whole node — correct
        when a brand new match is selected (any previous match's data at
        that path genuinely shouldn't persist), but wrong for anything
        smaller: a future judge-scoring app is expected to write scores
        into this same match_data tree (e.g. under corners/blue/judges),
        and a .set() from the operator panel at the wrong moment would
        silently erase that. Use update_live_state for any change that
        should touch ONLY the keys present in `data` — stage transitions,
        timer resets, etc. — and reserve push_live_state for "this is
        genuinely a new match, wipe the slate."

        Note RTDB .update() is a SHALLOW merge: it only protects sibling
        top-level keys not mentioned in `data`. If `data` itself contains
        a nested dict under one of its keys, that nested dict is still
        replaced wholesale, not deep-merged. Fine for this plugin's own
        writes (it only ever touches top-level stage/timer keys), just
        worth knowing before reusing this for anything nested.
        """
        def _update():
            ref = db.reference(f'tournaments/{tournament_id}/courts/{court_id}/match_data')
            ref.update(data)
        await asyncio.to_thread(_update)

    async def fetch_live_state(self, tournament_id: str, court_id: str) -> dict | None:
        """
        One-shot read of the SAME match_data path push_live_state /
        update_live_state write to. Used to check what a scoring app may
        have written there (e.g. judge scores) since this plugin last
        pushed, without holding open a persistent RTDB listener — a
        plain poll (see PoomsaeControlPlugin._poll_loop) is enough for
        "has every judge submitted yet", which doesn't need sub-second
        latency.
        """
        def _fetch():
            ref = db.reference(f'tournaments/{tournament_id}/courts/{court_id}/match_data')
            return ref.get()
        return await asyncio.to_thread(_fetch)

    async def fetch_judges_registry(self, tournament_id: str, court_id: str) -> dict | None:
        """
        One-shot read of /tournaments/{tournament_id}/courts/{court_id}/
        judges — a SEPARATE node from match_data, deliberately, because
        it needs to survive match_data being wholesale-replaced every
        time the operator selects a new match (see push_live_state's own
        docstring: a new match is a genuine clean slate for match_data).
        Judge identity/numbering ("Judge 1", "Judge 2", ...) is a
        COURT-level, cross-match concept — the same three judges keep
        the same numbers all day on that court, not just for one match.

        Written by the judge app itself (via an RTDB transaction on this
        same path, so two judges connecting at the same instant can't
        race each other into the same number — see judge_app's
        FirebaseJudgeRegistryRepository), never by this plugin. This
        method only ever reads it, to resolve a judgeId into the number
        the operator panel displays.

        Shape: { judgeId: { number: int, connectedAt: <server timestamp> } }
        """
        def _fetch():
            ref = db.reference(f'tournaments/{tournament_id}/courts/{court_id}/judges')
            return ref.get()
        return await asyncio.to_thread(_fetch)

    async def create_judge_slot(self, tournament_id: str, court_id: str, number: int) -> None:
        """
        Creates/overwrites one judge slot at
        /tournaments/{tournament_id}/courts/{court_id}/judges/{number} —
        `number` IS the key (stringified), not a separate UUID. This
        replaces an earlier design where the JUDGE APP itself minted a
        UUID and registered it via an RTDB transaction; now the OPERATOR
        explicitly creates each numbered slot via the "+ Add Judge"
        button (see plugin.py's judge slot endpoints), and the resulting
        QR code carries that number directly — so there's no longer any
        concurrent "who gets to be Judge 1" race to guard against with a
        transaction. Whichever plugin-side lock serializes concurrent
        "+" clicks (see PoomsaeControlPlugin._judges_lock) is enough.
        """
        def _set():
            # firebase_admin's PYTHON Admin SDK has no `ServerValue`
            # class — that's a JS/web SDK convention that doesn't carry
            # over. The Python SDK's actual mechanism for "let the
            # server fill in its own timestamp" is this raw special-
            # value dict, `{'.sv': 'timestamp'}`, passed as the field's
            # value directly. Using db.ServerValue.TIMESTAMP (which
            # doesn't exist) raised AttributeError at runtime here.
            ref = db.reference(f'tournaments/{tournament_id}/courts/{court_id}/judges/{number}')
            ref.set({'createdAt': {'.sv': 'timestamp'}})
        await asyncio.to_thread(_set)

    async def delete_judge_slot(self, tournament_id: str, court_id: str, number: int) -> None:
        def _delete():
            ref = db.reference(f'tournaments/{tournament_id}/courts/{court_id}/judges/{number}')
            ref.delete()
        await asyncio.to_thread(_delete)