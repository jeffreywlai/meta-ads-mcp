"""Opt-in advertising read-only policy, enforced at MCP execution."""

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.input_compat import canonical_tool_name


def read_only_tool_names() -> set[str]:
    """Allow audited catalog entries only; new or unknown tools fail closed."""
    # Import after registration to avoid a coordinator/utility import cycle.
    from meta_ads_mcp.tools.utility import TOOL_GROUPS

    groups = ("discovery", "analysis", "activity", "optimization", "social_feedback",
              "planning", "research", "assets", "signals", "experiments", "catalog",
              "access",
              "docs", "utility")
    return {name for group in groups for name in TOOL_GROUPS[group]} | {
        "search_tools", "call_tool", "get_ad_image", "preview_ad",
        "generate_auth_url", "get_token_info", "validate_token",
    }


def _registered_tool_names() -> set[str]:
    """Every tool name this server registers, in any group."""
    from meta_ads_mcp.tools.utility import TOOL_GROUPS

    return {tool for group in TOOL_GROUPS.values() for tool in group}


class ReadOnlyAdvertisingMiddleware(Middleware):
    """Block both direct and routed mutations before the tool starts."""

    async def on_call_tool(self, context, call_next):
        if get_settings().read_only:
            name = canonical_tool_name(context.message.name)
            if name not in read_only_tool_names():
                # Repeat the name only when it is a real tool. A name that matches no
                # tool is whatever the caller typed, which may be misplaced private data.
                known = name in _registered_tool_names()
                shown = f"'{name}'" if known else "the tool named"
                raise ToolError(f"META_READ_ONLY is enabled; {shown} is not permitted.")
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        if not get_settings().read_only:
            return tools
        allowed = read_only_tool_names()
        return [tool for tool in tools if tool.name in allowed]
