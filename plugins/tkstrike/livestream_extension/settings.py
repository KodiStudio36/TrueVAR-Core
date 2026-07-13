from pydantic import BaseModel, Field


class TaekwondoSettings(BaseModel):
    # --- OBS integration ---
    obs_collection: str = Field(default="Taekwondo",             description="OBS scene collection name")
    i3_workspace:   int = Field(default=2,                       description="i3 workspace for OBS window")

    # Scene names — configurable so no hardcoded strings live in code
    obs_scene_starting:        str = Field(default="Starting Scene",        description="Pre-tournament idle")
    obs_scene_main:            str = Field(default="Main Scene",            description="Main fight view")
    obs_scene_main_scoreboard: str = Field(default="Main Scene Scoreboard", description="Fight view with overlay")
    obs_scene_ivr:             str = Field(default="IVR Scene",             description="Full-screen replay")
    obs_scene_ivr_closeup:     str = Field(default="IVR Closeup Scene",     description="Close-up replay when mirrored")
    obs_scene_troubleshoot:    str = Field(default="Troubleshoot Scene",    description="Tech issue overlay")

    # Transition names
    obs_transition_stinger: str = Field(default="Stinger", description="Hard cut stinger")
    obs_transition_move:    str = Field(default="Move",    description="Smooth move transition")

    # --- YouTube subscribe reminder ---
    youtube_subscribe_interval_mins: int = Field(
        default=10,
        description="How often (minutes) to show the subscribe widget during a broadcast",
    )