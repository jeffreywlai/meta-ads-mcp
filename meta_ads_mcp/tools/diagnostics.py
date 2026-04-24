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
from meta_ads_mcp.normalize import blank_to_none, to_float
from meta_ads_mcp.pagination import extract_paging
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

QUALITY_RANKING_FIELDS = (
    "quality_ranking",
    "engagement_rate_ranking",
    "conversion_rate_ranking",
)

AD_QUALITY_FIELDS = [*DEFAULT_INSIGHTS_FIELDS, *QUALITY_RANKING_FIELDS]

OVERLAP_INSIGHTS_FIELDS = (
    "campaign_id campaign_name spend impressions reach "
    "frequency cpm"
).split()

WEAK_RANKINGS = {"BELOW_AVERAGE_10", "BELOW_AVERAGE_20", "BELOW_AVERAGE_35"}
FEEDBACK_UNAVAILABLE_SIGNALS = [
    "Customer feedback score is not exposed here as a stable public Marketing API field.",
    "Negative-feedback counts such as hides or reports are not exposed as stable Ads Insights fields here.",
    "Commerce/catalog product review feeds are not exposed here; use list_page_recommendations for Page-level recommendations.",
]


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
        ("adset", blank_to_none(adset_id)),
        ("campaign", blank_to_none(campaign_id)),
        ("account", blank_to_none(account_id)),
    ]
    provided_aliases = [(candidate_level, candidate_id) for candidate_level, candidate_id in alias_candidates if candidate_id]
    if len(provided_aliases) > 1:
        raise ValidationError("Provide only one entity-specific scope argument.")
    alias_level: str | None = None
    alias_object_id: str | None = None
    if provided_aliases:
        alias_level, candidate_id = provided_aliases[0]
        alias_object_id = normalize_account_id(candidate_id) if alias_level == "account" else candidate_id

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


def _snapshot_analysis(
    *,
    scope: dict[str, Any],
    metrics: dict[str, Any],
    child_rows: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard optimization analysis envelope."""
    findings = detect_snapshot_findings(metrics, child_rows)
    return analysis_response(
        scope=scope,
        metrics=metrics,
        findings=findings,
        evidence=summary_metric_evidence(metrics),
        suggestions=_snapshot_suggestions(findings),
        extra=extra,
    )


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


def _window_descriptor(date_preset: str | None, since: str | None, until: str | None) -> dict[str, Any]:
    """Return the caller-facing window shape used in analysis extras."""
    return {
        "date_preset": None if (since or until) else date_preset,
        "since": since,
        "until": until,
    }


def _compact_timeseries_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a token-efficient daily row shape for pacing-style outputs."""
    return [
        {
            "date_start": row.get("date_start"),
            "date_stop": row.get("date_stop"),
            "metrics": dict(row.get("metrics") or {}),
        }
        for row in rows
    ]


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
    if any(value is not None for value in (since, until, previous_since, previous_until)):
        if not since or not until:
            raise ValidationError("Provide both since and until for explicit fatigue windows.")
        current_since_date = _parse_required_date(since, field="since")
        current_until_date = _parse_required_date(until, field="until")
        if current_until_date < current_since_date:
            raise ValidationError("until must be on or after since.")
        if previous_since and previous_until:
            previous_since_date = _parse_required_date(previous_since, field="previous_since")
            previous_until_date = _parse_required_date(previous_until, field="previous_until")
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
    fields: list[str] | None = None,
    breakdowns: list[str] | None = None,
    action_breakdowns: list[str] | None = None,
    limit: int = 250,
    max_rows: int = 1000,
) -> list[dict[str, Any]]:
    """Fetch child-entity insights rows."""
    if limit < 1 or max_rows < 1:
        raise ValidationError("limit and max_rows must be positive.")
    client = get_graph_api_client()
    page_limit = min(limit, max_rows)
    base_params = _insights_params(
        level=level,
        date_preset=date_preset,
        since=since,
        until=until,
        breakdowns=breakdowns,
        action_breakdowns=action_breakdowns,
        limit=page_limit,
    )
    rows: list[dict[str, Any]] = []
    after: str | None = None
    seen_after: set[str] = set()
    while True:
        params = dict(base_params)
        if after:
            params["after"] = after
        payload = await client.get_insights(
            object_id,
            fields=fields or DEFAULT_INSIGHTS_FIELDS,
            params=params,
        )
        rows.extend(_normalize_rows(payload))
        if len(rows) >= max_rows:
            return rows[:max_rows]
        paging = extract_paging(payload)
        next_after = paging.get("after") if paging.get("next") else None
        if not next_after or next_after in seen_after:
            break
        seen_after.add(next_after)
        after = next_after
    return rows


