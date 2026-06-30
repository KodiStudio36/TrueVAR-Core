from abc import ABC, abstractmethod
from typing import Callable, Coroutine, Any

class BaseListener(ABC):
    """
    Abstract Port for all hardware scoreboard listeners.
    Requires implementation of connection management and guarantees
    that all listeners use standard callback injection.
    """
    
    def __init__(
        self, 
        logger: Any,
        on_stable: Callable[[dict], Coroutine],
        on_clock: Callable[[str], Coroutine],
        on_event: Callable[[str], Coroutine]
    ):
        self.logger = logger
        self.on_stable = on_stable
        self.on_clock = on_clock
        self.on_event = on_event

    @abstractmethod
    async def connect_to_hardware(self, ip: str, port: int, **kwargs) -> None:
        """Initiates the connection (TCP/UDP/Serial) to the scoring hardware."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Safely closes the connection and cleans up transport protocols."""
        pass