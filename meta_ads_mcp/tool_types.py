"""Shared input types for LLM-facing tool parameters."""

from __future__ import annotations

from typing import Annotated, Any, TypeAlias

from pydantic import BeforeValidator


def coerce_csv_string_list(value: Any) -> Any:
    """Accept either a JSON string array or a comma-separated string."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


FieldList: TypeAlias = Annotated[
    list[str],
    BeforeValidator(
        coerce_csv_string_list,
        json_schema_input_type=str | list[str],
    ),
]
