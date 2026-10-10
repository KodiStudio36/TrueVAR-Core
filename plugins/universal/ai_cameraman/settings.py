"""Settings shared by both AI cameraman plugins (kyorugi / poomsae).

Each discipline subclasses AICameramanBaseSettings (see kyorugi/settings.py and
poomsae/settings.py) and adds its own selection fields + tuned defaults.
The child processes rebuild the right class from JSON via settings_from_json().
"""
import json
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class AICameramanBaseSettings(BaseModel):
    discipline: str = Field("kyorugi", description="Set by the plugin; do not change")

    # ---- routing
    enabled: bool = Field(True, description="Start the AI cameraman when the plugin starts")
    source_index: int = Field(1, ge=1, description="IP camera to frame (1 = first RTSP camera)")
    input_width: int = Field(1920, description="Native camera width tapped before the 720p scale")
    input_height: int = Field(1080, description="Native camera height")
    output_width: int = Field(1280, description="Framed output width")
    output_height: int = Field(720, description="Framed output height")
    output_fps: int = Field(30, description="Nominal framerate in the output caps")
    replace_camera_output: bool = Field(
        True, description="Publish the AI picture on camera<N>_shm_socket instead of the raw camera "
                          "(OBS/IVR sources stay the same). Off = separate camera_ai_shm_socket",
    )
    output_socket: str = Field(
        "", description="Override the output socket path (only when replace_camera_output is off)"
    )

    # ---- broadcast smoothness (anti-flicker)
    output_delay_s: float = Field(
        1.0, ge=0.3, le=3.0,
        description="Output runs this far behind live. Buys look-ahead smoothing + a jitter buffer.",
    )
    path_smoothing_s: float = Field(
        0.15, gt=0.0, le=0.6, description="Gaussian sigma for the camera path (higher = silkier moves)"
    )
    anticipation_s: float = Field(
        0.3, ge=0.0, le=1.0, description="Camera starts moving this early (uses the delay; removes reaction lag)"
    )
    antialias: bool = Field(True, description="Low-pass before downscaling so fine detail never twinkles")

    # ---- detector (CPU only — the GPU is never used for inference)
    model_path: str = Field("yolov8n.pt", description="YOLO weights (.pt is exported to OpenVINO once)")
    backend: Literal["openvino", "pytorch"] = Field("openvino", description="openvino = fast & cool on CPU")
    fp16: bool = Field(True, description="FP16 weights in the exported model (smaller; CPU computes in FP32)")
    imgsz: int = Field(640, description="Detector input WIDTH; height follows the 16:9 frame (640 -> 640x384)")
    conf: float = Field(0.35, ge=0.0, le=1.0)
    analysis_width: int = Field(960, description="Frame size the AI looks at (cheap 2x downscale from 1080p)")
    analysis_height: int = Field(540)
    detect_fps: float = Field(10.0, gt=0, le=30, description="Detections per second. Biggest thermal knob.")

    # ---- framing (same meaning as the tkd_autocam.py CLI flags)
    max_zoom: float = Field(1.6, gt=1.0)
    zoom_out_time: float = Field(0.35, gt=0.0)
    smooth: float = Field(1.0, gt=0.0, description="2 = twice as calm, 0.5 = snappier")
    mat_color: Literal["auto", "blue", "red", "yellow", "green"] = Field(
        "auto",
        description="Colour of the competition area. auto = the biggest solid coloured area on the floor "
                    "(works for any colour, square or octagon). Force a colour only if auto picks wrong",
    )
    mat_polygon: Optional[List[List[int]]] = Field(
        None, description="Mat corners in INPUT (1080p) pixels, e.g. from tkd_autocam.py --select-mat. Empty = auto"
    )

    # ---- thermal / CPU budget
    cpu_cores: str = Field(
        "auto", description="CPU list for the AI threads, e.g. '12-15'. auto = E-cores on Intel hybrid CPUs, else none"
    )
    cpu_threads: int = Field(2, ge=1, description="Inference threads (2 = ~2 cores busy at most)")
    thermal_hot_c: float = Field(85.0, description="Package temp at which detection rate is halved")
    thermal_cool_c: float = Field(75.0, description="Package temp below which detection rate is restored")


def settings_from_json(text: str) -> AICameramanBaseSettings:
    """Rebuild the discipline's own settings class (used by the child processes)."""
    data = json.loads(text)
    if data.get("discipline") == "poomsae":
        from plugins.universal.ai_cameraman.poomsae.settings import PoomsaeCameramanSettings as cls
    else:
        from plugins.universal.ai_cameraman.kyorugi.settings import KyorugiCameramanSettings as cls
    return cls(**data)