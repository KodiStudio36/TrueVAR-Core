from pydantic import BaseModel, Field

class LivestreamCoreSettings(BaseModel):
    obs_host: str = Field(default="localhost", description="OBS WebSocket Host")
    obs_port: int = Field(default=4455, description="OBS WebSocket Port")
    obs_password: str = Field(default="samko211", description="OBS WebSocket Password")
    enable_overlays: bool = Field(default=True, description="Enable the web overlay endpoints")