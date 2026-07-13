from core.domain.entities import SystemStatus, Tournament
from core.event_bus import EventBus
from core.use_cases.plugin_factory import PluginFactory
from core.use_cases.settings_manager import SettingsManager

from plugins.gstreamer_core.plugin import GStreamerCorePlugin
from plugins.ivr.plugin import IVRPlugin

from datetime import datetime, time, timezone


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
                case_data = await self.db_adapter.fetch_case(device_license_key)
                self.system_status.is_licensed = case_data is not None

                if self.system_status.is_licensed:
                    self.system_status.active_tournaments = await self.db_adapter.fetch_today_tournaments()
                    self.root_logger.info(
                        f"Fetched {len(self.system_status.active_tournaments)} tournaments."
                    )
                    # NEW: make sure any existing binding still points at something real & current
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
        """
        If case_data has a tournamentId, confirm it still resolves to a real,
        current tournament. If it doesn't (deleted, or dateTime is in the
        past), strip tournamentId/courtId from both the returned dict and
        the DB so the frontend falls back to the normal selection flow
        instead of trying to auto-boot a dead tournament.
        """
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
        """A tournament counts as current if its dateTime is today (UTC) or later."""
        tournament_dt = getattr(tournament, "dateTime", None)
        if tournament_dt is None:
            return True  # no date info — don't block on it

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
                self.active_plugins.append(instance)
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

        # NEW: stale tournament is treated the same as "not found"
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
            "court_id": court_id,          # ← available to every plugin
            "fastapi_app": fastapi_app,
            "sio": fastapi_app.state.sio,
            "event_bus": self.event_bus,
        }

        await self._safe_load_plugins(fastapi_app, plugins_to_load, shared_context)
        self.root_logger.info("Online Tournament fully booted.")

    # async def boot_tournament(self, fastapi_app, tournament_id: str, court_id: str = None):
    #     # Guard: never boot twice in the same process lifetime
    #     if self.is_booted:
    #         self.root_logger.warning("System already booted — skipping re-boot.")
    #         return

    #     # Try active list first (populated by preflight)
    #     tournament = next(
    #         (t for t in self.system_status.active_tournaments if t.id == tournament_id), None
    #     )

    #     # Fallback: fetch directly (handles auto-boot after restart where preflight wasn't called)
    #     if not tournament:
    #         tournament = await self.db_adapter.fetch_tournament_by_id(tournament_id)
    #         if tournament:
    #             self.system_status.active_tournaments.append(tournament)

    #     if not tournament:
    #         self.root_logger.error(f"Cannot boot — tournament {tournament_id} not found.")
    #         raise ValueError("Tournament not found")

    #     self.system_status.active_tournament_id = tournament.id
    #     self.system_status.is_offline_mode = False
    #     self.root_logger.info(f"Booting system for {tournament.title} ({tournament.discipline})")

    #     plugins_to_load = PluginFactory.build_stack(
    #         tournament.discipline, tournament.settings["isStream"]
    #     )

    #     shared_context = {
    #         "db": self.db_adapter,
    #         "settings_manager": self.settings_manager,
    #         "network_adapter": self.network_adapter,
    #         "tournament": tournament,
    #         "court_id": court_id,          # ← available to every plugin
    #         "fastapi_app": fastapi_app,
    #         "sio": fastapi_app.state.sio,
    #         "event_bus": self.event_bus,
    #     }

    #     await self._safe_load_plugins(fastapi_app, plugins_to_load, shared_context)
    #     self.root_logger.info("Online Tournament fully booted.")

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