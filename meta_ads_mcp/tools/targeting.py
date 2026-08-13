"""Targeting and planning tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.api_compat import validate_targeting_placements
from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.tool_types import StringList


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve account id or default."""
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


def _cursor_options(after: str | None) -> dict[str, str]:
    """Return a Graph cursor argument only when it is nonblank."""
    cursor = blank_to_none(after)
    return {"after": cursor} if cursor else {}


@mcp_server.tool()
async def search_interests(
    query: str,
    account_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to find interest targeting options from a free-text query."""
    client = get_graph_api_client()
    payload = await client.search_interests(
        query=query,
        limit=limit,
        **_cursor_options(after),
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def get_interest_suggestions(
    interest_list: StringList,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user already has seed interests and wants closely related targeting ideas."""
    if not interest_list:
        raise ValidationError("interest_list must contain at least one interest.")
    client = get_graph_api_client()
    payload = await client.get_interest_suggestions(
        interest_list=interest_list,
        limit=limit,
        **_cursor_options(after),
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def validate_interests(
    interest_list: StringList | None = None,
    interest_ids: StringList | None = None,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to confirm that proposed interests still resolve in Meta's targeting catalog."""
    if not interest_list and not interest_ids:
        raise ValidationError("Provide interest_list or interest_ids.")
    client = get_graph_api_client()
    payload = await client.validate_interests(
        interest_list=interest_list,
        interest_ids=interest_ids,
        **_cursor_options(after),
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def search_geo_locations(
    query: str,
    location_types: StringList | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants countries, regions, cities, or other geo targeting matches from text."""
    client = get_graph_api_client()
    payload = await client.search_geo_locations(
        query=query,
        location_types=location_types,
        limit=limit,
        **_cursor_options(after),
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def search_behaviors(
    query: str | None = None,
    account_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants behavior-based targeting options rather than interests or demographics."""
    client = get_graph_api_client()
    payload = await client.search_targeting_categories(
        account_id=_resolve_account_id(account_id),
        category_class="behaviors",
        query=query,
        limit=limit,
        **_cursor_options(after),
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def get_targeting_categories(
    category_class: str,
    query: str | None = None,
    account_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user knows the targeting category class they want and needs matching options."""
    if not category_class:
        raise ValidationError("category_class is required.")
    client = get_graph_api_client()
    payload = await client.search_targeting_categories(
        account_id=_resolve_account_id(account_id),
        category_class=category_class,
        query=query,
        limit=limit,
        **_cursor_options(after),
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def search_demographics(
    demographic_class: str = "demographics",
    query: str | None = None,
    account_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants demographic targeting options such as age, education, or life-stage categories."""
    if not demographic_class:
        raise ValidationError("demographic_class is required.")
    client = get_graph_api_client()
    payload = await client.search_targeting_categories(
        account_id=_resolve_account_id(account_id),
        category_class=demographic_class,
        query=query,
        limit=limit,
        **_cursor_options(after),
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def estimate_audience_size(
    targeting_spec: dict[str, Any],
    account_id: str | None = None,
    optimization_goal: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants a reach-size estimate for a draft targeting spec before creating an ad set."""
    validate_targeting_placements(targeting_spec)
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
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants existing reach-and-frequency prediction objects for an account."""
    client = get_graph_api_client()
    payload = await client.get_reach_frequency_predictions(
        _resolve_account_id(account_id),
        limit=limit,
        **_cursor_options(after),
    )
    return normalize_collection(payload)
