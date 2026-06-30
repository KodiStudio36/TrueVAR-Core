from pydantic import BaseModel

class ScoreUpdatedEvent(BaseModel):
    red_score: int
    blue_score: int
    red_penalties: int
    blue_penalties: int
    round_number: int
    time_remaining: str  # e.g., "01:30"

class MatchStatusEvent(BaseModel):
    status: str  # "START", "PAUSE", "END", "IVR_REQUESTED"
    winner: str = None