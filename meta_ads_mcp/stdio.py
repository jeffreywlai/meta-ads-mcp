"""stdio entry point."""

from __future__ import annotations

from .coordinator import mcp_server
from .tools import TOOL_MODULES

TOOLS = TOOL_MODULES


def main() -> None:
    """Run the server over stdio."""
    mcp_server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
