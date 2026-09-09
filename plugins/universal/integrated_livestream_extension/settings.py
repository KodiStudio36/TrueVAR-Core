from pydantic import BaseModel, Field


class UniversalIntegratedLivestreamSettings(BaseModel):
    # --- OBS integration ---
    obs_collection: str = Field(default="Universal Integrated",       description="OBS scene collection name")
    i3_workspace:   int = Field(default=2,                       description="i3 workspace for OBS window")

    # Scene names — configurable so no hardcoded strings live in code
    obs_scene_main:            str = Field(default="Main Scene",            description="Main fight view")