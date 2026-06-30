import subprocess
from core.domain.ports.network_port import NetworkPort

class SystemNetworkAdapter(NetworkPort):
    def _ping(self, host: str) -> bool:
        try:
            res = subprocess.run(["ping", "-c", "1", "-W", "1", host], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return res.returncode == 0
        except Exception:
            return False

    def ping_internet(self) -> bool:
        return self._ping("8.8.8.8")

    def ping_camera(self, ip_address: str) -> bool:
        return self._ping(ip_address)