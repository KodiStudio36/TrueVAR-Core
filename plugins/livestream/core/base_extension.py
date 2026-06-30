from abc import abstractmethod

from numpy import broadcast
from plugins.base_plugin import BasePlugin

class BaseLivestreamExtension(BasePlugin):
    """
    Abstract base class for all sport-specific livestream plugins.
    Automatically handles OBS lifecycle, requiring children only to 
    define their sports logic and specific OBS collection/workspace.
    """
    
    @property
    @abstractmethod
    def obs_collection_name(self) -> str:
        """The child class must provide the OBS collection name to load."""
        pass

    @property
    @abstractmethod
    def i3_workspace(self) -> int:
        """The child class must provide the target i3 workspace."""
        pass

    async def start(self) -> None:
        self.logger.info(f"Booting Livestream Extension Base for {self.name}...")
        
        # 1. Grab Infrastructure
        self.obs = self.core_context.get("obs_adapter")
        self.broadcaster = self.core_context.get("overlay_broadcaster")
        
        if not self.obs or not self.broadcaster:
            self.logger.error("Livestream Core infrastructure is missing.")
            return

        # 2. Automatically Boot OBS for the child class
        app_booted = await self.obs.boot_application(
            collection_name=self.obs_collection_name,
            workspace=self.i3_workspace
        )
        
        if app_booted:
            connected = await self.obs.connect()
            if not connected:
                self.logger.error("OBS started but connection failed.")
                return
        else:
            self.logger.error("Could not launch OBS instance.")
            return
        
        # 6. Push the stream key into OBS
        broadcast = self.core_context["scheduled_broadcast"]
        self.obs.set_stream_key(broadcast["stream_key"])
        self.logger.info(
            f"Stream key applied  |  broadcast='{broadcast['title']}'  "
            f"court={broadcast["court_number"]}  url={broadcast['youtube_url']}"
        )

        # 3. Trigger the child class's specific setup logic
        await self.setup_discipline()
        self.logger.info(f"{self.name} successfully booted and attached to OBS.")

    async def stop(self) -> None:
        self.logger.warning(f"Tearing down {self.name}...")
        
        # 1. Let the child class clean up its listeners/hardware
        await self.teardown_discipline()
        
        # 2. Automatically kill OBS
        if getattr(self, "obs", None):
            self.obs.disconnect()

    # --- Hooks for Child Classes ---

    @abstractmethod
    async def setup_discipline(self) -> None:
        """Override this to boot your Automation Engines and Listeners."""
        pass

    @abstractmethod
    async def teardown_discipline(self) -> None:
        """Override this to cleanly disconnect your hardware listeners."""
        pass

    async def on_settings_changed(self, new_settings) -> None:
        pass