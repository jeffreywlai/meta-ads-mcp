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
# Verbs that also have everyday read meanings ("add up the spend"). A request
# that starts with one of these offers a write tool only when it also names
# something the tool acts on, so the verb alone never selects a mutation.
_SUBJECT_SCOPED_WRITE_VERBS = frozenset(
    {
        "add",
        "boost",
        "cancel",
        "batch",
        "clone",
        "connect",
        "copy",
        "disconnect",
        "duplicate",
        "remove",
    }
)
_WRITE_VERBS = _WRITE_VERBS | _SUBJECT_SCOPED_WRITE_VERBS
# A request verb that names tools under a different prefix.
_WRITE_VERB_TOOL_PREFIX = {"clone": "duplicate", "copy": "duplicate"}
# "batch" is also an everyday noun ("a batch report on the catalog"), so it must name
# two things the tool acts on before it offers a write.
_SUBJECT_WORDS_REQUIRED = {"batch": 2}
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
_CONFIGURATION_FACETS = (
    r"daily budget|lifetime budget|bid amount|bid strategy|status|budget|name|objective"
)
_CONFIGURATION_TOKEN = re.compile(
    r"\b(?P<entity>ad set|adset|campaign)\b|"
    rf"\b(?P<facet>{_CONFIGURATION_FACETS})\b"
)
_CONFIGURATION_CONTINUATION = re.compile(
    r"^\s*(?:(?:and(?:\s+then)?|or|then)\s+)?"
    r"(?:(?P<entity>ad set|adset|campaign)\s+)?"
    rf"(?:{_CONFIGURATION_FACETS})\b",
    re.IGNORECASE,
)
_PRESERVATION = (
    r"(?:(?:must|should|shall|will|would|can|could|may|might|need|do|does|did|"
    r"is|are|was|were|has|have)\s+(?:not|never)|cannot|"
    r"(?:mustn|shouldn|shan|won|wouldn|can|couldn|mightn|don|doesn|didn|isn|aren|wasn|weren)'t)\b|"
    r"(?:(?:(?:must|should|shall|will|would|may|might|needs?\s+to)\s+)?"
    r"(?:remain(?:s)?|stay(?:s)?|be|is|are|was|were)\s+)?(?:unchanged|(?:the\s+)?same)\b"
)
_POSTFIX_PRESERVATION = re.compile(rf"^[\s,:]*(?:{_PRESERVATION})", re.IGNORECASE)
_EXCLUDED_SUFFIX = re.compile(
    rf"\b(?:{_PRESERVATION}|without|except|not|never|don't|dont|do not|to\s+match|based\s+on|"
    r"but\s+(?:only\s+)?(?:show|read|list|get|inspect|compare|view))\b",
    re.IGNORECASE,
)
_QUOTED_VALUE = r'''(?:"(?:\\.|[^"\\])*"|(?<!\w)'(?:\\.|[^'\\])*')'''
_CONFIGURATION_VALUES = {
    **dict.fromkeys(
        ("budget", "daily budget", "lifetime budget", "bid amount"),
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
    ),
    "status": r"(?:active|paused|archived|deleted)",
    # Strategies are strings in the live schema. Accept enum-shaped tokens or
    # quoted strings without interpreting their contents as another field.
    "bid strategy": rf"(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)+|{_QUOTED_VALUE})",
    "name": _QUOTED_VALUE,
    "objective": rf"(?:outcome_[a-z_]+|{_QUOTED_VALUE})",
}
_CONFIGURATION_SEPARATOR = re.compile(
    r"^[\s,]*(?:(?:and(?:\s+then)?|or|then)[\s,]*)?$"
)
_STATUS_VERBS = frozenset({"pause", "resume", "enable", "disable"})
_EXPLICIT_OPERATION = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)*(?:"
    + "|".join(sorted(_READ_VERBS | _WRITE_VERBS))
    + r"|[a-z][a-z0-9]*_[a-z0-9_]+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _ConfigurationSubject:
    """One field and its optional entity and literal target value."""

    entity: str | None
    facet: str
    end: int
    has_value: bool
    scope_group: int


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
    preservation_scope = False
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
        if character in {"'", '"'} and not (
            character == "'" and index and query[index - 1].isalnum()
        ):
            quote = character
        elif character in "([{":
            stack.append(character)
        elif character in ")]}" and stack and stack[-1] == matching[character]:
            stack.pop()
        elif not stack and _EXCLUDED_SUFFIX.match(query, index):
            qualifier_scope = True
            clause_prefix = _OPTIONAL_POLITENESS.sub("", normalize_query(query[start:]))
            if _POSTFIX_PRESERVATION.match(query[index:]) and not _DIRECT_NEGATION.match(clause_prefix):
                preservation_scope = True
        elif character in ";\n," and not stack:
            if character == ",":
                remainder = query[index + 1 :]
                explicit_operation = _EXPLICIT_OPERATION.match(remainder)
                if (qualifier_scope and not (preservation_scope and explicit_operation)) or (
                    _POSTFIX_PRESERVATION.match(remainder)
                    or _CONFIGURATION_CONTINUATION.match(remainder)
                ):
                    continue
            clauses.append(query[start:index])
            start = index + 1
            qualifier_scope = False
            preservation_scope = False
        elif character.isspace() and not stack:
            separator = re.match(
                r"\s+(?:and(?:\s+then)?|then)\s+", query[index:], re.IGNORECASE
            )
            if separator is not None:
                remainder = query[index + separator.end() :]
                if qualifier_scope and not (
                    preservation_scope and _EXPLICIT_OPERATION.match(remainder)
                ):
                    continue
                # Entity repetition does not start a new command. Keep the
                # complete subject list under its read/write verb and shared
                # postfix predicate until configuration parsing is complete.
                if _CONFIGURATION_CONTINUATION.match(remainder):
                    continue
                clauses.append(query[start:index])
                start = index + separator.end()
                qualifier_scope = False
                preservation_scope = False
    clauses.append(query[start:])
    return tuple(clauses)


def _configuration_routes(text: str, *, allow_shorthand: bool) -> tuple[str, ...] | None:
    """Recognize a bounded field list; None leaves other commands to the router."""
    simplified = _strip_articles(text.replace("-", " ").split())
    verb, _space, tail = simplified.partition(" ")
    write_requested = verb in {"update", "set"} | _STATUS_VERBS
    read_requested = verb in _READ_VERBS
    if not write_requested and not read_requested:
        if not allow_shorthand:
            return None
        tail = simplified
    if not _CONFIGURATION_CONTINUATION.match(tail):
        return None
    # Preserve offsets but hide literal contents from field and qualifier scans.
    full_tail = re.sub(_QUOTED_VALUE, lambda match: " " * len(match[0]), tail)
    # Negative, reference, and read-only qualifiers are outside the changes.
    excluded_suffix = _EXCLUDED_SUFFIX.search(full_tail)
    end = excluded_suffix.start() if excluded_suffix is not None else len(full_tail)
    # Parse whole subjects before assigning routes. A shared postfix predicate
    # applies to its coordinated field list, including repeated entity names.
    subjects: list[_ConfigurationSubject] = []
    pending_entity: str | None = None
    previous_end = 0
    scope_group = 0
    while (token := _CONFIGURATION_TOKEN.search(full_tail, previous_end, end)) is not None:
        separator = tail[previous_end:token.start()]
        if not _CONFIGURATION_SEPARATOR.fullmatch(separator):
            break
        if raw_entity := token.group("entity"):
            if subjects and separator.strip():
                # Explicit entity coordination starts a new field scope while
                # retaining the command's verb and shared predicate.
                scope_group += 1
            pending_entity = raw_entity.replace("ad set", "adset")
            previous_end = token.end()
            continue
        facet = token.group("facet")
        value_pattern = _CONFIGURATION_VALUES.get(facet)
        value = re.match(
            rf"\s+(?:to\s+)?{value_pattern}(?=\s|[,;:]|$)",
            tail[token.end():end],
        ) if value_pattern is not None else None
        previous_end = token.end() + (value.end() if value is not None else 0)
        subjects.append(_ConfigurationSubject(
            entity=pending_entity,
            facet=facet,
            end=previous_end,
            has_value=value is not None,
            scope_group=scope_group,
        ))
        pending_entity = None
    if subjects and not read_requested and _POSTFIX_PRESERVATION.match(full_tail[subjects[-1].end:]):
        # The explicit verb owns the first field; subsequent unvalued fields
        # form the preservation subject. A supplied target value also closes
        # a positive subject before the following preservation list, including
        # shorthand within an already established write context.
        keep = 1 if write_requested and len(subjects) > 1 else 0
        for index, subject in enumerate(subjects[:-1]):
            if subject.has_value:
                keep = index + 1
        subjects = subjects[:keep]
    if verb in _STATUS_VERBS and subjects and subjects[0].facet != "status":
        return None
    if read_requested:
        # A report name can begin with a field, e.g. "budget pacing report".
        # Only consume a read when its entire subject is a configuration list;
        # otherwise let canonical read routing retain the requested workflow.
        if not _CONFIGURATION_SEPARATOR.fullmatch(full_tail[previous_end:end].rstrip(" .!?")):
            return None
        if verb in _GET_SYNONYMS | {"get"}:
            return tuple(dict.fromkeys(
                _ENTITY_GETTERS[subject.entity]
                for subject in subjects if subject.entity is not None
            ))
        return ()
    entity_groups = {
        group: tuple(dict.fromkeys(
            subject.entity for subject in subjects
            if subject.scope_group == group and subject.entity is not None
        ))
        for group in {subject.scope_group for subject in subjects}
    }
    entity: str | None = None
    explicitly_scoped = False
    routes: list[str] = []
    for subject in subjects:
        if subject.entity is not None:
            entity = subject.entity
            explicitly_scoped = True
        facet = subject.facet
        scopes = (entity,) if entity is not None else ()
        if facet in {"bid amount", "bid strategy"} and not scopes:
            scopes = ("adset",)
        entities = entity_groups[subject.scope_group]
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
            elif facet in {"name", "objective"}:
                routes.append(f"update_{scope}")
            else:
                routes.append(f"update_{scope}_budget")
        explicitly_scoped = False
    return tuple(dict.fromkeys(routes))


def _workflow_route(text: str) -> str | None:
    """Small aliases for the creative-replacement and saved-report workflows."""
    history_text = re.sub(_QUOTED_VALUE, "", text)
    history_subject = re.split(r"\b(?:named|called)\b", history_text, maxsplit=1)[0]
    if re.match(r"^(?:export|get|fetch|find|inspect|list|read|show|view|summarize)\b", history_text) and (
        re.search(r"\b(?:change history|activity logs?|audit logs?|changelog)\b", history_subject)
        or re.search(r"\b(?:changes|changed)\b", history_subject)
    ):
        return "list_change_history" if re.search(r"\b(?:page|preview)\b", history_subject) else "export_change_history"
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
    simplified = _strip_articles(text.replace("-", " ").replace("/", " ").split()).rstrip(" .!?")
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
    if re.fullmatch(_QUOTED_VALUE, text):
        # Quoting one exact tool identifier is still an explicit tool search.
        literal = text[1:-1]
        return (literal,) if literal in names else ()
    # Quoted configuration values are data, even when they contain the name
    # of a live tool. Exact call arguments were validated before this scan.
    text = re.sub(_QUOTED_VALUE, lambda match: " " * len(match[0]), text)
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

            configuration_routes = _configuration_routes(
                clause, allow_shorthand=allow_shorthand
            )
            if configuration_routes is not None:
                configuration_routes = tuple(name for name in configuration_routes if name in names)
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
                        if name.startswith(f"{_WRITE_VERB_TOOL_PREFIX.get(direct_verb, direct_verb)}_")
                        and (
                            direct_verb not in _SUBJECT_SCOPED_WRITE_VERBS
                            or _acts_on_tool_subject(
                                literal_clause,
                                name,
                                required=_SUBJECT_WORDS_REQUIRED.get(direct_verb, 1),
                            )
                        )
                    )
                continue
            if (
                negated is None
                and direct_verb in _SUBJECT_SCOPED_WRITE_VERBS
                and route in mutation_names
                and _is_analytical(literal_clause)
            ):
                # "boost instagram post engagement totals" starts with a tool's
                # name but asks for numbers; leave it to the read tools.
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


