"""Reporting and troubleshooting tools: opportunity score, trends, anomalies, rankings, context, delivery errors."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.diagnostics import build_finding, metric_evidence
from meta_ads_mcp.errors import MetaApiError, NotFoundError, RateLimitError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.money import from_minor_units, normalize_currency
from meta_ads_mcp.normalize import blank_to_none, to_float
from meta_ads_mcp.pagination import extract_paging
from meta_ads_mcp.schemas import analysis_response
from meta_ads_mcp.tool_types import StringList, coerce_csv_string_list
from meta_ads_mcp.tools import diagnostics as diagnostics_tools
from meta_ads_mcp.tools import insights as insights_tools
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
# Results follow Meta's own result for each object: the Insights `results` field ("The number of times your
# ad achieved an outcome, based on the objective and settings you selected") and `cost_per_result`, both
# list<AdsInsightsResult> in Meta's Insights reference. Each entry names the result in `indicator` and carries
# its number in `values[].value`; extra attribution windows add more `values` entries, and the default
# window is the one used here. An indicator of the form `actions:<action_type>` is an action count.
RESULT_FIELD = "results"
COST_PER_RESULT_FIELD = "cost_per_result"
ACTION_INDICATOR_PREFIX = "actions:"
RESULTS_DEFINITION = (
    "results are Meta's own result for the object as the Insights API reports it in the `results` field: the "
    "outcome its objective and optimization settings count, named by Meta's indicator. No other action is "
    "substituted when Meta reports no result. When the indicator is an action (actions:<action_type>), results "
    "add up across days, cost_per_result is spend / results (Meta's cost_per_result for that result), "
    "conversion_rate is results / clicks, and conversion_value is the action_values value of that same action, "
    "so ROAS is null when Meta reports no value for it. Any other indicator (for example reach) does not add up "
    "across days: it is shown only for single-day periods, with Meta's own cost_per_result. When rows report "
    "different indicators, results are not added together and every result metric is null."
)
TREND_DEFAULT_WINDOW_DAYS = {"day": 30, "week": 84, "month": 180}


def _today() -> date:
    """Return the server's date, used only when the ad account's time zone cannot be read."""
    return date.today()


def _now() -> datetime:
    """Return the current instant in UTC; isolated so tests can pin it."""
    return datetime.now(timezone.utc)


async def _reporting_today(
    level: str, object_id: str, *, account: dict[str, Any] | None = None,
) -> tuple[date, dict[str, Any], str | None]:
    """Return today's date in the ad account's time zone, a descriptor of it, and a missing signal on fallback.

    An account scope needs one read of the account (none when the caller already has it); a campaign,
    ad set or ad scope reads the object's account_id and then the account.
    """
    error: str | None = None
    zone: ZoneInfo | None = None
    try:
        if account is None:
            client = get_graph_api_client()
            account_id = object_id
            if level != "account":
                owner = await client.get_object(object_id, fields=["account_id"])
                raw_account_id = blank_to_none(str(owner.get("account_id") or ""))
                if raw_account_id is None:
                    raise ValueError("the object did not report its account_id")
                account_id = normalize_account_id(raw_account_id)
            account = await client.get_object(account_id, fields=["timezone_name"])
        timezone_name = blank_to_none(str(account.get("timezone_name") or ""))
        if timezone_name is None:
            raise ValueError("the ad account did not report timezone_name")
        try:
            zone = ZoneInfo(timezone_name)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unrecognized time zone {timezone_name!r}") from exc
    except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
        error = str(_public_error(exc).get("message"))
    except ValueError as exc:
        error = str(exc)
    if zone is None:
        today = _today()
        return (
            today,
            {"timezone": None, "source": "server_date", "today": today.isoformat()},
            (
                f"The ad account's time zone could not be read ({error}), so default dates use the server's date "
                "and may be one day off the account's reporting day."
            ),
        )
    today = _now().astimezone(zone).date()
    return today, {"timezone": zone.key, "source": "ad_account", "today": today.isoformat()}, None


EXPLICIT_DATES_TIMEZONE = {
    "timezone": None,
    "source": "explicit_dates",
    "note": "The dates given were sent as is; Meta reports them in the ad account's time zone.",
}


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
        except (MetaApiError, UnsupportedFeatureError) as exc:
            history = {"available": False, "window": window, "points": None, "error": _public_error(exc)}
            missing_signals.append(
                "opportunity_score_history: Meta returned an error for the history edge, so history is unknown "
                f"({_public_error(exc).get('message')}). Meta documents this edge on Graph API v27.0."
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


def _explicit_window(since: str | None, until: str | None) -> tuple[date, date] | None:
    """Validate an explicit window before any network call; None means the default window applies."""
    since = blank_to_none(since)
    until = blank_to_none(until)
    if bool(since) != bool(until):
        raise ValidationError("Provide both since and until, or neither.")
    if not (since and until):
        return None
    start = _parse_date(since, field="since")
    end = _parse_date(until, field="until")
    if end < start:
        raise ValidationError("until must be on or after since.")
    if (end - start).days + 1 > MAX_DAILY_WINDOW_DAYS:
        raise ValidationError(f"The date window may cover at most {MAX_DAILY_WINDOW_DAYS} days.")
    return start, end


def _normalize_metric_names(metrics: list[str] | None, default: tuple[str, ...]) -> list[str]:
    """Validate requested metric names against the documented derivations."""
    if metrics is None:
        return list(default)
    names: list[str] = []
    for raw in coerce_csv_string_list(metrics):
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


INSIGHTS_PAGE_LIMIT = 500
INSIGHTS_MAX_ROWS = 1000
INSIGHTS_MAX_PAGES = 10
# Meta error codes after which a read is repeated without the result fields: invalid parameter (100) and
# Meta's unknown or service errors (1, 2), which is how a heavy insights request failed live. Permission,
# token and throttling errors are raised instead, since dropping two fields cannot fix them.
RESULT_RETRY_CODES = {1, 2, 100}


def _result_fields(level: str) -> list[str]:
    """Meta's result fields to request at a level.

    Meta's own Ads MCP server states that account-level requests do not support `cost_per_result`, because an
    account can mix result types, so it is only requested below account level.
    """
    return [RESULT_FIELD] if level == "account" else [RESULT_FIELD, COST_PER_RESULT_FIELD]


def _parse_result_list(value: Any) -> dict[str, Any]:
    """Read one of Meta's result fields: a list of {indicator, values: [{value, attribution_windows}]}.

    Returns a status: absent (no field or an empty list), read (one indicator; value None when Meta lists the
    indicator without a value), mixed (several indicators in one row), or unreadable (any other shape).
    """
    if value is None:
        return {"status": "absent"}
    if not isinstance(value, list):
        return {"status": "unreadable"}
    entries = [entry for entry in value if isinstance(entry, dict)]
    if len(entries) != len(value):
        return {"status": "unreadable"}
    if not entries:
        return {"status": "absent"}
    indicators = list(dict.fromkeys(str(entry.get("indicator") or "").strip() for entry in entries))
    if "" in indicators:
        return {"status": "unreadable"}
    if len(indicators) > 1:
        return {"status": "mixed", "indicators": indicators}
    values = entries[0].get("values")
    if values is None or values == []:
        return {"status": "read", "indicator": indicators[0], "value": None}
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        return {"status": "unreadable", "indicator": indicators[0]}
    chosen = next((item for item in values if "default" in (item.get("attribution_windows") or [])), values[0])
    number = to_float(chosen.get("value"))
    if number is None:
        return {"status": "unreadable", "indicator": indicators[0]}
    return {"status": "read", "indicator": indicators[0], "value": number}


def _attach_meta_results(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace Meta's raw result fields with parsed `meta_result` and `meta_cost_per_result` on each row.

    This runs before the shared row normalization, which derives its own purchase-or-lead `results`.
    """
    data = []
    for raw in payload.get("data", []) or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        result = _parse_result_list(row.pop(RESULT_FIELD, None))
        cost = _parse_result_list(row.pop(COST_PER_RESULT_FIELD, None))
        row["meta_result"] = result
        row["meta_cost_per_result"] = (
            cost["value"]
            if cost["status"] == "read" and result.get("status") == "read" and cost.get("indicator") == result.get("indicator")
            else None
        )
        data.append(row)
    return {**payload, "data": data}


def _normalize_with_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize insights rows, keeping Meta's own result alongside the shared derived metrics."""
    return insights_tools._normalize_rows(_attach_meta_results(payload))


