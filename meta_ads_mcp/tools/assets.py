"""Read tools for Pages, media libraries, creative and audience usage, and field metadata."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.field_reference import FIELD_REFERENCE, SDK_SOURCE
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.pagination import extract_paging
from meta_ads_mcp.tool_types import (
    FieldList,
    StringList,
    coerce_csv_string_list,
    normalize_field_list,
)

PAGE_LIST_FIELDS = ["id", "name", "category", "link"]
USER_PAGE_LIST_FIELDS = [*PAGE_LIST_FIELDS, "tasks"]

AD_IMAGE_LIST_FIELDS = [
    "id",
    "hash",
    "name",
    "url",
    "permalink_url",
    "width",
    "height",
    "original_width",
    "original_height",
    "status",
    "created_time",
    "updated_time",
]

AD_VIDEO_LIST_FIELDS = [
    "id",
    "title",
    "length",
    "picture",
    "permalink_url",
    "status",
    "created_time",
    "updated_time",
]

CREATIVE_AD_FIELDS = [
    "id",
    "name",
    "status",
    "effective_status",
    "adset_id",
    "campaign_id",
    "creative{id}",
]

AUDIENCE_AD_FIELDS = [
    "id",
    "name",
    "effective_status",
    "adset_id",
    "campaign_id",
    "adset{id,name,effective_status}",
]

BOOST_ELIGIBILITY_FIELD = "boost_eligibility_info{eligible_to_boost,boost_ineligible_reason}"
INSTAGRAM_MEDIA_FIELDS = [
    "id",
    "caption",
    "media_type",
    "media_product_type",
    "timestamp",
    "permalink",
    "media_url",
    "thumbnail_url",
    BOOST_ELIGIBILITY_FIELD,
]

MAX_SCAN_PAGES = 10
MAX_SCAN_PAGE_SIZE = 500

_PAGE_SOURCES = ("user", "ad_account", "business")
_INSTAGRAM_MEDIA_SOURCES = ("media", "stories")

_LEVEL_ALIASES = {
    "campaign": "campaign",
    "adset": "adset",
    "ad_set": "adset",
    "ad": "ad",
}
_REFERENCE_URLS = {
    "campaign": "https://developers.facebook.com/docs/marketing-api/reference/ad-campaign-group",
    "adset": "https://developers.facebook.com/docs/marketing-api/reference/ad-campaign",
    "ad": "https://developers.facebook.com/docs/marketing-api/reference/adgroup",
}
_FILTER_SUPPORT = {
    "name": "Graph filtering with operator CONTAIN on list edges (list_campaigns, list_adsets and list_ads name_contains use it).",
    "effective_status": "effective_status parameter on the campaigns, adsets and ads list edges.",
}


def _require_id(value: str | None, name: str) -> str:
    """Return a stripped id or raise before any network call."""
    normalized = blank_to_none(value)
    if normalized is None:
        raise ValidationError(f"{name} is required.")
    return normalized


def _validate_limit(limit: int, *, maximum: int | None = None) -> None:
    """Reject page sizes Meta would refuse or that would never terminate."""
    if limit < 1:
        raise ValidationError("limit must be positive.")
    if maximum is not None and limit > maximum:
        raise ValidationError(f"limit must be at most {maximum}.")


def _page_params(limit: int, after: str | None) -> dict[str, Any]:
    """Build one Graph page request."""
    params: dict[str, Any] = {"limit": limit}
    cursor = blank_to_none(after)
    if cursor:
        params["after"] = cursor
    return params


def _with_page_summary(normalized: dict[str, Any]) -> dict[str, Any]:
    """Report page completeness the same way list_audiences does."""
    has_more = bool(normalized["paging"].get("next"))
    normalized["summary"].update({
        "complete": not has_more,
        "truncated": has_more,
        "next_after": normalized["paging"].get("after") if has_more else None,
    })
    return normalized


async def _scan_edge(
    client: Any,
    parent_id: str,
    edge: str,
    *,
    fields: list[str],
    params: dict[str, Any],
) -> dict[str, Any]:
    """Read up to MAX_SCAN_PAGES pages of one edge and report how far the scan got."""
    items: list[dict[str, Any]] = []
    query = dict(params)
    seen_cursors: set[str] = set()
    pages_read = 0
    complete = False
    next_after: str | None = None
    stop_reason = "page_cap"
    while pages_read < MAX_SCAN_PAGES:
        payload = await client.list_objects(parent_id, edge, fields=fields, params=query)
        pages_read += 1
        items.extend(payload.get("data", []))
        paging = extract_paging(payload)
        if not paging.get("next"):
            complete = True
            stop_reason = "end_of_edge"
            break
        cursor = paging.get("after")
        if not cursor or cursor in seen_cursors:
            stop_reason = "missing_or_repeated_cursor"
            next_after = None
            break
        seen_cursors.add(cursor)
        next_after = cursor
        query["after"] = cursor
    return {
        "items": items,
        "pages_read": pages_read,
        "complete": complete,
        "scan_capped": stop_reason == "page_cap",
        "stop_reason": stop_reason,
        "next_after": None if complete else next_after,
    }


def _scan_note(scan: dict[str, Any], noun: str) -> str:
    """Explain plainly whether the scan reached the end of Meta's list."""
    examined = len(scan["items"])
    if scan["complete"]:
        return f"Scan complete: all {examined} {noun} on the edge were examined."
    if scan["stop_reason"] == "page_cap":
        return (
            f"Scan incomplete: stopped after the {MAX_SCAN_PAGES}-page cap with {examined} {noun} examined. "
            "Call again with after=summary.next_after to continue."
        )
    return (
        f"Scan incomplete: Meta returned a missing or repeated cursor after {examined} {noun} were examined, "
        "so the scan cannot be continued safely."
    )


