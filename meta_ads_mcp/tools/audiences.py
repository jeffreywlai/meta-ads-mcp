"""Audience management tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import normalize_collection
from meta_ads_mcp.schemas import mutation_response


AUDIENCE_FIELDS = [
    "id",
    "name",
    "subtype",
    "description",
    "customer_file_source",
    "approximate_count_lower_bound",
    "approximate_count_upper_bound",
    "retention_days",
    "time_updated",
    "operation_status",
    "lookalike_spec",
]


def _resolve_lookalike_spec(
    *,
    country: str | None,
    countries: list[str] | None,
    ratio: float | None,
    starting_ratio: float | None,
    lookalike_type: str,
) -> dict[str, Any]:
    """Build a lookalike_spec payload."""
    location_values = countries or ([country] if country else None)
    if not location_values:
        raise ValidationError("country or countries is required for create_lookalike_audience.")
    spec: dict[str, Any] = {
        "type": lookalike_type,
        "country": location_values[0] if len(location_values) == 1 else None,
        "location_spec": {"countries": location_values},
    }
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
) -> dict[str, Any]:
    """Use this when the user needs audience ids, sizes, or subtype metadata for one ad account."""
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit}
    if subtype:
        params["subtype"] = subtype
    if after:
        params["after"] = after
    payload = await client.list_objects(
        normalize_account_id(account_id),
        "customaudiences",
        fields=AUDIENCE_FIELDS,
        params=params,
    )
    return normalize_collection(payload)


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
    payload: dict[str, Any] = {"name": name, "subtype": subtype, "prefill": prefill}
    if description:
        payload["description"] = description
    if customer_file_source:
        payload["customer_file_source"] = customer_file_source
    if retention_days is not None:
        payload["retention_days"] = retention_days
    if rule:
        payload["rule"] = rule
    if params:
        payload.update(params)
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
    countries: list[str] | None = None,
    ratio: float | None = None,
    starting_ratio: float | None = None,
    lookalike_type: str = "similarity",
    description: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to expand from a seed audience into a lookalike audience."""
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
    }
    if description:
        payload["description"] = description
    if params:
        payload.update(params)
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
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if retention_days is not None:
        payload["retention_days"] = retention_days
    if customer_file_source is not None:
        payload["customer_file_source"] = customer_file_source
    if params:
        payload.update(params)
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
