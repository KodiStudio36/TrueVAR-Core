from pydantic import BaseModel, Field


class KeyBindSettings(BaseModel):
    settings_key:               str = Field(default="S",           description="Open/close settings screen")
    replay_key:                 str = Field(default="R",           description="Open/close replay screen")
    record_key:                 str = Field(default="N",           description="Start/stop recording")
    next_camera_key:            str = Field(default="E",           description="Cycle camera angle in replay")
    play_pause_key:             str = Field(default=" ",           description="Play / pause in replay")
    frame_forward_key:          str = Field(default="Right",       description="Step one frame forward")
    frame_backward_key:         str = Field(default="Left",        description="Step one frame back")
    second_forward_key:         str = Field(default="Shift+Right", description="Jump 1 second forward")
    second_backward_key:        str = Field(default="Shift+Left",  description="Jump 1 second back")
    reset_zoom_key:             str = Field(default="Escape",      description="Reset video zoom")
    toggle_external_screen_key: str = Field(default="W",           description="Toggle external screen")


class IVRSettings(BaseModel):
    ws_host:        str  = Field(default="localhost",          description="IVR WebSocket host")
    ws_port:        int  = Field(default=8765,                 description="IVR WebSocket port")
    app_path:       str = Field(default="/home/kodi/Documents/programs/var/TrueVAR-IVR/main.py", description="Path to external application")
    window_class:   str = Field(default="truevar_ivr",                      description="WM_CLASS name for the window manager")
    workspace:      int = Field(default=3,                              description="Target i3 workspace")
    fps:            int  = Field(default=30,                   description="Recording frame rate")
    res_height:     int  = Field(default=720,                  description="Vertical resolution (width derived at 16:9)")
    camera_count:   int  = Field(default=1,                    description="Number of recording cameras")
    delete_records: bool = Field(default=True,                 description="Wipe old recordings on new session start")
    auto_record:    bool = Field(default=False,                description="Start recording automatically on launch")
    key_binds: KeyBindSettings = Field(
        default_factory=KeyBindSettings,
        description="IVR keyboard shortcuts",
    )