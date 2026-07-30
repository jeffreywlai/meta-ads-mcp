"""Shared FastMCP server instance."""

from __future__ import annotations

from collections.abc import Sequence
import re
from types import SimpleNamespace
from typing import Any, Callable

from meta_ads_mcp.config import get_settings


try:
    from fastmcp import FastMCP
    from fastmcp.server.transforms.search import BM25SearchTransform
    from fastmcp.tools.tool import Tool

    from meta_ads_mcp.overflow import (
        ArchivedResponseLimitingMiddleware,
        OverflowArtifactStore,
    )
except ImportError:  # pragma: no cover - fallback for tests without the package
    Tool = Any

    class OverflowArtifactStore:  # type: ignore[override]
        """Minimal local fallback for tests without FastMCP."""

        def __init__(self, export_directory: str | None = None, **_: Any) -> None:
            self.export_directory = export_directory

        def read(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("fastmcp is not installed.")

        def delete(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("fastmcp is not installed.")

    class ArchivedResponseLimitingMiddleware:  # type: ignore[override]
        """Minimal local fallback for tests without FastMCP."""

        def __init__(self, *, max_size: int, truncation_suffix: str, artifact_store: Any) -> None:
            self.max_size = max_size
            self.truncation_suffix = truncation_suffix
            self.artifact_store = artifact_store

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
            self._max_results = max_results
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


class IntentAwareBM25SearchTransform(BM25SearchTransform):
    """Apply narrow intent safeguards on top of BM25 tool search."""

    _mutation_prefixes = ("create_", "update_", "delete_", "set_", "upload_")
    _other_mutation_tools = {"setup_ab_test"}
    _read_terms = re.compile(
        r"\b(read|view|fetch|get|show|inspect|look(?:up)?|retrieve|details?|"
        r"metadata|fields?|data|spec|full|what\s+is)\b"
    )
    _analysis_terms = re.compile(
        r"\b(performance|fatigue|report|optimization|opportunities|diagnostics?)\b"
    )
    _creative_spec_terms = re.compile(
        r"\b(details?|metadata|fields?|spec|full|url_tags|asset_feed_spec|"
        r"degrees_of_freedom(?:_spec)?|object_story_spec)\b"
    )
    _adjacent_creative_surface = re.compile(
        r"\bcomments?\b|\b(?:creative|ad)\s+image\b|\bimage\s+(?:for|from)\s+"
        r"(?:creative|ad)\b|\bcreative\s+from\s+ad\b"
    )
    _read_only_phrase = re.compile(r"\b(?:just\s+read|read[\s-]?only)\b")
    _mutation_verbs = re.compile(
        r"\b(?P<delete>delet(?:e|es|ed|ing)|remov(?:e|es|ed|ing))\b"
        r"|\b(?P<update>updat(?:e|es|ed|ing)|edit(?:s|ed|ing)?|"
        r"chang(?:e|es|ed|ing)|modif(?:y|ies|ied|ying)|"
        r"renam(?:e|es|ed|ing))\b"
        r"|\b(?P<create>creat(?:e|es|ed|ing))\b"
        r"|\b(?P<upload>upload(?:s|ed|ing)?)\b"
        r"|\b(?P<preview>preview(?:s|ed|ing)?)\b"
    )
    _negation_prefix = re.compile(
        r"(?:\b(?:do\s+not|don't|dont|never|not|without|avoid|avoiding)\b"
        r"(?:\s+\w+){0,2}\s*)$"
    )
    _mutation_tools = {
        "delete": "delete_creative",
        "update": "update_creative",
        "create": "create_ad_creative",
        "upload": "upload_creative_asset",
        "preview": "preview_ad",
    }

    @staticmethod
    def _tool_named(tools: Sequence[Tool], name: str) -> Tool | None:
        """Find a tool in the searchable catalog by exact name."""
        return next((tool for tool in tools if getattr(tool, "name", None) == name), None)

    def _prioritize(
        self,
        tools: Sequence[Tool],
        ranked: Sequence[Tool],
        name: str,
        *,
        mutation_safe: bool = False,
        excluded_names: set[str] | None = None,
    ) -> Sequence[Tool]:
        """Put an exact-intent tool first and optionally suppress all mutations."""
        selected = self._tool_named(tools, name)
        filtered = [
            tool
            for tool in ranked
            if getattr(tool, "name", None) != name
            and getattr(tool, "name", None) not in (excluded_names or set())
            and (
                not mutation_safe
                or (
                    not str(getattr(tool, "name", "")).startswith(
                        self._mutation_prefixes
                    )
                    and getattr(tool, "name", None) not in self._other_mutation_tools
                )
            )
        ]
        if selected is not None:
            filtered.insert(0, selected)
        return filtered[: self._max_results]

    async def _search(self, tools: Sequence[Tool], query: str) -> Sequence[Tool]:
        """Rank tools with explicit creative read/write intent taking precedence."""
        ranked = await super()._search(tools, query)
        normalized = " ".join(query.lower().split())
        if re.search(r"\b(?:overflow|oversized)\b.*\b(?:response|artifact)\b", normalized):
            if re.search(r"\b(delete|remove|discard|purge)\b", normalized):
                return self._prioritize(
                    tools,
                    ranked,
                    "delete_overflow_artifact",
                )
            return self._prioritize(
                tools,
                ranked,
                "read_overflow_artifact",
                mutation_safe=True,
            )
        if not re.search(r"\bcreatives?\b", normalized):
            return ranked

        negated_mutation = bool(self._read_only_phrase.search(normalized))
        negated_tools: set[str] = set()
        positive_tools: list[str] = []
        for match in self._mutation_verbs.finditer(normalized):
            prefix = normalized[max(0, match.start() - 48) : match.start()]
            tool_name = self._mutation_tools[match.lastgroup or ""]
            if self._negation_prefix.search(prefix):
                negated_mutation = True
                negated_tools.add(tool_name)
                continue
            if match.lastgroup == "delete" and match.group().startswith("remov"):
                if re.search(r"\bremov\w*\s+(?:the\s+)?creative\b", normalized):
                    if re.search(
                        r"\bremov\w*\s+(?:the\s+)?creative\s+from\b",
                        normalized,
                    ):
                        continue
                else:
                    continue
            positive_tools.append(tool_name)
        if positive_tools:
            return self._prioritize(
                tools,
                ranked,
                positive_tools[0],
                excluded_names=negated_tools,
            )
        if self._analysis_terms.search(normalized) and not self._creative_spec_terms.search(
            normalized
        ):
            return ranked
        if re.search(r"\bcomments?\b", normalized):
            return self._prioritize(
                tools,
                ranked,
                "list_ad_comments",
                mutation_safe=True,
            )
        if re.search(r"\b(?:creative|ad)\s+image\b", normalized) or re.search(
            r"\bimage\s+(?:for|from)\s+(?:creative|ad)\b",
            normalized,
        ):
            return self._prioritize(
                tools,
                ranked,
                "get_ad_image",
                mutation_safe=True,
            )
        if re.search(r"\bcreative\s+from\s+ad\b", normalized):
            return self._prioritize(
                tools,
                ranked,
                "get_ad",
                mutation_safe=True,
            )
        if re.search(r"\bcreatives\b", normalized):
            return self._prioritize(
                tools,
                ranked,
                "list_creatives",
                mutation_safe=True,
            )
        if self._adjacent_creative_surface.search(normalized):
            return ranked
        if negated_mutation:
            return self._prioritize(
                tools,
                ranked,
                "get_creative",
                mutation_safe=True,
            )
        if self._read_terms.search(normalized):
            return self._prioritize(
                tools,
                ranked,
                "get_creative",
                mutation_safe=True,
            )
        return ranked


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

TOOL_SEARCH_TRANSFORM = IntentAwareBM25SearchTransform(
    max_results=6,
    always_visible=ALWAYS_VISIBLE_TOOLS,
    search_result_serializer=serialize_search_results_compact,
)

MAX_TOOL_RESPONSE_BYTES = 64_000
RESPONSE_LIMIT_HINT = (
    "\n\n[Response exceeded the safe inline size and could not be archived. "
    "Narrow fields or lower limit, and check META_EXPORT_DIR and the "
    "META_EXPORT_MAX_BYTES retention setting.]"
)

_settings = get_settings()
OVERFLOW_ARTIFACT_STORE = OverflowArtifactStore(
    _settings.export_directory,
    ttl_seconds=_settings.export_ttl_seconds,
    max_files=_settings.export_max_files,
    max_total_bytes=_settings.export_max_bytes,
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
        "and prefer ranked outputs when deciding what to optimize. When a "
        "response returns an overflow export_id, retrieve it in bounded chunks "
        "with read_overflow_artifact and delete it when finished."
    ),
    transforms=[TOOL_SEARCH_TRANSFORM],
    mask_error_details=False,
)
mcp_server.add_middleware(
    ArchivedResponseLimitingMiddleware(
        max_size=MAX_TOOL_RESPONSE_BYTES,
        truncation_suffix=RESPONSE_LIMIT_HINT,
        artifact_store=OVERFLOW_ARTIFACT_STORE,
    )
)