def _parse_required_date(value: str, *, field: str) -> date:
    """Parse an ISO date for compact period helpers."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} must be a valid ISO date in YYYY-MM-DD format.") from exc


def _year_ago_window(since: date, until: date) -> tuple[date, date]:
    """Return the same date window one year earlier."""
    try:
        return since.replace(year=since.year - 1), until.replace(year=until.year - 1)
    except ValueError:
        return since - timedelta(days=365), until - timedelta(days=365)


def _previous_comparable_window(since: date, until: date) -> tuple[date, date]:
    """Return previous calendar month for full-month windows, otherwise equal-length prior window."""
    is_single_full_month = (
        since.day == 1
        and (until + timedelta(days=1)).day == 1
        and since.year == until.year
        and since.month == until.month
    )
    if is_single_full_month:
        previous_until = since - timedelta(days=1)
        return previous_until.replace(day=1), previous_until
    return previous_window(since, until)


def _weak_quality_rows(rows: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """Return rows with below-average Meta quality ranking fields."""
    return [
        row for row in rows
        if any(str(row.get(field) or "").upper() in WEAK_RANKINGS for field in QUALITY_RANKING_FIELDS)
    ][:top_n]


def _quality_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build quality-ranking findings from weak rows."""
    if not rows:
        return [
            build_finding(
                "no_weak_quality_rankings_detected",
                "No below-average quality ranking signal was present in the returned rows.",
                severity="low",
                confidence=0.45,
            )
        ]
    return [
        build_finding(
            "weak_ad_quality_ranking",
            "One or more ads have below-average Meta quality, engagement, or conversion-rate rankings.",
            severity="medium",
            confidence=0.7,
            affected_entities=[{"ad_id": row.get("ad_id"), "ad_name": row.get("ad_name")} for row in rows],
            next_actions=[
                "Review creative freshness and message-market fit.",
                "Compare weak ads against top spenders before pausing or replacing assets.",
            ],
        )
    ]


def _platform_rows(rows: list[dict[str, Any]], *, min_spend: float) -> list[dict[str, Any]]:
    """Compact platform breakdown rows for overlap output."""
    platform_rows = []
    for row in rows:
        metrics = row.get("metrics") or {}
        spend = to_float(metrics.get("spend")) or 0.0
        if spend < min_spend:
            continue
        platform_rows.append(
            {
                "publisher_platform": row.get("publisher_platform"),
                "spend": spend,
                "reach": row.get("reach"),
                "frequency": metrics.get("frequency"),
                "cpm": metrics.get("cpm"),
            }
        )
    return platform_rows


