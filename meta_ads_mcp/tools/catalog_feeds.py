"""Product catalog feeds, feed uploads, feed rules, and catalog event sources.

These tools work on Meta product catalogs (commerce catalogs), not on this
server's own tool catalog.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.diagnostics import build_finding
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.normalize import blank_to_none, normalize_collection, to_int
from meta_ads_mcp.schemas import analysis_response, creation_response, mutation_response

UPLOAD_FIELDS = [
    "id",
    "start_time",
    "end_time",
    "input_method",
    "filename",
    "url",
    "error_count",
    "warning_count",
    "num_detected_items",
    "num_persisted_items",
    "num_invalid_items",
    "num_deleted_items",
]
UPLOAD_ERROR_FIELDS = [
    "id",
    "summary",
    "description",
    "severity",
    "error_type",
    "affected_surfaces",
    "total_count",
]
FEED_FIELDS = [
    "id",
    "name",
    "file_name",
    "country",
    "default_currency",
    "deletion_enabled",
    "delimiter",
    "encoding",
    "quoted_fields_mode",
    "ingestion_source_type",
    "item_sub_type",
    "override_type",
    "primary_feeds",
    "migrated_from_feed_id",
    "product_count",
    "created_time",
    "schedule",
    "update_schedule",
    "latest_upload{" + ",".join(UPLOAD_FIELDS) + "}",
]
FEED_LIST_FIELDS = [
    "id",
    "name",
    "file_name",
    "ingestion_source_type",
    "product_count",
    "schedule",
    "update_schedule",
    "latest_upload{id,start_time,end_time,error_count,warning_count}",
]
FEED_UPDATE_READ_FIELDS = [
    "id",
    "name",
    "default_currency",
    "deletion_enabled",
    "delimiter",
    "encoding",
    "quoted_fields_mode",
    "schedule",
    "update_schedule",
    "migrated_from_feed_id",
]
RULE_FIELDS = ["id", "attribute", "rule_type", "params"]
EVENT_SOURCE_FIELDS = ["id", "name", "source_type"]
EVENT_STAT_FIELDS = [
    "date_start",
    "date_stop",
    "event",
    "event_source",
    "total_matched_content_ids",
    "total_unmatched_content_ids",
    "unique_matched_content_ids",
    "unique_unmatched_content_ids",
    "total_content_ids_matched_other_catalogs",
    "unique_content_ids_matched_other_catalogs",
]
DA_CHECK_FIELDS = ["key", "title", "description", "result", "user_message", "action_uri"]
CATALOG_SCAN_FIELDS = ["id", "name"]
PIXEL_CANDIDATE_FIELDS = ["id", "name", "last_fired_time", "is_unavailable"]

SCHEDULE_INTERVALS = ("HOURLY", "DAILY", "WEEKLY", "MONTHLY")
SCHEDULE_DAYS = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")
SCHEDULE_KEYS = (
    "url",
    "interval",
    "interval_count",
    "hour",
    "minute",
    "day_of_week",
    "day_of_month",
    "timezone",
    "username",
    "password",
)
SCHEDULE_URL_SCHEMES = ("http", "https", "ftp")
FEED_RULE_TYPES = (
    "mapping_rule",
    "value_mapping_rule",
    "letter_case_rule",
    "fallback_rule",
    "regex_replace_rule",
)
LETTER_CASE_TYPES = ("to_upper", "to_lower", "capitalize_all", "capitalize_first")
VALUE_MAPPING_MAX_PAIRS = 10
VALUE_MAPPING_MAX_LENGTH = 20
DA_CHECK_SOURCE_TYPES = ("PIXEL", "APP")
DA_CHECK_EVALUATED_RESULTS = ("passed", "failed")

MAX_CATALOGS_SCANNED = 50
CATALOG_SCAN_PAGE_SIZE = 25
MAX_EDGE_PAGES = 10
MAX_PIXEL_CANDIDATES_CHECKED = 10
DEFAULT_MATCH_RATE_THRESHOLD = 0.8
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_DERIVED_METHOD = (
    "Derived by this server from Meta's documented catalog data; Meta publishes no "
    "single health or recommendation endpoint for catalog event sources."
)
_MATCH_RATE_NOTE = (
    "match_rate = total_matched_content_ids / (total_matched_content_ids + "
    "total_unmatched_content_ids) from the catalog's event_stats edge. This ratio and "
    "the threshold are computed here; Meta does not publish a match-rate threshold."
)


def _require_id(value: str | None, name: str) -> str:
    """Return a stripped id or raise before any network call."""
    normalized = blank_to_none(value)
    if normalized is None:
        raise ValidationError(f"{name} is required.")
    return normalized


def _validate_limit(limit: int) -> None:
    if limit < 1:
        raise ValidationError("limit must be positive.")


def _page_params(limit: int, after: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    cursor = blank_to_none(after)
    if cursor:
        params["after"] = cursor
    return params


def _paged_collection(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape one Graph page the way list_audiences does."""
    normalized = normalize_collection(payload)
    has_more = bool(normalized["paging"].get("next"))
    normalized["summary"].update({
        "complete": not has_more,
        "truncated": has_more,
        "next_after": normalized["paging"].get("after") if has_more else None,
    })
    return normalized


