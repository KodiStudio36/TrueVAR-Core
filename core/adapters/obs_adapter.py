import asyncio
import threading
from obswebsocket import obsws, requests
from core.domain.ports.broadcaster_port import BroadcasterPort
from core.use_cases.application_bootstrapper import ApplicationBootstrapper

class OBSAdapter(BroadcasterPort):
    def __init__(self, host: str, port: int, password: str, logger):
        self.host = host
        self.port = int(port)
        self.password = password
        self.logger = logger
        self._bootstrapper = ApplicationBootstrapper(logger)
        
        self.client = None
        self.connected = False
        self._obs_process = None

    async def boot_application(self, collection_name: str, workspace: int) -> bool:
        # Simply delegate to the utility class
        cmd = ["obs", "--collection", collection_name]
        return await self._bootstrapper.boot(
            cmd=cmd, 
            window_class="obs", 
            workspace=workspace, 
            startup_delay=4.0
        )

    async def connect(self, max_retries: int = 5) -> bool:
        """Attempts to connect to the OBS WebSocket with exponential backoff."""
        for attempt in range(1, max_retries + 1):
            try:
                self.logger.info(f"Attempting OBS WebSocket connection ({attempt}/{max_retries})...")
                
                # Connect synchronously in a thread to avoid blocking FastAPI
                def _connect():
                    self.client = obsws(self.host, self.port, self.password)
                    self.client.connect()
                
                await asyncio.to_thread(_connect)
                self.connected = True
                self.logger.info(f"Successfully connected to OBS at {self.host}:{self.port}")
                return True
                
            except Exception as e:
                self.logger.warning(f"OBS WebSocket not ready yet. Retrying in {attempt * 2}s...")
                await asyncio.sleep(attempt * 2)
                
        self.logger.error("Failed to connect to OBS WebSocket after multiple attempts.")
        self.connected = False
        return False

    def disconnect(self):
        if self.client and self.connected:
            self.client.disconnect()
            self.connected = False
            self.logger.info("Disconnected from OBS.")
            
        self._bootstrapper.terminate()

    # --- Broadcaster Controls ---

    def set_scene(self, scene_name: str):
        if not self.connected: return
        try:
            self.client.call(requests.SetCurrentProgramScene(sceneName=scene_name))
            self.logger.info(f"Switched scene to '{scene_name}'")
        except Exception as e:
            self.logger.error("OBS error switching scene", error=str(e))

    def set_transition(self, transition_name: str):
        if not self.connected: return
        try:
            self.client.call(requests.SetCurrentSceneTransition(transitionName=transition_name))
            self.logger.info(f"Set transition to '{transition_name}'")
        except Exception as e:
            self.logger.error("OBS error setting transition", error=str(e))

    def set_stream_key(self, stream_key: str):
        if not self.connected: return
        try:
            # First, get current settings to preserve the server URL
            current = self.client.call(requests.GetStreamServiceSettings())
            settings = current.getStreamServiceSettings()
            
            # Update just the key
            settings["key"] = stream_key
            
            self.client.call(requests.SetStreamServiceSettings(
                streamServiceType=current.getStreamServiceType(),
                streamServiceSettings=settings
            ))
            self.logger.info("Successfully updated YouTube stream key.")
        except Exception as e:
            self.logger.error("OBS error changing stream key", error=str(e))

    def set_streaming_state(self, active: bool):
        if not self.connected: return
        try:
            if active:
                self.client.call(requests.StartStream())
                self.logger.info("Livestream STARTED.")
            else:
                self.client.call(requests.StopStream())
                self.logger.info("Livestream STOPPED.")
        except Exception as e:
            self.logger.error("OBS error changing stream state", error=str(e))

    def reload_source(self, source_name: str, source_type: str):
        """
        OBS handles refreshing different sources in weird ways.
        This normalizes the behavior.
        """
        if not self.connected: return
        try:
            if source_type.lower() == "browser":
                # For browser sources, we can simulate pressing the "Refresh cache of current page" button
                self.client.call(requests.PressInputPropertiesButton(
                    inputName=source_name, 
                    propertyName="refreshnocache"
                ))
                self.logger.info(f"Reloaded Browser Source: {source_name}")
                
            elif source_type.lower() in ["camera", "microphone"]:
                # To reset hardware, we toggle it off and on quickly by disabling the item in the scene
                # Note: This requires knowing the active scene, or using SetInputSettings to force a restart.
                # A common hack is to alter the 'active' property slightly if the source supports it.
                self.logger.warning(f"Hardware reload requested for {source_name}. Implement specific toggle logic here based on your OBS composition.")
                
        except Exception as e:
            self.logger.error(f"OBS error reloading source {source_name}", error=str(e))