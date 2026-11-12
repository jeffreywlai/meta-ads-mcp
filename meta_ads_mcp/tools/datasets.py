"""Dataset (pixel), event-quality, custom conversion, and pixel setup read tools.

Endpoints (Meta Marketing API reference):
- GET act_{id}/adspixels and {business_id}/adspixels: ad-account/adspixels, business/adspixels
- GET {pixel_id}: ads-pixel
- GET {pixel_id}/stats: ads-pixel/stats
- GET dataset_quality?dataset_id=: conversions-api/dataset-quality-api
- GET act_{id}/customconversions: ad-account/customconversions
- GET {pixel_id}/event_rules: ads-pixel/event_rules (read only; no create/update/delete)
- GET {pixel_id}/extractors: ads-pixel/extractors (read only; no create/update/delete)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none, normalize_collection, to_float, to_int
from meta_ads_mcp.schemas import analysis_response
from meta_ads_mcp.tool_types import FieldList, normalize_field_list

DATASET_SUMMARY_FIELDS = [
    "id",
    "name",
    "creation_time",
    "last_fired_time",
    "is_unavailable",
    "data_use_setting",
]

DATASET_FIELDS = [
    "id",
    "name",
    "description",
    "creation_time",
    "last_fired_time",
    "is_unavailable",
    "is_created_by_business",
    "owner_business",
    "data_use_setting",
    "enable_automatic_matching",
    "automatic_matching_fields",
    "first_party_cookie_status",
    "last_upload_app",
    "last_upload_app_changed_time",
    "config",
]

CUSTOM_CONVERSION_FIELDS = [
    "id",
    "name",
    "custom_event_type",
    "rule",
    "pixel",
    "event_source_type",
    "is_archived",
    "is_unavailable",
    "creation_time",
    "first_fired_time",
    "last_fired_time",
]

# Field names from facebook_business 26.0.2 CalibratorExistingRule / SignalsIWLExtractor.
EVENT_RULE_FIELDS = [
    "id",
    "event_type",
    "rule_type",
    "rule",
    "status",
    "creation_source",
    "creation_time",
    "transforms",
]

PIXEL_PARAMETER_FIELDS = [
    "id",
    "event_type",
    "extractor_type",
    "extractor_config",
    "domain_uri",
]

# Aggregations accepted by GET {pixel_id}/stats (AdsPixelStatsResult.Aggregation).
STATS_AGGREGATIONS = (
    "browser_type",
    "custom_data_field",
    "device_os",
    "device_type",
    "event",
    "event_detection_method",
    "event_processing_results",
    "event_source",
    "event_total_counts",
    "event_value_count",
    "had_pii",
    "host",
    "match_keys",
    "pixel_fire",
    "url",
    "url_by_rule",
)
STATS_EVENT_SOURCES = ("WEB_ONLY", "SERVER_ONLY")
MAX_STATS_WINDOW_DAYS = 28
DEFAULT_STATS_WINDOW_DAYS = 7
MAX_STATS_PAGES = 50
MAX_ID_SCAN_PAGES = 10
ID_SCAN_PAGE_SIZE = 100

DATASET_QUALITY_FIELDS = (
    "web{event_name,"
    "event_match_quality{composite_score,"
    "match_key_feedback{identifier,coverage{percentage}},"
    "diagnostics{name,description,solution,percentage,affected_event_count,total_event_count}},"
    "event_coverage{percentage,goal_percentage,description},"
    "dedupe_key_feedback{dedupe_key,browser_events_with_dedupe_key{percentage},"
    "server_events_with_dedupe_key{percentage}},"
    "data_freshness{upload_frequency,description}}"
)
WEAK_EMQ_SCORE_BELOW = 6.0
DEFAULT_COVERAGE_GOAL_PERCENTAGE = 75
QUALITY_THRESHOLD_SOURCE = (
    "Event match quality is a score out of 10 (Meta Dataset Quality API). Meta's developer "
    "documentation publishes no numeric cutoff; Events Manager labels scores Poor, OK, Good "
    "and Great, and Meta recommends Good or Great. Scores below 6.0, the commonly reported "
    "lower bound of Good, are flagged here as weak. Event coverage is flagged when below "
    "Meta's returned goal_percentage, or 75% when Meta omits it, as the Dataset Quality API "
    "documentation describes."
)


def _utc_now() -> datetime:
    """Return the current UTC time; patched in tests."""
    return datetime.now(timezone.utc)


def _required_id(value: str | None, name: str) -> str:
    """Normalize a required object id or raise before any network call."""
    normalized = blank_to_none(value)
    if normalized is None:
        raise ValidationError(f"{name} is required.")
    return normalized


def _check_limit(limit: int) -> None:
    if limit < 1:
        raise ValidationError("limit must be positive.")


def _page_params(limit: int, after: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if cursor := blank_to_none(after):
        params["after"] = cursor
    return params


def _page_summary(normalized: dict[str, Any]) -> dict[str, Any]:
    """Add the list_audiences-style completeness keys to a collection summary."""
    has_more = bool(normalized["paging"].get("next"))
    normalized["summary"].update({
        "complete": not has_more,
        "truncated": has_more,
        "next_after": normalized["paging"].get("after") if has_more else None,
    })
    return normalized


def _next_cursor(payload: dict[str, Any]) -> str | None:
    paging = payload.get("paging") or {}
    if not paging.get("next"):
        return None
    return (paging.get("cursors") or {}).get("after")


async def _find_by_id(
    client: Any,
    parent_id: str,
    edge: str,
    *,
    target_id: str,
    fields: FieldList,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Scan a documented list edge for one id; no by-id node read is documented."""
    examined = 0
    pages = 0
    cursor: str | None = None
    found: dict[str, Any] | None = None
    complete = False
    while pages < MAX_ID_SCAN_PAGES:
        page_params = {"limit": ID_SCAN_PAGE_SIZE, **params}
        if cursor:
            page_params["after"] = cursor
        payload = await client.list_objects(parent_id, edge, fields=fields, params=page_params)
        pages += 1
        for item in payload.get("data") or []:
            examined += 1
            if str(item.get("id")) == target_id:
                found = item
                break
        if found is not None:
            complete = True
            break
        cursor = _next_cursor(payload)
        if cursor is None:
            complete = True
            break
    summary: dict[str, Any] = {
        "found": found is not None,
        "count": 1 if found is not None else 0,
        "examined": examined,
        "pages_fetched": pages,
        "complete": complete,
    }
    if not complete:
        summary["note"] = (
            f"Scan incomplete: stopped after {pages} pages and {examined} items without "
            f"finding {target_id}; it may exist on a later page."
        )
    elif found is None:
        summary["note"] = f"{target_id} was not among the {examined} items on this pixel."
    return {"item": found, "summary": summary}


