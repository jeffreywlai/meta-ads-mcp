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
        r"\b(read|view|fetch|get|show|list|inspect|look(?:up)?|retrieve|details?|"
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
    _history_terms = re.compile(
        r"\b(history|activity|audit|changelog)\b|\bchange[\s_-]+log\b"
    )
    _mutation_verbs = re.compile(
        r"\b(?P<delete>delet(?:e|es|ing)|remov(?:e|es|ing))\b"
        r"|\b(?P<update>updat(?:e|es|ing)|edit(?:s|ing)?|"
        r"chang(?:e|es|ing)|modif(?:y|ies|ying)|"
        r"renam(?:e|es|ing))\b"
        r"|\b(?P<create>creat(?:e|es|ing))\b"
        r"|\b(?P<upload>upload(?:s|ing)?)\b"
        r"|\b(?P<preview>preview(?:s|ing)?)\b"
    )
    _negation_prefix = re.compile(
        r"\b(?:do\s+not|don['’]?t|dont|should\s+not|shouldn['’]?t|"
        r"would\s+not|wouldn['’]?t|can\s+not|cannot|can['’]?t|"
        r"will\s+not|won['’]?t|must\s+not|never|neither|no|not|without|"
        r"skip|skipping|omit(?:s|ting)?|exclud(?:e|es|ing)|"
        r"bypass(?:es|ing)?|forgo(?:es|ing)?|"
        r"abstain(?:s|ing)?\s+from|"
        r"ignor(?:e|es|ing)|disregard(?:s|ing)?|"
        r"do\s+(?:anything\s+but|everything\s+except)|"
        r"anything\s+other\s+than|leave\s+out|"
        r"avoid|avoiding|refrain(?:ing)?\s+from|"
        r"refus(?:e|es|ing)(?:\s+to)?|declin(?:e|es|ing)(?:\s+to)?|"
        r"keep(?:ing)?\s+from|prohibit(?:s|ing)?|rather\s+than)\b"
    )
    _generic_write_verbs = re.compile(
        r"\b(?P<delete>delet(?:e|es|ing)|remov(?:e|es|ing))\b"
        r"|\b(?P<update>updat(?:e|es|ing)|edit(?:s|ing)?|"
        r"chang(?:e|es|ing)|modif(?:y|ies|ying)|renam(?:e|es|ing))\b"
        r"|\b(?P<create>creat(?:e|es|ing))\b"
        r"|\b(?P<set>set(?:s|ting)?)\b"
        r"|\b(?P<transition>switch(?:es|ing)?|flip(?:s|ping)?|"
        r"transition(?:s|ing)?|mov(?:e|es|ing)|turn(?:s|ing)?|"
        r"mark(?:s|ing)?|mak(?:e|es|ing)|tak(?:e|es|ing)|"
        r"bring(?:s|ing)?|put(?:s|ting)?)\b"
        r"|\b(?P<status>paus(?:e|es|ing)|unpaus(?:e|es|ing)|"
        r"resum(?:e|es|ing)|"
        r"enabl(?:e|es|ing)|disabl(?:e|es|ing)|"
        r"start(?:s|ing)?|stop(?:s|ping)?|"
        r"halt(?:s|ing)?|launch(?:es|ing)?|"
        r"shut(?:s|ting)?(?:\s+down)?|"
        r"suspend(?:s|ing)?|begin(?:s|ning)?|end(?:s|ing)?|"
        r"activat(?:e|es|ing)|reactivat(?:e|es|ing)|"
        r"deactivat(?:e|es|ing))\b"
        r"|\b(?P<budget>increas(?:e|es|ing)|decreas(?:e|es|ing))\b"
    )
    _specialized_read_terms = re.compile(
        r"\b(performance|insights?|reports?|comments?|images?|"
        r"feedback|recommendations?|opportunities|pacing|fatigue|breakdowns?|"
        r"actions?)\b"
    )
    _entity_read_routes = (
        (
            "adset",
            re.compile(r"\bad[\s_-]*sets\b|\badsets\b"),
            "list_adsets",
        ),
        (
            "adset",
            re.compile(r"\bad[\s_-]*set\b|\badset\b"),
            "get_adset",
        ),
        ("campaign", re.compile(r"\bcampaigns\b"), "list_campaigns"),
        ("campaign", re.compile(r"\bcampaign\b"), "get_campaign"),
        ("audience", re.compile(r"\baudiences\b"), "list_audiences"),
        ("audience", re.compile(r"\baudience\b"), "get_audience"),
        (
            "account",
            re.compile(r"\bad\s+accounts\b|\baccounts\b"),
            "list_ad_accounts",
        ),
        (
            "account",
            re.compile(r"\bad\s+account\b|\baccount\b"),
            "get_ad_account",
        ),
        ("ad", re.compile(r"\bads\b"), "list_ads"),
        (
            "ad",
            re.compile(r"\bad\b(?![\s_-]*(?:sets?|accounts?))"),
            "get_ad",
        ),
    )
    _generic_mutation_tools = {
        "campaign": {
            "delete": "delete_campaign",
            "update": "update_campaign",
            "create": "create_campaign",
            "status": "set_campaign_status",
            "budget": "update_campaign_budget",
        },
        "audience": {
            "delete": "delete_audience",
            "update": "update_custom_audience",
            "create": "create_custom_audience",
        },
        "adset": {
            "create": "create_ad_set",
            "status": "set_adset_status",
            "budget": "update_adset_budget",
        },
        "ad": {
            "create": "create_ad",
            "status": "set_ad_status",
        },
    }
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
        selected = next(
            (tool for tool in tools if getattr(tool, "name", None) == name),
            None,
        )
        if selected is not None:
            return selected
        server = globals().get("mcp_server")
        components = getattr(
            getattr(server, "local_provider", None),
            "_components",
            {},
        )
        return components.get(f"tool:{name}@")

    def _match_is_negated(
        self,
        normalized: str,
        match: re.Match[str],
        verb_pattern: re.Pattern[str],
    ) -> bool:
        """Resolve negation within one clause, including coordinated verbs."""
        prefix = normalized[: match.start()]
        clause_start = max(
            (prefix.rfind(boundary) for boundary in ".!?;"),
            default=-1,
        )
        clause = prefix[clause_start + 1 :]
        negations = list(self._negation_prefix.finditer(clause))
        if not negations:
            return False
        negation = negations[-1]
        negated_phrase = clause[negation.end() :]
        prior_verbs = list(verb_pattern.finditer(negated_phrase))
        if not prior_verbs:
            if (
                "," in negated_phrase
                and self._read_terms.search(negated_phrase)
            ):
                return False
            if (
                negation.group().lower() in {"not", "no"}
                and (
                    "," in negated_phrase
                    or re.search(r"\b(?:and|but|then)\b", negated_phrase)
                )
            ):
                return False
            return True
        bridge = negated_phrase[prior_verbs[-1].end() :]
        if re.search(r"\b(?:or|nor)\b", bridge):
            return True
        if (
            negation.group().lower() in {"no", "skip", "skipping"}
            and re.fullmatch(r"\s*", bridge)
        ):
            return True
        return bool(
            negation.group().lower() != "not"
            and (
                re.search(
                    r"\b(?:and|as\s+well\s+as|plus|along\s+with|"
                    r"together\s+with|in\s+addition\s+to)\b",
                    bridge,
                )
                or re.fullmatch(r"\s*,\s*", bridge)
            )
        )

    def _has_positive_term(
        self,
        normalized: str,
        pattern: re.Pattern[str],
    ) -> bool:
        """Return whether a specialized read term is requested rather than negated."""
        for match in pattern.finditer(normalized):
            if self._match_is_negated(normalized, match, pattern):
                continue
            suffix = normalized[match.end() :]
            if re.match(
                r"\s+(?:(?:(?:is|are)\s+not|isn['’]?t|aren['’]?t|not)\s+"
                r"(?:needed|required|wanted|requested|desired|necessary)|"
                r"(?:is|are)\s+"
                r"(?:unnecessary|irrelevant|unwanted|immaterial)|"
                r"(?:does\s+not|doesn['’]?t)\s+matter)\b",
                suffix,
            ):
                continue
            return True
        return False

    @staticmethod
    def _targets_status(normalized: str, match: re.Match[str]) -> bool:
        """Recognize update/change/set wording whose target is entity status."""
        suffix = normalized[match.end() :]
        return bool(
            re.match(
                r"\s+(?:(?:the|its)\s+)?"
                r"(?:(?:campaign|ad[\s_-]*set|adset|ad)\s+)?"
                r"(?:(?:id(?:\s+number)?\s+)?#?[a-z0-9_-]+"
                r"(?:['’]s)?\s+)?"
                r"(?:(?:delivery|current|effective)\s+)?"
                r"(?:status\b|(?:(?:to|as)\s+)?(?:back\s+)?"
                r"(?:active|inactive|paused|enabled|disabled|on|off|"
                r"online|offline|running|stopped)\b)",
                suffix,
            )
        )

    def _entity_route_for_query(
        self,
        normalized: str,
        write_matches: Sequence[re.Match[str]],
    ) -> tuple[str, str] | None:
        """Choose the entity nearest the operative read or mutation verb."""
        candidates: list[tuple[int, int, int, str, str]] = []
        for priority, (
            entity_name,
            entity_pattern,
            read_tool,
        ) in enumerate(self._entity_read_routes):
            candidates.extend(
                (
                    entity_match.start(),
                    entity_match.end(),
                    priority,
                    entity_name,
                    read_tool,
                )
                for entity_match in entity_pattern.finditer(normalized)
        )
        if not candidates:
            return None
        positive_writes = [
            write_match
            for write_match in write_matches
            if (
                write_match.lastgroup not in {"set", "transition"}
                or self._targets_status(normalized, write_match)
            )
            and not self._match_is_negated(
                normalized,
                write_match,
                self._generic_write_verbs,
            )
        ]
        command = (
            positive_writes[0]
            if positive_writes
            else self._read_terms.search(normalized)
        )
        possessives = list(
            re.finditer(r"(?:['’]s\b|s['’](?=\s))", normalized)
        )
        if possessives:
            possessive = possessives[-1]
            command_bridge = (
                normalized[command.end() : possessive.start()]
                if command is not None and command.end() <= possessive.start()
                else ""
            )
            is_modifier_reference = bool(
                re.search(
                    r"\b(?:using|according\s+to|based\s+on|with|per|under|"
                    r"following|pursuant\s+to|in\s+accordance\s+with|"
                    r"subject\s+to|governed\s+by|constrained\s+by|"
                    r"irrespective\s+of|while\s+honoring|"
                    r"as\s+dictated\s+by|despite)\b",
                    command_bridge,
                )
            )
            bridge_entities = {
                (candidate[0], candidate[1], candidate[3])
                for candidate in candidates
                if (
                    command is not None
                    and candidate[0] >= command.end()
                    and candidate[1] <= possessive.start()
                )
            }
            is_modifier_reference = (
                is_modifier_reference or len(bridge_entities) > 1
            )
            if not is_modifier_reference:
                possessive_end = possessive.end()
                possessed_candidates = [
                    candidate
                    for candidate in candidates
                    if candidate[0] >= possessive_end
                ]
                if possessed_candidates:
                    selected = min(
                        possessed_candidates,
                        key=lambda candidate: (
                            candidate[0] - possessive_end,
                            candidate[2],
                        ),
                    )
                    return selected[3], selected[4]
        if command is None:
            selected = min(candidates, key=lambda candidate: candidate[2])
        else:
            following = [
                candidate
                for candidate in candidates
                if candidate[0] >= command.end()
            ]
            if following:
                selected = min(
                    following,
                    key=lambda candidate: (
                        candidate[0] - command.end(),
                        candidate[2],
                    ),
                )
            else:
                selected = min(
                    candidates,
                    key=lambda candidate: (
                        abs(command.start() - candidate[1]),
                        candidate[2],
                    ),
                )
        return selected[3], selected[4]

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
            destructive_match = re.search(
                r"\b(delete|remove|discard|purge)\b",
                normalized,
            )
            destructive_is_negated = bool(
                destructive_match
                and self._match_is_negated(
                    normalized,
                    destructive_match,
                    re.compile(r"\b(?:delete|remove|discard|purge)\b"),
                )
            )
            if destructive_match and not destructive_is_negated:
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
        creative_query = bool(re.search(r"\bcreatives?\b", normalized))
        creative_mutation_matches = list(
            self._mutation_verbs.finditer(normalized)
        )
        positive_creative_mutation = any(
            not self._match_is_negated(
                normalized,
                mutation_match,
                self._mutation_verbs,
            )
            for mutation_match in creative_mutation_matches
        )
        comment_terms = re.compile(r"\bcomments?\b")
        reply_terms = re.compile(r"\brepl(?:y|ies)\b")
        comment_query = self._has_positive_term(normalized, comment_terms)
        reply_term = self._has_positive_term(normalized, reply_terms)
        reply_pagination = bool(
            re.search(
                r"\b(?:continue|next|more|remaining|cursor|after|page|"
                r"batch|forward)\b",
                normalized,
            )
        )
        reply_query = bool(
            reply_term and (comment_query or reply_pagination)
        )
        parent_comment_continuation = bool(
            comment_query
            and re.search(
                r"\b(?:(?:continue|more|remaining)\s+|"
                r"(?:fetch\s+)?(?:next|second|subsequent|another)"
                r"(?:\s+(?:page|batch))?(?:\s+of)?\s+|"
                r"page\s+(?:#?\d+\s+)?"
                r"(?:(?:forward\s+)?through|of)\s+)"
                r"(?:(?:post|page(?:[-\s]+post)?|"
                r"facebook(?:\s+(?:page|post))?|"
                r"instagram(?:\s+media)?|ad)\s+)?"
                r"comments\b",
                normalized,
            )
        )
        if parent_comment_continuation:
            return self._prioritize(
                tools,
                ranked,
                "list_ad_comments",
                mutation_safe=True,
            )
        if comment_query and reply_term and re.search(
            r"\bcomments\b",
            normalized,
        ):
            return self._prioritize(
                tools,
                ranked,
                "list_ad_comments",
                mutation_safe=True,
            )
        if reply_query:
            explicit_comment_replies = bool(
                re.search(
                    r"\bcomment(?:(?:['’]s)|(?:\s+(?:id\s+)?"
                    r"[a-z0-9_-]*\d[a-z0-9_-]*(?:['’]s)?))?"
                    r"\s+repl(?:y|ies)\b",
                    normalized,
                )
                or re.search(
                    r"\brepl(?:y|ies)\b.*\b(?:beneath|under|to|for|on)\b"
                    r".*\bcomments?\b",
                    normalized,
                )
                or re.search(
                    r"\bcomment\b.*\b(?:and|with)\s+(?:all\s+)?"
                    r"(?:(?:of\s+)?(?:its|the)\s+)?repl(?:y|ies)\b",
                    normalized,
                )
            )
            parent_ad_comments = bool(
                (
                    re.search(
                        r"\b(?:ad|facebook(?:\s+page)?|page(?:[-\s]+post)?|"
                        r"instagram(?:\s+media)?|post)\b.*\bcomments\b",
                        normalized,
                    )
                    or re.search(
                        r"\bcomments\b.*\b(?:on|from|for)\s+(?:the\s+)?"
                        r"(?:ad|facebook(?:\s+page)?|page(?:[-\s]+post)?|"
                        r"instagram(?:\s+media)?|post)\b",
                        normalized,
                    )
                )
                and not explicit_comment_replies
            )
            reply_continuation = bool(
                re.search(
                    r"\b(?:continue|next|more|remaining|cursor|after|page)\b",
                    normalized,
                )
                or re.search(
                    r"\brepl(?:y|ies)\s+(?:to|for|on)\s+(?:a\s+)?comment\b",
                    normalized,
                )
            )
            return self._prioritize(
                tools,
                ranked,
                (
                    "list_comment_replies"
                    if (
                        explicit_comment_replies
                        or (
                            reply_continuation
                            and not parent_ad_comments
                            and not parent_comment_continuation
                        )
                    )
                    else "list_ad_comments"
                ),
                mutation_safe=True,
            )
        if (
            creative_query
            and (
                self._read_terms.search(normalized)
                or re.search(
                    r"\bcreative\b.*\b(?:for|from|attached\s+to|used\s+by|"
                    r"linked\s+to|associated\s+with|belonging\s+to|"
                    r"referenced\s+by|connected\s+to|served\s+by|"
                    r"delivered\s+by|selected\s+by|tied\s+to|paired\s+with|"
                    r"embedded\s+in|driving|powering|behind|through|on)\s+"
                    r"(?:the\s+)?(?:ad|advert(?:isement)?)\b",
                    normalized,
                )
                or re.search(
                    r"\bcreative\b.*\b(?:that\s+)?"
                    r"(?:ad|advert(?:isement)?)\s+[a-z0-9_-]+\s+"
                    r"(?:uses|references)\b",
                    normalized,
                )
                or re.search(
                    r"\b(?:ad|advert(?:isement)?)\s+[a-z0-9_-]+\s+"
                    r"creative\b",
                    normalized,
                )
                or re.search(
                    r"\b(?:ad|advert(?:isement)?)\s+"
                    r"(?:id\s+)?#?[a-z0-9_-]*\d[a-z0-9_-]*\b",
                    normalized,
                )
            )
            and not positive_creative_mutation
            and not self._has_positive_term(normalized, self._analysis_terms)
            and not self._has_positive_term(
                normalized,
                re.compile(r"\binsights?\b"),
            )
            and not self._has_positive_term(
                normalized,
                re.compile(r"\b(?:comments?|images?)\b"),
            )
        ):
            direct_ad_creative_read = re.search(
                r"\b(?:list|show|get|read|fetch|inspect|retrieve)\s+"
                r"(?:the\s+)?(?:ad|advert(?:isement)?)\s+creatives?\b",
                normalized,
            )
            ad_relation_after_creative = re.search(
                r"\b(?:ad|advert(?:isement)?)\s+creative\b.*\b"
                r"(?:for|from|attached\s+to|"
                r"used\s+by|linked\s+to|associated\s+with|belonging\s+to|"
                r"referenced\s+by|connected\s+to|served\s+by|behind|through|"
                r"delivered\s+by|selected\s+by|tied\s+to|paired\s+with|"
                r"embedded\s+in|driving|powering|on)\s+"
                r"(?:the\s+)?(?:ad|advert(?:isement)?)\b",
                normalized,
            )
            if direct_ad_creative_read and not ad_relation_after_creative:
                return self._prioritize(
                    tools,
                    ranked,
                    (
                        "list_creatives"
                        if direct_ad_creative_read.group().endswith("creatives")
                        else "get_creative"
                    ),
                    mutation_safe=True,
                )
            if re.search(
                r"\b(?:list|show|get|read|fetch|inspect)\s+(?:the\s+)?"
                r"(?:ads?|adverts?|advertisements?)\b",
                normalized,
            ) or re.search(
                r"\bcreative\b.*\b(?:for|from|attached\s+to|used\s+by|"
                r"linked\s+to|associated\s+with|belonging\s+to|"
                r"referenced\s+by|connected\s+to|served\s+by|behind|through|"
                r"delivered\s+by|selected\s+by|tied\s+to|paired\s+with|"
                r"embedded\s+in|driving|powering|on)\s+"
                r"(?:the\s+)?(?:ad|advert(?:isement)?)\b",
                normalized,
            ) or re.search(
                r"\bcreative\b.*\b(?:that\s+)?"
                r"(?:ad|advert(?:isement)?)\s+[a-z0-9_-]+\s+"
                r"(?:uses|references)\b",
                normalized,
            ) or re.search(
                r"\b(?:ads?|adverts?|advertisements?)"
                r"(?:\s+[a-z0-9_-]+)?(?:['’]s|s['’])\s+"
                r"(?:attached\s+)?creative\b",
                normalized,
            ) or re.search(
                r"\b(?:ad|advert(?:isement)?)\s+[a-z0-9_-]+\s+"
                r"creative\b",
                normalized,
            ) or re.search(
                r"\b(?:ad|advert(?:isement)?)\s+"
                r"(?:id\s+)?#?[a-z0-9_-]*\d[a-z0-9_-]*\b",
                normalized,
            ):
                relation_tool = (
                    "list_ads"
                    if re.search(
                        r"\b(?:list|show|get|read|fetch|inspect)\s+"
                        r"(?:the\s+)?(?:ads|adverts|advertisements)\b",
                        normalized,
                    )
                    or re.search(
                        r"\b(?:ads|adverts|advertisements)"
                        r"(?:['’]s|s['’])\b",
                        normalized,
                    )
                    else "get_ad"
                )
                return self._prioritize(
                    tools,
                    ranked,
                    relation_tool,
                    mutation_safe=True,
                )
        generic_write_matches = list(
            self._generic_write_verbs.finditer(normalized)
        )
        entity_route = self._entity_route_for_query(
            normalized,
            generic_write_matches,
        )
        if not creative_query and entity_route is not None:
            entity_name, read_tool = entity_route
            if self._has_positive_term(normalized, self._history_terms):
                return self._prioritize(
                    tools,
                    ranked,
                    "list_change_history",
                    mutation_safe=True,
                )
            if self._has_positive_term(
                normalized,
                re.compile(r"\b(?:performance|insights?)\b"),
            ):
                analysis_tool = (
                    "get_audience_performance_report"
                    if entity_name == "audience"
                    else "get_entity_insights"
                )
                return self._prioritize(
                    tools,
                    ranked,
                    analysis_tool,
                    mutation_safe=True,
                )
        if (
            not creative_query
            and entity_route is not None
            and not self._has_positive_term(
                normalized,
                self._specialized_read_terms,
            )
        ):
            negated_categories: set[str] = set()
            positive_categories: list[str] = []
            for match in generic_write_matches:
                category = match.lastgroup or ""
                if category in {"update", "set", "transition"} and self._targets_status(
                    normalized,
                    match,
                ):
                    category = "status"
                elif category in {"set", "transition"}:
                    continue
                if self._match_is_negated(
                    normalized,
                    match,
                    self._generic_write_verbs,
                ):
                    negated_categories.add(category)
                else:
                    positive_categories.append(category)
            read_intent = bool(
                self._read_terms.search(normalized)
                or self._read_only_phrase.search(normalized)
                or negated_categories
                or read_tool.startswith("list_")
            )
            entity_name, read_tool = entity_route
            mutation_routes = self._generic_mutation_tools.get(entity_name, {})
            excluded_names = {
                mutation_routes[category]
                for category in negated_categories
                if category in mutation_routes
            }
            if positive_categories:
                selected_mutation = next(
                    (
                        mutation_routes[category]
                        for category in positive_categories
                        if category in mutation_routes
                    ),
                    "",
                )
                return self._prioritize(
                    tools,
                    ranked,
                    selected_mutation,
                    excluded_names=excluded_names,
                )
            if read_intent:
                return self._prioritize(
                    tools,
                    ranked,
                    read_tool,
                    mutation_safe=True,
                )
        if not creative_query:
            return ranked

        negated_mutation = bool(self._read_only_phrase.search(normalized))
        negated_tools: set[str] = set()
        positive_tools: list[str] = []
        if self._has_positive_term(normalized, self._history_terms):
            return self._prioritize(
                tools,
                ranked,
                "list_change_history",
                mutation_safe=True,
            )
        for match in creative_mutation_matches:
            tool_name = self._mutation_tools[match.lastgroup or ""]
            if self._match_is_negated(
                normalized,
                match,
                self._mutation_verbs,
            ):
                negated_mutation = True
                negated_tools.add(tool_name)
                continue
            if match.lastgroup == "delete" and match.group().startswith("remov"):
                if re.search(
                    r"\bremov\w*\s+(?:the\s+)?"
                    r"(?:(?:ad|advert(?:isement)?)\s+)?creative\b",
                    normalized,
                ):
                    if re.search(
                        r"\bremov\w*\s+(?:the\s+)?"
                        r"(?:(?:ad|advert(?:isement)?)\s+)?creative\s+from\b",
                        normalized,
                    ):
                        continue
                elif re.search(r"\bremov\w*\s+it\b", normalized):
                    if re.search(r"\bimages?\b", normalized):
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
        if self._has_positive_term(normalized, re.compile(r"\bfatigue\b")):
            return self._prioritize(
                tools,
                ranked,
                "get_creative_fatigue_report",
                mutation_safe=True,
            )
        if self._has_positive_term(
            normalized,
            re.compile(r"\b(?:insights?|performance)\b"),
        ):
            return self._prioritize(
                tools,
                ranked,
                "get_creative_performance_report",
                mutation_safe=True,
            )
        if self._has_positive_term(
            normalized,
            self._analysis_terms,
        ) and not self._has_positive_term(normalized, self._creative_spec_terms):
            return ranked
        if comment_query:
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
        if self._has_positive_term(
            normalized,
            self._adjacent_creative_surface,
        ):
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
