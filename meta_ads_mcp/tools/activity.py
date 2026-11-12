"""Activity and change-history tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import json
import re
import time as clock
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastmcp.tools.tool import ToolResult

from meta_ads_mcp.activity_scope import validate_activity_scope_arguments
from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import OVERFLOW_ARTIFACT_STORE, mcp_server
from meta_ads_mcp.errors import MetaAdsError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.money import SUPPORTED_CURRENCY_DECIMAL_PLACES
from meta_ads_mcp.normalize import blank_to_none
from meta_ads_mcp.tool_types import FieldList, StrictStringList, coerce_strict_csv_string_list, normalize_field_list

DEFAULT_ACTIVITY_FIELDS = [
    "actor_id",
    "actor_name",
    "application_id",
    "application_name",
    "date_time_in_timezone",
    "event_time",
    "event_type",
    "extra_data",
    "object_id",
    "object_name",
    "object_type",
    "tool",
    "translated_event_type",
]

ACTIVITY_CATEGORIES = {
    "ACCOUNT", "AD", "AD_KEYWORDS", "AD_SET", "AUDIENCE", "BID", "BUDGET",
    "CAMPAIGN", "DATE", "STATUS", "TARGETING",
}
ACTIVITY_DATA_SOURCES = {"CALYPSO", "TAO", "TAO_AD_ACCOUNT", "TAO_AD_STATUS"}
IDENTITY_FIELDS = ["event_time", "event_type", "object_id", "object_type"]
MAX_EXPORT_BYTES = 32 * 1024 * 1024
MAX_PARENT_LOOKUPS = 200
EXPORT_SECONDS = 45

HISTORY_LIMITATIONS = [
    "Completeness covers API-returned events for the effective time window, scope, and filters only. "
    "Meta retention, permissions, unlogged actions, and delayed events can limit coverage; "
    "absence of an event does not prove no change occurred.",
    "Meta uses legacy object types: CAMPAIGN_GROUP=campaign, CAMPAIGN=adset, ADGROUP=ad. "
    "Use entity_level and preserve unknown event types.",
    "Deletion can appear as a run-status update. Inspect extra_data_parsed, not only the event name. "
    "Missing old/new values mean Meta did not supply those details; never invent them.",
    "Activity payment_amount values remain raw minor currency units; use extra_data_units for currency metadata.",
    "Creative detail URLs can be generated per request. Raw exported URLs are references, not archived media.",
]

@dataclass(frozen=True, slots=True)
class ActivityScope:
    """Resolved activity query scope."""

    level: str
    object_id: str
    account_id: str
    parent_id: str
    object_filter_id: str | None


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve an ad account id, using the default when omitted."""
    account_id = blank_to_none(account_id)
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


def _configured_account_id(account_id: str | None) -> str | None:
    """Resolve an explicit or configured account id without requiring one."""
    if account_id := blank_to_none(account_id):
        return normalize_account_id(account_id)
    if default_account_id := blank_to_none(get_settings().default_account_id):
        return normalize_account_id(default_account_id)
    return None


async def _resolve_object_account_id(
    *,
    object_id: str,
    account_id: str | None,
    client: Any,
) -> str:
    """Resolve and verify an object's owning account."""
    configured_account_id = _configured_account_id(account_id)
    try:
        payload = await client.get_object(object_id, fields=["account_id"])
    except (MetaAdsError, httpx.HTTPError) as exc:
        if configured_account_id is not None:
            return configured_account_id
        raise ValidationError(
            f"account_id could not be derived from object '{object_id}'. "
            "Pass account_id explicitly or set META_DEFAULT_ACCOUNT_ID. "
            f"Ownership lookup failed: {exc}"
        ) from exc
    derived_account_id = payload.get("account_id")
    if derived_account_id is None or not str(derived_account_id).strip():
        if configured_account_id is not None:
            return configured_account_id
        raise ValidationError(
            f"account_id could not be derived from object '{object_id}'. "
            "Pass account_id explicitly or set META_DEFAULT_ACCOUNT_ID."
        )
    normalized_derived_account_id = normalize_account_id(str(derived_account_id).strip())
    if (
        configured_account_id is not None
        and configured_account_id != normalized_derived_account_id
    ):
        raise ValidationError(
            f"Object '{object_id}' belongs to account "
            f"'{normalized_derived_account_id}', but the supplied or default "
            f"account resolves to '{configured_account_id}'."
        )
    return normalized_derived_account_id


