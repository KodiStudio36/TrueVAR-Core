from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional

class Tournament(BaseModel):
    id: str
    title: str
    location: str
    courtNum: int
    dateTime: datetime
    settings: dict
    discipline: str

class SystemStatus(BaseModel):
    network_alive: bool = False
    is_licensed: bool = False
    active_tournaments: list[Tournament] = []
    is_offline_mode: bool = False
    active_tournament_id: Optional[str] = None