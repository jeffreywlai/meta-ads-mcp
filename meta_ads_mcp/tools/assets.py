"""Read tools for Pages, media libraries, creative and audience usage, and field metadata."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, TypeAlias

from pydantic import BeforeValidator

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, NotFoundError, ValidationError
from meta_ads_mcp.field_reference import FIELD_REFERENCE, SDK_SOURCE
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.pagination import extract_paging
from meta_ads_mcp.tool_types import (
    FieldList,
    GraphId,
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


def _requests_field(fields: list[str], name: str) -> bool:
    """Whether a field list already asks for a field, with or without a sub-selection."""
    return any(field.split("{", 1)[0].split(".", 1)[0].strip() == name for field in fields)


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


MAX_VIDEO_ID_LOOKUPS = 25

# Numeric ids as a JSON array or a comma-separated string.
GraphIdList: TypeAlias = Annotated[
    list[GraphId],
    BeforeValidator(coerce_csv_string_list, json_schema_input_type=str | list[str]),
]


def _with_processing_status(video: dict[str, Any]) -> dict[str, Any]:
    """Add Meta's overall video_status as processing_status (null when status was not read)."""
    status = video.get("status")
    processing = status.get("video_status") if isinstance(status, dict) else None
    return {**video, "processing_status": processing if isinstance(processing, str) else None}


async def _lookup_videos(client: Any, video_ids: list[str], fields: list[str]) -> dict[str, Any]:
    """Read each requested video by id, keeping the ones Meta could not return."""
    items: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for video_id in video_ids:
        try:
            video = await client.get_object(video_id, fields=fields)
        except NotFoundError:
            missing.append({"id": video_id, "reason": "not found"})
            continue
        except MetaApiError as exc:
            missing.append({"id": video_id, "reason": exc.message, "code": exc.code})
            continue
        items.append(_with_processing_status(video))
    return {
        "items": items,
        "missing": missing,
        "summary": {
            "count": len(items),
            "requested": len(video_ids),
            "missing_count": len(missing),
            "complete": not missing,
            "lookup": "by id, one request per video",
        },
    }


@mcp_server.tool()
async def list_ad_videos(
    account_id: str,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
    video_ids: GraphIdList | None = None,
    title_contains: str | None = None,
) -> dict[str, Any]:
    """List the ad account's video library, each item with processing_status (ready, processing, error or expired). video_ids looks up up to 25 videos by id and names any Meta could not return. title_contains scans up to 10 pages and matches titles locally (case-insensitive); check summary.complete. Otherwise one page per call; follow summary.next_after."""
    account_id = _require_id(account_id, "account_id")
    _validate_limit(limit)
    requested_fields = normalize_field_list(fields) or AD_VIDEO_LIST_FIELDS
    title_filter = blank_to_none(title_contains)
    client = get_graph_api_client
    if video_ids is not None:
        ids = coerce_csv_string_list(video_ids)
        if not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value.strip().isdigit() for value in ids):
            raise ValidationError("video_ids must contain numeric video ids.")
        ids = list(dict.fromkeys(value.strip() for value in ids))
        if len(ids) > MAX_VIDEO_ID_LOOKUPS:
            raise ValidationError(f"video_ids accepts at most {MAX_VIDEO_ID_LOOKUPS} ids per call.")
        if title_filter or blank_to_none(after):
            raise ValidationError("video_ids is a direct lookup; do not combine it with title_contains or after.")
        return await _lookup_videos(client(), ids, list(requested_fields))
    if title_filter:
        _validate_limit(limit, maximum=MAX_SCAN_PAGE_SIZE)
        # The match runs here on each video's title, so the title must be read even when
        # the caller's fields leave it out; it is dropped from the rows afterwards.
        scan_fields = list(requested_fields)
        title_added = not _requests_field(scan_fields, "title")
        if title_added:
            scan_fields.append("title")
        scan = await _scan_edge(
            client(), normalize_account_id(account_id), "advideos",
            fields=scan_fields, params=_page_params(limit, after),
        )
        needle = title_filter.casefold()
        matched = [
            _with_processing_status(
                {key: value for key, value in video.items() if not (title_added and key == "title")}
            )
            for video in scan["items"]
            if isinstance(video, dict) and needle in str(video.get("title") or "").casefold()
        ]
        return {
            "items": matched,
            "summary": {
                "count": len(matched),
                "title_contains": title_filter,
                "matching": "local, case-insensitive, on title",
                "scanned_count": len(scan["items"]),
                "pages_read": scan["pages_read"],
                "complete": scan["complete"],
                "scan_capped": scan["scan_capped"],
                "next_after": scan["next_after"],
                "scan_note": _scan_note(scan, "videos"),
            },
        }
    payload = await client().list_objects(
        normalize_account_id(account_id),
        "advideos",
        fields=requested_fields,
        params=_page_params(limit, after),
    )
    normalized = _with_page_summary(normalize_collection(payload))
    normalized["items"] = [
        _with_processing_status(item) if isinstance(item, dict) else item for item in normalized["items"]
    ]
    return normalized


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