async def _resolve_scope(
    *,
    level: str | None,
    object_id: str | None,
    account_id: str | None,
    campaign_id: str | None,
    adset_id: str | None,
    ad_id: str | None,
    client: Any,
) -> ActivityScope:
    """Resolve user scope into the ad-account activities edge plus optional object filter."""
    try:
        validated = validate_activity_scope_arguments(
            level=level,
            object_id=object_id,
            account_id=account_id,
            campaign_id=campaign_id,
            adset_id=adset_id,
            ad_id=ad_id,
        )
    except ValueError as error:
        raise ValidationError(str(error)) from error
    normalized_level = validated.level
    normalized_object_id = validated.object_id
    normalized_account_id = validated.account_id
    provided_object_aliases = (
        [validated.object_alias]
        if validated.object_alias is not None
        else []
    )

    if normalized_object_id is not None:
        if normalized_level == "account":
            resolved_account_id = _resolve_account_id(normalized_object_id)
            return ActivityScope(
                level="account",
                object_id=resolved_account_id,
                account_id=resolved_account_id,
                parent_id=resolved_account_id,
                object_filter_id=None,
            )
        resolved_account_id = await _resolve_object_account_id(
            object_id=normalized_object_id,
            account_id=normalized_account_id,
            client=client,
        )
        return ActivityScope(
            level=normalized_level,
            object_id=normalized_object_id,
            account_id=resolved_account_id,
            parent_id=resolved_account_id,
            object_filter_id=normalized_object_id,
        )

    if provided_object_aliases:
        alias_level, alias_object_id = provided_object_aliases[0]
        resolved_account_id = await _resolve_object_account_id(
            object_id=alias_object_id,
            account_id=normalized_account_id,
            client=client,
        )
        return ActivityScope(
            level=alias_level,
            object_id=alias_object_id,
            account_id=resolved_account_id,
            parent_id=resolved_account_id,
            object_filter_id=alias_object_id,
        )

    if normalized_level in (None, "account"):
        resolved_account_id = _resolve_account_id(normalized_account_id)
        return ActivityScope(
            level="account",
            object_id=resolved_account_id,
            account_id=resolved_account_id,
            parent_id=resolved_account_id,
            object_filter_id=None,
        )
    raise ValidationError("The activity scope could not be resolved. Provide an account or object scope.")


def _activity_params(
    *,
    limit: int,
    after: str | None,
    since: str | None,
    until: str | None,
    category: str | None,
    business_id: str | None,
    uid: str | int | None,
    object_filter_id: str | None,
) -> dict[str, Any]:
    """Build Graph API parameters for the activities edge."""
    if limit <= 0:
        raise ValidationError("limit must be greater than 0.")
    params: dict[str, Any] = {"limit": limit}
    if after := blank_to_none(after):
        params["after"] = after
    if since := blank_to_none(since):
        params["since"] = since
    if until := blank_to_none(until):
        params["until"] = until
    if category := blank_to_none(category):
        params["category"] = category.upper()
    if business_id := blank_to_none(business_id):
        params["business_id"] = business_id
    if isinstance(uid, str):
        uid = blank_to_none(uid)
    if uid is not None:
        params["uid"] = uid
    if object_filter_id := blank_to_none(object_filter_id):
        params["oid"] = object_filter_id
    return params


def _category(value: str | None) -> str | None:
    value = (blank_to_none(value) or "").upper().replace(" ", "_").replace("-", "_")
    value = {"ADSET": "AD_SET", "ALL": "", "ANY": ""}.get(value, value)
    if value and value not in ACTIVITY_CATEGORIES:
        raise ValidationError(f"category must be one of {sorted(ACTIVITY_CATEGORIES)} or omitted for all types.")
    return value or None


def _extra_ids(value: Any) -> list[str]:
    values = coerce_strict_csv_string_list(value)
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValidationError("extra_oids must contain nonblank object ID strings.")
    if len(values) > 1000:
        raise ValidationError("extra_oids supports at most 1000 explicitly known related objects.")
    return list(dict.fromkeys(v.strip() for v in values))


