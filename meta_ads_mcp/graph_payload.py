"""Safe construction of Graph API mutation payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meta_ads_mcp.errors import ValidationError

RESERVED_GRAPH_PARAMS = {"access_token", "execution_options"}
OMIT = object()


def merge_graph_payload(
    typed_fields: Mapping[str, Any],
    extra_fields: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge extension fields without bypassing the tool's typed contract."""
    merged = {
        key: value
        for key, value in typed_fields.items()
        if value is not OMIT
    }
    if not extra_fields:
        return merged

    reserved = sorted(RESERVED_GRAPH_PARAMS.intersection(extra_fields))
    if reserved:
        raise ValidationError(
            f"params cannot contain transport-managed fields: {', '.join(reserved)}."
        )

    conflicts = sorted(set(typed_fields).intersection(extra_fields))
    if conflicts:
        raise ValidationError(
            "params cannot override typed fields or supply omitted tool-managed fields: "
            f"{', '.join(conflicts)}. Use the named tool parameters instead."
        )

    for key, value in extra_fields.items():
        merged.setdefault(key, value)
    return merged


def add_validate_only(payload: dict[str, Any], *, validate_only: bool) -> None:
    """Own Meta's validation control field, omitting it from ordinary calls."""
    payload["execution_options"] = ["validate_only"] if validate_only else OMIT
