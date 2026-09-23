"""First-class remote-desktop backend implementations."""

from .base import ComputerBackend
from .config import BackendConfig, BackendKind, RdpConfig, VncConfig, load_backend_config
from .rdp import RDPBackend
from .vnc import VNCBackend

__all__ = [
    "BackendConfig",
    "BackendKind",
    "ComputerBackend",
    "RDPBackend",
    "RdpConfig",
    "VNCBackend",
    "VncConfig",
    "load_backend_config",
]
