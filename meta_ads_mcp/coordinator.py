"""Shared FastMCP server instance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from typing import Any, Callable

from meta_ads_mcp.config import get_settings


try:
    from fastmcp import FastMCP
    from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext
    from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
    from fastmcp.server.transforms.search import BM25SearchTransform
    from fastmcp.tools.tool import ToolResult
    import mcp.types as mt
    from mcp.types import TextContent
    import pydantic_core

    class ArchivedResponseLimitingMiddleware(ResponseLimitingMiddleware):
        """Archive oversized results and return a compact local artifact reference."""

        def __init__(
            self,
            *,
            max_size: int,
            truncation_suffix: str,
            export_directory: str | Path | None = None,
        ) -> None:
            super().__init__(
                max_size=max_size,
                truncation_suffix=truncation_suffix,
            )
            self.export_directory = (
                Path(export_directory).expanduser()
                if export_directory
                else Path(tempfile.gettempdir()) / "meta-ads-mcp-exports"
            )

        def _archive_result(
            self,
            result: ToolResult,
            *,
            tool_name: str,
        ) -> tuple[Path, int]:
            """Write the complete structured tool result to a private JSON file."""
            artifact_payload = (
                result.structured_content
                if result.structured_content is not None
                else result.model_dump(mode="json", by_alias=True)
            )
            artifact_bytes = json.dumps(
                artifact_payload,
                ensure_ascii=False,
                indent=2,
                default=str,
            ).encode("utf-8")
            self.export_directory.mkdir(parents=True, exist_ok=True)
            safe_tool_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", tool_name).strip("-") or "tool"
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f"{safe_tool_name}-",
                suffix=".json",
                dir=self.export_directory,
            )
            artifact_path = Path(raw_path)
            try:
                with os.fdopen(descriptor, "wb") as artifact_file:
                    artifact_file.write(artifact_bytes)
            except Exception:
                artifact_path.unlink(missing_ok=True)
                raise
            return artifact_path, len(artifact_bytes)

        async def on_call_tool(
            self,
            context: MiddlewareContext[mt.CallToolRequestParams],
            call_next: CallNext[mt.CallToolRequestParams, ToolResult],
        ) -> ToolResult:
            """Archive responses whose complete serialized MCP result exceeds the cap."""
            result = await call_next(context)
            if self.tools is not None and context.message.name not in self.tools:
                return result

            serialized = pydantic_core.to_json(result, fallback=str)
            if len(serialized) <= self.max_size:
                return result

            try:
                artifact_path, artifact_size = self._archive_result(
                    result,
                    tool_name=context.message.name,
                )
            except (OSError, TypeError, ValueError):
                return ToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=self.truncation_suffix.strip(),
                        )
                    ],
                    meta={
                        "overflow": {
                            "archived": False,
                            "original_response_bytes": len(serialized),
                            "max_inline_bytes": self.max_size,
                        }
                    },
                )

            message = (
                f"[Response exceeded the {self.max_size:,}-byte inline limit. "
                f"The complete JSON response was saved to {artifact_path}. "
                "Read that file for the full result, or narrow fields/lower limit for inline output.]"
            )
            return ToolResult(
                content=[TextContent(type="text", text=message)],
                meta={
                    "overflow": {
                        "archived": True,
                        "artifact_path": str(artifact_path),
                        "artifact_bytes": artifact_size,
                        "original_response_bytes": len(serialized),
                        "max_inline_bytes": self.max_size,
                    }
                },
            )
except ImportError:  # pragma: no cover - fallback for tests without the package
    class ResponseLimitingMiddleware:  # type: ignore[override]
        """Minimal local fallback for tests without FastMCP."""

        def __init__(
            self,
            *,
            max_size: int,
            truncation_suffix: str,
        ) -> None:
            self.max_size = max_size
            self.truncation_suffix = truncation_suffix

    class ArchivedResponseLimitingMiddleware(ResponseLimitingMiddleware):
        """Minimal local fallback for tests without FastMCP."""

        def __init__(
            self,
            *,
            max_size: int,
            truncation_suffix: str,
            export_directory: str | Path | None = None,
        ) -> None:
            super().__init__(
                max_size=max_size,
                truncation_suffix=truncation_suffix,
            )
            self.export_directory = export_directory

    class BM25SearchTransform:  # type: ignore[override]
        """Minimal local fallback for the FastMCP 3.1 search transform."""

        def __init__(
            self,
            *,
            max_results: int = 5,
            always_visible: list[str] | None = None,
            search_tool_name: str = "search_tools",
            call_tool_name: str = "call_tool",
            search_result_serializer: Callable[..., Any] | None = None,
        ) -> None:
            self.max_results = max_results
            self.always_visible = always_visible or []
            self.search_tool_name = search_tool_name
            self.call_tool_name = call_tool_name
            self.search_result_serializer = search_result_serializer

    class FastMCP:  # type: ignore[override]
        """Minimal local fallback used only when fastmcp is unavailable."""

        def __init__(
            self,
            name: str,
            instructions: str | None = None,
            version: str | None = None,
            mask_error_details: bool = False,
            transforms: list[Any] | None = None,
            **_: Any,
        ) -> None:
            self.name = name
            self.instructions = instructions
            self.version = version
            self.mask_error_details = mask_error_details
            self._tools: dict[str, Callable[..., Any]] = {}
            self._resources: dict[str, Callable[..., Any]] = {}
            self.transforms = list(transforms or [])
            self._transforms = list(transforms or [])

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

        def add_transform(self, transform: Any) -> None:
            self.transforms.append(transform)
            self._transforms.append(transform)

        def add_middleware(self, middleware: Any) -> None:
            self.middleware = [*getattr(self, "middleware", []), middleware]

        async def list_tools(self, *, run_middleware: bool = True) -> list[Any]:
            _ = run_middleware
            return [SimpleNamespace(name=name) for name in self._tools]

        def run(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("fastmcp is not installed in this environment.")


ALWAYS_VISIBLE_TOOLS = [
    "health_check",
    "get_capabilities",
    "list_ad_accounts",
]


def _first_sentence(text: str | None) -> str:
    """Return the first sentence from a tool description."""
    if not text:
        return ""
    stripped = " ".join(text.strip().split())
    head, _sep, _tail = stripped.partition(". ")
    return head.rstrip(".")


def _compact_description(text: str | None) -> str:
    """Trim repetitive docstring prefixes to keep search output compact."""
    sentence = _first_sentence(text)
    prefixes = (
        "Use this first when ",
        "Use this only when ",
        "Use this when ",
        "Use this before ",
        "Use this after ",
        "Use this for ",
    )
    for prefix in prefixes:
        if sentence.startswith(prefix):
            return sentence[len(prefix) :]
    return sentence


def _argument_summary(tool: Any) -> str:
    """Render a compact argument summary from the tool parameter schema."""
    parameters = getattr(tool, "parameters", None) or {}
    properties = parameters.get("properties", {}) or {}
    required = list(parameters.get("required", []) or [])
    optional = [name for name in properties if name not in required]

    def _format_names(names: list[str], *, label: str) -> str:
        if not names:
            return ""
        shown = names[:3]
        extra = len(names) - len(shown)
        suffix = f" +{extra}" if extra > 0 else ""
        return f"{label}: {', '.join(shown)}{suffix}"

    parts = []
    required_part = _format_names(required, label="req")
    optional_part = _format_names(optional, label="opt")
    if required_part:
        parts.append(required_part)
    if optional_part:
        parts.append(optional_part)
    return " | ".join(parts) if parts else "no args"


def serialize_search_results_compact(tools: list[Any]) -> str:
    """Serialize search results as compact markdown with minimal argument hints."""
    if not tools:
        return "No tools matched. Try a narrower query or call get_capabilities(intent=...)."

    lines = ["Matches:"]
    for tool in tools:
        name = getattr(tool, "name", "unknown_tool")
        description = _compact_description(getattr(tool, "description", None))
        args = _argument_summary(tool)
        line = f"- `{name}` | {args}"
        if description:
            line += f" | {description}"
        lines.append(line)
    lines.append("Next: use `call_tool` with the exact tool name and JSON arguments.")
    return "\n".join(lines)

TOOL_SEARCH_TRANSFORM = BM25SearchTransform(
    max_results=6,
    always_visible=ALWAYS_VISIBLE_TOOLS,
    search_result_serializer=serialize_search_results_compact,
)

MAX_TOOL_RESPONSE_BYTES = 64_000
RESPONSE_LIMIT_HINT = (
    "\n\n[Response exceeded the safe inline size and could not be archived. "
    "Narrow fields or lower limit, and ensure META_EXPORT_DIR is writable.]"
)

mcp_server = FastMCP(
    name="Meta Ads FastMCP",
    version="0.1.0",
    instructions=(
        "Optimization-first Meta Ads MCP server running on FastMCP 3.1. "
        "FastMCP tool search is enabled, so if the exact tool is not visible, "
        "use search_tools and then call_tool instead of exploring multiple "
        "tools blindly. If you are unsure which tool to use, call "
        "get_capabilities with an intent key for a compact routing answer. "
        "Start with health_check if auth or connectivity is uncertain, then "
        "use list_ad_accounts and discovery tools to find ids. Use "
        "get_account_pages before creative creation when a Page-linked asset "
        "is needed and list_instagram_accounts when an Instagram identity is "
        "needed. Use get_creative when a creative id is already known and "
        "full creative fields are needed. For detailed reporting use "
        "get_entity_insights. For "
        "action counts like appointments use summarize_actions. For "
        "multi-entity comparisons use compare_performance. For explicit account "
        "period comparisons use get_account_health_snapshot. For cannibalization "
        "or overlap checks use detect_auction_overlap. Use export_insights only "
        "when the user explicitly wants raw rows or CSV output. For optimization "
        "questions prefer the snapshot and diagnostic tools before mutations. "
        "For raw ad comments use list_ad_comments, for Page reviews or "
        "testimonials use list_page_recommendations, and for social ids behind "
        "an ad use get_ad_social_context. For quality rankings or unavailable "
        "feedback-score explanations use get_ad_feedback_signals. For Meta-native opportunity scans call "
        "get_recommendations once, and use typed opportunity tools only for "
        "category-specific follow-up. Use list_mutation_tools when the user asks "
        "what can be changed. Use planning tools for audience or targeting questions, "
        "including get_targeting_categories for generic category discovery, and "
        "search_ads_archive for public competitor/ad research. Ask for "
        "confirmation before spend-affecting changes. Treat all ids as strings "
        "and prefer ranked outputs when deciding what to optimize."
    ),
    transforms=[TOOL_SEARCH_TRANSFORM],
    mask_error_details=False,
)
mcp_server.add_middleware(
    ArchivedResponseLimitingMiddleware(
        max_size=MAX_TOOL_RESPONSE_BYTES,
        truncation_suffix=RESPONSE_LIMIT_HINT,
        export_directory=get_settings().export_directory,
    )
)
