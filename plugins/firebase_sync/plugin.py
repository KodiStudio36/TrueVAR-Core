from typing import Optional

from fastapi import APIRouter

from plugins.base_plugin import BasePlugin
from plugins.firebase_sync.settings import FirebaseSyncSettings


class FirebaseSyncPlugin(BasePlugin):
    """
    Every scoreboard.stable_update is pushed to Firebase Realtime Database
    via DatabasePort.push_live_state() — unchanged from before.

    Additionally, this plugin tracks the match id ("id" field) carried in
    each stable_update. When the id changes, the previous match has just
    ended, so its last known stable state is archived to Firestore at
        sports/{sport_id}/disciplines/{discipline_id}/matches/{match_id}
    BEFORE the new update is pushed to RTDB.
    """

    def __init__(self, core_context: dict, logger):
        super().__init__(core_context, logger)
        self._settings: Optional[FirebaseSyncSettings] = None
        self._subscriptions: list[tuple] = []

        self._tournament = None
        self._tournament_id: Optional[str] = None
        self._sport_id: Optional[str] = None
        self._discipline_id: Optional[str] = None

        self._last_match_id: Optional[str] = None
        self._last_match_state: Optional[dict] = None

        self._push_count = 0
        self._history_count = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return "firebase_sync"

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

        if not self._settings.enabled:
            self.logger.info("FirebaseSync disabled via settings — plugin idle.")
            return

        bus = self.core_context.get("event_bus")
        if bus:
            self._subscriptions = [("scoreboard.stable_update", self._on_stable_update)]
            bus.subscribe_many(self._subscriptions)

        self.logger.info(
            "FirebaseSync started",
            tournament_id=self._tournament_id,
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

        match_id = data.get("id")

        # Match id changed → the previous match just ended. Archive its
        # last known state to Firestore before pushing the new one to RTDB.
        if (
            match_id
            and self._last_match_id
            and match_id != self._last_match_id
            and self._last_match_state is not None
        ):
            await self._archive_match(self._last_match_id, self._last_match_state)

        self._last_match_id = match_id
        self._last_match_state = data

        try:
            await self.core_context["db"].push_live_state(self._tournament_id, data)
            self._push_count += 1
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("FirebaseSync: push_live_state failed", exception=exc)

    async def _archive_match(self, match_id: str, state: dict) -> None:
        if not self._sport_id or not self._discipline_id:
            self.logger.warning(
                "FirebaseSync: no sport/discipline context — skipping history save."
            )
            return

        payload = dict(state)
        payload["tournamentId"] = self._tournament_id
        payload["tournamentName"] = getattr(self._tournament, "title", None)
        payload.setdefault("athletes", [])  # fill in / restructure as you finalize the schema

        try:
            await self.core_context["db"].save_match_state(
                self._sport_id, self._discipline_id, match_id, payload
            )
            self._history_count += 1
        except Exception as exc:
            self._last_error = str(exc)
            self.logger.error("FirebaseSync: save_match_state (history) failed", exception=exc)

    # ------------------------------------------------------------------ HTTP router

    def get_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/firebase_sync", tags=["FirebaseSync"])

        @router.get("/status")
        async def status():
            return {
                "enabled": bool(self._settings and self._settings.enabled),
                "tournament_id": self._tournament_id,
                "sport_id": self._sport_id,
                "discipline_id": self._discipline_id,
                "last_match_id": self._last_match_id,
                "push_count": self._push_count,
                "history_count": self._history_count,
                "last_error": self._last_error,
            }

        return router