import asyncio
from core.domain.ports.listener_port import BaseListener
from plugins.tkstrike.listener.gen3.parser import TkStrikeStateEngine

class TkStrikeUDPLifecycleProtocol(asyncio.DatagramProtocol):
    """Low-level AsyncIO datagram socket engine."""
    def __init__(self, parser: TkStrikeStateEngine, logger, log_callback, error_callback=None):
        self.parser = parser
        self.logger = logger
        self.log_callback = log_callback
        self.error_callback = error_callback

    def datagram_received(self, data, addr):
        message = ""
        try:
            message = data.decode(errors='ignore').strip()
            if message:
                # Mirroring your legacy log emit format smoothly
                self.log_callback(f"[{addr[0]}] {message}")
                self.parser.parse_string(message)
        except Exception as e:
            self.logger.error("Error processing inbound Daedo packet", error=str(e))
            if self.error_callback:
                self.error_callback(f"Parser error on [{addr[0]}] {message!r}: {e}")

    def error_received(self, exc):
        self.logger.error("Daedo UDP socket error", error=str(exc))
        if self.error_callback:
            self.error_callback(f"UDP socket error: {exc}")

    def connection_lost(self, exc):
        if exc and self.error_callback:
            self.error_callback(f"UDP socket closed with error: {exc}")

class TkStrikeListener(BaseListener):
    """Manages the lifecycle of the Async UDP Server without PyQt threads."""
    def __init__(self, logger, on_stable, on_clock, on_event):
        super().__init__(logger, on_stable, on_clock, on_event)

        self.parser = TkStrikeStateEngine(
            self.on_stable,
            self.on_clock,
            self.on_event
        )
        self.transport = None
        self.protocol = None

    async def connect_to_hardware(self, ip: str, port: int, log_callback=None, error_callback=None) -> bool:
        loop = asyncio.get_running_loop()
        default_logger = lambda msg: self.logger.info(f"[Daedo Raw Log] {msg}")
        self.logger.info(f"Binding Daedo UDP Listener to {ip}:{port}...")
        try:
            self.transport, self.protocol = await loop.create_datagram_endpoint(
                lambda: TkStrikeUDPLifecycleProtocol(
                    self.parser, self.logger, log_callback or default_logger, error_callback),
                local_addr=(ip, port)
            )
            self.logger.info(f"Daedo network listener safely active on {ip}:{port}")
            return True
        except Exception as e:
            self.logger.error(f"Fatal assignment failure matching to port {port}", error=str(e))
            if error_callback:
                error_callback(f"Could not bind UDP {ip}:{port}: {e}")
            return False

    async def disconnect(self):
        if self.transport:
            self.transport.close()
            self.logger.warning("Daedo UDP transport protocol channel severed.")