async def _collect_edge(
    client: Any,
    parent_id: str,
    edge: str,
    *,
    fields: list[str],
    page_size: int,
    max_items: int | None = None,
    max_pages: int = MAX_EDGE_PAGES,
    extra_params: dict[str, Any] | None = None,
    stop_reasons: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read an edge across pages up to a cap; report whether the end was reached.

    The end is reached only when Meta's page has no next link. When the read stops
    early, a plain-language reason is appended to stop_reasons if a list is given.
    """
    items: list[dict[str, Any]] = []
    after: str | None = None

    def stopped(reason: str) -> None:
        if stop_reasons is not None:
            stop_reasons.append(f"{edge} of {parent_id}: {reason}")

    for _ in range(max_pages):
        params: dict[str, Any] = {**(extra_params or {}), "limit": page_size}
        if after:
            params["after"] = after
        payload = await client.list_objects(parent_id, edge, fields=fields, params=params)
        page = normalize_collection(payload)
        items.extend(page["items"])
        has_next = bool(page["paging"].get("next"))
        if max_items is not None and len(items) >= max_items:
            more = len(items) > max_items or has_next
            if more:
                stopped(f"stopped at the {max_items}-item cap; more items exist.")
            return items[:max_items], not more
        if not has_next:
            return items, True
        after = page["paging"].get("after")
        if not after:
            stopped("Meta reported another page but gave no cursor to read it.")
            return items, False
    stopped(f"stopped after {max_pages} pages; more pages exist.")
    return items, False


def _object_id(value: Any) -> str | None:
    """Read an id from a nested Graph object or a bare id string."""
    if isinstance(value, dict):
        candidate = value.get("id")
        return str(candidate) if candidate not in (None, "") else None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None


def _validate_http_url(value: Any, *, name: str, schemes: tuple[str, ...]) -> str:
    """Accept only absolute URLs with an allowed scheme and a host."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be an absolute {'/'.join(schemes)} URL.")
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in schemes or not parts.netloc:
        raise ValidationError(
            f"{name} must be an absolute URL using {', '.join(schemes)}; got {url!r}."
        )
    return url


def _bounded_int(value: Any, *, name: str, low: int, high: int | None) -> None:
    """Validate an integer-like schedule value without changing its form."""
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer.")
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    elif isinstance(value, int):
        number = value
    else:
        raise ValidationError(f"{name} must be an integer.")
    if number < low or (high is not None and number > high):
        bound = f"{low} to {high}" if high is not None else f"at least {low}"
        raise ValidationError(f"{name} must be {bound}.")


def _normalize_schedule(value: Any, *, name: str) -> dict[str, Any]:
    """Validate a feed fetch schedule against Meta's ProductFeedSchedule fields."""
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object with url and interval.")
    unknown = sorted(set(value) - set(SCHEDULE_KEYS))
    if unknown:
        raise ValidationError(
            f"{name} has unsupported keys: {', '.join(unknown)}. "
            f"Accepted keys: {', '.join(SCHEDULE_KEYS)}."
        )
    normalized = dict(value)
    normalized["url"] = _validate_http_url(
        value.get("url"), name=f"{name}.url", schemes=SCHEDULE_URL_SCHEMES
    )
    interval = value.get("interval")
    interval_value = interval.strip().upper() if isinstance(interval, str) else None
    if interval_value not in SCHEDULE_INTERVALS:
        raise ValidationError(
            f"{name}.interval must be one of {', '.join(SCHEDULE_INTERVALS)}."
        )
    normalized["interval"] = interval_value
    if "hour" in value:
        _bounded_int(value["hour"], name=f"{name}.hour", low=0, high=23)
    if "minute" in value:
        _bounded_int(value["minute"], name=f"{name}.minute", low=0, high=59)
    if "day_of_month" in value:
        _bounded_int(value["day_of_month"], name=f"{name}.day_of_month", low=1, high=31)
    if "interval_count" in value:
        _bounded_int(value["interval_count"], name=f"{name}.interval_count", low=1, high=None)
    if "day_of_week" in value:
        day = value["day_of_week"]
        day_value = day.strip().upper() if isinstance(day, str) else None
        if day_value not in SCHEDULE_DAYS:
            raise ValidationError(f"{name}.day_of_week must be one of {', '.join(SCHEDULE_DAYS)}.")
        normalized["day_of_week"] = day_value
    for key in ("timezone", "username", "password"):
        if key in value and not isinstance(value[key], str):
            raise ValidationError(f"{name}.{key} must be a string.")
    return normalized


def _redact_schedules(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a payload for display with schedule passwords removed."""
    shown = dict(payload)
    for key in ("schedule", "update_schedule"):
        schedule = shown.get(key)
        if isinstance(schedule, dict) and "password" in schedule:
            shown[key] = {**schedule, "password": "[redacted]"}
    return shown


def _upload_status(upload: dict[str, Any]) -> str:
    """Meta has no upload status field; an end_time means the upload finished."""
    return "finished" if upload.get("end_time") else "in_progress"


def _sort_upload_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order upload errors by rows affected, fatal before warning on ties."""
    def key(error: dict[str, Any]) -> tuple[bool, int, int]:
        count = to_int(error.get("total_count"))
        severity = str(error.get("severity") or "").lower()
        return (count is None, -(count or 0), 0 if severity == "fatal" else 1)

    return sorted(errors, key=key)


async def _upload_with_errors(client: Any, upload: dict[str, Any]) -> dict[str, Any]:
    """Attach the upload's errors (by rows affected) and a derived status."""
    upload_id = _object_id(upload.get("id"))
    item = {**upload, "status": _upload_status(upload)}
    error_count = to_int(upload.get("error_count"))
    warning_count = to_int(upload.get("warning_count"))
    if upload_id is None or (error_count == 0 and warning_count == 0):
        item.update({"errors": [], "errors_complete": True})
        return item
    payload = await client.list_objects(
        upload_id, "errors", fields=UPLOAD_ERROR_FIELDS, params={"limit": 100}
    )
    page = normalize_collection(payload)
    item.update({
        "errors": _sort_upload_errors(page["items"]),
        "errors_complete": not bool(page["paging"].get("next")),
    })
    return item


def _validate_rule_params(rule_type: str, params: Any) -> dict[str, str]:
    """Check rule inputs against the shapes Meta documents for each rule type."""
    if not isinstance(params, dict) or not params:
        raise ValidationError("params must be a non-empty object of string keys and string values.")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in params.items()):
        raise ValidationError("params keys and values must all be strings.")
    if rule_type == "mapping_rule":
        if not params.get("map_from", "").strip():
            raise ValidationError("mapping_rule params need map_from, the column name in your feed file.")
    elif rule_type == "letter_case_rule":
        if params.get("type") not in LETTER_CASE_TYPES:
            raise ValidationError(f"letter_case_rule params.type must be one of {', '.join(LETTER_CASE_TYPES)}.")
    elif rule_type == "regex_replace_rule":
        regex = params.get("regex")
        if not regex:
            raise ValidationError("regex_replace_rule params need regex.")
        try:
            re.compile(regex)
        except re.error as exc:
            raise ValidationError(f"regex_replace_rule params.regex is not a valid regular expression: {exc}.") from exc
    elif rule_type == "fallback_rule":
        if not params.get("user_default_value", "").strip():
            raise ValidationError("fallback_rule params need user_default_value.")
    elif rule_type == "value_mapping_rule":
        if len(params) > VALUE_MAPPING_MAX_PAIRS:
            raise ValidationError(f"value_mapping_rule allows at most {VALUE_MAPPING_MAX_PAIRS} mappings.")
        if any(len(text) > VALUE_MAPPING_MAX_LENGTH for pair in params.items() for text in pair):
            raise ValidationError(f"value_mapping_rule strings can be at most {VALUE_MAPPING_MAX_LENGTH} characters.")
    return dict(params)