@mcp_server.tool()
async def list_pages(
    source: str,
    account_id: str | None = None,
    business_id: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List Facebook Pages by source: user (Pages the token's user can promote), ad_account (Pages promoted by that ad account; needs account_id) or business (Pages the business owns; needs business_id). One page per call; follow summary.next_after."""
    normalized_source = (blank_to_none(source) or "").lower()
    if normalized_source not in _PAGE_SOURCES:
        raise ValidationError(f"source must be one of {', '.join(_PAGE_SOURCES)}.")
    account_id = blank_to_none(account_id)
    business_id = blank_to_none(business_id)
    _validate_limit(limit)
    params = _page_params(limit, after)
    if normalized_source == "user":
        if account_id or business_id:
            raise ValidationError("source=user lists the token user's Pages; omit account_id and business_id.")
        parent_id, edge = "me", "accounts"
        params["is_promotable"] = True
        default_fields = USER_PAGE_LIST_FIELDS
    elif normalized_source == "ad_account":
        if account_id is None:
            raise ValidationError("account_id is required when source=ad_account.")
        if business_id:
            raise ValidationError("source=ad_account does not use business_id.")
        parent_id, edge = normalize_account_id(account_id), "promote_pages"
        default_fields = PAGE_LIST_FIELDS
    else:
        if business_id is None:
            raise ValidationError("business_id is required when source=business.")
        if account_id:
            raise ValidationError("source=business does not use account_id.")
        parent_id, edge = business_id, "owned_pages"
        default_fields = PAGE_LIST_FIELDS
    payload = await get_graph_api_client().list_objects(
        parent_id,
        edge,
        fields=normalize_field_list(fields) or default_fields,
        params=params,
    )
    normalized = _with_page_summary(normalize_collection(payload))
    normalized["summary"].update({"source": normalized_source, "edge": f"{parent_id}/{edge}"})
    return normalized


@mcp_server.tool()
async def list_ad_images(
    account_id: str,
    hashes: StringList | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List the ad account's image library, optionally only the given image hashes. One page per call; follow summary.next_after."""
    account_id = _require_id(account_id, "account_id")
    _validate_limit(limit)
    params = _page_params(limit, after)
    if hashes is not None:
        hash_values = coerce_csv_string_list(hashes)
        if not isinstance(hash_values, list) or not hash_values or any(
            not isinstance(value, str) or not value.strip() for value in hash_values
        ):
            raise ValidationError("hashes must contain nonblank image hashes.")
        params["hashes"] = [value.strip() for value in hash_values]
    payload = await get_graph_api_client().list_objects(
        normalize_account_id(account_id),
        "adimages",
        fields=normalize_field_list(fields) or AD_IMAGE_LIST_FIELDS,
        params=params,
    )
    return _with_page_summary(normalize_collection(payload))


@mcp_server.tool()
async def list_ad_videos(
    account_id: str,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List the ad account's video library. One page per call; follow summary.next_after."""
    account_id = _require_id(account_id, "account_id")
    _validate_limit(limit)
    payload = await get_graph_api_client().list_objects(
        normalize_account_id(account_id),
        "advideos",
        fields=normalize_field_list(fields) or AD_VIDEO_LIST_FIELDS,
        params=_page_params(limit, after),
    )
    return _with_page_summary(normalize_collection(payload))


@mcp_server.tool()
async def list_creative_ads(
    creative_id: str,
    account_id: str | None = None,
    effective_status: StringList | None = None,
    limit: int = 200,
    after: str | None = None,
) -> dict[str, Any]:
    """Find the ads that use one creative. Meta has no server-side creative filter, so this scans the account's ads (limit per request, at most 10 requests per call) and matches locally; check summary.complete and continue with after=summary.next_after."""
    creative_id = _require_id(creative_id, "creative_id")
    account_id = blank_to_none(account_id)
    _validate_limit(limit, maximum=MAX_SCAN_PAGE_SIZE)
    client = get_graph_api_client()
    if account_id is None:
        creative = await client.get_object(creative_id, fields=["id", "account_id"])
        account_id = blank_to_none(str(creative.get("account_id") or ""))
        if account_id is None:
            raise ValidationError("account_id is required: Meta did not return the creative's account_id.")
    resolved_account_id = normalize_account_id(account_id)
    params = _page_params(limit, after)
    if effective_status:
        params["effective_status"] = list(effective_status)
    scan = await _scan_edge(client, resolved_account_id, "ads", fields=CREATIVE_AD_FIELDS, params=params)
    matched: list[dict[str, Any]] = []
    for ad in scan["items"]:
        creative = ad.get("creative") if isinstance(ad.get("creative"), dict) else {}
        if str(creative.get("id") or "") != creative_id:
            continue
        item = {key: value for key, value in ad.items() if key != "creative"}
        item["creative_id"] = creative_id
        matched.append(item)
    return {
        "items": matched,
        "summary": {
            "count": len(matched),
            "creative_id": creative_id,
            "account_id": resolved_account_id,
            "scanned_count": len(scan["items"]),
            "pages_read": scan["pages_read"],
            "complete": scan["complete"],
            "scan_capped": scan["scan_capped"],
            "next_after": scan["next_after"],
            "matching": "local",
            "effective_status_filter": list(effective_status) if effective_status else None,
            "scan_note": _scan_note(scan, "ads"),
        },
    }


def _boost_eligibility(item: dict[str, Any]) -> tuple[bool | None, str | None]:
    """Read Meta's boost_eligibility_info without guessing when it is absent."""
    info = item.get("boost_eligibility_info")
    if not isinstance(info, dict) or not isinstance(info.get("eligible_to_boost"), bool):
        return None, None
    eligible = info["eligible_to_boost"]
    return eligible, None if eligible else info.get("boost_ineligible_reason")


@mcp_server.tool()
async def list_instagram_media(
    instagram_account_id: str,
    boost_eligible_only: bool = True,
    limit: int = 25,
    after: str | None = None,
    fields: FieldList | None = None,
    source: str = "media",
) -> dict[str, Any]:
    """List an Instagram account's recent posts and reels (source=media) or active stories (source=stories), each marked boost_eligible with Meta's reason when not. boost_eligible_only filters this page locally; follow summary.next_after."""
    instagram_account_id = _require_id(instagram_account_id, "instagram_account_id")
    edge = (blank_to_none(source) or "").lower()
    if edge not in _INSTAGRAM_MEDIA_SOURCES:
        raise ValidationError(f"source must be one of {', '.join(_INSTAGRAM_MEDIA_SOURCES)}.")
    _validate_limit(limit)
    requested_fields = list(normalize_field_list(fields) or INSTAGRAM_MEDIA_FIELDS)
    if not any(field.split("{", 1)[0].strip() == "boost_eligibility_info" for field in requested_fields):
        requested_fields.append(BOOST_ELIGIBILITY_FIELD)
    payload = await get_graph_api_client().list_objects(
        instagram_account_id,
        edge,
        fields=requested_fields,
        params=_page_params(limit, after),
    )
    normalized = normalize_collection(payload)
    scanned = normalized["items"]
    items: list[dict[str, Any]] = []
    excluded_ineligible = 0
    unknown = 0
    for media in scanned:
        eligible, reason = _boost_eligibility(media)
        if eligible is None:
            unknown += 1
        elif eligible is False:
            excluded_ineligible += 1
        if boost_eligible_only and eligible is not True:
            continue
        items.append({**media, "boost_eligible": eligible, "boost_ineligible_reason": reason})
    normalized["items"] = items
    normalized["summary"] = {"count": len(items)}
    _with_page_summary(normalized)
    normalized["summary"].update({
        "source": edge,
        "boost_eligible_only": boost_eligible_only,
        "scanned_count": len(scanned),
        "excluded_ineligible": excluded_ineligible if boost_eligible_only else 0,
        "eligibility_unknown": unknown,
        "ineligible_count": excluded_ineligible,
        "eligibility_note": (
            "boost_eligible is null when Meta returned no boost_eligibility_info for the item; "
            "such items are left out when boost_eligible_only is true."
        ),
    })
    return normalized


@mcp_server.tool()
async def list_audience_adsets(
    audience_id: str,
    limit: int = 200,
    after: str | None = None,
) -> dict[str, Any]:
    """List the ad sets whose ads use one custom audience, grouped from the audience's ads edge (Meta has no ad set edge on audiences). Ad sets with no ads are not found. Scans up to 10 requests per call; check summary.complete."""
    audience_id = _require_id(audience_id, "audience_id")
    _validate_limit(limit, maximum=MAX_SCAN_PAGE_SIZE)
    client = get_graph_api_client()
    scan = await _scan_edge(
        client,
        audience_id,
        "ads",
        fields=AUDIENCE_AD_FIELDS,
        params=_page_params(limit, after),
    )
    adsets: dict[str, dict[str, Any]] = {}
    ads_without_adset = 0
    for ad in scan["items"]:
        nested = ad.get("adset") if isinstance(ad.get("adset"), dict) else {}
        adset_id = str(ad.get("adset_id") or nested.get("id") or "")
        if not adset_id:
            ads_without_adset += 1
            continue
        entry = adsets.setdefault(adset_id, {
            "id": adset_id,
            "name": None,
            "effective_status": None,
            "campaign_id": None,
            "ad_ids": [],
            "ad_count": 0,
        })
        for key in ("name", "effective_status"):
            if entry[key] is None and nested.get(key) is not None:
                entry[key] = nested[key]
        if entry["campaign_id"] is None and ad.get("campaign_id"):
            entry["campaign_id"] = str(ad["campaign_id"])
        if ad.get("id"):
            entry["ad_ids"].append(str(ad["id"]))
        entry["ad_count"] += 1
    return {
        "items": list(adsets.values()),
        "summary": {
            "count": len(adsets),
            "audience_id": audience_id,
            "source_edge": f"{audience_id}/ads",
            "scanned_ads": len(scan["items"]),
            "ads_without_adset_id": ads_without_adset,
            "pages_read": scan["pages_read"],
            "complete": scan["complete"],
            "scan_capped": scan["scan_capped"],
            "next_after": scan["next_after"],
            "scan_note": _scan_note(scan, "ads"),
            "derivation_note": (
                "Derived from the ads Meta reports as using this audience; an ad set can repeat "
                "across continuation calls, so merge by id."
            ),
        },
    }


@mcp_server.tool()
async def get_field_reference(
    level: str,
    field_names: StringList | None = None,
    object_id: str | None = None,
) -> dict[str, Any]:
    """Look up campaign, ad set or ad field types, whether each field can be updated, and allowed values where known. With object_id and field_names it also reads those fields' current values. Descriptions live at summary.reference_url."""
    normalized_level = _LEVEL_ALIASES.get((blank_to_none(level) or "").lower())
    if normalized_level is None:
        raise ValidationError("level must be one of campaign, adset, ad.")
    reference = FIELD_REFERENCE[normalized_level]
    requested = coerce_csv_string_list(field_names) if field_names is not None else None
    if requested is not None and (
        not isinstance(requested, list) or any(not isinstance(name, str) or not name.strip() for name in requested)
    ):
        raise ValidationError("field_names must contain nonblank field names.")
    names = list(dict.fromkeys(name.strip() for name in requested)) if requested else sorted(reference)
    known = [name for name in names if name in reference]
    unknown = [name for name in names if name not in reference]
    object_id = blank_to_none(object_id)
    current: dict[str, Any] | None = None
    if object_id is not None:
        if not requested:
            raise ValidationError("field_names is required with object_id, so only the named fields are read.")
        if not known:
            raise ValidationError(f"No known fields to read for level {normalized_level}; unknown: {', '.join(unknown)}.")
        current = await get_graph_api_client().get_object(object_id, fields=known)
    items = []
    for name in known:
        entry = reference[name]
        item: dict[str, Any] = {
            "name": name,
            "type": entry["type"],
            "updatable": entry["updatable"],
            "allowed_values": list(entry["allowed_values"]) if entry["allowed_values"] else None,
            "filter_support": _FILTER_SUPPORT.get(name),
            "description": None,
        }
        if current is not None:
            item["current_value"] = current.get(name)
        items.append(item)
    return {
        "items": items,
        "summary": {
            "count": len(items),
            "level": normalized_level,
            "unknown_fields": unknown,
            "object_id": object_id,
            "source": f"{SDK_SOURCE} (field types, updatable flags and enum values)",
            "source_note": (
                "Meta stopped returning live field metadata (metadata=1) in Graph API v25, so this "
                "reference comes from Meta's SDK source. allowed_values can include legacy values that "
                "Meta now rejects for new objects; descriptions are on the reference page."
            ),
            "reference_url": _REFERENCE_URLS[normalized_level],
        },
    }
