"""Small, deterministic safety layer for FastMCP tool search.

This module is deliberately not a general natural-language parser.  It gives
the BM25 search coordinator three guarantees:

* an exact public tool name selects that tool;
* a small set of direct English commands selects deterministic routes; and
* everything else is read-safe and left to BM25 for ranking.

FastMCP remains the coordinator and the source of truth for tool schemas.  An
exact call-like request is accepted only when its literal keyword arguments
validate against the live schema supplied by the coordinator.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from meta_ads_mcp.tool_contracts import ToolContract

MAX_QUERY_CHARS = 4_096


class Effect(str, Enum):
    """The externally observable effect of a tool."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class RouteFrame:
    """Small diagnostic view of what the finite router recognized."""

    normalized: str
    read_requested: bool
    named_tools: tuple[str, ...]
    invalid_exact_call: bool = False


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Preferred tools and the hard candidate filter used by the coordinator."""

    frame: RouteFrame
    preferred_tool: str | None
    additional_preferred_tools: tuple[str, ...]
    compatible_tools: frozenset[str]
    excluded_tools: frozenset[str]
    suppress_mutations: bool


_DIRECT_NEGATION = re.compile(r"^(?:do\s+not|don't|dont|never)\b\s*")
_OPTIONAL_POLITENESS = re.compile(r"^(?:(?:please|kindly)\s+)+")
_CANONICAL_INTRO = re.compile(r"^(?:(?:call|use|run|find\s+tool)\s+)")
_EXACT_CALL = re.compile(
    r"^(?:(?:call|use|run)\s+)?(?P<name>[a-z][a-z0-9_]*)\s*(?P<call>\(.*\))\s*$",
    re.DOTALL,
)
_READ_VERBS = frozenset(
    {
        "aggregate",
        "compare",
        "detect",
        "estimate",
        "exchange",
        "export",
        "fetch",
        "find",
        "generate",
        "get",
        "inspect",
        "list",
        "preview",
        "read",
        "refresh",
        "search",
        "show",
        "summarize",
        "validate",
        "view",
    }
)
_WRITE_VERBS = frozenset(
    {
        "create",
        "delete",
        "disable",
        "enable",
        "pause",
        "resume",
        "set",
        "setup",
        "update",
        "upload",
    }
)
_GET_SYNONYMS = frozenset({"fetch", "find", "inspect", "read", "show", "view"})
_ARTICLES = frozenset({"a", "an", "the"})

_ENTITY_GETTERS = {
    "account": "get_ad_account",
    "ad account": "get_ad_account",
    "ad": "get_ad",
    "ad set": "get_adset",
    "adset": "get_adset",
    "audience": "get_audience",
    "campaign": "get_campaign",
    "creative": "get_creative",
}
_ENTITY_LISTERS = {
    "accounts": "list_ad_accounts",
    "ad accounts": "list_ad_accounts",
    "ads": "list_ads",
    "ad sets": "list_adsets",
    "adsets": "list_adsets",
    "audiences": "list_audiences",
    "campaigns": "list_campaigns",
    "creatives": "list_creatives",
}
_STATUS_ALIASES = {
    "ad": "set_ad_status",
    "ad set": "set_adset_status",
    "adset": "set_adset_status",
    "campaign": "set_campaign_status",
}
_BUDGET_ALIASES = {
    "ad set": "update_adset_budget",
    "adset": "update_adset_budget",
    "campaign": "update_campaign_budget",
}
_CREATE_ALIASES = {
    "creative": "create_ad_creative",
}
_CONFIGURATION_TOKEN = re.compile(
    r"\b(?P<entity>ad set|adset|campaign)\b|"
    r"\b(?P<facet>daily budget|lifetime budget|bid amount|bid strategy|status|budget)\b"
)
_CONFIGURATION_CONTINUATION = re.compile(
    r"^\s*(?:(?:and(?:\s+then)?|or|then)\s+)?"
    r"(?:(?P<entity>ad set|adset|campaign)\s+)?"
    r"(?:daily budget|lifetime budget|budget|bid amount|bid strategy|status)\b",
    re.IGNORECASE,
)
_EXCLUDED_SUFFIX = re.compile(
    r"\b(?:without|except|not|never|don't|dont|do not|to\s+match|based\s+on|"
    r"but\s+(?:only\s+)?(?:show|read|list|get|inspect|compare|view))\b",
    re.IGNORECASE,
)


def normalize_query(query: str) -> str:
    """Normalize the finite command surface without semantic rewriting."""
    return " ".join(query.strip().lower().split())


def _compact_query(query: str) -> str:
    """Normalize whitespace while preserving literal value case."""
    return " ".join(query.strip().split())


def _operation_clauses(query: str) -> tuple[str, ...]:
    """Split a command list outside literals and exact-call arguments."""
    clauses: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    qualifier_scope = False
    matching = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(query):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "([{":
            stack.append(character)
        elif character in ")]}" and stack and stack[-1] == matching[character]:
            stack.pop()
        elif not stack and _EXCLUDED_SUFFIX.match(query, index):
            qualifier_scope = True
        elif character in ";\n," and not stack:
            if character == ",":
                continuation = _CONFIGURATION_CONTINUATION.match(query[index + 1 :])
                if qualifier_scope or (
                    continuation is not None and continuation.group("entity") is None
                ):
                    continue
            clauses.append(query[start:index])
            start = index + 1
            qualifier_scope = False
        elif character.isspace() and not stack:
            if qualifier_scope:
                continue
            separator = re.match(
                r"\s+(?:and(?:\s+then)?|then)\s+", query[index:], re.IGNORECASE
            )
            if separator is not None:
                continuation = _CONFIGURATION_CONTINUATION.match(query[index + separator.end() :])
                if continuation is not None and continuation.group("entity") is None:
                    continue
                clauses.append(query[start:index])
                start = index + separator.end()
    clauses.append(query[start:])
    return tuple(clauses)


def _configuration_routes(text: str, *, allow_shorthand: bool) -> tuple[str, ...]:
    """Recognize a bounded budget/bid/status list, never arbitrary write prose."""
    simplified = _strip_articles(text.replace("-", " ").split())
    verb, _space, tail = simplified.partition(" ")
    if verb not in {"update", "set"}:
        if not allow_shorthand:
            return ()
        tail = simplified
    # Negative, reference, and read-only qualifiers are outside the changes.
    tail = _EXCLUDED_SUFFIX.split(tail, maxsplit=1)[0].strip()
    if not _CONFIGURATION_CONTINUATION.match(tail):
        return ()
    # A compact list contains adjacent configuration terms and list separators.
    # Stop at prose such as "to match", "based on", or "only show" so a
    # referenced configuration cannot become a second requested change.
    tokens = []
    previous_end = 0
    for token in _CONFIGURATION_TOKEN.finditer(tail):
        gap = tail[previous_end:token.start()].replace(",", " ").split()
        if any(word not in {"and", "or", "then"} for word in gap):
            break
        tokens.append(token)
        previous_end = token.end()
    entities = tuple(
        dict.fromkeys(
            token.group("entity").replace("ad set", "adset")
            for token in tokens if token.group("entity")
        )
    )
    entity: str | None = None
    explicitly_scoped = False
    routes: list[str] = []
    for token in tokens:
        if raw_entity := token.group("entity"):
            entity = raw_entity.replace("ad set", "adset")
            explicitly_scoped = True
            continue
        facet = token.group("facet")
        scopes = (entity,) if entity is not None else ()
        if facet in {"bid amount", "bid strategy"} and not scopes:
            scopes = ("adset",)
        if facet == "bid strategy" and not explicitly_scoped and len(entities) > 1:
            # An unqualified strategy in a compact multi-entity search is
            # ambiguous: expose both typed tools rather than lose one.
            scopes = entities
        for scope in scopes:
            if facet == "status":
                routes.append(f"set_{scope}_status")
            elif facet == "bid amount":
                routes.append(f"update_{scope}_bid_amount")
            elif facet == "bid strategy":
                routes.append(f"update_{scope}_bid_strategy")
            else:
                routes.append(f"update_{scope}_budget")
        explicitly_scoped = False
    return tuple(dict.fromkeys(routes))


def _workflow_route(text: str) -> str | None:
    """Small aliases for the creative-replacement and saved-report workflows."""
    if re.match(r"^(?:get|fetch|find|inspect|read|show|view) ad creative\b", text):
        return "get_creative"
    if re.match(r"^update ad creative\b", text):
        return "update_ad_creative"
    if re.match(r"^upload (?:an? )?image\b", text):
        return "upload_creative_asset"
    if re.match(r"^(?:summarize|aggregate)\b", text) and re.search(
        r"\b(?:async(?: insights)? report|report_run_id|report run id)\b", text
    ):
        return "summarize_async_insights_report"
    return None


def _effect_from_contract(contract: ToolContract) -> Effect | None:
    raw = getattr(contract, "effect", None)
    if isinstance(raw, Effect):
        return raw
    value = getattr(raw, "value", raw)
    if value in {"read", "write"}:
        return Effect(value)
    return None


def _is_mutation(name: str, contracts: Mapping[str, ToolContract]) -> bool:
    contract = contracts.get(name)
    if contract is None:
        return False
    return _effect_from_contract(contract) is Effect.WRITE


def _is_json_value(value: object) -> bool:
    """Accept only values representable by an MCP JSON argument."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False


