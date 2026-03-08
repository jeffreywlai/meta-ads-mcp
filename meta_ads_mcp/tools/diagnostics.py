"""Optimization-oriented diagnostics tools."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.diagnostics import (
    build_finding,
    compare_metric_sets,
    detect_snapshot_findings,
    metric_evidence,
    previous_window,
    rank_rows,
)
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.schemas import analysis_response
from meta_ads_mcp.tools.insights import (
    DEFAULT_INSIGHTS_FIELDS,
    _aggregate_metrics,
    _insights_params,
    _normalize_rows,
    compare_time_ranges,
    get_entity_insights,
)


def _snapshot_suggestions(findings: list[dict[str, Any]]) -> list[str]:
    """Map findings to simple next actions."""
    suggestions: list[str] = []
    finding_types = {finding["type"] for finding in findings}
    if "budget_concentration" in finding_types:
        suggestions.append("Inspect budget allocation across the top spend drivers.")
    if "high_spend_low_conversion" in finding_types:
        suggestions.append("Check audience, creative, and landing-page alignment.")
    if "high_frequency_declining_ctr" in finding_types:
        suggestions.append("Review creative fatigue and audience saturation.")
    if not suggestions:
        suggestions.append("Compare this window to a prior window before making changes.")
    return suggestions


async def _child_insights(
    object_id: str,
    *,
    level: str,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Fetch child-entity insights rows."""
    client = get_graph_api_client()
    payload = await client.get_insights(
        object_id,
        fields=DEFAULT_INSIGHTS_FIELDS,
        params=_insights_params(
            level=level,
            date_preset=date_preset,
            since=since,
            until=until,
            limit=limit,
        ),
    )
    return _normalize_rows(payload)


@mcp_server.tool()
async def get_account_optimization_snapshot(
    account_id: str,
    date_preset: str | None = "last_7d",
    compare_to_previous: bool = False,
    top_n: int = 5,
) -> dict[str, Any]:
    """Build an account-level optimization briefing."""
    account_scope = await get_entity_insights(
        level="account",
        object_id=account_id,
        date_preset=date_preset,
    )
    campaigns = await _child_insights(account_id, level="campaign", date_preset=date_preset)
    findings = detect_snapshot_findings(account_scope["summary"]["metrics"], campaigns)
    ranked_by_spend = rank_rows(campaigns, "spend")
    ranked_by_roas = rank_rows(campaigns, "roas")
    extra: dict[str, Any] = {
        "top_spend_drivers": ranked_by_spend[:top_n],
        "top_result_drivers": ranked_by_roas[:top_n],
    }
    if compare_to_previous:
        extra["comparison_hint"] = "Use compare_time_ranges for explicit date windows."

    return analysis_response(
        scope={"level": "account", "object_id": account_id},
        metrics=account_scope["summary"]["metrics"],
        findings=findings,
        evidence=[],
        suggestions=_snapshot_suggestions(findings),
        extra=extra,
    )


@mcp_server.tool()
async def get_campaign_optimization_snapshot(
    campaign_id: str,
    date_preset: str | None = "last_7d",
    top_n_adsets: int = 5,
    top_n_ads: int = 5,
) -> dict[str, Any]:
    """Build a campaign-level optimization briefing."""
    campaign_scope = await get_entity_insights(
        level="campaign",
        object_id=campaign_id,
        date_preset=date_preset,
    )
    adsets = await _child_insights(campaign_id, level="adset", date_preset=date_preset)
    ads = await _child_insights(campaign_id, level="ad", date_preset=date_preset)
    findings = detect_snapshot_findings(campaign_scope["summary"]["metrics"], adsets)
    return analysis_response(
        scope={"level": "campaign", "object_id": campaign_id},
        metrics=campaign_scope["summary"]["metrics"],
        findings=findings,
        suggestions=_snapshot_suggestions(findings),
        extra={
            "top_adsets_by_spend": rank_rows(adsets, "spend")[:top_n_adsets],
            "top_ads_by_spend": rank_rows(ads, "spend")[:top_n_ads],
            "top_ads_by_roas": rank_rows(ads, "roas")[:top_n_ads],
        },
    )


@mcp_server.tool()
async def get_budget_pacing_report(
    level: str,
    object_id: str,
    date_preset: str | None = "last_7d",
) -> dict[str, Any]:
    """Report simple pacing and spend-trend signals."""
    payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        date_preset=date_preset,
        time_increment=1,
    )
    rows = payload["items"]
    summary_metrics = payload["summary"]["metrics"]
    findings = detect_snapshot_findings(summary_metrics)
    return analysis_response(
        scope={"level": level, "object_id": object_id},
        metrics=summary_metrics,
        findings=findings,
        suggestions=_snapshot_suggestions(findings),
        extra={
            "daily_rows": rows,
            "trend_summary": {
                "days": len(rows),
                "first_day_spend": rows[0]["metrics"]["spend"] if rows else None,
                "last_day_spend": rows[-1]["metrics"]["spend"] if rows else None,
            },
        },
    )


@mcp_server.tool()
async def get_creative_performance_report(
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    date_preset: str | None = "last_7d",
    top_n: int = 10,
) -> dict[str, Any]:
    """Rank ad-level creative performance."""
    object_id = adset_id or campaign_id or account_id
    level = "ad"
    rows = await _child_insights(object_id, level=level, date_preset=date_preset)
    ranked = rank_rows(rows, "roas")
    summary_metrics = _aggregate_metrics(rows)
    findings = detect_snapshot_findings(summary_metrics, rows)
    return analysis_response(
        scope={"level": level, "object_id": object_id},
        metrics=summary_metrics,
        findings=findings,
        suggestions=_snapshot_suggestions(findings),
        extra={
            "top_creatives": ranked[:top_n],
            "worst_creatives": list(reversed(ranked[-top_n:])) if ranked else [],
        },
    )


