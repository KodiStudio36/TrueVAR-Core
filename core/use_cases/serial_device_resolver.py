"""
serial_device_resolver.py

Finds a USB-serial device by VID:PID (matches ANY unit of the same
hardware/firmware — not a single physical device with a fixed serial
number) and hands back its info. Symlinking to a stable path (e.g.
/dev/com1) is a separate, opt-in step — not every consumer of the device
needs one; a plugin that talks to it directly via pyserial can just use
the raw port path from find()/require().

Requires: pyserial
    pip install pyserial

Example device this was built for:
    ID 2047:0100  Texas Instruments MSP430-USB Example
"""

import os
from dataclasses import dataclass
from typing import Optional

import serial.tools.list_ports as list_ports


class DeviceNotFoundError(Exception):
    """Raised when no matching USB serial device could be located."""
    pass


@dataclass(frozen=True)
class UsbDeviceId:
    vendor_id: int
    product_id: int
    label: str = ""

    def __str__(self) -> str:
        return f"{self.label or 'device'} ({self.vendor_id:04x}:{self.product_id:04x})"


# The device you gave: "ID 2047:0100 Texas Instruments MSP430-USB Example"
MSP430_USB = UsbDeviceId(vendor_id=0x2047, product_id=0x0100, label="MSP430-USB Example")


@dataclass(frozen=True)
class DeviceInfo:
    """Everything a consumer might need — whether it wants to open the
    port directly (pyserial), symlink it, or just log/display it."""
    port: str  # e.g. /dev/ttyACM0
    vendor_id: int
    product_id: int
    serial_number: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None


class SerialDeviceResolver:
    """
    Locates a USB-serial device by VID:PID. Discovery (find/require) is
    the core operation; symlinking is an optional extra for consumers
    that specifically need a fixed path (e.g. Wine's COM-port mapping).
    """

    def __init__(self, logger, device: UsbDeviceId):
        self.logger = logger
        self.device = device

    def find(self) -> Optional[DeviceInfo]:
        """
        Scans all currently enumerated serial ports and returns a
        DeviceInfo for the one matching self.device's VID:PID.
        Returns None if the device isn't plugged in — use this when the
        caller wants to handle "not found" itself instead of an exception.
        """
        for port in list_ports.comports():
            if port.vid == self.device.vendor_id and port.pid == self.device.product_id:
                info = DeviceInfo(
                    port=port.device,
                    vendor_id=port.vid,
                    product_id=port.pid,
                    serial_number=port.serial_number,
                    location=port.location,
                    description=port.description,
                )
                self.logger.info(f"Found {self.device} at {info.port}")
                return info
        return None

    def require(self) -> DeviceInfo:
        """
        Same lookup as find(), but raises DeviceNotFoundError instead of
        returning None. Use this when a plugin genuinely can't proceed
        without the device — call it before starting anything downstream.
        """
        info = self.find()
        if info is None:
            raise DeviceNotFoundError(
                f"{self.device} not found. Please check it is plugged in."
            )
        return info

    @staticmethod
    def symlink(target_path: str, link_name: str, symlink_dir: str = "/dev") -> str:
        """
        Creates (or refreshes) a symlink, e.g. /dev/com1 -> /dev/ttyACM0.
        Purely optional — only call this if something downstream needs a
        fixed path rather than the raw port. Safe to call repeatedly.

        Note: writing into /dev generally requires root, or a udev rule
        granting write access. If you don't have that, point symlink_dir
        at somewhere you own instead.
        """
        link_path = os.path.join(symlink_dir, link_name)
        if os.path.islink(link_path) or os.path.exists(link_path):
            os.remove(link_path)
        os.symlink(target_path, link_path)
        return link_path

    def require_linked(self, link_name: str, symlink_dir: str = os.path.expanduser("~")) -> str:
        """
        Convenience for consumers that specifically need a stable path
        (like TkStrike's Wine COM-port mapping): require() + symlink() in
        one call. Raises DeviceNotFoundError if the device is missing.
        """
        info = self.require()
        link_path = self.symlink(info.port, link_name, symlink_dir)
        self.logger.info(f"Linked {link_path} -> {info.port}")
        return link_path