async def _read_insights_pages(
    object_id: str, fields: list[str], params: dict[str, Any], *, max_rows: int = INSIGHTS_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Read every page of one insights request; fail clearly instead of returning a partial list.

    Pages are capped separately from rows, and a missing or repeated cursor stops the read with an error.
    """
    client = get_graph_api_client()
    rows: list[dict[str, Any]] = []
    page_params = dict(params)
    seen: set[str] = set()
    pages = 0
    while True:
        payload = await client.get_insights(object_id, fields=fields, params=page_params)
        pages += 1
        rows.extend(_normalize_with_results(payload))
        paging = extract_paging(payload)
        if not paging.get("next"):
            return rows
        if len(rows) >= max_rows or pages >= INSIGHTS_MAX_PAGES:
            raise ValidationError(
                f"Insights exceeded the {max_rows}-row scan limit; narrow the window or the number of objects. "
                "No partial result was returned."
            )
        cursor = paging.get("after")
        if not cursor or cursor in seen:
            raise ValidationError("Insights pagination could not finish: missing or repeated cursor.")
        seen.add(cursor)
        page_params = {**params, "after": cursor}


async def _read_with_results(
    object_id: str, fields: list[str], params: dict[str, Any], *, level: str, max_rows: int = INSIGHTS_MAX_ROWS,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read insights with Meta's result fields; if Meta rejects that request, read once more without them.

    Returns the rows and, when the second read was needed, Meta's error for the first. A failure of the
    second read is raised.
    """
    with_results = [*fields, *(field for field in _result_fields(level) if field not in fields)]
    try:
        return await _read_insights_pages(object_id, with_results, params, max_rows=max_rows), None
    except (MetaApiError, UnsupportedFeatureError) as exc:
        if isinstance(exc, MetaApiError) and exc.code not in RESULT_RETRY_CODES:
            raise
        error = str(_public_error(exc).get("message"))
    return await _read_insights_pages(object_id, fields, params, max_rows=max_rows), error


async def _daily_rows(
    level: str, object_id: str, start: date, end: date, *, extra_fields: list[str] | None = None,
    filtering: list[dict[str, Any]] | None = None, row_level: str | None = None, max_rows: int = INSIGHTS_MAX_ROWS,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch every daily insights row for one object across the window, with Meta's result.

    row_level reports rows for the object's children (for example campaigns under an account) instead of the
    object itself. Returns the rows sorted by day and the error that made the result fields unavailable, if any.
    """
    rows_level = row_level or level
    params = insights_tools._insights_params(
        level=rows_level, since=start.isoformat(), until=end.isoformat(), time_increment=1,
        limit=INSIGHTS_PAGE_LIMIT, filtering=filtering,
    )
    fields = [*DAILY_INSIGHTS_FIELDS, *(extra_fields or [])]
    rows, result_error = await _read_with_results(object_id, fields, params, level=rows_level, max_rows=max_rows)
    rows = [row for row in rows if row.get("date_start")]
    rows.sort(key=lambda row: str(row["date_start"]))
    return rows, result_error


# Two separate rules govern a value a row does not report:
# - Delivery inputs (spend, impressions, clicks) are reported by Meta on every row it returns. A row
#   that lacks one is missing data, so the period's total, and every ratio built on it, is unknown.
# - Result inputs (results, conversion value) are deliberately different: Meta omits a result that did not
#   happen, so once any row in the window reports the result, a row without it counts as zero.
_DELIVERY_INPUT_SOURCES = {
    "spend": "spend",
    "impressions": "impressions",
    "clicks": "clicks",
}
INPUT_POLICY = {
    "delivery_inputs": (
        "spend, impressions and clicks: when any row in a period lacks the value, the period's total is null and "
        "every metric that needs it (CPC, CPM, CTR, cost per result, ROAS, conversion rate) is null for that period."
    ),
    "action_inputs": (
        "results and conversion value: Meta omits a result that did not happen, so once any row in the window "
        "reports Meta's result, a row without it counts as zero; when no row reports it, it is null. A row whose "
        "result cannot be read makes its period's results null."
    ),
}


def _result_basis(rows: list[dict[str, Any]], *, result_error: str | None = None) -> dict[str, Any]:
    """Decide which result the rows report, and whether it can be added up across rows.

    Statuses: single (one indicator), mixed (several), not_reported (no row reports a result), unreadable (rows
    report a result in a shape that cannot be read), or request_failed (Meta rejected the result fields).
    """
    if result_error is not None:
        return {"status": "request_failed", "error": result_error}
    indicators: list[str] = []
    mixed = False
    unreadable = 0
    for row in rows:
        result = row.get("meta_result") or {"status": "absent"}
        if result["status"] == "read":
            if result["indicator"] not in indicators:
                indicators.append(result["indicator"])
        elif result["status"] == "mixed":
            mixed = True
            indicators.extend(item for item in result["indicators"] if item not in indicators)
        elif result["status"] == "unreadable":
            unreadable += 1
    if mixed or len(indicators) > 1:
        return {"status": "mixed", "indicators": indicators}
    if not indicators:
        return {"status": "unreadable"} if unreadable else {"status": "not_reported"}
    indicator = indicators[0]
    if indicator.startswith(ACTION_INDICATOR_PREFIX):
        action_type: str | None = indicator[len(ACTION_INDICATOR_PREFIX):] or None
    elif any(indicator in (row.get("actions_map") or {}) for row in rows):
        action_type = indicator
    else:
        action_type = None
    return {
        "status": "single",
        "indicator": indicator,
        "action_type": action_type,
        "additive": action_type is not None,
        "value_reported": action_type is not None
        and any(action_type in (row.get("action_values_map") or {}) for row in rows),
        "rows_unreadable": unreadable,
    }


def _row_result(row: dict[str, Any], basis: dict[str, Any]) -> float | None:
    """One row's count of the basis result: absent counts as zero, unreadable is unknown."""
    if basis.get("status") != "single":
        return None
    result = row.get("meta_result") or {"status": "absent"}
    if result["status"] == "absent":
        return 0.0
    if result["status"] == "read" and result["indicator"] == basis["indicator"]:
        return 0.0 if result["value"] is None else result["value"]
    return None


def _period_inputs(rows: list[dict[str, Any]], basis: dict[str, Any]) -> dict[str, float | None]:
    """Sum additive inputs under INPUT_POLICY for the rows of one period."""
    totals: dict[str, float | None] = {}
    for name, source in _DELIVERY_INPUT_SOURCES.items():
        values = [to_float((row.get("metrics") or {}).get(source)) for row in rows]
        totals[name] = None if not values or any(value is None for value in values) else sum(values)
    results: float | None = None
    conversion_value: float | None = None
    meta_cost: float | None = None
    if basis.get("status") == "single" and rows:
        counts = [_row_result(row, basis) for row in rows]
        if basis["additive"]:
            results = None if any(count is None for count in counts) else sum(counts)  # type: ignore[arg-type]
            if basis["value_reported"]:
                conversion_value = sum(
                    to_float((row.get("action_values_map") or {}).get(basis["action_type"])) or 0.0 for row in rows
                )
        elif len(rows) == 1:
            results = counts[0]
        if len(rows) == 1:
            meta_cost = to_float(rows[0].get("meta_cost_per_result"))
    totals["results"] = results
    totals["conversion_value"] = conversion_value
    totals["meta_cost_per_result"] = meta_cost
    return totals


def _missing_delivery_signal(periods: list[tuple[str, dict[str, float | None]]], unit: str) -> list[str]:
    """Describe periods whose delivery inputs were missing from Meta's rows."""
    affected: dict[str, list[str]] = {}
    for label, inputs in periods:
        for name in _DELIVERY_INPUT_SOURCES:
            if inputs.get(name) is None:
                affected.setdefault(name, []).append(label)
    return [
        f"{name}: {len(labels)} {unit}(s) had a row without {name} (first {labels[0]}), so metrics that need it are "
        "null there rather than computed from zero."
        for name, labels in affected.items()
    ]


def _ratio(numerator: float | None, denominator: float | None, scale: float = 1.0) -> float | None:
    """Divide only when both values are known and the denominator is nonzero."""
    if numerator is None or not denominator:
        return None
    return numerator / denominator * scale


def _derived_metrics(inputs: dict[str, float | None], basis: dict[str, Any]) -> dict[str, float | None]:
    """Derive every supported metric from additive inputs and the result basis."""
    spend = inputs["spend"]
    impressions = inputs["impressions"]
    clicks = inputs["clicks"]
    results = inputs["results"]
    additive = bool(basis.get("additive"))
    return {
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "results": results,
        "ctr": _ratio(clicks, impressions),
        "cpc": _ratio(spend, clicks),
        "cpm": _ratio(spend, impressions, 1000.0),
        "cost_per_result": _ratio(spend, results) if additive else inputs.get("meta_cost_per_result"),
        "conversion_rate": _ratio(results, clicks) if additive else None,
        "roas": _ratio(inputs["conversion_value"], spend),
    }


def _result_summary(basis: dict[str, Any]) -> dict[str, Any]:
    """The result definition actually used for one object, for the response."""
    summary: dict[str, Any] = {"source": "meta_results_field", "status": basis["status"]}
    for key in ("indicator", "action_type", "additive", "indicators", "error"):
        if key in basis:
            summary[key] = basis[key]
    if basis.get("status") == "single":
        summary["conversion_value_source"] = (
            f"action_values {basis['action_type']}" if basis["value_reported"] else None
        )
    return summary


def _result_signals(basis: dict[str, Any], level: str, *, rows_exist: bool) -> list[str]:
    """Plain statements of what the result basis means for the result metrics."""
    if not rows_exist:
        return []
    status = basis["status"]
    if status == "request_failed":
        return [
            f"Meta rejected the request for its result fields ({basis['error']}), so the rows were read without "
            "them; results, cost per result, conversion rate and ROAS are null. No other action is substituted."
        ]
    if status == "not_reported":
        hint = (
            " At account level Meta may not report a result, because an account can mix result types; use "
            "child_level='campaign' to see each campaign's own result."
            if level == "account" else ""
        )
        return [
            "Meta reported no result (the Insights results field) for this object in this window, so results, cost "
            f"per result, conversion rate and ROAS are null. No other action is substituted.{hint}"
        ]
    if status == "unreadable":
        return ["Meta's results field could not be read in this window, so the result metrics are null."]
    if status == "mixed":
        return [
            f"Meta reported more than one result type in this window ({', '.join(basis['indicators'])}); they "
            "cannot be added together, so results, cost per result, conversion rate and ROAS are null. Use "
            "child_level or object_ids to see each entity's own result."
        ]
    signals: list[str] = []
    if not basis["additive"]:
        signals.append(
            f"Meta's result here is {basis['indicator']}, which is not an action count and does not add up across "
            "days, so results are shown only for single-day periods, with Meta's own cost per result; "
            "conversion rate and ROAS are null."
        )
    elif not basis["value_reported"]:
        signals.append(
            f"Meta reported no value for the result's action ({basis['action_type']}), so ROAS is null."
        )
    if basis.get("rows_unreadable"):
        signals.append(
            f"{basis['rows_unreadable']} row(s) had a result that could not be read; their periods' results are null."
        )
    return signals


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


# ---------------------------------------------------------------------------
# Several entities per call (object_ids, or a child-level split of one object)
# ---------------------------------------------------------------------------

MAX_ENTITIES = 10
DEFAULT_MAX_ENTITIES = 5
ENTITY_CONCURRENCY = 3
CHILD_LEVELS: dict[str, tuple[str, ...]] = {
    "account": ("campaign", "adset", "ad"),
    "campaign": ("adset", "ad"),
    "adset": ("ad",),
    "ad": (),
}
_ENTITY_READ_ERRORS = (MetaApiError, NotFoundError, UnsupportedFeatureError, ValidationError)


def _require_graph_ids(values: Any, *, field: str = "object_ids", limit: int = MAX_ENTITIES) -> list[str]:
    """Validate a list of numeric Meta ids before any network call; duplicates are dropped."""
    ids = list(dict.fromkeys(
        str(value).strip() for value in (coerce_csv_string_list(values) or []) if str(value).strip()
    ))
    if not ids:
        raise ValidationError(f"{field} must contain at least one id.")
    if len(ids) > limit:
        raise ValidationError(f"Provide at most {limit} {field} per call.")
    bad = [value for value in ids if not value.isdigit()]
    if bad:
        raise ValidationError(f"{field} must be numeric Meta ids passed as strings; not valid: {bad[:3]}.")
    return ids


def _entity_targets(
    level: str, object_id: str | None, object_ids: Any, child_level: str | None, max_entities: int,
) -> dict[str, Any]:
    """Validate how many entities a call covers, before any network call.

    Modes: single (object_id), object_ids (several objects at `level`), children (the highest-spending children of
    one object at child_level).
    """
    has_ids = object_ids is not None and coerce_csv_string_list(object_ids) not in ([], "")
    child = (child_level or "").strip().lower() or None
    if not 1 <= max_entities <= MAX_ENTITIES:
        raise ValidationError(f"max_entities must be between 1 and {MAX_ENTITIES}.")
    if has_ids:
        if blank_to_none(object_id):
            raise ValidationError("Give object_id or object_ids, not both.")
        if child:
            raise ValidationError("child_level splits one object_id; it cannot be combined with object_ids.")
        if level == "account":
            raise ValidationError("object_ids take campaign, ad set or ad ids; call once per ad account.")
        return {"mode": "object_ids", "ids": _require_graph_ids(object_ids)}
    resolved = _require_object_id(level, object_id)
    if child:
        if child not in CHILD_LEVELS[level]:
            allowed = list(CHILD_LEVELS[level]) or "none (an ad has no children)"
            raise ValidationError(f"child_level for level {level} must be one of {allowed}.")
        return {"mode": "children", "object_id": resolved, "child_level": child, "max_entities": max_entities}
    return {"mode": "single", "object_id": resolved}


async def _select_children(
    object_id: str, child_level: str, start: date, end: date, max_entities: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Pick the highest-spending children in the window with one lean, spend-sorted request.

    Lean because a sorted request that carried action fields failed at Meta on a large account.
    """
    params = insights_tools._insights_params(
        level=child_level, since=start.isoformat(), until=end.isoformat(), limit=max_entities,
    )
    params["sort"] = ["spend_descending"]
    id_field, name_field = f"{child_level}_id", f"{child_level}_name"
    payload = await get_graph_api_client().get_insights(object_id, fields=[id_field, name_field, "spend"], params=params)
    children: list[dict[str, Any]] = []
    for row in payload.get("data", []) or []:
        if isinstance(row, dict) and row.get(id_field) is not None and str(row[id_field]) not in {
            child["object_id"] for child in children
        }:
            children.append({"object_id": str(row[id_field]), "name": row.get(name_field), "spend": to_float(row.get("spend"))})
    return children[:max_entities], bool(extract_paging(payload).get("next"))


async def _read_entities_one_by_one(
    level: str, ids: list[str], start: date, end: date,
) -> dict[str, dict[str, Any]]:
    """Read each entity's daily rows with its own request; a failure is recorded for that entity only.

    After a rate-limit error no further entity is read, so the call does not keep spending the shared limit.
    """
    semaphore = asyncio.Semaphore(ENTITY_CONCURRENCY)
    stopped: dict[str, str] = {}
    name_field = f"{level}_name"

    async def read(entity_id: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            if stopped:
                return entity_id, {"status": "not_read", "error": stopped["reason"]}
            try:
                rows, result_error = await _daily_rows(level, entity_id, start, end, extra_fields=[name_field])
            except RateLimitError as exc:
                stopped["reason"] = f"Meta's rate limit was reached ({exc}); this entity was not read."
                return entity_id, {"status": "unreadable", "error": str(exc)}
            except _ENTITY_READ_ERRORS as exc:
                return entity_id, {"status": "unreadable", "error": str(_public_error(exc).get("message"))}
            name = next((row.get(name_field) for row in rows if row.get(name_field)), None)
            return entity_id, {"status": "read", "rows": rows, "result_error": result_error, "name": name}

    return dict(await asyncio.gather(*(read(entity_id) for entity_id in ids)))


async def _read_children(
    object_id: str, child_level: str, children: list[dict[str, Any]], start: date, end: date,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read the selected children's daily rows in one request filtered to their ids.

    The filter uses Meta's documented `filtering` parameter (DOT notation, IN operator). Meta's reference shows
    no example of filtering insights by an id, so if that request fails, each child is read with its own request.
    Rows are matched to children by id, so a filter Meta ignores cannot mix in other objects.
    """
    ids = [child["object_id"] for child in children]
    id_field = f"{child_level}_id"
    days = (end - start).days + 1
    try:
        rows, result_error = await _daily_rows(
            child_level, object_id, start, end, row_level=child_level, extra_fields=[id_field],
            filtering=[{"field": f"{child_level}.id", "operator": "IN", "value": ids}],
            max_rows=len(ids) * days,
        )
    except RateLimitError:
        raise
    except _ENTITY_READ_ERRORS as exc:
        error = str(_public_error(exc).get("message"))
        entities = await _read_entities_one_by_one(child_level, ids, start, end)
        return entities, {"data_read": "one_request_per_entity", "filtered_read_error": error}
    grouped: dict[str, list[dict[str, Any]]] = {entity_id: [] for entity_id in ids}
    for row in rows:
        key = str(row.get(id_field) or "")
        if key in grouped:
            grouped[key].append(row)
    entities = {
        entity_id: {"status": "read", "rows": grouped[entity_id], "result_error": result_error}
        for entity_id in ids
    }
    return entities, {"data_read": "one_filtered_request", "filtered_read_error": None}


async def _read_targets(
    targets: dict[str, Any], level: str, start: date, end: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the daily rows of every entity a multi-entity call covers, with coverage."""
    coverage: dict[str, Any] = {"mode": targets["mode"]}
    if targets["mode"] == "object_ids":
        ids = targets["ids"]
        read = await _read_entities_one_by_one(level, ids, start, end)
        names: dict[str, Any] = {}
        coverage["data_read"] = "one_request_per_entity"
        coverage["why"] = (
            "object_ids may belong to different campaigns or accounts, so no single parent request can serve them."
        )
        entity_level = level
    else:
        entity_level = targets["child_level"]
        children, more = await _select_children(
            targets["object_id"], entity_level, start, end, targets["max_entities"],
        )
        ids = [child["object_id"] for child in children]
        names = {child["object_id"]: child["name"] for child in children}
        coverage["selection"] = {
            "method": (
                f"one lean request at {entity_level} level for the whole window, sorted by spend highest first, "
                f"limit {targets['max_entities']}"
            ),
            "children_selected": len(ids),
            "max_entities": targets["max_entities"],
            "more_children_exist": more,
        }
        read, method = (await _read_children(targets["object_id"], entity_level, children, start, end)) if ids else ({}, {
            "data_read": "none", "filtered_read_error": None,
        })
        coverage.update(method)
    entities: list[dict[str, Any]] = []
    for entity_id in ids:
        outcome = read[entity_id]
        entity: dict[str, Any] = {
            "object_id": entity_id,
            "level": entity_level,
            "name": outcome.get("name") or names.get(entity_id),
            "status": outcome["status"],
        }
        if outcome["status"] == "read":
            entity["_rows"] = outcome["rows"]
            entity["_result_error"] = outcome["result_error"]
            if not outcome["rows"]:
                entity["status"] = "no_rows"
        else:
            entity["error"] = outcome["error"]
        entities.append(entity)
    coverage.update({
        "entities_requested": len(ids),
        "entities_read": sum(1 for entity in entities if entity["status"] == "read"),
        "entities_without_rows": [entity["object_id"] for entity in entities if entity["status"] == "no_rows"],
        "entities_unreadable": [
            {"object_id": entity["object_id"], "error": entity.get("error")}
            for entity in entities if entity["status"] in ("unreadable", "not_read")
        ],
    })
    coverage["complete"] = (
        not coverage["entities_unreadable"]
        and not (coverage.get("selection") or {}).get("more_children_exist", False)
    )
    return entities, coverage


def _coverage_signals(coverage: dict[str, Any]) -> list[str]:
    """Name every entity that could not be read or had no rows; never omit one silently."""
    signals = []
    for item in coverage["entities_unreadable"]:
        signals.append(f"{item['object_id']} could not be read ({item['error']}); the other entities are reported.")
    if coverage["entities_without_rows"]:
        signals.append(
            "Meta returned no insights rows in the window for " + ", ".join(coverage["entities_without_rows"]) + "."
        )
    selection = coverage.get("selection")
    if selection and selection["more_children_exist"]:
        signals.append(
            f"Only the {selection['children_selected']} highest-spending {coverage.get('child_level', 'child')} "
            f"entities were reported; more exist. Raise max_entities (up to {MAX_ENTITIES}) to cover more."
        )
    if coverage.get("filtered_read_error"):
        signals.append(
            "The single request filtered to the selected ids failed "
            f"({coverage['filtered_read_error']}), so each entity was read with its own request."
        )
    return signals


def _multi_scope(level: str, targets: dict[str, Any]) -> dict[str, Any]:
    if targets["mode"] == "object_ids":
        return {"level": level, "object_ids": list(targets["ids"])}
    return {"level": level, "object_id": targets["object_id"], "child_level": targets["child_level"]}


def _trend_periods(
    rows: list[dict[str, Any]], basis: dict[str, Any], start: date, end: date, granularity: str,
    metric_names: list[str],
) -> list[dict[str, Any]]:
    """Group daily rows into periods, sum inputs under INPUT_POLICY, and derive each metric."""
    buckets: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        day = _parse_date(str(row["date_start"]), field="date_start")
        buckets.setdefault(_period_bounds(day, granularity)[0], []).append(row)
    periods: list[dict[str, Any]] = []
    for period_start in sorted(buckets):
        _, period_end = _period_bounds(period_start, granularity)
        inputs = _period_inputs(buckets[period_start], basis)
        derived = _derived_metrics(inputs, basis)
        periods.append({
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "partial": period_start < start or period_end > end,
            "days_with_data": len(buckets[period_start]),
            "inputs": inputs,
            "metrics": {name: derived[name] for name in metric_names},
        })
    return periods


def _trend_signals(rows: list[dict[str, Any]], basis: dict[str, Any], periods: list[dict[str, Any]], level: str) -> list[str]:
    signals: list[str] = []
    if not rows:
        signals.append("Meta returned no insights rows for this object and window.")
    signals.extend(_result_signals(basis, level, rows_exist=bool(rows)))
    signals.extend(
        _missing_delivery_signal([(period["period_start"], period["inputs"]) for period in periods], "period")
    )
    return signals


_COMPACT_CHANGE_KEYS = ("first", "last", "first_period", "last_period", "pct_change", "direction", "assessment")
TREND_METHOD = (
    "Daily rows are read with time_increment=1, additive inputs are summed per period under input_policy, "
    "and each metric is divided from those sums. Direction compares the first and last periods with a "
    "value; partial periods are flagged and are not scaled."
)


@mcp_server.tool()
async def get_performance_trend(
    level: str,
    object_id: str | None = None,
    metrics: StringList | None = None,
    since: str | None = None,
    until: str | None = None,
    granularity: str = "day",
    object_ids: StringList | None = None,
    child_level: str | None = None,
    max_entities: int = DEFAULT_MAX_ENTITIES,
) -> dict[str, Any]:
    """Trend CPC, CPM, cost per result, ROAS, CTR and conversion rate for an account, campaign, ad set or ad by day, week (Monday start) or month, with first-to-last change and direction. Results follow Meta's own result for the object (the Insights results field); the response states the result used. Periods are summed from daily rows, then divided; zero denominators give null. Default window ends yesterday in the ad account's time zone (30/84/180 days). Several entities: object_ids (up to 10 numeric ids at level) or child_level (the max_entities highest-spending children of object_id); these return per-entity totals and changes, not periods."""
    resolved_level = _require_level(level, REPORTING_LEVELS)
    targets = _entity_targets(resolved_level, object_id, object_ids, child_level, max_entities)
    resolved_granularity = (granularity or "").strip().lower()
    if resolved_granularity not in TREND_DEFAULT_WINDOW_DAYS:
        raise ValidationError("granularity must be day, week or month.")
    metric_names = _normalize_metric_names(metrics, TREND_DEFAULT_METRICS)
    explicit = _explicit_window(since, until)
    missing_signals: list[str] = []
    if explicit is None:
        anchor_level, anchor_id = (
            (resolved_level, targets["ids"][0]) if targets["mode"] == "object_ids" else (resolved_level, targets["object_id"])
        )
        today, reporting_timezone, timezone_signal = await _reporting_today(anchor_level, anchor_id)
        if targets["mode"] == "object_ids":
            reporting_timezone["anchor_object_id"] = anchor_id
            reporting_timezone["note"] = "Default dates come from the ad account of the first object id."
        if timezone_signal:
            missing_signals.append(timezone_signal)
        end = today - timedelta(days=1)
        start = end - timedelta(days=TREND_DEFAULT_WINDOW_DAYS[resolved_granularity] - 1)
    else:
        start, end = explicit
        reporting_timezone = dict(EXPLICIT_DATES_TIMEZONE)
    common = {
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "reporting_timezone": reporting_timezone,
        "granularity": resolved_granularity,
        "metrics_requested": metric_names,
        "metric_definitions": {name: METRIC_DEFINITIONS[name] for name in metric_names},
        "results_definition": RESULTS_DEFINITION,
        "input_policy": INPUT_POLICY,
    }

    if targets["mode"] == "single":
        resolved_object_id = targets["object_id"]
        rows, result_error = await _daily_rows(resolved_level, resolved_object_id, start, end)
        basis = _result_basis(rows, result_error=result_error)
        periods = _trend_periods(rows, basis, start, end, resolved_granularity, metric_names)
        missing_signals.extend(_trend_signals(rows, basis, periods, resolved_level))
        return analysis_response(
            scope={"level": resolved_level, "object_id": resolved_object_id},
            metrics={},
            missing_signals=missing_signals,
            extra={
                **common,
                "result": _result_summary(basis),
                "periods": periods,
                "changes": {name: _first_to_last(name, periods) for name in metric_names},
                "method": TREND_METHOD,
                "daily_rows_returned": len(rows),
            },
        )

    entities, coverage = await _read_targets(targets, resolved_level, start, end)
    if targets["mode"] == "children":
        coverage["child_level"] = targets["child_level"]
    for entity in entities:
        rows = entity.pop("_rows", None)
        result_error = entity.pop("_result_error", None)
        if rows is None:
            continue
        basis = _result_basis(rows, result_error=result_error)
        periods = _trend_periods(rows, basis, start, end, resolved_granularity, metric_names)
        totals_inputs = _period_inputs(rows, basis) if rows else None
        entity["daily_rows_returned"] = len(rows)
        entity["result"] = _result_summary(basis)
        entity["totals"] = None if totals_inputs is None else {
            "inputs": totals_inputs,
            "metrics": {name: _derived_metrics(totals_inputs, basis)[name] for name in metric_names},
        }
        entity["changes"] = {
            name: {key: change[key] for key in _COMPACT_CHANGE_KEYS}
            for name in metric_names
            for change in (_first_to_last(name, periods),)
        }
        entity["missing_signals"] = _trend_signals(rows, basis, periods, entity["level"])
    missing_signals.extend(_coverage_signals(coverage))
    return analysis_response(
        scope=_multi_scope(resolved_level, targets),
        metrics={},
        missing_signals=missing_signals,
        extra={
            **common,
            "entities": entities,
            "coverage": coverage,
            "method": TREND_METHOD + " With several entities, each entity's window totals and first-to-last changes "
            "are returned; call with that entity's object_id for its periods.",
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
MULTI_ENTITY_MAX_FINDINGS = 20


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


def _anomaly_analysis(
    rows: list[dict[str, Any]], basis: dict[str, Any], metric_names: list[str], *, baseline_start: date,
    baseline_end: date, lookback_start: date, lookback_days: int, baseline_days: int, multiplier: float,
) -> dict[str, Any]:
    """Score each lookback day of one object against its own baseline."""
    daily: dict[date, dict[str, float | None]] = {}
    daily_inputs: list[tuple[str, dict[str, float | None]]] = []
    for row in rows:
        day = _parse_date(str(row["date_start"]), field="date_start")
        inputs = _period_inputs([row], basis)
        daily_inputs.append((day.isoformat(), inputs))
        daily[day] = _derived_metrics(inputs, basis)
    baseline_dates = sorted(day for day in daily if baseline_start <= day <= baseline_end)
    lookback_dates = [lookback_start + timedelta(days=offset) for offset in range(lookback_days)]
    analysis: dict[str, Any] = {
        "anomalies": [],
        "metric_status": {},
        "missing_signals": [],
        "baseline_rows": len(baseline_dates),
        "lookback_rows": sum(1 for day in lookback_dates if day in daily),
        "insufficient": len(baseline_dates) < baseline_days,
    }
    if analysis["insufficient"]:
        analysis["missing_signals"].append(
            f"Not enough history: {len(baseline_dates)} of {baseline_days} baseline days returned insights rows."
        )
        return analysis
    anomalies = analysis["anomalies"]
    missing_signals = analysis["missing_signals"]
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
        analysis["metric_status"][metric] = status
        if len(baseline_points) < MIN_BASELINE_VALUES:
            status["status"] = "not_enough_values"
            missing_signals.append(
                f"{metric}: only {len(baseline_points)} baseline days have a value; at least {MIN_BASELINE_VALUES} are needed."
            )
            continue
        overall_median, overall_mad = _median_and_mad([value for _, value in baseline_points])
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
    missing_signals.extend(_missing_delivery_signal(daily_inputs, "day"))
    anomalies.sort(key=lambda anomaly: (-abs(anomaly["robust_score"]), anomaly["day"], anomaly["metric"]))
    return analysis


def _anomaly_finding(anomaly: dict[str, Any], multiplier: float, entity: dict[str, Any]) -> dict[str, Any]:
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
                    "baseline_values": anomaly.get("baseline_values"),
                },
            )
        ],
        affected_entities=[dict(entity)],
        next_actions=[
            "Check the change history and delivery settings for that day before acting.",
            "Compare with get_performance_trend to see whether the change persisted.",
        ],
    )
    finding["confidence_status"] = "uncalibrated"
    return finding


def _no_anomaly_finding() -> dict[str, Any]:
    return {
        **build_finding(
            "no_anomalies_detected",
            "No examined day fell outside the flag range for the selected metrics.",
            severity="low",
            confidence=None,
        ),
        "confidence_status": "uncalibrated",
    }


_COMPACT_ANOMALY_KEYS = (
    "day", "metric", "value", "baseline_median", "baseline_mad", "threshold_low", "threshold_high",
    "robust_score", "direction", "assessment", "baseline_type", "baseline_points",
)
_COMPACT_STATUS_KEYS = ("status", "days_examined", "flagged_days", "baseline_median", "baseline_values_count")


@mcp_server.tool()
async def detect_performance_anomalies(
    level: str,
    object_id: str | None = None,
    metrics: StringList | None = None,
    lookback_days: int = 7,
    baseline_days: int = 28,
    sensitivity: str = "medium",
    end_date: str | None = None,
    object_ids: StringList | None = None,
    child_level: str | None = None,
    max_entities: int = DEFAULT_MAX_ENTITIES,
) -> dict[str, Any]:
    """Flag days in the last lookback_days whose spend, CPC, CPM, CTR, cost per result or ROAS is far from the object's own baseline (the baseline_days before), using median and median absolute deviation, with same-weekday baselines when four or more exist. Cost per result and ROAS follow Meta's own result for the object (the Insights results field). Returns the rule, baseline values and thresholds. Rule-based screen, not a statistical test. end_date defaults to yesterday in the ad account's time zone. Several entities: object_ids (up to 10 numeric ids at level) or child_level (the max_entities highest-spending children of object_id)."""
    resolved_level = _require_level(level, REPORTING_LEVELS)
    targets = _entity_targets(resolved_level, object_id, object_ids, child_level, max_entities)
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
    timezone_signals: list[str] = []
    if end_text:
        end = _parse_date(end_text, field="end_date")
        reporting_timezone = dict(EXPLICIT_DATES_TIMEZONE)
    else:
        anchor_id = targets["ids"][0] if targets["mode"] == "object_ids" else targets["object_id"]
        today, reporting_timezone, timezone_signal = await _reporting_today(resolved_level, anchor_id)
        if targets["mode"] == "object_ids":
            reporting_timezone["anchor_object_id"] = anchor_id
            reporting_timezone["note"] = "Default dates come from the ad account of the first object id."
        timezone_signals = [timezone_signal] if timezone_signal else []
        end = today - timedelta(days=1)
    lookback_start = end - timedelta(days=lookback_days - 1)
    baseline_end = lookback_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=baseline_days - 1)
    rule = _anomaly_rule(resolved_sensitivity, multiplier)
    windows = {
        "baseline_window": {"since": baseline_start.isoformat(), "until": baseline_end.isoformat()},
        "lookback_window": {"since": lookback_start.isoformat(), "until": end.isoformat()},
        "reporting_timezone": reporting_timezone,
    }
    analysis_kwargs = {
        "baseline_start": baseline_start, "baseline_end": baseline_end, "lookback_start": lookback_start,
        "lookback_days": lookback_days, "baseline_days": baseline_days, "multiplier": multiplier,
    }

    if targets["mode"] == "single":
        resolved_object_id = targets["object_id"]
        scope = {"level": resolved_level, "object_id": resolved_object_id}
        rows, result_error = await _daily_rows(resolved_level, resolved_object_id, baseline_start, end)
        basis = _result_basis(rows, result_error=result_error)
        analysis = _anomaly_analysis(rows, basis, metric_names, **analysis_kwargs)
        result_signals = _result_signals(basis, resolved_level, rows_exist=bool(rows))
        if analysis["insufficient"]:
            return analysis_response(
                scope=scope,
                metrics={},
                findings=[{
                    **build_finding(
                        "insufficient_data",
                        f"Only {analysis['baseline_rows']} of {baseline_days} baseline days have data, so no day was judged.",
                        severity="low",
                        confidence=None,
                        next_actions=["Use a shorter baseline_days or an object with a longer delivery history."],
                    ),
                    "confidence_status": "uncalibrated",
                }],
                missing_signals=[*timezone_signals, *analysis["missing_signals"]],
                extra={
                    **windows,
                    "rule": rule,
                    "anomalies": [],
                    "metric_status": {},
                    "baseline_rows": analysis["baseline_rows"],
                    "lookback_rows": analysis["lookback_rows"],
                    "result": _result_summary(basis),
                    "results_definition": RESULTS_DEFINITION,
                    "input_policy": INPUT_POLICY,
                },
            )
        findings = [_anomaly_finding(anomaly, multiplier, scope) for anomaly in analysis["anomalies"]]
        return analysis_response(
            scope=scope,
            metrics={},
            findings=findings or [_no_anomaly_finding()],
            missing_signals=[*timezone_signals, *result_signals, *analysis["missing_signals"]],
            extra={
                **windows,
                "rule": rule,
                "anomalies": analysis["anomalies"],
                "metric_status": analysis["metric_status"],
                "baseline_rows": analysis["baseline_rows"],
                "lookback_rows": analysis["lookback_rows"],
                "metric_definitions": {name: METRIC_DEFINITIONS[name] for name in metric_names},
                "result": _result_summary(basis),
                "results_definition": RESULTS_DEFINITION,
                "input_policy": INPUT_POLICY,
            },
        )

    entities, coverage = await _read_targets(targets, resolved_level, baseline_start, end)
    if targets["mode"] == "children":
        coverage["child_level"] = targets["child_level"]
    scored: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entity in entities:
        rows = entity.pop("_rows", None)
        result_error = entity.pop("_result_error", None)
        if rows is None:
            continue
        basis = _result_basis(rows, result_error=result_error)
        analysis = _anomaly_analysis(rows, basis, metric_names, **analysis_kwargs)
        reference = {"level": entity["level"], "object_id": entity["object_id"], "name": entity["name"]}
        entity["result"] = _result_summary(basis)
        entity["baseline_rows"] = analysis["baseline_rows"]
        entity["lookback_rows"] = analysis["lookback_rows"]
        entity["judged"] = not analysis["insufficient"]
        entity["anomaly_count"] = len(analysis["anomalies"])
        entity["anomalies"] = [
            {key: anomaly[key] for key in _COMPACT_ANOMALY_KEYS} for anomaly in analysis["anomalies"][:5]
        ]
        entity["metric_status"] = {
            metric: {key: status[key] for key in _COMPACT_STATUS_KEYS if key in status}
            for metric, status in analysis["metric_status"].items()
        }
        entity["missing_signals"] = [
            *_result_signals(basis, entity["level"], rows_exist=bool(rows)), *analysis["missing_signals"],
        ]
        scored.extend((anomaly, reference) for anomaly in analysis["anomalies"])
    scored.sort(key=lambda item: (-abs(item[0]["robust_score"]), item[0]["day"], item[1]["object_id"], item[0]["metric"]))
    findings = []
    for anomaly, reference in scored[:MULTI_ENTITY_MAX_FINDINGS]:
        compact = {key: anomaly[key] for key in _COMPACT_ANOMALY_KEYS}
        findings.append(_anomaly_finding(compact, multiplier, reference))
    missing_signals = [*timezone_signals, *_coverage_signals(coverage)]
    if len(scored) > MULTI_ENTITY_MAX_FINDINGS:
        missing_signals.append(
            f"{len(scored)} anomalies were found; findings list the {MULTI_ENTITY_MAX_FINDINGS} furthest from their "
            "baselines. Each entity lists its count and its five largest."
        )
    return analysis_response(
        scope=_multi_scope(resolved_level, targets),
        metrics={"anomaly_count": len(scored)},
        findings=findings or [_no_anomaly_finding()],
        missing_signals=missing_signals,
        extra={
            **windows,
            "rule": rule,
            "entities": entities,
            "coverage": coverage,
            "metric_definitions": {name: METRIC_DEFINITIONS[name] for name in metric_names},
            "results_definition": RESULTS_DEFINITION,
            "input_policy": INPUT_POLICY,
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
# The ad-level read is one bounded page ordered by spend, highest first, using the
# Insights API's documented sort parameter ("reach_descending" style, one element).
# On a live account with more than 2,000 ads the sorted read failed ("An unknown error
# occurred") whenever it carried the action fields, and succeeded with identifiers, spend,
# impressions and the rankings only. So the sorted read is always lean, and results come
# from one further request restricted to the selected ads.
RANKING_DEFAULT_MAX_ADS = 200
RANKING_MAX_ADS_LIMIT = 500
RANKING_SORT = ["spend_descending"]
RANKING_LEAN_FIELDS = [
    "ad_id", "ad_name", "campaign_id", "adset_id", "spend", "impressions", *RANKING_FIELDS,
]
# Cost evidence: one unsorted request filtered with the documented `filtering` parameter
# (dot notation for the object's fields, IN operator) to the selected ads' ids. Meta's reference
# documents the parameter, the IN operator and DOT notation, but shows no example that filters
# insights by an id; the rankings-only diagnosis is kept when this request fails.
RANKING_COST_FIELDS = ["ad_id", "clicks", "actions", "action_values", RESULT_FIELD, COST_PER_RESULT_FIELD]
RANKING_COST_MAX_ADS = 100
RANKING_SCOPE_FIELDS = ["spend", "impressions", "clicks", "actions", "action_values"]
RANKING_SCOPE_LEAN_FIELDS = ["spend", "impressions"]
# Below this share of examined ads with all three rankings known, the report says it rests on little evidence.
RANKING_EVIDENCE_MIN_SHARE = 0.5
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
    "conversion_path": (
        "only conversion_rate_ranking is below average; quality and engagement are both known and average or above. "
        "When either of them is UNKNOWN, no diagnosis is made."
    ),
    "bid_and_budget": (
        "all three rankings are average or above, and either cost per result is at least cost_ratio_threshold times "
        "the baseline cost per result for the same Meta result, or the ad has no results after spending at least one "
        "baseline cost per result. Needs the ad's results from the cost-evidence read; without them this diagnosis "
        "is not made."
    ),
    "cost_ratio_threshold": COST_RATIO_THRESHOLD,
    "cost_baseline": (
        "An ad is compared only with cost for the same Meta result (indicator): the scope's total when the scope "
        "reports that one result, otherwise the examined ads with cost evidence that report it. Ads with different "
        "results are never compared."
    ),
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
        unknown = [field for field in ("quality_ranking", "engagement_rate_ranking") if points[field] is None]
        if unknown:
            return (
                None,
                f"The conversion-rate ranking is below average, but {' and '.join(unknown)} is unknown, so the "
                "evidence cannot tell the post-click path apart from the creative; no diagnosis is made.",
                below,
            )
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
    if metrics["results"] == 0 and scope_cost and (metrics["spend"] or 0) >= scope_cost:
        return (
            "bid_and_budget",
            "All three rankings are average or above, yet the ad has no results after spending at least one scope "
            "cost per result, which points at auction competition, bid or budget.",
            below,
        )
    return None, None, below


async def _ranking_scope_rows(
    level: str, object_id: str, window: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None, bool]:
    """Read the scope's own insights total with Meta's result; if that read fails, retry with spend and impressions.

    Returns the rows, the errors met, and whether the rows carry Meta's result fields. Failures are reported,
    never raised.
    """
    params = insights_tools._insights_params(level=level, default_date_preset="last_30d", limit=25, **window)
    errors: list[str] = []
    attempts = (([*RANKING_SCOPE_FIELDS, *_result_fields(level)], True), (RANKING_SCOPE_LEAN_FIELDS, False))
    for fields, with_results in attempts:
        try:
            payload = await get_graph_api_client().get_insights(object_id, fields=fields, params=params)
        except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
            errors.append(str(_public_error(exc).get("message")))
            continue
        return _normalize_with_results(payload), ("; ".join(errors) or None), with_results
    return None, "; ".join(errors), False


async def _ranking_cost_rows(
    object_id: str, ad_ids: list[str], window: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Read results for the selected ads in one request filtered to their ids. Never raises."""
    if not ad_ids:
        return {}, None
    params = insights_tools._insights_params(
        level="ad", default_date_preset="last_30d", limit=len(ad_ids),
        filtering=[{"field": "ad.id", "operator": "IN", "value": list(ad_ids)}], **window,
    )
    try:
        payload = await get_graph_api_client().get_insights(object_id, fields=RANKING_COST_FIELDS, params=params)
    except Exception as exc:  # an enrichment failure must not fail the rankings report
        return {}, str(_public_error(exc).get("message"))
    wanted = set(ad_ids)
    rows: dict[str, dict[str, Any]] = {}
    for row in payload.get("data", []) or []:
        if isinstance(row, dict) and str(row.get("ad_id") or "") in wanted:
            rows[str(row["ad_id"])] = row
    return rows, None


def _known_ranking(value: Any) -> bool:
    return RANKING_POINTS.get(str(value or "").upper()) is not None


def _row_spend(row: dict[str, Any]) -> float:
    return to_float((row.get("metrics") or {}).get("spend")) or 0.0


def _is_action_indicator(indicator: str, row: dict[str, Any]) -> bool:
    return indicator.startswith(ACTION_INDICATOR_PREFIX) or indicator in (row.get("actions_map") or {})


def _pooled_costs(rows: list[dict[str, Any]], pool_basis: dict[str, Any]) -> dict[str, float | None]:
    """Cost per result for each action result across ads: summed spend / summed results, per indicator."""
    sums: dict[str, list[float | None]] = {}
    for row in rows:
        result = row.get("meta_result") or {"status": "absent"}
        if result["status"] == "read":
            indicator, count = result["indicator"], result["value"] or 0.0
        elif result["status"] == "absent" and pool_basis.get("status") == "single":
            indicator, count = pool_basis["indicator"], 0.0
        else:
            continue
        if not _is_action_indicator(indicator, row):
            continue
        spend = to_float((row.get("metrics") or {}).get("spend"))
        total = sums.setdefault(indicator, [0.0, 0.0])
        total[0] = None if spend is None or total[0] is None else total[0] + spend
        total[1] = (total[1] or 0.0) + count
    return {indicator: _ratio(spend, results) for indicator, (spend, results) in sums.items()}


async def _ranking_entity(
    level: str, object_id: str, window: dict[str, Any], max_ads: int,
) -> dict[str, Any]:
    """Run the ranking analysis for one scope: lean sorted read, scope total, and one bounded cost read."""
    ad_params = insights_tools._insights_params(level="ad", default_date_preset="last_30d", limit=max_ads, **window)
    ad_params["sort"] = list(RANKING_SORT)

    async def read_ads() -> dict[str, Any]:
        return await get_graph_api_client().get_insights(object_id, fields=RANKING_LEAN_FIELDS, params=ad_params)

    async def read_scope() -> tuple[list[dict[str, Any]] | None, str | None, bool]:
        if level == "ad":
            return None, None, False
        return await _ranking_scope_rows(level, object_id, window)

    ad_payload, (scope_rows, scope_error, scope_has_results) = await asyncio.gather(read_ads(), read_scope())
    lean_rows = [row for row in (ad_payload.get("data", []) or []) if isinstance(row, dict)][:max_ads]
    more_ads_exist = bool(extract_paging(ad_payload).get("next"))
    cost_ids = list(dict.fromkeys(
        str(row["ad_id"]) for row in lean_rows
        if row.get("ad_id") and any(_known_ranking(row.get(field)) for field in RANKING_FIELDS)
    ))
    cost_ids_capped = cost_ids[:RANKING_COST_MAX_ADS]
    cost_rows, cost_error = await _ranking_cost_rows(object_id, cost_ids_capped, window)
    rows = _normalize_with_results({"data": [
        {**cost_rows[str(row.get("ad_id"))], **row} if str(row.get("ad_id")) in cost_rows else row
        for row in lean_rows
    ]})
    spends = [_row_spend(row) for row in rows]
    order_confirmed = all(earlier >= later for earlier, later in zip(spends, spends[1:]))

    missing_signals: list[str] = []
    examined_spend = sum(spends)
    if level == "ad":
        scope_spend: float | None = examined_spend
    elif scope_rows is None:
        scope_spend = None
        missing_signals.append(
            f"The scope's total spend could not be read, so the share of spend covered is unknown ({scope_error})."
        )
    else:
        scope_spend = sum(_row_spend(row) for row in scope_rows)
    share = _ratio(examined_spend, scope_spend)
    if share is not None:
        share = min(share, 1.0)
    if more_ads_exist:
        coverage = f"; they cover {share:.0%} of the scope's spend" if share is not None else ""
        note = (
            f"Examined the {len(rows)} highest-spending ads in the window{coverage}. "
            "More ads exist; lower-spending ads were not ranked. Raise max_ads to examine more."
        )
        missing_signals.append(note)
    else:
        note = f"Examined all {len(rows)} ads with insights in the window, highest spend first."
    if not order_confirmed:
        missing_signals.append(
            "Meta returned the ad rows not ordered by spend, so the examined ads may not be the highest-spending ones."
        )

    if cost_error is not None:
        cost_status = "failed"
        missing_signals.append(
            f"Cost-per-result evidence was not available ({cost_error}); diagnoses rest on the rankings only, so "
            "no ad can be diagnosed as bid_and_budget and results are null."
        )
    elif cost_ids_capped:
        cost_status = "read"
    else:
        cost_status = "not_requested"
        if rows:
            missing_signals.append(
                "No examined ad has a known ranking, so results and cost per result were not read and are null."
            )
    without_cost = len(rows) - len(cost_rows)
    if cost_status == "read" and without_cost:
        missing_signals.append(
            f"Results and cost per result were read for {len(cost_rows)} of {len(rows)} examined ads: those with at "
            f"least one known ranking (at most {RANKING_COST_MAX_ADS}, highest spend first). The others show them as null."
        )
    cost_evidence = {
        "status": cost_status,
        "method": (
            "one insights request at ad level for the same window, filtered to the selected ad ids "
            "(filtering ad.id IN), unsorted, with Meta's results and cost_per_result fields"
        ),
        "selection": f"examined ads with at least one known ranking, highest spend first, at most {RANKING_COST_MAX_ADS}",
        "ads_requested": len(cost_ids_capped),
        "ads_returned": len(cost_rows),
        "ads_eligible_not_requested": len(cost_ids) - len(cost_ids_capped),
        "error": cost_error,
    }

    cost_known_rows = [row for row in rows if str(row.get("ad_id")) in cost_rows]
    pool_basis = _result_basis(cost_known_rows)
    scope_basis = _result_basis(scope_rows or []) if scope_has_results else {"status": "not_reported"}
    if scope_rows and scope_basis["status"] == "single":
        scope_metrics = _derived_metrics(_period_inputs(scope_rows, scope_basis), scope_basis)
        scope_metrics_basis = "scope_total"
        scope_result = scope_basis
    elif cost_known_rows:
        scope_metrics = _derived_metrics(_period_inputs(cost_known_rows, pool_basis), pool_basis)
        scope_metrics_basis = "examined_ads_with_cost_evidence"
        scope_result = pool_basis
    elif scope_rows:
        scope_metrics = _derived_metrics(_period_inputs(scope_rows, scope_basis), scope_basis)
        scope_metrics_basis = "scope_total"
        scope_result = scope_basis
    else:
        scope_metrics = _derived_metrics(_period_inputs(rows, {"status": "not_reported"}), {})
        scope_metrics_basis = "examined_ads"
        scope_result = {"status": "not_reported"}
    pooled = _pooled_costs(cost_known_rows, pool_basis)
    scope_cost = (
        _derived_metrics(_period_inputs(scope_rows, scope_basis), scope_basis)["cost_per_result"]
        if scope_rows and scope_basis["status"] == "single" else None
    )
    if cost_status == "read" and pool_basis["status"] in ("mixed", "not_reported"):
        missing_signals.extend(
            _result_signals(pool_basis, "ad", rows_exist=bool(cost_known_rows))
            if pool_basis["status"] == "not_reported" else [
                f"The examined ads report different Meta results ({', '.join(pool_basis['indicators'])}); each ad is "
                "compared only with cost for its own result."
            ]
        )

    ads: list[dict[str, Any]] = []
    for row in rows:
        has_cost = str(row.get("ad_id")) in cost_rows
        own = _result_basis([row]) if has_cost else {"status": "not_requested"}
        if has_cost and own["status"] == "not_reported" and pool_basis["status"] == "single":
            own = pool_basis
        inputs = _period_inputs([row], own)
        metrics = _derived_metrics(inputs, own)
        indicator = own.get("indicator") if own["status"] == "single" else None
        baseline: float | None = None
        baseline_basis: str | None = None
        if indicator is not None:
            if scope_basis.get("status") == "single" and scope_basis["indicator"] == indicator and scope_cost is not None:
                baseline, baseline_basis = scope_cost, "scope_total"
            elif indicator in pooled and pooled[indicator] is not None:
                baseline, baseline_basis = pooled[indicator], "examined_ads_with_same_result"
        rankings = {field: row.get(field) for field in RANKING_FIELDS}
        diagnosis, reason, below = _ranking_diagnosis(rankings, metrics, {"cost_per_result": baseline})
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
            "result_indicator": indicator,
            "results": metrics["results"],
            "cost_per_result": metrics["cost_per_result"],
            "cpm": metrics["cpm"],
            "ctr": metrics["ctr"],
            "cost_per_result_baseline": {"value": baseline, "basis": baseline_basis},
            "cost_per_result_vs_scope": _ratio(metrics["cost_per_result"], baseline),
            "cpm_vs_scope": _ratio(metrics["cpm"], scope_metrics["cpm"]),
            "diagnosis": diagnosis,
            "reason": reason,
        })
    return {
        "ads": ads,
        "rows_examined": len(rows),
        "missing_signals": missing_signals,
        "scope_metrics": scope_metrics,
        "scope_metrics_basis": scope_metrics_basis,
        "scope_result": _result_summary(scope_result) if scope_result["status"] != "not_requested" else None,
        "cost_evidence": cost_evidence,
        "examined": {
            "ads_examined": len(rows),
            "max_ads": max_ads,
            "selection": "highest_spend_first",
            "request_sort": list(RANKING_SORT),
            "request_fields": list(RANKING_LEAN_FIELDS),
            "meta_order_confirmed": order_confirmed,
            "more_ads_exist": more_ads_exist,
            "examined_spend": examined_spend,
            "scope_spend": scope_spend,
            "share_of_scope_spend": share,
            "note": note,
        },
    }


RANKING_MAX_ENTITIES = 5


def _ranking_findings(ads: list[dict[str, Any]], top_n: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Rank ads, pick weak ones, and build findings shared by single and multi-entity reports."""
    ranking_coverage: dict[str, int] = {"ads_examined": len(ads)}
    for field in RANKING_FIELDS:
        ranking_coverage[f"{field}_known"] = sum(1 for ad in ads if _known_ranking(ad["rankings"][field]))
    ranking_coverage["all_three_known"] = sum(1 for ad in ads if ad["ranking_points"] is not None)
    ranked = sorted(
        ads,
        key=lambda ad: (ad["ranking_points"] is None, ad["ranking_points"] or 0, -(ad["spend"] or 0.0), ad["ad_id"] or ""),
    )
    weak = sorted((ad for ad in ads if ad["diagnosis"]), key=lambda ad: (-(ad["spend"] or 0.0), ad["ad_id"] or ""))
    findings: list[dict[str, Any]] = []
    if ads and ranking_coverage["all_three_known"] < RANKING_EVIDENCE_MIN_SHARE * len(ads):
        limited = build_finding(
            "limited_ranking_evidence",
            (
                f"Only {ranking_coverage['quality_ranking_known']} of {len(ads)} examined ads have a known quality "
                f"ranking ({ranking_coverage['engagement_rate_ranking_known']} engagement-rate, "
                f"{ranking_coverage['conversion_rate_ranking_known']} conversion-rate; "
                f"{ranking_coverage['all_three_known']} have all three). Meta reports UNKNOWN when an ad has too "
                "little data, so this report rests on little evidence."
            ),
            severity="medium",
            confidence=None,
            evidence=[metric_evidence(
                "all_three_rankings_known_share",
                _ratio(ranking_coverage["all_three_known"], len(ads)),
                "ads with all three rankings known / ads examined",
                dict(ranking_coverage),
            )],
            next_actions=["Treat the diagnoses below as indicative only; widen the window for more ranked ads."],
        )
        limited["confidence_status"] = "uncalibrated"
        findings.append(limited)
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
                    "ad cost_per_result / baseline cost_per_result for the same Meta result",
                    {
                        "ad_cost_per_result": ad["cost_per_result"],
                        "baseline_cost_per_result": ad["cost_per_result_baseline"]["value"],
                        "baseline_basis": ad["cost_per_result_baseline"]["basis"],
                        "result_indicator": ad["result_indicator"],
                    },
                ),
                metric_evidence(
                    "cpm_vs_scope",
                    ad["cpm_vs_scope"],
                    "ad cpm / scope cpm",
                    {"ad_cpm": ad["cpm"], "ad_spend": ad["spend"]},
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
    if not weak:
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
    return ranked, weak, findings, ranking_coverage


@mcp_server.tool()
async def get_auction_ranking_report(
    level: str,
    object_id: str | None = None,
    date_preset: str | None = "last_30d",
    since: str | None = None,
    until: str | None = None,
    top_n: int = 10,
    max_ads: int = RANKING_DEFAULT_MAX_ADS,
    object_ids: StringList | None = None,
) -> dict[str, Any]:
    """Rank the ads under an account, campaign, ad set (or one ad) by Meta's quality, engagement-rate and conversion-rate rankings with spend and results, and say for each weak ad whether the evidence points at creative quality, the post-click path, or bid and budget. Results follow Meta's own result for each ad, and an ad's cost is compared only with cost for the same result. Reads only the max_ads highest-spending ads (default 200, at most 500) and reports what share of the scope's spend they cover. object_ids (up to 5 numeric campaign, ad set or ad ids) ranks the ads of several scopes together, max_ads shared between them. Rankings compare ads competing for the same audience."""
    resolved_level = _require_level(level, RANKING_REPORT_LEVELS)
    has_ids = object_ids is not None and coerce_csv_string_list(object_ids) not in ([], "")
    if has_ids:
        if blank_to_none(object_id):
            raise ValidationError("Give object_id or object_ids, not both.")
        if resolved_level == "account":
            raise ValidationError("object_ids take campaign, ad set or ad ids; call once per ad account.")
        entity_ids = _require_graph_ids(object_ids, limit=RANKING_MAX_ENTITIES)
    else:
        entity_ids = [_require_object_id(resolved_level, object_id)]
    if top_n < 1:
        raise ValidationError("top_n must be at least 1.")
    if not 1 <= max_ads <= RANKING_MAX_ADS_LIMIT:
        raise ValidationError(f"max_ads must be between 1 and {RANKING_MAX_ADS_LIMIT}.")
    window = diagnostics_tools._window_kwargs(
        date_preset=date_preset, since=since, until=until, default_date_preset="last_30d",
    )
    window_descriptor = diagnostics_tools._window_descriptor(date_preset, since, until, default_date_preset="last_30d")

    if not has_ids:
        resolved_object_id = entity_ids[0]
        report = await _ranking_entity(resolved_level, resolved_object_id, window, max_ads)
        ranked, weak, findings, ranking_coverage = _ranking_findings(report["ads"], top_n)
        return analysis_response(
            scope={"level": resolved_level, "object_id": resolved_object_id},
            metrics=report["scope_metrics"],
            findings=findings,
            missing_signals=report["missing_signals"],
            extra={
                "analyzed_level": "ad",
                "window": window_descriptor,
                "ads_analyzed": len(report["ads"]),
                "examined": report["examined"],
                "ranking_coverage": ranking_coverage,
                "cost_evidence": report["cost_evidence"],
                "scope_metrics": report["scope_metrics"],
                "scope_metrics_basis": report["scope_metrics_basis"],
                "scope_result": report["scope_result"],
                "ranked_ads": ranked[:top_n],
                "weak_ads": weak[:top_n],
                "weak_ad_count": len(weak),
                "unranked_ad_count": sum(1 for ad in report["ads"] if ad["ranking_points"] is None),
                "rules": RANKING_RULES,
                "results_definition": RESULTS_DEFINITION,
            },
        )

    per_entity_ads = max(1, max_ads // len(entity_ids))
    semaphore = asyncio.Semaphore(2)

    async def run(entity_id: str) -> tuple[str, dict[str, Any] | None, str | None]:
        async with semaphore:
            try:
                return entity_id, await _ranking_entity(resolved_level, entity_id, window, per_entity_ads), None
            except RateLimitError as exc:
                return entity_id, None, str(exc)
            except _ENTITY_READ_ERRORS as exc:
                return entity_id, None, str(_public_error(exc).get("message"))

    outcomes = await asyncio.gather(*(run(entity_id) for entity_id in entity_ids))
    all_ads: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    missing_signals: list[str] = []
    for entity_id, report, error in outcomes:
        if report is None:
            entities.append({"object_id": entity_id, "status": "unreadable", "error": error})
            missing_signals.append(f"{entity_id} could not be read ({error}); the other scopes are reported.")
            continue
        for ad in report["ads"]:
            ad["scope_object_id"] = entity_id
        all_ads.extend(report["ads"])
        entities.append({
            "object_id": entity_id,
            "status": "read",
            "examined": {key: report["examined"][key] for key in (
                "ads_examined", "more_ads_exist", "examined_spend", "scope_spend", "share_of_scope_spend",
                "meta_order_confirmed",
            )},
            "cost_evidence_status": report["cost_evidence"]["status"],
            "scope_metrics_basis": report["scope_metrics_basis"],
            "scope_result": report["scope_result"],
            "missing_signals": report["missing_signals"],
        })
    ranked, weak, findings, ranking_coverage = _ranking_findings(all_ads, top_n)
    unreadable = [entity["object_id"] for entity in entities if entity["status"] != "read"]
    return analysis_response(
        scope={"level": resolved_level, "object_ids": entity_ids},
        metrics={"ads_analyzed": len(all_ads)},
        findings=findings,
        missing_signals=missing_signals,
        extra={
            "analyzed_level": "ad",
            "window": window_descriptor,
            "ads_analyzed": len(all_ads),
            "entities": entities,
            "coverage": {
                "mode": "object_ids",
                "data_read": "one lean sorted read, one scope total and one cost read per scope",
                "max_ads_per_scope": per_entity_ads,
                "entities_requested": len(entity_ids),
                "entities_read": len(entity_ids) - len(unreadable),
                "entities_unreadable": unreadable,
                "complete": not unreadable and not any(
                    entity["examined"]["more_ads_exist"] for entity in entities if entity["status"] == "read"
                ),
            },
            "ranking_coverage": ranking_coverage,
            "ranked_ads": ranked[:top_n],
            "weak_ads": weak[:top_n],
            "weak_ad_count": len(weak),
            "unranked_ad_count": sum(1 for ad in all_ads if ad["ranking_points"] is None),
            "rules": RANKING_RULES,
            "results_definition": RESULTS_DEFINITION,
        },
    )


ACCOUNT_STATUS_LABELS = {
    1: "ACTIVE",
    2: "DISABLED",
    3: "UNSETTLED",
    7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT",
    9: "IN_GRACE_PERIOD",
    100: "PENDING_CLOSURE",
    101: "CLOSED",
    201: "ANY_ACTIVE",
    202: "ANY_CLOSED",
}
DISABLE_REASON_LABELS = {
    0: "NONE",
    1: "ADS_INTEGRITY_POLICY",
    2: "ADS_IP_REVIEW",
    3: "RISK_PAYMENT",
    4: "GRAY_ACCOUNT_SHUT_DOWN",
    5: "ADS_AFC_REVIEW",
    6: "BUSINESS_INTEGRITY_RAR",
    7: "PERMANENT_CLOSE",
    8: "UNUSED_RESELLER_ACCOUNT",
    9: "UNUSED_ACCOUNT",
    10: "UMBRELLA_AD_ACCOUNT",
    11: "BUSINESS_MANAGER_INTEGRITY_POLICY",
    12: "MISREPRESENTED_AD_ACCOUNT",
    13: "AOAB_DESHARE_LEGAL_ENTITY",
    14: "CTX_THREAD_REVIEW",
    15: "COMPROMISED_AD_ACCOUNT",
}
CONTEXT_ACCOUNT_FIELDS = [
    "id",
    "name",
    "account_status",
    "disable_reason",
    "currency",
    "timezone_name",
    "business",
    "amount_spent",
    "spend_cap",
    "created_time",
]
CONTEXT_INSIGHTS_FIELDS = [
    "campaign_id",
    "adset_id",
    "adset_name",
    "objective",
    "optimization_goal",
    "date_start",
    "date_stop",
    "spend",
    "impressions",
    "clicks",
    "actions",
    "action_values",
]
PURCHASE_ACTION_TYPES = [
    "purchase",
    "omni_purchase",
    "offsite_conversion.purchase",
    "offsite_conversion.fb_pixel_purchase",
    "onsite_conversion.purchase",
]
LEAD_ACTION_TYPES = ["lead", "onsite_conversion.lead", "offsite_conversion.fb_pixel_lead"]
FUNNEL_STEPS: list[tuple[str, list[str]]] = [
    ("link_click", ["link_click"]),
    ("landing_page_view", ["landing_page_view", "omni_landing_page_view"]),
    ("view_content", ["view_content", "omni_view_content", "offsite_conversion.fb_pixel_view_content"]),
    ("add_to_cart", ["add_to_cart", "omni_add_to_cart", "offsite_conversion.fb_pixel_add_to_cart"]),
    (
        "initiate_checkout",
        ["initiate_checkout", "omni_initiated_checkout", "offsite_conversion.fb_pixel_initiate_checkout"],
    ),
    ("purchase", PURCHASE_ACTION_TYPES),
]
LEARNING_EVENTS_PER_WEEK = 50
# Each optimization goal maps to the action types of the event it optimizes for. In every row the first
# reported alias is counted. Goals whose event cannot be identified from ad set insights are not assessed.
GOAL_EVENT_BASIS: dict[str, tuple[list[str], str]] = {
    "LEAD_GENERATION": (
        LEAD_ACTION_TYPES,
        "lead actions (the first of lead, onsite_conversion.lead, offsite_conversion.fb_pixel_lead in each row)",
    ),
    "LINK_CLICKS": (["link_click"], "link_click actions"),
    "LANDING_PAGE_VIEWS": (["landing_page_view", "omni_landing_page_view"], "landing_page_view actions"),
}
_PROMOTED_EVENT_REASON = (
    "The event this goal optimizes for is the conversion event set in each ad set's promoted_object, which ad set "
    "insights do not report; counting purchases or leads instead would judge a proxy, so the goal is not assessed."
)
GOAL_EVENT_UNIDENTIFIED: dict[str, str] = {
    "OFFSITE_CONVERSIONS": _PROMOTED_EVENT_REASON,
    "VALUE": _PROMOTED_EVENT_REASON,
    "QUALITY_LEAD": (
        "This goal optimizes for leads Meta predicts are higher quality; insights do not report those apart from "
        "all leads, so the goal is not assessed."
    ),
}
CONTEXT_EDGE_SECTIONS: list[tuple[str, str, list[str]]] = [
    ("datasets", "adspixels", ["id", "name", "last_fired_time", "is_unavailable"]),
    ("custom_conversions", "customconversions", ["id", "name", "custom_event_type", "last_fired_time", "is_archived"]),
    ("pages", "promote_pages", ["id", "name", "category"]),
    ("instagram_accounts", "instagram_accounts", ["id", "username"]),
]
CONTEXT_EDGE_LIMIT = 25
CONTEXT_INSIGHTS_PAGE_LIMIT = 250


def _code_label(value: Any, labels: dict[int, str]) -> dict[str, Any] | None:
    """Return a numeric Meta code with its documented label."""
    code = _number(value)
    if code is None:
        return None
    return {"code": code, "label": labels.get(int(code), "UNKNOWN")}


def _public_error(exc: Exception) -> dict[str, Any]:
    """Return a caller-safe description of a section read failure."""
    if isinstance(exc, MetaApiError):
        return exc.to_public_dict()
    return {"type": type(exc).__name__, "message": str(exc)}


async def _optional_edge_section(client: Any, account_id: str, edge: str, fields: list[str]) -> dict[str, Any]:
    """Read the first page of an account edge, reporting failures instead of an empty list."""
    try:
        payload = await client.list_objects(account_id, edge, fields=fields, params={"limit": CONTEXT_EDGE_LIMIT})
    except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
        return {"available": False, "edge": edge, "items": None, "count": None, "complete": None, "error": _public_error(exc)}
    items = [item for item in payload.get("data", []) or [] if isinstance(item, dict)]
    return {
        "available": True,
        "edge": edge,
        "items": items,
        "count": len(items),
        "complete": not bool(extract_paging(payload).get("next")),
        "page_limit": CONTEXT_EDGE_LIMIT,
    }


def _known_total_spend(rows: list[dict[str, Any]]) -> float | None:
    """Sum spend across rows; a row without spend makes the total unknown rather than smaller."""
    values = [to_float((row.get("metrics") or {}).get("spend")) for row in rows]
    return None if any(value is None for value in values) else sum(values)  # type: ignore[arg-type]


def _group_spend(
    rows: list[dict[str, Any]], key: str, total_spend: float | None, *, result_error: str | None = None,
) -> list[dict[str, Any]]:
    """Group ad set rows by a dimension, with spend, share, and Meta's result where the group has one.

    A row without spend makes its group's spend (and share) null. Results are added up only when every ad set in
    the group that reports a result reports the same action result; otherwise they are null and the group lists
    the result types it mixes.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "UNKNOWN"), []).append(row)
    output: list[dict[str, Any]] = []
    for label, group_rows in grouped.items():
        basis = _result_basis(group_rows, result_error=result_error)
        inputs = _period_inputs(group_rows, basis)
        spend = _known_total_spend(group_rows)
        group: dict[str, Any] = {
            key: label,
            "spend": spend,
            "adset_count": len(group_rows),
            "spend_share": _ratio(spend, total_spend),
            "result_indicator": basis.get("indicator") if basis["status"] == "single" else None,
            "results": inputs["results"],
            "cost_per_result": _derived_metrics(inputs, basis)["cost_per_result"],
        }
        if basis["status"] == "mixed":
            group["result_indicators"] = basis["indicators"]
        output.append(group)
    return sorted(output, key=lambda group: (group["spend"] is None, -(group["spend"] or 0.0), group[key]))


FUNNEL_EVENT_FAMILIES: list[tuple[str, list[str]]] = [*FUNNEL_STEPS, ("lead", LEAD_ACTION_TYPES)]


def _sum_actions(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Total each funnel event family: per row take the first alias Meta reports, then sum across rows.

    Choosing the alias per row keeps rows that report a different alias of the same event, and
    still counts an event only once in a row that reports several aliases of it.
    """
    totals: dict[str, dict[str, Any]] = {
        family: {"count": None, "action_types_used": []} for family, _ in FUNNEL_EVENT_FAMILIES
    }
    for row in rows:
        actions = row.get("actions_map") or {}
        for family, candidates in FUNNEL_EVENT_FAMILIES:
            matched = next((candidate for candidate in candidates if candidate in actions), None)
            if matched is None:
                continue
            total = totals[family]
            total["count"] = (total["count"] or 0.0) + (to_float(actions[matched]) or 0.0)
            if matched not in total["action_types_used"]:
                total["action_types_used"].append(matched)
    for family, candidates in FUNNEL_EVENT_FAMILIES:
        totals[family]["action_types_used"].sort(key=candidates.index)
    return totals


def _funnel(family_totals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build funnel step counts and drop-off between adjacent steps that have data."""
    steps = []
    for step, candidates in FUNNEL_STEPS:
        total = family_totals[step]
        steps.append({
            "step": step,
            "count": _number(total["count"]),
            "action_types_used": list(total["action_types_used"]),
            "action_types_checked": candidates,
        })
    present = [step for step in steps if step["count"] is not None]
    drop_off = []
    for previous, current in zip(present, present[1:]):
        rate = _ratio(current["count"], previous["count"])
        drop_off.append({
            "from": previous["step"],
            "to": current["step"],
            "from_count": previous["count"],
            "to_count": current["count"],
            "drop_off": None if rate is None else 1 - rate,
            "formula": "1 - to_count / from_count",
        })
    lead = family_totals["lead"]
    return {
        "steps": steps,
        "drop_off": drop_off,
        "lead_events": {"count": _number(lead["count"]), "action_types_used": list(lead["action_types_used"])},
        "note": (
            "In each ad set row, a step uses the first matching action type Meta reports, so the same event is not "
            "counted twice; the chosen counts are then summed across rows, so rows reporting different aliases of "
            "one event all count. Steps with no data are skipped when computing drop-off. Counts use Meta attribution."
        ),
    }


GOAL_VOLUME_TOP_ADSETS = 3


def _adset_volume_entry(row: dict[str, Any], events: float | None, per_week: float | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "adset_id": row.get("adset_id"),
        "adset_name": row.get("adset_name"),
        "spend": to_float((row.get("metrics") or {}).get("spend")),
    }
    if events is not None:
        entry["events"] = events
        entry["events_per_week"] = per_week
    return entry


def _top_by_spend(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        entries, key=lambda entry: (entry["spend"] is None, -(entry["spend"] or 0.0), str(entry["adset_id"] or "")),
    )
    return ordered[:GOAL_VOLUME_TOP_ADSETS]


_META_RESULT_EVENT_BASIS = (
    "Meta's own result for each ad set (the Insights results field), which counts the event the ad set "
    "optimizes for; an ad set whose row reports no result counts zero events"
)


def _meta_result_events(row: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    """An ad set's optimization events from Meta's result: (events, indicator, reason it cannot be judged)."""
    result = row.get("meta_result") or {"status": "absent"}
    if result["status"] == "absent":
        return 0.0, None, None
    if result["status"] == "mixed":
        return None, None, f"the ad set reports several results ({', '.join(result['indicators'])})"
    if result["status"] != "read":
        return None, None, "the ad set's result could not be read"
    if not _is_action_indicator(result["indicator"], row):
        return None, result["indicator"], f"Meta's result {result['indicator']} is not an event count"
    return (result["value"] or 0.0), result["indicator"], None


def _results_reported(rows: list[dict[str, Any]]) -> bool:
    return any((row.get("meta_result") or {}).get("status") in ("read", "mixed", "unreadable") for row in rows)


def _goal_volume(rows: list[dict[str, Any]], lookback_days: int) -> list[dict[str, Any]]:
    """Summarize, per optimization goal, how many ad sets reach the learning-phase weekly volume, with the top spenders.

    When Meta reports a result for any ad set, each ad set is judged on its own result. Otherwise only goals whose
    event is known from the goal itself (GOAL_EVENT_BASIS) are judged, on that action. Ad sets with zero spend are
    skipped; an ad set whose spend is missing is kept and makes its goal's spend null.
    """
    use_results = _results_reported(rows)
    by_goal: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        spend = to_float((row.get("metrics") or {}).get("spend"))
        if spend is not None and spend <= 0:
            continue
        by_goal.setdefault(str(row.get("optimization_goal") or "UNKNOWN"), []).append(row)
    output = []
    for goal in sorted(by_goal):
        goal_rows = by_goal[goal]
        goal_spend = _known_total_spend(goal_rows)
        unknown_spend = sum(1 for row in goal_rows if to_float((row.get("metrics") or {}).get("spend")) is None)
        mapping = GOAL_EVENT_BASIS.get(goal)
        if not use_results and mapping is None:
            output.append({
                "optimization_goal": goal,
                "adset_count": len(goal_rows),
                "spend": goal_spend,
                "adsets_with_unknown_spend": unknown_spend,
                "verdict": "not_assessed",
                "reason": GOAL_EVENT_UNIDENTIFIED.get(goal, "No event mapping is defined for this optimization goal."),
                "top_adsets_by_spend": _top_by_spend([_adset_volume_entry(row, None, None) for row in goal_rows]),
            })
            continue
        meeting: list[dict[str, Any]] = []
        below: list[dict[str, Any]] = []
        not_judged: list[dict[str, Any]] = []
        reasons: list[str] = []
        indicators: list[str] = []
        for row in goal_rows:
            if use_results:
                events, indicator, reason = _meta_result_events(row)
                if indicator and indicator not in indicators:
                    indicators.append(indicator)
            else:
                action_types, _ = mapping  # type: ignore[misc]
                actions = row.get("actions_map") or {}
                matched = next((candidate for candidate in action_types if candidate in actions), None)
                events, reason = ((to_float(actions.get(matched)) or 0.0) if matched else 0.0), None
            if events is None:
                not_judged.append(_adset_volume_entry(row, None, None))
                if reason and reason not in reasons:
                    reasons.append(reason)
                continue
            per_week = events / lookback_days * 7
            entry = _adset_volume_entry(row, events, per_week)
            (meeting if per_week >= LEARNING_EVENTS_PER_WEEK else below).append(entry)
        if not meeting and not below:
            output.append({
                "optimization_goal": goal,
                "adset_count": len(goal_rows),
                "spend": goal_spend,
                "adsets_with_unknown_spend": unknown_spend,
                "verdict": "not_assessed",
                "reason": "No ad set could be judged: " + "; ".join(reasons) + ".",
                "top_adsets_by_spend": _top_by_spend(not_judged),
            })
            continue
        verdict = (
            "all_adsets_meet_threshold" if not below
            else "some_adsets_meet_threshold" if meeting
            else "no_adsets_meet_threshold"
        )
        entry_out: dict[str, Any] = {
            "optimization_goal": goal,
            "event_basis": _META_RESULT_EVENT_BASIS if use_results else mapping[1],  # type: ignore[index]
            "threshold_events_per_week": LEARNING_EVENTS_PER_WEEK,
            "adset_count": len(goal_rows),
            "spend": goal_spend,
            "adsets_with_unknown_spend": unknown_spend,
            "adsets_meeting_threshold": len(meeting),
            "adsets_below_threshold": len(below),
            "verdict": verdict,
            "top_meeting_threshold": _top_by_spend(meeting),
            "top_below_threshold": _top_by_spend(below),
        }
        if use_results:
            entry_out["result_indicators"] = indicators[:5]
        if not_judged:
            entry_out["adsets_not_judged"] = len(not_judged)
            entry_out["not_judged_reasons"] = reasons
            entry_out["verdict"] = verdict if verdict != "all_adsets_meet_threshold" else "judged_adsets_meet_threshold"
        output.append(entry_out)
    return output


MAX_CONTEXT_OBJECT_IDS = 50


@mcp_server.tool()
async def get_advertiser_context(
    account_id: str,
    lookback_days: int = 30,
    object_ids: StringList | None = None,
) -> dict[str, Any]:
    """Briefing built from this account's data: account and business settings, spend by objective and optimization goal, funnel event volumes with drop-off, pixels/datasets and custom conversions, Pages and Instagram identities, and which optimization goals get about 50 events per ad set per week, judged on Meta's own result for each ad set. object_ids (up to 50 numeric campaign or ad set ids in this account) limits the spend, funnel and goal sections to those; account settings and the asset lists stay account-wide. Derived here; not Meta's own summary."""
    resolved_account_id = _require_account_id(account_id)
    if not 7 <= lookback_days <= 90:
        raise ValidationError("lookback_days must be between 7 and 90.")
    has_ids = object_ids is not None and coerce_csv_string_list(object_ids) not in ([], "")
    scope_ids = _require_graph_ids(object_ids, limit=MAX_CONTEXT_OBJECT_IDS) if has_ids else None
    client = get_graph_api_client()
    account = await client.get_object(resolved_account_id, fields=CONTEXT_ACCOUNT_FIELDS)
    today, reporting_timezone, timezone_signal = await _reporting_today(
        "account", resolved_account_id, account=account,
    )
    end = today - timedelta(days=1)
    start = end - timedelta(days=lookback_days - 1)
    params = insights_tools._insights_params(
        level="adset", since=start.isoformat(), until=end.isoformat(), limit=CONTEXT_INSIGHTS_PAGE_LIMIT,
    )
    (rows, result_error), *edge_sections = await asyncio.gather(
        _read_with_results(resolved_account_id, CONTEXT_INSIGHTS_FIELDS, params, level="adset"),
        *(
            _optional_edge_section(client, resolved_account_id, edge, fields)
            for _, edge, fields in CONTEXT_EDGE_SECTIONS
        ),
    )
    missing_signals: list[str] = [timezone_signal] if timezone_signal else []
    scope: dict[str, Any] = {"level": "account", "object_id": resolved_account_id}
    object_scope: dict[str, Any] | None = None
    if scope_ids is not None:
        wanted = set(scope_ids)
        account_rows = len(rows)
        rows = [
            row for row in rows
            if str(row.get("campaign_id") or "") in wanted or str(row.get("adset_id") or "") in wanted
        ]
        matched = {str(row.get(key)) for row in rows for key in ("campaign_id", "adset_id") if row.get(key)}
        unmatched = [value for value in scope_ids if value not in matched]
        scope["object_ids"] = scope_ids
        object_scope = {
            "object_ids": scope_ids,
            "matched_adset_rows": len(rows),
            "account_adset_rows_read": account_rows,
            "unmatched_object_ids": unmatched,
            "method": "one account-level ad set insights read, then kept ad set rows whose campaign_id or adset_id is listed",
            "account_wide_sections": ["account", "datasets", "custom_conversions", "pages", "instagram_accounts"],
        }
        if unmatched:
            missing_signals.append(
                "No ad set insights rows in the window matched " + ", ".join(unmatched)
                + " (not a campaign or ad set of this account, or no delivery in the window)."
            )
    currency = _supported_currency(account.get("currency"))
    money: dict[str, float | None] = {}
    for field in ("amount_spent", "spend_cap"):
        value = account.get(field)
        money[field] = from_minor_units(value, currency, field_name=field) if currency else None
    if currency is None:
        missing_signals.append("account currency: unknown or unsupported, so amount_spent and spend_cap are null.")
    business = account.get("business") if isinstance(account.get("business"), dict) else None

    total_spend = _known_total_spend(rows)
    if not rows:
        missing_signals.append("Meta returned no ad set insights rows for the lookback window.")
    elif total_spend is None:
        missing_signals.append(
            "At least one ad set row had no spend, so total spend and spend shares are null rather than computed "
            "from zero."
        )
    if result_error is not None:
        missing_signals.append(
            f"Meta rejected the request for its result fields ({result_error}), so ad set results are null and goal "
            "volume falls back to the goal's own event where that is known."
        )
    elif rows and not _results_reported(rows):
        missing_signals.append(
            "Meta reported no result for any ad set, so results are null and goal volume is judged only for goals "
            "whose event is known from the goal itself (LEAD_GENERATION, LINK_CLICKS, LANDING_PAGE_VIEWS)."
        )
    sections = {
        name: section for (name, _, _), section in zip(CONTEXT_EDGE_SECTIONS, edge_sections, strict=True)
    }
    section_errors = {name: section["error"] for name, section in sections.items() if not section["available"]}
    for name, error in section_errors.items():
        missing_signals.append(f"{name}: Meta returned an error, so this section is unknown ({error.get('message')}).")
    goal_volume = _goal_volume(rows, lookback_days)
    unassessed = [entry for entry in goal_volume if entry["verdict"] == "not_assessed"]
    if unassessed:
        missing_signals.append(
            "Weekly optimization event volume was not assessed for "
            + ", ".join(f"{entry['optimization_goal']} ({entry['adset_count']} ad sets)" for entry in unassessed)
            + " because the event each optimizes for could not be identified from ad set insights; see each "
            "goal's reason in optimization_goal_volume."
        )

    extra: dict[str, Any] = {
        "source": "derived",
        "derived_note": (
            "Built by this server from the account's own data. This is not Meta's own advertiser summary."
        ),
        "window": {"since": start.isoformat(), "until": end.isoformat(), "days": lookback_days},
        "reporting_timezone": reporting_timezone,
        "account": {
            "id": str(account.get("id") or resolved_account_id),
            "name": account.get("name"),
            "account_status": _code_label(account.get("account_status"), ACCOUNT_STATUS_LABELS),
            "disable_reason": _code_label(account.get("disable_reason"), DISABLE_REASON_LABELS),
            "currency": account.get("currency"),
            "timezone_name": account.get("timezone_name"),
            "business": (
                {"id": str(business.get("id")) if business.get("id") is not None else None, "name": business.get("name")}
                if business else None
            ),
            "amount_spent": money["amount_spent"],
            "spend_cap": money["spend_cap"],
            "money_units": f"major units of {currency}" if currency else None,
            "created_time": account.get("created_time"),
        },
        "spend_by_objective": _group_spend(rows, "objective", total_spend, result_error=result_error),
        "spend_by_optimization_goal": _group_spend(rows, "optimization_goal", total_spend, result_error=result_error),
        "funnel": _funnel(_sum_actions(rows)),
        "optimization_goal_volume": goal_volume,
        "optimization_goal_volume_rule": (
            f"An ad set meets the threshold when its events / lookback_days x 7 is at least "
            f"{LEARNING_EVENTS_PER_WEEK}, following Meta's learning-phase guidance of about 50 optimization "
            "events per ad set in a 7-day period. Events are Meta's own result for the ad set when Meta reports "
            "results; a result that is not an action count (for example reach) is not judged. Only ad sets with "
            f"spend in the window are counted. Each goal lists counts and the {GOAL_VOLUME_TOP_ADSETS} "
            "highest-spending ad sets that meet and that miss the threshold, not every ad set."
        ),
        **sections,
        "section_errors": section_errors,
        "results_definition": RESULTS_DEFINITION,
    }
    if object_scope is not None:
        extra["object_scope"] = object_scope
    return analysis_response(
        scope=scope,
        metrics={"spend": total_spend, "adset_rows": len(rows)},
        missing_signals=missing_signals,
        extra=extra,
    )


DELIVERY_LEVELS = ("account", "campaign", "adset", "ad")
DELIVERY_EDGES = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}
DELIVERY_OBJECT_FIELDS = {
    "campaign": ["id", "name", "status", "effective_status", "updated_time", "issues_info"],
    "adset": ["id", "name", "status", "effective_status", "updated_time", "campaign_id", "issues_info"],
    "ad": [
        "id", "name", "status", "effective_status", "updated_time", "campaign_id", "adset_id",
        "issues_info", "failed_delivery_checks", "ad_review_feedback",
    ],
}
# effective_status values sent as the documented effective_status filter on the
# account's campaigns, adsets and ads edges (as list_ads does). By default only
# problem states are requested, so a healthy account's scan is one short page.
DELIVERY_PROBLEM_STATUSES = {
    "campaign": ["WITH_ISSUES"],
    "adset": ["WITH_ISSUES"],
    "ad": ["DISAPPROVED", "PENDING_BILLING_INFO", "WITH_ISSUES"],
}
DELIVERY_ACTIVE_STATUSES = {
    "campaign": ["ACTIVE", "IN_PROCESS"],
    "adset": ["ACTIVE", "IN_PROCESS"],
    "ad": ["ACTIVE", "IN_PROCESS", "PENDING_REVIEW", "PREAPPROVED"],
}
DELIVERY_PAUSED_STATUSES = {
    "campaign": ["PAUSED"],
    "adset": ["PAUSED", "CAMPAIGN_PAUSED"],
    "ad": ["PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED"],
}
DELIVERY_ACCOUNT_FIELDS = ["id", "name", "account_status", "disable_reason", "currency"]
BLOCKING_EFFECTIVE_STATUSES = {"DISAPPROVED", "WITH_ISSUES", "PENDING_BILLING_INFO"}
PAUSED_EFFECTIVE_STATUSES = {"PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED"}
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
MAX_DELIVERY_SCAN = 2000
MAX_DELIVERY_PAGE = 100
# Pages are capped separately from records, so pages that return no records cannot keep the scan going.
MAX_DELIVERY_PAGES = 25
MAX_NAMED_OBJECTS = 50
DEFAULT_MAX_PROBLEMS = 15
MAX_PROBLEMS_LIMIT = 200
MAX_REASONS_PER_PROBLEM = 2
DELIVERY_TEXT_LIMIT = 160
# Recent spend comes from at most DELIVERY_SPEND_MAX_REQUESTS account-level insights pages at the
# scanned level, each lean (id and spend only) and sorted by spend, highest first; a per-object
# lookup cost one request per problem and hit Meta's rate limit on a large account.
DELIVERY_SPEND_PAGE = 200
DELIVERY_SPEND_MAX_REQUESTS = 2
DELIVERY_ID_FIELDS = {"campaign": "campaign_id", "adset": "adset_id", "ad": "ad_id"}
DELIVERY_SPEND_GROUPS = {"account": 0, "read": 1, "not_in_top_spenders": 2, "no_spend_in_window": 3, "spend_unavailable": 3}
DELIVERY_SEVERITY_POLICY = {
    "critical": "the ad account is not active (account_status other than ACTIVE or IN_GRACE_PERIOD), which stops all delivery",
    "high": (
        "account in grace period; or the object's effective_status is DISAPPROVED, WITH_ISSUES or "
        "PENDING_BILLING_INFO; or the ad has review feedback while not paused"
    ),
    "medium": "issues_info or failed_delivery_checks on an object that is not paused",
    "low": (
        "problems on an object whose effective_status is PAUSED, CAMPAIGN_PAUSED or ADSET_PAUSED; such objects are "
        "only reported when include_paused is true"
    ),
}


def _object_reasons(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect delivery problems Meta reports on one campaign, ad set or ad."""
    reasons: list[dict[str, Any]] = []
    for issue in item.get("issues_info") or []:
        if not isinstance(issue, dict):
            continue
        reasons.append({
            "source": "issues_info",
            "code": issue.get("error_code"),
            "summary": issue.get("error_summary"),
            "message": issue.get("error_message"),
            "error_type": issue.get("error_type"),
            "issue_level": issue.get("level"),
        })
    for check in item.get("failed_delivery_checks") or []:
        if not isinstance(check, dict):
            continue
        reasons.append({
            "source": "failed_delivery_checks",
            "code": check.get("check_name"),
            "summary": check.get("summary"),
            "message": check.get("description"),
            "extra_info": check.get("extra_info"),
        })
    feedback = item.get("ad_review_feedback")
    if isinstance(feedback, dict):
        global_feedback = feedback.get("global")
        if isinstance(global_feedback, dict):
            for reason, message in global_feedback.items():
                reasons.append({"source": "ad_review_feedback", "code": reason, "message": message})
        if feedback.get("placement_specific"):
            reasons.append({
                "source": "ad_review_feedback",
                "code": "placement_specific",
                "message": feedback.get("placement_specific"),
            })
    status = str(item.get("effective_status") or "").upper()
    if status in BLOCKING_EFFECTIVE_STATUSES and not reasons:
        reasons.append({"source": "effective_status", "code": status, "message": f"Meta reports effective_status {status}."})
    return reasons


def _object_severity(item: dict[str, Any], reasons: list[dict[str, Any]]) -> str:
    """Apply DELIVERY_SEVERITY_POLICY to one object."""
    status = str(item.get("effective_status") or "").upper()
    if status in BLOCKING_EFFECTIVE_STATUSES:
        return "high"
    if status in PAUSED_EFFECTIVE_STATUSES:
        return "low"
    if any(reason["source"] == "ad_review_feedback" for reason in reasons):
        return "high"
    return "medium"


def _short(value: Any) -> Any:
    """Cut long text; nested values (such as placement-specific review feedback) become cut JSON text."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, separators=(",", ":"), default=str)
    if isinstance(value, str) and len(value) > DELIVERY_TEXT_LIMIT:
        return value[:DELIVERY_TEXT_LIMIT] + "..."
    return value


def _compact_reasons(reasons: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop empty values, cut long text, and keep at most MAX_REASONS_PER_PROBLEM reasons."""
    compact = [
        {key: _short(value) for key, value in reason.items() if value is not None}
        for reason in reasons[:MAX_REASONS_PER_PROBLEM]
    ]
    return compact, max(0, len(reasons) - MAX_REASONS_PER_PROBLEM)


def _updated_timestamp(value: Any) -> float:
    """Parse Meta's updated_time (2026-11-01T10:00:00+0000); unknown values sort as oldest."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError):
        return float("-inf")


async def _delivery_spend(
    client: Any, account_id: str, level: str, params: dict[str, Any], wanted: set[str],
) -> dict[str, Any]:
    """Read recent spend per object at one level with at most two lean, spend-sorted insights pages. Never raises."""
    id_field = DELIVERY_ID_FIELDS[level]
    spend: dict[str, float] = {}
    requests = 0
    complete = False
    error: str | None = None
    after: str | None = None
    lowest: float | None = None
    order_confirmed = True
    while requests < DELIVERY_SPEND_MAX_REQUESTS:
        page_params = {**params, "limit": DELIVERY_SPEND_PAGE, "sort": ["spend_descending"]}
        if after:
            page_params["after"] = after
        requests += 1
        try:
            payload = await client.get_insights(account_id, fields=[id_field, "spend"], params=page_params)
        except Exception as exc:  # a spend failure must not hide the problems found
            error = str(_public_error(exc).get("message"))
            break
        for row in payload.get("data", []) or []:
            if not isinstance(row, dict) or row.get(id_field) is None:
                continue
            value = to_float(row.get("spend")) or 0.0
            if lowest is not None and value > lowest:
                order_confirmed = False
            lowest = value if lowest is None else min(lowest, value)
            object_id = str(row[id_field])
            spend[object_id] = spend.get(object_id, 0.0) + value
        paging = extract_paging(payload)
        if not paging.get("next"):
            complete = True
            break
        following = paging.get("after")
        if not following or following == after or wanted <= set(spend):
            break
        after = following
    return {
        "spend": spend,
        "requests": requests,
        "status": "failed" if error and not spend else "read",
        "complete": complete and error is None,
        "lowest_spend_read": lowest,
        "order_confirmed": order_confirmed,
        "error": error,
    }


def _account_problem(account: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    """Turn account status and disable reason into a problem entry when delivery is affected."""
    status = _code_label(account.get("account_status"), ACCOUNT_STATUS_LABELS)
    reason = _code_label(account.get("disable_reason"), DISABLE_REASON_LABELS)
    reasons: list[dict[str, Any]] = []
    severity: str | None = None
    if status is not None and status["code"] not in (1, 201):
        severity = "high" if status["code"] == 9 else "critical"
        reasons.append({"source": "account_status", "code": status["label"], "message": f"account_status {status['code']}"})
    if reason is not None and reason["code"] != 0:
        severity = severity or "high"
        reasons.append({"source": "disable_reason", "code": reason["label"], "message": f"disable_reason {reason['code']}"})
    if not reasons:
        return None
    return {
        "level": "account",
        "object_id": account_id,
        "name": account.get("name"),
        "effective_status": status["label"] if status else None,
        "severity": severity,
        "reasons": reasons,
    }


@mcp_server.tool()
async def get_delivery_errors(
    account_id: str,
    level: str = "ad",
    object_ids: StringList | None = None,
    limit: int = 500,
    after: str | None = None,
    spend_date_preset: str = "last_7d",
    include_active: bool = False,
    include_paused: bool = False,
    max_problems: int = DEFAULT_MAX_PROBLEMS,
) -> dict[str, Any]:
    """List delivery-blocking problems: account status and disable reason, plus issues_info, failed delivery checks and ad review feedback on campaigns, ad sets or ads, ordered by severity, then spend in spend_date_preset (one or two spend-sorted insights reads), then most recently updated. By default the scan asks Meta only for objects in problem states (disapproved, with issues, pending billing); include_active also scans active objects for warnings, include_paused reports paused ones. object_ids (up to 50) reads named objects instead. Returns up to max_problems problems with the total found."""
    resolved_account_id = _require_account_id(account_id)
    resolved_level = _require_level(level, DELIVERY_LEVELS)
    named_ids = [
        str(object_id).strip()
        for object_id in (coerce_csv_string_list(object_ids) or [])
        if str(object_id).strip()
    ]
    cursor = blank_to_none(after)
    if named_ids and resolved_level == "account":
        raise ValidationError("object_ids require level campaign, adset or ad.")
    if len(named_ids) > MAX_NAMED_OBJECTS:
        raise ValidationError(f"Provide at most {MAX_NAMED_OBJECTS} object_ids per call.")
    if named_ids and cursor:
        raise ValidationError("after continues an account scan; omit it when passing object_ids.")
    if not 1 <= limit <= MAX_DELIVERY_SCAN:
        raise ValidationError(f"limit must be between 1 and {MAX_DELIVERY_SCAN}.")
    if not 1 <= max_problems <= MAX_PROBLEMS_LIMIT:
        raise ValidationError(f"max_problems must be between 1 and {MAX_PROBLEMS_LIMIT}.")
    spend_params = insights_tools._insights_params(
        level=resolved_level, date_preset=spend_date_preset, limit=DELIVERY_SPEND_PAGE,
    )

    client = get_graph_api_client()
    account = await client.get_object(resolved_account_id, fields=DELIVERY_ACCOUNT_FIELDS)
    scanned: list[dict[str, Any]] = []
    scan: dict[str, Any]
    not_scanned: list[str] = []
    if resolved_level == "account":
        scan = {"mode": "account_only", "level": "account", "objects_examined": 0, "complete": True,
                "next_after": None, "stop_reason": None, "pages_fetched": 0, "limit": limit,
                "effective_status_filter": None}
    elif named_ids:
        fields = DELIVERY_OBJECT_FIELDS[resolved_level]
        unique_ids = list(dict.fromkeys(named_ids))
        scanned = list(await asyncio.gather(*(client.get_object(object_id, fields=fields) for object_id in unique_ids)))
        scan = {"mode": "named_objects", "level": resolved_level, "objects_examined": len(scanned), "complete": True,
                "next_after": None, "stop_reason": None, "pages_fetched": 0, "limit": limit,
                "effective_status_filter": None}
    else:
        statuses = list(DELIVERY_PROBLEM_STATUSES[resolved_level])
        if include_active:
            statuses += DELIVERY_ACTIVE_STATUSES[resolved_level]
        else:
            not_scanned.append(
                f"{resolved_level} objects in active states ({', '.join(DELIVERY_ACTIVE_STATUSES[resolved_level])}) "
                "were not scanned; Meta can attach warnings or review feedback to them. Pass include_active=true to "
                "scan them (slower on large accounts)."
            )
        if include_paused:
            statuses += DELIVERY_PAUSED_STATUSES[resolved_level]
        pages = 0
        complete = False
        next_after: str | None = None
        stop_reason: str | None = None
        seen_cursors: set[str] = {cursor} if cursor else set()
        while True:
            params: dict[str, Any] = {
                "limit": min(MAX_DELIVERY_PAGE, limit - len(scanned)),
                "effective_status": statuses,
            }
            if cursor:
                params["after"] = cursor
            payload = await client.list_objects(
                resolved_account_id, DELIVERY_EDGES[resolved_level],
                fields=DELIVERY_OBJECT_FIELDS[resolved_level], params=params,
            )
            pages += 1
            scanned.extend(item for item in payload.get("data", []) or [] if isinstance(item, dict))
            paging = extract_paging(payload)
            if not paging.get("next"):
                complete = True
                break
            following = paging.get("after")
            if not following or following in seen_cursors:
                stop_reason = "pagination_cursor_missing_or_repeated"
                break
            if len(scanned) >= limit:
                stop_reason = "limit_reached"
                next_after = following
                break
            if pages >= MAX_DELIVERY_PAGES:
                stop_reason = "page_cap_reached"
                next_after = following
                break
            seen_cursors.add(following)
            cursor = following
        scan = {"mode": "account_scan", "level": resolved_level, "objects_examined": len(scanned),
                "complete": complete, "next_after": next_after, "stop_reason": stop_reason,
                "pages_fetched": pages, "limit": limit, "effective_status_filter": statuses}
    scan["paused_included"] = include_paused
    scan["active_included"] = include_active or scan["mode"] == "named_objects"

    problems: list[dict[str, Any]] = []
    account_problem = _account_problem(account, resolved_account_id)
    if account_problem:
        problems.append(account_problem)
    paused_skipped = 0
    for item in scanned:
        reasons = _object_reasons(item)
        if not reasons:
            continue
        if not include_paused and str(item.get("effective_status") or "").upper() in PAUSED_EFFECTIVE_STATUSES:
            paused_skipped += 1
            continue
        problem: dict[str, Any] = {
            "level": resolved_level,
            "object_id": str(item.get("id")) if item.get("id") is not None else None,
            "name": item.get("name"),
            "effective_status": item.get("effective_status"),
            "severity": _object_severity(item, reasons),
            "updated_time": item.get("updated_time"),
            "reasons": reasons,
        }
        for key in ("campaign_id", "adset_id"):
            if item.get(key) is not None:
                problem[key] = str(item[key])
        problems.append(problem)
    scan["paused_objects_skipped"] = paused_skipped
    scan["not_scanned"] = not_scanned

    object_problems = [problem for problem in problems if problem["level"] != "account"]
    spend_lookup: dict[str, Any] = {
        "method": (
            f"account-level insights at {resolved_level} level for {spend_params['date_preset']}, fields "
            f"{DELIVERY_ID_FIELDS.get(resolved_level, 'id')} and spend only, sorted by spend highest first, "
            f"{DELIVERY_SPEND_PAGE} rows per request, at most {DELIVERY_SPEND_MAX_REQUESTS} requests; "
            "objects without a row had no delivery in the window"
        ),
        "status": "not_needed",
        "requests": 0,
        "objects_with_spend_read": 0,
        "all_spending_objects_read": None,
        "lowest_spend_read": None,
        "meta_order_confirmed": None,
        "error": None,
    }
    if object_problems:
        read = await _delivery_spend(
            client, resolved_account_id, resolved_level, spend_params,
            {problem["object_id"] for problem in object_problems if problem["object_id"]},
        )
        spend_lookup.update({
            "status": read["status"],
            "requests": read["requests"],
            "objects_with_spend_read": len(read["spend"]),
            "all_spending_objects_read": read["complete"],
            "lowest_spend_read": read["lowest_spend_read"],
            "meta_order_confirmed": read["order_confirmed"],
            "error": _short(read["error"]),
        })
        for problem in object_problems:
            value = read["spend"].get(problem["object_id"] or "")
            if read["status"] == "failed":
                problem["spend"], problem["spend_status"] = None, "spend_unavailable"
            elif value:
                problem["spend"], problem["spend_status"] = value, "read"
            elif value is not None or read["complete"]:
                problem["spend"], problem["spend_status"] = 0.0, "no_spend_in_window"
            else:
                problem["spend"], problem["spend_status"] = None, "not_in_top_spenders"
    for problem in problems:
        if problem["level"] == "account":
            problem["spend"], problem["spend_status"] = None, "account"
    spend_failed = spend_lookup["status"] == "failed"
    problems.sort(key=lambda problem: (
        SEVERITY_RANK[problem["severity"]],
        DELIVERY_SPEND_GROUPS[problem["spend_status"]],
        -(problem["spend"] or 0.0),
        -_updated_timestamp(problem.get("updated_time")),
        problem["object_id"] or "",
    ))
    ordering = (
        ["severity", "account_first", "updated_time_desc", "object_id"] if spend_failed
        else ["severity", "account_first", "recent_spend_desc", "updated_time_desc", "object_id"]
    )
    ordering_note = (
        "Within each severity, an account problem comes first (it stops all delivery); then objects by most "
        "recently updated, because recent spend could not be read."
        if spend_failed else
        "Within each severity, an account problem comes first (it stops all delivery); then objects with recent "
        "spend, highest first; then objects whose spend was not among those read; then objects with no spend in "
        "the window; ties by most recently updated."
    )
    no_spend = sum(1 for problem in object_problems if problem["spend_status"] == "no_spend_in_window")
    beyond = sum(1 for problem in object_problems if problem["spend_status"] == "not_in_top_spenders")
    by_severity: dict[str, int] = {}
    for problem in problems:
        by_severity[problem["severity"]] = by_severity.get(problem["severity"], 0) + 1
    returned = problems[:max_problems]
    for problem in returned:
        problem["reasons"], omitted = _compact_reasons(problem["reasons"])
        if omitted:
            problem["reasons_omitted"] = omitted
        if isinstance(problem.get("name"), str):
            problem["name"] = _short(problem["name"])

    missing_signals: list[str] = []
    if not scan["complete"]:
        detail = (
            f"pass after={scan['next_after']!r} to continue" if scan["next_after"]
            else "Meta's pagination cursor was missing or repeated an earlier one, so the scan cannot continue"
        )
        cap = f" after the {MAX_DELIVERY_PAGES}-page cap" if scan["stop_reason"] == "page_cap_reached" else ""
        missing_signals.append(
            f"Scan incomplete: examined {scan['objects_examined']} {resolved_level}(s) in {scan['pages_fetched']} "
            f"page(s){cap} and more may exist; {detail}. Problems on objects not yet examined are not listed."
        )
    if len(problems) > max_problems:
        missing_signals.append(
            f"{len(problems)} problems were found; only the first {max_problems} in the stated ordering are listed. "
            f"Raise max_problems (up to {MAX_PROBLEMS_LIMIT}) or narrow with object_ids to see more."
        )
    if spend_failed:
        missing_signals.append(
            f"Recent spend could not be read ({spend_lookup['error']}), so problems are ordered by severity, then "
            "most recently updated, and their spend is null."
        )
    elif spend_lookup["error"]:
        missing_signals.append(
            f"The second spend page failed ({spend_lookup['error']}); spend beyond the first page is unknown."
        )
    if no_spend:
        missing_signals.append(
            f"{no_spend} of {len(object_problems)} problems had no spend in {spend_params['date_preset']}; "
            "they are listed after the problems with recent spend."
        )
    if beyond:
        missing_signals.append(
            f"{beyond} problem(s) were not among the {spend_lookup['objects_with_spend_read']} highest-spending "
            f"{resolved_level}s read; their spend is at most {spend_lookup['lowest_spend_read']} and is shown as null."
        )
    if spend_lookup["meta_order_confirmed"] is False:
        missing_signals.append("Meta returned the spend rows not ordered by spend, so spend coverage may be uneven.")

    if problems:
        counts = ", ".join(f"{by_severity[name]} {name}" for name in SEVERITY_RANK if name in by_severity)
        worst = problems[0]["severity"]
        findings = [{
            **build_finding(
                "delivery_problems_found",
                f"{len(problems)} delivery problem(s) found ({counts}); each is listed once in problems, "
                + (
                    "ordered by severity, then most recently updated (spend could not be read)."
                    if spend_failed else
                    "ordered by severity, then recent spend, then most recently updated."
                ),
                severity=worst,
                confidence=None,
            ),
            "confidence_status": "uncalibrated",
        }]
    else:
        findings = [{
            **build_finding(
                "no_delivery_problems_found",
                "No delivery problem was found in the objects examined.",
                severity="low",
                confidence=None,
            ),
            "confidence_status": "uncalibrated",
        }]

    return analysis_response(
        scope={"level": "account", "object_id": resolved_account_id},
        metrics={
            "problem_count": len(problems),
            "problems_returned": len(returned),
            "problems_truncated": len(problems) > len(returned),
            "by_severity": by_severity,
            "problems_without_recent_spend": no_spend,
        },
        findings=findings,
        missing_signals=missing_signals,
        extra={
            "account": {
                "id": str(account.get("id") or resolved_account_id),
                "name": account.get("name"),
                "account_status": _code_label(account.get("account_status"), ACCOUNT_STATUS_LABELS),
                "disable_reason": _code_label(account.get("disable_reason"), DISABLE_REASON_LABELS),
            },
            "problems": returned,
            "scan": scan,
            "spend_window": {"date_preset": spend_params["date_preset"]},
            "spend_lookup": spend_lookup,
            "ordering": ordering,
            "ordering_note": ordering_note,
            "severity_policy": DELIVERY_SEVERITY_POLICY,
        },
    )