def _timestamp(value: str, zone: tzinfo, *, end: bool = False) -> datetime:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value) + (timedelta(days=1) if end else timedelta())
            return datetime.combine(day, time.min, zone).astimezone(timezone.utc)
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return datetime.fromtimestamp(float(value), timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp needs an offset")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValidationError("Use YYYY-MM-DD, Unix seconds, or an ISO-8601 timestamp with an explicit UTC offset.") from exc


def _history_window(since: str | None, until: str | None, time_zone: str, *, require_explicit: bool = False) -> dict[str, Any]:
    since, until = blank_to_none(since), blank_to_none(until)
    if require_explicit and (since is None or until is None):
        raise ValidationError("export_change_history requires explicit since and until. Choose the requested period; do not silently substitute a recent window for all history.")
    try:
        zone = timezone.utc if time_zone == "UTC" else ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValidationError("time_zone must be an IANA zone such as America/New_York or UTC.") from exc
    now = datetime.now(timezone.utc)
    requested_end = _timestamp(until, zone, end=True) if until else now
    end = min(requested_end, now)
    start = _timestamp(since, zone) if since else end - timedelta(days=7)
    if start >= end:
        raise ValidationError("since must precede the effective until (no later than the collection time).")
    return {
        "requested": {"since": since, "until": until},
        "since": start.isoformat(), "until": end.isoformat(), "time_zone": time_zone,
        "since_inclusive": True, "until_exclusive": True,
        "date_only_until_includes_entire_day": True,
        "defaulted_since": since is None, "defaulted_until": until is None,
        "future_until_clamped_to_collection_time": requested_end > now,
        "collected_at": now.isoformat(),
    }


def _entity_level(item: dict[str, Any]) -> str:
    raw = str(item.get("object_type") or "").upper()
    known = {
        "CAMPAIGN_GROUP": "campaign", "CAMPAIGN": "adset", "ADGROUP": "ad",
        "AD": "ad", "AD_SET": "adset", "ADSET": "adset",
        "ACCOUNT": "account", "AD_ACCOUNT": "account", "ADACCOUNT": "account",
        "AUDIENCE": "audience", "CUSTOM_AUDIENCE": "audience", "CREATIVE": "creative",
    }
    if raw in known:
        return known[raw]
    event = str(item.get("event_type") or "")
    if "audience" in event and not event.startswith(("ad_account_", "adaccount_")):
        return "audience"
    return "unknown"


def _paging(payload: dict[str, Any]) -> tuple[bool, str | None]:
    paging = payload.get("paging") or {}
    has_more = bool(paging.get("next"))
    cursor = (paging.get("cursors") or {}).get("after")
    if has_more and not cursor and isinstance(paging["next"], str):
        cursor = parse_qs(urlsplit(paging["next"]).query).get("after", [None])[0]
    return has_more, cursor if has_more else None


def _within_window(item: dict[str, Any], window: dict[str, Any]) -> bool:
    # Graph may include the end boundary. Keep unparseable/missing timestamps rather than discard events.
    try:
        event_time = _timestamp(str(item.get("event_time") or ""), timezone.utc)
    except ValidationError:
        return True
    return datetime.fromisoformat(window["since"]) <= event_time < datetime.fromisoformat(window["until"])


class _HistoryScopeMatcher:
    """Resolve only objects observed in account activity, including readable deleted objects."""

    def __init__(self, client: Any, scope: ActivityScope, include_descendants: bool, extra_oids: list[str], deadline: float):
        self.client, self.scope = client, scope
        self.include_descendants = include_descendants
        self.explicit_ids = {scope.object_id, *extra_oids}
        self.cache: dict[str, tuple[str, str]] = {}
        self.lookups = 0
        self.deadline = deadline

    async def match(self, item: dict[str, Any]) -> tuple[str, str]:
        oid = str(item.get("object_id") or "")
        if self.scope.level == "account":
            return "included", "account_activity"
        if not oid:
            return "unresolved", "missing_object_id"
        if oid in self.explicit_ids:
            return "included", "explicit_object_id"
        if not self.include_descendants or self.scope.level == "ad":
            return "excluded", "different_object"
        level = item["entity_level"]
        candidates = {"adset", "ad"} if self.scope.level == "campaign" else {"ad"}
        if level not in candidates:
            return ("unresolved", "unknown_entity_level") if level == "unknown" else ("excluded", "different_entity_level")
        if oid not in self.cache:
            if self.lookups >= MAX_PARENT_LOOKUPS or clock.monotonic() >= self.deadline:
                return "unresolved", "parent_lookup_budget_reached"
            parent_field = "campaign_id" if self.scope.level == "campaign" else "adset_id"
            self.lookups += 1
            try:
                async with asyncio.timeout(max(0.001, self.deadline - clock.monotonic())):
                    obj = await self.client.get_object(oid, fields=[parent_field])
                parent = obj.get(parent_field)
                self.cache[oid] = (
                    ("included", "resolved_parent") if str(parent) == self.scope.object_id
                    else ("excluded", "different_parent") if parent
                    else ("unresolved", "parent_id_unavailable")
                )
            except (MetaAdsError, httpx.HTTPError, TimeoutError):
                self.cache[oid] = ("unresolved", "parent_lookup_failed")
        return self.cache[oid]


def _payment_amount_units(value: Any, path: str) -> list[dict[str, Any]]:
    """Describe typed activity amounts without converting Meta's raw values."""
    units: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("type") == "payment_amount":
            currency = value.get("currency")
            metadata: dict[str, Any] = {
                "path": path,
                "currency": currency,
                "unit": "minor_currency_units",
            }
            decimal_places = (
                SUPPORTED_CURRENCY_DECIMAL_PLACES.get(currency)
                if isinstance(currency, str) else None
            )
            if decimal_places is not None:
                metadata["major_unit_divisor"] = 10 ** decimal_places
            units.append(metadata)
        for key, child in value.items():
            escaped_key = str(key).replace("~", "~0").replace("/", "~1")
            units.extend(_payment_amount_units(child, f"{path}/{escaped_key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            units.extend(_payment_amount_units(child, f"{path}/{index}"))
    return units


def _normalize_activity_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add parsed extra_data when Meta returns JSON encoded details."""
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        normalized = dict(item)
        normalized["entity_level"] = _entity_level(item)
        extra_data = normalized.get("extra_data")
        if isinstance(extra_data, str) and extra_data.strip():
            try:
                normalized["extra_data_parsed"] = json.loads(extra_data)
            except json.JSONDecodeError:
                pass
        parsed = normalized.get("extra_data_parsed", extra_data)
        units = _payment_amount_units(
            parsed,
            "/extra_data_parsed" if "extra_data_parsed" in normalized else "/extra_data",
        )
        if units:
            normalized["extra_data_units"] = units
        normalized["change_details_available"] = {
            "old_value": isinstance(parsed, dict) and "old_value" in parsed,
            "new_value": isinstance(parsed, dict) and "new_value" in parsed,
            "raw_extra_data": extra_data is not None,
        }
        normalized_items.append(normalized)
    return normalized_items


async def _read_history(
    *, tool_name: str, level: str | None, object_id: str | None,
    account_id: str | None, campaign_id: str | None, adset_id: str | None,
    ad_id: str | None, since: str | None, until: str | None, time_zone: str,
    category: str | None, business_id: str | None, uid: str | int | None,
    fields: FieldList | None, limit: int, after: str | None,
    include_descendants: bool, extra_oids: StrictStringList | None,
    data_source: str | None, max_pages: int,
) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise ValidationError("limit must be between 1 and 500 events per Meta page.")
    if not 1 <= max_pages <= 1000:
        raise ValidationError("max_pages must be between 1 and 1000; a reached budget returns an explicit partial export.")
    exporting = tool_name == "export_change_history"
    window = _history_window(since, until, time_zone, require_explicit=exporting)
    category = _category(category)
    data_source = (blank_to_none(data_source) or "").upper() or None
    if data_source is not None and data_source not in ACTIVITY_DATA_SOURCES:
        raise ValidationError(f"data_source must be one of {sorted(ACTIVITY_DATA_SOURCES)} or omitted for Meta's default.")
    extra = _extra_ids(extra_oids)
    after = blank_to_none(after)
    client = get_graph_api_client()
    scope = await _resolve_scope(level=level, object_id=object_id, account_id=account_id,
        campaign_id=campaign_id, adset_id=adset_id, ad_id=ad_id, client=client)
    if extra and scope.level == "account":
        raise ValidationError("Account history already includes all objects. Use extra_oids only with an object scope.")
    selected = normalize_field_list(fields) or DEFAULT_ACTIVITY_FIELDS
    requested_fields = list(dict.fromkeys([*selected, *IDENTITY_FIELDS]))
    scanning_account = include_descendants and scope.level in {"campaign", "adset"}
    params = _activity_params(limit=limit, after=after, since=window["since"], until=window["until"],
        category=category, business_id=business_id, uid=uid,
        object_filter_id=None if scanning_account else scope.object_filter_id)
    if extra and not scanning_account:
        params["extra_oids"] = extra
    if data_source:
        params["data_source"] = data_source
    deadline = clock.monotonic() + EXPORT_SECONDS
    matcher = _HistoryScopeMatcher(client, scope, include_descendants, extra, deadline)
    items: list[dict[str, Any]] = []
    pages = scanned = excluded = boundary_excluded = collected_bytes = 0
    has_more, cursor, stop_reason, error = True, after, None, None
    seen_cursors = {after} if after else set()
    while pages < max_pages:
        if pages and (clock.monotonic() >= deadline or collected_bytes >= MAX_EXPORT_BYTES):
            stop_reason = "time_budget" if clock.monotonic() >= deadline else "byte_budget"
            break
        try:
            async with asyncio.timeout(max(0.001, deadline - clock.monotonic())):
                payload = await client.list_objects(scope.parent_id, "activities", fields=requested_fields, params=params)
            raw_items = payload.get("data")
            if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
                raise ValidationError("Meta returned an invalid activity collection.")
        except (MetaAdsError, httpx.HTTPError, TimeoutError) as exc:
            if not pages:
                if isinstance(exc, TimeoutError):
                    raise ValidationError("The first history page timed out; no events were retrieved. Retry with a smaller date window or page limit.") from exc
                raise
            stop_reason = "api_error"
            error = {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                     "retry_after_seconds": getattr(exc, "retry_after_seconds", None),
                     "message": "A later activity page could not be fetched. Already fetched events are preserved; retry the next_call."}
            break
        pages += 1
        scanned += len(raw_items)
        for item in _normalize_activity_items(raw_items):
            if not _within_window(item, window):
                boundary_excluded += 1
                continue
            match, reason = await matcher.match(item)
            if match == "excluded":
                excluded += 1
                continue
            item["scope_match"] = {"status": match, "reason": reason}
            # Keep unresolved events visible, even if a deleted object can no longer be read.
            items.append(item)
            collected_bytes += len(json.dumps(item, ensure_ascii=True, default=str).encode())
        has_more, next_cursor = _paging(payload)
        if not has_more:
            cursor = None
            break
        if not next_cursor or next_cursor in seen_cursors:
            stop_reason = "missing_cursor" if not next_cursor else "repeated_cursor"
            cursor = None
            break
        cursor = next_cursor
        seen_cursors.add(cursor)
        params["after"] = cursor
    if has_more and not stop_reason:
        stop_reason = "page_limit"
    unresolved = [item for item in items if item["scope_match"]["status"] == "unresolved"]
    source_exhausted = not has_more and stop_reason is None
    complete = source_exhausted and after is None and not unresolved
    filters = {"category": category, "uid": params.get("uid"), "business_id": params.get("business_id"), "data_source": data_source}
    query_args = {
        "level": scope.level, "object_id": scope.object_id, "account_id": scope.account_id,
        "since": window["since"], "until": window["until"], "time_zone": time_zone,
        **filters, "include_descendants": include_descendants, "extra_oids": extra or None, "limit": limit,
    }
    if exporting:
        query_args["max_pages"] = max_pages
    else:
        query_args["fields"] = requested_fields
    query_args = {key: value for key, value in query_args.items() if value is not None}
    next_call = {"name": tool_name, "arguments": {**query_args, "after": cursor}} if has_more and cursor else None
    export_args = {key: value for key, value in query_args.items() if key != "fields"}
    export_args["include_descendants"] = True
    export_args.pop("max_pages", None)
    warnings = list(HISTORY_LIMITATIONS)
    if has_more:
        warnings.insert(0, "MORE HISTORY REMAINS. This response is partial; follow next_call before drawing conclusions, even when items is empty.")
    if after:
        warnings.append("This is a continuation part. Combine it with every preceding part; reaching the last page does not make this part the full history. A combined retrieval is complete for its query only when it starts at the beginning, follows every issued cursor without gaps, exhausts the API pages, and has no unresolved scope candidates.")
    if not include_descendants and scope.level in {"campaign", "adset"}:
        warnings.append("Object-only scope excludes child changes. Use include_descendants=true or the suggested export_call for the hierarchy.")
    if any(value is not None for value in filters.values()):
        warnings.append("Filters are active. A category is not an entity-level selector and can omit changes on that entity. Omit category and uid when requesting every change.")
    if window["defaulted_since"]:
        warnings.append("No since was supplied: this request covers only the last seven days ending at effective until, not all retained history. Choose explicit export dates for a wider request.")
    if window["future_until_clamped_to_collection_time"]:
        warnings.append("The requested end extends into the future. Coverage stops at collected_at; later changes require a new request.")
    omitted_fields = sorted(set(DEFAULT_ACTIVITY_FIELDS) - set(requested_fields))
    if omitted_fields:
        warnings.append("A field projection omits change details. export_change_history always requests every supported activity field.")
    if unresolved:
        warnings.append("Some events have unresolved parent relationships and are included as candidates, not confirmed descendants. Do not discard them. Supply known related IDs in extra_oids or use an account-wide export.")
    if stop_reason in {"missing_cursor", "repeated_cursor"}:
        warnings.append("Meta pagination could not advance safely. No complete export is claimed. Retry smaller explicit time windows; no reusable next_call is available.")
    # Preserve the legacy nullable URL shape without returning credential-bearing Graph URLs.
    # next_call remains authoritative for MCP scope resolution and continuation metadata.
    next_url = None
    if has_more and cursor:
        query = {**params, "after": cursor, "fields": ",".join(requested_fields)}
        encoded = urlencode({key: json.dumps(value) if isinstance(value, (list, dict)) else value for key, value in query.items()})
        next_url = f"https://graph.facebook.com/{get_settings().api_version}/{scope.parent_id}/activities?{encoded}"
    return {
        "items": items, "complete": complete, "has_more": has_more,
        "complete_means": "All API-returned events for this effective window and selected scope/filters were retrieved from the beginning, with no unresolved scope candidates. It does not guarantee all real-world changes or all-time history.",
        "paging": {"after": cursor, "next": next_url, "before": None},
        "next_call": next_call,
        "export_call": {"name": "export_change_history", "arguments": export_args},
        "unfiltered_account_export_call": {"name": "export_change_history", "arguments": {
            "account_id": scope.account_id, "since": window["since"], "until": window["until"], "time_zone": time_zone,
            **({"business_id": params["business_id"]} if params.get("business_id") else {}),
        }},
        "scope": {"level": scope.level, "object_id": scope.object_id, "account_id": scope.account_id},
        "coverage": {
            "status": "complete_api_query" if complete else "partial_api_query",
            "api_pages_exhausted": source_exhausted, "starts_at_beginning": after is None,
            "all_meta_changes_guaranteed": False, "api_retention_verified": False,
            "api_version": get_settings().api_version,
            "unfiltered_account_all_levels": scope.level == "account" and not any(value is not None for value in filters.values()),
            "window": window, "filters": filters, "fields": requested_fields,
            "omitted_detail_fields": omitted_fields,
            "scope_mode": "account_all_levels" if scope.level == "account" else "account_scan_with_parent_resolution" if scanning_account else "object_only_or_explicit_ids",
            "includes_descendants": scope.level == "account" or scanning_account,
            "unresolved_scope_events": len(unresolved), "parent_lookups": matcher.lookups,
            "stop_reason": stop_reason, "error": error,
        },
        "summary": {
            "count": len(items), "pages_fetched": pages, "scanned_events": scanned,
            "excluded_other_scope_events": excluded, "excluded_time_boundary_events": boundary_excluded,
            "by_entity_level": dict(sorted(Counter(item["entity_level"] for item in items).items())),
            "by_event_type": dict(sorted(Counter(str(item.get("event_type") or "unknown") for item in items).items())),
            "events_without_old_value": sum(not item["change_details_available"]["old_value"] for item in items),
            "events_without_new_value": sum(not item["change_details_available"]["new_value"] for item in items),
            "events_without_actor_id": sum(not item.get("actor_id") for item in items),
            "events_without_event_time": sum(not item.get("event_time") for item in items),
            "level": scope.level, "object_id": scope.object_id, "account_id": scope.account_id,
            "object_filter_id": params.get("oid"), "date_window": window["requested"],
            **filters, "default_window": "The server freezes a seven-day window when since is omitted; explicit dates are required for exports.",
        },
        "ai_instructions": warnings,
    }


@mcp_server.tool(tags={"read-only"})
async def list_change_history(
    level: str | None = None,
    object_id: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    ad_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    category: str | None = None,
    business_id: str | None = None,
    uid: str | int | None = None,
    fields: FieldList | None = None,
    limit: int = 50,
    after: str | None = None,
    time_zone: str = "UTC",
    include_descendants: bool = False,
    extra_oids: StrictStringList | None = None,
    data_source: str | None = None,
) -> dict[str, Any]:
    """Read ONE history page; follow next_call or use export_change_history for every API-returned event. Account scope includes all levels; campaign/adset scope includes children only with include_descendants=true. Read coverage and ai_instructions before claiming completeness. Omitted since means seven days. Date-only until includes that entire day in time_zone (default UTC); timestamp until is exclusive. Omit category/uid for all change types. Payment amounts remain raw minor currency units."""
    return await _read_history(tool_name="list_change_history", level=level, object_id=object_id,
        account_id=account_id, campaign_id=campaign_id, adset_id=adset_id, ad_id=ad_id,
        since=since, until=until, time_zone=time_zone, category=category, business_id=business_id,
        uid=uid, fields=fields, limit=limit, after=after, include_descendants=include_descendants,
        extra_oids=extra_oids, data_source=data_source, max_pages=1)


@mcp_server.tool(tags={"read-only"})
async def export_change_history(
    since: str,
    until: str,
    level: str | None = None,
    object_id: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    ad_id: str | None = None,
    time_zone: str = "UTC",
    include_descendants: bool = True,
    extra_oids: StrictStringList | None = None,
    category: str | None = None,
    business_id: str | None = None,
    uid: str | int | None = None,
    data_source: str | None = None,
    limit: int = 100,
    max_pages: int = 100,
    after: str | None = None,
    inline_limit: int = 10,
) -> dict[str, Any]:
    """Export complete account, campaign, ad set, or ad change history: who changed budgets, bids, status, targeting, names, schedules, or creatives, with raw before/after details and explicit coverage. Supply explicit since/until; descendants are included by default. Retrieve ALL artifact pages using read_overflow_artifact; inline items are only a preview. If complete=false inspect coverage and next_call: budgets/errors can require more export parts, and unresolved descendants remain visible. No event-type allowlist or deduplication discards changes. Date-only until includes the whole day in time_zone (UTC by default). API retention/permissions can prevent a guarantee of every real-world change."""
    if not 0 <= inline_limit <= 100:
        raise ValidationError("inline_limit must be between 0 and 100; all fetched events remain in the export artifact.")
    payload = await _read_history(tool_name="export_change_history", level=level, object_id=object_id,
        account_id=account_id, campaign_id=campaign_id, adset_id=adset_id, ad_id=ad_id,
        since=since, until=until, time_zone=time_zone, category=category, business_id=business_id,
        uid=uid, fields=None, limit=limit, after=after, include_descendants=include_descendants,
        extra_oids=extra_oids, data_source=data_source, max_pages=max_pages)
    payload["ai_instructions"].insert(0, "Read and save every event in this artifact BEFORE following next_call for another export part. Artifact read complete means only this artifact was read; check coverage and next_call for source completeness. Combine all export parts, retaining raw details and unresolved scope candidates, before claiming a full picture.")
    if payload["next_call"]:
        payload["next_call"]["arguments"]["inline_limit"] = inline_limit
    try:
        export_id, artifact_bytes = OVERFLOW_ARTIFACT_STORE.create(
            ToolResult(content=[], structured_content=payload), tool_name="export_change_history")
    except (OSError, TypeError, ValueError) as exc:
        raise ValidationError("History export could not be saved. No successful export is claimed. Retry with fewer max_pages/a smaller date window or check export storage settings.") from exc
    return {
        **payload, "items": payload["items"][:inline_limit], "export_id": export_id,
        "format": "json", "record_count": len(payload["items"]),
        "returned_count": min(len(payload["items"]), inline_limit),
        "delivery": {
            "inline_is_preview": True, "artifact_contains_all_fetched_events": True,
            "artifact_bytes": artifact_bytes, "ttl_seconds": OVERFLOW_ARTIFACT_STORE.ttl_seconds,
            "read_call": {"name": "read_overflow_artifact", "arguments": {"export_id": export_id, "item_offset": 0}},
            "byte_read_call": {"name": "read_overflow_artifact", "arguments": {"export_id": export_id, "offset": 0}},
            "instructions": "Follow next_item_offset until artifact complete=true; use byte mode for an oversized item or the full JSON document. Also follow next_call for additional Meta pages. Delete artifacts only after every part is saved/read.",
        },
    }
