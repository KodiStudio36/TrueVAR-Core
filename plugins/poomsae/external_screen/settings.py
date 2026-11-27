from pydantic import BaseModel, Field


class PoomsaeExternalScreenSettings(BaseModel):
    main_display:     str   = Field(default="DP-1",   description="Primary display name (xrandr)")
    external_display: str   = Field(default="HDMI-1", description="External display name (xrandr)")
    target_workspace: int   = Field(default=6,        description="i3 workspace number on the external display")
    screen_url:       str   = Field(default="auto",
                                    description="\"auto\" = this server's own address + /api/poomsae-control/screen, or a full URL")
    browser_command:  str   = Field(default="auto", description="Chromium-based browser command, or \"auto\" to detect (chromium, google-chrome, brave, edge…)")
    profile_dir:      str   = Field(default="/tmp/truevar-poomsae-screen",
                                    description="Separate browser profile, so the screen is its own process we can close")
    window_title:     str   = Field(default="Poomsae Elimination — Screen",
                                    description="X11 window title (the page's <title>) used to move the window with i3")
    window_delay:     float = Field(default=3.0, description="Seconds to wait for the browser window before moving it")