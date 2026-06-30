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
    
    async def push_live_state(self, tournament_id: str, data: dict) -> None:
        """
        Pushes a direct live scoring update to Firebase Realtime Database.
        Saves data to the path: /tournaments/{tournament_id}/live/state
        """
        def _push():
            # Create a reference pointing to your nested JSON path in the RTDB tree
            ref = db.reference(f'tournaments/{tournament_id}/live/state')
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