from plugins.gstreamer_core.base_extension import BaseGStreamerExtension


class TkStrikeExternalScreenExtension(BaseGStreamerExtension):
    """Adds an xvimagesink branch to the master source tee (source 0)."""

    @property
    def name(self) -> str:
        return "tkstrike_external_screen"

    def get_branches(self, settings) -> dict[int, str]:
        return {
            0: (
                "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                "xvimagesink name=extsink force-aspect-ratio=true sync=false"
            )
        }