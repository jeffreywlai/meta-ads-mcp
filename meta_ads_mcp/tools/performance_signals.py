"""Reporting and troubleshooting tools: opportunity score, trends, anomalies, rankings, context, delivery errors."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
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
    "reach": {
        "formula": (
            "Meta's reach (accounts that saw the ads at least once, estimated by Meta) read with one query over "
            "exactly the period's days; never summed across days"
        ),
        "better": "neutral",
    },
}
REACH_POLICY = (
    "Reach counts unique accounts, so it does not add up across days: the reach of a week is not the sum of its "
    "days. A day's reach comes from that day's row; a week's or month's reach, and a multi-entity window total, "
    "come from one Meta query over exactly that period's days inside the window (the Insights time_ranges "
    "parameter). Reach is never summed here."
)
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


async def _history_pages(client: Any, account_id: str, params: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Read every page of the score history edge (bounded); return the rows and why the read stopped early."""
    rows: list[dict[str, Any]] = []
    page_params = dict(params)
    seen: set[str] = set()
    seen_pages: set[str] = set()
    unreadable = 0
    for _request in range(INSIGHTS_MAX_PAGES):
        payload = await client.list_objects(account_id, "opportunity_score_history", params=page_params)
        page_rows, page_unreadable = _page_rows(payload)
        if _seen_page(page_rows, seen_pages):
            return {"data": rows}, _stop_reason(PAGE_REPEATED, unreadable)
        rows.extend(page_rows)
        unreadable += page_unreadable
        paging = extract_paging(payload)
        if not paging.get("next"):
            return {"data": rows}, _stop_reason(None, unreadable)
        cursor = paging.get("after")
        if not cursor:
            return {"data": rows}, _stop_reason(CURSOR_MISSING, unreadable)
        if cursor in seen:
            return {"data": rows}, _stop_reason(CURSOR_REPEATED, unreadable)
        seen.add(cursor)
        page_params = {**params, "after": cursor}
    return {"data": rows}, _stop_reason(f"stopped after {INSIGHTS_MAX_PAGES} requests with more pages left", unreadable)


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
            payload, history_stopped = await _history_pages(client, resolved_account_id, params)
        except (MetaApiError, UnsupportedFeatureError) as exc:
            history = {"available": False, "window": window, "points": None, "error": _public_error(exc)}
            missing_signals.append(
                "opportunity_score_history: Meta returned an error for the history edge, so history is unknown "
                f"({_public_error(exc).get('message')}). Meta documents this edge on Graph API v27.0."
            )
        else:
            points = []
            dates_seen: set[str] = set()
            for raw in payload.get("data", []) or []:
                if not isinstance(raw, dict):
                    continue
                if raw.get("date") is not None:
                    # A page Meta repeats must not count its days twice.
                    if str(raw["date"]) in dates_seen:
                        continue
                    dates_seen.add(str(raw["date"]))
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
                "complete": history_stopped is None,
                "summary": _history_summary(points),
                "note": OPPORTUNITY_SCORE_HISTORY_NOTE,
            }
            if history_stopped is not None:
                history["incomplete_reason"] = history_stopped
                missing_signals.append(
                    f"opportunity_score_history could not be read to the last page ({history_stopped}), so the "
                    "history and its summary cover only the days read."
                )
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


def _unreadable(reason: str, indicator: str | None = None) -> dict[str, Any]:
    parsed: dict[str, Any] = {"status": "unreadable", "reason": reason}
    if indicator is not None:
        parsed["indicator"] = indicator
    return parsed


def _result_number(raw: Any) -> float | None:
    """A result value only when it is a finite, non-negative number (or a numeric string); never a boolean."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        number = float(raw)
    elif isinstance(raw, str):
        try:
            number = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _parse_result_values(indicator: str, values: Any) -> dict[str, Any]:
    """Pick the one value of an indicator's `values` list, or say why no single value can be chosen."""
    if values is None or values == []:
        return {"status": "read", "indicator": indicator, "value": None}
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        return _unreadable(f"{indicator}: values is not a list of objects", indicator)
    defaults: list[dict[str, Any]] = []
    for item in values:
        windows = item.get("attribution_windows")
        if windows is None:
            continue
        if not isinstance(windows, list) or not all(isinstance(window, str) for window in windows):
            return _unreadable(f"{indicator}: attribution_windows is not a list of window names", indicator)
        if "default" in windows:
            defaults.append(item)
    if len(values) == 1:
        chosen = values[0]
    elif len(defaults) == 1:
        chosen = defaults[0]
    elif defaults:
        return _unreadable(f"{indicator}: more than one value is marked as the default attribution window", indicator)
    else:
        return _unreadable(
            f"{indicator}: several attribution-window values and none is marked as the default", indicator,
        )
    raw = chosen.get("value")
    number = _result_number(raw)
    if number is None:
        if isinstance(raw, bool):
            detail = "a true or false value, not a number"
        elif raw is None:
            detail = "no value"
        else:
            detail = "not a finite number of zero or more"
        return _unreadable(f"{indicator}: the value is {detail}", indicator)
    return {"status": "read", "indicator": indicator, "value": number}


