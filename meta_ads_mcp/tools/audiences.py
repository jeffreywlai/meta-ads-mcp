"""Audience management tools."""

from __future__ import annotations

import math
from typing import Annotated, Any

from pydantic import Field

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.schemas import mutation_response
from meta_ads_mcp.tool_types import FieldList, StringList, coerce_csv_string_list

AUDIENCE_SUMMARY_FIELDS = [
    "id",
    "name",
    "subtype",
    "approximate_count_lower_bound",
    "approximate_count_upper_bound",
    "time_updated",
    "operation_status",
]

AUDIENCE_FIELDS = [
    *AUDIENCE_SUMMARY_FIELDS,
    "description",
    "customer_file_source",
    "retention_days",
    "lookalike_spec",
]


def _resolve_lookalike_spec(
    *,
    country: str | None,
    countries: list[str] | None,
    ratio: float | None,
    starting_ratio: float | None,
    lookalike_type: str | None,
) -> dict[str, Any]:
    """Build a lookalike_spec payload."""
    country = blank_to_none(country)
    if country and countries:
        raise ValidationError("Provide country or countries, not both.")
    location_values = coerce_csv_string_list(countries) or ([country] if country else None)
    if not location_values:
        raise ValidationError("country or countries is required for create_lookalike_audience.")
    if any(not isinstance(value, str) or not value.strip() for value in location_values):
        raise ValidationError("countries must contain nonblank country codes.")
    location_values = list(dict.fromkeys(value.strip().upper() for value in location_values))
    resolved_type = blank_to_none(lookalike_type) or (
        "custom_ratio" if ratio is not None else "similarity"
    )
    if ratio is not None:
        if not math.isfinite(ratio) or not 0 < ratio <= 1:
            raise ValidationError("ratio must be a finite fraction greater than 0 and at most 1.")
    elif starting_ratio is not None:
        raise ValidationError("ratio is required when starting_ratio is supplied.")
    if starting_ratio is not None and (
        not math.isfinite(starting_ratio) or not 0 <= starting_ratio < ratio
    ):
        raise ValidationError("starting_ratio must be finite, nonnegative, and less than ratio.")
    spec: dict[str, Any] = {"type": resolved_type}
    if len(location_values) == 1:
        spec["country"] = location_values[0]
    else:
        spec["location_spec"] = {"geo_locations": {"countries": location_values}}
    if ratio is not None:
        spec["ratio"] = ratio
    if starting_ratio is not None:
        spec["starting_ratio"] = starting_ratio
    return spec


