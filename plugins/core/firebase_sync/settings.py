from pydantic import BaseModel, Field


class FirebaseSyncSettings(BaseModel):
    """Configuration for the Firebase Realtime Database live-sync plugin."""

    enabled: bool = Field(
        True,
        description="Push stable scoreboard state to Firebase Realtime Database.",
    )