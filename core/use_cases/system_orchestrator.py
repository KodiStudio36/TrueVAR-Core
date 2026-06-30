from core.domain.entities import SystemStatus, Tournament
from core.event_bus import EventBus
from core.use_cases.plugin_factory import PluginFactory
from core.use_cases.settings_manager import SettingsManager

from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin


class SystemOrchestrator:
    def __init__(self, db_adapter, settings_adapter, network_adapter, root_logger):
        self.db_adapter = db_adapter
        self.settings_adapter = settings_adapter
        self.network_adapter = network_adapter
        self.root_logger = root_logger

        self.active_plugins = []
        self.system_status = SystemStatus()
        self.settings_manager = SettingsManager(self.settings_adapter)
        self.event_bus = EventBus(logger=root_logger)

    @property
    def is_booted(self) -> bool:
        return len(self.active_plugins) > 0

    async def run_preflight_async(self, device_license_key: str) -> dict:
        self.root_logger.info("Running preflight checks...")

        self.system_status.network_alive = self.network_adapter.ping_internet()

        case_data = None

        if self.system_status.network_alive:
            try:
                # fetch_case replaces verify_license — it returns the full doc
                case_data = await self.db_adapter.fetch_case(device_license_key)
                self.system_status.is_licensed = case_data is not None

                if self.system_status.is_licensed:
                    self.system_status.active_tournaments = await self.db_adapter.fetch_today_tournaments()
                    self.root_logger.info(
                        f"Fetched {len(self.system_status.active_tournaments)} tournaments."
                    )
                else:
                    self.root_logger.warning("License key invalid or missing. Booting offline mode.")

            except Exception as e:
                self.root_logger.error("Database connection failed", exception=e)
                self.system_status.network_alive = False

        return {
            "network": self.system_status.network_alive,
            "licensed": self.system_status.is_licensed,
            "tournaments": [t.model_dump() for t in self.system_status.active_tournaments],
            "case": case_data,  # ← includes tournamentId / courtId if already bound
        }

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
                self.active_plugins.append(instance)
                self.root_logger.info(f"Loaded Plugin: {instance.name}")

            except Exception as e:
                self.root_logger.error(
                    f"FATAL: Failed to load plugin {plugin_class.__name__}", exception=str(e)
                )

    async def boot_tournament(self, fastapi_app, tournament_id: str, court_id: str = None):
        # Guard: never boot twice in the same process lifetime
        if self.is_booted:
            self.root_logger.warning("System already booted — skipping re-boot.")
            return

        # Try active list first (populated by preflight)
        tournament = next(
            (t for t in self.system_status.active_tournaments if t.id == tournament_id), None
        )

        # Fallback: fetch directly (handles auto-boot after restart where preflight wasn't called)
        if not tournament:
            tournament = await self.db_adapter.fetch_tournament_by_id(tournament_id)
            if tournament:
                self.system_status.active_tournaments.append(tournament)

        if not tournament:
            self.root_logger.error(f"Cannot boot — tournament {tournament_id} not found.")
            raise ValueError("Tournament not found")

        self.system_status.active_tournament_id = tournament.id
        self.system_status.is_offline_mode = False
        self.root_logger.info(f"Booting system for {tournament.title} ({tournament.discipline})")

        plugins_to_load = PluginFactory.build_stack(
            tournament.discipline, tournament.settings["isStream"]
        )

        shared_context = {
            "db": self.db_adapter,
            "settings_manager": self.settings_manager,
            "network_adapter": self.network_adapter,
            "tournament": tournament,
            "court_id": court_id,          # ← available to every plugin
            "fastapi_app": fastapi_app,
            "sio": fastapi_app.state.sio,
            "event_bus": self.event_bus,
        }

        await self._safe_load_plugins(fastapi_app, plugins_to_load, shared_context)
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

        await self._safe_load_plugins(fastapi_app, plugins_to_load, shared_context)
        self.root_logger.info("Offline System fully booted.")