def _singular(word: str) -> str:
    """Fold simple plurals so "studies" matches "study" and "posts" matches "post"."""
    if len(word) > 4 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _names_tool_subject(clause: str, name: str, *, required: int = 1) -> bool:
    """Return whether a clause mentions enough of what a verb-named tool acts on.

    Adjacent words are also tried joined, so "ad set" matches a tool named for "adset".
    """
    tokens = [_singular(word) for word in re.findall(r"[a-z0-9]+", clause)]
    tokens = [_SUBJECT_SYNONYMS.get(token, token) for token in tokens]
    words = set(tokens) | {f"{first}{second}" for first, second in zip(tokens, tokens[1:])}
    subjects = {_singular(part) for part in name.split("_")[1:]}
    return len(subjects & words) >= required


# Words a request uses for what a subject-scoped write tool acts on, mapped to
# the word in the tool's name ("add customers" adds audience users).
_SUBJECT_SYNONYMS = {
    "contact": "user",
    "customer": "user",
    "member": "user",
    "people": "user",
    "person": "user",
    "dataset": "source",
    "pixel": "source",
}
# Words that end the thing a verb acts on. When one follows the verb directly
# ("add up", "boost in", "copy of", "cancel out"), the verb is not a command
# acting on anything.
_OBJECT_PHRASE_ENDINGS = frozenset(
    {
        "about", "across", "against", "and", "as", "at", "between", "by", "for",
        "from", "in", "into", "of", "off", "on", "onto", "or", "out", "over", "per",
        "then", "to", "together", "under", "up", "versus", "vs", "with", "within",
        "without",
    }
)
# Reporting and arithmetic words. A clause that contains one is asking about
# numbers ("add audience performance to the report"), not changing an object.
# Count nouns ("event source counts", "audience size") are numbers too.
_ANALYTICAL_WORDS = frozenset(
    {
        "analysis", "average", "breakdown", "chart", "click", "count", "cpa", "cpc",
        "cpm", "ctr", "engagement", "export", "figure", "frequency", "impression",
        "insight", "kpi", "metric", "noise", "number", "outlier", "overlap",
        "percent", "percentage", "performance", "rate", "ratio", "reach", "report",
        "reporting", "result", "revenue", "roas", "roi", "size", "spend", "stat",
        "statistic", "sum", "summary", "table", "tally", "total", "trend", "volume",
    }
)
# Places reporting output goes. Adding something to one of these, or removing
# it from one, edits a report rather than the object ("add audience users to
# the report"). "list" is not one: a customer list is an audience.
_REPORTING_DESTINATIONS = frozenset(
    {
        "breakdown", "cell", "chart", "column", "dashboard", "deck", "doc",
        "document", "export", "graph", "plot", "presentation", "readout", "recap",
        "report", "row", "scorecard", "sheet", "slide", "spreadsheet", "summary",
        "tab", "table", "visualization", "workbook", "worksheet",
    }
)
# Nouns for the things the subject-scoped write tools act on. A reporting word
# before one of them is part of its description or name ("the Reach campaign",
# "the top engagement post"); a reporting word after the last of them is what
# the request is about ("audience performance", "post engagement totals").
_ENTITY_NOUNS = frozenset(
    {
        "account", "ad", "adset", "audience", "campaign", "catalog", "contact",
        "creative", "customer", "dataset", "event", "feed", "image", "member",
        "page", "people", "person", "pixel", "post", "product", "set", "source",
        "study", "user", "video",
    }
)
# Words after which the rest of an object or destination phrase is a name
# ('the campaign named "Reach"'), not something acted on.
_NAME_MARKERS = frozenset(
    {"called", "entitled", "labeled", "labelled", "named", "nicknamed", "titled"}
)
# Words after which the rest of the clause describes which object is meant
# ("the campaign listed in the report", "the study measuring reach"), so no
# destination follows.
_TARGET_DESCRIPTORS = frozenset(
    {
        "appear", "appearing", "appears", "containing", "featured", "flagged",
        "having", "highlighted", "included", "listed", "measuring", "mentioned",
        "running", "shown", "showing", "targeting", "testing", "tracking", "using",
        "which", "who", "whose", "where",
    }
)
# Prepositions that introduce where a verb puts its object or takes it from,
# strongest first: in "add the customers in the spreadsheet to the audience"
# the destination is the audience, so "in" counts only when "to" is absent.
_DESTINATION_PREPOSITIONS: dict[str, tuple[frozenset[str], ...]] = {
    "add": (frozenset({"to", "into", "onto"}), frozenset({"in"})),
    "remove": (frozenset({"from"}), frozenset({"off", "out"})),
    "connect": (frozenset({"to", "into", "onto"}), frozenset({"with"})),
    "disconnect": (frozenset({"from"}),),
    "duplicate": (frozenset({"to", "into", "onto"}), frozenset({"in"})),
    "copy": (frozenset({"to", "into", "onto"}), frozenset({"in"})),
    "clone": (frozenset({"to", "into", "onto"}), frozenset({"in"})),
}