def _parse_result_list(value: Any) -> dict[str, Any]:
    """Read one of Meta's result fields: a list of {indicator, values: [{value, attribution_windows}]}.

    Live shape (confirmed): [{"indicator": "actions:<type>", "values": [{"value": "12",
    "attribution_windows": ["default"]}]}]. Returns a status: absent (no field or an empty list), read (one
    indicator with one unambiguous value; value None when Meta lists the indicator without values), mixed
    (several different indicators in one row), or unreadable with a `reason` (any other shape). A value is used
    only when it is unambiguous: one entry for the indicator, a finite non-negative number that is not a boolean,
    and either a single value or exactly one value in the default attribution window. Nothing is guessed.
    """
    if value is None:
        return {"status": "absent"}
    if not isinstance(value, list):
        return _unreadable("the results field is not a list")
    if not value:
        return {"status": "absent"}
    parsed: list[dict[str, Any]] = []
    seen: list[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            return _unreadable("an entry of the results field is not an object")
        indicator = entry.get("indicator")
        if not isinstance(indicator, str) or not indicator.strip():
            return _unreadable("an entry of the results field has no indicator name")
        indicator = indicator.strip()
        if indicator in seen:
            return _unreadable(f"{indicator}: the results field lists this indicator more than once", indicator)
        seen.append(indicator)
        item = _parse_result_values(indicator, entry.get("values"))
        if item["status"] == "unreadable":
            return item
        parsed.append(item)
    if len(parsed) > 1:
        return {"status": "mixed", "indicators": seen}
    return parsed[0]


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
        if cost["status"] == "unreadable":
            row["meta_cost_per_result_issue"] = f"cost_per_result: {cost['reason']}"
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
    seen_pages: set[str] = set()
    pages = 0
    while True:
        payload = await client.get_insights(object_id, fields=fields, params=page_params)
        pages += 1
        page_rows, unreadable = _page_rows(payload)
        if unreadable:
            raise ValidationError(
                f"Insights returned {unreadable} row(s) that could not be read, so totals built from them would be "
                "wrong. No partial result was returned."
            )
        if _seen_page(page_rows, seen_pages):
            raise ValidationError(
                "Insights pagination could not finish: Meta returned a page that had already been read. "
                "No partial result was returned."
            )
        rows.extend(_normalize_with_results({**payload, "data": page_rows}))
        paging = extract_paging(payload)
        if not paging.get("next"):
            return rows
        if len(rows) >= max_rows:
            raise ValidationError(
                f"Insights exceeded the {max_rows}-row scan limit; narrow the window or the number of objects. "
                "No partial result was returned."
            )
        if pages >= INSIGHTS_MAX_PAGES:
            raise ValidationError(
                f"Insights needed more than {INSIGHTS_MAX_PAGES} pages ({len(rows)} rows so far); narrow the window "
                "or the number of objects. No partial result was returned."
            )
        cursor = paging.get("after")
        if not cursor or cursor in seen:
            raise ValidationError("Insights pagination could not finish: missing or repeated cursor.")
        seen.add(cursor)
        page_params = {**params, "after": cursor}


ROW_BOUND_REACHED = "row_bound_reached"
CURSOR_MISSING = "Meta reported another page but gave no cursor to read it"
CURSOR_REPEATED = "Meta returned the same page cursor twice, so the read stopped"
PAGE_REPEATED = "Meta returned a page that had already been read, so it was not counted again and the read stopped"


def _page_rows(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """A page's rows that are objects, and how many entries could not be read.

    A data value that is not a list (or a payload that is not an object) counts as one unreadable entry: rows
    may have been there, so the page must not pass as empty.
    """
    if not isinstance(payload, dict):
        return [], 1
    data = payload.get("data")
    if data is None:
        return [], 0
    if not isinstance(data, list):
        return [], 1
    rows = [row for row in data if isinstance(row, dict)]
    return rows, len(data) - len(rows)


def _seen_page(rows: list[dict[str, Any]], seen_pages: set[str]) -> bool:
    """True when a page with exactly these rows was already read; otherwise remember it.

    Empty pages are not compared: they add nothing, and a cursor loop over them is caught by the cursor check.
    """
    if not rows:
        return False
    fingerprint = json.dumps(rows, sort_keys=True, default=str)
    if fingerprint in seen_pages:
        return True
    seen_pages.add(fingerprint)
    return False


def _unreadable_rows_reason(count: int) -> str:
    return f"Meta returned {count} row(s) that could not be read, so the rows read are incomplete"


def _stop_reason(stopped: str | None, unreadable: int) -> str | None:
    """Combine why a read stopped with any rows it could not read; either makes the read partial."""
    if not unreadable:
        return stopped
    reason = _unreadable_rows_reason(unreadable)
    if stopped is None:
        return reason
    if stopped == ROW_BOUND_REACHED:
        return f"{reason}; the row limit was also reached"
    return f"{reason}; {stopped}"


def _finite(value: Any) -> float | None:
    """A known, finite number, or None (missing, unreadable, true or false, NaN or infinite)."""
    if isinstance(value, bool):
        return None
    number = to_float(value)
    return number if number is not None and math.isfinite(number) else None


async def _read_bounded_pages(
    object_id: str, fields: list[str], params: dict[str, Any], *,
    max_pages: int = INSIGHTS_MAX_PAGES, max_rows: int | None = None,
    enough: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read one insights request page by page, keeping what was read when the read cannot finish.

    Returns the raw rows and None when Meta reported no further page (or `enough` says the rows already hold
    everything the caller needs), or the reason the read is partial: ROW_BOUND_REACHED once max_rows rows
    are read, a next page without a cursor, a repeated cursor, a page whose rows were already read (not added
    a second time), rows that could not be read, or the request cap. Callers label the result partial instead
    of failing.
    """
    client = get_graph_api_client()
    rows: list[dict[str, Any]] = []
    page_params = dict(params)
    seen: set[str] = set()
    seen_pages: set[str] = set()
    unreadable = 0
    for _request in range(max_pages):
        payload = await client.get_insights(object_id, fields=fields, params=page_params)
        page_rows, page_unreadable = _page_rows(payload)
        if _seen_page(page_rows, seen_pages):
            return rows, _stop_reason(PAGE_REPEATED, unreadable)
        rows.extend(page_rows)
        unreadable += page_unreadable
        paging = extract_paging(payload)
        if not paging.get("next") or (enough is not None and enough(rows)):
            return rows, _stop_reason(None, unreadable)
        if max_rows is not None and len(rows) >= max_rows:
            return rows, _stop_reason(ROW_BOUND_REACHED, unreadable)
        cursor = paging.get("after")
        if not cursor:
            return rows, _stop_reason(CURSOR_MISSING, unreadable)
        if cursor in seen:
            return rows, _stop_reason(CURSOR_REPEATED, unreadable)
        seen.add(cursor)
        page_params = {**params, "after": cursor}
    return rows, _stop_reason(f"stopped after {max_pages} requests with more pages left", unreadable)


def _covers(key: str, ids: list[str]) -> Callable[[list[dict[str, Any]]], bool]:
    """True once the rows read hold a row for every one of ids (by the row's `key` field)."""
    wanted = set(ids)
    return lambda rows: wanted <= {str(row.get(key) or "") for row in rows}


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
    fields = [*DAILY_INSIGHTS_FIELDS, *(field for field in (extra_fields or []) if field not in DAILY_INSIGHTS_FIELDS)]
    rows, result_error = await _read_with_results(object_id, fields, params, level=rows_level, max_rows=max_rows)
    rows = [row for row in rows if row.get("date_start")]
    rows.sort(key=lambda row: str(row["date_start"]))
    return rows, result_error


# Reach over a period comes from one query per period (Meta's documented `time_ranges`: "Each time range will
# have one result set"). Meta documents no maximum number of ranges, so they are sent in small batches.
REACH_RANGES_PER_REQUEST = 25
_ReachKey = tuple[str | None, str, str]


async def _reach_for_ranges(
    object_id: str, row_level: str, ranges: list[tuple[date, date]], *, id_field: str | None = None,
    ids: list[str] | None = None,
) -> tuple[dict[_ReachKey, float | None], str | None]:
    """Read Meta's reach for each (since, until) range with one query per range, never summing days.

    With id_field and ids the query is at the children's level, filtered to those ids (adset.id IN style), and
    the result is keyed by child id; otherwise the key's id is None. Never raises: a failure returns the error,
    and every reach is then unknown.
    """
    unique = sorted(set(ranges))
    found: dict[_ReachKey, float | None] = {}
    fields = ["reach", "date_start", "date_stop", *([id_field] if id_field else [])]
    for offset in range(0, len(unique), REACH_RANGES_PER_REQUEST):
        batch = unique[offset:offset + REACH_RANGES_PER_REQUEST]
        params: dict[str, Any] = {
            "level": row_level,
            "limit": INSIGHTS_PAGE_LIMIT,
            "use_unified_attribution_setting": "true",
            "time_ranges": [{"since": since.isoformat(), "until": until.isoformat()} for since, until in batch],
        }
        if id_field and ids:
            params["filtering"] = [{"field": f"{row_level}.id", "operator": "IN", "value": list(ids)}]
        try:
            rows = await _read_insights_pages(
                object_id, fields, params, max_rows=len(batch) * max(1, len(ids or [])) + INSIGHTS_PAGE_LIMIT,
            )
        except (*_ENTITY_READ_ERRORS, RateLimitError) as exc:
            return {}, str(_public_error(exc).get("message"))
        wanted = set(ids or [])
        for row in rows:
            key_id = str(row.get(id_field) or "") if id_field else None
            if id_field and key_id not in wanted:
                continue
            key = (key_id, str(row.get("date_start") or ""), str(row.get("date_stop") or ""))
            found[key] = to_float(row.get("reach"))
    return found, None


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


MAX_UNREADABLE_REASONS = 3


def _result_basis(
    rows: list[dict[str, Any]], *, result_error: str | None = None, one_entity: bool = True,
) -> dict[str, Any]:
    """Decide which result the rows report, and whether it can be added up across rows.

    Statuses: single (one indicator), mixed (several), not_reported (no row reports a result), unreadable (rows
    report a result in a shape that cannot be read, with the reasons), or request_failed (Meta rejected the
    result fields). one_entity says the rows all belong to one object (its days): only then does a row without a
    result count as zero, because the object's own other rows define its result. Rows of different objects
    (ad sets in a group, ads in a pool) never lend their result to an object that reports none.
    """
    if result_error is not None:
        return {"status": "request_failed", "error": result_error}
    indicators: list[str] = []
    mixed = False
    unreadable = 0
    reasons: list[str] = []
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
            reason = result.get("reason") or "the results field has an unexpected shape"
            if reason not in reasons:
                reasons.append(reason)
        issue = row.get("meta_cost_per_result_issue")
        if issue and issue not in reasons:
            reasons.append(issue)
    reasons = reasons[:MAX_UNREADABLE_REASONS]
    if mixed or len(indicators) > 1:
        basis: dict[str, Any] = {"status": "mixed", "indicators": indicators}
        if reasons:
            basis["unreadable_reasons"] = reasons
        return basis
    if not indicators:
        if unreadable:
            return {"status": "unreadable", "rows_unreadable": unreadable, "unreadable_reasons": reasons}
        return {"status": "not_reported", **({"unreadable_reasons": reasons} if reasons else {})}
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
        "absent_counts_zero": one_entity,
        **({"unreadable_reasons": reasons} if reasons else {}),
    }


def _row_result(row: dict[str, Any], basis: dict[str, Any]) -> float | None:
    """One row's count of the basis result.

    Absent counts as zero only when the basis comes from the same object's own rows; unreadable is unknown.
    """
    if basis.get("status") != "single":
        return None
    result = row.get("meta_result") or {"status": "absent"}
    if result["status"] == "absent":
        return 0.0 if basis.get("absent_counts_zero", False) else None
    if result["status"] == "read" and result["indicator"] == basis["indicator"]:
        return 0.0 if result["value"] is None else result["value"]
    return None


def _period_inputs(
    rows: list[dict[str, Any]], basis: dict[str, Any], *, with_reach: bool = False,
) -> dict[str, float | None]:
    """Sum additive inputs under INPUT_POLICY for the rows of one period.

    Reach is never summed: with_reach takes it only from a single row (one day); longer periods get their reach
    from a query over the whole period (see _reach_for_ranges).
    """
    totals: dict[str, float | None] = {}
    for name, source in _DELIVERY_INPUT_SOURCES.items():
        values = [_finite((row.get("metrics") or {}).get(source)) for row in rows]
        totals[name] = None if not values or any(value is None for value in values) else sum(values)  # type: ignore[arg-type]
    results: float | None = None
    conversion_value: float | None = None
    meta_cost: float | None = None
    if basis.get("status") == "single" and rows:
        counts = [_row_result(row, basis) for row in rows]
        if basis["additive"]:
            results = None if any(count is None for count in counts) else sum(counts)  # type: ignore[arg-type]
            if basis["value_reported"] and (results is not None or basis.get("absent_counts_zero")):
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
    if with_reach:
        totals["reach"] = to_float(rows[0].get("reach")) if len(rows) == 1 else None
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
        "reach": inputs.get("reach"),
    }


def _result_summary(basis: dict[str, Any]) -> dict[str, Any]:
    """The result definition actually used for one object, for the response."""
    summary: dict[str, Any] = {"source": "meta_results_field", "status": basis["status"]}
    for key in ("indicator", "action_type", "additive", "indicators", "error", "unreadable_reasons"):
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
    reasons = basis.get("unreadable_reasons") or []
    why = f" ({'; '.join(reasons)})" if reasons else ""
    if status == "unreadable":
        return [
            f"Meta's results field could not be read in this window{why}, so the result metrics are null. No "
            "value was guessed from it."
        ]
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
            f"{basis['rows_unreadable']} row(s) had a result that could not be read{why}; their periods' results "
            "are null."
        )
    elif reasons:
        signals.append(f"Meta's cost per result could not be read in some rows{why}; it is null there.")
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
    rows, unreadable = _page_rows(payload)
    children: list[dict[str, Any]] = []
    for row in rows:
        if row.get(id_field) is None:
            # A row without its id may be a child that was not selected, so the selection is not all of them.
            unreadable += 1
        elif str(row[id_field]) not in {child["object_id"] for child in children}:
            children.append({"object_id": str(row[id_field]), "name": row.get(name_field), "spend": _finite(row.get("spend"))})
    return children[:max_entities], bool(extract_paging(payload).get("next")) or unreadable > 0


async def _read_entities_one_by_one(
    level: str, ids: list[str], start: date, end: date, *, extra_fields: tuple[str, ...] = (),
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
                rows, result_error = await _daily_rows(
                    level, entity_id, start, end, extra_fields=[name_field, *extra_fields],
                )
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
    *, extra_fields: tuple[str, ...] = (),
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
            child_level, object_id, start, end, row_level=child_level, extra_fields=[id_field, *extra_fields],
            filtering=[{"field": f"{child_level}.id", "operator": "IN", "value": ids}],
            max_rows=len(ids) * days,
        )
    except RateLimitError:
        raise
    except _ENTITY_READ_ERRORS as exc:
        error = str(_public_error(exc).get("message"))
        entities = await _read_entities_one_by_one(child_level, ids, start, end, extra_fields=extra_fields)
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
    targets: dict[str, Any], level: str, start: date, end: date, *, extra_fields: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the daily rows of every entity a multi-entity call covers, with coverage."""
    coverage: dict[str, Any] = {"mode": targets["mode"]}
    if targets["mode"] == "object_ids":
        ids = targets["ids"]
        read = await _read_entities_one_by_one(level, ids, start, end, extra_fields=extra_fields)
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
        read, method = (await _read_children(
            targets["object_id"], entity_level, children, start, end, extra_fields=extra_fields,
        )) if ids else ({}, {
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
    if coverage.get("selection") is not None and coverage["entities_requested"] == 0:
        signals.append(
            f"Meta returned no {coverage.get('child_level', 'child')} with insights in the window, so there is "
            "nothing to report."
        )
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


def _group_entity_signals(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move each entity's missing signals into one list, each signal once with the entities it applies to."""
    grouped: dict[str, list[str]] = {}
    for entity in entities:
        for signal in entity.pop("missing_signals", []) or []:
            grouped.setdefault(signal, []).append(entity["object_id"])
    return [{"signal": signal, "object_ids": ids} for signal, ids in grouped.items()]


def _compact_result(basis: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"status": basis["status"]}
    for key in ("indicator", "additive", "indicators", "error", "unreadable_reasons"):
        if key in basis:
            compact[key] = basis[key]
    return compact


def _multi_scope(level: str, targets: dict[str, Any]) -> dict[str, Any]:
    if targets["mode"] == "object_ids":
        return {"level": level, "object_ids": list(targets["ids"])}
    return {"level": level, "object_id": targets["object_id"], "child_level": targets["child_level"]}


def _trend_periods(
    rows: list[dict[str, Any]], basis: dict[str, Any], start: date, end: date, granularity: str,
    metric_names: list[str], *, with_reach: bool = False,
) -> list[dict[str, Any]]:
    """Group daily rows into periods, sum inputs under INPUT_POLICY, and derive each metric.

    With reach, a day takes its own row's reach; a week or month gets None here and is filled from a query over
    that period (_fill_period_reach), never from a sum of days.
    """
    buckets: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        day = _parse_date(str(row["date_start"]), field="date_start")
        buckets.setdefault(_period_bounds(day, granularity)[0], []).append(row)
    periods: list[dict[str, Any]] = []
    for period_start in sorted(buckets):
        _, period_end = _period_bounds(period_start, granularity)
        inputs = _period_inputs(buckets[period_start], basis, with_reach=with_reach)
        if with_reach and granularity != "day":
            inputs["reach"] = None
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


def _clipped_range(period: dict[str, Any], start: date, end: date) -> tuple[date, date]:
    """The days of a period that fall inside the window."""
    return (
        max(date.fromisoformat(period["period_start"]), start),
        min(date.fromisoformat(period["period_end"]), end),
    )


def _reach_ranges(periods: list[dict[str, Any]], start: date, end: date, granularity: str, *, window: bool) -> list[tuple[date, date]]:
    """The ranges whose reach needs its own query: each multi-day period, and the window for totals."""
    ranges = [] if granularity == "day" else [_clipped_range(period, start, end) for period in periods]
    if window:
        ranges.append((start, end))
    return ranges


def _fill_period_reach(
    periods: list[dict[str, Any]], reach: dict[_ReachKey, float | None], key_id: str | None, start: date, end: date,
) -> int:
    """Set each multi-day period's reach from its own query; return how many periods Meta gave no reach for."""
    missing = 0
    for period in periods:
        since, until = _clipped_range(period, start, end)
        value = reach.get((key_id, since.isoformat(), until.isoformat()))
        if value is None:
            missing += 1
        period["inputs"]["reach"] = value
        if "reach" in period["metrics"]:
            period["metrics"]["reach"] = value
    return missing


def _reach_signals(error: str | None, missing_periods: int, *, totals: bool = False) -> list[str]:
    signals = []
    where = "weeks or months" + (" and the window total" if totals else "")
    if error is not None:
        signals.append(
            f"Reach for {where} could not be read ({error}), so it is null there; it is never summed from days."
        )
    elif missing_periods:
        signals.append(
            f"Meta returned no reach for {missing_periods} period(s) read with their own query, so reach is null there."
        )
    return signals


_COMPACT_CHANGE_KEYS = ("first", "last", "first_period", "last_period", "pct_change", "direction", "assessment")
TREND_METHOD = (
    "Daily rows are read with time_increment=1, additive inputs are summed per period under input_policy, "
    "and each metric is divided from those sums. Direction compares the first and last periods with a "
    "value; partial periods are flagged and are not scaled."
)
# A multi-entity trend returns each entity's periods as columns aligned to one shared list of period starts.
# By default the most recent periods that keep the whole response under SERIES_TARGET_BYTES are returned;
# series_periods asks for a number of periods, still capped so the response stays under SERIES_MAX_BYTES (this
# server moves responses above about 63 KB to an overflow artifact, which the default never relies on).
SERIES_TARGET_BYTES = 19_000
SERIES_MAX_BYTES = 55_000


def _window_period_starts(start: date, end: date, granularity: str) -> list[date]:
    starts: list[date] = []
    current = _period_bounds(start, granularity)[0]
    while current <= end:
        starts.append(current)
        current = _period_bounds(current, granularity)[1] + timedelta(days=1)
    return starts


SERIES_SIGNIFICANT_FIGURES = 6


def _series_value(value: Any) -> Any:
    """A series value rounded to six significant figures, to keep the response small; totals stay exact."""
    if isinstance(value, float) and math.isfinite(value):
        rounded = float(f"{value:.{SERIES_SIGNIFICANT_FIGURES}g}")
        return int(rounded) if rounded.is_integer() and abs(rounded) < 1e15 else rounded
    return value


def _entity_series(
    periods: list[dict[str, Any]], starts: list[str], metric_names: list[str], granularity: str,
) -> dict[str, list[Any]]:
    """One entity's metrics as columns aligned to the shared period starts; null where it has no row."""
    by_start = {period["period_start"]: period for period in periods}
    series: dict[str, list[Any]] = {
        name: [_series_value(by_start[start]["metrics"].get(name)) if start in by_start else None for start in starts]
        for name in metric_names
    }
    if granularity != "day":
        series["days_with_data"] = [by_start[start]["days_with_data"] if start in by_start else 0 for start in starts]
    return series


def _fit_series(
    build: Any, full: dict[str, dict[str, list[Any]]], starts: list[str], requested: int | None,
) -> tuple[dict[str, Any], int]:
    """Choose how many of the most recent periods to return, and build the response with them.

    Without a request, the most periods that keep the response under SERIES_TARGET_BYTES; with one, at most
    that many, still under SERIES_MAX_BYTES. Returns the response and the number of periods kept.
    """
    limit = SERIES_TARGET_BYTES if requested is None else SERIES_MAX_BYTES
    wanted = len(starts) if requested is None else min(requested, len(starts))

    def response_for(count: int) -> dict[str, Any]:
        kept = starts[len(starts) - count:] if count else []
        columns = {
            entity_id: {name: values[len(values) - count:] if count else [] for name, values in series.items()}
            for entity_id, series in full.items()
        }
        return build(kept, columns)

    def size(count: int) -> int:
        return len(json.dumps(response_for(count), default=str).encode())

    if size(wanted) <= limit:
        return response_for(wanted), wanted
    low, high = 0, wanted
    while low < high:
        middle = (low + high + 1) // 2
        if size(middle) <= limit:
            low = middle
        else:
            high = middle - 1
    return response_for(low), low


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
    series_periods: int | None = None,
) -> dict[str, Any]:
    """Trend CPC, CPM, cost per result, ROAS, CTR and conversion rate (or spend, impressions, clicks, results, reach) for an account, campaign, ad set or ad by day, week (Monday start) or month, with first-to-last change and direction. Results follow Meta's own result for the object (the Insights results field); the response states the result used. Periods are summed from daily rows, then divided; zero denominators give null. Reach is never summed: each week or month gets its own reach query. Default window ends yesterday in the ad account's time zone (30/84/180 days). Several entities: object_ids (up to 10 numeric ids at level) or child_level (the max_entities highest-spending children of object_id); each entity gets totals, changes and a period series. By default the series keeps the most recent periods that fit a small response and says what was left out; series_periods (0 to 366) asks for a number of periods."""
    resolved_level = _require_level(level, REPORTING_LEVELS)
    targets = _entity_targets(resolved_level, object_id, object_ids, child_level, max_entities)
    resolved_granularity = (granularity or "").strip().lower()
    if resolved_granularity not in TREND_DEFAULT_WINDOW_DAYS:
        raise ValidationError("granularity must be day, week or month.")
    metric_names = _normalize_metric_names(metrics, TREND_DEFAULT_METRICS)
    if series_periods is not None:
        if isinstance(series_periods, bool) or not 0 <= series_periods <= MAX_DAILY_WINDOW_DAYS:
            raise ValidationError(f"series_periods must be between 0 and {MAX_DAILY_WINDOW_DAYS}.")
        if targets["mode"] == "single":
            raise ValidationError(
                "series_periods applies to several entities (object_ids or child_level); a single object always "
                "returns every period."
            )
    with_reach = "reach" in metric_names
    extra_fields = ("reach",) if with_reach else ()
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
    common: dict[str, Any] = {
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "reporting_timezone": reporting_timezone,
        "granularity": resolved_granularity,
        "metrics_requested": metric_names,
        "metric_definitions": {name: METRIC_DEFINITIONS[name] for name in metric_names},
        "results_definition": RESULTS_DEFINITION,
        "input_policy": INPUT_POLICY,
    }
    if with_reach:
        common["reach_policy"] = REACH_POLICY

    if targets["mode"] == "single":
        resolved_object_id = targets["object_id"]
        rows, result_error = await _daily_rows(
            resolved_level, resolved_object_id, start, end, extra_fields=list(extra_fields),
        )
        basis = _result_basis(rows, result_error=result_error)
        periods = _trend_periods(rows, basis, start, end, resolved_granularity, metric_names, with_reach=with_reach)
        missing_signals.extend(_trend_signals(rows, basis, periods, resolved_level))
        ranges = _reach_ranges(periods, start, end, resolved_granularity, window=False) if with_reach else []
        if ranges:
            reach, reach_error = await _reach_for_ranges(resolved_object_id, resolved_level, ranges)
            missing = _fill_period_reach(periods, reach, None, start, end)
            missing_signals.extend(_reach_signals(reach_error, 0 if reach_error else missing))
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

    entities, coverage = await _read_targets(targets, resolved_level, start, end, extra_fields=extra_fields)
    if targets["mode"] == "children":
        coverage["child_level"] = targets["child_level"]
    entity_periods: dict[str, list[dict[str, Any]]] = {}
    entity_rows: dict[str, list[dict[str, Any]]] = {}
    entity_basis: dict[str, dict[str, Any]] = {}
    for entity in entities:
        rows = entity.pop("_rows", None)
        result_error = entity.pop("_result_error", None)
        if rows is None:
            continue
        basis = _result_basis(rows, result_error=result_error)
        entity_rows[entity["object_id"]] = rows
        entity_basis[entity["object_id"]] = basis
        entity_periods[entity["object_id"]] = _trend_periods(
            rows, basis, start, end, resolved_granularity, metric_names, with_reach=with_reach,
        )

    reach_by_entity: dict[str, tuple[dict[_ReachKey, float | None], str | None]] = {}
    reach_ids = [entity_id for entity_id, rows in entity_rows.items() if rows]
    if with_reach and reach_ids:
        if targets["mode"] == "children":
            child = targets["child_level"]
            ranges = [
                item for entity_id in reach_ids
                for item in _reach_ranges(entity_periods[entity_id], start, end, resolved_granularity, window=True)
            ]
            shared = await _reach_for_ranges(
                targets["object_id"], child, ranges, id_field=f"{child}_id", ids=reach_ids,
            )
            reach_by_entity = {entity_id: shared for entity_id in reach_ids}
        else:
            semaphore = asyncio.Semaphore(ENTITY_CONCURRENCY)

            async def entity_reach(entity_id: str) -> tuple[str, tuple[dict[_ReachKey, float | None], str | None]]:
                async with semaphore:
                    ranges = _reach_ranges(entity_periods[entity_id], start, end, resolved_granularity, window=True)
                    return entity_id, await _reach_for_ranges(entity_id, resolved_level, ranges)

            reach_by_entity = dict(await asyncio.gather(*(entity_reach(entity_id) for entity_id in reach_ids)))

    for entity in entities:
        entity_id = entity["object_id"]
        if entity_id not in entity_rows:
            continue
        rows, basis, periods = entity_rows[entity_id], entity_basis[entity_id], entity_periods[entity_id]
        signals = _trend_signals(rows, basis, periods, entity["level"])
        totals_inputs = _period_inputs(rows, basis) if rows else None
        if with_reach and totals_inputs is not None:
            reach, reach_error = reach_by_entity.get(entity_id, ({}, None))
            key_id = entity_id if targets["mode"] == "children" else None
            missing = _fill_period_reach(periods, reach, key_id, start, end) if resolved_granularity != "day" else 0
            totals_inputs["reach"] = reach.get((key_id, start.isoformat(), end.isoformat()))
            signals.extend(_reach_signals(reach_error, 0 if reach_error else missing, totals=True))
            if reach_error is None and totals_inputs["reach"] is None:
                signals.append("Meta returned no reach for the window total, so total reach is null.")
        entity["daily_rows_returned"] = len(rows)
        entity["result"] = _compact_result(basis)
        entity["totals"] = None if totals_inputs is None else {
            "inputs": totals_inputs,
            "metrics": {name: _derived_metrics(totals_inputs, basis)[name] for name in metric_names},
        }
        entity["changes"] = {
            name: {key: change[key] for key in _COMPACT_CHANGE_KEYS}
            for name in metric_names
            for change in (_first_to_last(name, periods),)
        }
        entity["missing_signals"] = signals
    missing_signals.extend(_coverage_signals(coverage))

    starts = [day.isoformat() for day in _window_period_starts(start, end, resolved_granularity)]
    partial_starts = [
        value for value in starts
        if date.fromisoformat(value) < start or _period_bounds(date.fromisoformat(value), resolved_granularity)[1] > end
    ]
    full_series = {
        entity_id: _entity_series(periods, starts, metric_names, resolved_granularity)
        for entity_id, periods in entity_periods.items()
    }
    entity_signals = _group_entity_signals(entities)

    def build(kept: list[str], columns: dict[str, dict[str, list[Any]]]) -> dict[str, Any]:
        left_out = len(starts) - len(kept)
        shown = [
            {**entity, "series": columns[entity["object_id"]]} if entity["object_id"] in columns else entity
            for entity in entities
        ]
        series_info: dict[str, Any] = {
            "period_starts": kept,
            "partial_periods": [value for value in partial_starts if value in kept],
            "layout": (
                "each entity's series holds one list per metric, aligned to period_starts; null where the entity "
                "had no row in that period or the metric has no value; values are rounded to "
                f"{SERIES_SIGNIFICANT_FIGURES} significant figures (totals and changes are exact)"
                + ("; days_with_data counts the daily rows in each period" if resolved_granularity != "day" else "")
            ),
            "periods_in_window": len(starts),
            "periods_returned": len(kept),
            "periods_left_out": left_out,
            "requested_periods": series_periods,
        }
        signals = list(missing_signals)
        if left_out and kept:
            series_info["left_out"] = f"the earliest {left_out} period(s), before {kept[0]}"
            signals.append(
                f"The per-entity series covers the most recent {len(kept)} of {len(starts)} periods to keep the "
                f"response small; the earliest {left_out} were left out (totals and changes still cover the whole "
                "window). Ask for fewer metrics, a coarser granularity, fewer entities or series_periods, or call "
                "with one object_id for every period."
            )
        elif left_out and series_periods != 0:
            series_info["left_out"] = "every period"
            signals.append(
                "The per-entity series was left out to keep the response small; totals and changes cover the whole "
                "window. Ask for fewer metrics or entities, or call with one object_id for every period."
            )
        return analysis_response(
            scope=_multi_scope(resolved_level, targets),
            metrics={},
            missing_signals=signals,
            extra={
                **common,
                "entity_signals": entity_signals,
                "series": series_info,
                "entities": shown,
                "coverage": coverage,
                "method": TREND_METHOD + " With several entities, each entity also gets window totals, first-to-last "
                "changes and a series of its periods (see series).",
            },
        )

    response, _ = _fit_series(build, full_series, starts, series_periods)
    return response


