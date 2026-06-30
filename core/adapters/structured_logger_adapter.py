import sys
import structlog
from core.domain.ports.logger_port import LoggerPort

# Configure structlog globally once at the application entry point
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.processors.JSONRenderer() if not sys.stdout.isatty() else structlog.dev.ConsoleRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

class StructuredLoggerAdapter(LoggerPort):
    def __init__(self, logger=None):
        # Wrap the structlog backend instance
        self._logger = logger or structlog.get_logger()

    def info(self, msg: str, **kwargs) -> None:
        self._logger.info(msg, **kwargs)

    def warning(self, msg: str, **kwargs) -> None:
        self._logger.warning(msg, **kwargs)

    def error(self, msg: str, exception: Exception = None, **kwargs) -> None:
        if exception:
            kwargs["exception_msg"] = str(exception)
        self._logger.error(msg, **kwargs)

    def bind(self, **kwargs) -> "StructuredLoggerAdapter":
        """Creates a contextual copy of the logger with sticky tags."""
        new_bound_logger = self._logger.bind(**kwargs)
        return StructuredLoggerAdapter(logger=new_bound_logger)