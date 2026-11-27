import importlib
import logging
from typing import Dict, Iterable, Optional, Type

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

# class name -> module that defines it. Imported lazily (on first use), so
# importing the registry never pulls in a plugin's dependencies (gi/GStreamer,
# OpenCV, xrandr, ...). A plugin whose module can't be imported on this
# machine is skipped and reported in PluginRegistry.unavailable() instead of
# breaking the whole core — e.g. TrueVAR-TOO on Windows has no GStreamer.
_PLUGIN_MODULES: Dict[str, str] = {
    "GStreamerCorePlugin":           "plugins.core.gstreamer_core.plugin",
    "PipelineDiagnosticsPlugin":     "plugins.core.diagnostics.plugin",
    "FirebaseSyncPlugin":            "plugins.core.firebase_sync.plugin",
    "LivestreamCorePlugin":          "plugins.core.livestream.plugin",
    "IVRPlugin":                     "plugins.core.ivr.plugin",
    "ShuttleXpressPlugin":           "plugins.core.shuttle_xpress.plugin",
    "AICameramanKyorugiPlugin":      "plugins.universal.ai_cameraman.kyorugi.plugin",
    "AICameramanPoomsaePlugin":      "plugins.universal.ai_cameraman.poomsae.plugin",
    "TkStrikeExternalScreenPlugin":  "plugins.tkstrike.external_screen.plugin",
    "TkStrikeListenerGen2Plugin":    "plugins.tkstrike.listener.gen2.plugin",
    "TkStrikeListenerGen3Plugin":    "plugins.tkstrike.listener.gen3.plugin",
    "TkStrikeServerGen2Plugin":      "plugins.tkstrike.server.gen2.plugin",
    "TkStrikeServerGen3Plugin":      "plugins.tkstrike.server.gen3.plugin",
    "TkStrikeGen2Plugin":            "plugins.tkstrike.tkstrike.gen2.plugin",
    "TkStrikeLivestreamExtension":   "plugins.tkstrike.livestream_extension.plugin",
    "PoomsaeExternalScreenPlugin":   "plugins.poomsae.external_screen.plugin",
    "PoomsaeControlPlugin":          "plugins.poomsae.poomsae.plugin",
    "PoomsaeLivestreamExtension":    "plugins.poomsae.livestream_extension.plugin",
    "BoutCntrlPlugin":               "plugins.bout_cntrl.bout_cntrl.plugin",
    "BoutCntrlLivestreamExtension":  "plugins.bout_cntrl.livestream_extension.plugin",
}


class PluginRegistry:
    _CLASSES: Optional[Dict[str, Type[BasePlugin]]] = None
    _UNAVAILABLE: Dict[str, str] = {}
    _ALLOWED: Optional[frozenset] = None

    # ------------------------------------------------------------------ configuration

    @classmethod
    def restrict(cls, class_names: Iterable[str]) -> None:
        """Only these plugins exist for this app (call before first use).
        Others are never imported nor offered on the dashboard."""
        names = frozenset(class_names)
        unknown = names - _PLUGIN_MODULES.keys()
        if unknown:
            raise ValueError(f"Unknown plugin class(es): {', '.join(sorted(unknown))}")
        cls._ALLOWED = names
        cls._CLASSES = None
        cls._UNAVAILABLE = {}

    # ------------------------------------------------------------------ loading

    @classmethod
    def _load(cls) -> Dict[str, Type[BasePlugin]]:
        if cls._CLASSES is not None:
            return cls._CLASSES
        classes: Dict[str, Type[BasePlugin]] = {}
        unavailable: Dict[str, str] = {}
        for name, module in _PLUGIN_MODULES.items():
            if cls._ALLOWED is not None and name not in cls._ALLOWED:
                continue
            try:
                classes[name] = getattr(importlib.import_module(module), name)
            except Exception as exc:   # missing dependency on this platform, ...
                unavailable[name] = f"{type(exc).__name__}: {exc}"
                log.warning(f"Plugin {name} unavailable: {unavailable[name]}")
        cls._CLASSES = classes
        cls._UNAVAILABLE = unavailable
        cls._validate()
        return classes

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
                if d not in _PLUGIN_MODULES:
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

    # ------------------------------------------------------------------ public API

    @classmethod
    def all(cls) -> Dict[str, Type[BasePlugin]]:
        return dict(cls._load())

    @classmethod
    def get(cls, class_name: str) -> Optional[Type[BasePlugin]]:
        return cls._load().get(class_name)

    @classmethod
    def unavailable(cls) -> Dict[str, str]:
        """class name -> why it couldn't be imported on this machine."""
        cls._load()
        return dict(cls._UNAVAILABLE)
