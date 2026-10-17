"""print-warden: deterministic 3D-print monitoring with an MCP surface."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("print-warden")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