@dataclass(frozen=True, slots=True)
class _Word:
    """One word of a clause, lowercased, whether it was written as a Title-case name,
    whether it was written as a possessive ("report's", "reports'"), and whether it
    is a number written as an id ("#123", "act_123")."""

    text: str
    titled: bool
    possessive: bool = False
    marked_id: bool = False


# An apostrophe (straight or curly) that makes the word before it possessive.
_POSSESSIVE_AFTER = re.compile(r"['’]s(?![A-Za-z0-9])")
_PLURAL_POSSESSIVE_AFTER = re.compile(r"['’](?![A-Za-z0-9])")


def _phrase_words(clause: str) -> list[_Word]:
    """The clause's words with quoted text removed: a quoted name is never a subject.

    In a clause that also has ordinary lowercase words, a word written in Title
    case after the verb ("duplicate the campaign Reach") is marked as possibly
    part of a name. All-capital words (CTR, ROAS) are not marked. The "s" of a
    possessive stays a word of its own, as before.
    """
    text = re.sub(_QUOTED_VALUE, " ", clause)
    matches = list(re.finditer(r"[A-Za-z0-9]+", text))
    raw = [match[0] for match in matches]
    mixed = any(word[:1].islower() for word in raw[1:])
    return [
        _Word(
            word.lower(),
            mixed and index > 0 and word[:1].isupper() and not word.isupper(),
            bool(
                _POSSESSIVE_AFTER.match(text, match.end())
                or (word[-1:] in "sS" and _PLURAL_POSSESSIVE_AFTER.match(text, match.end()))
            ),
            word.isdigit() and _id_mark_before(text, match.start()),
        )
        for index, (word, match) in enumerate(zip(raw, matches))
    ]


