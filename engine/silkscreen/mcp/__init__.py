"""Model Context Protocol server exposing the Silkscreen engine as tools."""

from .server import TOOLS, Server, handle

__all__ = ["Server", "handle", "TOOLS"]
