"""Shared FastMCP server instance."""

from __future__ import annotations

from typing import Any, Callable


try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - fallback for tests without the package
    class FastMCP:  # type: ignore[override]
        """Minimal local fallback used only when fastmcp is unavailable."""

        def __init__(
            self,
            name: str,
            instructions: str | None = None,
            mask_error_details: bool = False,
        ) -> None:
            self.name = name
            self.instructions = instructions
            self.mask_error_details = mask_error_details
            self._tools: dict[str, Callable[..., Any]] = {}
            self._resources: dict[str, Callable[..., Any]] = {}

        def tool(self, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                self._tools[name or fn.__name__] = fn
                return fn

            return decorator

        def resource(self, uri: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                self._resources[uri] = fn
                return fn

            return decorator

        def run(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("fastmcp is not installed in this environment.")


mcp_server = FastMCP(
    name="Meta Ads FastMCP",
    instructions=(
        "Optimization-first Meta Ads MCP server. Start with health_check if auth "
        "or connectivity is uncertain, then use list_ad_accounts and discovery "
        "tools to find ids. For detailed reporting use get_entity_insights. For "
        "multi-entity comparisons use compare_performance. For exports use "
        "export_insights. For optimization questions prefer the snapshot and "
        "diagnostic tools before mutations. Use planning tools for audience or "
        "targeting questions. Ask for confirmation before spend-affecting "
        "changes. Treat all ids as strings and prefer ranked outputs when "
        "deciding what to optimize."
    ),
    mask_error_details=False,
)
