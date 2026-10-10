"""CPU package temperature + a tiny hysteresis governor for the detection rate."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional


def find_package_temp() -> Optional[Path]:
    """Path of the CPU package temperature (millidegrees C), or None.
    Intel: coretemp "Package id 0".  AMD: k10temp / zenpower "Tctl" (or "Tdie")."""
    hwmon = Path("/sys/class/hwmon")
    preferred = {"coretemp": ("Package",), "k10temp": ("Tctl", "Tdie"), "zenpower": ("Tdie", "Tctl")}
    for hw in sorted(hwmon.glob("hwmon*")):
        try:
            name = (hw / "name").read_text().strip()
            if name not in preferred:
                continue
            for want in preferred[name]:
                for label in sorted(hw.glob("temp*_label")):
                    if label.read_text().strip().startswith(want):
                        return label.with_name(label.name.replace("_label", "_input"))
            if (hw / "temp1_input").exists():
                return hw / "temp1_input"
        except OSError:
            continue
    for tz in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            if (tz / "type").read_text().strip() in ("x86_pkg_temp", "k10temp"):
                return tz / "temp"
        except OSError:
            continue
    return None


def efficiency_cores() -> Optional[set[int]]:
    """E-cores on Intel hybrid CPUs (cpu_atom PMU), intersected with what we may use."""
    p = Path("/sys/devices/cpu_atom/cpus")
    try:
        cores = parse_cpu_list(p.read_text())
    except OSError:
        return None
    cores &= os.sched_getaffinity(0)
    return cores or None


def parse_cpu_list(text: str) -> set[int]:
    out: set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


class ThermalGovernor:
    """scale = 1.0 / 0.5 / 0.25 of the configured detection rate.

    Steps down when package temp >= hot, back up when <= cool, at most one
    step per `settle` seconds so it doesn't oscillate.
    """

    STEPS = (1.0, 0.5, 0.25)

    def __init__(self, hot_c: float, cool_c: float, period: float = 2.0, settle: float = 10.0):
        self.hot, self.cool = hot_c, cool_c
        self.period, self.settle = period, settle
        self.path = find_package_temp()
        self.level = 0
        self.temp_c: Optional[float] = None
        self._next_read = 0.0
        self._last_change = 0.0

    @property
    def scale(self) -> float:
        return self.STEPS[self.level]

    def poll(self) -> bool:
        """Cheap; call often. Returns True when the scale changed."""
        now = time.monotonic()
        if self.path is None or now < self._next_read:
            return False
        self._next_read = now + self.period
        try:
            self.temp_c = int(self.path.read_text()) / 1000.0
        except (OSError, ValueError):
            return False
        if now - self._last_change < self.settle:
            return False
        if self.temp_c >= self.hot and self.level < len(self.STEPS) - 1:
            self.level += 1
        elif self.temp_c <= self.cool and self.level > 0:
            self.level -= 1
        else:
            return False
        self._last_change = now
        return True