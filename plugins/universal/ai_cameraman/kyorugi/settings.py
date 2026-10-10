from typing import Literal

from pydantic import Field

from plugins.universal.ai_cameraman.settings import AICameramanBaseSettings


class KyorugiCameramanSettings(AICameramanBaseSettings):
    """Kyorugi: frame the two fighters (white dobok + red/blue hogu); the referee
    (dark clothes) is ignored. Same behaviour as the original AI cameraman."""

    discipline: Literal["kyorugi"] = Field("kyorugi", description="Set by the plugin; do not change")

    athletes: int = Field(2, ge=1, description="Fighters to frame")
    kit_min: float = Field(
        0.2, ge=0.0, le=1.0,
        description="Min kit score (white dobok / red-blue hogu). Lower if fighters get missed",
    )
    include_referee: bool = Field(False, description="Frame the referee too (3 people)")
    athlete_size: float = Field(0.45, gt=0.0, le=1.0,
                                description="Max share of the picture height one fighter may fill")
    min_width_athletes: float = Field(3.5, gt=0.0,
                                      description="Picture is at least this many fighter-heights wide")