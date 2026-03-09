"""Optimization-oriented diagnostics tools."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.diagnostics import (
    annotate_share_metrics,
    build_finding,
    compare_metric_sets,
    detect_snapshot_findings,
    metric_evidence,
    previous_window,
    rank_rows,
    summary_metric_evidence,
)
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.schemas import analysis_response
from meta_ads_mcp.tools.insights import (
    DEFAULT_INSIGHTS_FIELDS,
    _aggregate_metrics,
    _insights_params,
    _normalize_rows,
    get_entity_insights,
)
from meta_ads_mcp.errors import ValidationError

LEARNING_PHASE_FIELDS_BY_LEVEL = {
    "campaign": [
        "id",
        "name",
        "status",
        "effective_status",
        "objective",
        "buying_type",
        "bid_strategy",
        "daily_budget",
        "lifetime_budget",
        "special_ad_categories",
    ],
    "adset": [
        "id",
        "name",
        "status",
        "effective_status",
        "campaign_id",
        "optimization_goal",
        "billing_event",
        "bid_strategy",
        "daily_budget",
        "lifetime_budget",
        "start_time",
        "end_time",
        "targeting",
    ],
}


def _resolve_scope(
    *,
    allowed_levels: tuple[str, ...],
    level: str | None = None,
    object_id: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
) -> tuple[str, str]:
    """Resolve generic or legacy scope inputs into one normalized level/object_id pair."""
    alias_candidates = [
        ("adset", adset_id),
        ("campaign", campaign_id),
        ("account", account_id),
    ]
    alias_level: str | None = None
    alias_object_id: str | None = None
    for candidate_level, candidate_id in alias_candidates:
        if candidate_id:
            alias_level = candidate_level
            alias_object_id = normalize_account_id(candidate_id) if candidate_level == "account" else candidate_id
            break

    normalized_level = level.strip() if isinstance(level, str) else level
    normalized_object_id = object_id.strip() if isinstance(object_id, str) else object_id
    if normalized_level == "":
        normalized_level = None
    if normalized_object_id == "":
        normalized_object_id = None

    if normalized_level is not None or normalized_object_id is not None:
        if not normalized_level or not normalized_object_id:
            raise ValidationError("Provide both level and object_id when using generic scope arguments.")
        if normalized_level not in allowed_levels:
            raise ValidationError(f"level must be one of {sorted(allowed_levels)}.")
        resolved_object_id = (
            normalize_account_id(normalized_object_id) if normalized_level == "account" else normalized_object_id
        )
        if alias_level and alias_object_id and (alias_level != normalized_level or alias_object_id != resolved_object_id):
            raise ValidationError("Conflicting scope arguments. Use either level/object_id or entity-specific params.")
        return normalized_level, resolved_object_id

    if alias_level and alias_object_id:
        if alias_level not in allowed_levels:
            raise ValidationError(f"level must be one of {sorted(allowed_levels)}.")
        return alias_level, alias_object_id

    raise ValidationError("Provide a valid scope using level/object_id or the tool's entity-specific params.")

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


def _window_kwargs(
    *,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Build shared date-window kwargs for diagnostics reads."""
    return {
        "date_preset": None if (since or until) else date_preset,
        "since": since,
        "until": until,
    }


