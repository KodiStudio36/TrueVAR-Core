from typing import List, Type
from plugins.base_plugin import BasePlugin
from plugins.firebase_sync.plugin import FirebaseSyncPlugin
from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin
from plugins.bout_cntrl.livestream_extension.plugin import BoutCntrlLivestreamExtension
from plugins.livestream.plugin import LivestreamCorePlugin
from plugins.shuttle_xpress.plugin import ShuttleXpressPlugin
from plugins.tkstrike.livestream_extension.plugin import TkStrikeLivestreamExtension
from plugins.tkstrike.external_screen.plugin import TkStrikeExternalScreenPlugin
from plugins.tkstrike.listener.gen3.plugin import TkStrikeListenerGen3Plugin
from plugins.bout_cntrl.bout_cntrl.plugin import BoutCntrlPlugin
from plugins.tkstrike.server.gen3.plugin import TkStrikeServerGen3Plugin
from plugins.tkstrike.tkstrike.gen2.plugin import TkStrikeGen2Plugin

# === Taekwondo ===============
TAEKWONDO = "Taekwondo".lower()

# disciplines
KYORUGI = "Kyorugi".lower()
POOMSAE = "Poomsae".lower()

# providers
DAEDO = "DAEDO"

# modes
INTEGRATED_TKSTRIKE = "Integrated TkStrike"
EXTERNAL_TKSTRIKE = "External TkStrike"


# === Box =====================
BOXING = "Boxing".lower()

# disciplines
OLYMPIC_BOXING = "Olympic Boxing".lower()

# providers
BOUT_CNTRL = "Bout Cntrl"

# modes
INTEGRATED_BOUT_CNTRL = "Integrated Bout Cntrl"


class PluginFactory:
    @staticmethod
    def build_stack(sport: str, discipline: str, provider: str, mode: str, is_stream: bool, is_public_register) -> List[Type[BasePlugin]]:
        """Determines exactly which plugins need to boot for this tournament."""
        
        # 1. Base Layer: GStreamer handles the hardware for everything
        stack = [GStreamerCorePlugin, FirebaseSyncPlugin] # FirebaseSyncPlugin

        if provider == DAEDO:
            stack.append(TkStrikeListenerGen3Plugin)
            stack.append(IVRPlugin)
            stack.append(ShuttleXpressPlugin)

            if is_public_register == True:
                stack.append(TkStrikeServerGen3Plugin)

            if mode == INTEGRATED_TKSTRIKE:
                stack.append(TkStrikeGen2Plugin)

            if mode == EXTERNAL_TKSTRIKE:
                stack.append(TkStrikeExternalScreenPlugin)

        elif provider == BOUT_CNTRL:
            stack.append(BoutCntrlPlugin)
            
        if is_stream:
            stack.append(LivestreamCorePlugin)

            if provider == DAEDO:
                stack.append(TkStrikeLivestreamExtension)

            elif provider == BOUT_CNTRL:
                stack.append(BoutCntrlLivestreamExtension)
            
        return stack