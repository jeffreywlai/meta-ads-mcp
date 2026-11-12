"""Reporting and troubleshooting tools: opportunity score, trends, anomalies, rankings, context, delivery errors."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.diagnostics import build_finding, metric_evidence
from meta_ads_mcp.errors import UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.money import from_minor_units, normalize_currency
from meta_ads_mcp.normalize import blank_to_none, to_float
from meta_ads_mcp.schemas import analysis_response
from meta_ads_mcp.tool_types import StringList
from meta_ads_mcp.tools import diagnostics as diagnostics_tools
from meta_ads_mcp.tools import recommendations as recommendation_tools
from meta_ads_mcp.tools.insights import get_entity_insights

OPPORTUNITY_SCORE_ACCOUNT_FIELDS = ["id", "name", "currency", "opportunity_score"]
OPPORTUNITY_SCORE_HISTORY_MAX_DAYS = 45
OPPORTUNITY_SCORE_HISTORY_NOTE = (
    "Meta documents a 45-day maximum history window and says the most recent two days may be missing."
)


REPORTING_LEVELS = ("account", "campaign", "adset", "ad")
DAILY_INSIGHTS_FIELDS = [
    "date_start",
    "date_stop",
    "spend",
    "impressions",
    "clicks",
    "actions",
    "action_values",
]
MAX_DAILY_WINDOW_DAYS = 366
TREND_DEFAULT_METRICS = ("cpc", "cpm", "cost_per_result", "roas", "ctr", "conversion_rate")
METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "spend": {"formula": "sum of Meta spend", "better": "neutral"},
    "impressions": {"formula": "sum of Meta impressions", "better": "neutral"},
    "clicks": {"formula": "sum of Meta clicks (all clicks)", "better": "neutral"},
    "results": {"formula": "sum of results", "better": "higher"},
    "ctr": {"formula": "clicks / impressions", "better": "higher"},
    "cpc": {"formula": "spend / clicks", "better": "lower"},
    "cpm": {"formula": "spend / impressions * 1000", "better": "lower"},
    "cost_per_result": {"formula": "spend / results", "better": "lower"},
    "conversion_rate": {"formula": "results / clicks", "better": "higher"},
    "roas": {"formula": "conversion_value / spend", "better": "higher"},
}
RESULTS_DEFINITION = (
    "results are the first purchase or lead action Meta reports for the row (purchase, omni_purchase, "
    "offsite_conversion.purchase, offsite_conversion.fb_pixel_purchase, onsite_conversion.purchase, lead, "
    "onsite_conversion.lead, offsite_conversion.fb_pixel_lead); conversion_value is the matching purchase value. "
    "A day with no such action counts as zero once any day in the window reports one. "
    "These are not necessarily the result Meta optimizes each ad set for."
)
TREND_DEFAULT_WINDOW_DAYS = {"day": 30, "week": 84, "month": 180}


def _today() -> date:
    """Return today's date; isolated so tests can pin it."""
    return date.today()


def _require_account_id(account_id: str | None) -> str:
    """Normalize a required ad account id before any network call."""
    value = blank_to_none(account_id)
    if value is None:
        raise ValidationError("account_id is required.")
    return normalize_account_id(value)


def _number(value: Any) -> int | float | None:
    """Parse Meta numeric strings, keeping whole numbers as ints."""
    numeric = to_float(value)
    if numeric is None:
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _supported_currency(currency: Any) -> str | None:
    """Return a Meta-supported currency code or None."""
    try:
        return normalize_currency(currency)
    except ValidationError:
        return None


def _convert_history_budgets(changelog: Any, currency: str | None) -> Any:
    """Convert changelog budget minor units into major units of the account currency."""
    if not isinstance(changelog, list) or currency is None:
        return changelog
    converted = []
    for entry in changelog:
        if not isinstance(entry, dict):
            converted.append(entry)
            continue
        entry = dict(entry)
        details = entry.get("campaign_details")
        if isinstance(details, list):
            new_details = []
            for detail in details:
                if isinstance(detail, dict):
                    detail = dict(detail)
                    for key in ("budget_then", "budget_now"):
                        if key in detail:
                            detail[key] = from_minor_units(detail[key], currency, field_name=key)
                new_details.append(detail)
            entry["campaign_details"] = new_details
        converted.append(entry)
    return converted