def _id_mark_before(text: str, start: int) -> bool:
    """Whether what is written right before position start marks a number as an id: "#123", "act_123"."""
    if text[start - 1:start] == "#":
        return True
    return text[max(0, start - 4):start].lower() == "act_" and not text[start - 5:start - 4].isalnum()
# Words that say the numbers after them are ids: "id 123", "ID: 123", "ids 123, 456".
_ID_WORDS = frozenset({"id", "ids"})
# A bare number this long is an id; a shorter one is a position ("page 2", "section 3").
# Meta ids are much longer than any page, row or section number.
_MIN_BARE_ID_DIGITS = 5


# Words that say the number after them is a position: "page no. 2", "page number 2".
_POSITION_WORDS = frozenset({"no", "nr", "num", "number"})
# Words joining several numbers: "pages 2 and 3", "ids 1, 2 or 3".
_NUMBER_JOINERS = frozenset({"and", "or"})


def _without_trailing_id(phrase: list[_Word], *, any_number: bool = False) -> list[_Word]:
    """The phrase without the id that can follow its entity noun to say which one is meant.

    That is a number written after "id" or "ids", after "#" or as act_<digits>, or a bare
    number of at least _MIN_BARE_ID_DIGITS digits ("the dashboard's audience 1234567890").
    A short bare number is kept: "section 2" is a place in a report. A quoted name and
    punctuation are already gone (see _phrase_words).

    any_number sets aside every trailing number with what marks it ("no. 2", "number 2",
    "#2", "id 42", "2 and 3"), to find the noun the number follows (see _part_of_a_report).
    """
    end = len(phrase)
    while end:
        start = end
        while start and (
            phrase[start - 1].text.isdigit()
            or (any_number and start < end and phrase[start - 1].text in _NUMBER_JOINERS)
        ):
            start -= 1
        while start < end and not phrase[start].text.isdigit():
            start += 1  # a joiner before the first number belongs to the phrase
        numbers = phrase[start:end]
        if not numbers:
            break
        qualified = bool(start) and phrase[start - 1].text in _ID_WORDS
        marked = any(word.marked_id for word in numbers)
        if not (any_number or qualified or marked or all(len(word.text) >= _MIN_BARE_ID_DIGITS for word in numbers)):
            break
        if qualified or (marked and start and phrase[start - 1].text == "act"):
            start -= 1
        elif any_number and start and phrase[start - 1].text in _POSITION_WORDS:
            start -= 1
        end = start
    return phrase[:end]


