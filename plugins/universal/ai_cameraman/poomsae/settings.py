from typing import Literal

from pydantic import Field

from plugins.universal.ai_cameraman.settings import AICameramanBaseSettings


class PoomsaeCameramanSettings(AICameramanBaseSettings):
    """Poomsae: frame everyone standing on the (square) competition area —
    individual, pairs or teams. No kit test and no referee to ignore."""

    discipline: Literal["poomsae"] = Field("poomsae", description="Set by the plugin; do not change")

    max_people: int = Field(8, ge=1, description="Safety cap on people framed (team poomsae)")
    enter_s: float = Field(
        1.0, ge=0.0, le=5.0,
        description="Seconds someone must stand on the mat before the shot includes them "
                    "(people crossing a corner don't move the camera)",
    )
    hold_s: float = Field(
        1.5, ge=0.0, le=10.0,
        description="Keep someone framed this long after they are hidden or step just off",
    )
    foot_tolerance: float = Field(
        0.0, ge=0.0, le=0.5,
        description="How far feet may be outside the blue area, as a fraction of the person's own "
                    "height (0.1 ≈ 17 cm, the same near and far). 0 = feet must be on the blue",
    )

    # ---- camera style: detail first, hold still, re-frame fast (see autocam.PoomsaeCamera)
    max_zoom: float = Field(
        1.5, gt=1.0, le=1.5,
        description="Zoom it always holds. 1.5 = 1:1 pixels (1280x720 cut from 1920x1080); "
                    "more would upscale and soften the picture",
    )
    move_threshold: float = Field(
        0.15, ge=0.0, le=0.5,
        description="Camera holds still until the athletes drift this far from where it points "
                    "(fraction of the shot), then re-frames. Bigger = fewer moves",
    )
    move_time: float = Field(
        0.9, gt=0.05, le=5.0, description="How fast a re-frame is (seconds, smooth start/stop)"
    )
    edge_margin: float = Field(
        0.05, ge=0.0, le=0.3,
        description="Always re-frame before anyone gets this close to an edge (fraction of the shot)",
    )
    lookahead_s: float = Field(
        0.6, ge=0.0, le=2.0,
        description="Keep room for where the athletes are heading this far ahead (only while they move)",
    )
    zoom_in_delay_s: float = Field(
        2.5, ge=0.0, le=20.0, description="After a zoom-out, everyone must fit this long before zooming back in"
    )
    zoom_out_time: float = Field(0.5, gt=0.0, description="Seconds for a needed zoom-out")
    path_smoothing_s: float = Field(
        0.15, gt=0.0, le=0.6, description="Gaussian sigma for the camera path (rounds off move starts/stops)"
    )
    anticipation_s: float = Field(
        0.2, ge=0.0, le=1.0, description="Camera starts moving this early (uses the delay; removes reaction lag)"
    )