def _unique_campaign_refs(campaigns: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Return unique campaign refs by id while preserving input order."""
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for campaign in campaigns:
        campaign_id = str(campaign.get("id") or "").strip()
        if not campaign_id or campaign_id in seen:
            continue
        seen.add(campaign_id)
        selected.append(campaign)
        if len(selected) >= limit:
            break
    return selected


def _group_overlap_platforms(campaigns: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group platform rows shared by more than one campaign."""
    platform_to_campaigns: dict[str, list[dict[str, Any]]] = {}
    for campaign in campaigns:
        for platform in campaign["platforms"]:
            platform_name = str(platform.get("publisher_platform") or "unknown")
            platform_to_campaigns.setdefault(platform_name, []).append(
                {
                    "campaign_id": campaign["campaign_id"],
                    "campaign_name": campaign["campaign_name"],
                    **platform,
                }
            )
    return {
        platform: sorted(rows, key=lambda row: row["spend"], reverse=True)
        for platform, rows in platform_to_campaigns.items()
        if len(rows) >= 2
    }


def _overlap_findings(overlap_platforms: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Build overlap findings from shared platform groups."""
    findings = []
    for platform, rows in overlap_platforms.items():
        total_spend = sum(row["spend"] for row in rows)
        findings.append(
            build_finding(
                "potential_auction_overlap",
                f"{len(rows)} campaigns spent on {platform}, which is a directional overlap signal.",
                severity="medium",
                confidence=0.55,
                evidence=[
                    metric_evidence(
                        "platform_spend",
                        total_spend,
                        "sum campaign spend on publisher_platform",
                        {"publisher_platform": platform, "campaign_count": len(rows)},
                    )
                ],
                affected_entities=[{"campaign_id": row["campaign_id"]} for row in rows],
                next_actions=[
                    "Inspect ad set targeting for shared audiences.",
                    "Compare combined CPA/ROAS before changing budgets.",
                ],
            )
        )
    return findings or [
        build_finding(
            "no_platform_overlap_detected",
            "No shared publisher-platform spend above the threshold was detected.",
            severity="low",
            confidence=0.45,
        )
    ]


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
    window = _window_kwargs(date_preset=date_preset, since=since, until=until)
    account_scope, campaigns = await asyncio.gather(
        get_entity_insights(
            level="account",
            object_id=resolved_account_id,
            **window,
        ),
        _child_insights(
            resolved_account_id,
            level="campaign",
            **window,
        ),
    )
    campaigns = annotate_share_metrics(campaigns)
    ranked_by_spend = rank_rows(campaigns, "spend")
    ranked_by_roas = rank_rows(campaigns, "roas")
    extra: dict[str, Any] = {
        "top_spend_drivers": ranked_by_spend[:top_n],
        "top_result_drivers": ranked_by_roas[:top_n],
    }
    if compare_to_previous:
        extra["comparison_hint"] = "Use compare_time_ranges for explicit date windows."

    return _snapshot_analysis(
        scope={"level": "account", "object_id": resolved_account_id},
        metrics=account_scope["summary"]["metrics"],
        child_rows=campaigns,
        extra=extra,
    )


@mcp_server.tool()
async def get_account_health_snapshot(
    account_id: str,
    date_preset: str | None = "last_30d",
    since: str | None = None,
    until: str | None = None,
    include_previous: bool = True,
    include_year_over_year: bool = True,
) -> dict[str, Any]:
    """Use this for one-call account totals with optional previous-window and year-over-year comparisons."""
    resolved_account_id = normalize_account_id(account_id)
    window = _window_kwargs(date_preset=date_preset, since=since, until=until)
    current = await get_entity_insights(
        level="account",
        object_id=resolved_account_id,
        **window,
    )
    current_metrics = current["summary"]["metrics"]
    extra: dict[str, Any] = {"current_window": _window_descriptor(date_preset, since, until)}

    if since and until and (include_previous or include_year_over_year):
        since_date = _parse_required_date(since, field="since")
        until_date = _parse_required_date(until, field="until")
        if until_date < since_date:
            raise ValidationError("until must be on or after since.")
        windows: list[tuple[str, date, date]] = []
        if include_previous:
            previous_since, previous_until = _previous_comparable_window(since_date, until_date)
            windows.append(("previous", previous_since, previous_until))
        if include_year_over_year:
            yoy_since, yoy_until = _year_ago_window(since_date, until_date)
            windows.append(("year_over_year", yoy_since, yoy_until))
        extra.update(
            {
                f"{label}_window": {"since": window_since.isoformat(), "until": window_until.isoformat()}
                for label, window_since, window_until in windows
            }
        )
        comparison_payloads = await asyncio.gather(
            *[
                get_entity_insights(
                    level="account",
                    object_id=resolved_account_id,
                    date_preset=None,
                    since=window_since.isoformat(),
                    until=window_until.isoformat(),
                )
                for _, window_since, window_until in windows
            ]
        )
        extra["comparisons"] = {
            label: {
                "metrics": payload["summary"]["metrics"],
                "comparison": compare_metric_sets(current_metrics, payload["summary"]["metrics"]),
            }
            for (label, _, _), payload in zip(windows, comparison_payloads, strict=True)
        }
    elif include_previous or include_year_over_year:
        extra["comparison_hint"] = "Provide explicit since and until to include previous-window or year-over-year comparisons."

    return _snapshot_analysis(
        scope={"level": "account", "object_id": resolved_account_id},
        metrics=current_metrics,
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
    """Use this for a campaign health or optimization snapshot that ranks the most important ad sets and ads."""
    window = _window_kwargs(date_preset=date_preset, since=since, until=until)
    campaign_scope, adsets, ads = await asyncio.gather(
        get_entity_insights(
            level="campaign",
            object_id=campaign_id,
            **window,
        ),
        _child_insights(campaign_id, level="adset", **window),
        _child_insights(campaign_id, level="ad", **window),
    )
    adsets = annotate_share_metrics(adsets)
    ads = annotate_share_metrics(ads)
    return _snapshot_analysis(
        scope={"level": "campaign", "object_id": campaign_id},
        metrics=campaign_scope["summary"]["metrics"],
        child_rows=adsets,
        extra={
            "top_adsets_by_spend": rank_rows(adsets, "spend")[:top_n_adsets],
            "top_ads_by_spend": rank_rows(ads, "spend")[:top_n_ads],
            "top_ads_by_roas": rank_rows(ads, "roas")[:top_n_ads],
        },
    )


@mcp_server.tool()
async def detect_auction_overlap(
    account_id: str,
    campaign_ids: list[str] | None = None,
    date_preset: str | None = "last_30d",
    since: str | None = None,
    until: str | None = None,
    max_campaigns: int = 12,
    min_platform_spend: float = 1.0,
) -> dict[str, Any]:
    """Use this for a compact cannibalization or platform-overlap screen across selected campaign ids."""
    resolved_account_id = normalize_account_id(account_id)
    if campaign_ids:
        selected_campaigns = _unique_campaign_refs(
            [{"id": campaign_id, "name": campaign_id} for campaign_id in campaign_ids],
            limit=max_campaigns,
        )
    else:
        client = get_graph_api_client()
        payload = await client.list_objects(
            resolved_account_id,
            "campaigns",
            fields=["id", "name", "effective_status"],
            params={"limit": max_campaigns, "effective_status": ["ACTIVE"]},
        )
        selected_campaigns = _unique_campaign_refs(payload.get("data", []), limit=max_campaigns)

    window = _window_kwargs(date_preset=date_preset, since=since, until=until)

    async def campaign_summary(campaign: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(campaign.get("id"))
        payload = await get_entity_insights(
            level="campaign",
            object_id=campaign_id,
            **window,
            fields=OVERLAP_INSIGHTS_FIELDS,
            breakdowns=["publisher_platform"],
        )
        return {
            "campaign_id": campaign_id,
            "campaign_name": campaign.get("name") or campaign_id,
            "metrics": payload["summary"]["metrics"],
            "platforms": _platform_rows(payload["items"], min_spend=min_platform_spend),
        }

    campaign_summaries = list(
        await asyncio.gather(*(campaign_summary(campaign) for campaign in selected_campaigns))
    )

    overlap_platforms = _group_overlap_platforms(campaign_summaries)

    return analysis_response(
        scope={"level": "account", "object_id": resolved_account_id},
        metrics={},
        findings=_overlap_findings(overlap_platforms),
        missing_signals=[
            "Publisher-platform overlap is directional; it is not person-level auction overlap.",
            "Audience overlap requires inspecting targeting and reach context outside this aggregate insights response.",
        ],
        extra={
            "campaign_count": len(campaign_summaries),
            "overlap_platforms": overlap_platforms,
            "campaigns": campaign_summaries,
            "window": _window_descriptor(date_preset, since, until),
        },
    )


@mcp_server.tool()
async def get_budget_pacing_report(
    level: str,
    object_id: str,
    date_preset: str | None = "last_7d",
    since: str | None = None,
    until: str | None = None,
    include_full_daily_rows: bool = False,
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
    return _snapshot_analysis(
        scope={"level": level, "object_id": object_id},
        metrics=summary_metrics,
        extra={
            "daily_rows": rows if include_full_daily_rows else _compact_timeseries_rows(rows),
            "daily_row_detail": "full" if include_full_daily_rows else "compact",
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
    include_quality_rankings: bool = True,
) -> dict[str, Any]:
    """Use this when the user wants top and worst ad-level creative performers, including quality rankings when available."""
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
        fields=AD_QUALITY_FIELDS if include_quality_rankings else None,
    )
    rows = annotate_share_metrics(rows)
    ranked = rank_rows(rows, "roas")
    summary_metrics = _aggregate_metrics(rows)
    return _snapshot_analysis(
        scope={"level": "ad", "object_id": resolved_object_id},
        metrics=summary_metrics,
        child_rows=rows,
        extra={
            "top_creatives": ranked[:top_n],
            "worst_creatives": list(reversed(ranked[-top_n:])) if ranked else [],
            "quality_rankings_requested": include_quality_rankings,
        },
    )


@mcp_server.tool()
async def get_ad_feedback_signals(
    level: str | None = None,
    object_id: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    ad_id: str | None = None,
    date_preset: str | None = "last_30d",
    since: str | None = None,
    until: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Use this when the user asks for ad comments, reviews, testimonials, customer feedback, negative feedback, or quality rankings."""
    if not any([level, object_id, account_id, campaign_id, adset_id, ad_id]):
        available_signals = [
            "raw Facebook or Instagram comments when an ad exposes a social story/media id",
            "Facebook Page recommendations for owned Pages",
            *QUALITY_RANKING_FIELDS,
            "creative fatigue and performance from ad-level insights",
        ]
        recommended_tools = [
            "list_ad_comments(ad_id='...')",
            "list_page_recommendations(page_id='...')",
            "get_ad_social_context(ad_id='...')",
            "get_ad_feedback_signals(level='campaign', object_id='...')",
            "get_creative_performance_report(level='campaign', object_id='...')",
            "get_creative_fatigue_report(campaign_id='...')",
        ]
        return analysis_response(
            scope={"level": None, "object_id": None},
            metrics={},
            missing_signals=FEEDBACK_UNAVAILABLE_SIGNALS,
            suggestions=["Use a recommended tool with a concrete ad, campaign, ad set, account, or Page id."],
            extra={
                "available_signals": available_signals,
                "unavailable_signals": FEEDBACK_UNAVAILABLE_SIGNALS,
                "recommended_tools": recommended_tools,
            },
        )

    if ad_id:
        if any([level, object_id, account_id, campaign_id, adset_id]):
            raise ValidationError("When using ad_id, do not provide other scope arguments.")
        resolved_level, resolved_object_id = "ad", ad_id
    else:
        resolved_level, resolved_object_id = _resolve_scope(
            allowed_levels=("account", "campaign", "adset", "ad"),
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
        fields=AD_QUALITY_FIELDS,
    )
    ranked_by_spend = rank_rows(rows, "spend")[:top_n]
    weak_rows = _weak_quality_rows(rows, top_n)
    return analysis_response(
        scope={"level": resolved_level, "object_id": resolved_object_id},
        metrics=_aggregate_metrics(rows),
        findings=_quality_findings(weak_rows),
        missing_signals=FEEDBACK_UNAVAILABLE_SIGNALS,
        extra={
            "ads_by_spend": ranked_by_spend,
            "weak_quality_ads": weak_rows,
            "quality_fields": list(QUALITY_RANKING_FIELDS),
            "window": _window_descriptor(date_preset, since, until),
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
    return _snapshot_analysis(
        scope={"level": resolved_level, "object_id": resolved_object_id},
        metrics=metrics,
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