# Nouns that name a part of a document. "page" is also an entity noun (a Facebook
# Page), and the only one that names a document part; the others are not entities,
# so a report owning them already makes them a place in the report.
_DOCUMENT_PARTS = _REPORTING_DESTINATIONS | frozenset(
    {"area", "block", "chapter", "page", "panel", "paragraph", "part", "section"}
)


def _is_entity(word: str) -> bool:
    return word in _ENTITY_NOUNS or _singular(word) in _ENTITY_NOUNS


def _phrase(words: list[_Word], start: int) -> tuple[list[_Word], int]:
    """Words from start up to a preposition, conjunction, name marker or descriptor.

    A descriptor ends the phrase only after an entity noun: in "the campaign
    listed in the report" it describes the campaign, while in "the running
    studies" it is part of the phrase.
    """
    phrase: list[_Word] = []
    index = start
    while index < len(words):
        word = words[index].text
        if word in _OBJECT_PHRASE_ENDINGS or word in _NAME_MARKERS:
            break
        if word in _TARGET_DESCRIPTORS and any(_is_entity(item.text) for item in phrase):
            break
        phrase.append(words[index])
        index += 1
    return phrase, index


def _object_span(clause: str) -> tuple[list[_Word], list[_Word], int]:
    """The clause's words, the object phrase (see _object_phrase), and where the phrase stopped."""
    words = _phrase_words(clause)
    phrase, stop = _phrase(words, 1)
    last_entity = max((index for index, word in enumerate(phrase) if _is_entity(word.text)), default=-1)
    named = [
        word for index, word in enumerate(phrase)
        if not (index > last_entity >= 0 and word.titled and not _is_entity(word.text))
    ]
    return words, named, stop


