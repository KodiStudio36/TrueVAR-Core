from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugins.gstreamer_core.settings import CameraSettings


class BaseGStreamerExtension(ABC):
    """A branch that attaches to one or more source tees in the pipeline.

    The pipeline names tee elements tee_src0 (master/scoreboard), tee_src1..n
    (IP cameras).  An extension returns a dict of  { source_index: branch_string }
    where  branch_string  is everything AFTER  "tee_srcN. ! ".

    Example for an xvimagesink on the master source:
        {0: "queue leaky=downstream max-size-buffers=2 ! xvimagesink sync=false"}
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique key used for registration / lookup."""

    @abstractmethod
    def get_branches(self, settings: "CameraSettings") -> dict[int, str]:
        """Return { source_index: pipeline_branch_string } pairs."""