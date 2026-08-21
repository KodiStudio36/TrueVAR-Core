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