def _object_phrase(clause: str) -> list[_Word]:
    """The words right after the verb, without a name written after the entity noun.

    The phrase ends at the first preposition, conjunction, name marker
    ("named", "called") or descriptor ("listed"). Quoted text is removed first,
    and Title-case words after the last entity noun ("the campaign Reach Test")
    are dropped as the entity's name.
    """
    return _object_span(clause)[1]


def _object_words(clause: str) -> list[str]:
    """Return the lowercase words of the thing a clause's verb acts on (see _object_phrase)."""
    return [word.text for word in _object_phrase(clause)]


def _after_object(clause: str) -> tuple[list[_Word], int, int]:
    """The clause's words, and the span after the object that can say where it goes.

    A descriptor ("listed", "shown") ends the span: what follows says which
    object is meant, not where it goes or how it is laid out.
    """
    words = _phrase_words(clause)
    if not words:
        return [], 0, 0
    _object, start = _phrase(words, 1)
    end = next(
        (
            index
            for index in range(start, len(words))
            if words[index].text in _TARGET_DESCRIPTORS
            and any(_is_entity(word.text) for word in words[1:index])
        ),
        len(words),
    )
    return words, start, end


def _destination_span(clause: str) -> tuple[list[_Word], list[_Word], int]:
    """The clause's words, the phrase after the verb's destination preposition, and where it stopped.

    "add audience users to the report" has the destination "the report"; with
    no destination preposition the phrase is empty.
    """
    words, start, end = _after_object(clause)
    if not words:
        return [], [], 0
    for prepositions in _DESTINATION_PREPOSITIONS.get(words[0].text, ()):
        for index in range(start, end):
            if words[index].text in prepositions:
                phrase, stop = _phrase(words, index + 1)
                return words, phrase, stop
    return words, [], len(words)


