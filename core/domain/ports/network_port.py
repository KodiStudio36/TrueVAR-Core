from abc import ABC, abstractmethod

class NetworkPort(ABC):
    @abstractmethod
    def ping_internet(self) -> bool:
        """Check if global internet is reachable."""
        pass

    @abstractmethod
    def ping_camera(self, ip_address: str) -> bool:
        """Check if a specific camera IP is reachable on the local network."""
        pass