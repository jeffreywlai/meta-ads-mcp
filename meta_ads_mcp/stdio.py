"""stdio entry point."""

from __future__ import annotations

from .coordinator import mcp_server
from .tools import (
    audiences,
    auth_tools,
    campaigns,
    creatives,
    diagnostics,
    discovery,
    docs,
    execution,
    insights,
    recommendations,
    targeting,
)

TOOLS = [
    audiences,
    auth_tools,
    campaigns,
    creatives,
    diagnostics,
    discovery,
    docs,
    execution,
    insights,
    recommendations,
    targeting,
]


def main() -> None:
    """Run the server over stdio."""
    mcp_server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