def _fatigue_windows(
    *,
    since: str | None,
    until: str | None,
    previous_since: str | None,
    previous_until: str | None,
    current_window_days: int,
    previous_window_days: int,
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve current and previous windows for fatigue analysis."""
    def _parse_iso(value: str, *, field: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{field} must be a valid ISO date in YYYY-MM-DD format.") from exc

    if any(value is not None for value in (since, until, previous_since, previous_until)):
        if not since or not until:
            raise ValidationError("Provide both since and until for explicit fatigue windows.")
        current_since_date = _parse_iso(since, field="since")
        current_until_date = _parse_iso(until, field="until")
        if current_until_date < current_since_date:
            raise ValidationError("until must be on or after since.")
        if previous_since and previous_until:
            previous_since_date = _parse_iso(previous_since, field="previous_since")
            previous_until_date = _parse_iso(previous_until, field="previous_until")
            if previous_until_date < previous_since_date:
                raise ValidationError("previous_until must be on or after previous_since.")
        elif previous_since or previous_until:
            raise ValidationError("Provide both previous_since and previous_until when specifying a previous fatigue window.")
        else:
            previous_since_date, previous_until_date = previous_window(current_since_date, current_until_date)
        return (
            {"since": current_since_date.isoformat(), "until": current_until_date.isoformat()},
            {"since": previous_since_date.isoformat(), "until": previous_until_date.isoformat()},
        )

    today = date.today()
    current_until = today - timedelta(days=1)
    current_since = current_until - timedelta(days=current_window_days - 1)
    previous_until_date = current_since - timedelta(days=1)
    previous_since_date = previous_until_date - timedelta(days=previous_window_days - 1)
    return (
        {"since": current_since.isoformat(), "until": current_until.isoformat()},
        {"since": previous_since_date.isoformat(), "until": previous_until_date.isoformat()},
    )


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
    since: str | None = None,
    until: str | None = None,
    compare_to_previous: bool = False,
    top_n: int = 5,
) -> dict[str, Any]:
    """Use this for a top-level account briefing before asking narrower optimization questions."""
    resolved_account_id = normalize_account_id(account_id)
    account_scope, campaigns = await asyncio.gather(
        get_entity_insights(
            level="account",
            object_id=resolved_account_id,
            **_window_kwargs(date_preset=date_preset, since=since, until=until),
        ),
        _child_insights(
            resolved_account_id,
            level="campaign",
            **_window_kwargs(date_preset=date_preset, since=since, until=until),
        ),
    )
    campaigns = annotate_share_metrics(campaigns)
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
        scope={"level": "account", "object_id": resolved_account_id},
        metrics=account_scope["summary"]["metrics"],
        findings=findings,
        evidence=summary_metric_evidence(account_scope["summary"]["metrics"]),
        suggestions=_snapshot_suggestions(findings),
        extra=extra,
    )


@mcp_server.tool()
async def get_campaign_optimization_snapshot(
    campaign_id: str,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    top_n_adsets: int = 5,
    top_n_ads: int = 5,
) -> dict[str, Any]:
    """Use this for a campaign summary that ranks the most important ad sets and ads."""
    campaign_scope, adsets, ads = await asyncio.gather(
        get_entity_insights(
            level="campaign",
            object_id=campaign_id,
            **_window_kwargs(date_preset=date_preset, since=since, until=until),
        ),
        _child_insights(campaign_id, level="adset", **_window_kwargs(date_preset=date_preset, since=since, until=until)),
        _child_insights(campaign_id, level="ad", **_window_kwargs(date_preset=date_preset, since=since, until=until)),
    )
    adsets = annotate_share_metrics(adsets)
    ads = annotate_share_metrics(ads)
    findings = detect_snapshot_findings(campaign_scope["summary"]["metrics"], adsets)
    return analysis_response(
        scope={"level": "campaign", "object_id": campaign_id},
        metrics=campaign_scope["summary"]["metrics"],
        findings=findings,
        evidence=summary_metric_evidence(campaign_scope["summary"]["metrics"]),
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
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Use this when the user asks about spend pacing, spend trend, or daily delivery consistency."""
    payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        **_window_kwargs(date_preset=date_preset, since=since, until=until),
        time_increment=1,
    )
    rows = payload["items"]
    summary_metrics = payload["summary"]["metrics"]
    findings = detect_snapshot_findings(summary_metrics)
    return analysis_response(
        scope={"level": level, "object_id": object_id},
        metrics=summary_metrics,
        findings=findings,
        evidence=summary_metric_evidence(summary_metrics),
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
    level: str | None = None,
    object_id: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Use this when the user wants top and worst ad-level creative performers within an account, campaign, or ad set. Prefer level/object_id for consistency."""
    _scope_level, resolved_object_id = _resolve_scope(
        allowed_levels=("account", "campaign", "adset"),
        level=level,
        object_id=object_id,
        account_id=account_id,
        campaign_id=campaign_id,
        adset_id=adset_id,
    )
    rows = await _child_insights(
        resolved_object_id,
        level="ad",
        **_window_kwargs(date_preset=date_preset, since=since, until=until),
    )
    rows = annotate_share_metrics(rows)
    ranked = rank_rows(rows, "roas")
    summary_metrics = _aggregate_metrics(rows)
    findings = detect_snapshot_findings(summary_metrics, rows)
    return analysis_response(
        scope={"level": "ad", "object_id": resolved_object_id},
        metrics=summary_metrics,
        findings=findings,
        evidence=summary_metric_evidence(summary_metrics),
        suggestions=_snapshot_suggestions(findings),
        extra={
            "top_creatives": ranked[:top_n],
            "worst_creatives": list(reversed(ranked[-top_n:])) if ranked else [],
        },
    )


@mcp_server.tool()
async def get_creative_fatigue_report(
    level: str | None = None,
    object_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    previous_since: str | None = None,
    previous_until: str | None = None,
    current_window_days: int = 7,
    previous_window_days: int = 7,
) -> dict[str, Any]:
    """Use this when the user asks whether ads are fatiguing between a current and previous window. Prefer level/object_id for consistency."""
    _scope_level, resolved_object_id = _resolve_scope(
        allowed_levels=("campaign", "adset"),
        level=level,
        object_id=object_id,
        campaign_id=campaign_id,
        adset_id=adset_id,
    )
    current_window, previous_window_range = _fatigue_windows(
        since=since,
        until=until,
        previous_since=previous_since,
        previous_until=previous_until,
        current_window_days=current_window_days,
        previous_window_days=previous_window_days,
    )

    current_rows, previous_rows = await asyncio.gather(
        _child_insights(
            resolved_object_id,
            level="ad",
            since=current_window["since"],
            until=current_window["until"],
            date_preset=None,
        ),
        _child_insights(
            resolved_object_id,
            level="ad",
            since=previous_window_range["since"],
            until=previous_window_range["until"],
            date_preset=None,
        ),
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
                    evidence=[
                        metric_evidence(
                            "frequency_change",
                            comparison["frequency"]["pct_delta"],
                            "(current_frequency - previous_frequency) / previous_frequency",
                            {
                                "current_frequency": current["metrics"].get("frequency"),
                                "previous_frequency": prior["metrics"].get("frequency"),
                            },
                        ),
                        metric_evidence(
                            "ctr",
                            current["metrics"].get("ctr"),
                            "clicks / impressions",
                            {
                                "current_clicks": current.get("clicks"),
                                "current_impressions": current.get("impressions"),
                                "previous_clicks": prior.get("clicks"),
                                "previous_impressions": prior.get("impressions"),
                            },
                        ),
                    ],
                    affected_entities=[{"ad_id": entity_id}],
                    next_actions=[
                        "Review creative freshness.",
                        "Check audience saturation.",
                    ],
                )
            )
    return analysis_response(
        scope={"level": "ad", "object_id": resolved_object_id},
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
            "current_window": current_window,
            "previous_window": previous_window_range,
        },
    )


@mcp_server.tool()
async def get_audience_performance_report(
    level: str,
    object_id: str,
    segment_by: str = "country",
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Use this when the user wants performance ranked by one audience-like breakdown, such as country or region."""
    payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        **_window_kwargs(date_preset=date_preset, since=since, until=until),
        breakdowns=[segment_by],
    )
    rows = payload["items"]
    rows = annotate_share_metrics(rows)
    ranked = rank_rows(rows, "roas")
    return analysis_response(
        scope={"level": level, "object_id": object_id},
        metrics=payload["summary"]["metrics"],
        findings=detect_snapshot_findings(payload["summary"]["metrics"], rows),
        evidence=summary_metric_evidence(payload["summary"]["metrics"]),
        extra={
            "segment_by": segment_by,
            "top_segments": ranked[:10],
            "weak_segments": list(reversed(ranked[-10:])) if ranked else [],
        },
    )


