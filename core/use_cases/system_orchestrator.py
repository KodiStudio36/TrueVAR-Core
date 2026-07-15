import asyncio
from datetime import datetime, time, timezone
from typing import Optional

from core.domain.entities import SystemStatus, Tournament
from core.event_bus import EventBus
from core.use_cases.plugin_factory import PluginFactory
from core.use_cases.settings_manager import SettingsManager
from plugins.plugin_registry import PluginRegistry

from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin


class SystemOrchestrator:
    def __init__(self, db_adapter, settings_adapter, network_adapter, root_logger):
        self.db_adapter = db_adapter
        self.settings_adapter = settings_adapter
        self.network_adapter = network_adapter
        self.root_logger = root_logger

        # Keyed by instance.name so individual plugins can be addressed
        # by the dashboard (restart/detach/settings).
        self.active_plugins: dict[str, "BasePlugin"] = {}
        self._booted: bool = False
        self._shared_context: Optional[dict] = None

        self.system_status = SystemStatus()
        self.settings_manager = SettingsManager(self.settings_adapter)
        self.event_bus = EventBus(logger=root_logger)

    @property
    def is_booted(self) -> bool:
        # A real flag rather than len(active_plugins) > 0 — detaching every
        # plugin shouldn't make the system look un-booted and eligible for
        # a fresh boot_tournament()/boot_offline() call.
        return self._booted

    async def run_preflight_async(self, device_license_key: str) -> dict:
        self.root_logger.info("Running preflight checks...")

        self.system_status.network_alive = self.network_adapter.ping_internet()

        case_data = None

        if self.system_status.network_alive:
            try:
                case_data = await self.db_adapter.fetch_case(device_license_key)
                self.system_status.is_licensed = case_data is not None

                if self.system_status.is_licensed:
                    self.system_status.active_tournaments = await self.db_adapter.fetch_today_tournaments()
                    self.root_logger.info(
                        f"Fetched {len(self.system_status.active_tournaments)} tournaments."
                    )
                    case_data = await self._validate_case_binding(device_license_key, case_data)
                else:
                    self.root_logger.warning("License key invalid or missing. Booting offline mode.")

            except Exception as e:
                self.root_logger.error("Database connection failed", exception=e)
                self.system_status.network_alive = False

        return {
            "network": self.system_status.network_alive,
            "licensed": self.system_status.is_licensed,
            "tournaments": [t.model_dump() for t in self.system_status.active_tournaments],
            "case": case_data,
        }

    async def _validate_case_binding(self, license_key: str, case_data: dict) -> dict:
        tournament_id = case_data.get("tournamentId") if case_data else None
        if not tournament_id:
            return case_data

        try:
            tournament = await self.db_adapter.fetch_tournament_by_id(tournament_id)
        except Exception as e:
            self.root_logger.error("Failed to verify bound tournament", exception=e)
            tournament = None

        stale = tournament is None or not self._is_tournament_current(tournament)

        if stale:
            reason = "missing" if tournament is None else "stale (dateTime in the past)"
            self.root_logger.warning(
                f"Case bound to {reason} tournament {tournament_id} — clearing binding."
            )
            try:
                await self.db_adapter.clear_case_binding(license_key)
            except Exception as e:
                self.root_logger.error("Failed to clear stale case binding", exception=e)

            case_data.pop("tournamentId", None)
            case_data.pop("courtId", None)

        return case_data

    @staticmethod
    def _is_tournament_current(tournament: Tournament) -> bool:
        tournament_dt = getattr(tournament, "dateTime", None)
        if tournament_dt is None:
            return True

        if tournament_dt.tzinfo is None:
            tournament_dt = tournament_dt.replace(tzinfo=timezone.utc)

        start_of_today = datetime.combine(
            datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc
        )
        return tournament_dt >= start_of_today

    async def _safe_load_plugins(self, fastapi_app, plugins_to_load: list, shared_context: dict):
        for plugin_class in plugins_to_load:
            try:
                plugin_logger = self.root_logger.bind(plugin=plugin_class.__name__.lower())
                instance = plugin_class(core_context=shared_context, logger=plugin_logger)

                self.settings_manager.register_plugin(
                    plugin_name=instance.name,
                    schema=instance.settings_schema,
                    callback=instance.on_settings_changed,
                )

                try:
                    from core.infrastructure.routers.settings_router_factory import (
                        create_plugin_settings_router,
                    )
                    settings_router = create_plugin_settings_router(
                        instance.name, self.settings_manager
                    )
                    if settings_router:
                        fastapi_app.include_router(settings_router)
                except Exception as e:
                    self.root_logger.warning(
                        f"Skipping settings UI router for {instance.name}", error=str(e)
                    )

                custom_router = instance.get_router()
                if custom_router:
                    fastapi_app.include_router(custom_router)

                await instance.start()
                self.active_plugins[instance.name] = instance
                self.root_logger.info(f"Loaded Plugin: {instance.name}")

            except Exception as e:
                self.root_logger.error(
                    f"FATAL: Failed to load plugin {plugin_class.__name__}", exception=str(e)
                )

    async def boot_tournament(
        self, fastapi_app, tournament_id: str, court_id: str = None, license_key: str = None
    ):
        if self.is_booted:
            self.root_logger.warning("System already booted — skipping re-boot.")
            return

        tournament: Tournament = next(
            (t for t in self.system_status.active_tournaments if t.id == tournament_id), None
        )

        if not tournament:
            tournament = await self.db_adapter.fetch_tournament_by_id(tournament_id)
            if tournament:
                self.system_status.active_tournaments.append(tournament)

        if tournament and not self._is_tournament_current(tournament):
            self.root_logger.warning(f"Tournament {tournament_id} is stale — treating as not found.")
            tournament = None

        if not tournament:
            if license_key:
                try:
                    await self.db_adapter.clear_case_binding(license_key)
                    self.root_logger.info(f"Cleared stale/missing binding for license {license_key}.")
                except Exception as e:
                    self.root_logger.error("Failed to clear stale case binding", exception=e)
            self.root_logger.error(f"Cannot boot — tournament {tournament_id} not found or stale.")
            raise ValueError("Tournament not found or stale")

        self.system_status.active_tournament_id = tournament.id
        self.system_status.is_offline_mode = False
        self.root_logger.info(f"Booting system for {tournament.title} ({tournament.discipline})")

        plugins_to_load = PluginFactory.build_stack(
            tournament.sport,
            tournament.discipline,
            tournament.settings["provider"],
            tournament.settings["mode"],
            tournament.settings["isStream"],
        )

        shared_context = {
            "db": self.db_adapter,
            "settings_manager": self.settings_manager,
            "network_adapter": self.network_adapter,
            "tournament": tournament,
            "court_id": court_id,
            "fastapi_app": fastapi_app,
            "sio": fastapi_app.state.sio,
            "event_bus": self.event_bus,
        }
        self._shared_context = shared_context

        await self._safe_load_plugins(fastapi_app, plugins_to_load, shared_context)
        self._booted = True
        self.root_logger.info("Online Tournament fully booted.")

    async def boot_offline(self, fastapi_app):
        if self.is_booted:
            self.root_logger.warning("System already booted — skipping re-boot.")
            return

        self.system_status.is_offline_mode = True
        self.root_logger.info("Booting system in OFFLINE mode")

        plugins_to_load = [GStreamerCorePlugin, IVRPlugin]

        shared_context = {
            "db": self.db_adapter,
            "settings_manager": self.settings_manager,
            "network_adapter": self.network_adapter,
            "tournament": None,
            "court_id": None,
            "fastapi_app": fastapi_app,
            "sio": fastapi_app.state.sio,
            "event_bus": self.event_bus,
        }
        self._shared_context = shared_context

        await self._safe_load_plugins(fastapi_app, plugins_to_load, shared_context)
        self._booted = True
        self.root_logger.info("Offline System fully booted.")

    # ------------------------------------------------------------------ dashboard: dynamic plugin control

    async def restart_plugin(self, plugin_name: str) -> dict:
        instance = self.active_plugins.get(plugin_name)
        if not instance:
            raise ValueError(f"Plugin '{plugin_name}' is not currently attached.")

        self.root_logger.info(f"Restarting plugin: {plugin_name}")
        await instance.stop()
        await instance.start()
        self.root_logger.info(f"Plugin restarted: {plugin_name}")
        return {"name": plugin_name, "status": "restarted"}

    async def detach_plugin(self, plugin_name: str) -> dict:
        instance = self.active_plugins.pop(plugin_name, None)
        if not instance:
            raise ValueError(f"Plugin '{plugin_name}' is not currently attached.")

        self.root_logger.info(f"Detaching plugin: {plugin_name}")
        try:
            await instance.stop()
        except Exception as e:
            self.root_logger.error(f"Error stopping plugin during detach: {plugin_name}", exception=e)

        self.root_logger.warning(f"Plugin detached: {plugin_name}")
        return {"name": plugin_name, "status": "detached"}

    async def attach_plugin(self, class_name: str, fastapi_app) -> dict:
        if self._shared_context is None:
            raise ValueError("System has not booted yet — nothing to attach a plugin to.")

        plugin_class = PluginRegistry.get(class_name)
        if plugin_class is None:
            raise ValueError(f"Unknown plugin class '{class_name}'.")

        if any(type(p) is plugin_class for p in self.active_plugins.values()):
            raise ValueError(f"An instance of '{class_name}' is already attached.")

        self.root_logger.info(f"Attaching new plugin: {class_name}")
        await self._safe_load_plugins(fastapi_app, [plugin_class], self._shared_context)

        # _safe_load_plugins logs and swallows failures rather than raising,
        # so confirm the plugin actually made it in before reporting success.
        attached = next((p for p in self.active_plugins.values() if type(p) is plugin_class), None)
        if attached is None:
            raise ValueError(f"Plugin '{class_name}' failed to start — check server logs.")

        return {"name": attached.name, "class_name": class_name, "status": "attached"}

    def get_dashboard_status(self) -> dict:
        loaded = [
            {
                "name": instance.name,
                "class_name": type(instance).__name__,
                "requires_internet": instance.requires_internet,
            }
            for instance in self.active_plugins.values()
        ]
        active_classes = {type(instance) for instance in self.active_plugins.values()}
        available = [
            class_name
            for class_name, cls in PluginRegistry.all().items()
            if cls not in active_classes
        ]
        return {
            "is_offline_mode": self.system_status.is_offline_mode,
            "active_tournament_id": self.system_status.active_tournament_id,
            "loaded_plugins": loaded,
            "available_plugins": sorted(available),
        }

    # ------------------------------------------------------------------

    async def shutdown_system(self):
        self.root_logger.info("SystemOrchestrator initiating system-wide shutdown sequence...")

        if not self.active_plugins:
            self.root_logger.info("No active plugins to shut down.")
            self._booted = False
            return

        tasks = []
        for plugin in self.active_plugins.values():
            self.root_logger.info(f"Preparing shutdown for plugin: {plugin.name}")
            tasks.append(plugin.stop())

        await asyncio.gather(*tasks, return_exceptions=True)

        self.active_plugins.clear()
        self._booted = False
        self.root_logger.info("SystemOrchestrator clean shutdown complete.")