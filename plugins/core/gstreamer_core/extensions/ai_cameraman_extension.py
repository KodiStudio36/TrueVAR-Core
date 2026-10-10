from plugins.core.gstreamer_core.base_extension import BaseGStreamerExtension


class AICameramanExtension(BaseGStreamerExtension):
    """Hands the decoded native-resolution (1080p) stream of one IP camera to the
    AI cameraman renderer process over shared memory.

        raw_srcN. ! queue (leaky, 1 buf) ! vapostproc (VA: VAMemory -> 1080p NV12 sysmem)
                  ! shmsink  <raw_socket>

    Pure GStreamer/C: no Python runs per frame in the main app. The branch is
    fully leaky, so a slow, missing or restarting renderer can never
    back-pressure the camera's normal 720p/shm path.
    """

    def __init__(self, raw_socket: str, source_index: int = 1, width: int = 1920, height: int = 1080,
                 replace_output: bool = False):
        self.raw_socket = raw_socket
        self.source_index = source_index
        self.width = width
        self.height = height
        self.replace_output = replace_output

    @property
    def name(self) -> str:
        return "ai_cameraman"

    def get_branches(self, settings) -> dict[int, str]:
        return {}

    def get_raw_branches(self, settings) -> dict[int, str]:
        frame = self.width * self.height * 3 // 2
        return {
            self.source_index: (
                "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                "vapostproc ! "
                f"video/x-raw,format=NV12,width={self.width},height={self.height} ! "
                f"shmsink socket-path={self.raw_socket} wait-for-connection=false "
                f"shm-size={frame * 12} sync=false async=false"
            )
        }

    def get_replaced_outputs(self, settings) -> set[int]:
        return {self.source_index} if self.replace_output else set()