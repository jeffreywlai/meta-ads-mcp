"""Canonical normalization for forgiving LLM-facing tool inputs."""

from __future__ import annotations

import json
from typing import Any

from meta_ads_mcp.errors import ValidationError

TOOL_NAME_ALIASES = {
    "get_ad_set": "get_adset",
    "list_ad_sets": "list_adsets",
    "delete_ad_set": "delete_adset",
}


def canonical_tool_name(name: str) -> str:
    """Resolve a human-friendly tool alias to one canonical catalog name."""
    normalized = name.strip()
    if not normalized:
        raise ValidationError("name or tool_name must identify a tool.")
    return TOOL_NAME_ALIASES.get(normalized, normalized)


def resolve_tool_name(name: str | None, tool_name: str | None) -> str:
    """Resolve the canonical and compatibility proxy name parameters."""
    normalized_name = name.strip() if isinstance(name, str) else None
    normalized_alias = tool_name.strip() if isinstance(tool_name, str) else None
    canonical_name = canonical_tool_name(normalized_name) if normalized_name else None
    canonical_alias = canonical_tool_name(normalized_alias) if normalized_alias else None
    if canonical_name and canonical_alias and canonical_name != canonical_alias:
        raise ValidationError("name and tool_name must match when both are provided.")
    selected = canonical_name or canonical_alias
    if not selected:
        raise ValidationError("Provide name or tool_name.")
    return selected


def normalize_tool_arguments(arguments: dict[str, Any] | str | None) -> dict[str, Any]:
    """Accept a JSON object or its common stringified representation."""
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ValidationError("arguments must be a JSON object or object-valued JSON string.") from exc
    if not isinstance(decoded, dict):
        raise ValidationError("arguments JSON must decode to an object.")
    return decoded


def resolve_identifier_alias(
    primary: str | None,
    alias: str | None,
    *,
    primary_name: str,
    alias_name: str,
    required: bool = False,
) -> str | None:
    """Resolve two names for one string identifier without hiding conflicts."""
    normalized_primary = primary.strip() if isinstance(primary, str) and primary.strip() else None
    normalized_alias = alias.strip() if isinstance(alias, str) and alias.strip() else None
    if normalized_primary and normalized_alias and normalized_primary != normalized_alias:
        raise ValidationError(
            f"{primary_name} and {alias_name} must match when both are provided."
        )
    resolved = normalized_primary or normalized_alias
    if required and resolved is None:
        raise ValidationError(f"Provide {primary_name} or {alias_name}.")
    return resolved
