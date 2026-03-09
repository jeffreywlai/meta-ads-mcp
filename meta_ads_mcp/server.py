"""HTTP entry point."""

from __future__ import annotations

from .config import get_settings
from .coordinator import mcp_server
from .tools import TOOL_MODULES

TOOLS = TOOL_MODULES


def main() -> None:
    """Run the server over HTTP."""
    settings = get_settings()
    mcp_server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        show_banner=False,
    )


if __name__ == "__main__":
    main()