def _literal_kwargs(call_text: str, expected_name: str) -> dict[str, object] | None:
    """Parse one direct Python-like call without executing input."""
    try:
        expression = ast.parse(f"{expected_name}{call_text}", mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    if not isinstance(expression, ast.Call) or not isinstance(expression.func, ast.Name):
        return None
    if expression.func.id != expected_name or expression.args:
        return None
    result: dict[str, object] = {}
    for keyword in expression.keywords:
        if keyword.arg is None or keyword.arg in result:
            return None
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, MemoryError, RecursionError):
            return None
        if not _is_json_value(value):
            return None
        result[keyword.arg] = value
    return result


def _valid_exact_call(contract: ToolContract, kwargs: Mapping[str, object]) -> bool:
    validator = getattr(contract, "validator", None)
    if validator is None:
        return False
    try:
        return validator.is_valid(dict(kwargs))
    except (TypeError, ValueError, RecursionError):
        return False


def _strip_articles(words: Iterable[str]) -> str:
    return " ".join(word for word in words if word not in _ARTICLES)


def _canonical_surface_route(text: str, names: frozenset[str]) -> str | None:
    """Match the longest direct command against a canonical tool surface."""
    simplified = _strip_articles(text.replace("-", " ").replace("/", " ").split())
    surfaces = {
        name: tuple(
            dict.fromkeys(
                (
                    name.replace("_", " "),
                    name.replace("_", " ").replace("adset", "ad set"),
                )
            )
        )
        for name in names
    }
    candidates = sorted(
        names,
        key=lambda item: (-max(len(surface) for surface in surfaces[item]), item),
    )
    for name in candidates:
        for surface in surfaces[name]:
            if simplified == surface or simplified.startswith(f"{surface} "):
                return name

    words = simplified.split()
    if not words:
        return None
    verb, tail = words[0], " ".join(words[1:])
    if verb in _GET_SYNONYMS:
        for name in candidates:
            for surface in surfaces[name]:
                if not surface.startswith(("get ", "list ")):
                    continue
                canonical_tail = surface.split(" ", 1)[1]
                if tail == canonical_tail or tail.startswith(f"{canonical_tail} "):
                    return name
    return None


