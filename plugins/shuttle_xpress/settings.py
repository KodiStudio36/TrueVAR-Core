from typing import Optional

from pydantic import BaseModel, Field


class ShuttleXpressSettings(BaseModel):
    # Stable path — survives reboots/replugs, unlike /dev/input/eventN which
    # can renumber. This is what start()'s connection check polls for.
    device_path: str = Field(
        default="/dev/input/by-id/usb-Contour_Design_ShuttleXpress-event-mouse",
        description="udev by-id symlink for the ShuttleXpress HID event node.",
    )

    # Vendor/product IDs from `lsusb`, used only to sanity-check that
    # whatever is at device_path is actually a ShuttleXpress and not some
    # other device that happened to get symlinked there.
    vendor_id: str = "0b33"
    product_id: str = "0020"

    # Path to the shuttle-go binary. Relative paths are resolved against
    # cwd of the parent process, so prefer an absolute path in production.
    shuttle_go_path: str = "/home/kodi/Documents/programs/var/shuttle-go/shuttle-go"

    # Optional path to a shuttle-go key-binding config file. Passed as
    # `-config <path>` when set; omitted entirely otherwise so shuttle-go
    # falls back to its own default.
    config_path: Optional[str] = None

    # How often to poll for the device node while it's absent.
    poll_interval_seconds: float = 5.0

    # Backoff cap for restarting shuttle-go after it exits unexpectedly
    # (e.g. device unplugged mid-run) — mirrors the reconnect backoff
    # pattern used elsewhere in the codebase (see IVRPlugin).
    restart_backoff_min_seconds: float = 1.0
    restart_backoff_max_seconds: float = 30.044