@mcp_server.tool()
async def get_creative_fatigue_report(
    campaign_id: str | None = None,
    adset_id: str | None = None,
    current_window_days: int = 7,
    previous_window_days: int = 7,
) -> dict[str, Any]:
    """Detect simple creative fatigue signals."""
    object_id = adset_id or campaign_id
    today = date.today()
    current_until = today - timedelta(days=1)
    current_since = current_until - timedelta(days=current_window_days - 1)
    previous_until = current_since - timedelta(days=1)
    previous_since = previous_until - timedelta(days=previous_window_days - 1)

    current_rows = await _child_insights(
        object_id,
        level="ad",
        since=current_since.isoformat(),
        until=current_until.isoformat(),
        date_preset=None,
    )
    previous_rows = await _child_insights(
        object_id,
        level="ad",
        since=previous_since.isoformat(),
        until=previous_until.isoformat(),
        date_preset=None,
    )
    previous_by_id = {row.get("ad_id") or row.get("id"): row for row in previous_rows}
    findings: list[dict[str, Any]] = []
    for current in current_rows:
        entity_id = current.get("ad_id") or current.get("id")
        prior = previous_by_id.get(entity_id)
        if not prior:
            continue
        comparison = compare_metric_sets(current["metrics"], prior["metrics"])
        ctr_drop = comparison["ctr"]["pct_delta"]
        freq_rise = comparison["frequency"]["pct_delta"]
        if (ctr_drop is not None and ctr_drop <= -0.2) and (freq_rise is not None and freq_rise >= 0.2):
            findings.append(
                build_finding(
                    "creative_fatigue_risk",
                    f"Ad {entity_id} shows higher frequency and weaker CTR than the prior window.",
                    severity="medium",
                    confidence=0.75,
                    affected_entities=[{"ad_id": entity_id}],
                    next_actions=[
                        "Review creative freshness.",
                        "Check audience saturation.",
                    ],
                )
            )
    return analysis_response(
        scope={"level": "ad", "object_id": object_id},
        metrics={},
        findings=findings or [
            build_finding(
                "insufficient_data",
                "No strong fatigue pattern was detected across the compared windows.",
                severity="low",
                confidence=0.4,
            )
        ],
        extra={
            "current_window": {"since": current_since.isoformat(), "until": current_until.isoformat()},
            "previous_window": {"since": previous_since.isoformat(), "until": previous_until.isoformat()},
        },
    )


@mcp_server.tool()
async def get_audience_performance_report(
    level: str,
    object_id: str,
    segment_by: str = "country",
    date_preset: str | None = "last_7d",
) -> dict[str, Any]:
    """Rank audience segments by performance."""
    payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        date_preset=date_preset,
        breakdowns=[segment_by],
    )
    rows = payload["items"]
    ranked = rank_rows(rows, "roas")
    return analysis_response(
        scope={"level": level, "object_id": object_id},
        metrics=payload["summary"]["metrics"],
        findings=detect_snapshot_findings(payload["summary"]["metrics"], rows),
        extra={
            "segment_by": segment_by,
            "top_segments": ranked[:10],
            "weak_segments": list(reversed(ranked[-10:])) if ranked else [],
        },
    )


@mcp_server.tool()
async def get_delivery_risk_report(
    campaign_id: str | None = None,
    adset_id: str | None = None,
    date_preset: str | None = "last_7d",
) -> dict[str, Any]:
    """Highlight delivery or efficiency risks."""
    object_id = adset_id or campaign_id
    level = "adset" if adset_id else "campaign"
    payload = await get_entity_insights(level=level, object_id=object_id, date_preset=date_preset)
    metrics = payload["summary"]["metrics"]
    findings = detect_snapshot_findings(metrics)
    evidence = [
        metric_evidence("frequency", metrics.get("frequency"), "provided_by_meta", {"frequency": metrics.get("frequency")}),
        metric_evidence("ctr", metrics.get("ctr"), "provided_or_derived", {"ctr": metrics.get("ctr")}),
        metric_evidence("roas", metrics.get("roas"), "conversion_value / spend", {"roas": metrics.get("roas")}),
    ]
    return analysis_response(
        scope={"level": level, "object_id": object_id},
        metrics=metrics,
        findings=findings,
        evidence=evidence,
        suggestions=_snapshot_suggestions(findings),
    )


@mcp_server.tool()
async def get_learning_phase_report(
    campaign_id: str | None = None,
    adset_id: str | None = None,
) -> dict[str, Any]:
    """Return learning-relevant metadata for a campaign or ad set."""
    client = get_graph_api_client()
    object_id = adset_id or campaign_id
    fields = [
        "id",
        "name",
        "status",
        "effective_status",
        "optimization_goal",
        "bid_strategy",
        "daily_budget",
        "lifetime_budget",
        "start_time",
        "end_time",
    ]
    item = await client.get_object(object_id, fields=fields)
    return analysis_response(
        scope={"level": "adset" if adset_id else "campaign", "object_id": object_id},
        metrics={},
        findings=[],
        missing_signals=["Learning-phase state is not always exposed consistently across objects."],
        extra={"item": item},
    )