_INSTAGRAM_MEDIA_TYPES = frozenset({"IMAGE", "VIDEO", "CAROUSEL_ALBUM"})
_INSTAGRAM_PRODUCT_TYPES = frozenset({"FEED", "REELS", "STORY", "AD"})
_PRODUCT_TYPE_WORDS = {"REEL": "REELS", "STORIES": "STORY", "POST": "FEED", "POSTS": "FEED"}


def _media_window(since: str | None, until: str | None) -> tuple[datetime | None, datetime | None] | None:
    """Turn inclusive UTC dates into [start, end) datetimes."""
    since_text, until_text = blank_to_none(since), blank_to_none(until)
    if since_text is None and until_text is None:
        return None
    try:
        start = datetime.combine(date.fromisoformat(since_text), time.min, UTC) if since_text else None
        end = datetime.combine(date.fromisoformat(until_text), time.min, UTC) + timedelta(days=1) if until_text else None
    except ValueError as exc:
        raise ValidationError("since and until must be dates in YYYY-MM-DD format.") from exc
    if start is not None and end is not None and end <= start:
        raise ValidationError("until must be on or after since.")
    return start, end


def _media_filter(values: Any, name: str, allowed: frozenset[str]) -> set[str] | None:
    if values is None:
        return None
    items = coerce_csv_string_list(values)
    if not isinstance(items, list) or not items or any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValidationError(f"{name} must list values from {', '.join(sorted(allowed))}.")
    normalized = {_PRODUCT_TYPE_WORDS.get(item.strip().upper(), item.strip().upper()) for item in items}
    unknown = sorted(normalized - allowed)
    if unknown:
        raise ValidationError(f"{name} has unknown values {', '.join(unknown)}; use {', '.join(sorted(allowed))}.")
    return normalized