ANOMALY_DEFAULT_METRICS = ("spend", "cpc", "cpm", "ctr", "cost_per_result", "roas")
SENSITIVITY_MULTIPLIERS = {"low": 5.0, "medium": 3.5, "high": 2.5}
MAD_SCALE = 1.4826
MIN_SAME_WEEKDAY_POINTS = 4
MIN_BASELINE_VALUES = 7
MAX_LOOKBACK_DAYS = 31
MIN_BASELINE_DAYS = 7
MAX_BASELINE_DAYS = 90
MULTI_ENTITY_MAX_FINDINGS = 10
MULTI_ENTITY_ANOMALIES_PER_ENTITY = 2


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
        inputs = _period_inputs([row], basis, with_reach="reach" in metric_names)
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
_ENTITY_ANOMALY_KEYS = ("day", "metric", "value", "baseline_median", "robust_score", "direction")


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
    """Flag days in the last lookback_days whose spend, CPC, CPM, CTR, cost per result or ROAS (or another metric named in metrics, such as daily reach) is far from the object's own baseline (the baseline_days before), using median and median absolute deviation, with same-weekday baselines when four or more exist. Cost per result and ROAS follow Meta's own result for the object (the Insights results field). Returns the rule, baseline values and thresholds. Rule-based screen, not a statistical test. end_date defaults to yesterday in the ad account's time zone. Several entities: object_ids (up to 10 numeric ids at level) or child_level (the max_entities highest-spending children of object_id)."""
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
    extra_fields = ("reach",) if "reach" in metric_names else ()
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
        rows, result_error = await _daily_rows(
            resolved_level, resolved_object_id, baseline_start, end, extra_fields=list(extra_fields),
        )
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

    entities, coverage = await _read_targets(targets, resolved_level, baseline_start, end, extra_fields=extra_fields)
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
        entity["result"] = _compact_result(basis)
        entity["baseline_rows"] = analysis["baseline_rows"]
        entity["lookback_rows"] = analysis["lookback_rows"]
        entity["judged"] = not analysis["insufficient"]
        entity["anomaly_count"] = len(analysis["anomalies"])
        entity["anomalies"] = [
            {key: anomaly[key] for key in _ENTITY_ANOMALY_KEYS}
            for anomaly in analysis["anomalies"][:MULTI_ENTITY_ANOMALIES_PER_ENTITY]
        ]
        entity["metric_status"] = {metric: status["status"] for metric, status in analysis["metric_status"].items()}
        entity["missing_signals"] = [
            *_result_signals(basis, entity["level"], rows_exist=bool(rows)), *analysis["missing_signals"],
        ]
        scored.extend((anomaly, reference) for anomaly in analysis["anomalies"])
    scored.sort(key=lambda item: (-abs(item[0]["robust_score"]), item[0]["day"], item[1]["object_id"], item[0]["metric"]))
    findings = []
    for anomaly, reference in scored[:MULTI_ENTITY_MAX_FINDINGS]:
        compact = {key: anomaly[key] for key in _COMPACT_ANOMALY_KEYS}
        finding = _anomaly_finding(compact, multiplier, reference)
        finding["evidence"][0]["inputs"] = {
            key: finding["evidence"][0]["inputs"][key]
            for key in ("day", "baseline_median", "robust_score", "threshold_low", "threshold_high")
        }
        findings.append(finding)
    missing_signals = [*timezone_signals, *_coverage_signals(coverage)]
    if len(scored) > MULTI_ENTITY_MAX_FINDINGS:
        missing_signals.append(
            f"{len(scored)} anomalies were found; findings list the {MULTI_ENTITY_MAX_FINDINGS} furthest from their "
            f"baselines. Each entity lists its count and its {MULTI_ENTITY_ANOMALIES_PER_ENTITY} largest; call with "
            "that entity's object_id for the full detail."
        )
    return analysis_response(
        scope=_multi_scope(resolved_level, targets),
        metrics={"anomaly_count": len(scored)},
        findings=findings or [_no_anomaly_finding()],
        missing_signals=missing_signals,
        extra={
            **windows,
            "rule": rule,
            "entity_signals": _group_entity_signals(entities),
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
            raw_rows, stopped = await _read_bounded_pages(object_id, fields, params)
        except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
            errors.append(str(_public_error(exc).get("message")))
            continue
        if stopped is not None:
            # A total summed from part of Meta's pages would understate the scope's spend.
            errors.append(f"the scope's own rows could not be read to the last page ({stopped})")
            return None, "; ".join(errors), False
        return _normalize_with_results({"data": raw_rows}), ("; ".join(errors) or None), with_results
    return None, "; ".join(errors), False


async def _ranking_cost_rows(
    object_id: str, ad_ids: list[str], window: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    """Read results for the selected ads in one request filtered to their ids, page by page. Never raises.

    Returns the rows by ad id, the error that stopped the read, and why the read stopped before Meta's last
    page while some selected ads still had no row.
    """
    if not ad_ids:
        return {}, None, None
    params = insights_tools._insights_params(
        level="ad", default_date_preset="last_30d", limit=len(ad_ids),
        filtering=[{"field": "ad.id", "operator": "IN", "value": list(ad_ids)}], **window,
    )
    try:
        raw_rows, stopped = await _read_bounded_pages(
            object_id, RANKING_COST_FIELDS, params, enough=_covers("ad_id", ad_ids),
        )
    except Exception as exc:  # an enrichment failure must not fail the rankings report
        return {}, str(_public_error(exc).get("message")), None
    wanted = set(ad_ids)
    rows: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        key = str(row.get("ad_id") or "")
        if key in wanted and key not in rows:
            rows[key] = row
    return rows, None, (stopped if len(rows) < len(wanted) else None)


def _known_ranking(value: Any) -> bool:
    return RANKING_POINTS.get(str(value or "").upper()) is not None


def _row_spend(row: dict[str, Any]) -> float | None:
    """A normalized row's spend when it is known and finite; None otherwise, never zero in its place."""
    return _finite((row.get("metrics") or {}).get("spend"))


def _known_sum(values: list[float | None]) -> float | None:
    """The sum of values only when every one is known; an empty list sums to zero."""
    return None if any(value is None for value in values) else sum(values)  # type: ignore[arg-type]


def _is_action_indicator(indicator: str, row: dict[str, Any]) -> bool:
    return indicator.startswith(ACTION_INDICATOR_PREFIX) or indicator in (row.get("actions_map") or {})


def _pooled_costs(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    """Cost per result for each action result across ads: summed spend / summed results, per indicator.

    Only ads that report that result themselves are pooled; an ad that reports no result is left out rather
    than counted as zero for another ad's result.
    """
    sums: dict[str, list[float | None]] = {}
    for row in rows:
        result = row.get("meta_result") or {"status": "absent"}
        if result["status"] != "read":
            continue
        indicator, count = result["indicator"], result["value"] or 0.0
        if not _is_action_indicator(indicator, row):
            continue
        spend = _row_spend(row)
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
    lean_rows, lean_unreadable = _page_rows(ad_payload)
    lean_rows = lean_rows[:max_ads]
    # An ad row that could not be read is an ad that was not examined.
    more_ads_exist = bool(extract_paging(ad_payload).get("next")) or lean_unreadable > 0
    cost_ids = list(dict.fromkeys(
        str(row["ad_id"]) for row in lean_rows
        if row.get("ad_id") and any(_known_ranking(row.get(field)) for field in RANKING_FIELDS)
    ))
    cost_ids_capped = cost_ids[:RANKING_COST_MAX_ADS]
    cost_rows, cost_error, cost_stopped = await _ranking_cost_rows(object_id, cost_ids_capped, window)
    rows = _normalize_with_results({"data": [
        {**cost_rows[str(row.get("ad_id"))], **row} if str(row.get("ad_id")) in cost_rows else row
        for row in lean_rows
    ]})
    spends = [_row_spend(row) for row in rows]
    spend_unknown = sum(1 for spend in spends if spend is None)
    # Order can be confirmed only from known spends; a missing one is reported below, not read as zero.
    order_confirmed = not spend_unknown and all(
        earlier >= later for earlier, later in zip(spends, spends[1:])  # type: ignore[operator]
    )

    missing_signals: list[str] = []
    if lean_unreadable:
        missing_signals.append(
            f"Meta returned {lean_unreadable} ad row(s) that could not be read; those ads were not examined or ranked."
        )
    examined_spend = _known_sum(spends)
    if spend_unknown:
        missing_signals.append(
            f"{spend_unknown} examined ad(s) had no readable spend, so the examined spend, the share of the scope's "
            "spend they cover, and whether Meta returned them highest spend first are unknown."
        )
    if level == "ad":
        scope_spend: float | None = examined_spend
    elif scope_rows is None:
        scope_spend = None
        missing_signals.append(
            f"The scope's total spend could not be read, so the share of spend covered is unknown ({scope_error})."
        )
    else:
        scope_spend = _known_sum([_row_spend(row) for row in scope_rows])
        if scope_spend is None:
            missing_signals.append(
                f"{sum(1 for row in scope_rows if _row_spend(row) is None)} of the scope's {len(scope_rows)} total "
                "row(s) had no readable spend, so the scope's spend and the share covered are unknown."
            )
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
    if not order_confirmed and not spend_unknown:
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
    if cost_status == "read" and cost_stopped is not None:
        missing_signals.append(
            f"The results read stopped before Meta's last page ({cost_stopped}), so "
            f"{len(cost_ids_capped) - len(cost_rows)} of the {len(cost_ids_capped)} ads selected for cost evidence "
            "have no results or cost per result."
        )
    elif cost_status == "read" and without_cost:
        missing_signals.append(
            f"Results and cost per result were read for {len(cost_rows)} of {len(rows)} examined ads: those with at "
            f"least one known ranking (at most {RANKING_COST_MAX_ADS}, highest spend first). The others show them as null."
        )
    cost_evidence = {
        "status": cost_status,
        "method": (
            "one insights request at ad level for the same window, filtered to the selected ad ids "
            "(filtering ad.id IN), unsorted, with Meta's results and cost_per_result fields, read page by page "
            f"until Meta reports no more (at most {INSIGHTS_MAX_PAGES} requests)"
        ),
        "selection": f"examined ads with at least one known ranking, highest spend first, at most {RANKING_COST_MAX_ADS}",
        "ads_requested": len(cost_ids_capped),
        "ads_returned": len(cost_rows),
        "ads_eligible_not_requested": len(cost_ids) - len(cost_ids_capped),
        "error": cost_error,
        "read_stopped": cost_stopped,
    }

    cost_known_rows = [row for row in rows if str(row.get("ad_id")) in cost_rows]
    pool_basis = _result_basis(cost_known_rows, one_entity=False)
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
    pooled = _pooled_costs(cost_known_rows)
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

    unreported = sum(
        1 for row in cost_known_rows if (row.get("meta_result") or {}).get("status", "absent") == "absent"
    )
    if cost_status == "read" and unreported and pool_basis["status"] != "not_reported":
        missing_signals.append(
            f"{unreported} examined ad(s) with cost evidence reported no Meta result. Their result type is unknown, "
            "so their results are null rather than zero, they are left out of every cost baseline, and they are "
            "not diagnosed on cost."
        )
    unreadable_ads = [row for row in cost_known_rows if (row.get("meta_result") or {}).get("status") == "unreadable"]
    if cost_status == "read" and unreadable_ads:
        reasons = list(dict.fromkeys(str(row["meta_result"].get("reason")) for row in unreadable_ads))
        missing_signals.append(
            f"{len(unreadable_ads)} examined ad(s) had a Meta result that could not be read "
            f"({'; '.join(reasons[:MAX_UNREADABLE_REASONS])}); their results are null and no value was guessed."
        )

    ads: list[dict[str, Any]] = []
    for row in rows:
        has_cost = str(row.get("ad_id")) in cost_rows
        own = _result_basis([row]) if has_cost else {"status": "not_requested"}
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
            "result_status": own["status"],
            **({"result_unreadable_reasons": own["unreadable_reasons"]} if own.get("unreadable_reasons") else {}),
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
    items, unreadable = _page_rows(payload)
    section = {
        "available": True,
        "edge": edge,
        "items": items,
        "count": len(items),
        "complete": not bool(extract_paging(payload).get("next")) and not unreadable,
        "page_limit": CONTEXT_EDGE_LIMIT,
    }
    if unreadable:
        section["rows_unreadable"] = unreadable
    return section


def _known_total_spend(rows: list[dict[str, Any]]) -> float | None:
    """Sum spend across rows; a row without spend makes the total unknown rather than smaller."""
    return _known_sum([_row_spend(row) for row in rows])


def _group_spend(
    rows: list[dict[str, Any]], key: str, total_spend: float | None, *, result_error: str | None = None,
    unit: str = "adset",
) -> list[dict[str, Any]]:
    """Group ad set (or ad) rows by a dimension, with spend, share, and Meta's result where the group has one.

    A row without spend makes its group's spend (and share) null. Results are added up only when every row in
    the group reports the same action result itself; a row that reports no result has an unknown result type,
    so it makes the group's results null (never zero), and the group counts such rows. Mixed result types are
    null and listed.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "UNKNOWN"), []).append(row)
    output: list[dict[str, Any]] = []
    for label, group_rows in grouped.items():
        basis = _result_basis(group_rows, result_error=result_error, one_entity=False)
        inputs = _period_inputs(group_rows, basis)
        spend = _known_total_spend(group_rows)
        group: dict[str, Any] = {
            key: label,
            "spend": spend,
            f"{unit}_count": len(group_rows),
            "spend_share": _ratio(spend, total_spend),
            "result_indicator": basis.get("indicator") if basis["status"] == "single" else None,
            "results": inputs["results"],
            "cost_per_result": _derived_metrics(inputs, basis)["cost_per_result"],
        }
        if basis["status"] == "mixed":
            group["result_indicators"] = basis["indicators"]
        if basis["status"] in ("single", "mixed"):
            without = sum(
                1 for row in group_rows if (row.get("meta_result") or {}).get("status", "absent") == "absent"
            )
            if without:
                group[f"{unit}s_without_reported_result"] = without
        if basis.get("unreadable_reasons"):
            group["result_unreadable_reasons"] = basis["unreadable_reasons"]
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
        "spend": _row_spend(row),
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
    "optimizes for"
)


def _meta_result_events(row: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    """An ad set's optimization events from its own reported Meta result: (events, indicator, reason it cannot be judged).

    Only called for a row that reports a result; a row without one is handled by its own goal, never as zero.
    """
    result = row.get("meta_result") or {"status": "absent"}
    if result["status"] == "mixed":
        return None, None, f"the ad set reports several results ({', '.join(result['indicators'])})"
    if result["status"] != "read":
        reason = result.get("reason") or "an unexpected shape"
        return None, None, f"the ad set's result could not be read ({reason})"
    if not _is_action_indicator(result["indicator"], row):
        return None, result["indicator"], f"Meta's result {result['indicator']} is not an event count"
    return (result["value"] or 0.0), result["indicator"], None


def _results_reported(rows: list[dict[str, Any]]) -> bool:
    return any((row.get("meta_result") or {}).get("status") in ("read", "mixed", "unreadable") for row in rows)


GOAL_VOLUME_MIN_DAYS = 7
_SHORT_WINDOW_REASON = (
    "The window has fewer than 7 days. Meta's learning-phase guidance counts events over 7 days, and a shorter "
    "window is not scaled up to a week, so the goal is not assessed."
)


def _goal_volume(rows: list[dict[str, Any]], lookback_days: int) -> list[dict[str, Any]]:
    """Summarize, per optimization goal, how many ad sets reach the learning-phase weekly volume, with the top spenders.

    Each ad set is judged on its own data only. An ad set whose row reports Meta's result is judged on that
    result. An ad set whose row reports no result is judged on its goal's own event when the goal identifies it
    (GOAL_EVENT_BASIS); otherwise it is not judged. Another ad set's result never decides how this one is read.
    Ad sets with zero spend are skipped; an ad set whose spend is missing is kept and makes its goal's spend null.
    """
    by_goal: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        spend = _row_spend(row)
        if spend is not None and spend <= 0:
            continue
        by_goal.setdefault(str(row.get("optimization_goal") or "UNKNOWN"), []).append(row)
    output = []
    for goal in sorted(by_goal):
        goal_rows = by_goal[goal]
        goal_spend = _known_total_spend(goal_rows)
        unknown_spend = sum(1 for row in goal_rows if _row_spend(row) is None)
        mapping = GOAL_EVENT_BASIS.get(goal)
        unidentified = GOAL_EVENT_UNIDENTIFIED.get(goal, "No event mapping is defined for this optimization goal.")
        short_window = lookback_days < GOAL_VOLUME_MIN_DAYS
        if short_window or (mapping is None and not _results_reported(goal_rows)):
            output.append({
                "optimization_goal": goal,
                "adset_count": len(goal_rows),
                "spend": goal_spend,
                "adsets_with_unknown_spend": unknown_spend,
                "verdict": "not_assessed",
                "reason": _SHORT_WINDOW_REASON if short_window else unidentified,
                "top_adsets_by_spend": _top_by_spend([_adset_volume_entry(row, None, None) for row in goal_rows]),
            })
            continue
        meeting: list[dict[str, Any]] = []
        below: list[dict[str, Any]] = []
        not_judged: list[dict[str, Any]] = []
        reasons: list[str] = []
        indicators: list[str] = []
        bases: set[str] = set()
        for row in goal_rows:
            if (row.get("meta_result") or {}).get("status", "absent") != "absent":
                events, indicator, reason = _meta_result_events(row)
                if indicator and indicator not in indicators:
                    indicators.append(indicator)
                if events is not None:
                    bases.add("results")
            elif mapping is not None:
                action_types, _ = mapping
                actions = row.get("actions_map") or {}
                matched = next((candidate for candidate in action_types if candidate in actions), None)
                events, reason = ((to_float(actions.get(matched)) or 0.0) if matched else 0.0), None
                bases.add("goal_event")
            else:
                events = None
                reason = (
                    "an ad set reports no Meta result, so its result type is unknown and it is not counted as zero; "
                    + unidentified[0].lower() + unidentified[1:]
                )
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
        if bases == {"results"}:
            event_basis = _META_RESULT_EVENT_BASIS
        elif bases == {"goal_event"}:
            event_basis = mapping[1]  # type: ignore[index]
        else:
            event_basis = (
                f"{_META_RESULT_EVENT_BASIS} where the ad set reports one; otherwise {mapping[1]}"  # type: ignore[index]
            )
        entry_out: dict[str, Any] = {
            "optimization_goal": goal,
            "event_basis": event_basis,
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
        if indicators:
            entry_out["result_indicators"] = indicators[:5]
        if not_judged:
            entry_out["adsets_not_judged"] = len(not_judged)
            entry_out["not_judged_reasons"] = reasons
            entry_out["verdict"] = verdict if verdict != "all_adsets_meet_threshold" else "judged_adsets_meet_threshold"
        output.append(entry_out)
    return output


MAX_CONTEXT_OBJECT_IDS = 50
# The ad set read is bounded so a large account gives a partial briefing, never an error. As in the ranking
# report (confirmed live), the highest-spending ad sets are chosen with one lean, spend-sorted request; their
# full rows come from one request filtered to their ids (adset.id IN); the account's own total gives the share
# of spend they cover. An unbounded read of every ad set failed live on an account with many ad sets.
CONTEXT_MAX_ADSETS = 250
CONTEXT_SORTED_FIELDS = ["adset_id", "spend"]
_CONTEXT_READ_ERRORS = (MetaApiError, NotFoundError, UnsupportedFeatureError, ValidationError, RateLimitError)


async def _read_pages_with_results(
    object_id: str, fields: list[str], params: dict[str, Any], *, level: str, max_rows: int | None = None,
    enough: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Read every page (bounded) with Meta's result fields, once more without them if Meta rejects them.

    Returns the normalized rows, why the read stopped before Meta's last page (None when it reached it; see
    _read_bounded_pages), and the error that made the result fields unavailable.
    """
    with_results = [*fields, *(field for field in _result_fields(level) if field not in fields)]
    bounds: dict[str, Any] = {"max_rows": max_rows, "enough": enough}
    try:
        rows, stopped = await _read_bounded_pages(object_id, with_results, params, **bounds)
        result_error = None
    except (MetaApiError, UnsupportedFeatureError) as exc:
        if isinstance(exc, MetaApiError) and exc.code not in RESULT_RETRY_CODES:
            raise
        result_error = str(_public_error(exc).get("message"))
        rows, stopped = await _read_bounded_pages(object_id, fields, params, **bounds)
    return _normalize_with_results({"data": rows}), stopped, result_error


async def _context_account_spend(account_id: str, start: date, end: date) -> tuple[float | None, str | None]:
    """The account's own spend in the window, for the share the examined ad sets cover. Never raises."""
    params = insights_tools._insights_params(level="account", since=start.isoformat(), until=end.isoformat(), limit=1)
    try:
        rows, stopped = await _read_bounded_pages(account_id, ["spend"], params)
    except _CONTEXT_READ_ERRORS as exc:
        return None, str(_public_error(exc).get("message"))
    if stopped is not None:
        return None, f"Meta's account total could not be read to its last page ({stopped})"
    values = [_finite(row.get("spend")) for row in rows]
    if not rows:
        return 0.0, None
    if any(value is None for value in values):
        return None, "Meta's account total had a row without a readable spend value"
    return sum(values), None  # type: ignore[arg-type]


async def _context_adset_rows(
    account_id: str, start: date, end: date, scope_ids: list[str] | None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Read a bounded set of ad set rows: the highest-spending ones, or those under the named objects.

    Never raises for a read failure or a large account; the coverage block says what was read.
    """
    window = {"since": start.isoformat(), "until": end.isoformat()}
    full_params = insights_tools._insights_params(level="adset", limit=CONTEXT_MAX_ADSETS, **window)
    coverage: dict[str, Any] = {"max_adsets": CONTEXT_MAX_ADSETS, "status": "read", "error": None}
    rows: list[dict[str, Any]] = []
    result_error: str | None = None
    more = False
    stop_reasons: list[str] = []
    if scope_ids is not None:
        coverage.update({
            "mode": "object_ids",
            "method": (
                "two ad set insights requests filtered to the named ids (campaign.id IN, adset.id IN), each read "
                f"page by page until Meta reports no more, {CONTEXT_MAX_ADSETS} rows or {INSIGHTS_MAX_PAGES} "
                "requests"
            ),
        })
        reads = await asyncio.gather(*(
            _read_pages_with_results(
                account_id, CONTEXT_INSIGHTS_FIELDS,
                {**full_params, "filtering": [{"field": field, "operator": "IN", "value": list(scope_ids)}]},
                level="adset", max_rows=CONTEXT_MAX_ADSETS,
            )
            for field in ("campaign.id", "adset.id")
        ), return_exceptions=True)
        errors = []
        seen: set[str] = set()
        for read in reads:
            if isinstance(read, BaseException):
                if not isinstance(read, _CONTEXT_READ_ERRORS):
                    raise read
                errors.append(str(_public_error(read).get("message")))
                continue
            read_rows, stopped, read_error = read
            more = more or stopped is not None
            if stopped not in (None, ROW_BOUND_REACHED):
                stop_reasons.append(stopped)
            result_error = result_error or read_error
            for row in read_rows:
                key = str(row.get("adset_id") or "")
                named = str(row.get("campaign_id") or "") in scope_ids or key in scope_ids
                if named and key not in seen:
                    seen.add(key)
                    rows.append(row)
        if errors:
            coverage["error"] = "; ".join(errors)
            coverage["status"] = "failed" if len(errors) == len(reads) else "partly_read"
        account_spend, spend_error = await _context_account_spend(account_id, start, end)
    else:
        coverage.update({
            "mode": "top_spenders",
            "method": (
                f"one lean ad set request sorted by spend, highest first (limit {CONTEXT_MAX_ADSETS}), then one "
                "request for those ad sets' full rows filtered by adset.id IN, read page by page until Meta reports "
                f"no more (at most {INSIGHTS_MAX_PAGES} requests)"
            ),
        })
        sorted_params = {**insights_tools._insights_params(level="adset", limit=CONTEXT_MAX_ADSETS, **window),
                         "sort": ["spend_descending"]}

        async def selection() -> dict[str, Any] | BaseException:
            try:
                return await get_graph_api_client().get_insights(
                    account_id, fields=CONTEXT_SORTED_FIELDS, params=sorted_params,
                )
            except _CONTEXT_READ_ERRORS as exc:
                return exc

        picked, (account_spend, spend_error) = await asyncio.gather(
            selection(), _context_account_spend(account_id, start, end),
        )
        if isinstance(picked, BaseException):
            coverage.update({"status": "failed", "error": str(_public_error(picked).get("message"))})
        else:
            picked_rows, picked_unreadable = _page_rows(picked)
            # A selection row that cannot be read (or has no ad set id) is an ad set that was not examined.
            picked_unreadable += sum(1 for row in picked_rows if row.get("adset_id") is None)
            all_ids = list(dict.fromkeys(
                str(row["adset_id"]) for row in picked_rows if row.get("adset_id") is not None
            ))
            ids = all_ids[:CONTEXT_MAX_ADSETS]
            more = bool(extract_paging(picked).get("next")) or len(all_ids) > len(ids) or picked_unreadable > 0
            if picked_unreadable:
                coverage["selection_rows_unreadable"] = picked_unreadable
            if ids:
                try:
                    read_rows, stopped, result_error = await _read_pages_with_results(
                        account_id, CONTEXT_INSIGHTS_FIELDS,
                        {**full_params, "limit": len(ids),
                         "filtering": [{"field": "adset.id", "operator": "IN", "value": ids}]},
                        level="adset", enough=_covers("adset_id", ids),
                    )
                except _CONTEXT_READ_ERRORS as exc:
                    coverage.update({"status": "failed", "error": str(_public_error(exc).get("message"))})
                else:
                    wanted = set(ids)
                    kept: set[str] = set()
                    for row in read_rows:
                        key = str(row.get("adset_id") or "")
                        if key in wanted and key not in kept:
                            kept.add(key)
                            rows.append(row)
                    if len(rows) < len(ids):
                        # A selected ad set without its full row leaves its spend, funnel and goal events out.
                        # Once every selected ad set has its row, a page Meta still offers holds nothing needed.
                        coverage["adsets_selected"] = len(ids)
                        coverage["adsets_selected_without_rows"] = len(ids) - len(rows)
                        more = True
                        if stopped is not None:
                            stop_reasons.append(stopped)
    coverage["rows_read_stopped"] = "; ".join(dict.fromkeys(stop_reasons)) or None
    examined_spend = _known_total_spend(rows)
    share = _ratio(examined_spend, account_spend)
    coverage.update({
        "adsets_examined": len(rows),
        "more_adsets_exist": more,
        "examined_spend": examined_spend,
        "account_spend": account_spend,
        "account_spend_error": spend_error,
        "share_of_account_spend": None if share is None else min(share, 1.0),
        "complete": coverage["status"] == "read" and not more,
    })
    return rows, result_error, coverage


CONTEXT_AD_FIELDS = [*CONTEXT_INSIGHTS_FIELDS, "ad_id", "ad_name"]


async def _context_ad_rows(
    account_id: str, start: date, end: date, ad_ids: list[str],
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Read the named ads' rows in one ad-level request filtered to their ids (ad.id IN). Never raises for a read failure."""
    params = insights_tools._insights_params(
        level="ad", since=start.isoformat(), until=end.isoformat(), limit=len(ad_ids),
        filtering=[{"field": "ad.id", "operator": "IN", "value": list(ad_ids)}],
    )
    coverage: dict[str, Any] = {
        "mode": "ad_ids",
        "row_level": "ad",
        "method": (
            "one ad-level insights request filtered to the named ad ids (ad.id IN), read page by page until Meta "
            f"reports no more (at most {INSIGHTS_MAX_PAGES} requests)"
        ),
        "status": "read",
        "error": None,
        "rows_read_stopped": None,
    }
    rows: list[dict[str, Any]] = []
    result_error: str | None = None
    more = False

    async def read() -> tuple[list[dict[str, Any]], str | None, str | None] | BaseException:
        try:
            return await _read_pages_with_results(
                account_id, CONTEXT_AD_FIELDS, params, level="ad", enough=_covers("ad_id", ad_ids),
            )
        except _CONTEXT_READ_ERRORS as exc:
            return exc

    outcome, (account_spend, spend_error) = await asyncio.gather(
        read(), _context_account_spend(account_id, start, end),
    )
    if isinstance(outcome, BaseException):
        coverage.update({"status": "failed", "error": str(_public_error(outcome).get("message"))})
    else:
        read_rows, stopped, result_error = outcome
        wanted = set(ad_ids)
        seen: set[str] = set()
        for row in read_rows:
            key = str(row.get("ad_id") or "")
            if key in wanted and key not in seen:
                seen.add(key)
                rows.append(row)
        # Once every named ad has its row, a page Meta still offers holds nothing needed.
        if stopped is not None and len(rows) < len(wanted):
            more = True
            coverage["rows_read_stopped"] = stopped
    examined_spend = _known_total_spend(rows)
    share = _ratio(examined_spend, account_spend)
    coverage.update({
        "ads_examined": len(rows),
        "more_ads_exist": more,
        "examined_spend": examined_spend,
        "account_spend": account_spend,
        "account_spend_error": spend_error,
        "share_of_account_spend": None if share is None else min(share, 1.0),
        "complete": coverage["status"] == "read" and not more,
    })
    return rows, result_error, coverage


_AD_SCOPE_GOAL_REASON = (
    "ad_ids names ads, but Meta's learning phase counts optimization events per ad set, and the named ads may be "
    "only part of their ad sets' delivery, so weekly volume per ad set is not assessed. Pass the ad set ids "
    "(adset_ids_of_named_ads) in object_ids to assess it."
)


def _ad_scope_goal_volume(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per goal, the named ads and their ad sets; the learning-phase volume itself is not judged for ads."""
    by_goal: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_goal.setdefault(str(row.get("optimization_goal") or "UNKNOWN"), []).append(row)
    output = []
    for goal in sorted(by_goal):
        goal_rows = by_goal[goal]
        adset_ids = list(dict.fromkeys(str(row["adset_id"]) for row in goal_rows if row.get("adset_id")))
        output.append({
            "optimization_goal": goal,
            "ad_count": len(goal_rows),
            "spend": _known_total_spend(goal_rows),
            "verdict": "not_assessed",
            "reason": _AD_SCOPE_GOAL_REASON,
            "adset_ids_of_named_ads": adset_ids[:MAX_CONTEXT_OBJECT_IDS],
        })
    return output


def _coverage_signal(coverage: dict[str, Any]) -> list[str]:
    """Plain statements of how much of the account's ad set (or named ad) activity the briefing examined."""
    unit = "ad" if coverage.get("row_level") == "ad" else "ad set"
    if coverage["status"] == "failed":
        return [
            f"{unit.capitalize()} insights could not be read ({coverage['error']}), so spend by objective and goal, "
            "the funnel and goal volume are empty; account settings and asset lists are still reported."
        ]
    signals = []
    if coverage["status"] == "partly_read":
        signals.append(f"One of the filtered ad set reads failed ({coverage['error']}); the other was used.")
    if coverage.get("more_adsets_exist") or coverage.get("more_ads_exist"):
        share = coverage["share_of_account_spend"]
        covered = f", covering {share:.0%} of the account's spend in the window" if share is not None else ""
        which = {
            "top_spenders": "highest-spending ad sets",
            "object_ids": "ad sets under the named objects",
            "ad_ids": "named ads",
        }[coverage["mode"]]
        examined = coverage.get("adsets_examined", coverage.get("ads_examined"))
        signals.append(
            f"Partial briefing: examined the {examined} {which}{covered}; the remaining {unit}s were not examined, "
            "so spend, funnel and goal sections describe only the examined ones."
        )
    if coverage.get("adsets_selected_without_rows"):
        signals.append(
            f"{coverage['adsets_selected_without_rows']} of the {coverage['adsets_selected']} selected "
            "highest-spending ad sets had no full row read, so their spend, funnel and goal events are left out."
        )
    if coverage.get("rows_read_stopped"):
        signals.append(f"Meta's {unit} rows could not be read to the last page ({coverage['rows_read_stopped']}).")
    if coverage.get("account_spend_error"):
        signals.append(
            f"The account's total spend could not be read ({coverage['account_spend_error']}), so the share of spend "
            "covered is unknown."
        )
    return signals


def _context_window(
    lookback_days: int | None, since: str | None, until: str | None,
) -> tuple[tuple[date, date] | None, int]:
    """Validate the briefing window before any network call: lookback_days, or an explicit since and until."""
    explicit = _explicit_window(since, until)
    if explicit is not None and lookback_days is not None:
        raise ValidationError("Give lookback_days or since and until, not both.")
    if explicit is None:
        days = CONTEXT_DEFAULT_LOOKBACK_DAYS if lookback_days is None else lookback_days
        if isinstance(days, bool) or not 7 <= days <= 90:
            raise ValidationError("lookback_days must be between 7 and 90.")
        return None, days
    return explicit, (explicit[1] - explicit[0]).days + 1


CONTEXT_DEFAULT_LOOKBACK_DAYS = 30


@mcp_server.tool()
async def get_advertiser_context(
    account_id: str,
    lookback_days: int | None = None,
    object_ids: StringList | None = None,
    since: str | None = None,
    until: str | None = None,
    ad_ids: StringList | None = None,
) -> dict[str, Any]:
    """Briefing built from this account's data: account and business settings, spend by objective and optimization goal, funnel event volumes with drop-off, pixels/datasets and custom conversions, Pages and Instagram identities, and which optimization goals get about 50 events per ad set per week, judged on each ad set's own Meta result. Window: lookback_days (7 to 90, default 30, ending yesterday in the account's time zone) or since and until (YYYY-MM-DD, up to 366 days). object_ids (up to 50 numeric campaign or ad set ids) or ad_ids (up to 50 numeric ad ids) limits the spend, funnel and goal sections to those; account settings and the asset lists stay account-wide. Derived here; not Meta's own summary."""
    resolved_account_id = _require_account_id(account_id)
    explicit, window_days = _context_window(lookback_days, since, until)
    has_ids = object_ids is not None and coerce_csv_string_list(object_ids) not in ([], "")
    has_ad_ids = ad_ids is not None and coerce_csv_string_list(ad_ids) not in ([], "")
    if has_ids and has_ad_ids:
        raise ValidationError("Give object_ids (campaigns or ad sets) or ad_ids, not both.")
    scope_ids = _require_graph_ids(object_ids, limit=MAX_CONTEXT_OBJECT_IDS) if has_ids else None
    scope_ad_ids = _require_graph_ids(ad_ids, field="ad_ids", limit=MAX_CONTEXT_OBJECT_IDS) if has_ad_ids else None
    client = get_graph_api_client()
    account = await client.get_object(resolved_account_id, fields=CONTEXT_ACCOUNT_FIELDS)
    today, reporting_timezone, timezone_signal = await _reporting_today(
        "account", resolved_account_id, account=account,
    )
    if explicit is None:
        end = today - timedelta(days=1)
        start = end - timedelta(days=window_days - 1)
        window_source = "lookback_days"
    else:
        start, end = explicit
        window_source = "since_until"
    if scope_ad_ids is not None:
        rows_read = _context_ad_rows(resolved_account_id, start, end, scope_ad_ids)
    else:
        rows_read = _context_adset_rows(resolved_account_id, start, end, scope_ids)
    (rows, result_error, row_coverage), *edge_sections = await asyncio.gather(
        rows_read,
        *(
            _optional_edge_section(client, resolved_account_id, edge, fields)
            for _, edge, fields in CONTEXT_EDGE_SECTIONS
        ),
    )
    ad_scope = scope_ad_ids is not None
    unit, units = ("ad", "ads") if ad_scope else ("ad set", "ad sets")
    missing_signals: list[str] = [timezone_signal] if timezone_signal else []
    if explicit is not None and end >= today:
        missing_signals.append(
            f"The window ends on {end.isoformat()}, on or after today ({today.isoformat()}) in the account's time "
            "zone, so its last days are incomplete or empty."
        )
    missing_signals.extend(_coverage_signal(row_coverage))
    scope: dict[str, Any] = {"level": "account", "object_id": resolved_account_id}
    object_scope: dict[str, Any] | None = None
    account_wide = ["account", "datasets", "custom_conversions", "pages", "instagram_accounts"]
    if scope_ids is not None:
        matched = {str(row.get(key)) for row in rows for key in ("campaign_id", "adset_id") if row.get(key)}
        unmatched = [value for value in scope_ids if value not in matched]
        scope["object_ids"] = scope_ids
        object_scope = {
            "object_ids": scope_ids,
            "matched_adset_rows": len(rows),
            "unmatched_object_ids": unmatched,
            "method": row_coverage["method"],
            "account_wide_sections": account_wide,
        }
        if unmatched and row_coverage["status"] != "failed":
            missing_signals.append(
                "No ad set insights rows in the window matched " + ", ".join(unmatched) + (
                    " among the rows read; the read stopped before Meta's last page, so their rows may not have "
                    "been read." if row_coverage["more_adsets_exist"]
                    else " (not a campaign or ad set of this account, or no delivery in the window)."
                )
            )
    if scope_ad_ids is not None:
        matched = {str(row.get("ad_id")) for row in rows if row.get("ad_id")}
        unmatched = [value for value in scope_ad_ids if value not in matched]
        scope["ad_ids"] = scope_ad_ids
        object_scope = {
            "ad_ids": scope_ad_ids,
            "matched_ad_rows": len(rows),
            "unmatched_ad_ids": unmatched,
            "method": row_coverage["method"],
            "account_wide_sections": account_wide,
        }
        if unmatched and row_coverage["status"] != "failed":
            missing_signals.append(
                "No ad insights rows in the window matched " + ", ".join(unmatched) + (
                    " among the rows read; the read stopped before Meta's last page, so their rows may not have "
                    "been read." if row_coverage["more_ads_exist"]
                    else " (not an ad of this account, or no delivery in the window)."
                )
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
    if not rows and row_coverage["status"] != "failed":
        missing_signals.append(f"Meta returned no {unit} insights rows for the window.")
    elif total_spend is None:
        missing_signals.append(
            f"At least one {unit} row had no spend, so total spend and spend shares are null rather than computed "
            "from zero."
        )
    if result_error is not None:
        missing_signals.append(
            f"Meta rejected the request for its result fields ({result_error}), so {unit} results are null and goal "
            "volume falls back to the goal's own event where that is known."
        )
    elif rows and not _results_reported(rows):
        missing_signals.append(
            f"Meta reported no result for any {unit}, so results are null and goal volume is judged only for goals "
            "whose event is known from the goal itself (LEAD_GENERATION, LINK_CLICKS, LANDING_PAGE_VIEWS)."
        )
    sections = {
        name: section for (name, _, _), section in zip(CONTEXT_EDGE_SECTIONS, edge_sections, strict=True)
    }
    section_errors = {name: section["error"] for name, section in sections.items() if not section["available"]}
    for name, error in section_errors.items():
        missing_signals.append(f"{name}: Meta returned an error, so this section is unknown ({error.get('message')}).")
    goal_volume = _ad_scope_goal_volume(rows) if ad_scope else _goal_volume(rows, window_days)
    unassessed = [entry for entry in goal_volume if entry["verdict"] == "not_assessed"]
    if unassessed:
        count_key = "ad_count" if ad_scope else "adset_count"
        missing_signals.append(
            "Weekly optimization event volume was not assessed for "
            + ", ".join(f"{entry['optimization_goal']} ({entry[count_key]} {units})" for entry in unassessed)
            + "; see each goal's reason in optimization_goal_volume."
        )

    extra: dict[str, Any] = {
        "source": "derived",
        "derived_note": (
            "Built by this server from the account's own data. This is not Meta's own advertiser summary."
        ),
        "window": {
            "since": start.isoformat(), "until": end.isoformat(), "days": window_days, "source": window_source,
            "weekly_volume_basis": (
                f"events in these {window_days} days / {window_days} x 7; there is no comparison period"
            ),
        },
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
        "spend_by_objective": _group_spend(
            rows, "objective", total_spend, result_error=result_error, unit="ad" if ad_scope else "adset",
        ),
        "spend_by_optimization_goal": _group_spend(
            rows, "optimization_goal", total_spend, result_error=result_error, unit="ad" if ad_scope else "adset",
        ),
        "funnel": _funnel(_sum_actions(rows)),
        "optimization_goal_volume": goal_volume,
        "optimization_goal_volume_rule": (
            f"An ad set meets the threshold when its events / window days x 7 is at least "
            f"{LEARNING_EVENTS_PER_WEEK}, following Meta's learning-phase guidance of about 50 optimization "
            "events per ad set in a 7-day period. Events are each ad set's own Meta result when it reports one, "
            "otherwise its goal's own event where the goal identifies it; a result that is not an action count "
            "(for example reach) is not judged, and windows shorter than 7 days are not judged. Only ad sets with "
            f"spend in the window are counted. Each goal lists counts and the {GOAL_VOLUME_TOP_ADSETS} "
            "highest-spending ad sets that meet and that miss the threshold, not every ad set."
        ),
        **sections,
        "section_errors": section_errors,
        "results_definition": RESULTS_DEFINITION,
        ("ad_coverage" if ad_scope else "adset_coverage"): row_coverage,
    }
    if object_scope is not None:
        extra["object_scope"] = object_scope
    return analysis_response(
        scope=scope,
        metrics={
            "spend": total_spend,
            ("ad_rows" if ad_scope else "adset_rows"): len(rows),
            "account_spend": row_coverage["account_spend"],
        },
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
    """Read recent spend per object at one level with at most two lean, spend-sorted insights pages. Never raises.

    A page Meta repeats is not added again and ends the read; a row that cannot be read (not an object, or no
    id) makes the read incomplete; an object whose row has no readable spend is listed in spend_unreadable
    rather than read as zero.
    """
    id_field = DELIVERY_ID_FIELDS[level]
    spend: dict[str, float] = {}
    spend_unreadable: set[str] = set()
    unreadable_rows = 0
    requests = 0
    complete = False
    error: str | None = None
    after: str | None = None
    lowest: float | None = None
    order_confirmed = True
    seen_cursors: set[str] = set()
    seen_pages: set[str] = set()
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
        page_rows, page_unreadable = _page_rows(payload)
        if _seen_page(page_rows, seen_pages):
            break
        unreadable_rows += page_unreadable
        for row in page_rows:
            if row.get(id_field) is None:
                unreadable_rows += 1
                continue
            object_id = str(row[id_field])
            value = _finite(row.get("spend"))
            if value is None:
                spend_unreadable.add(object_id)
                continue
            if lowest is not None and value > lowest:
                order_confirmed = False
            lowest = value if lowest is None else min(lowest, value)
            spend[object_id] = spend.get(object_id, 0.0) + value
        paging = extract_paging(payload)
        if not paging.get("next"):
            complete = True
            break
        following = paging.get("after")
        if not following or following == after or following in seen_cursors or wanted <= set(spend) | spend_unreadable:
            break
        seen_cursors.add(following)
        after = following
    return {
        "spend": {key: value for key, value in spend.items() if key not in spend_unreadable},
        "spend_unreadable": spend_unreadable,
        "rows_unreadable": unreadable_rows,
        "requests": requests,
        "status": "failed" if error and not spend and not spend_unreadable else "read",
        "complete": complete and error is None and not unreadable_rows,
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
        seen_pages: set[str] = set()
        rows_unreadable = 0
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
            page_rows, page_unreadable = _page_rows(payload)
            if _seen_page(page_rows, seen_pages):
                # Its objects are already listed; adding them again would list each problem twice.
                stop_reason = "page_repeated"
                break
            rows_unreadable += page_unreadable
            scanned.extend(page_rows)
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
        if rows_unreadable:
            # An object row that could not be read may hold a problem, so the scan is not complete.
            complete = False
            stop_reason = stop_reason or "rows_unreadable"
        scan = {"mode": "account_scan", "level": resolved_level, "objects_examined": len(scanned),
                "complete": complete, "next_after": next_after, "stop_reason": stop_reason,
                "pages_fetched": pages, "limit": limit, "effective_status_filter": statuses}
        if rows_unreadable:
            scan["rows_unreadable"] = rows_unreadable
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
        if read["spend_unreadable"]:
            spend_lookup["objects_with_unreadable_spend"] = len(read["spend_unreadable"])
        if read["rows_unreadable"]:
            spend_lookup["rows_unreadable"] = read["rows_unreadable"]
        for problem in object_problems:
            value = read["spend"].get(problem["object_id"] or "")
            if read["status"] == "failed" or (problem["object_id"] or "") in read["spend_unreadable"]:
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
        if scan["next_after"]:
            detail = f"pass after={scan['next_after']!r} to continue"
        elif scan["stop_reason"] == "page_repeated":
            detail = (
                "Meta returned a page that had already been read, so it was not counted again and the scan "
                "cannot continue"
            )
        elif scan["stop_reason"] == "rows_unreadable":
            detail = f"Meta returned {scan.get('rows_unreadable')} object row(s) that could not be read"
        else:
            detail = "Meta's pagination cursor was missing or repeated an earlier one, so the scan cannot continue"
        cap = f" after the {MAX_DELIVERY_PAGES}-page cap" if scan["stop_reason"] == "page_cap_reached" else ""
        missing_signals.append(
            f"Scan incomplete: examined {scan['objects_examined']} {resolved_level}(s) in {scan['pages_fetched']} "
            f"page(s){cap} and more may exist; {detail}. Problems on objects not yet examined are not listed."
        )
    if scan.get("rows_unreadable") and scan["stop_reason"] != "rows_unreadable":
        missing_signals.append(
            f"Meta also returned {scan['rows_unreadable']} object row(s) that could not be read; problems on them "
            "are not listed."
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
    if spend_lookup.get("objects_with_unreadable_spend"):
        missing_signals.append(
            f"{spend_lookup['objects_with_unreadable_spend']} {resolved_level}(s) had a spend row without a readable "
            "spend value; their spend is null (spend_unavailable), not zero."
        )
    if spend_lookup.get("rows_unreadable"):
        missing_signals.append(
            f"{spend_lookup['rows_unreadable']} spend row(s) could not be read, so the spend read is incomplete and "
            "a problem without spend is not assumed to have had none."
        )

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
