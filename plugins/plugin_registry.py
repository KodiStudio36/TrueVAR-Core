from typing import Dict, Optional, Type

from plugins.base_plugin import BasePlugin
from plugins.firebase_sync.plugin import FirebaseSyncPlugin
from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin
from plugins.bout_cntrl.livestream_extension.plugin import BoutCntrlLivestreamExtension
from plugins.livestream.plugin import LivestreamCorePlugin
from plugins.tkstrike.livestream_extension.plugin import TkStrikeLivestreamExtension
from plugins.tkstrike.external_screen.plugin import TkStrikeExternalScreenPlugin
from plugins.tkstrike.listener.plugin import TkStrikeListenerPlugin
from plugins.bout_cntrl.bout_cntrl.plugin import BoutCntrlPlugin
from plugins.tkstrike.tkstrike.plugin import TkStrikePlugin


class PluginRegistry:
    """
    Static catalogue of every plugin class the application knows about.

    PluginFactory.build_stack() decides sane defaults for a given
    tournament; this registry is the full universe of what CAN be
    attached on top of that from the dashboard, regardless of whether
    it's normally auto-booted.

    Keyed by class name (e.g. "FirebaseSyncPlugin") since that's
    available without instantiating anything.
    """

    _CLASSES: Dict[str, Type[BasePlugin]] = {
        cls.__name__: cls
        for cls in [
            GStreamerCorePlugin,
            FirebaseSyncPlugin,
            IVRPlugin,
            TkStrikeListenerPlugin,
            TkStrikeExternalScreenPlugin,
            TkStrikePlugin,
            TkStrikeLivestreamExtension,
            BoutCntrlPlugin,
            BoutCntrlLivestreamExtension,
            LivestreamCorePlugin,
        ]
    }

    @classmethod
    def all(cls) -> Dict[str, Type[BasePlugin]]:
        return dict(cls._CLASSES)

    @classmethod
    def get(cls, class_name: str) -> Optional[Type[BasePlugin]]:
        return cls._CLASSES.get(class_name)