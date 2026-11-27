import socket
import subprocess
import sys
from core.domain.ports.network_port import NetworkPort

_WINDOWS = sys.platform == "win32"
# A windowed (no console) Windows app would flash a console for every ping.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WINDOWS else 0


class SystemNetworkAdapter(NetworkPort):
    def _ping(self, host: str) -> bool:
        cmd = (["ping", "-n", "1", "-w", "1000", host] if _WINDOWS
               else ["ping", "-c", "1", "-W", "1", host])
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=5, creationflags=_NO_WINDOW)
            return res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def ping_internet(self) -> bool:
        # ICMP is often blocked (venue networks, Windows firewall) while the
        # internet works fine: a TCP connection to public DNS counts too.
        return (self._tcp_reachable("8.8.8.8", 53) or self._tcp_reachable("1.1.1.1", 53)
                or self._ping("8.8.8.8"))

    def ping_camera(self, ip_address: str) -> bool:
        return self._ping(ip_address)

    @staticmethod
    def ethernet_interfaces() -> list[dict]:
        """Physical wired NICs (no Wi-Fi, loopback, docker/bridges/veth) with
        their IPv4 addresses: [{"interface", "ipv4": [...], "link_up"}]."""
        import json
        import os

        if _WINDOWS:
            return SystemNetworkAdapter._ethernet_interfaces_psutil()

        base = "/sys/class/net"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return SystemNetworkAdapter._ethernet_interfaces_psutil()

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

    @staticmethod
    def _ethernet_interfaces_psutil() -> list[dict]:
        """Non-Linux: wired adapters by name ("Ethernet", "Ethernet 2", ...),
        virtual ones (Hyper-V, VirtualBox, VPN, Bluetooth) left out."""
        try:
            import psutil
        except ImportError:
            return []
        virtual = ("vethernet", "virtual", "vmware", "vbox", "hyper-v", "loopback",
                   "bluetooth", "vpn", "tap", "tun", "wsl", "docker")
        stats = psutil.net_if_stats()
        result = []
        for name, addrs in sorted(psutil.net_if_addrs().items()):
            low = name.lower()
            if not (low.startswith("ethernet") or low.startswith("eth") or low.startswith("en")):
                continue
            if any(v in low for v in virtual):
                continue
            ipv4 = [f"{a.address}/{sum(bin(int(o)).count('1') for o in a.netmask.split('.'))}"
                    if a.netmask else a.address
                    for a in addrs if a.family == socket.AF_INET]
            st = stats.get(name)
            result.append({"interface": name, "ipv4": ipv4, "link_up": bool(st and st.isup)})
        return result