def _is_report_container(word: str) -> bool:
    return word in _REPORTING_DESTINATIONS or _singular(word) in _REPORTING_DESTINATIONS


def _part_of_a_report(words: list[_Word], phrase: list[_Word], stop: int) -> bool:
    """Whether a phrase names a part of a report rather than the thing a tool acts on.

    The report is the phrase's owner, written as a possessive ("the report's
    audience section") or after "of" ("the audience section of the
    dashboard"), and the phrase's last word, which says what the phrase is, is
    not an entity noun. When it is one ("the dashboard's audience", like "the
    dashboard audience"), the report only says which audience is meant.

    The rule for a number after the noun (R6 L10, R7 M3):
    - The noun is the last word before any trailing number and its marker
      ("2", "#2", "no. 2", "number 2", "id 42", "act_42", "2 and 3"). A quoted
      name is already gone.
    - Without a report owner the phrase is never a part of a report, whatever
      number follows: "the Page 123456789012345" is a Facebook Page.
    - With a report owner, a document part ("page", "section", "tab" and the
      rest of _DOCUMENT_PARTS) is a place in the report, and a number after it is
      its position in any form: "page #2 of the report", "the report's page
      1234567890". "page" is both a document part and a Facebook Page; a report
      owner decides it is the report's page, also with no number or with a name
      after it. A caller who means a Page names it without a report owner.
    - With a report owner, an entity noun (audience, campaign, catalog) is that
      entity, and a number after it identifies which one in any form: "the
      dashboard's audience 1234567890", "the report's audience 2", "the audience
      id 42 of the dashboard". The report only says which entity is meant.
    """
    head = _without_trailing_id(phrase, any_number=True) or phrase
    if not head:
        return False
    if any(word.possessive and _is_report_container(word.text) for word in head[:-1]):
        owned = True
    elif stop < len(words) and words[stop].text == "of":
        owner = _phrase(words, stop + 1)[0]
        owned = bool(owner) and _is_report_container(owner[-1].text)
    else:
        owned = False
    if not owned:
        return False
    noun = head[-1].text
    return noun in _DOCUMENT_PARTS or _singular(noun) in _DOCUMENT_PARTS or not _is_entity(noun)


# Prepositions that say how the object is laid out in a report, or what it is
# for: "add audience users as a column", "per row", "by column", "at row 3",
# "for each row", "for the report".
_LAYOUT_PREPOSITIONS = frozenset({"as", "at", "by", "for", "per"})


