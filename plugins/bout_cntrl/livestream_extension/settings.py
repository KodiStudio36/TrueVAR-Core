from pydantic import BaseModel, Field


class BoutCntrlSettings(BaseModel):
    # --- OBS integration ---
    obs_collection: str = Field(default="Boxing Manual",             description="OBS scene collection name")
    i3_workspace:   int = Field(default=2,                       description="i3 workspace for OBS window")

    # Scene names — configurable so no hardcoded strings live in code
    obs_scene_main:            str = Field(default="Main Scene",            description="Main fight view")

    # Transition names
    obs_transition_stinger: str = Field(default="TrueVAR Stinger", description="Hard cut stinger")
    obs_transition_move:    str = Field(default="Move",    description="Smooth move transition")

    # --- YouTube subscribe reminder ---
    youtube_subscribe_interval_mins: int = Field(
        default=10,
        description="How often (minutes) to show the subscribe widget during a broadcast",
    )