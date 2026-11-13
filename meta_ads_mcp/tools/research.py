"""Public research tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.tool_types import FieldList, StringList, coerce_csv_string_list

ADS_ARCHIVE_FIELDS = [
    "id",
    "page_name",
    "ad_snapshot_url",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
]
# Meta's Ad Library API reference: search_page_ids takes up to ten Page ids; ad_active_status is ACTIVE, ALL or
# INACTIVE and defaults to ACTIVE at Meta when omitted.
MAX_SEARCH_PAGE_IDS = 10
AD_ACTIVE_STATUSES = ("ACTIVE", "ALL", "INACTIVE")


def _page_ids(values: Any) -> list[str] | None:
    """Validate Page ids for the Ad Library search before any network call."""
    if values is None:
        return None
    ids = list(dict.fromkeys(
        str(value).strip() for value in (coerce_csv_string_list(values) or []) if str(value).strip()
    ))
    if not ids:
        raise ValidationError("search_page_ids must contain at least one Page id.")
    if len(ids) > MAX_SEARCH_PAGE_IDS:
        raise ValidationError(f"Meta's Ad Library accepts at most {MAX_SEARCH_PAGE_IDS} search_page_ids per search.")
    bad = [value for value in ids if not value.isdigit()]
    if bad:
        raise ValidationError(f"search_page_ids must be numeric Page ids passed as strings; not valid: {bad[:3]}.")
    return ids


@mcp_server.tool()
async def search_ads_archive(
    search_terms: str | None = None,
    *,
    ad_reached_countries: StringList,
    ad_type: str = "ALL",
    limit: int = 25,
    fields: FieldList | None = None,
    after: str | None = None,
    search_page_ids: StringList | None = None,
    ad_active_status: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants competitor or market research from the public Meta Ads Library. Give search_terms, or search_page_ids (up to 10 numeric Page ids) to see one advertiser's ads, or both; ad_reached_countries is required. ad_active_status is ACTIVE, ALL or INACTIVE; Meta returns only active ads when it is omitted."""
    if not ad_reached_countries:
        raise ValidationError("ad_reached_countries must contain at least one country code.")
    page_ids = _page_ids(search_page_ids)
    status = None
    if ad_active_status is not None:
        status = ad_active_status.strip().upper()
        if status not in AD_ACTIVE_STATUSES:
            raise ValidationError(f"ad_active_status must be one of {list(AD_ACTIVE_STATUSES)}.")
    client = get_graph_api_client()
    normalized_after = blank_to_none(after)
    if page_ids is None and status is None:
        if search_terms is None:
            raise ValidationError("Give search_terms, search_page_ids, or both.")
        payload = await client.search_ads_archive(
            search_terms=search_terms,
            ad_reached_countries=ad_reached_countries,
            ad_type=ad_type,
            limit=limit,
            fields=fields or ADS_ARCHIVE_FIELDS,
            **({"after": normalized_after} if normalized_after else {}),
        )
        return normalize_collection(payload)
    if search_terms is None and page_ids is None:
        raise ValidationError("Give search_terms, search_page_ids, or both.")
    payload = await client.search_ads_library(
        ad_reached_countries=ad_reached_countries,
        ad_type=ad_type,
        limit=limit,
        fields=fields or ADS_ARCHIVE_FIELDS,
        after=normalized_after,
        search_terms=search_terms,
        search_page_ids=page_ids,
        ad_active_status=status,
    )
    return normalize_collection(payload)