@mcp_server.tool()
async def list_audiences(
    account_id: str,
    subtype: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List one page of audience ids, sizes, or subtype metadata. Follow summary.next_after with after until summary.complete is true."""
    if limit < 1:
        raise ValidationError("limit must be positive.")
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit}
    if subtype:
        params["subtype"] = subtype
    if after:
        params["after"] = after
    payload = await client.list_objects(
        normalize_account_id(account_id),
        "customaudiences",
        fields=fields or AUDIENCE_SUMMARY_FIELDS,
        params=params,
    )
    normalized = normalize_collection(payload)
    has_more = bool(normalized["paging"].get("next"))
    normalized["summary"].update({
        "complete": not has_more,
        "truncated": has_more,
        "next_after": normalized["paging"].get("after") if has_more else None,
    })
    return normalized


@mcp_server.tool()
async def get_audience(
    audience_id: str,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Get, fetch, show, or inspect one custom or lookalike audience by id with selectable detail fields."""
    audience_id = blank_to_none(audience_id)
    if audience_id is None:
        raise ValidationError("audience_id is required.")
    audience = await get_graph_api_client().get_object(
        audience_id,
        fields=fields or AUDIENCE_FIELDS,
    )
    return {"item": audience, "summary": {"count": 1}}


@mcp_server.tool()
async def create_custom_audience(
    account_id: str,
    name: str,
    subtype: str = "CUSTOM",
    description: str | None = None,
    customer_file_source: str | None = None,
    retention_days: int | None = None,
    rule: dict[str, Any] | None = None,
    prefill: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to create a first-party or rule-based custom audience."""
    payload: dict[str, Any] = {
        "name": name,
        "subtype": subtype,
        "prefill": prefill,
        "description": description or OMIT,
        "customer_file_source": customer_file_source or OMIT,
        "retention_days": OMIT if retention_days is None else retention_days,
        "rule": rule or OMIT,
    }
    payload = merge_graph_payload(payload, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(
        normalize_account_id(account_id),
        "customaudiences",
        data=payload,
    )
    return {
        "ok": True,
        "action": "create_custom_audience",
        "target": {"account_id": normalize_account_id(account_id)},
        "created": created,
    }


@mcp_server.tool()
async def create_lookalike_audience(
    account_id: str,
    name: str,
    origin_audience_id: str,
    country: str | None = None,
    countries: StringList | None = None,
    ratio: float | None = None,
    starting_ratio: float | None = None,
    lookalike_type: Annotated[
        str | None,
        Field(description="Defaults to custom_ratio when ratio is supplied, otherwise similarity. Explicit types are preserved and Meta validates their compatibility."),
    ] = None,
    description: str | None = None,
    params: Annotated[
        dict[str, Any] | None,
        Field(description="Additional Graph parameters. Cannot supply lookalike_spec or override tool-managed named fields; use country/countries, ratio, starting_ratio, and lookalike_type."),
    ] = None,
) -> dict[str, Any]:
    """Create a lookalike from a seed with tool-managed lookalike_spec. Supply country or countries; ratio is a fraction (0.01 means 1%). params cannot carry lookalike_spec."""
    payload: dict[str, Any] = {
        "name": name,
        "subtype": "LOOKALIKE",
        "origin_audience_id": origin_audience_id,
        "lookalike_spec": _resolve_lookalike_spec(
            country=country,
            countries=countries,
            ratio=ratio,
            starting_ratio=starting_ratio,
            lookalike_type=lookalike_type,
        ),
        "description": description or OMIT,
    }
    payload = merge_graph_payload(payload, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(
        normalize_account_id(account_id),
        "customaudiences",
        data=payload,
    )
    return {
        "ok": True,
        "action": "create_lookalike_audience",
        "target": {"account_id": normalize_account_id(account_id), "origin_audience_id": origin_audience_id},
        "created": created,
    }


@mcp_server.tool()
async def update_custom_audience(
    audience_id: str,
    name: str | None = None,
    description: str | None = None,
    retention_days: int | None = None,
    customer_file_source: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use this when the user already has an audience id and needs to change audience metadata."""
    payload: dict[str, Any] = {
        "name": OMIT,
        "description": OMIT,
        "retention_days": OMIT,
        "customer_file_source": OMIT,
    }
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if retention_days is not None:
        payload["retention_days"] = retention_days
    if customer_file_source is not None:
        payload["customer_file_source"] = customer_file_source
    payload = merge_graph_payload(payload, params)
    if not payload:
        raise ValidationError("At least one field must be provided for update_custom_audience.")
    client = get_graph_api_client()
    previous = await client.get_object(audience_id, fields=AUDIENCE_FIELDS)
    await client.update_object(audience_id, data=payload)
    return mutation_response(
        action="update_custom_audience",
        target={"audience_id": audience_id},
        previous={
            "name": previous.get("name"),
            "description": previous.get("description"),
            "retention_days": previous.get("retention_days"),
            "customer_file_source": previous.get("customer_file_source"),
        },
        current=payload,
    )


@mcp_server.tool()
async def delete_audience(audience_id: str) -> dict[str, Any]:
    """Use this only when the user explicitly wants to remove a custom audience."""
    client = get_graph_api_client()
    result = await client.delete_object(audience_id)
    return {
        "ok": True,
        "action": "delete_audience",
        "target": {"audience_id": audience_id},
        "result": result,
    }
