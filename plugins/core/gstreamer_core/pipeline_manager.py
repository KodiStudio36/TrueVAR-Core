import asyncio
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from gi.repository import Gst
    from plugins.core.gstreamer_core.plugin import GStreamerCorePlugin
    from plugins.core.gstreamer_core.settings import CameraSettings
    from core.event_bus import EventBus

from plugins.core.gstreamer_core.base_extension import BaseGStreamerExtension

log = logging.getLogger(__name__)


class PipelineManager:
    """Extension registry for the GStreamer pipeline.

    Exposed as core_context["pipeline_manager"] so any plugin can register
    or unregister GStreamer extensions at runtime.  Rapid successive changes
    are debounced (100 ms) to avoid multiple restarts in a single boot cycle.

    Bus events emitted:
        gstreamer.pipeline.rebuilt  {"extensions": [name, ...]}
    """

    _DEBOUNCE_DELAY = 0.1   # seconds

    def __init__(
        self,
        plugin: "GStreamerCorePlugin",
        event_bus: Optional["EventBus"],
        logger,
    ):
        self._plugin = plugin
        self._bus = event_bus
        self._logger = logger
        self._extensions: dict[str, BaseGStreamerExtension] = {}
        self._rebuild_lock = asyncio.Lock()
        self._pending_rebuild: Optional[asyncio.Task] = None
        self._active = True       # False once the core plugin is stopping: no more rebuilds

    # ------------------------------------------------------------------ public API

    def register_extension(self, ext: BaseGStreamerExtension) -> None:
        if ext.name in self._extensions:
            self._logger.warning(f"Extension '{ext.name}' already registered — replacing.")
        self._extensions[ext.name] = ext
        self._logger.info(f"Pipeline extension registered: '{ext.name}'")
        self._schedule_rebuild()

    def unregister_extension(self, name: str) -> None:
        if name not in self._extensions:
            return
        del self._extensions[name]
        self._logger.info(f"Pipeline extension unregistered: '{name}'")
        self._schedule_rebuild()

    def get_branches_for_source(
        self, source_index: int, settings: "CameraSettings"
    ) -> list[str]:
        """Return fully-prefixed branch strings ready for insertion into the pipeline."""
        result = []
        for ext in self._extensions.values():
            branches = ext.get_branches(settings)
            if source_index in branches:
                result.append(f"tee_src{source_index}. ! {branches[source_index]}")
        return result

    def get_raw_branches_for_source(
        self, source_index: int, settings: "CameraSettings"
    ) -> list[str]:
        """Branches on the decoded native-resolution stream (raw_srcN), IP cameras only."""
        result = []
        for ext in self._extensions.values():
            branches = ext.get_raw_branches(settings)
            if source_index in branches:
                result.append(f"raw_src{source_index}. ! {branches[source_index]}")
        return result

    def get_replaced_outputs(self, settings: "CameraSettings") -> set[int]:
        """Camera indices whose fixed shm output an extension has taken over."""
        out: set[int] = set()
        for ext in self._extensions.values():
            out |= set(ext.get_replaced_outputs(settings))
        return out

    def deactivate(self) -> None:
        """Called by the core plugin on stop: (un)registrations during shutdown must
        not resurrect the pipeline."""
        self._active = False
        if self._pending_rebuild and not self._pending_rebuild.done():
            self._pending_rebuild.cancel()

    @property
    def pipeline(self) -> Optional["Gst.Pipeline"]:
        """The live pipeline object. Changes identity on every rebuild."""
        return self._plugin.pipeline

    def get_element(self, name: str):
        """Look up a named element in the live pipeline (None if not running)."""
        pipeline = self._plugin.pipeline
        return pipeline.get_by_name(name) if pipeline is not None else None

    # ------------------------------------------------------------------ debounced rebuild

    def _schedule_rebuild(self) -> None:
        if not self._active:
            return
        if self._pending_rebuild and not self._pending_rebuild.done():
            self._pending_rebuild.cancel()
        self._pending_rebuild = asyncio.create_task(self._debounced_rebuild())

    async def _debounced_rebuild(self) -> None:
        try:
            await asyncio.sleep(self._DEBOUNCE_DELAY)
            async with self._rebuild_lock:
                await self._plugin.rebuild_pipeline()
                if self._bus:
                    await self._bus.publish(
                        "gstreamer.pipeline.rebuilt",
                        {"extensions": list(self._extensions.keys())},
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._logger.error("Pipeline rebuild failed", exception=exc)