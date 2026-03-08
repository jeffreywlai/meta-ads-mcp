"""Public research tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import normalize_collection


ADS_ARCHIVE_FIELDS = [
    "id",
    "page_name",
    "ad_snapshot_url",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
]


@mcp_server.tool()
async def search_ads_archive(
    search_terms: str,
    ad_reached_countries: list[str],
    ad_type: str = "ALL",
    limit: int = 25,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Use this when the user wants competitor or market research from the public Meta Ads Library."""
    if not ad_reached_countries:
        raise ValidationError("ad_reached_countries must contain at least one country code.")
    client = get_graph_api_client()
    payload = await client.search_ads_archive(
        search_terms=search_terms,
        ad_reached_countries=ad_reached_countries,
        ad_type=ad_type,
        limit=limit,
        fields=fields or ADS_ARCHIVE_FIELDS,
    )
    return normalize_collection(payload)