def _entity_route(text: str) -> str | None:
    words = _strip_articles(text.replace("-", " ").split()).split()
    if len(words) < 2:
        return None
    verb = words[0]
    tail = " ".join(words[1:])
    if verb in _GET_SYNONYMS | {"get"}:
        for phrase, name in sorted(
            {**_ENTITY_GETTERS, **_ENTITY_LISTERS}.items(),
            key=lambda item: -len(item[0]),
        ):
            if tail == phrase or tail.startswith(f"{phrase} "):
                return name
    if verb == "list":
        for phrase, name in sorted(_ENTITY_LISTERS.items(), key=lambda item: -len(item[0])):
            if tail == phrase or tail.startswith(f"{phrase} "):
                return name
    if verb == "create":
        for phrase, name in sorted(_CREATE_ALIASES.items(), key=lambda item: -len(item[0])):
            if tail == phrase or tail.startswith(f"{phrase} "):
                return name
    if verb in {"pause", "resume", "enable", "disable"}:
        for phrase, name in sorted(_STATUS_ALIASES.items(), key=lambda item: -len(item[0])):
            if tail == phrase or tail.startswith(f"{phrase} "):
                return name
    if verb == "set":
        for phrase, name in sorted(_STATUS_ALIASES.items(), key=lambda item: -len(item[0])):
            if tail.startswith(f"{phrase} status"):
                return name
        for phrase, name in sorted(_BUDGET_ALIASES.items(), key=lambda item: -len(item[0])):
            if tail.startswith(f"{phrase} budget") or tail.startswith(f"{phrase} daily budget"):
                return name
    return None


def _bare_canonical_names(text: str, names: frozenset[str]) -> tuple[str, ...]:
    """Return live underscore identifiers in source order."""
    matches: list[tuple[int, str]] = []
    for name in names:
        match = re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text)
        if match is not None:
            matches.append((match.start(), name))
    return tuple(name for _start, name in sorted(matches))


