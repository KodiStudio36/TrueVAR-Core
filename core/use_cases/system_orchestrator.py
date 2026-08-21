import asyncio
from datetime import datetime, time, timezone
from typing import Optional

from core.domain.entities import SystemStatus, Tournament
from core.event_bus import EventBus
from core.use_cases.plugin_factory import PluginFactory
from core.use_cases.settings_manager import SettingsManager
from plugins.base_plugin import PluginState
from plugins.plugin_registry import PluginRegistry

from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin


class SystemOrchestrator:
    def __init__(self, db_adapter, settings_adapter, network_adapter, root_logger):
        self.db_adapter = db_adapter
        self.settings_adapter = settings_adapter
        self.network_adapter = network_adapter
        self.root_logger = root_logger

        self.active_plugins: dict[str, "BasePlugin"] = {}
        # Tracks plugin classes whose start() raised, keyed by class name,
        # so the dashboard can notify the operator and offer a retry —
        # without this, a failed plugin just silently vanishes (it's never
        # added to active_plugins, so it looks identical to "never attached").
        self.failed_plugins: dict[str, dict] = {}
        self._booted: bool = False
        self._shared_context: Optional[dict] = None

        self.system_status = SystemStatus()
        self.settings_manager = SettingsManager(self.settings_adapter)
        self.event_bus = EventBus(logger=root_logger)

    @property
    def is_booted(self) -> bool:
        return self._booted

    async def run_preflight_async(self, device_license_key: str) -> dict:
        self.root_logger.info("Running preflight checks...")

        self.system_status.network_alive = self.network_adapter.ping_internet()

        case_data = None

        self._license_key = device_license_key

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
            class_name = plugin_class.__name__
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

                instance.set_status(PluginState.STARTING)
                await instance.start()
                # If the plugin never touched its own status, assume a clean
                # start means it's active — keeps old plugins meaningful on
                # the dashboard without requiring every one to be rewritten.
                if instance.get_status().state in (PluginState.IDLE, PluginState.STARTING):
                    instance.set_status(PluginState.ACTIVE)

                self.active_plugins[instance.name] = instance
                self.failed_plugins.pop(class_name, None)
                self.root_logger.info(f"Loaded Plugin: {instance.name}")

            except Exception as e:
                self.root_logger.error(
                    f"FATAL: Failed to load plugin {plugin_class.__name__}", exception=str(e)
                )
                self.failed_plugins[class_name] = {
                    "class_name": class_name,
                    "namespace": getattr(plugin_class, "namespace", "uncategorized"),
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

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
            tournament.isRegistrationOpen,
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
        instance.set_status(PluginState.STARTING, "Restarting...")
        await instance.stop()
        await instance.start()
        if instance.get_status().state in (PluginState.IDLE, PluginState.STARTING):
            instance.set_status(PluginState.ACTIVE)
        self.root_logger.info(f"Plugin restarted: {plugin_name}")
        return {"name": plugin_name, "status": "restarted"}

    async def detach_plugin(self, plugin_name: str) -> dict:
        instance = self.active_plugins.get(plugin_name)
        if not instance:
            raise ValueError(f"Plugin '{plugin_name}' is not currently attached.")

        this_class_name = type(instance).__name__
        dependents = [
            p.name for p in self.active_plugins.values()
            if p is not instance and this_class_name in type(p).dependencies
        ]
        if dependents:
            raise ValueError(
                f"Cannot detach '{plugin_name}' — required by: {', '.join(dependents)}"
            )

        del self.active_plugins[plugin_name]
        self.root_logger.info(f"Detaching plugin: {plugin_name}")
        try:
            await instance.stop()
            instance.set_status(PluginState.STOPPED)
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

        active_class_names = {type(p).__name__ for p in self.active_plugins.values()}
        missing = [d for d in plugin_class.dependencies if d not in active_class_names]
        if missing:
            raise ValueError(
                f"Cannot attach '{class_name}' — missing dependencies: {', '.join(missing)}"
            )

        self.root_logger.info(f"Attaching new plugin: {class_name}")
        await self._safe_load_plugins(fastapi_app, [plugin_class], self._shared_context)

        attached = next((p for p in self.active_plugins.values() if type(p) is plugin_class), None)
        if attached is None:
            raise ValueError(f"Plugin '{class_name}' failed to start — check server logs.")

        return {"name": attached.name, "class_name": class_name, "status": "attached"}

    def get_dashboard_status(self) -> dict:
        active_class_names = {type(p).__name__ for p in self.active_plugins.values()}
        active_classes = set(type(p) for p in self.active_plugins.values())

        loaded = []
        for instance in self.active_plugins.values():
            status = instance.get_status()
            this_class_name = type(instance).__name__
            dependents = [
                p.name for p in self.active_plugins.values()
                if p is not instance and this_class_name in type(p).dependencies
            ]
            loaded.append({
                "name": instance.name,
                "class_name": this_class_name,
                "namespace": type(instance).namespace,
                "requires_internet": instance.requires_internet,
                "status": status.state.value,
                "message": status.message,
                "dependencies": type(instance).dependencies,
                "required_by": dependents,
            })

        available = []
        for class_name, cls in PluginRegistry.all().items():
            if cls in active_classes:
                continue
            deps = [
                {"class_name": d, "satisfied": d in active_class_names}
                for d in cls.dependencies
            ]
            available.append({
                "class_name": class_name,
                "namespace": cls.namespace,
                "dependencies": deps,
            })

        if self.system_status.is_offline_mode or not self._shared_context or self._shared_context["tournament"] is None:
            tournament_info = {"mode": "offline"}
        else:
            tournament_info = {
                "mode": "online",
                "tournament_id": self._shared_context["tournament"].id,
                "tournament_name": getattr(self._shared_context["tournament"], "title", None),
                "court_id": self._shared_context["court_id"],
            }

        return {
            "tournament": tournament_info,
            "loaded_plugins": loaded,
            "available_plugins": sorted(available, key=lambda p: (p["namespace"], p["class_name"])),
            "failed_plugins": sorted(self.failed_plugins.values(), key=lambda p: p["timestamp"]),
        }

    # ------------------------------------------------------------------ dashboard: back to tournament selection

    async def reset_to_tournament_selection(self) -> dict:
        """
        Undoes the current tournament binding and fully shuts the system
        down so the frontend can send the user back to the selection
        screen. Mirrors what _validate_case_binding does for a stale
        binding, but triggered on demand instead of at preflight.
        """
        if self._license_key:
            print("jjj")
            try:
                await self.db_adapter.clear_case_binding(self._license_key)
                self.root_logger.info(
                    f"Cleared case binding for license {self._license_key} (user requested change)."
                )
            except Exception as e:
                self.root_logger.error("Failed to clear case binding on reset", exception=e)

        print("hahaha")
        await self.shutdown_system()

        self.system_status = SystemStatus()
        self._shared_context = None

        self.root_logger.info("System reset — returning to tournament selection.")
        return {"status": "reset"}

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
        self.failed_plugins.clear()
        self._booted = False
        self.root_logger.info("SystemOrchestrator clean shutdown complete.")