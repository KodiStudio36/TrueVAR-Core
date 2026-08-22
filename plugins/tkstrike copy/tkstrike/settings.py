from pydantic import BaseModel, Field

class TkStrikeSettings(BaseModel):
    wine_prefix:        str   = Field(default="/home/kodi/Documents/programs/var/TKStrike/pfx")
    app_dir:            str   = Field(default="/home/kodi/Documents/programs/var/TKStrike/pfx/drive_c/users/kodi/AppData/Local/tkStrikeGen2")
    exe_name:           str   = Field(default="tkStrikeGen2.exe")
    window_class:       str   = Field(default="tkstrikegen2.exe")
    workspace:          int   = Field(default=2)
    ivr_workspace:      int   = Field(default=3)
    startup_delay:      float = Field(default=6.0) 

    replay_start_event: str   = Field(default="scoreboard.START_FIGHT")
    replay_stop_event:  str   = Field(default="ivr.replay_closed")