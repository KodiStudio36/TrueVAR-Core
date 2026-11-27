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
    @staticmethod
    def ethernet_interfaces() -> list[dict]:
        """Physical wired NICs (no Wi-Fi, loopback, docker/bridges/veth) with
        their IPv4 addresses: [{"interface", "ipv4": [...], "link_up"}]."""
        import json
        import os

        base = "/sys/class/net"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return []

        def read(path: str) -> str:
            try:
                with open(path) as f:
                    return f.read().strip()
            except OSError:
                return ""

        wired = [
            n for n in names
            if os.path.exists(f"{base}/{n}/device")          # real hardware, not virtual
            and not os.path.exists(f"{base}/{n}/wireless")   # not Wi-Fi
            and not os.path.exists(f"{base}/{n}/phy80211")
            and read(f"{base}/{n}/type") == "1"              # ARPHRD_ETHER
        ]

        addrs: dict[str, list[str]] = {}
        try:
            out = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True,
                                 text=True, timeout=2).stdout
            for iface in json.loads(out or "[]"):
                addrs[iface.get("ifname", "")] = [
                    f"{a['local']}/{a['prefixlen']}" for a in iface.get("addr_info", [])
                    if a.get("family") == "inet" and "local" in a
                ]
        except Exception:
            pass

        return [
            {
                "interface": n,
                "ipv4": addrs.get(n, []),
                # carrier = cable plugged in and link negotiated
                "link_up": read(f"{base}/{n}/carrier") == "1",
            }
            for n in wired
        ]
