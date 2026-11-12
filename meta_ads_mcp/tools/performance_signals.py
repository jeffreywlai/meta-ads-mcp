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
