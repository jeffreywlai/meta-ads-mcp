"""Meta Ads FastMCP package."""

from .server import main as http_main
from .stdio import main as stdio_main

__all__ = ["http_main", "stdio_main"]

