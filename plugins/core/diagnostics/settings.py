from pydantic import BaseModel, Field


class DiagnosticsSettings(BaseModel):
    enabled: bool = Field(default=True, description="Attach frame probes to the pipeline (can also be toggled live from the dashboard)")
    stall_ms: int = Field(default=500, description="A stage counts as stalled after this many ms without a frame")
    summary_interval_s: int = Field(default=10, description="Write a per-camera fps/gap summary line every N seconds (0 = off)")
    session_file: bool = Field(default=True, description="Write the whole dashboard timeline (UDP, IVR, pipeline, stalls) to a .jsonl file per session")
    log_dir: str = Field(default="logs/diagnostics", description="Folder for the session .jsonl files")