def _history_summary(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize score history without inferring causes."""
    scores = [point["opportunity_score"] for point in points if point["opportunity_score"] is not None]
    if not scores:
        return {"days": len(points), "first": None, "last": None, "change": None, "min": None, "max": None}
    return {
        "days": len(points),
        "first": scores[0],
        "last": scores[-1],
        "change": scores[-1] - scores[0],
        "min": min(scores),
        "max": max(scores),
    }


@mcp_server.tool()
async def get_opportunity_score(
    account_id: str,
    include_history: bool = True,
    history_days: int = 14,
    include_recommendations: bool = True,
    top_n: int = 10,
    include_history_reasons: bool = False,
) -> dict[str, Any]:
    """Read the ad account's 0-100 opportunity score, its daily history (up to 45 days), and Meta's recommendations ranked by estimated score gain. include_history_reasons adds Meta's per-day changelog."""
    resolved_account_id = _require_account_id(account_id)
    if not 1 <= history_days <= OPPORTUNITY_SCORE_HISTORY_MAX_DAYS:
        raise ValidationError(f"history_days must be between 1 and {OPPORTUNITY_SCORE_HISTORY_MAX_DAYS}.")
    if top_n < 1:
        raise ValidationError("top_n must be at least 1.")

    client = get_graph_api_client()
    account = await client.get_object(resolved_account_id, fields=OPPORTUNITY_SCORE_ACCOUNT_FIELDS)
    missing_signals: list[str] = []
    score = _number(account.get("opportunity_score"))
    if score is None:
        missing_signals.append(
            "opportunity_score: Meta did not return a score for this account (Meta omits it when no score is available)."
        )
    currency = _supported_currency(account.get("currency"))

    history: dict[str, Any] | None = None
    if include_history:
        to_date = _today()
        from_date = to_date - timedelta(days=history_days - 1)
        params: dict[str, Any] = {"from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
        if include_history_reasons:
            params["get_reason"] = True
        window = {"from_date": params["from_date"], "to_date": params["to_date"]}
        try:
            payload = await client.list_objects(resolved_account_id, "opportunity_score_history", params=params)
        except UnsupportedFeatureError as exc:
            history = {"available": False, "window": window, "reason": str(exc), "points": []}
            missing_signals.append(
                "opportunity_score_history: Meta rejected the history edge for this account or API version."
            )
        else:
            points = []
            for raw in payload.get("data", []) or []:
                if not isinstance(raw, dict):
                    continue
                point: dict[str, Any] = {
                    "date": raw.get("date"),
                    "opportunity_score": _number(raw.get("opportunity_score")),
                }
                if include_history_reasons and "changelog" in raw:
                    point["changelog"] = _convert_history_budgets(raw.get("changelog"), currency)
                points.append(point)
            points.sort(key=lambda point: str(point.get("date") or ""))
            history = {
                "available": True,
                "window": window,
                "points": points,
                "summary": _history_summary(points),
                "note": OPPORTUNITY_SCORE_HISTORY_NOTE,
            }
            if include_history_reasons:
                history["budget_units"] = (
                    f"major units of {currency}" if currency else "Meta minor units (account currency unknown)"
                )

    top_recommendations: dict[str, Any] | None = None
    if include_recommendations:
        collection = await recommendation_tools._recommendation_collection(account_id=resolved_account_id)
        if not collection.get("supported", True):
            missing_signals.append("recommendations: Meta's recommendations edge is unavailable for this account.")
            top_recommendations = {"supported": False, "reason": collection.get("reason"), "items": []}
        else:
            items = []
            for item in collection.get("items", []):
                ranked = dict(item)
                ranked["estimated_score_gain"] = to_float(item.get("opportunity_score_lift"))
                items.append(ranked)
            items.sort(key=lambda item: (item["estimated_score_gain"] is None, -(item["estimated_score_gain"] or 0.0)))
            top_recommendations = {
                "supported": True,
                "items": items[:top_n],
                "returned_count": min(len(items), top_n),
                "available_count": len(items),
                "complete": bool(collection.get("complete", True)),
                "ranked_by": "estimated_score_gain_desc",
                "note": "estimated_score_gain is Meta's opportunity_score_lift: the points Meta expects the score to rise if the recommendation is applied.",
            }

    return analysis_response(
        scope={"level": "account", "object_id": resolved_account_id},
        metrics={"opportunity_score": score},
        missing_signals=missing_signals,
        extra={
            "account_name": account.get("name"),
            "opportunity_score": score,
            "score_scale": "0-100; Meta's measure of how optimized the account's campaigns, ad sets and ads are.",
            "source": "meta",
            "history": history,
            "top_recommendations": top_recommendations,
        },
    )


def _require_level(level: str | None, allowed: tuple[str, ...]) -> str:
    """Validate a reporting level before any network call."""
    normalized = (level or "").strip().lower()
    if normalized not in allowed:
        raise ValidationError(f"level must be one of {list(allowed)}.")
    return normalized


def _require_object_id(level: str, object_id: str | None) -> str:
    """Validate the object id and add the act_ prefix for accounts."""
    value = blank_to_none(object_id)
    if value is None:
        raise ValidationError("object_id is required.")
    return normalize_account_id(value) if level == "account" else value


def _parse_date(value: str, *, field: str) -> date:
    """Parse a YYYY-MM-DD date or raise a ValidationError."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} must be a valid ISO date in YYYY-MM-DD format.") from exc


def _resolve_window(since: str | None, until: str | None, *, default_days: int) -> tuple[date, date]:
    """Resolve an explicit or default trailing window ending yesterday."""
    since = blank_to_none(since)
    until = blank_to_none(until)
    if bool(since) != bool(until):
        raise ValidationError("Provide both since and until, or neither.")
    if since and until:
        start = _parse_date(since, field="since")
        end = _parse_date(until, field="until")
        if end < start:
            raise ValidationError("until must be on or after since.")
    else:
        end = _today() - timedelta(days=1)
        start = end - timedelta(days=default_days - 1)
    if (end - start).days + 1 > MAX_DAILY_WINDOW_DAYS:
        raise ValidationError(f"The date window may cover at most {MAX_DAILY_WINDOW_DAYS} days.")
    return start, end


def _normalize_metric_names(metrics: list[str] | None, default: tuple[str, ...]) -> list[str]:
    """Validate requested metric names against the documented derivations."""
    if metrics is None:
        return list(default)
    names: list[str] = []
    for raw in metrics:
        name = str(raw).strip().lower()
        if not name:
            continue
        if name not in METRIC_DEFINITIONS:
            raise ValidationError(f"Unknown metric {raw!r}. Supported metrics: {sorted(METRIC_DEFINITIONS)}.")
        if name not in names:
            names.append(name)
    if not names:
        raise ValidationError("metrics must name at least one metric.")
    return names


async def _daily_rows(level: str, object_id: str, start: date, end: date) -> list[dict[str, Any]]:
    """Fetch every daily insights row for one object across the window."""
    payload = await get_entity_insights(
        level=level,
        object_id=object_id,
        since=start.isoformat(),
        until=end.isoformat(),
        fields=DAILY_INSIGHTS_FIELDS,
        time_increment=1,
        limit=500,
        fetch_all=True,
    )
    rows = [row for row in payload.get("items", []) if row.get("date_start")]
    rows.sort(key=lambda row: str(row["date_start"]))
    return rows


_INPUT_SOURCES = {
    "spend": "spend",
    "impressions": "impressions",
    "clicks": "clicks",
    "results": "conversions",
    "conversion_value": "conversion_value",
}


def _known_inputs(rows: list[dict[str, Any]]) -> set[str]:
    """Return the additive inputs that at least one row reports."""
    known: set[str] = set()
    for row in rows:
        metrics = row.get("metrics") or {}
        for name, source in _INPUT_SOURCES.items():
            if to_float(metrics.get(source)) is not None:
                known.add(name)
    return known


def _period_inputs(rows: list[dict[str, Any]], known: set[str]) -> dict[str, float | None]:
    """Sum additive inputs; inputs no row in the window reports stay unknown."""
    totals: dict[str, float | None] = {}
    for name, source in _INPUT_SOURCES.items():
        if name not in known:
            totals[name] = None
            continue
        totals[name] = sum(to_float((row.get("metrics") or {}).get(source)) or 0.0 for row in rows)
    return totals


def _ratio(numerator: float | None, denominator: float | None, scale: float = 1.0) -> float | None:
    """Divide only when both values are known and the denominator is nonzero."""
    if numerator is None or not denominator:
        return None
    return numerator / denominator * scale


def _derived_metrics(inputs: dict[str, float | None]) -> dict[str, float | None]:
    """Derive every supported metric from additive inputs."""
    spend = inputs["spend"]
    impressions = inputs["impressions"]
    clicks = inputs["clicks"]
    results = inputs["results"]
    return {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "results": results,
        "ctr": _ratio(clicks, impressions),
        "cpc": _ratio(spend, clicks),
        "cpm": _ratio(spend, impressions, 1000.0),
        "cost_per_result": _ratio(spend, results),
        "conversion_rate": _ratio(results, clicks),
        "roas": _ratio(inputs["conversion_value"], spend),
    }


def _period_bounds(day: date, granularity: str) -> tuple[date, date]:
    """Return the calendar day, Monday-start week, or month containing day."""
    if granularity == "day":
        return day, day
    if granularity == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    start = day.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month - timedelta(days=1)


def _assessment(metric: str, direction: str | None) -> str | None:
    """Translate a direction into better/worse using the metric's documented preference."""
    if direction is None:
        return None
    if direction == "unchanged":
        return "unchanged"
    better = METRIC_DEFINITIONS[metric]["better"]
    if better == "neutral":
        return "neutral"
    improved = (direction == "up") == (better == "higher")
    return "better" if improved else "worse"


def _first_to_last(metric: str, periods: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the first and last periods that have a value for the metric."""
    valued = [period for period in periods if period["metrics"].get(metric) is not None]
    if not valued:
        return {
            "first": None, "last": None, "first_period": None, "last_period": None,
            "change": None, "pct_change": None, "direction": None, "assessment": None,
            "periods_with_value": 0, "first_period_partial": None, "last_period_partial": None,
        }
    first_period, last_period = valued[0], valued[-1]
    first = first_period["metrics"][metric]
    last = last_period["metrics"][metric]
    change = last - first
    direction = "up" if change > 0 else "down" if change < 0 else "unchanged"
    return {
        "first": first,
        "last": last,
        "first_period": first_period["period_start"],
        "last_period": last_period["period_start"],
        "change": change,
        "pct_change": (change / first) if first else None,
        "direction": direction,
        "assessment": _assessment(metric, direction),
        "periods_with_value": len(valued),
        "first_period_partial": first_period["partial"],
        "last_period_partial": last_period["partial"],
    }


@mcp_server.tool()
async def get_performance_trend(
    level: str,
    object_id: str,
    metrics: StringList | None = None,
    since: str | None = None,
    until: str | None = None,
    granularity: str = "day",
) -> dict[str, Any]:
    """Trend CPC, CPM, cost per result, ROAS, CTR and conversion rate for one account, campaign, ad set or ad by day, week (Monday start) or month, with first-to-last change and direction. Periods are summed from daily rows, then divided; zero denominators give null. Default window ends yesterday (30/84/180 days)."""
    resolved_level = _require_level(level, REPORTING_LEVELS)
    resolved_object_id = _require_object_id(resolved_level, object_id)
    resolved_granularity = (granularity or "").strip().lower()
    if resolved_granularity not in TREND_DEFAULT_WINDOW_DAYS:
        raise ValidationError("granularity must be day, week or month.")
    metric_names = _normalize_metric_names(metrics, TREND_DEFAULT_METRICS)
    start, end = _resolve_window(since, until, default_days=TREND_DEFAULT_WINDOW_DAYS[resolved_granularity])

    rows = await _daily_rows(resolved_level, resolved_object_id, start, end)
    known = _known_inputs(rows)
    buckets: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        day = _parse_date(str(row["date_start"]), field="date_start")
        buckets.setdefault(_period_bounds(day, resolved_granularity)[0], []).append(row)

    periods: list[dict[str, Any]] = []
    for period_start in sorted(buckets):
        _, period_end = _period_bounds(period_start, resolved_granularity)
        inputs = _period_inputs(buckets[period_start], known)
        derived = _derived_metrics(inputs)
        periods.append({
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "partial": period_start < start or period_end > end,
            "days_with_data": len(buckets[period_start]),
            "inputs": inputs,
            "metrics": {name: derived[name] for name in metric_names},
        })

    missing_signals: list[str] = []
    if not rows:
        missing_signals.append("Meta returned no insights rows for this object and window.")
    elif "results" not in known:
        missing_signals.append(
            "No purchase or lead actions were reported, so results, cost per result and conversion rate are null."
        )
    if rows and "conversion_value" not in known:
        missing_signals.append("No purchase value was reported, so ROAS is null.")

    return analysis_response(
        scope={"level": resolved_level, "object_id": resolved_object_id},
        metrics={},
        missing_signals=missing_signals,
        extra={
            "window": {"since": start.isoformat(), "until": end.isoformat()},
            "granularity": resolved_granularity,
            "metrics_requested": metric_names,
            "periods": periods,
            "changes": {name: _first_to_last(name, periods) for name in metric_names},
            "metric_definitions": {name: METRIC_DEFINITIONS[name] for name in metric_names},
            "results_definition": RESULTS_DEFINITION,
            "method": (
                "Daily rows are read with time_increment=1, additive inputs are summed per period, and each metric is "
                "divided from those sums. Direction compares the first and last periods with a value; partial periods "
                "are flagged and are not scaled."
            ),
            "daily_rows_returned": len(rows),
        },
    )


ANOMALY_DEFAULT_METRICS = ("spend", "cpc", "cpm", "ctr", "cost_per_result", "roas")
SENSITIVITY_MULTIPLIERS = {"low": 5.0, "medium": 3.5, "high": 2.5}
MAD_SCALE = 1.4826
MIN_SAME_WEEKDAY_POINTS = 4
MIN_BASELINE_VALUES = 7
MAX_LOOKBACK_DAYS = 31
MIN_BASELINE_DAYS = 7
MAX_BASELINE_DAYS = 90


def _median(values: list[float]) -> float:
    """Return the median of a non-empty list."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _median_and_mad(values: list[float]) -> tuple[float, float]:
    """Return the median and the median absolute deviation from it."""
    median = _median(values)
    return median, _median([abs(value - median) for value in values])


def _anomaly_rule(sensitivity: str, multiplier: float) -> dict[str, Any]:
    """Describe the exact flagging rule applied."""
    return {
        "description": (
            "A day is flagged when |value - baseline median| is greater than multiplier x 1.4826 x the baseline's "
            "median absolute deviation (MAD). The baseline is the baseline_days days just before the lookback window. "
            f"When the baseline has at least {MIN_SAME_WEEKDAY_POINTS} values on the same weekday as the day examined "
            "and those values vary, only same-weekday values are used; otherwise all baseline days are used. "
            "Days are not scored when the baseline has no variation (MAD of 0)."
        ),
        "sensitivity": sensitivity,
        "multiplier": multiplier,
        "sensitivity_multipliers": dict(SENSITIVITY_MULTIPLIERS),
        "mad_scale": MAD_SCALE,
        "min_same_weekday_points": MIN_SAME_WEEKDAY_POINTS,
        "min_baseline_values_per_metric": MIN_BASELINE_VALUES,
        "baseline_coverage_required": "a daily insights row for every baseline day",
        "severity_policy": "high when |robust_score| is at least twice the multiplier, otherwise medium",
        "note": (
            "This is a rule-based screen against the object's own recent history. It is not a statistical test "
            "and gives no probability or confidence."
        ),
    }


@mcp_server.tool()
async def detect_performance_anomalies(
    level: str,
    object_id: str,
    metrics: StringList | None = None,
    lookback_days: int = 7,
    baseline_days: int = 28,
    sensitivity: str = "medium",
    end_date: str | None = None,
) -> dict[str, Any]:
    """Flag days in the last lookback_days whose spend, CPC, CPM, CTR, cost per result or ROAS is far from the object's own baseline (the baseline_days before), using median and median absolute deviation, with same-weekday baselines when four or more exist. Returns the rule, baseline values and thresholds. Rule-based screen, not a statistical test. end_date defaults to yesterday."""
    resolved_level = _require_level(level, REPORTING_LEVELS)
    resolved_object_id = _require_object_id(resolved_level, object_id)
    metric_names = _normalize_metric_names(metrics, ANOMALY_DEFAULT_METRICS)
    if not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValidationError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}.")
    if not MIN_BASELINE_DAYS <= baseline_days <= MAX_BASELINE_DAYS:
        raise ValidationError(f"baseline_days must be between {MIN_BASELINE_DAYS} and {MAX_BASELINE_DAYS}.")
    resolved_sensitivity = (sensitivity or "").strip().lower()
    if resolved_sensitivity not in SENSITIVITY_MULTIPLIERS:
        raise ValidationError(f"sensitivity must be one of {list(SENSITIVITY_MULTIPLIERS)}.")
    multiplier = SENSITIVITY_MULTIPLIERS[resolved_sensitivity]
    end_text = blank_to_none(end_date)
    end = _parse_date(end_text, field="end_date") if end_text else _today() - timedelta(days=1)
    lookback_start = end - timedelta(days=lookback_days - 1)
    baseline_end = lookback_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=baseline_days - 1)

    rows = await _daily_rows(resolved_level, resolved_object_id, baseline_start, end)
    known = _known_inputs(rows)
    daily: dict[date, dict[str, float | None]] = {}
    for row in rows:
        day = _parse_date(str(row["date_start"]), field="date_start")
        daily[day] = _derived_metrics(_period_inputs([row], known))
    baseline_dates = sorted(day for day in daily if baseline_start <= day <= baseline_end)
    lookback_dates = [lookback_start + timedelta(days=offset) for offset in range(lookback_days)]
    rule = _anomaly_rule(resolved_sensitivity, multiplier)
    scope = {"level": resolved_level, "object_id": resolved_object_id}
    windows = {
        "baseline_window": {"since": baseline_start.isoformat(), "until": baseline_end.isoformat()},
        "lookback_window": {"since": lookback_start.isoformat(), "until": end.isoformat()},
    }

    if len(baseline_dates) < baseline_days:
        return analysis_response(
            scope=scope,
            metrics={},
            findings=[{
                **build_finding(
                    "insufficient_data",
                    f"Only {len(baseline_dates)} of {baseline_days} baseline days have data, so no day was judged.",
                    severity="low",
                    confidence=None,
                    next_actions=["Use a shorter baseline_days or an object with a longer delivery history."],
                ),
                "confidence_status": "uncalibrated",
            }],
            missing_signals=[
                f"Not enough history: {len(baseline_dates)} of {baseline_days} baseline days returned insights rows."
            ],
            extra={
                **windows,
                "rule": rule,
                "anomalies": [],
                "metric_status": {},
                "baseline_rows": len(baseline_dates),
                "lookback_rows": sum(1 for day in lookback_dates if day in daily),
                "results_definition": RESULTS_DEFINITION,
            },
        )

    anomalies: list[dict[str, Any]] = []
    metric_status: dict[str, dict[str, Any]] = {}
    missing_signals: list[str] = []
    for metric in metric_names:
        baseline_points = [(day, daily[day][metric]) for day in baseline_dates if daily[day][metric] is not None]
        status: dict[str, Any] = {
            "baseline_values_count": len(baseline_points),
            "days_examined": 0,
            "days_without_data": 0,
            "days_with_no_baseline_variation": 0,
            "days_differing_from_flat_baseline": 0,
            "baseline_types_used": [],
            "flagged_days": 0,
        }
        metric_status[metric] = status
        if len(baseline_points) < MIN_BASELINE_VALUES:
            status["status"] = "not_enough_values"
            missing_signals.append(
                f"{metric}: only {len(baseline_points)} baseline days have a value; at least {MIN_BASELINE_VALUES} are needed."
            )
            continue
        all_values = [value for _, value in baseline_points]
        overall_median, overall_mad = _median_and_mad(all_values)
        status.update({"baseline_median": overall_median, "baseline_mad": overall_mad})
        for day in lookback_dates:
            value = daily.get(day, {}).get(metric)
            if value is None:
                status["days_without_data"] += 1
                continue
            status["days_examined"] += 1
            candidates: list[tuple[str, list[tuple[date, float]]]] = []
            same_weekday = [(point_day, point) for point_day, point in baseline_points if point_day.weekday() == day.weekday()]
            if len(same_weekday) >= MIN_SAME_WEEKDAY_POINTS:
                candidates.append(("same_weekday", same_weekday))
            candidates.append(("all_days", baseline_points))
            chosen: tuple[str, list[tuple[date, float]], float, float] | None = None
            for baseline_type, points in candidates:
                median, mad = _median_and_mad([point for _, point in points])
                if mad > 0:
                    chosen = (baseline_type, points, median, mad)
                    break
            if chosen is None:
                status["days_with_no_baseline_variation"] += 1
                if value != overall_median:
                    status["days_differing_from_flat_baseline"] += 1
                continue
            baseline_type, points, median, mad = chosen
            if baseline_type not in status["baseline_types_used"]:
                status["baseline_types_used"].append(baseline_type)
            scaled_mad = MAD_SCALE * mad
            robust_score = (value - median) / scaled_mad
            if abs(robust_score) <= multiplier:
                continue
            status["flagged_days"] += 1
            direction = "above" if robust_score > 0 else "below"
            anomalies.append({
                "day": day.isoformat(),
                "metric": metric,
                "value": value,
                "baseline_median": median,
                "baseline_mad": mad,
                "scaled_mad": scaled_mad,
                "threshold_multiplier": multiplier,
                "threshold_low": median - multiplier * scaled_mad,
                "threshold_high": median + multiplier * scaled_mad,
                "robust_score": robust_score,
                "direction": direction,
                "assessment": _assessment(metric, "up" if direction == "above" else "down"),
                "baseline_type": baseline_type,
                "baseline_points": len(points),
                "baseline_days": [point_day.isoformat() for point_day, _ in points],
                "baseline_values": [point for _, point in points],
            })
        if status["days_examined"] and status["days_with_no_baseline_variation"] == status["days_examined"]:
            status["status"] = "no_variation"
            missing_signals.append(
                f"{metric}: the baseline had no variation (median absolute deviation of 0), so days could not be scored; "
                f"{status['days_differing_from_flat_baseline']} examined day(s) differ from the flat baseline value."
            )
        elif status["days_examined"]:
            status["status"] = "analyzed"
        else:
            status["status"] = "no_lookback_data"
            missing_signals.append(f"{metric}: no lookback day has a value.")

    anomalies.sort(key=lambda anomaly: (-abs(anomaly["robust_score"]), anomaly["day"], anomaly["metric"]))
    findings: list[dict[str, Any]] = []
    for anomaly in anomalies:
        finding = build_finding(
            "performance_anomaly",
            (
                f"{anomaly['metric']} on {anomaly['day']} was {anomaly['value']:.4g}, {anomaly['direction']} the "
                f"baseline median {anomaly['baseline_median']:.4g} (flag range outside "
                f"{anomaly['threshold_low']:.4g} to {anomaly['threshold_high']:.4g})."
            ),
            severity="high" if abs(anomaly["robust_score"]) >= 2 * multiplier else "medium",
            confidence=None,
            evidence=[
                metric_evidence(
                    anomaly["metric"],
                    anomaly["value"],
                    "(value - baseline_median) / (1.4826 * baseline_mad)",
                    {
                        "day": anomaly["day"],
                        "baseline_median": anomaly["baseline_median"],
                        "baseline_mad": anomaly["baseline_mad"],
                        "robust_score": anomaly["robust_score"],
                        "threshold_multiplier": multiplier,
                        "threshold_low": anomaly["threshold_low"],
                        "threshold_high": anomaly["threshold_high"],
                        "baseline_type": anomaly["baseline_type"],
                        "baseline_values": anomaly["baseline_values"],
                    },
                )
            ],
            affected_entities=[dict(scope)],
            next_actions=[
                "Check the change history and delivery settings for that day before acting.",
                "Compare with get_performance_trend to see whether the change persisted.",
            ],
        )
        finding["confidence_status"] = "uncalibrated"
        findings.append(finding)
    if not findings:
        findings.append({
            **build_finding(
                "no_anomalies_detected",
                "No examined day fell outside the flag range for the selected metrics.",
                severity="low",
                confidence=None,
            ),
            "confidence_status": "uncalibrated",
        })

    return analysis_response(
        scope=scope,
        metrics={},
        findings=findings,
        missing_signals=missing_signals,
        extra={
            **windows,
            "rule": rule,
            "anomalies": anomalies,
            "metric_status": metric_status,
            "baseline_rows": len(baseline_dates),
            "lookback_rows": sum(1 for day in lookback_dates if day in daily),
            "metric_definitions": {name: METRIC_DEFINITIONS[name] for name in metric_names},
            "results_definition": RESULTS_DEFINITION,
        },
    )


RANKING_FIELDS = ("quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking")
RANKING_POINTS = {
    "ABOVE_AVERAGE": 4,
    "AVERAGE": 3,
    "BELOW_AVERAGE_35": 2,
    "BELOW_AVERAGE_20": 1,
    "BELOW_AVERAGE_10": 0,
}
BELOW_AVERAGE_POINTS = 2
COST_RATIO_THRESHOLD = 1.5
RANKING_REPORT_LEVELS = ("account", "campaign", "adset", "ad")
RANKING_RULES = {
    "ranking_values": {
        "ABOVE_AVERAGE": "above average",
        "AVERAGE": "average",
        "BELOW_AVERAGE_35": "below average, bottom 35% of ads competing for the same audience",
        "BELOW_AVERAGE_20": "below average, bottom 20%",
        "BELOW_AVERAGE_10": "below average, bottom 10%",
        "UNKNOWN": "Meta had too little data to rank the ad",
    },
    "creative_quality": "quality_ranking or engagement_rate_ranking is below average.",
    "conversion_path": "only conversion_rate_ranking is below average; quality and engagement are average or above.",
    "bid_and_budget": (
        "all three rankings are average or above, and either cost per result is at least cost_ratio_threshold times "
        "the scope's cost per result, or the ad has no results after spending at least one scope cost per result."
    ),
    "cost_ratio_threshold": COST_RATIO_THRESHOLD,
    "weak_ad_order": "spend descending",
    "ranked_ad_order": "ranked ads first by total ranking points ascending (above average 4 ... bottom 10% 0), then spend descending; ads with any UNKNOWN ranking last",
    "note": (
        "Meta's rankings compare each ad with ads that competed for the same audience. They are the closest "
        "available comparison with other advertisers. The diagnosis says where the evidence points; it is a rule, "
        "not a measured cause."
    ),
}


def _ranking_diagnosis(
    rankings: dict[str, str | None],
    metrics: dict[str, float | None],
    scope_metrics: dict[str, float | None],
) -> tuple[str | None, str | None, list[str]]:
    """Apply the ranking rule to one ad and return (diagnosis, reason, below-average fields)."""
    points = {field: RANKING_POINTS.get(str(value or "").upper()) for field, value in rankings.items()}
    below = [field for field in RANKING_FIELDS if points[field] is not None and points[field] <= BELOW_AVERAGE_POINTS]
    if "quality_ranking" in below or "engagement_rate_ranking" in below:
        return (
            "creative_quality",
            "Meta ranks this ad's quality or expected engagement below ads competing for the same audience, which "
            "points at the creative.",
            below,
        )
    if below == ["conversion_rate_ranking"]:
        return (
            "conversion_path",
            "Only the conversion-rate ranking is below average, which points at what happens after the click "
            "(offer, landing page or audience intent) rather than the creative or the bid.",
            below,
        )
    if any(value is None for value in points.values()):
        return None, None, below
    cost_ratio = _ratio(metrics["cost_per_result"], scope_metrics["cost_per_result"])
    if cost_ratio is not None and cost_ratio >= COST_RATIO_THRESHOLD:
        return (
            "bid_and_budget",
            f"All three rankings are average or above, yet cost per result is {cost_ratio:.2f} times the scope's. "
            "With relevance in line with competing ads, the higher cost points at auction competition, bid or budget.",
            below,
        )
    scope_cost = scope_metrics["cost_per_result"]
    if (metrics["results"] or 0) == 0 and scope_cost and (metrics["spend"] or 0) >= scope_cost:
        return (
            "bid_and_budget",
            "All three rankings are average or above, yet the ad has no results after spending at least one scope "
            "cost per result, which points at auction competition, bid or budget.",
            below,
        )
    return None, None, below


@mcp_server.tool()
async def get_auction_ranking_report(
    level: str,
    object_id: str,
    date_preset: str | None = "last_30d",
    since: str | None = None,
    until: str | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Rank the ads under an account, campaign, ad set (or one ad) by Meta's quality, engagement-rate and conversion-rate rankings with spend and results, and say for each weak ad whether the evidence points at creative quality, the post-click path, or bid and budget. Rankings compare ads competing for the same audience."""
    resolved_level = _require_level(level, RANKING_REPORT_LEVELS)
    resolved_object_id = _require_object_id(resolved_level, object_id)
    if top_n < 1:
        raise ValidationError("top_n must be at least 1.")
    window = diagnostics_tools._window_kwargs(
        date_preset=date_preset, since=since, until=until, default_date_preset="last_30d",
    )
    rows = await diagnostics_tools._child_insights(
        resolved_object_id,
        level="ad",
        fields=diagnostics_tools.AD_QUALITY_FIELDS,
        **window,
    )
    known = _known_inputs(rows)
    scope_metrics = _derived_metrics(_period_inputs(rows, known))
    ads: list[dict[str, Any]] = []
    for row in rows:
        metrics = _derived_metrics(_period_inputs([row], known))
        rankings = {field: row.get(field) for field in RANKING_FIELDS}
        diagnosis, reason, below = _ranking_diagnosis(rankings, metrics, scope_metrics)
        points = [RANKING_POINTS.get(str(value or "").upper()) for value in rankings.values()]
        ads.append({
            "ad_id": str(row.get("ad_id") or row.get("id") or "") or None,
            "ad_name": row.get("ad_name"),
            "campaign_id": row.get("campaign_id"),
            "adset_id": row.get("adset_id"),
            "rankings": rankings,
            "ranking_points": None if None in points else sum(points),
            "below_average_rankings": below,
            "spend": metrics["spend"],
            "impressions": metrics["impressions"],
            "results": metrics["results"],
            "cost_per_result": metrics["cost_per_result"],
            "cpm": metrics["cpm"],
            "ctr": metrics["ctr"],
            "cost_per_result_vs_scope": _ratio(metrics["cost_per_result"], scope_metrics["cost_per_result"]),
            "cpm_vs_scope": _ratio(metrics["cpm"], scope_metrics["cpm"]),
            "diagnosis": diagnosis,
            "reason": reason,
        })

    ranked = sorted(
        ads,
        key=lambda ad: (ad["ranking_points"] is None, ad["ranking_points"] or 0, -(ad["spend"] or 0.0), ad["ad_id"] or ""),
    )
    weak = sorted(
        (ad for ad in ads if ad["diagnosis"]),
        key=lambda ad: (-(ad["spend"] or 0.0), ad["ad_id"] or ""),
    )
    findings: list[dict[str, Any]] = []
    for ad in weak[:top_n]:
        finding = build_finding(
            "weak_auction_ranking",
            f"Ad {ad['ad_id']}: {ad['reason']}",
            severity="high" if ad["diagnosis"] == "creative_quality" and any(
                str(ad["rankings"][field] or "").upper() == "BELOW_AVERAGE_10" for field in RANKING_FIELDS
            ) else "medium",
            confidence=None,
            evidence=[
                metric_evidence(
                    "rankings", None, "Meta-reported ranking fields",
                    {field: ad["rankings"][field] for field in RANKING_FIELDS},
                ),
                metric_evidence(
                    "cost_per_result_vs_scope",
                    ad["cost_per_result_vs_scope"],
                    "ad cost_per_result / scope cost_per_result",
                    {"ad_cost_per_result": ad["cost_per_result"], "scope_cost_per_result": scope_metrics["cost_per_result"]},
                ),
                metric_evidence(
                    "cpm_vs_scope",
                    ad["cpm_vs_scope"],
                    "ad cpm / scope cpm",
                    {"ad_cpm": ad["cpm"], "scope_cpm": scope_metrics["cpm"], "ad_spend": ad["spend"]},
                ),
            ],
            affected_entities=[{"ad_id": ad["ad_id"], "ad_name": ad["ad_name"], "diagnosis": ad["diagnosis"]}],
            next_actions={
                "creative_quality": ["Test new creative against this ad before shifting budget."],
                "conversion_path": ["Check the landing page, offer and audience intent for this ad."],
                "bid_and_budget": ["Review bid strategy, cost controls and budget for the ad's ad set."],
            }[ad["diagnosis"]],
        )
        finding["confidence_status"] = "uncalibrated"
        findings.append(finding)
    if not findings:
        has_ranked = any(ad["ranking_points"] is not None for ad in ads)
        findings.append({
            **build_finding(
                "no_weak_ads_detected" if has_ranked else "insufficient_data",
                (
                    "No ad matched the weak-ranking or high-cost rules."
                    if has_ranked
                    else "No ad in the window had all three Meta rankings."
                ),
                severity="low",
                confidence=None,
            ),
            "confidence_status": "uncalibrated",
        })

    return analysis_response(
        scope={"level": resolved_level, "object_id": resolved_object_id},
        metrics=scope_metrics,
        findings=findings,
        extra={
            "analyzed_level": "ad",
            "window": diagnostics_tools._window_descriptor(date_preset, since, until, default_date_preset="last_30d"),
            "ads_analyzed": len(ads),
            "scope_metrics": scope_metrics,
            "ranked_ads": ranked[:top_n],
            "weak_ads": weak[:top_n],
            "weak_ad_count": len(weak),
            "unranked_ad_count": sum(1 for ad in ads if ad["ranking_points"] is None),
            "rules": RANKING_RULES,
            "results_definition": RESULTS_DEFINITION,
        },
    )