def _layout_spans(clause: str) -> tuple[list[_Word], list[tuple[list[_Word], int]]]:
    """The clause's words, and each phrase after a layout preposition with where it stopped."""
    words, start, end = _after_object(clause)
    return words, [
        _phrase(words, index + 1)
        for index in range(start, end)
        if words[index].text in _LAYOUT_PREPOSITIONS
    ]


def _reporting_head(words: list[str], vocabulary: frozenset[str]) -> bool:
    """Whether a vocabulary word follows the phrase's last entity noun (or any, without one)."""
    last_entity = max((index for index, word in enumerate(words) if _is_entity(word)), default=-1)
    return any(
        word in vocabulary or _singular(word) in vocabulary
        for index, word in enumerate(words)
        if index > last_entity
    )


def _is_analytical(clause: str) -> bool:
    """Return whether a clause's verb acts on a reporting number or puts it in a report.

    Only the object right after the verb and the verb's destination count.
    "boost instagram post engagement totals" asks for numbers, and so does
    "add audience users to the report"; "boost the post with the best
    engagement" is a command whose target is described by a number, and in
    'duplicate campaign named "Reach"' or "duplicate the Reach campaign" the
    reporting word is part of the campaign's name. The clause may keep its
    original letter case, which is used only to recognise unquoted names.

    A reporting container ending the object is what the verb acts on: in
    "remove audience users column" the column is removed, not customers, and
    so is one after a layout preposition ("add audience users as a column").
    The exception is a destination that names what the tool acts on: in "add
    the customer spreadsheet to the audience" the spreadsheet is where the
    customers come from.

    A part of a report is a reporting place even when it is named after an
    entity: "the report's audience section" and "the audience section of the
    dashboard" are in a report, as a destination or as the object (see
    _part_of_a_report).
    """
    words, object_phrase, object_stop = _object_span(clause)
    object_words = [word.text for word in object_phrase]
    _words, destination_phrase, destination_stop = _destination_span(clause)
    destination = [word.text for word in destination_phrase]
    if _reporting_head(object_words, _ANALYTICAL_WORDS) or _reporting_head(
        destination, _ANALYTICAL_WORDS | _REPORTING_DESTINATIONS
    ):
        return True
    if _part_of_a_report(words, destination_phrase, destination_stop):
        return True
    layout_words, layouts = _layout_spans(clause)
    container = (
        _reporting_head(object_words, _REPORTING_DESTINATIONS)
        or _part_of_a_report(words, object_phrase, object_stop)
        or any(
            _reporting_head([word.text for word in phrase], _REPORTING_DESTINATIONS)
            or _part_of_a_report(layout_words, phrase, stop)
            for phrase, stop in layouts
        )
    )
    return container and not any(_is_entity(word) for word in destination)


def _acts_on_tool_subject(clause: str, name: str, *, required: int = 1) -> bool:
    """Return whether a clause starting with a subject-scoped verb is a command for this tool.

    The words right after the verb, up to the first preposition or conjunction,
    must name what the tool acts on: "add customers to the audience" adds
    audience users, while in "remove outliers from audience performance" the
    thing removed is outliers. A clause that uses the verb with a particle
    ("add up", "boost in") or whose object is a reporting number is never a command.
    The required count still applies to the whole clause.
    """
    if _is_analytical(clause):
        return False
    object_words = _object_words(clause)
    if not object_words or not _names_tool_subject(" ".join(object_words), name):
        return False
    return _names_tool_subject(clause.lower(), name, required=required)


def is_compatible_name(name: str, decision: RouteDecision) -> bool:
    """Return whether one live tool can participate in this decision."""
    return name not in decision.excluded_tools and name in decision.compatible_tools


def filter_compatible_names(
    names: Iterable[str],
    decision: RouteDecision,
) -> list[str]:
    """Filter ranked names using the production compatibility predicate."""
    return [name for name in names if is_compatible_name(name, decision)]
