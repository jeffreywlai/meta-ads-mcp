"""Minimal contracts derived from the live FastMCP tool catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for


ToolEffect = Literal["read", "write"]

_WRITE_PREFIXES = (
    "archive_",
    "create_",
    "delete_",
    "disable_",
    "enable_",
    "modify_",
    "pause_",
    "resume_",
    "set_",
    "update_",
    "upload_",
)
_WRITE_NAMES = frozenset(
    {
        "exchange_code_for_token",
        "generate_system_user_token",
        "refresh_to_long_lived_token",
        "setup_ab_test",
    }
)
_READ_TAGS = frozenset({"read", "read-only", "readonly"})
_WRITE_TAGS = frozenset({"mutating", "mutation", "write", "write-only"})


def _compile_validator(schema: Mapping[str, Any]) -> Any | None:
    """Compile a schema once; malformed schemas fail closed."""
    try:
        validator_type = validator_for(schema)
        validator_type.check_schema(schema)
        return validator_type(schema, format_checker=FormatChecker())
    except (SchemaError, TypeError, ValueError):
        return None


def _annotation_hint(annotations: object, name: str) -> bool | None:
    """Read an MCP annotation from either a model or a plain mapping."""
    value = (
        annotations.get(name)
        if isinstance(annotations, Mapping)
        else getattr(annotations, name, None)
    )
    return value if isinstance(value, bool) else None


def _normalized_tags(raw_tags: object) -> frozenset[str]:
    """Normalize FastMCP tags without treating one string as characters."""
    if isinstance(raw_tags, str):
        values = (raw_tags,)
    elif isinstance(raw_tags, (set, frozenset, list, tuple)):
        values = raw_tags
    else:
        values = ()
    return frozenset(str(tag).strip().casefold() for tag in values)


def _tool_effect(tool: Any, name: str) -> ToolEffect:
    """Infer read/write behavior from MCP metadata, then stable names."""
    annotations = getattr(tool, "annotations", None)
    destructive = _annotation_hint(annotations, "destructiveHint")
    read_only = _annotation_hint(annotations, "readOnlyHint")
    if destructive is True:
        return "write"
    if read_only is not None:
        return "read" if read_only else "write"

    tags = _normalized_tags(getattr(tool, "tags", ()))
    if tags & _WRITE_TAGS:
        return "write"
    if tags & _READ_TAGS:
        return "read"

    return (
        "write"
        if name in _WRITE_NAMES or name.startswith(_WRITE_PREFIXES)
        else "read"
    )


def _partial_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Retain argument constraints while making root fields optional."""
    partial = dict(schema)
    partial.pop("required", None)
    return partial


@dataclass(frozen=True, slots=True)
class ToolContract:
    """Coordinator-facing metadata for one live FastMCP tool."""

    name: str
    schema: Mapping[str, Any] = field(compare=False, hash=False, repr=False)
    effect: ToolEffect
    validator: Any | None = field(default=None, compare=False, hash=False, repr=False)
    partial_validator: Any | None = field(
        default=None,
        compare=False,
        hash=False,
        repr=False,
    )


def build_tool_contracts(tools: Sequence[Any]) -> dict[str, ToolContract]:
    """Build contracts from the exact FastMCP tools exposed to search."""
    contracts: dict[str, ToolContract] = {}
    for tool in tools:
        name = str(getattr(tool, "name", "")).strip()
        schema = getattr(tool, "parameters", None)
        if not name or not isinstance(schema, Mapping):
            continue
        contracts[name] = ToolContract(
            name=name,
            schema=schema,
            effect=_tool_effect(tool, name),
            validator=_compile_validator(schema),
            partial_validator=_compile_validator(_partial_schema(schema)),
        )
    return contracts
