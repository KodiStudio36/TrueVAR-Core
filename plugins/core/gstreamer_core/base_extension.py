from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugins.core.gstreamer_core.settings import CameraSettings


class BaseGStreamerExtension(ABC):
    """A branch that attaches to one or more source tees in the pipeline.

    The pipeline names tee elements tee_src0 (master/scoreboard), tee_src1..n
    (IP cameras).  An extension returns a dict of  { source_index: branch_string }
    where  branch_string  is everything AFTER  "tee_srcN. ! ".

    Example for an xvimagesink on the master source:
        {0: "queue leaky=downstream max-size-buffers=2 ! xvimagesink sync=false"}

    IP cameras (1..n) additionally expose the decoded, native-resolution stream
    (before the 720p vapostproc) on  raw_srcN  — see get_raw_branches().
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique key used for registration / lookup."""

    @abstractmethod
    def get_branches(self, settings: "CameraSettings") -> dict[int, str]:
        """Return { source_index: pipeline_branch_string } pairs."""

    def get_raw_branches(self, settings: "CameraSettings") -> dict[int, str]:
        """Optional: branches on the decoded native-resolution stream of IP cameras.

        Returns { source_index: branch_string } where branch_string is everything
        AFTER  "raw_srcN. ! ".  Only indices 1..n (IP cameras) are honoured.
        Buffers are video/x-raw(memory:VAMemory) straight from vah264dec, so a
        branch normally starts with a leaky queue and its own vapostproc.

        The raw tee is only inserted into the pipeline when at least one
        extension returns a branch for that camera, so existing setups produce
        a byte-identical pipeline string.
        """
        return {}

    def get_replaced_outputs(self, settings: "CameraSettings") -> set[int]:
        """Optional: camera indices whose fixed  camera{N}_shm_socket  this extension
        takes over (it publishes its own stream on that socket instead).

        For those indices the core neither builds the fixed shmsink branch nor
        deletes the socket file at pipeline start, so the extension's live socket
        survives every rebuild. The 720p path is skipped entirely when nothing
        else uses it.
        """
        return set()