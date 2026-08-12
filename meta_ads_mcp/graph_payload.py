"""Safe construction of Graph API mutation payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meta_ads_mcp.errors import ValidationError

RESERVED_GRAPH_PARAMS = {"access_token"}


def merge_graph_payload(
    typed_fields: Mapping[str, Any],
    extra_fields: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge extension fields without allowing silent typed-field overrides."""
    merged = dict(typed_fields)
    if not extra_fields:
        return merged

    reserved = sorted(RESERVED_GRAPH_PARAMS.intersection(extra_fields))
    if reserved:
        raise ValidationError(
            f"params cannot contain transport-managed fields: {', '.join(reserved)}."
        )

    conflicts = sorted(
        key
        for key, value in extra_fields.items()
        if key in merged and merged[key] != value
    )
    if conflicts:
        raise ValidationError(
            "params cannot override typed fields with different values: "
            f"{', '.join(conflicts)}. Use the named tool parameters instead."
        )

    for key, value in extra_fields.items():
        merged.setdefault(key, value)
    return merged


def add_validate_only(payload: dict[str, Any], *, validate_only: bool) -> None:
    """Request Meta validation without creating an object."""
    if validate_only:
        payload["execution_options"] = ["validate_only"]
