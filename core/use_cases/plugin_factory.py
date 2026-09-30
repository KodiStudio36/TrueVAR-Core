from typing import List, Type
from plugins.base_plugin import BasePlugin
from plugins.core.firebase_sync.plugin import FirebaseSyncPlugin
from plugins.core.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.core.ivr.plugin import IVRPlugin
from plugins.bout_cntrl.livestream_extension.plugin import BoutCntrlLivestreamExtension
from plugins.core.livestream.plugin import LivestreamCorePlugin
from plugins.core.shuttle_xpress.plugin import ShuttleXpressPlugin
from plugins.poomsae.plugin import PoomsaeControlPlugin
from plugins.tkstrike.listener.gen2.plugin import TkStrikeListenerGen2Plugin
from plugins.tkstrike.livestream_extension.plugin import TkStrikeLivestreamExtension
from plugins.tkstrike.external_screen.plugin import TkStrikeExternalScreenPlugin
from plugins.tkstrike.listener.gen3.plugin import TkStrikeListenerGen3Plugin
from plugins.bout_cntrl.bout_cntrl.plugin import BoutCntrlPlugin
from plugins.tkstrike.server.gen2.plugin import TkStrikeServerGen2Plugin
from plugins.tkstrike.server.gen3.plugin import TkStrikeServerGen3Plugin
from plugins.tkstrike.tkstrike.gen2.plugin import TkStrikeGen2Plugin
from plugins.universal.integrated_livestream_extension.plugin import UniversalIntegratedLivestreamExtension
from plugins.universal.integrated_livestream_extension.settings import UniversalIntegratedLivestreamSettings

# === Taekwondo ===============
TAEKWONDO = "Taekwondo".lower()

# disciplines
KYORUGI = "Kyorugi".lower()
POOMSAE = "Poomsae".lower()

# providers
DAEDO = "DAEDO"

# modes
EXTERNAL_TKSTRIKE_GEN3 = "External TkStrike Gen3"
INTEGRATED_TKSTRIKE_GEN2 = "Integrated TkStrike Gen2"
EXTERNAL_TKSTRIKE_GEN2 = "External TkStrike Gen2"


# === Box =====================
BOXING = "Boxing".lower()

# disciplines
OLYMPIC_BOXING = "Olympic Boxing".lower()

# providers
BOUT_CNTRL = "Bout Cntrl"

# modes
INTEGRATED_BOUT_CNTRL = "Integrated Bout Cntrl"

# === Universal Livestream ====
UNIVERSAL = "Universal".lower()

INTEGRATED_UNIVERSAL = "Integrated Universal Livestream"
EXTERNAL_UNIVERSAL = "External Universal Livestream"


class PluginFactory:
    @staticmethod
    def build_stack(sport: str, discipline: str, provider: str, mode: str, is_stream: bool, is_public_register) -> List[Type[BasePlugin]]:
        """Determines exactly which plugins need to boot for this tournament."""
        
        # 1. Base Layer: GStreamer handles the hardware for everything
        stack = [GStreamerCorePlugin, FirebaseSyncPlugin] # FirebaseSyncPlugin

        if provider == DAEDO:
            stack.append(IVRPlugin)
            stack.append(ShuttleXpressPlugin)


            if "Gen3" in mode:
                if is_public_register == True:
                    stack.append(TkStrikeServerGen3Plugin)

                if mode == EXTERNAL_TKSTRIKE_GEN3:
                    stack.append(TkStrikeListenerGen3Plugin)
                    stack.append(TkStrikeExternalScreenPlugin)

            else:
                if is_public_register == True:
                    stack.append(TkStrikeServerGen2Plugin)

                if mode == INTEGRATED_TKSTRIKE_GEN2:
                    stack.append(TkStrikeListenerGen2Plugin)
                    stack.append(TkStrikeGen2Plugin)

                if mode == EXTERNAL_TKSTRIKE_GEN2:
                    stack.append(TkStrikeListenerGen2Plugin)
                    stack.append(TkStrikeExternalScreenPlugin)

        elif discipline == POOMSAE:
            stack.append(PoomsaeControlPlugin)

        elif provider == BOUT_CNTRL:
            stack.append(BoutCntrlPlugin)
            
        if is_stream:
            stack.append(LivestreamCorePlugin)

            if provider == DAEDO:
                stack.append(TkStrikeLivestreamExtension)

            elif provider == BOUT_CNTRL:
                stack.append(BoutCntrlLivestreamExtension)

            elif mode == INTEGRATED_UNIVERSAL:
                stack.append(UniversalIntegratedLivestreamExtension)
            
            elif mode == EXTERNAL_UNIVERSAL:
                pass
            
        return stack