"""Convert application exceptions into stable MCP error envelopes."""

from __future__ import annotations

import json

from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware import Middleware
from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext
from pydantic import ValidationError as PydanticValidationError

from meta_ads_mcp.argument_privacy import (
    PRIVATE_ARGUMENT_TOOLS,
    private_validation_message,
    schema_parameter_names,
    unknown_parameter_count,
)
from meta_ads_mcp.config import get_settings
from meta_ads_mcp.errors import MetaApiError, RateLimitError
from meta_ads_mcp.official_parity import official_tool_guidance


# The search transform's proxy tool, and the envelope keys that name its target
# (name, and the compatibility alias tool_name). See coordinator.py.
PROXY_TOOL_NAMES: frozenset[str] = frozenset({"call_tool"})
_PROXY_TARGET_KEYS = ("name", "tool_name")
# The search transform's own tools. The proxy refuses them with its own message.
_SYNTHETIC_TOOL_NAMES: frozenset[str] = frozenset({"call_tool", "search_tools"})

# Returned instead of FastMCP's "Unknown tool: '<name>'", which repeats the name:
# a name that matches no tool may be a customer value put in the wrong place.
UNKNOWN_TOOL_MESSAGE = (
    "No tool here has the name given. The name is not repeated because it may hold "
    "personal data. Find tool names with search_tools(query='what you want to do'), "
    "or get_capabilities(intent=...), and call one of the names it returns."
)
# In read-only mode a name that is no tool was refused as not permitted; that stays
# visible, still without the name.
READ_ONLY_UNKNOWN_TOOL_MESSAGE = f"META_READ_ONLY is enabled; the tool named is not permitted. {UNKNOWN_TOOL_MESSAGE}"


def unknown_tool_message() -> str:
    """The refusal for a tool name that matches no tool, which never repeats the name."""
    return READ_ONLY_UNKNOWN_TOOL_MESSAGE if get_settings().read_only else UNKNOWN_TOOL_MESSAGE


class ToolParameterHelpMiddleware(Middleware):
    """Reject unknown keywords with the live schema before a tool executes.

    For tools in PRIVATE_ARGUMENT_TOOLS, unknown parameters are counted rather
    than named, and the framework's argument-validation error is replaced by one
    that names parameters only: those arguments can hold personal data or
    secrets, and a misplaced value can arrive as a parameter name.

    The call_tool proxy's own envelope (name, arguments, tool_name) gets the
    same handling unless every target it names is a registered tool outside
    that list: a customer value misplaced beside the nested arguments, or in
    name itself, must not be quoted back either.

    A tool name that matches no tool, called directly or named as the proxy's
    target, is refused with unknown_tool_message(), which does not repeat it.
    """

    @staticmethod
    async def _tool_or_none(context: MiddlewareContext, name: str):
        try:
            return await context.fastmcp_context.fastmcp.get_tool(name)
        except Exception:
            return None

    async def _envelope_is_private(self, context: MiddlewareContext) -> bool:
        """Whether a proxy call targets a private tool, or a target that cannot be determined."""
        arguments = context.message.arguments or {}
        supplied = [arguments.get(key) for key in _PROXY_TARGET_KEYS if arguments.get(key) is not None]
        if not supplied:
            return True
        for value in supplied:
            if not isinstance(value, str) or not value.strip():
                return True
            # get_tool resolves the same aliases and official names the proxy does.
            target = await self._tool_or_none(context, value.strip())
            if (
                target is None
                or target.name in PRIVATE_ARGUMENT_TOOLS
                or target.name in PROXY_TOOL_NAMES
            ):
                return True
        return False

    async def _names_an_unknown_target(self, context: MiddlewareContext) -> bool:
        """Whether a proxy call names a target that is no tool here.

        Blank and wrongly typed targets are left to the proxy and the framework,
        whose errors do not repeat them (see _envelope_is_private). An official
        Meta name that several tools here serve, or none, is left to the proxy,
        which explains it; the proxy's own tools are refused there too.
        """
        arguments = context.message.arguments or {}
        for key in _PROXY_TARGET_KEYS:
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            target = value.strip()
            if target in _SYNTHETIC_TOOL_NAMES or official_tool_guidance(target):
                continue
            if await self._tool_or_none(context, target) is None:
                return True
        return False

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool = None
        if context.fastmcp_context is not None:
            tool = await self._tool_or_none(context, context.message.name)
            if tool is None:
                # The framework's own error would repeat the name.
                raise ToolError(unknown_tool_message())
        proxy = tool is not None and tool.name in PROXY_TOOL_NAMES
        private = tool is not None and (
            tool.name in PRIVATE_ARGUMENT_TOOLS
            or (proxy and await self._envelope_is_private(context))
        )
        if tool is not None and tool.parameters.get("additionalProperties") is False:
            accepted = list(tool.parameters.get("properties", {}))
            unexpected = sorted(set(context.message.arguments or {}) - set(accepted))
            if unexpected:
                reason = (
                    "the tool being called can receive personal data or secrets"
                    if proxy
                    else "this tool's arguments can hold personal data or secrets"
                )
                described = (
                    f"{unknown_parameter_count(len(unexpected))} (names not repeated because {reason})"
                    if private
                    else ", ".join(unexpected)
                )
                raise ToolError(
                    f"Unknown parameters for {tool.name}: {described}. "
                    f"Accepted parameters: {', '.join(accepted) or '(none)'}. "
                    f"Inspect types and defaults with get_capabilities(tool_name='{tool.name}')."
                )
        if proxy and await self._names_an_unknown_target(context):
            # Refused before dispatch: the nested call's "Unknown tool" error
            # would repeat the target in the returned error and the log.
            raise ToolError(unknown_tool_message())
        if not private:
            return await call_next(context)
        failure: ToolError | None = None
        try:
            return await call_next(context)
        except (FastMCPValidationError, PydanticValidationError) as exc:
            cause = exc if isinstance(exc, PydanticValidationError) else exc.__cause__
            errors = cause.errors(include_url=False) if isinstance(cause, PydanticValidationError) else None
            failure = ToolError(
                private_validation_message(
                    tool.name, errors, schema_parameter_names(tool.parameters)
                )
            )
        # Raised outside the except block so the original error, which quotes
        # the values, is neither the cause nor the context of this one.
        raise failure


class StructuredMetaErrorMiddleware(Middleware):
    """Expose actionable Meta failures without leaking arbitrary response data."""

    @staticmethod
    def _meta_cause(exc: BaseException) -> MetaApiError | RateLimitError | None:
        """Find the typed upstream error after FastMCP wraps tool exceptions."""
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (RateLimitError, MetaApiError)):
                return current
            current = current.__cause__ or current.__context__
        return None

    @staticmethod
    def _structured_error(exc: MetaApiError | RateLimitError) -> ToolError:
        """Build one compact, allowlisted MCP error."""
        return ToolError(
            json.dumps({"error": exc.to_public_dict()}, separators=(",", ":"))
        )

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        try:
            return await call_next(context)
        except RateLimitError as exc:
            raise self._structured_error(exc) from exc
        except MetaApiError as exc:
            raise self._structured_error(exc) from exc
        except ToolError as exc:
            meta_cause = self._meta_cause(exc)
            if meta_cause is None:
                raise
            raise self._structured_error(meta_cause) from meta_cause
