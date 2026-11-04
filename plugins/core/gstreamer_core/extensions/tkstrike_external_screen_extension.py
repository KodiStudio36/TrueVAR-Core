from plugins.core.gstreamer_core.base_extension import BaseGStreamerExtension


class TkStrikeExternalScreenExtension(BaseGStreamerExtension):
    """Adds an xvimagesink branch to one source tee (default: source 0, the scoreboard).

    The tee carries NV12. Many Xv adaptors don't accept NV12, and a failed caps
    negotiation on this branch takes the whole source down (including the IVR
    shmsink). The videoconvert lets xvimagesink pick a format it supports.
    """

    SINK_NAME = "extsink"

    def __init__(self, source_index: int = 0):
        self.source_index = source_index

    @property
    def name(self) -> str:
        return "tkstrike_external_screen"

    def get_branches(self, settings) -> dict[int, str]:
        # Source 0 only exists when the scoreboard camera is enabled; returning a
        # branch for a missing tee would make parse_launch fail for everyone.
        if self.source_index == 0 and not getattr(settings, "is_scoreboard", False):
            return {}
        return {
            self.source_index: (
                "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                "videoconvert ! "
                f"xvimagesink name={self.SINK_NAME} force-aspect-ratio=true sync=false async=false"
            )
        }