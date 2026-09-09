"""OctoBrowser-MCP -- steer antidetect browser profiles with AI over MCP."""

__version__ = "0.3.0"
__author__ = "eforus-overseer"

__all__ = [
    "CloudConduit",
    "Helmsman",
    "LocalConduit",
    "OctoApiFault",
    "__version__",
]

from .conduits import CloudConduit, LocalConduit, OctoApiFault
from .helmsman import Helmsman
