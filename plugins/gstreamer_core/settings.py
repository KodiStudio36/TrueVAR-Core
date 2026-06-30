from pydantic import BaseModel, Field
from typing import List

class CameraSettings(BaseModel):
    is_scoreboard: bool = Field(default=True, description="Enable the USB Scoreboard camera")
    scoreboard_device: int = Field(default=0, description="Video device index (/dev/videoX)")
    debug: bool = Field(default=False, description="Use videotestsrc instead of real cameras")
    network_ip: str = Field(default="192.168.0.1/24", description="IP Network")
    camera_ips: List[str] = Field(
        default=["192.168.0.11", "192.168.0.12", "192.168.0.13"], 
        description="List of specific IP addresses for the cameras"
    )
    enable_external_screen: bool = Field(default=False, description="Enable xvimagesink branch")
    shm_base_dir: str = Field(default="/tmp", description="Directory for shared memory sockets")