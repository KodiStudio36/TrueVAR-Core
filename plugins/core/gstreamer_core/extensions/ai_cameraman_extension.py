from plugins.core.gstreamer_core.base_extension import BaseGStreamerExtension


def _has_element(name: str) -> bool:
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        if not Gst.is_initialized():
            Gst.init(None)
        return Gst.ElementFactory.find(name) is not None
    except Exception:
        return False


class AICameramanExtension(BaseGStreamerExtension):
    """Hands the decoded native-resolution (1080p) stream of one IP camera to the
    AI cameraman renderer process over shared memory.

    decoder="cpu" (default): that camera is decoded by avdec_h264 into normal memory
        raw_srcN. ! queue (leaky) ! videoconvert (I420 -> NV12) ! queue (leaky) ! shmsink
      No GPU -> CPU read-back: on Intel iGPUs reading 1080p out of VA memory is
      slow enough to lose ~10 % of the frames (a visible stutter).
    decoder="gpu": vah264dec, then
        raw_srcN. ! queue (leaky, 3) ! vapostproc (VA -> 1080p NV12 sysmem) ! queue ! shmsink

    Pure GStreamer/C: no Python runs per frame in the main app. The branch is
    fully leaky, so a slow, missing or restarting renderer can never
    back-pressure the camera's normal 720p/shm path.
    """

    def __init__(self, raw_socket: str, source_index: int = 1, width: int = 1920, height: int = 1080,
                 replace_output: bool = False, decoder: str = "cpu"):
        self.raw_socket = raw_socket
        self.source_index = source_index
        self.width = width
        self.height = height
        self.replace_output = replace_output
        self.requested_decoder = decoder  # "cpu" (avdec_h264, default) | "gpu" (vah264dec)
        # CPU decoding needs gst-libav; without it fall back to the GPU path rather than
        # breaking the whole core pipeline.
        self.decoder = decoder if decoder != "cpu" or _has_element("avdec_h264") else "gpu"

    @property
    def name(self) -> str:
        return "ai_cameraman"

    def get_branches(self, settings) -> dict[int, str]:
        return {}

    def get_decoders(self, settings) -> dict[int, str]:
        return {self.source_index: "avdec_h264"} if self.decoder == "cpu" else {}

    def get_raw_branches(self, settings) -> dict[int, str]:
        frame = self.width * self.height * 3 // 2
        sink = (f"shmsink socket-path={self.raw_socket} wait-for-connection=false "
                f"shm-size={frame * 32} sync=false async=false")
        if self.decoder == "cpu":
            # Frames are already in normal memory: only I420 -> NV12 (and a resize if the
            # camera is not 1080p). 16 frames of slack ride out keyframe stalls / bursts.
            return {
                self.source_index: (
                    "queue max-size-buffers=16 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                    "videoconvert ! videoscale ! "
                    f"video/x-raw,format=NV12,width={self.width},height={self.height} ! "
                    "queue max-size-buffers=16 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                    f"{sink}"
                )
            }
        return {
            self.source_index: (
                # 3 VA surfaces + 8 system-memory frames of slack: a keyframe arrives late
                # and the next frames burst in right behind it; a 1-frame queue dropped
                # one frame per keyframe (= a visible skip every second with a 1 s GOP).
                "queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                "vapostproc ! "
                f"video/x-raw,format=NV12,width={self.width},height={self.height} ! "
                "queue max-size-buffers=16 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                f"{sink}"
            )
        }

    def get_replaced_outputs(self, settings) -> set[int]:
        return {self.source_index} if self.replace_output else set()