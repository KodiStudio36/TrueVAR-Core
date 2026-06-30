from abc import ABC, abstractmethod
from typing import List
from core.domain.entities import Tournament
from typing import Optional

class DatabasePort(ABC):
    @abstractmethod
    async def verify_license(self, license_key: str) -> bool:
        """Checks if the license key exists in the database."""
        pass

    @abstractmethod
    async def fetch_today_tournaments(self) -> List[Tournament]:
        """Fetches tournaments scheduled for the current day."""
        pass

    @abstractmethod                                          # ADD
    async def push_live_state(self, tournament_id: str, data: dict) -> None:
        """Writes the current match state to the live document in the DB."""
        pass

    @abstractmethod
    async def fetch_case(self, license_key: str) -> Optional[dict]:
        """Returns the full case document dict, or None if not found."""
        raise NotImplementedError

    @abstractmethod
    async def update_case_binding(self, license_key: str, tournament_id: str, court_id: int) -> None:
        """Writes tournamentId + courtId onto the case document."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_tournament_by_id(self, tournament_id: str) -> Optional[object]:
        """Fetches a single tournament by its Firestore document ID."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_courts_for_tournament(self, tournament_id: str) -> list[str]:
        """Returns a list of court ID strings for a tournament."""
        raise NotImplementedError
    
    @abstractmethod
    async def fetch_scheduled_broadcast(self, tournament_id: str, court_number: str) -> dict | None:
        """Returns the scheduled_broadcast doc for a given tournament + court, or None."""
        raise NotImplementedError