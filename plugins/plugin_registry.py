from typing import Dict, Optional, Type

from plugins.base_plugin import BasePlugin
from plugins.firebase_sync.plugin import FirebaseSyncPlugin
from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin
from plugins.bout_cntrl.livestream_extension.plugin import BoutCntrlLivestreamExtension
from plugins.livestream.plugin import LivestreamCorePlugin
from plugins.shuttle_xpress.plugin import ShuttleXpressPlugin
from plugins.tkstrike.livestream_extension.plugin import TkStrikeLivestreamExtension
from plugins.tkstrike.external_screen.plugin import TkStrikeExternalScreenPlugin
from plugins.tkstrike.listener.plugin import TkStrikeListenerPlugin
from plugins.bout_cntrl.bout_cntrl.plugin import BoutCntrlPlugin
from plugins.tkstrike.server.plugin import TkStrikeServerPlugin
from plugins.tkstrike.tkstrike.plugin import TkStrikePlugin


class PluginRegistry:
    _CLASSES: Dict[str, Type[BasePlugin]] = {
        cls.__name__: cls
        for cls in [
            GStreamerCorePlugin,
            FirebaseSyncPlugin,
            IVRPlugin,
            ShuttleXpressPlugin,
            TkStrikeListenerPlugin,
            TkStrikeExternalScreenPlugin,
            TkStrikePlugin,
            TkStrikeServerPlugin,
            TkStrikeLivestreamExtension,
            BoutCntrlPlugin,
            BoutCntrlLivestreamExtension,
            LivestreamCorePlugin,
        ]
    }

    @classmethod
    def _validate(cls) -> None:
        for name, plugin_cls in cls._CLASSES.items():
            deps = plugin_cls.__dict__.get("dependencies", plugin_cls.dependencies)
            if isinstance(deps, property):
                raise TypeError(
                    f"{name}.dependencies is declared as a @property — it must be a "
                    f"plain class attribute (e.g. `dependencies = [\"OtherPlugin\"]`)."
                )
            if not isinstance(deps, list):
                raise TypeError(f"{name}.dependencies must be a list, got {type(deps).__name__}.")
            for d in deps:
                if not isinstance(d, str):
                    raise TypeError(
                        f"{name}.dependencies contains {d!r} ({type(d).__name__}) — "
                        f"entries must be class-name strings, not the class object itself."
                    )
                if d not in cls._CLASSES:
                    raise TypeError(
                        f"{name}.dependencies references unknown plugin '{d}' — "
                        f"check spelling against the registered class name."
                    )

            ns = plugin_cls.__dict__.get("namespace", plugin_cls.namespace)
            if isinstance(ns, property):
                raise TypeError(
                    f"{name}.namespace is declared as a @property — it must be a "
                    f"plain class attribute (e.g. `namespace = Namespace.SCORING`)."
                )
            if not isinstance(ns, str):
                raise TypeError(f"{name}.namespace must be a string, got {type(ns).__name__}.")
            
            ui = plugin_cls.__dict__.get("settings_ui", plugin_cls.settings_ui)
            if isinstance(ui, property):
                raise TypeError(f"{name}.settings_ui must be a plain class attribute, not a @property.")
            if ui is not None and not isinstance(ui, str):
                raise TypeError(f"{name}.settings_ui must be None or a string, got {type(ui).__name__}.")


    @classmethod
    def all(cls) -> Dict[str, Type[BasePlugin]]:
        return dict(cls._CLASSES)

    @classmethod
    def get(cls, class_name: str) -> Optional[Type[BasePlugin]]:
        return cls._CLASSES.get(class_name)


PluginRegistry._validate()