class StructuredIntentRouter:
    """Route only finite direct commands; leave unsupported prose to BM25."""

    def __init__(
        self,
        *,
        tool_contracts: Mapping[str, ToolContract] | None = None,
    ) -> None:
        self._tool_contracts = tool_contracts

    def decide(
        self,
        query: str,
        *,
        tool_contracts: Mapping[str, ToolContract] | None = None,
    ) -> RouteDecision:
        contracts = dict(
            tool_contracts
            if tool_contracts is not None
            else self._tool_contracts or {}
        )
        names = frozenset(contracts)
        read_names = frozenset(
            name for name in names if not _is_mutation(name, contracts)
        )
        mutation_names = names - read_names
        normalized = normalize_query(query[:MAX_QUERY_CHARS])
        if len(query) > MAX_QUERY_CHARS:
            return self._decision(
                normalized,
                (),
                read_names,
                (),
                False,
                mutation_names,
            )

        preferred: list[str] = []
        verb_scoped_candidates: set[str] = set()
        excluded: set[str] = set()
        invalid_exact = False
        clauses = _operation_clauses(query)
        allow_shorthand = len(clauses) > 1 and any(
            re.match(
                r"^(?:update|set|pause|resume|enable|disable)\b",
                _OPTIONAL_POLITENESS.sub("", normalize_query(clause)),
            )
            for clause in clauses
        )
        for raw_clause in clauses:
            literal_clause = _compact_query(raw_clause)
            clause = literal_clause.lower()
            politeness = _OPTIONAL_POLITENESS.match(clause)
            if politeness is not None:
                literal_clause = literal_clause[politeness.end() :]
                clause = clause[politeness.end() :]
            if not clause:
                continue
            negated = _DIRECT_NEGATION.match(clause)
            if negated is not None:
                literal_clause = literal_clause[negated.end() :].strip()
                clause = clause[negated.end() :].strip()

            exact = _EXACT_CALL.fullmatch(literal_clause)
            if exact is not None and exact.group("name") in names:
                name = exact.group("name")
                contract = contracts.get(name)
                kwargs = _literal_kwargs(exact.group("call"), name)
                valid = (
                    contract is not None
                    and kwargs is not None
                    and _valid_exact_call(contract, kwargs)
                )
                if not valid:
                    invalid_exact = True
                    excluded.add(name)
                    continue
                if negated is not None:
                    excluded.add(name)
                else:
                    preferred.append(name)
                continue

            call_name = re.match(
                r"^(?:(?:call|use|run)\s+)?(?P<name>[a-z][a-z0-9_]*)\s*\(",
                clause,
            )
            if call_name is not None and call_name.group("name") in names:
                invalid_exact = True
                excluded.add(call_name.group("name"))
                continue

            canonical_text = _CANONICAL_INTRO.sub("", clause)
            explicit = _bare_canonical_names(canonical_text, names)
            if explicit:
                if negated is not None:
                    excluded.update(explicit)
                else:
                    preferred.extend(explicit)
                continue

            configuration_routes = tuple(
                name for name in _configuration_routes(
                    clause, allow_shorthand=allow_shorthand
                )
                if name in names
            )
            if configuration_routes:
                if negated is not None:
                    excluded.update(configuration_routes)
                else:
                    preferred.extend(configuration_routes)
                continue

            direct_verb = clause.split(" ", 1)[0]
            if direct_verb not in _READ_VERBS | _WRITE_VERBS:
                continue
            route = (
                _workflow_route(clause)
                or _canonical_surface_route(clause, names)
                or _entity_route(clause)
            )
            if route is None or route not in names:
                if negated is None and direct_verb in _WRITE_VERBS:
                    verb_scoped_candidates.update(
                        name
                        for name in names
                        if name.startswith(f"{direct_verb}_")
                    )
                continue
            if negated is not None:
                excluded.add(route)
            else:
                preferred.append(route)

        preferred = list(dict.fromkeys(name for name in preferred if name not in excluded))
        if invalid_exact and not preferred:
            compatible = frozenset()
        elif preferred:
            compatible = frozenset(preferred)
        elif verb_scoped_candidates:
            compatible = frozenset(verb_scoped_candidates) - excluded
        else:
            compatible = read_names - excluded
        return self._decision(
            normalized,
            tuple(preferred),
            compatible,
            tuple(sorted(excluded)),
            invalid_exact,
            mutation_names,
        )

    @staticmethod
    def _decision(
        normalized: str,
        preferred: tuple[str, ...],
        compatible: frozenset[str],
        excluded: tuple[str, ...],
        invalid_exact: bool,
        mutation_names: frozenset[str],
    ) -> RouteDecision:
        first = preferred[0] if preferred else None
        named = preferred + tuple(name for name in excluded if name not in preferred)
        return RouteDecision(
            frame=RouteFrame(
                normalized=normalized,
                read_requested=bool(preferred) or bool(compatible),
                named_tools=named,
                invalid_exact_call=invalid_exact,
            ),
            preferred_tool=first,
            additional_preferred_tools=preferred[1:],
            compatible_tools=compatible,
            excluded_tools=frozenset(excluded),
            suppress_mutations=not any(
                name in mutation_names for name in compatible
            ),
        )


def is_compatible_name(name: str, decision: RouteDecision) -> bool:
    """Return whether one live tool can participate in this decision."""
    return name not in decision.excluded_tools and name in decision.compatible_tools


def filter_compatible_names(
    names: Iterable[str],
    decision: RouteDecision,
) -> list[str]:
    """Filter ranked names using the production compatibility predicate."""
    return [name for name in names if is_compatible_name(name, decision)]
