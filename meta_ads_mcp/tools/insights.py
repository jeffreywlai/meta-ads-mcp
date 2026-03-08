"""Insights and reporting tools."""

from __future__ import annotations

import asyncio
import csv
from datetime import date
from io import StringIO
import json
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.diagnostics import compare_metric_sets, derive_core_metrics
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import normalize_collection, normalize_insights_row
from meta_ads_mcp.schemas import analysis_response, collection_response

DEFAULT_INSIGHTS_FIELDS = [
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "date_start",
    "date_stop",
    "spend",
    "impressions",
    "reach",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
    "frequency",
    "actions",
    "action_values",
]

DEFAULT_COMPARE_METRICS = [
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "cpc",
    "cpm",
    "conversions",
    "cpa",
    "roas",
]

LOWER_IS_BETTER_METRICS = {"cpc", "cpm", "cpa", "cpp", "cost_per_result", "spend"}


def _build_date_params(
    *,
    date_preset: str | None,
    since: str | None,
    until: str | None,
) -> dict[str, Any]:
    """Build a Graph API date filter."""
    if date_preset and (since or until):
        raise ValidationError("Use date_preset or since/until, not both.")
    if (since and not until) or (until and not since):
        raise ValidationError("Provide both since and until.")
    if since and until:
        date.fromisoformat(since)
        date.fromisoformat(until)
        return {"time_range": json.dumps({"since": since, "until": until})}
    return {"date_preset": date_preset or "last_7d"}


