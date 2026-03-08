"""HTTP entry point."""

from __future__ import annotations

from .config import get_settings
from .coordinator import mcp_server
from .tools import diagnostics, discovery, docs, execution, insights, recommendations, targeting

TOOLS = [
    diagnostics,
    discovery,
    docs,
    execution,
    insights,
    recommendations,
    targeting,
]


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

