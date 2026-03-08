"""stdio entry point."""

from __future__ import annotations

from .coordinator import mcp_server
from .tools import (
    ads,
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
    research,
    targeting,
    utility,
)

TOOLS = [
    ads,
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
    research,
    targeting,
    utility,
]


def main() -> None:
    """Run the server over stdio."""
    mcp_server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
