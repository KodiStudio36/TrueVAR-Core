from pydantic import BaseModel, Field

class TkStrikeListenerSettings(BaseModel):
    bind_host: str = Field(default="0.0.0.0", description="UDP bind address")
    bind_port: int = Field(default=9998,      description="Daedo UDP port")