@mcp_server.tool()
async def get_delivery_risk_report(
    level: str | None = None,
    object_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Use this when the user asks about delivery risk, weak efficiency, or whether a campaign/ad set looks unhealthy. Prefer level/object_id for consistency."""
    resolved_level, resolved_object_id = _resolve_scope(
        allowed_levels=("campaign", "adset"),
        level=level,
        object_id=object_id,
        campaign_id=campaign_id,
        adset_id=adset_id,
    )
    payload = await get_entity_insights(
        level=resolved_level,
        object_id=resolved_object_id,
        **_window_kwargs(date_preset=date_preset, since=since, until=until),
    )
    metrics = payload["summary"]["metrics"]
    findings = detect_snapshot_findings(metrics)
    evidence = summary_metric_evidence(metrics)
    return analysis_response(
        scope={"level": resolved_level, "object_id": resolved_object_id},
        metrics=metrics,
        findings=findings,
        evidence=evidence,
        suggestions=_snapshot_suggestions(findings),
    )


@mcp_server.tool()
async def get_learning_phase_report(
    level: str | None = None,
    object_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
) -> dict[str, Any]:
    """Use this when the user asks about learning-phase context or setup metadata for a campaign or ad set. Prefer level/object_id for consistency."""
    client = get_graph_api_client()
    resolved_level, resolved_object_id = _resolve_scope(
        allowed_levels=("campaign", "adset"),
        level=level,
        object_id=object_id,
        campaign_id=campaign_id,
        adset_id=adset_id,
    )
    fields = LEARNING_PHASE_FIELDS_BY_LEVEL[resolved_level]
    item = await client.get_object(resolved_object_id, fields=fields)
    missing_signals = ["Learning-phase state is not always exposed consistently across objects."]
    if resolved_level == "campaign":
        missing_signals.append("optimization_goal is available on ad sets, not campaigns.")
    return analysis_response(
        scope={"level": resolved_level, "object_id": resolved_object_id},
        metrics={},
        findings=[],
        missing_signals=missing_signals,
        extra={"item": item},
    )