async def _connected_sources(
    client: Any, catalog_id: str, stop_reasons: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    return await _collect_edge(
        client, catalog_id, "external_event_sources",
        fields=EVENT_SOURCE_FIELDS, page_size=100, max_pages=5, stop_reasons=stop_reasons,
    )


def _match_rate(matched: int | None, unmatched: int | None) -> float | None:
    """Matched share of content ids; None unless both counters are known."""
    if matched is None or unmatched is None:
        return None
    total = matched + unmatched
    return round(matched / total, 4) if total else None


def _add_counts(totals: list[int], matched: int | None, unmatched: int | None) -> None:
    """Add one row's counters; [matched, unmatched, rows missing matched, rows missing unmatched]."""
    if matched is None:
        totals[2] += 1
    else:
        totals[0] += matched
    if unmatched is None:
        totals[3] += 1
    else:
        totals[1] += unmatched


def _counts_view(totals: list[int]) -> dict[str, Any]:
    """A total is unknown (None) when any row behind it lacked that counter."""
    matched = None if totals[2] else totals[0]
    unmatched = None if totals[3] else totals[1]
    return {
        "matched_content_ids": matched,
        "unmatched_content_ids": unmatched,
        "match_rate": _match_rate(matched, unmatched),
    }


def _known_sum(values: Any) -> int | None:
    """Sum counters; None when any of them is unknown."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _summarize_event_stats(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], int]:
    """Group event_stats rows per source: daily match history and per-event totals.

    A counter Meta omitted is unknown, not zero: any total it feeds is None, and
    match rates are computed only when both counters are known.
    """
    per_source: dict[str, dict[str, Any]] = {}
    unattributed = 0
    for row in rows:
        source_id = _object_id(row.get("event_source"))
        if source_id is None:
            unattributed += 1
            continue
        matched = to_int(row.get("total_matched_content_ids"))
        unmatched = to_int(row.get("total_unmatched_content_ids"))
        bucket = per_source.setdefault(
            source_id, {"days": {}, "events": {}, "window": [0, 0, 0, 0], "rows_missing_counters": 0},
        )
        if matched is None or unmatched is None:
            bucket["rows_missing_counters"] += 1
        day = str(row.get("date_start") or row.get("date_stop") or "unknown")
        _add_counts(bucket["days"].setdefault(day, [0, 0, 0, 0]), matched, unmatched)
        event = str(row.get("event") or "unknown")
        _add_counts(bucket["events"].setdefault(event, [0, 0, 0, 0]), matched, unmatched)
        _add_counts(bucket["window"], matched, unmatched)
    summaries: dict[str, dict[str, Any]] = {}
    for source_id, bucket in per_source.items():
        window = _counts_view(bucket["window"])
        summaries[source_id] = {
            "matched_content_ids": window["matched_content_ids"],
            "unmatched_content_ids": window["unmatched_content_ids"],
            "window_match_rate": window["match_rate"],
            "rows_missing_counters": bucket["rows_missing_counters"],
            "match_rate_history": [
                {"date": day, **_counts_view(totals)}
                for day, totals in sorted(bucket["days"].items())
            ],
            "by_event": {
                event: _counts_view(totals)
                for event, totals in sorted(bucket["events"].items())
            },
        }
    return summaries, unattributed


def _validate_threshold(match_rate_threshold: float) -> None:
    if not 0 < match_rate_threshold <= 1:
        raise ValidationError("match_rate_threshold must be greater than 0 and at most 1.")


def _sorted_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda finding: _SEVERITY_ORDER.get(finding["severity"], 3))


async def _catalog_signal_inputs(client: Any, catalog_id: str) -> dict[str, Any]:
    """Read connected sources and 28-day event stats for one product catalog.

    Each incomplete read adds a plain-language entry to scan_gaps, for missing_signals.
    """
    source_reasons: list[str] = []
    sources, sources_complete = await _connected_sources(client, catalog_id, source_reasons)
    stat_reasons: list[str] = []
    stat_rows, stats_complete = await _collect_edge(
        client, catalog_id, "event_stats", fields=EVENT_STAT_FIELDS, page_size=500,
        stop_reasons=stat_reasons,
    )
    stats, unattributed = _summarize_event_stats(stat_rows)
    scan_gaps: list[str] = []
    if not sources_complete:
        scan_gaps.append(
            "The connected-source list is partial: "
            f"{' '.join(source_reasons) or 'external_event_sources had more pages than were read.'}"
        )
    if not stats_complete:
        scan_gaps.append(
            f"event_stats was only partly read ({len(stat_rows)} rows), so 28-day counts and match "
            f"rates are partial: {' '.join(stat_reasons) or 'more pages exist.'}"
        )
    return {
        "sources": sources,
        "sources_complete": sources_complete,
        "stat_rows": len(stat_rows),
        "stats_complete": stats_complete,
        "stats": stats,
        "unattributed_rows": unattributed,
        "scan_gaps": scan_gaps,
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@mcp_server.tool()
async def list_product_feeds(
    catalog_id: str | None = None,
    feed_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List data feeds on a Meta product catalog, or pass feed_id for one feed's settings, fetch schedules, and latest upload. Give exactly one of catalog_id or feed_id."""
    catalog_id = blank_to_none(catalog_id)
    feed_id = blank_to_none(feed_id)
    if (catalog_id is None) == (feed_id is None):
        raise ValidationError("Provide exactly one of catalog_id or feed_id.")
    _validate_limit(limit)
    client = get_graph_api_client()
    if feed_id is not None:
        feed = await client.get_object(feed_id, fields=FEED_FIELDS)
        return {"item": feed, "summary": {"count": 1}}
    payload = await client.list_objects(
        catalog_id, "product_feeds", fields=FEED_LIST_FIELDS, params=_page_params(limit, after),
    )
    return _paged_collection(payload)


@mcp_server.tool()
async def create_product_feed(
    catalog_id: str,
    name: str,
    schedule: dict[str, Any] | None = None,
    update_schedule: dict[str, Any] | None = None,
    file_name: str | None = None,
    country: str | None = None,
    default_currency: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a data feed on a product catalog. schedule {url, interval HOURLY|DAILY|WEEKLY|MONTHLY, hour, ...} fetches the file on a timer and replaces items; update_schedule only adds or updates items. Omit both to upload manually."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    feed_name = blank_to_none(name)
    if feed_name is None:
        raise ValidationError("name is required.")
    payload: dict[str, Any] = {
        "name": feed_name,
        "schedule": OMIT if schedule is None else _normalize_schedule(schedule, name="schedule"),
        "update_schedule": (
            OMIT if update_schedule is None
            else _normalize_schedule(update_schedule, name="update_schedule")
        ),
        "file_name": blank_to_none(file_name) or OMIT,
        "country": (blank_to_none(country) or "").upper() or OMIT,
        "default_currency": (blank_to_none(default_currency) or "").upper() or OMIT,
    }
    data = merge_graph_payload(payload, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(catalog_id, "product_feeds", data=data)
    return creation_response(
        action="create_product_feed",
        target={"catalog_id": catalog_id},
        result=created,
    )


@mcp_server.tool()
async def update_product_feed(
    feed_id: str,
    name: str | None = None,
    schedule: dict[str, Any] | None = None,
    update_schedule: dict[str, Any] | None = None,
    default_currency: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a product catalog data feed's name, default currency, parsing options (via params: delimiter, encoding, quoted_fields_mode), or fetch schedules, returning before and after."""
    feed_id = _require_id(feed_id, "feed_id")
    payload: dict[str, Any] = {
        "name": OMIT,
        "schedule": OMIT if schedule is None else _normalize_schedule(schedule, name="schedule"),
        "update_schedule": (
            OMIT if update_schedule is None
            else _normalize_schedule(update_schedule, name="update_schedule")
        ),
        "default_currency": (blank_to_none(default_currency) or "").upper() or OMIT,
    }
    if name is not None:
        if blank_to_none(name) is None:
            raise ValidationError("name cannot be blank.")
        payload["name"] = name.strip()
    data = merge_graph_payload(payload, params)
    if not data:
        raise ValidationError("At least one field must be provided for update_product_feed.")
    client = get_graph_api_client()
    previous = await client.get_object(feed_id, fields=FEED_UPDATE_READ_FIELDS)
    await client.update_object(feed_id, data=data)
    return mutation_response(
        action="update_product_feed",
        target={"feed_id": feed_id},
        previous=_redact_schedules({
            key: previous.get(key) for key in data if key in FEED_UPDATE_READ_FIELDS
        }),
        current=_redact_schedules(data),
    )


@mcp_server.tool()
async def delete_product_feed(feed_id: str) -> dict[str, Any]:
    """Use only on an explicit request to delete a product catalog data feed and its schedule. Products the feed supplied are removed from the catalog afterwards unless another source also supplies them, and ads using them stop. Returns the feed's prior settings."""
    feed_id = _require_id(feed_id, "feed_id")
    client = get_graph_api_client()
    previous = await client.get_object(feed_id, fields=FEED_FIELDS)
    result = await client.delete_object(feed_id)
    return mutation_response(
        action="delete_product_feed",
        target={"feed_id": feed_id},
        previous=_redact_schedules(previous),
        current={"deleted": True, "result": result},
    )


@mcp_server.tool()
async def create_product_feed_upload(
    feed_id: str,
    url: str,
    update_only: bool | None = None,
) -> dict[str, Any]:
    """Start a one-off upload for a product catalog data feed from an http(s) URL. update_only=true only adds or updates items; otherwise items missing from the file may be deleted. Check progress with list_product_feed_uploads."""
    feed_id = _require_id(feed_id, "feed_id")
    data: dict[str, Any] = {"url": _validate_http_url(url, name="url", schemes=("http", "https"))}
    if update_only is not None:
        data["update_only"] = update_only
    client = get_graph_api_client()
    created = await client.create_edge_object(feed_id, "uploads", data=data)
    return creation_response(
        action="create_product_feed_upload",
        target={"feed_id": feed_id},
        result=created,
    )


@mcp_server.tool()
async def list_product_feed_uploads(
    feed_id: str,
    upload_id: str | None = None,
    limit: int = 10,
    after: str | None = None,
) -> dict[str, Any]:
    """List uploads for a product catalog data feed, newest first, with item counts and errors ordered by rows affected; pass upload_id for one upload. status is finished when Meta reports an end_time."""
    feed_id = _require_id(feed_id, "feed_id")
    upload_id = blank_to_none(upload_id)
    _validate_limit(limit)
    client = get_graph_api_client()
    if upload_id is not None:
        upload = await client.get_object(upload_id, fields=UPLOAD_FIELDS)
        return {
            "item": await _upload_with_errors(client, upload),
            "feed_id": feed_id,
            "summary": {"count": 1},
        }
    payload = await client.list_objects(
        feed_id, "uploads", fields=UPLOAD_FIELDS, params=_page_params(limit, after),
    )
    collection = _paged_collection(payload)
    collection["items"] = [
        await _upload_with_errors(client, upload) for upload in collection["items"]
    ]
    return collection


@mcp_server.tool()
async def list_feed_rules(
    feed_id: str,
    limit: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """List the attribute mapping and transformation rules applied to a product catalog data feed's uploads."""
    feed_id = _require_id(feed_id, "feed_id")
    _validate_limit(limit)
    client = get_graph_api_client()
    payload = await client.list_objects(
        feed_id, "rules", fields=RULE_FIELDS, params=_page_params(limit, after),
    )
    return _paged_collection(payload)


@mcp_server.tool()
async def create_feed_rule(
    feed_id: str,
    attribute: str,
    rule_type: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Add a rule to a product catalog data feed. rule_type and params: mapping_rule {map_from}, value_mapping_rule {old: new} (max 10), letter_case_rule {type}, regex_replace_rule {regex}, fallback_rule {user_default_value}. One rule per attribute and rule_type."""
    feed_id = _require_id(feed_id, "feed_id")
    attribute_name = _require_id(attribute, "attribute")
    normalized_type = rule_type.strip().lower() if isinstance(rule_type, str) else ""
    if normalized_type not in FEED_RULE_TYPES:
        raise ValidationError(f"rule_type must be one of {', '.join(FEED_RULE_TYPES)}.")
    rule_params = _validate_rule_params(normalized_type, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(
        feed_id,
        "rules",
        data={"attribute": attribute_name, "rule_type": normalized_type, "params": rule_params},
    )
    return creation_response(
        action="create_feed_rule",
        target={"feed_id": feed_id},
        result=created,
    )


@mcp_server.tool()
async def delete_feed_rule(rule_id: str) -> dict[str, Any]:
    """Use only on an explicit request to delete one product catalog feed rule. The feed and its products are unaffected; later uploads are no longer transformed by the rule. Returns the rule as it was."""
    rule_id = _require_id(rule_id, "rule_id")
    client = get_graph_api_client()
    previous = await client.get_object(rule_id, fields=RULE_FIELDS)
    result = await client.delete_object(rule_id)
    return mutation_response(
        action="delete_feed_rule",
        target={"rule_id": rule_id},
        previous=previous,
        current={"deleted": True, "result": result},
    )


@mcp_server.tool()
async def list_catalog_event_sources(
    catalog_id: str,
    limit: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """List the pixels, apps, and other event sources connected to a Meta product catalog."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    _validate_limit(limit)
    client = get_graph_api_client()
    payload = await client.list_objects(
        catalog_id, "external_event_sources",
        fields=EVENT_SOURCE_FIELDS, params=_page_params(limit, after),
    )
    return _paged_collection(payload)


@mcp_server.tool()
async def list_event_source_catalogs(
    event_source_id: str,
    business_id: str | None = None,
) -> dict[str, Any]:
    """Find the product catalogs one pixel or app is connected to by checking a business's owned catalogs. business_id defaults to the pixel's owner business; the summary says how many catalogs were checked and whether the scan was complete."""
    event_source_id = _require_id(event_source_id, "event_source_id")
    business_id = blank_to_none(business_id)
    client = get_graph_api_client()
    if business_id is None:
        source = await client.get_object(event_source_id, fields=["id", "owner_business"])
        business_id = _object_id(source.get("owner_business"))
        if business_id is None:
            raise ValidationError(
                "The event source has no readable owner_business; pass business_id to choose "
                "which business's catalogs to check."
            )
    stop_reasons: list[str] = []
    catalogs, catalogs_complete = await _collect_edge(
        client, business_id, "owned_product_catalogs",
        fields=CATALOG_SCAN_FIELDS, page_size=CATALOG_SCAN_PAGE_SIZE,
        max_items=MAX_CATALOGS_SCANNED, stop_reasons=stop_reasons,
    )
    matches: list[dict[str, Any]] = []
    sources_complete = True
    for catalog in catalogs:
        catalog_id = _object_id(catalog.get("id"))
        if catalog_id is None:
            continue
        sources, complete = await _connected_sources(client, catalog_id, stop_reasons)
        sources_complete = sources_complete and complete
        if any(_object_id(source.get("id")) == event_source_id for source in sources):
            matches.append(catalog)
    complete = catalogs_complete and sources_complete
    note = (
        "Every owned catalog of the business was checked."
        if complete
        else f"The scan is incomplete after checking {len(catalogs)} catalogs: "
        f"{' '.join(stop_reasons)} Catalogs or sources not read may also be connected."
    )
    return {
        "items": matches,
        "summary": {
            "count": len(matches),
            "event_source_id": event_source_id,
            "business_id": business_id,
            "catalogs_scanned": len(catalogs),
            "complete": complete,
            "stop_reasons": stop_reasons,
            "method": "derived: each catalog owned by the business is checked for this source "
            "on its external_event_sources edge; catalogs shared with the business by others "
            "are not checked.",
            "note": note,
        },
    }


@mcp_server.tool()
async def get_catalog_event_source_health(
    catalog_id: str,
    match_rate_threshold: float = DEFAULT_MATCH_RATE_THRESHOLD,
) -> dict[str, Any]:
    """Health of each pixel or app connected to a product catalog: daily match-rate history over Meta's 28-day event_stats window, per-event match rates, and failed Dynamic Ads checks, ranked as findings."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    _validate_threshold(match_rate_threshold)
    client = get_graph_api_client()
    inputs = await _catalog_signal_inputs(client, catalog_id)
    findings: list[dict[str, Any]] = []
    missing: list[str] = []
    reports: list[dict[str, Any]] = []
    for source in inputs["sources"]:
        source_id = _object_id(source.get("id"))
        if source_id is None:
            continue
        source_type = str(source.get("source_type") or "").upper()
        stats = inputs["stats"].get(source_id)
        report: dict[str, Any] = {
            "id": source_id,
            "name": source.get("name"),
            "source_type": source.get("source_type"),
            "history_available": stats is not None,
            "match_rate_history": stats["match_rate_history"] if stats else [],
            "window_match_rate": stats["window_match_rate"] if stats else None,
            "matched_content_ids": stats["matched_content_ids"] if stats else None,
            "unmatched_content_ids": stats["unmatched_content_ids"] if stats else None,
            "by_event": stats["by_event"] if stats else {},
            "checks": [],
            "failed_checks": [],
            "unevaluated_checks": [],
        }
        if stats is None and not inputs["stats_complete"]:
            missing.append(
                f"No event_stats rows for source {source_id} were among the pages read; event_stats "
                "has more pages, so whether it sent catalog events in the 28-day window is unknown."
            )
        elif stats is None:
            missing.append(
                f"No event_stats rows for source {source_id} in Meta's 28-day window, so no "
                "match-rate history is available for it."
            )
            findings.append(build_finding(
                "no_event_stats",
                f"Source {source_id} is connected but sent no catalog events Meta counted in the last 28 days.",
                severity="medium",
                confidence=0.7,
                affected_entities=[{"event_source_id": source_id}],
                next_actions=["Confirm the pixel or app fires ViewContent, AddToCart and Purchase with content_ids."],
            ))
        elif (
            inputs["stats_complete"]
            and stats["window_match_rate"] is not None
            and stats["window_match_rate"] < match_rate_threshold
        ):
            findings.append(build_finding(
                "low_match_rate",
                f"Only {stats['window_match_rate']:.0%} of content ids from source {source_id} matched "
                f"catalog items (threshold {match_rate_threshold:.0%}).",
                severity="medium",
                confidence=0.6,
                evidence=[{
                    "event_source_id": source_id,
                    "matched_content_ids": stats["matched_content_ids"],
                    "unmatched_content_ids": stats["unmatched_content_ids"],
                    "threshold": match_rate_threshold,
                }],
                affected_entities=[{"event_source_id": source_id}],
                next_actions=["Make the content_ids the source sends equal the catalog's retailer ids."],
            ))
        if stats is not None and stats["rows_missing_counters"]:
            missing.append(
                f"{stats['rows_missing_counters']} event_stats rows for source {source_id} lacked a "
                "matched or unmatched counter; totals and match rates those rows feed are unknown (null)."
            )
        if source_type in DA_CHECK_SOURCE_TYPES:
            checks_payload = await client.list_objects(source_id, "da_checks", fields=DA_CHECK_FIELDS)
            checks_page = normalize_collection(checks_payload)
            checks = checks_page["items"]
            failed = [check for check in checks if str(check.get("result") or "").lower() == "failed"]
            unevaluated = [
                check for check in checks
                if str(check.get("result") or "").lower() not in DA_CHECK_EVALUATED_RESULTS
            ]
            report["checks"] = checks
            report["failed_checks"] = failed
            report["unevaluated_checks"] = unevaluated
            for check in unevaluated:
                missing.append(
                    f"Meta's check {check.get('key')} for source {source_id} returned result "
                    f"{check.get('result')!r}, so it was neither passed nor failed and could not be evaluated."
                )
            if not checks:
                missing.append(f"Meta returned no Dynamic Ads checks for source {source_id}.")
            if checks_page["paging"].get("next"):
                missing.append(f"da_checks for source {source_id} had more pages than were read.")
            for check in failed:
                findings.append(build_finding(
                    "failed_dynamic_ads_check",
                    f"Source {source_id} failed Meta's check {check.get('key')}: {check.get('title') or ''}".strip(),
                    severity="high",
                    confidence=0.9,
                    evidence=[check],
                    affected_entities=[{"event_source_id": source_id}],
                    next_actions=[check["action_uri"]] if check.get("action_uri") else [],
                ))
        else:
            missing.append(
                f"Meta da_checks were not run for source {source_id}: source_type "
                f"{source.get('source_type')!r} is not one documented for da_checks (pixel or app)."
            )
        reports.append(report)
    if not inputs["sources"] and inputs["sources_complete"]:
        findings.append(build_finding(
            "no_connected_event_sources",
            "No pixel or app is connected to this catalog, so catalog ads get no event signal from it.",
            severity="high",
            confidence=0.9,
            next_actions=["Use get_catalog_event_source_recommendations, then connect_catalog_event_source."],
        ))
    elif not inputs["sources"]:
        missing.append(
            "No connected sources were among the pages read, but the list is partial; whether any "
            "source is connected could not be determined."
        )
    missing.extend(inputs["scan_gaps"])
    if not inputs["stats_complete"]:
        missing.append("Low-match-rate findings were not made because event_stats was only partly read.")
    if inputs["unattributed_rows"]:
        missing.append(f"{inputs['unattributed_rows']} event_stats rows had no event_source and were not attributed.")
    return analysis_response(
        scope={"catalog_id": catalog_id},
        metrics={
            "connected_sources": len(reports),
            "sources_complete": inputs["sources_complete"],
            "event_stat_rows": inputs["stat_rows"],
            "stats_complete": inputs["stats_complete"],
            "match_rate_threshold": match_rate_threshold,
        },
        findings=_sorted_findings(findings),
        missing_signals=missing,
        extra={
            "sources": reports,
            "method": _DERIVED_METHOD,
            "match_rate_definition": _MATCH_RATE_NOTE,
            "signals_used": [
                "GET {catalog_id}/external_event_sources (connected sources)",
                "GET {catalog_id}/event_stats (daily matched and unmatched content ids per event and source, last 28 days)",
                "GET {event_source_id}/da_checks for pixel and app sources (Meta's Dynamic Ads checks: passed, failed, unavailable)",
            ],
        },
    )


@mcp_server.tool()
async def get_catalog_event_source_recommendations(
    catalog_id: str,
    business_id: str | None = None,
    match_rate_threshold: float = DEFAULT_MATCH_RATE_THRESHOLD,
) -> dict[str, Any]:
    """Recommend pixels worth connecting to a product catalog when its event signal is weak or missing, with evidence. Candidates are the business's pixels not yet connected; Meta's Dynamic Ads check shows whether each already sends catalog events."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    business_id = blank_to_none(business_id)
    _validate_threshold(match_rate_threshold)
    client = get_graph_api_client()
    if business_id is None:
        catalog = await client.get_object(catalog_id, fields=["id", "business"])
        business_id = _object_id(catalog.get("business"))
        if business_id is None:
            raise ValidationError(
                "The catalog has no readable business; pass business_id to choose whose pixels to consider."
            )
    inputs = await _catalog_signal_inputs(client, catalog_id)
    connected_ids = {_object_id(source.get("id")) for source in inputs["sources"]}
    findings: list[dict[str, Any]] = []
    missing: list[str] = []
    matched = _known_sum(stat["matched_content_ids"] for stat in inputs["stats"].values())
    unmatched = _known_sum(stat["unmatched_content_ids"] for stat in inputs["stats"].values())
    overall_rate = _match_rate(matched, unmatched)
    rows_missing_counters = sum(stat["rows_missing_counters"] for stat in inputs["stats"].values())
    if rows_missing_counters:
        missing.append(
            f"{rows_missing_counters} event_stats rows lacked a matched or unmatched counter; "
            "the 28-day totals and match rate they feed are unknown (null)."
        )
    missing.extend(inputs["scan_gaps"])
    sources_complete = inputs["sources_complete"]
    stats_complete = inputs["stats_complete"]
    coverage_evidence: dict[str, Any] = {
        "connected_sources": len(inputs["sources"]),
        "sources_complete": sources_complete,
        "stats_complete": stats_complete,
        "matched_content_ids_28d": matched if stats_complete else None,
        "unmatched_content_ids_28d": unmatched if stats_complete else None,
        "match_rate_28d": overall_rate if stats_complete else None,
        "threshold": match_rate_threshold,
    }
    if not stats_complete:
        coverage_evidence["partial_counts_from_pages_read"] = {
            "event_stat_rows": inputs["stat_rows"],
            "matched_content_ids": matched,
            "unmatched_content_ids": unmatched,
        }
    if not inputs["sources"] and not sources_complete:
        missing.append(
            "No connected sources were among the pages read, but the list is partial; whether any "
            "source is connected could not be determined."
        )
    elif not inputs["sources"]:
        findings.append(build_finding(
            "no_connected_event_sources",
            "No pixel or app is connected to this catalog.",
            severity="high",
            confidence=0.9,
            evidence=[coverage_evidence],
        ))
    elif not stats_complete:
        missing.append(
            "Whether the connected sources' catalog signal coverage is weak could not be determined "
            "because event_stats was only partly read."
        )
    elif matched != 0 and overall_rate is None:
        missing.append(
            "Whether the connected sources' catalog signal coverage is weak could not be determined "
            "because Meta omitted matched or unmatched counters."
        )
    elif matched == 0 or (overall_rate is not None and overall_rate < match_rate_threshold):
        findings.append(build_finding(
            "weak_catalog_signal_coverage",
            "Connected sources produced few matched catalog events in Meta's 28-day window.",
            severity="medium",
            confidence=0.6,
            evidence=[coverage_evidence],
            next_actions=[
                "Connect a pixel that already sends catalog events, or fix content_ids on the connected sources.",
            ],
        ))

    pixels, pixels_complete = await _collect_edge(
        client, business_id, "adspixels", fields=PIXEL_CANDIDATE_FIELDS, page_size=100, max_pages=5,
    )
    if not pixels_complete:
        missing.append("The business has more pixels than were read; some candidates were not considered.")
    candidates = [
        pixel for pixel in pixels
        if _object_id(pixel.get("id")) not in connected_ids and not pixel.get("is_unavailable")
    ]
    excluded = [
        {"event_source_id": _object_id(pixel.get("id")), "reason": "Meta marks this pixel unavailable."}
        for pixel in pixels
        if _object_id(pixel.get("id")) not in connected_ids and pixel.get("is_unavailable")
    ]
    if not pixels:
        missing.append(f"No pixels were returned for business {business_id}, so there are no candidates to recommend.")
    if candidates and not sources_complete:
        missing.append(
            "Candidate pixels were not among the connected sources read, but that list is partial, "
            "so some candidates may already be connected (connection_status unknown)."
        )
    connection_status = "not_connected" if sources_complete else "unknown"
    recommendations: list[dict[str, Any]] = []
    for index, pixel in enumerate(candidates):
        pixel_id = _object_id(pixel.get("id"))
        evidence: list[dict[str, Any]] = [{"last_fired_time": pixel.get("last_fired_time")}]
        strength = "unchecked"
        reason = (
            "Pixel belongs to the business and is not connected to this catalog."
            if sources_complete
            else "Pixel belongs to the business and was not among the connected sources read."
        )
        if index < MAX_PIXEL_CANDIDATES_CHECKED:
            checks_payload = await client.list_objects(pixel_id, "da_checks", fields=DA_CHECK_FIELDS)
            checks = normalize_collection(checks_payload)["items"]
            dpa_check = next((c for c in checks if c.get("key") == "pixel_missing_dpa_event"), None)
            result = str((dpa_check or {}).get("result") or "").lower()
            if dpa_check is not None:
                evidence.append(dpa_check)
            if result == "passed":
                strength = "strong"
                reason = "Meta's check pixel_missing_dpa_event passed: the pixel already sends catalog events."
            elif result == "failed":
                strength = "weak"
                reason = "Meta's check pixel_missing_dpa_event failed: the pixel does not send all catalog events yet."
            else:
                strength = "unknown"
                reason = "Meta returned no usable pixel_missing_dpa_event result for this pixel."
        recommendations.append({
            "event_source_id": pixel_id,
            "name": pixel.get("name"),
            "source_type": "PIXEL",
            "connection_status": connection_status,
            "strength": strength,
            "reason": reason,
            "evidence": evidence,
            "next_step": {
                "tool": "connect_catalog_event_source",
                "arguments": {"catalog_id": catalog_id, "event_source_id": pixel_id},
            },
        })
    if len(candidates) > MAX_PIXEL_CANDIDATES_CHECKED:
        missing.append(
            f"Only the first {MAX_PIXEL_CANDIDATES_CHECKED} unconnected pixels were checked with da_checks; "
            "the rest are listed as unchecked."
        )
    missing.append("Apps are not considered as candidates: Meta documents no business edge listing apps for this purpose.")
    strength_order = {"strong": 0, "unknown": 1, "unchecked": 2, "weak": 3}

    def recommendation_key(item: dict[str, Any]) -> tuple[int, float]:
        fired = _parse_time(item["evidence"][0].get("last_fired_time"))
        return (strength_order[item["strength"]], -(fired.timestamp() if fired else 0.0))

    recommendations.sort(key=recommendation_key)
    return analysis_response(
        scope={"catalog_id": catalog_id, "business_id": business_id},
        metrics=coverage_evidence,
        findings=_sorted_findings(findings),
        missing_signals=missing,
        extra={
            "recommendations": recommendations,
            "excluded": excluded,
            "method": _DERIVED_METHOD,
            "match_rate_definition": _MATCH_RATE_NOTE,
            "signals_used": [
                "GET {catalog_id}/external_event_sources (already connected sources)",
                "GET {catalog_id}/event_stats (matched and unmatched content ids, last 28 days)",
                "GET {business_id}/adspixels (candidate pixels with last_fired_time, is_unavailable)",
                "GET {pixel_id}/da_checks check pixel_missing_dpa_event (whether the pixel sends catalog events)",
            ],
        },
    )


@mcp_server.tool()
async def connect_catalog_event_source(
    catalog_id: str,
    event_source_id: str,
) -> dict[str, Any]:
    """Connect a pixel or app to a Meta product catalog so its catalog events can match items. Returns the connected sources before and after; an already connected source is left as is."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    event_source_id = _require_id(event_source_id, "event_source_id")
    client = get_graph_api_client()
    sources, _ = await _connected_sources(client, catalog_id)
    before = [source_id for source in sources if (source_id := _object_id(source.get("id")))]
    target = {"catalog_id": catalog_id, "event_source_id": event_source_id}
    if event_source_id in before:
        return mutation_response(
            action="connect_catalog_event_source",
            target=target,
            previous={"connected_event_source_ids": before},
            current={"connected_event_source_ids": before, "already_connected": True},
        )
    await client.create_edge_object(
        catalog_id, "external_event_sources", data={"external_event_sources": [event_source_id]},
    )
    return mutation_response(
        action="connect_catalog_event_source",
        target=target,
        previous={"connected_event_source_ids": before},
        current={"connected_event_source_ids": [*before, event_source_id]},
    )


@mcp_server.tool()
async def disconnect_catalog_event_source(
    catalog_id: str,
    event_source_id: str,
) -> dict[str, Any]:
    """Use only on an explicit request to disconnect a pixel or app from a Meta product catalog; catalog ads stop using its events. Returns the source and the connected list before and after."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    event_source_id = _require_id(event_source_id, "event_source_id")
    client = get_graph_api_client()
    sources, _ = await _connected_sources(client, catalog_id)
    before = [source_id for source in sources if (source_id := _object_id(source.get("id")))]
    source = next((s for s in sources if _object_id(s.get("id")) == event_source_id), None)
    if source is None:
        raise ValidationError(
            f"Event source {event_source_id} is not connected to catalog {catalog_id}; nothing was changed."
        )
    await client.delete_edge_object(
        catalog_id, "external_event_sources", data={"external_event_sources": [event_source_id]},
    )
    return mutation_response(
        action="disconnect_catalog_event_source",
        target={"catalog_id": catalog_id, "event_source_id": event_source_id},
        previous={"connected_event_source_ids": before, "event_source": source},
        current={"connected_event_source_ids": [s for s in before if s != event_source_id]},
    )
