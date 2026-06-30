from abc import ABC, abstractmethod

class BroadcasterPort(ABC):
    @abstractmethod
    async def boot_application(self, collection_name: str, workspace: int) -> bool:
        """Launches the broadcaster application natively and sets up the workspace."""
        pass

    @abstractmethod
    async def connect(self) -> bool:
        """Connects to the broadcaster's API (e.g., WebSocket)."""
        pass

    @abstractmethod
    async def disconnect(self):
        """Cleanly severs the connection."""
        pass

    @abstractmethod
    def set_scene(self, scene_name: str):
        pass

    @abstractmethod
    def set_transition(self, transition_name: str):
        pass

    @abstractmethod
    def set_stream_key(self, stream_key: str):
        pass

    @abstractmethod
    def set_streaming_state(self, active: bool):
        """Starts or stops the stream."""
        pass

    @abstractmethod
    def reload_source(self, source_name: str, source_type: str):
        """
        Reloads a specific source. 
        source_type: 'browser', 'camera', or 'microphone'
        """
        pass