def _parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a date in YYYY-MM-DD format.") from exc


def _resolve_stats_window(since: str | None, until: str | None, now: datetime) -> tuple[date, date]:
    today = now.date()
    since_value = blank_to_none(since)
    until_value = blank_to_none(until)
    until_day = _parse_day(until_value, "until") if until_value else None
    since_day = _parse_day(since_value, "since") if since_value else None
    if until_day is None:
        until_day = today if since_day is None else min(
            today, since_day + timedelta(days=DEFAULT_STATS_WINDOW_DAYS - 1)
        )
    if since_day is None:
        since_day = until_day - timedelta(days=DEFAULT_STATS_WINDOW_DAYS - 1)
    if until_day < since_day:
        raise ValidationError("until must not be before since.")
    if since_day > today:
        raise ValidationError("since is in the future; dataset stats cover past events only.")
    if (until_day - since_day).days + 1 > MAX_STATS_WINDOW_DAYS:
        raise ValidationError(
            f"The stats window may cover at most {MAX_STATS_WINDOW_DAYS} days; "
            "split longer periods into several calls."
        )
    return since_day, until_day


@mcp_server.tool()
async def list_datasets(
    account_id: str | None = None,
    business_id: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List the pixels (Meta datasets) on one ad account or one business. Pass exactly one of account_id or business_id. Follow summary.next_after with after until summary.complete is true. A permission error is raised, never shown as an empty list."""
    account_id = blank_to_none(account_id)
    business_id = blank_to_none(business_id)
    if (account_id is None) == (business_id is None):
        raise ValidationError("Provide exactly one of account_id or business_id.")
    _check_limit(limit)
    if account_id is not None:
        parent_id = normalize_account_id(account_id)
        scope = {"account_id": parent_id}
    else:
        parent_id = business_id
        scope = {"business_id": business_id}
    payload = await get_graph_api_client().list_objects(
        parent_id,
        "adspixels",
        fields=normalize_field_list(fields) or DATASET_SUMMARY_FIELDS,
        params=_page_params(limit, after),
    )
    normalized = _page_summary(normalize_collection(payload))
    normalized["summary"]["scope"] = scope
    return normalized


@mcp_server.tool()
async def get_dataset(
    dataset_id: str,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Get one pixel (dataset) by id: settings, advanced matching, data use, last fire time, and the app that last uploaded server (Conversions API) events. The pixel install code is omitted unless requested in fields."""
    dataset_id = _required_id(dataset_id, "dataset_id")
    dataset = await get_graph_api_client().get_object(dataset_id, fields=normalize_field_list(fields) or DATASET_FIELDS)
    return {"item": dataset, "summary": {"count": 1}}


@mcp_server.tool()
async def get_dataset_stats(
    dataset_id: str,
    since: str | None = None,
    until: str | None = None,
    aggregation: str = "event",
    event: str | None = None,
    event_source: str | None = None,
    include_hourly: bool = False,
) -> dict[str, Any]:
    """Event volume a pixel received between since and until (YYYY-MM-DD, UTC, inclusive; default the last 7 days; at most 28 days), totalled per value of aggregation: event (default), device_type, device_os, browser_type, event_source, match_keys, host, url, custom_data_field (needs event), and others. event_source filters to WEB_ONLY or SERVER_ONLY."""
    dataset_id = _required_id(dataset_id, "dataset_id")
    aggregation = (blank_to_none(aggregation) or "event").lower()
    if aggregation not in STATS_AGGREGATIONS:
        raise ValidationError(f"aggregation must be one of: {', '.join(STATS_AGGREGATIONS)}.")
    event = blank_to_none(event)
    if aggregation == "custom_data_field" and event is None:
        raise ValidationError("event is required when aggregation is custom_data_field.")
    source = blank_to_none(event_source)
    if source is not None:
        source = source.upper()
        if source not in STATS_EVENT_SOURCES:
            raise ValidationError(f"event_source must be one of: {', '.join(STATS_EVENT_SOURCES)}.")
    now = _utc_now()
    since_day, until_day = _resolve_stats_window(since, until, now)
    start_time = int(datetime.combine(since_day, time.min, tzinfo=timezone.utc).timestamp())
    end_time = min(
        int(datetime.combine(until_day + timedelta(days=1), time.min, tzinfo=timezone.utc).timestamp()),
        int(now.timestamp()),
    )
    base_params: dict[str, Any] = {
        "aggregation": aggregation,
        "start_time": start_time,
        "end_time": end_time,
    }
    if event is not None:
        base_params["event"] = event
    if source is not None:
        base_params["event_source"] = source

    client = get_graph_api_client()
    totals: dict[str, int] = {}
    hourly: list[dict[str, Any]] = []
    hours: list[str] = []
    pages = 0
    cursor: str | None = None
    complete = False
    while pages < MAX_STATS_PAGES:
        params = dict(base_params)
        if cursor:
            params["after"] = cursor
        payload = await client.list_objects(dataset_id, "stats", params=params)
        pages += 1
        for bucket in payload.get("data") or []:
            rows = []
            for row in bucket.get("data") or []:
                value = str(row.get("value"))
                count = to_int(row.get("count")) or 0
                totals[value] = totals.get(value, 0) + count
                rows.append({"value": value, "count": count})
            if bucket.get("start_time") is not None:
                hours.append(str(bucket["start_time"]))
            if include_hourly:
                hourly.append({"start_time": bucket.get("start_time"), "data": rows})
        cursor = _next_cursor(payload)
        if cursor is None:
            complete = True
            break

    items = [
        {"value": value, "count": count}
        for value, count in sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    summary: dict[str, Any] = {
        "count": len(items),
        "total_count": sum(totals.values()),
        "hours_with_data": len(set(hours)),
        "first_hour": min(hours) if hours else None,
        "last_hour": max(hours) if hours else None,
        "pages_fetched": pages,
        "complete": complete,
    }
    if not complete:
        summary["note"] = (
            f"Incomplete: stopped after {pages} pages of hourly stats; totals cover only the "
            "hours read. Narrow the window to read the rest."
        )
    response: dict[str, Any] = {
        "scope": {
            "dataset_id": dataset_id,
            "since": since_day.isoformat(),
            "until": until_day.isoformat(),
            "aggregation": aggregation,
            "event": event,
            "event_source": source,
            "start_time": start_time,
            "end_time": end_time,
        },
        "items": items,
        "summary": summary,
    }
    if include_hourly:
        response["hourly"] = hourly
    return response


def _percentage(node: Any) -> float | None:
    return to_float(node.get("percentage")) if isinstance(node, dict) else None


def _quality_event(row: dict[str, Any]) -> dict[str, Any]:
    emq = row.get("event_match_quality") or {}
    score = to_float(emq.get("composite_score"))
    coverage_node = row.get("event_coverage") or {}
    coverage = _percentage(coverage_node)
    goal = to_float(coverage_node.get("goal_percentage"))
    if goal is None:
        goal = DEFAULT_COVERAGE_GOAL_PERCENTAGE
    freshness = row.get("data_freshness")
    dedupe = [
        {
            "dedupe_key": item.get("dedupe_key"),
            "browser_events_percentage": _percentage(item.get("browser_events_with_dedupe_key")),
            "server_events_percentage": _percentage(item.get("server_events_with_dedupe_key")),
        }
        for item in row.get("dedupe_key_feedback") or []
    ]
    return {
        "event_name": row.get("event_name"),
        "emq_score": score,
        "emq_is_weak": None if score is None else score < WEAK_EMQ_SCORE_BELOW,
        "match_key_coverage": [
            {"identifier": item.get("identifier"), "coverage_percentage": _percentage(item.get("coverage"))}
            for item in emq.get("match_key_feedback") or []
        ],
        "event_coverage_percentage": coverage,
        "event_coverage_goal_percentage": goal,
        "event_coverage_below_goal": None if coverage is None else coverage < goal,
        "data_freshness": freshness if isinstance(freshness, dict) else None,
        "dedupe_key_feedback": dedupe,
        "diagnostics": list(emq.get("diagnostics") or []),
    }


@mcp_server.tool()
async def get_dataset_quality(
    dataset_id: str,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Event match quality (score out of 10) per web event for one pixel, with coverage per match key, event coverage, deduplication and data freshness, and plain flags for weak scores stating the threshold used. agent_name limits scoring to events sent by one partner integration."""
    dataset_id = _required_id(dataset_id, "dataset_id")
    agent_name = blank_to_none(agent_name)
    params: dict[str, Any] = {"dataset_id": dataset_id}
    if agent_name is not None:
        params["agent_name"] = agent_name
    payload = await get_graph_api_client().get_object(
        "dataset_quality",
        fields=[DATASET_QUALITY_FIELDS],
        params=params,
    )
    rows = payload.get("web") or []
    events = [_quality_event(row) for row in rows if isinstance(row, dict)]
    findings: list[dict[str, Any]] = []
    missing: list[str] = []
    if not events:
        missing.append(
            "Meta returned no web event quality data for this dataset; event match quality "
            "is only computed for web events that received server (Conversions API) events."
        )
    for event in events:
        name = event["event_name"]
        if event["emq_score"] is None:
            missing.append(f"No event match quality score returned for {name}.")
        elif event["emq_is_weak"]:
            findings.append({
                "type": "weak_event_match_quality",
                "event_name": name,
                "composite_score": event["emq_score"],
                "threshold": WEAK_EMQ_SCORE_BELOW,
                "message": f"{name} scores {event['emq_score']} out of 10, below {WEAK_EMQ_SCORE_BELOW}.",
            })
        if event["event_coverage_below_goal"]:
            findings.append({
                "type": "low_event_coverage",
                "event_name": name,
                "coverage_percentage": event["event_coverage_percentage"],
                "threshold": event["event_coverage_goal_percentage"],
                "message": (
                    f"{name} server event coverage is {event['event_coverage_percentage']}%, "
                    f"below the {event['event_coverage_goal_percentage']}% goal."
                ),
            })
    findings.sort(key=lambda item: (item["type"], item.get("composite_score") or item.get("coverage_percentage") or 0))
    metrics = {
        "events": len(events),
        "events_with_emq_score": sum(1 for event in events if event["emq_score"] is not None),
        "weak_emq_events": sum(1 for item in findings if item["type"] == "weak_event_match_quality"),
        "low_coverage_events": sum(1 for item in findings if item["type"] == "low_event_coverage"),
    }
    return analysis_response(
        scope={"dataset_id": dataset_id, "channel": "web", "agent_name": agent_name},
        metrics=metrics,
        findings=findings,
        missing_signals=missing,
        extra={
            "events": events,
            "thresholds": {
                "weak_emq_score_below": WEAK_EMQ_SCORE_BELOW,
                "event_coverage_default_goal_percentage": DEFAULT_COVERAGE_GOAL_PERCENTAGE,
                "source": QUALITY_THRESHOLD_SOURCE,
            },
        },
    )


@mcp_server.tool()
async def list_custom_conversions(
    account_id: str,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List custom conversions on an ad account with their event type, rule and pixel. Follow summary.next_after with after until summary.complete is true."""
    account_id = _required_id(account_id, "account_id")
    _check_limit(limit)
    payload = await get_graph_api_client().list_objects(
        normalize_account_id(account_id),
        "customconversions",
        fields=normalize_field_list(fields) or CUSTOM_CONVERSION_FIELDS,
        params=_page_params(limit, after),
    )
    return _page_summary(normalize_collection(payload))


async def _list_or_find(
    *,
    parent_id: str,
    edge: str,
    target_id: str | None,
    target_name: str,
    limit: int,
    after: str | None,
    fields: FieldList,
    extra_params: dict[str, Any],
) -> dict[str, Any]:
    _check_limit(limit)
    if target_id is not None and blank_to_none(after) is not None:
        raise ValidationError(f"after cannot be combined with {target_name}; the lookup scans pages itself.")
    client = get_graph_api_client()
    if target_id is not None:
        result = await _find_by_id(
            client, parent_id, edge, target_id=target_id, fields=fields, params=extra_params,
        )
    else:
        payload = await client.list_objects(
            parent_id, edge, fields=fields, params={**_page_params(limit, after), **extra_params},
        )
        result = _page_summary(normalize_collection(payload))
    result["summary"]["read_only_surface"] = True
    return result


@mcp_server.tool()
async def list_pixel_event_rules(
    pixel_id: str,
    rule_id: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List the event rules (codeless and URL rules that turn page activity into events) defined on a pixel, or find one rule by rule_id. Meta's public API only reads these rules; creating, editing or deleting them is done in Events Manager."""
    pixel_id = _required_id(pixel_id, "pixel_id")
    return await _list_or_find(
        parent_id=pixel_id,
        edge="event_rules",
        target_id=blank_to_none(rule_id),
        target_name="rule_id",
        limit=limit,
        after=after,
        fields=normalize_field_list(fields) or EVENT_RULE_FIELDS,
        extra_params={},
    )


@mcp_server.tool()
async def list_pixel_parameters(
    pixel_id: str,
    parameter_id: str | None = None,
    current_domain: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List the parameter extractors a pixel uses to grab values (such as price or content id) for codeless events, or find one by parameter_id; current_domain filters by site. Meta's public API only reads these; changes are made in Events Manager."""
    pixel_id = _required_id(pixel_id, "pixel_id")
    extra: dict[str, Any] = {}
    if domain := blank_to_none(current_domain):
        extra["current_domain"] = domain
    return await _list_or_find(
        parent_id=pixel_id,
        edge="extractors",
        target_id=blank_to_none(parameter_id),
        target_name="parameter_id",
        limit=limit,
        after=after,
        fields=normalize_field_list(fields) or PIXEL_PARAMETER_FIELDS,
        extra_params=extra,
    )
