"""Targeting and planning tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import normalize_collection


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve account id or default."""
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


@mcp_server.tool()
async def search_interests(
    query: str,
    account_id: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Search interest targeting options."""
    client = get_graph_api_client()
    payload = await client.search_interests(query=query, limit=limit)
    return normalize_collection(payload)


@mcp_server.tool()
async def search_geo_locations(
    query: str,
    location_types: list[str] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Search geo targeting options."""
    client = get_graph_api_client()
    payload = await client.search_geo_locations(query=query, location_types=location_types, limit=limit)
    return normalize_collection(payload)


@mcp_server.tool()
async def estimate_audience_size(
    targeting_spec: dict[str, Any],
    account_id: str | None = None,
    optimization_goal: str | None = None,
) -> dict[str, Any]:
    """Estimate audience size for a targeting spec."""
    client = get_graph_api_client()
    payload = await client.estimate_audience_size(
        _resolve_account_id(account_id),
        targeting_spec=targeting_spec,
        optimization_goal=optimization_goal,
    )
    return {"item": payload, "summary": {"count": 1}}


@mcp_server.tool()
async def get_reach_frequency_predictions(
    account_id: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """List reach frequency predictions."""
    client = get_graph_api_client()
    payload = await client.get_reach_frequency_predictions(_resolve_account_id(account_id), limit=limit)
    return normalize_collection(payload)
