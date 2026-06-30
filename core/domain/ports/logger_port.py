from abc import ABC, abstractmethod

class LoggerPort(ABC):
    @abstractmethod
    def info(self, msg: str, **kwargs) -> None: pass

    @abstractmethod
    def warning(self, msg: str, **kwargs) -> None: pass

    @abstractmethod
    def error(self, msg: str, exception: Exception = None, **kwargs) -> None: pass

    @abstractmethod
    def bind(self, **kwargs) -> "LoggerPort":
        """Returns a new logger instance with fixed contextual tags (e.g., plugin name)"""
        pass