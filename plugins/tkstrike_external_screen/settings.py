from pydantic import BaseModel, Field


class TkStrikeExternalScreenSettings(BaseModel):
    main_display:     str = Field(default="DP-1",   description="Primary display name (xrandr)")
    external_display: str = Field(default="HDMI-1", description="External display name (xrandr)")
    target_workspace: int = Field(default=6,        description="i3 workspace number for the screen window")
    window_title:     str = Field(default="python", description="X11 window title of the xvimagesink window")
    window_delay:     float = Field(default=1.5,    description="Seconds to wait for window creation before moving it")