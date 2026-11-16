"""Targeting and planning tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.api_compat import validate_targeting_placements
from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none, collection_fault, normalize_collection, read_collection_page
from meta_ads_mcp.pagination import extract_paging
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


MAX_CATEGORY_PAGES = 10


def _name_matches(row: dict[str, Any], needle: str | None) -> bool:
    """True when there is no query, or the row's name contains it (both case-folded)."""
    if needle is None:
        return True
    name = row.get("name")
    return isinstance(name, str) and needle in name.casefold()


async def _search_categories(
    category_class: str,
    query: str | None,
    limit: int,
    after: str | None,
) -> dict[str, Any]:
    """Read Meta's categories of one class page by page and keep the rows whose name matches.

    Reading stops when limit rows have matched at the end of a page, when Meta reports no
    further page, or after MAX_CATEGORY_PAGES pages. complete is true only when Meta reported
    no further page, every entry read was an object, and every matching row is returned. A
    page without a readable data list stops the read and is never shown as an empty result.
    """
    if limit < 1:
        raise ValidationError("limit must be 1 or more.")
    raw_query = blank_to_none(query)
    needle = raw_query.strip().casefold() if raw_query else None
    client = get_graph_api_client()
    cursor = blank_to_none(after)
    seen: set[str] = set()
    matched: list[dict[str, Any]] = []
    rows_read = pages_read = unreadable_rows = 0
    reasons: list[str] = []
    next_after: str | None = None
    more_pages = False
    while True:
        payload = await client.search_targeting_categories(category_class=category_class, **_cursor_options(cursor))
        pages_read += 1
        rows, bad_rows, page_unreadable = read_collection_page(payload)
        if page_unreadable:
            reasons.append(collection_fault(payload) or "Meta's answer could not be read.")
            break
        unreadable_rows += bad_rows
        rows_read += len(rows)
        matched.extend(row for row in rows if _name_matches(row, needle))
        paging = extract_paging(payload)
        if not paging.get("next"):
            break
        new_cursor = paging.get("after")
        if not new_cursor:
            reasons.append("Meta reported another page but gave no cursor to read it.")
            break
        if new_cursor == cursor or new_cursor in seen:
            reasons.append("Meta returned the same page cursor twice, so the read stopped.")
            break
        seen.add(new_cursor)
        cursor = new_cursor
        if len(matched) >= limit:
            more_pages, next_after = True, new_cursor
            break
        if pages_read >= MAX_CATEGORY_PAGES:
            more_pages, next_after = True, new_cursor
            reasons.append(f"Stopped after {MAX_CATEGORY_PAGES} pages; Meta reported more. Continue with after=summary.next_after.")
            break
    if unreadable_rows:
        entries = "1 entry" if unreadable_rows == 1 else f"{unreadable_rows} entries"
        reasons.append(f"{entries} in Meta's answer could not be read, so the list may be missing rows.")
    items = matched[:limit]
    cut = len(matched) - len(items)
    if cut:
        # Meta's cursor points past the whole page, so it would skip the rows cut here.
        next_after = None
        reasons.append(
            f"{cut} more matching row(s) were read but not returned because of limit; raise limit to see them."
        )
    summary: dict[str, Any] = {
        "count": len(items),
        "rows_read": rows_read,
        "rows_matched": len(matched),
        "pages_read": pages_read,
        "complete": not reasons and not more_pages,
        "truncated": more_pages or cut > 0,
        "next_after": next_after,
    }
    if reasons:
        summary["incomplete_reason"] = " ".join(reasons)
    elif not matched:
        summary["note"] = (
            f"Meta returned {rows_read} {category_class} categories; none has a name containing the query."
            if rows_read
            else f"Meta returned no categories for class {category_class!r}; check the class name."
        )
    return {"items": items, "paging": {"before": None, "after": next_after, "next": None}, "summary": summary}


@mcp_server.tool()
async def search_behaviors(
    query: str | None = None,
    account_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants behavior-based targeting options rather than interests or demographics. Keeps Meta's behaviors whose name contains query, ignoring case (no query keeps all); account_id is not needed. summary gives rows_read and rows_matched; follow summary.next_after with after until summary.complete is true."""
    return await _search_categories("behaviors", query, limit, after)


@mcp_server.tool()
async def get_targeting_categories(
    category_class: str,
    query: str | None = None,
    account_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user knows the targeting category class they want and needs matching options, for example behaviors, demographics, life_events, industries, income, family_statuses, user_device or user_os. Keeps rows whose name contains query, ignoring case (no query keeps all); account_id is not needed. summary gives rows_read and rows_matched; follow summary.next_after with after until summary.complete is true."""
    if not category_class:
        raise ValidationError("category_class is required.")
    return await _search_categories(category_class, query, limit, after)


@mcp_server.tool()
async def search_demographics(
    demographic_class: str = "demographics",
    query: str | None = None,
    account_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants demographic targeting options such as age, education, or life-stage categories. Keeps rows of demographic_class whose name contains query, ignoring case (no query keeps all); account_id is not needed. summary gives rows_read and rows_matched; follow summary.next_after with after until summary.complete is true."""
    if not demographic_class:
        raise ValidationError("demographic_class is required.")
    return await _search_categories(demographic_class, query, limit, after)


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
