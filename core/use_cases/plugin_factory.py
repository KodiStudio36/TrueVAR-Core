from typing import List, Type
from plugins.base_plugin import BasePlugin
from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin
from plugins.livestream.boxing_manual.plugin import BoxingLivestreamExtension
from plugins.livestream.core.plugin import LivestreamCorePlugin
from plugins.livestream.kyorugi_tkstrike.plugin import TaekwondoLivestreamExtension
from plugins.tkstrike_external_screen.plugin import TkStrikeExternalScreenPlugin
from plugins.tkstrike_listener.plugin import KyorugiListenerPlugin
from plugins.bout_control.plugin import BoutControlPlugin
# from plugins.livestream.plugin import LivestreamPlugin
# from plugins.judo.plugin import JudoScoreboardPlugin

KYORUGI_TKSTRIKE = "Kyorugi TkStrike"
POOMSAE_FITOFAN = "Poomsae Fitofan"
BOXING_MANUAL = "Boxing Manual"

class PluginFactory:
    @staticmethod
    def build_stack(discipline: str, is_stream: bool) -> List[Type[BasePlugin]]:
        """Determines exactly which plugins need to boot for this tournament."""
        
        # 1. Base Layer: GStreamer handles the hardware for everything
        stack = [GStreamerCorePlugin]

        if discipline == KYORUGI_TKSTRIKE:
            stack.append(KyorugiListenerPlugin)
            stack.append(TkStrikeExternalScreenPlugin)
            # stack.append(IVRPlugin)

        elif discipline == BOXING_MANUAL:
            stack.append(BoutControlPlugin)
            
        if is_stream:
            stack.append(LivestreamCorePlugin)

            if discipline == KYORUGI_TKSTRIKE:
                stack.append(TaekwondoLivestreamExtension)

            elif discipline == BOXING_MANUAL:
                stack.append(BoxingLivestreamExtension)
            
        return stack