def _media_matches(
    media: dict[str, Any],
    window: tuple[datetime | None, datetime | None] | None,
    type_filter: set[str] | None,
    product_filter: set[str] | None,
) -> bool:
    if type_filter is not None and str(media.get("media_type") or "").upper() not in type_filter:
        return False
    if product_filter is not None and str(media.get("media_product_type") or "").upper() not in product_filter:
        return False
    if window is not None:
        try:
            created = datetime.strptime(str(media.get("timestamp") or ""), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return False
        start, end = window
        if (start is not None and created < start) or (end is not None and created >= end):
            return False
    return True


@mcp_server.tool()
async def list_instagram_media(
    instagram_account_id: str,
    boost_eligible_only: bool = True,
    limit: int = 25,
    after: str | None = None,
    fields: FieldList | None = None,
    source: str = "media",
    since: str | None = None,
    until: str | None = None,
    media_types: StringList | None = None,
    media_product_types: StringList | None = None,
) -> dict[str, Any]:
    """List an Instagram account's recent posts and reels (source=media) or active stories (source=stories), each marked boost_eligible with Meta's reason when not. Filters: since and until (YYYY-MM-DD, UTC, inclusive; sent to Meta for source=media and checked locally), media_types (IMAGE, VIDEO, CAROUSEL_ALBUM) and media_product_types (FEED, REELS, STORY, AD). boost_eligible_only and the type filters apply to this page locally; follow summary.next_after."""
    instagram_account_id = _require_id(instagram_account_id, "instagram_account_id")
    edge = (blank_to_none(source) or "").lower()
    if edge not in _INSTAGRAM_MEDIA_SOURCES:
        raise ValidationError(f"source must be one of {', '.join(_INSTAGRAM_MEDIA_SOURCES)}.")
    _validate_limit(limit)
    window = _media_window(since, until)
    type_filter = _media_filter(media_types, "media_types", _INSTAGRAM_MEDIA_TYPES)
    product_filter = _media_filter(media_product_types, "media_product_types", _INSTAGRAM_PRODUCT_TYPES)
    if product_filter is not None:
        if edge == "media" and product_filter == {"STORY"}:
            raise ValidationError("Stories are not returned by source=media; use source=stories.")
        if edge == "stories" and "STORY" not in product_filter:
            raise ValidationError("source=stories returns stories only; include STORY or use source=media.")
    requested_fields = list(normalize_field_list(fields) or INSTAGRAM_MEDIA_FIELDS)
    if not _requests_field(requested_fields, "boost_eligibility_info"):
        requested_fields.append(BOOST_ELIGIBILITY_FIELD)
    for needed, active in (("timestamp", window), ("media_type", type_filter), ("media_product_type", product_filter)):
        if active is not None and not _requests_field(requested_fields, needed):
            requested_fields.append(needed)
    params = _page_params(limit, after)
    if window is not None and edge == "media":
        if window[0] is not None:
            params["since"] = int(window[0].timestamp())
        if window[1] is not None:
            params["until"] = int(window[1].timestamp())
    payload = await get_graph_api_client().list_objects(
        instagram_account_id,
        edge,
        fields=requested_fields,
        params=params,
    )
    normalized = normalize_collection(payload)
    scanned = normalized["items"]
    items: list[dict[str, Any]] = []
    excluded_ineligible = 0
    unknown = 0
    excluded_by_filters = 0
    for media in scanned:
        if not _media_matches(media, window, type_filter, product_filter):
            excluded_by_filters += 1
            continue
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
    if window is not None or type_filter is not None or product_filter is not None:
        normalized["summary"].update({
            "filters": {
                "since": blank_to_none(since),
                "until": blank_to_none(until),
                "media_types": sorted(type_filter) if type_filter else None,
                "media_product_types": sorted(product_filter) if product_filter else None,
                "date_range_sent_to_meta": window is not None and edge == "media",
            },
            "excluded_by_filters": excluded_by_filters,
            "filter_note": (
                "Filters are applied to this page; a page can return few or no matches while more remain. "
                "Follow next_after until complete is true."
            ),
        })
    return normalized


_AUDIENCE_ADSET_SOURCES = ("both", "ads", "targeting")
AUDIENCE_TARGETING_ADSET_FIELDS = ["id", "name", "effective_status", "campaign_id", "targeting"]


def _audience_ids(targeting: Any, key: str) -> set[str]:
    """Ids listed under one targeting key, which Meta returns as a list of {id, name}."""
    if not isinstance(targeting, dict):
        return set()
    entries = targeting.get(key)
    if not isinstance(entries, list):
        return set()
    ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") is not None:
            ids.add(str(entry["id"]))
        elif isinstance(entry, (str, int)):
            ids.add(str(entry))
    return ids


def _audience_adset_entry(adsets: dict[str, dict[str, Any]], adset_id: str) -> dict[str, Any]:
    return adsets.setdefault(adset_id, {
        "id": adset_id,
        "name": None,
        "effective_status": None,
        "campaign_id": None,
        "ad_ids": [],
        "ad_count": 0,
        "found_by": [],
        "targeting_use": [],
    })


def _skipped_scan(reason: str) -> dict[str, Any]:
    return {"ran": False, "complete": False, "skipped_reason": reason}


@mcp_server.tool()
async def list_audience_adsets(
    audience_id: str,
    limit: int = 200,
    after: str | None = None,
    source: str = "both",
    account_id: str | None = None,
    targeting_after: str | None = None,
) -> dict[str, Any]:
    """List the ad sets that use one custom audience. source=both (default) combines two bounded scans: the audience's ads edge (finds ad sets with ads) and the ad account's ad sets, matching the audience id in targeting custom_audiences or excluded_custom_audiences (also finds ad sets with no ads). Each scan reads up to 10 pages; summary.complete is true only when every scan run reached the end. Continue with after (ads scan) and targeting_after (ad set scan). account_id defaults to the audience's account."""
    audience_id = _require_id(audience_id, "audience_id")
    _validate_limit(limit, maximum=MAX_SCAN_PAGE_SIZE)
    mode = (blank_to_none(source) or "").lower()
    if mode not in _AUDIENCE_ADSET_SOURCES:
        raise ValidationError(f"source must be one of {', '.join(_AUDIENCE_ADSET_SOURCES)}.")
    if mode == "ads" and (blank_to_none(targeting_after) or blank_to_none(account_id)):
        raise ValidationError("targeting_after and account_id apply to the ad set targeting scan; use source=both or targeting.")
    if mode == "targeting" and blank_to_none(after):
        raise ValidationError("after continues the ads scan; use targeting_after with source=targeting.")
    client = get_graph_api_client()
    adsets: dict[str, dict[str, Any]] = {}
    summary: dict[str, Any] = {"audience_id": audience_id, "source": mode}

    if mode in {"both", "ads"}:
        scan = await _scan_edge(client, audience_id, "ads", fields=AUDIENCE_AD_FIELDS, params=_page_params(limit, after))
        ads_without_adset = 0
        for ad in scan["items"]:
            nested = ad.get("adset") if isinstance(ad.get("adset"), dict) else {}
            adset_id = str(ad.get("adset_id") or nested.get("id") or "")
            if not adset_id:
                ads_without_adset += 1
                continue
            entry = _audience_adset_entry(adsets, adset_id)
            if "ads" not in entry["found_by"]:
                entry["found_by"].append("ads")
            for key in ("name", "effective_status"):
                if entry[key] is None and nested.get(key) is not None:
                    entry[key] = nested[key]
            if entry["campaign_id"] is None and ad.get("campaign_id"):
                entry["campaign_id"] = str(ad["campaign_id"])
            if ad.get("id"):
                entry["ad_ids"].append(str(ad["id"]))
            entry["ad_count"] += 1
        summary.update({
            "source_edge": f"{audience_id}/ads",
            "scanned_ads": len(scan["items"]),
            "ads_without_adset_id": ads_without_adset,
            "pages_read": scan["pages_read"],
            "scan_capped": scan["scan_capped"],
            "next_after": scan["next_after"],
            "scan_note": _scan_note(scan, "ads"),
        })
        ads_scan = {"ran": True, "complete": scan["complete"]}
    else:
        ads_scan = _skipped_scan("source=targeting")
        summary["next_after"] = None

    if mode in {"both", "targeting"}:
        resolved_account = blank_to_none(account_id)
        if resolved_account is None:
            audience = await client.get_object(audience_id, fields=["id", "account_id"])
            resolved_account = blank_to_none(str(audience.get("account_id") or ""))
        if resolved_account is None:
            targeting_scan = _skipped_scan(
                "Meta did not return the audience's account_id; pass account_id to scan ad set targeting."
            )
            summary["targeting_next_after"] = None
        else:
            account_edge = normalize_account_id(resolved_account)
            scan = await _scan_edge(
                client, account_edge, "adsets",
                fields=AUDIENCE_TARGETING_ADSET_FIELDS, params=_page_params(limit, targeting_after),
            )
            matched = 0
            for adset in scan["items"]:
                targeting = adset.get("targeting")
                uses = []
                if audience_id in _audience_ids(targeting, "custom_audiences"):
                    uses.append("included")
                if audience_id in _audience_ids(targeting, "excluded_custom_audiences"):
                    uses.append("excluded")
                if not uses or not adset.get("id"):
                    continue
                matched += 1
                entry = _audience_adset_entry(adsets, str(adset["id"]))
                entry["found_by"].append("targeting")
                entry["targeting_use"] = uses
                for key in ("name", "effective_status"):
                    if adset.get(key) is not None:
                        entry[key] = adset[key]
                if adset.get("campaign_id"):
                    entry["campaign_id"] = str(adset["campaign_id"])
            targeting_scan = {
                "ran": True,
                "complete": scan["complete"],
                "source_edge": f"{account_edge}/adsets",
                "scanned_adsets": len(scan["items"]),
                "matched_adsets": matched,
                "pages_read": scan["pages_read"],
                "scan_capped": scan["scan_capped"],
                "scan_note": _scan_note(scan, "ad sets"),
                "coverage_note": (
                    "Reads the ad sets Meta lists on the account's adsets edge with its default status filter, "
                    "which leaves out deleted ad sets."
                ),
            }
            summary["targeting_next_after"] = scan["next_after"]
    else:
        targeting_scan = _skipped_scan("source=ads")
        summary["targeting_next_after"] = None

    requested_scans = {"both": (ads_scan, targeting_scan), "ads": (ads_scan,), "targeting": (targeting_scan,)}[mode]
    summary.update({
        "count": len(adsets),
        "complete": all(scan["complete"] for scan in requested_scans),
        "ads_scan": ads_scan,
        "targeting_scan": targeting_scan,
        "derivation_note": (
            "found_by says which scan found each ad set; targeting_use says whether the audience is included or "
            "excluded. An ad set can repeat across continuation calls, so merge by id."
        ),
    })
    return {"items": list(adsets.values()), "summary": summary}


_METRIC_LEVELS = frozenset({"metric", "metrics", "insights"})
INSIGHTS_REFERENCE_URL = "https://developers.facebook.com/docs/marketing-api/reference/ad-account/insights"
# Operators of the Insights `filtering` parameter, from Meta's ad account insights reference.
INSIGHTS_FILTER_OPERATORS = (
    "EQUAL", "NOT_EQUAL", "GREATER_THAN", "GREATER_THAN_OR_EQUAL", "LESS_THAN", "LESS_THAN_OR_EQUAL",
    "IN_RANGE", "NOT_IN_RANGE", "CONTAIN", "NOT_CONTAIN", "CONTAINS_ANY", "CONTAINS_ALL", "NOT_CONTAINS_ANY",
    "STEM_MATCH", "IN", "NOT_IN", "STARTS_WITH", "ENDS_WITH", "ANY", "ALL", "AFTER", "BEFORE", "ON_OR_AFTER",
    "ON_OR_BEFORE", "NONE", "TOP",
)
# Metrics this server lists in meta_ads_mcp/context/insights_metrics.md.
SERVER_CORE_METRICS = (
    "spend", "impressions", "reach", "clicks", "ctr", "cpc", "cpm", "frequency", "actions", "action_values",
    "quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking",
)
# Metrics this server computes itself (meta_ads_mcp/diagnostics.py); Meta cannot sort or filter on them.
SERVER_DERIVED_METRICS: dict[str, dict[str, Any]] = {
    "conversions": {"formula": "the row's results value", "inputs": ["results"]},
    "conversion_value": {"formula": "the row's result_value", "inputs": ["result_value"]},
    "cvr": {"formula": "conversions / clicks", "inputs": ["conversions", "clicks"]},
    "cpa": {"formula": "spend / conversions", "inputs": ["spend", "conversions"]},
    "roas": {"formula": "conversion_value / spend", "inputs": ["conversion_value", "spend"]},
}

# AdsInsights field types from facebook-python-business-sdk 26.0.2 (adsinsights.py _field_types).
INSIGHTS_FIELD_TYPES: dict[str, tuple[str, ...]] = {
    'int': (
        'msa_seller_budget',
    ),
    'list<AdsActionStats>': (
        'action_values', 'actions', 'ad_click_actions', 'ad_impression_actions', 'advanced_actions_28d_view',
        'average_purchases_conversion_value', 'catalog_segment_actions', 'catalog_segment_value',
        'catalog_segment_value_mobile_purchase_roas', 'catalog_segment_value_omni_purchase_roas',
        'catalog_segment_value_website_purchase_roas', 'configurable_attribution_action',
        'configurable_attribution_actionvalue', 'configurable_audience_overlap_reach',
        'configurable_reachbyfrequency_action', 'conversion_lead_rate', 'conversion_leads', 'conversion_values',
        'conversions', 'converted_product_app_custom_event_fb_mobile_purchase',
        'converted_product_app_custom_event_fb_mobile_purchase_value', 'converted_product_offline_purchase',
        'converted_product_offline_purchase_value', 'converted_product_omni_purchase',
        'converted_product_omni_purchase_values', 'converted_product_quantity', 'converted_product_value',
        'converted_product_website_pixel_purchase', 'converted_product_website_pixel_purchase_value',
        'converted_promoted_product_app_custom_event_fb_mobile_purchase',
        'converted_promoted_product_app_custom_event_fb_mobile_purchase_value',
        'converted_promoted_product_offline_purchase', 'converted_promoted_product_offline_purchase_value',
        'converted_promoted_product_omni_purchase', 'converted_promoted_product_omni_purchase_values',
        'converted_promoted_product_quantity', 'converted_promoted_product_value',
        'converted_promoted_product_website_pixel_purchase',
        'converted_promoted_product_website_pixel_purchase_value', 'cost_per_15_sec_video_view',
        'cost_per_2_sec_continuous_video_view', 'cost_per_6_sec_video_view', 'cost_per_action_type',
        'cost_per_ad_click', 'cost_per_conversion', 'cost_per_conversion_lead',
        'cost_per_one_thousand_ad_impression', 'cost_per_outbound_click', 'cost_per_thruplay',
        'cost_per_unique_action_type', 'cost_per_unique_conversion', 'cost_per_unique_outbound_click',
        'instant_experience_outbound_clicks', 'interactive_component_tap', 'mobile_app_purchase_roas',
        'onsite_conversion_messaging_detected_purchase_deduped', 'outbound_clicks', 'outbound_clicks_ctr',
        'purchase_roas', 'total_postbacks_detailed', 'total_postbacks_detailed_v4', 'unique_actions',
        'unique_conversions', 'unique_outbound_clicks', 'unique_outbound_clicks_ctr',
        'unique_video_continuous_2_sec_watched_actions', 'unique_video_view_15_sec', 'video_15_sec_watched_actions',
        'video_30_sec_watched_actions', 'video_6_sec_watched_actions', 'video_avg_time_watched_actions',
        'video_continuous_2_sec_watched_actions', 'video_p100_watched_actions', 'video_p25_watched_actions',
        'video_p50_watched_actions', 'video_p75_watched_actions', 'video_p95_watched_actions', 'video_play_actions',
        'video_thruplay_watched_actions', 'video_time_watched_actions', 'video_view_per_impression', 'website_ctr',
        'website_purchase_roas'
    ),
    'list<AdsHistogramStats>': (
        'video_play_curve_actions', 'video_play_retention_0_to_15s_actions',
        'video_play_retention_20_to_60s_actions', 'video_play_retention_graph_actions'
    ),
    'list<Object>': (
        'cost_per_objective_result', 'cost_per_result', 'creative_diversity_data', 'creative_fatigue_summary',
        'creative_fatigued_ads', 'dda_results', 'link_clicks_per_results', 'objective_result_rate',
        'objective_results', 'result_rate', 'results'
    ),
    'string': (
        'account_currency', 'account_id', 'account_name', 'ad_id', 'ad_name', 'adset_end', 'adset_id', 'adset_name',
        'adset_start', 'advanced_reach_1d_lookback', 'advanced_reach_28d_lookback', 'advanced_reach_7d_lookback',
        'age_targeting', 'anchor_event_attribution_setting', 'anchor_events_performance_indicator',
        'attribution_setting', 'auction_bid', 'auction_competitiveness', 'auction_max_competitor_bid',
        'buying_type', 'campaign_id', 'campaign_name', 'canvas_avg_view_percent', 'canvas_avg_view_time', 'clicks',
        'configurable_audience_overlap_with_conv_action', 'configurable_audience_overlap_with_conv_converters',
        'configurable_audience_overlap_with_conv_exposure_cost',
        'configurable_audience_overlap_with_conv_exposure_impressions',
        'configurable_audience_overlap_with_conv_exposure_reach', 'configurable_placement_ptc_conversions',
        'configurable_placement_ptc_converters', 'configurable_placement_ptc_reach',
        'configurable_reachbyfrequency_converters_count', 'configurable_reachbyfrequency_impressions_cost',
        'configurable_reachbyfrequency_impressions_count', 'configurable_reachbyfrequency_reach',
        'conversion_rate_ranking', 'cost_per_dda_countby_convs', 'cost_per_estimated_ad_recallers',
        'cost_per_inline_link_click', 'cost_per_inline_post_engagement', 'cost_per_message_delivered',
        'cost_per_unique_click', 'cost_per_unique_inline_link_click', 'cpc', 'cpm', 'cpp', 'created_time',
        'creative_diversity_label', 'creative_diversity_score', 'creative_media_type', 'ctr', 'date_start',
        'date_stop', 'dda_countby_convs', 'engagement_rate_ranking', 'estimated_ad_recall_rate',
        'estimated_ad_recall_rate_lower_bound', 'estimated_ad_recall_rate_upper_bound', 'estimated_ad_recallers',
        'estimated_ad_recallers_lower_bound', 'estimated_ad_recallers_upper_bound', 'frequency',
        'full_view_impressions', 'full_view_reach', 'gender_targeting', 'impressions', 'inline_link_click_ctr',
        'inline_link_clicks', 'inline_post_engagement', 'instagram_profile_follow', 'instagram_profile_visits',
        'instagram_upcoming_event_reminders_set', 'instant_experience_clicks_to_open',
        'instant_experience_clicks_to_start', 'labels', 'landing_page_view_actions_per_link_click',
        'landing_page_view_per_link_click', 'landing_page_view_per_purchase_rate', 'location',
        'marketing_messages_click_rate_benchmark', 'marketing_messages_cost_per_delivered',
        'marketing_messages_cost_per_link_btn_click', 'marketing_messages_delivered',
        'marketing_messages_delivery_rate', 'marketing_messages_link_btn_click',
        'marketing_messages_link_btn_click_rate', 'marketing_messages_media_view_rate',
        'marketing_messages_phone_call_btn_click_rate', 'marketing_messages_quick_reply_btn_click',
        'marketing_messages_quick_reply_btn_click_rate', 'marketing_messages_read', 'marketing_messages_read_rate',
        'marketing_messages_read_rate_benchmark', 'marketing_messages_sent', 'marketing_messages_spend',
        'marketing_messages_spend_currency', 'messages_delivered', 'messages_delivered_ctr',
        'multi_event_conversion_attribution_setting', 'objective', 'opportunity_score_l4', 'optimization_goal',
        'place_page_name', 'playable_average_game_length', 'playable_game_start_rate', 'product_group_retailer_id',
        'product_retailer_id', 'product_views', 'purchase_per_landing_page_view', 'purchases_per_link_click',
        'qualifying_question_qualify_answer_rate', 'quality_ranking', 'reach', 'read_rate',
        'result_values_performance_indicator', 'shop_clicks', 'shops_assisted_purchases', 'social_spend', 'spend',
        'total_card_view', 'total_postbacks', 'unique_clicks', 'unique_ctr', 'unique_inline_link_click_ctr',
        'unique_inline_link_clicks', 'unique_link_clicks_ctr', 'updated_time', 'wish_bid'
    ),
}

_INSIGHTS_FIELD_TYPE_BY_NAME = {name: field_type for field_type, names in INSIGHTS_FIELD_TYPES.items() for name in names}


def _metric_item(name: str) -> dict[str, Any]:
    if name in SERVER_DERIVED_METRICS:
        derived = SERVER_DERIVED_METRICS[name]
        return {
            "name": name,
            "kind": "server_derived",
            "formula": derived["formula"],
            "inputs": list(derived["inputs"]),
            "meta_sort_values": None,
            "meta_filterable": False,
            "note": "Computed by this server after reading Insights; Meta cannot sort or filter on it.",
        }
    sdk_type = _INSIGHTS_FIELD_TYPE_BY_NAME[name]
    if sdk_type == "list<AdsActionStats>":
        shape = "action_list"
        sort_values = [f"{name}:<action_type>_ascending", f"{name}:<action_type>_descending"]
    elif sdk_type.startswith("list<"):
        shape = "list"
        sort_values = None
    else:
        shape = "scalar"
        sort_values = [f"{name}_ascending", f"{name}_descending"]
    return {
        "name": name,
        "kind": "insights_field",
        "sdk_type": sdk_type,
        "value_shape": shape,
        "in_server_metrics_reference": name in SERVER_CORE_METRICS,
        "meta_sort_values": sort_values,
        "description": None,
    }


def _metric_reference(field_names: Any) -> dict[str, Any]:
    requested = coerce_csv_string_list(field_names) if field_names is not None else None
    if requested is not None and (
        not isinstance(requested, list) or any(not isinstance(name, str) or not name.strip() for name in requested)
    ):
        raise ValidationError("field_names must contain nonblank field names.")
    known_names = set(_INSIGHTS_FIELD_TYPE_BY_NAME) | set(SERVER_DERIVED_METRICS)
    if requested:
        names = list(dict.fromkeys(name.strip() for name in requested))
    else:
        names = [*SERVER_CORE_METRICS, *SERVER_DERIVED_METRICS]
    items = [_metric_item(name) for name in names if name in known_names]
    summary: dict[str, Any] = {
        "count": len(items),
        "level": "metric",
        "unknown_fields": [name for name in names if name not in known_names],
        "source": (
            f"{SDK_SOURCE} AdsInsights field types; this server's metrics reference "
            "(meta_ads_mcp/context/insights_metrics.md) and derived metrics (meta_ads_mcp/diagnostics.py)"
        ),
        "sort_syntax": (
            "Meta's Insights sort parameter takes one value: a field name with _ascending or _descending, "
            "or actions:<action_type>_ascending for action lists. Meta does not publish which fields it accepts."
        ),
        "filter_operators": list(INSIGHTS_FILTER_OPERATORS),
        "filter_syntax": (
            "Meta's Insights filtering parameter takes objects with field, operator and value. "
            "Meta does not publish which fields accept which operators; it rejects unsupported combinations."
        ),
        "aliases_note": (
            "Meta publishes no list of metric aliases. The names this server computes itself are listed with "
            "kind server_derived and their formulas."
        ),
        "description_note": "Meta's live field descriptions are not available (metadata=1 was removed in v25); see reference_url.",
        "reference_url": INSIGHTS_REFERENCE_URL,
    }
    if not requested:
        summary["all_insights_fields"] = sorted(_INSIGHTS_FIELD_TYPE_BY_NAME)
        summary["listing_note"] = (
            "Items cover this server's core and derived metrics; name any field from all_insights_fields "
            "in field_names for its details."
        )
    return {"items": items, "summary": summary}


@mcp_server.tool()
async def get_field_reference(
    level: str,
    field_names: StringList | None = None,
    object_id: str | None = None,
) -> dict[str, Any]:
    """Look up campaign, ad set or ad field types, whether each field can be updated, and allowed values where known. With object_id and field_names it also reads those fields' current values. level=metric covers Insights metrics: each field's type, value shape, Meta's sort syntax, this server's derived metrics with formulas, and the documented filter operators. Descriptions live at summary.reference_url."""
    raw_level = (blank_to_none(level) or "").lower()
    if raw_level in _METRIC_LEVELS:
        if blank_to_none(object_id) is not None:
            raise ValidationError("object_id applies to campaign, adset and ad levels; metrics have no stored value to read.")
        return _metric_reference(field_names)
    normalized_level = _LEVEL_ALIASES.get(raw_level)
    if normalized_level is None:
        raise ValidationError("level must be one of campaign, adset, ad, metric.")
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
