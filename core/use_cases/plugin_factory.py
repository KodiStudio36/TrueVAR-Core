from typing import List, Type
from plugins.base_plugin import BasePlugin
from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin
from plugins.bout_cntrl.livestream_extension.plugin import BoutCntrlLivestreamExtension
from plugins.livestream.plugin import LivestreamCorePlugin
from plugins.tkstrike.livestream_extension.plugin import TkStrikeLivestreamExtension
from plugins.tkstrike.external_screen.plugin import TkStrikeExternalScreenPlugin
from plugins.tkstrike.listener.plugin import TkStrikeListenerPlugin
from plugins.bout_cntrl.bout_cntrl.plugin import BoutCntrlPlugin

# === Taekwondo ===============
TAEKWONDO = "Taekwondo"

# disciplines
KYORUGI = "Kyorugi"
POOMSAE = "Poomsae"

# providers
DAEDO = "DAEDO"

# modes
INTEGRATED_TKSTRIKE = "Integrated TkStrike"
EXTERNAL_TKSTRIKE = "External TkStrike"


# === Box =====================
BOXING = "Boxing"

# disciplines
OLYMPIC_BOXING = "Olympic Boxing"

# providers
BOUT_CNTRL = "Bout Cntrl"

# modes
INTEGRATED_BOUT_CNTRL = "Integrated Bout Cntrl"


class PluginFactory:
    @staticmethod
    def build_stack(sport: str, discipline: str, provider: str, mode: str, is_stream: bool) -> List[Type[BasePlugin]]:
        """Determines exactly which plugins need to boot for this tournament."""
        
        # 1. Base Layer: GStreamer handles the hardware for everything
        stack = [GStreamerCorePlugin]

        if provider == DAEDO:
            stack.append(TkStrikeListenerPlugin)
            stack.append(TkStrikeExternalScreenPlugin)
            stack.append(IVRPlugin)

            if mode == INTEGRATED_TKSTRIKE:
                pass


        elif provider == BOUT_CNTRL:
            stack.append(BoutCntrlPlugin)
            
        if is_stream:
            stack.append(LivestreamCorePlugin)

            if provider == DAEDO:
                stack.append(TkStrikeLivestreamExtension)

            elif provider == BOUT_CNTRL:
                stack.append(BoutCntrlLivestreamExtension)
            
        return stack