"""Shared input types for LLM-facing tool parameters."""

from __future__ import annotations

from typing import Annotated, Any, TypeAlias

from pydantic import BeforeValidator

from meta_ads_mcp.errors import ValidationError


def coerce_csv_string_list(value: Any) -> Any:
    """Accept a list or a top-level comma-separated Graph field expression."""
    if not isinstance(value, str):
        return value

    items: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    matching = {"}": "{", "]": "[", ")": "("}
    for index, character in enumerate(value):
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
        elif character in "{[(":
            stack.append(character)
        elif character in "}])":
            if stack and stack[-1] == matching[character]:
                stack.pop()
        elif character == "," and not stack:
            if item := value[start:index].strip():
                items.append(item)
            start = index + 1
    if item := value[start:].strip():
        items.append(item)
    return items


def coerce_strict_csv_string_list(value: Any) -> Any:
    """Accept simple CSV while preserving blank entries for domain validation."""
    if value is None:
        return []
    if not isinstance(value, str):
        return value
    return [item.strip() for item in value.split(",")]


StringList: TypeAlias = Annotated[
    list[str],
    BeforeValidator(
        coerce_csv_string_list,
        json_schema_input_type=str | list[str],
    ),
]

StrictStringList: TypeAlias = Annotated[
    list[str],
    BeforeValidator(
        coerce_strict_csv_string_list,
        json_schema_input_type=str | list[str],
    ),
]

# Graph fields use the same top-level CSV grammar, including nested field expressions.
FieldList: TypeAlias = StringList


def normalize_recommendation_filters(
    recommendation_names: StrictStringList | str | None,
    recommendation_stages: StrictStringList | str | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Validate native filters consistently for MCP and direct Python calls."""
    filters: list[list[str] | None] = []
    for parameter, value in (
        ("recommendation_names", recommendation_names),
        ("recommendation_stages", recommendation_stages),
    ):
        if value is None:
            filters.append(None)
            continue
        normalized = coerce_strict_csv_string_list(value)
        if not isinstance(normalized, list) or any(
            not isinstance(item, str) or not item.strip() for item in normalized
        ):
            raise ValidationError(
                f"{parameter} must be a list of nonblank strings or a comma-separated string."
            )
        normalized = [item.strip() for item in normalized]
        if parameter == "recommendation_stages" and any(
            stage not in {"MFR", "PCR", "PFR"} for stage in normalized
        ):
            raise ValidationError("recommendation_stages must contain only MFR, PCR, or PFR.")
        filters.append(normalized)
    return filters[0], filters[1]


def normalize_field_list(value: FieldList | str | None) -> list[str] | None:
    """Normalize field inputs for direct Python calls as well as MCP validation."""
    if value is None:
        return None
    normalized = coerce_csv_string_list(value)
    if not isinstance(normalized, list) or not all(
        isinstance(item, str) for item in normalized
    ):
        raise TypeError("fields must be a list of strings or a comma-separated string.")
    return normalized