def _insights_params(
    *,
    level: str,
    date_preset: str | None = None,
    since: str | None = None,
    until: str | None = None,
    breakdowns: list[str] | None = None,
    action_breakdowns: list[str] | None = None,
    time_increment: int | str | None = None,
    use_unified_attribution_setting: bool = True,
    action_attribution_windows: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Build insights params."""
    params: dict[str, Any] = {
        "level": level,
        "limit": limit,
        "use_unified_attribution_setting": str(use_unified_attribution_setting).lower(),
        **_build_date_params(date_preset=date_preset, since=since, until=until),
    }
    if breakdowns:
        params["breakdowns"] = ",".join(breakdowns)
    if action_breakdowns:
        params["action_breakdowns"] = ",".join(action_breakdowns)
    if time_increment is not None:
        params["time_increment"] = time_increment
    if action_attribution_windows:
        params["action_attribution_windows"] = ",".join(action_attribution_windows)
    return params


def _normalize_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize insights rows and attach derived metrics."""
    rows = []
    for row in payload.get("data", []):
        normalized = normalize_insights_row(row)
        normalized["metrics"] = derive_core_metrics(normalized)
        rows.append(normalized)
    return rows


def _serialize_cell(value: Any) -> str:
    """Convert nested row values into a CSV-safe string."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """Render normalized insights rows to CSV."""
    if not rows:
        return ""

    fieldnames = sorted({key for row in rows for key in row})
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _serialize_cell(row.get(key)) for key in fieldnames})
    return output.getvalue()


async def _object_name(object_id: str) -> str | None:
    """Best-effort name lookup for comparison output."""
    client = get_graph_api_client()
    try:
        payload = await client.get_object(object_id, fields=["name"])
    except Exception:
        return None
    name = payload.get("name")
    return str(name) if name is not None else None


async def _comparison_row(
    *,
    level: str,
    object_id: str,
    date_preset: str | None,
    since: str | None,
    until: str | None,
    fields: list[str] | None,
    breakdowns: list[str] | None,
    action_breakdowns: list[str] | None,
    time_increment: int | str | None,
    limit: int,
) -> dict[str, Any]:
    """Fetch one object's insights summary for compare_performance."""
    try:
        payload = await get_entity_insights(
            level=level,
            object_id=object_id,
            date_preset=date_preset,
            since=since,
            until=until,
            fields=fields,
            breakdowns=breakdowns,
            action_breakdowns=action_breakdowns,
            time_increment=time_increment,
            limit=limit,
        )
        return {
            "object_id": object_id,
            "object_name": await _object_name(object_id) or object_id,
            "metrics": payload["summary"]["metrics"],
            "record_count": payload["summary"]["count"],
        }
    except Exception as exc:
        return {
            "object_id": object_id,
            "object_name": object_id,
            "error": str(exc),
        }


def _rank_comparisons(
    comparisons: list[dict[str, Any]],
    metrics: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Rank successful comparisons by metric."""
    rankings: dict[str, list[dict[str, Any]]] = {}
    successful = [item for item in comparisons if "metrics" in item]
    for metric in metrics:
        ranked = []
        for item in successful:
            value = item["metrics"].get(metric)
            if value is None:
                continue
            ranked.append(
                {
                    "object_id": item["object_id"],
                    "object_name": item["object_name"],
                    "value": value,
                }
            )
        ranked.sort(
            key=lambda item: float(item["value"]),
            reverse=metric not in LOWER_IS_BETTER_METRICS,
        )
        if ranked:
            rankings[metric] = ranked
    return rankings


def _aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate row metrics into a coarse summary."""
    if len(rows) == 1:
        return rows[0]["metrics"]

    spend = sum((row["metrics"].get("spend") or 0.0) for row in rows)
    impressions = sum((row["metrics"].get("impressions") or 0) for row in rows)
    clicks = sum((row["metrics"].get("clicks") or 0) for row in rows)
    conversions = sum((row["metrics"].get("conversions") or 0.0) for row in rows)
    conversion_value = sum((row["metrics"].get("conversion_value") or 0.0) for row in rows)
    frequency = None
    if impressions:
        reach = sum((row.get("reach") or 0) for row in rows)
        if reach:
            frequency = impressions / reach
    ctr = (clicks / impressions) if impressions else None
    cpc = (spend / clicks) if clicks else None
    cpm = ((spend / impressions) * 1000) if impressions else None
    cvr = (conversions / clicks) if clicks else None
    cpa = (spend / conversions) if conversions else None
    roas = (conversion_value / spend) if spend else None
    return {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "frequency": frequency,
        "ctr": ctr,
        "cpc": cpc,
        "cpm": cpm,
        "conversions": conversions,
        "conversion_value": conversion_value,
        "cvr": cvr,
        "cpa": cpa,
        "roas": roas,
    }


@mcp_server.tool()
async def get_entity_insights(
    level: str,
    object_id: str,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    fields: list[str] | None = None,
    breakdowns: list[str] | None = None,
    action_breakdowns: list[str] | None = None,
    time_increment: int | str | None = None,
    use_unified_attribution_setting: bool = True,
    action_attribution_windows: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return normalized insights for an account, campaign, ad set, or ad."""
    client = get_graph_api_client()
    payload = await client.get_insights(
        object_id,
        fields=fields or DEFAULT_INSIGHTS_FIELDS,
        params=_insights_params(
            level=level,
            date_preset=date_preset,
            since=since,
            until=until,
            breakdowns=breakdowns,
            action_breakdowns=action_breakdowns,
            time_increment=time_increment,
            use_unified_attribution_setting=use_unified_attribution_setting,
            action_attribution_windows=action_attribution_windows,
            limit=limit,
        ),
    )
    rows = _normalize_rows(payload)
    response = normalize_collection(payload)
    response["items"] = rows
    response["summary"]["metrics"] = _aggregate_metrics(rows)
    return response


@mcp_server.tool()
async def get_performance_breakdown(
    level: str,
    object_id: str,
    breakdown: str,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    fields: list[str] | None = None,
    sort_by: str = "spend",
) -> dict[str, Any]:
    """Return a ranked performance breakdown."""
    payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        date_preset=date_preset,
        since=since,
        until=until,
        fields=fields,
        breakdowns=[breakdown],
        limit=500,
    )
    ranked = sorted(
        payload["items"],
        key=lambda row: row["metrics"].get(sort_by) or 0.0,
        reverse=True,
    )
    return collection_response(
        ranked,
        paging=payload["paging"],
        summary={
            "count": len(ranked),
            "breakdown": breakdown,
            "metrics": payload["summary"]["metrics"],
            "top_segments": ranked[:5],
            "bottom_segments": ranked[-5:] if ranked else [],
        },
    )


@mcp_server.tool()
async def compare_time_ranges(
    level: str,
    object_id: str,
    current_since: str,
    current_until: str,
    previous_since: str,
    previous_until: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Compare two windows for the same entity."""
    current_payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        since=current_since,
        until=current_until,
        date_preset=None,
        fields=fields,
    )
    previous_payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        since=previous_since,
        until=previous_until,
        date_preset=None,
        fields=fields,
    )
    comparison = compare_metric_sets(
        current_payload["summary"]["metrics"],
        previous_payload["summary"]["metrics"],
    )
    return analysis_response(
        scope={"level": level, "object_id": object_id},
        metrics=current_payload["summary"]["metrics"],
        evidence=[],
        extra={
            "previous_metrics": previous_payload["summary"]["metrics"],
            "comparison": comparison,
            "current_window": {"since": current_since, "until": current_until},
            "previous_window": {"since": previous_since, "until": previous_until},
        },
    )


@mcp_server.tool()
async def compare_performance(
    level: str,
    object_ids: list[str],
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    fields: list[str] | None = None,
    breakdowns: list[str] | None = None,
    action_breakdowns: list[str] | None = None,
    time_increment: int | str | None = None,
    limit: int = 100,
    metrics: list[str] | None = None,
) -> dict[str, Any]:
    """Compare multiple objects over the same reporting window."""
    if not object_ids:
        raise ValidationError("Provide at least one object_id.")
    comparisons = await asyncio.gather(
        *[
            _comparison_row(
                level=level,
                object_id=object_id,
                date_preset=date_preset,
                since=since,
                until=until,
                fields=fields,
                breakdowns=breakdowns,
                action_breakdowns=action_breakdowns,
                time_increment=time_increment,
                limit=limit,
            )
            for object_id in object_ids
        ]
    )
    metrics_to_rank = metrics or DEFAULT_COMPARE_METRICS
    rankings = _rank_comparisons(comparisons, metrics_to_rank)
    successful = sum(1 for item in comparisons if "metrics" in item)
    failed = len(comparisons) - successful
    return collection_response(
        comparisons,
        summary={
            "count": len(comparisons),
            "successful": successful,
            "failed": failed,
            "metrics_compared": metrics_to_rank,
            "rankings": rankings,
            "window": {
                "date_preset": date_preset,
                "since": since,
                "until": until,
            },
        },
    )


@mcp_server.tool()
async def export_insights(
    level: str,
    object_id: str,
    format: str = "json",
    date_preset: str | None = "last_30d",
    since: str | None = None,
    until: str | None = None,
    fields: list[str] | None = None,
    breakdowns: list[str] | None = None,
    action_breakdowns: list[str] | None = None,
    time_increment: int | str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Export normalized insights as JSON or CSV."""
    export_format = format.lower()
    if export_format not in {"json", "csv"}:
        raise ValidationError("format must be 'json' or 'csv'.")
    payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        date_preset=date_preset,
        since=since,
        until=until,
        fields=fields,
        breakdowns=breakdowns,
        action_breakdowns=action_breakdowns,
        time_increment=time_increment,
        limit=limit,
    )
    data = (
        json.dumps(payload["items"], indent=2, sort_keys=True)
        if export_format == "json"
        else _rows_to_csv(payload["items"])
    )
    return {
        "format": export_format,
        "mime_type": "application/json" if export_format == "json" else "text/csv",
        "record_count": len(payload["items"]),
        "summary": payload["summary"],
        "query": {
            "level": level,
            "object_id": object_id,
            "date_preset": date_preset,
            "since": since,
            "until": until,
            "breakdowns": breakdowns or [],
            "action_breakdowns": action_breakdowns or [],
        },
        "data": data,
    }


@mcp_server.tool()
async def create_async_insights_report(
    level: str,
    object_id: str,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    fields: list[str] | None = None,
    breakdowns: list[str] | None = None,
    action_breakdowns: list[str] | None = None,
    time_increment: int | str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Start an async insights report."""
    client = get_graph_api_client()
    payload = await client.create_async_insights_report(
        object_id,
        fields=fields or DEFAULT_INSIGHTS_FIELDS,
        params=_insights_params(
            level=level,
            date_preset=date_preset,
            since=since,
            until=until,
            breakdowns=breakdowns,
            action_breakdowns=action_breakdowns,
            time_increment=time_increment,
            limit=limit,
        ),
    )
    return {
        "report_run_id": payload.get("report_run_id") or payload.get("id"),
        "status": payload,
        "poll_hint": "Use get_async_insights_report with the returned report_run_id.",
    }


@mcp_server.tool()
async def get_async_insights_report(
    report_run_id: str,
    fields: list[str] | None = None,
    limit: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """Poll and fetch an async insights report."""
    client = get_graph_api_client()
    payload = await client.get_async_report(
        report_run_id,
        fields=fields or DEFAULT_INSIGHTS_FIELDS,
        limit=limit,
        after=after,
    )
    rows_payload = payload.get("rows", {})
    if rows_payload:
        rows = _normalize_rows(rows_payload)
        return {
            "status": payload["status"],
            "rows": collection_response(
                rows,
                paging=normalize_collection(rows_payload)["paging"],
                summary={"count": len(rows), "metrics": _aggregate_metrics(rows)},
            ),
        }
    return {"status": payload["status"], "rows": {"items": [], "paging": {}, "summary": {"count": 0}}}
