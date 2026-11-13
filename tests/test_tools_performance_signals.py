"""Tests for reporting and troubleshooting tools in performance_signals."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from meta_ads_mcp.errors import MetaApiError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.tools import diagnostics, insights, performance_signals, recommendations


class RecordingClient:
    """Fake Graph client that records every call and serves canned payloads."""

    def __init__(
        self,
        *,
        objects: dict[str, Any] | None = None,
        edges: dict[tuple[str, str], Any] | None = None,
        insights_handler: Callable[[str, list[str], dict[str, Any]], dict[str, Any]] | None = None,
        recommendations_payload: Any = None,
    ) -> None:
        self.objects = objects or {}
        self.edges = edges or {}
        self.insights_handler = insights_handler
        self.recommendations_payload = recommendations_payload
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _resolve(value: Any, *args: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(*args)
        return value

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append({"method": "get_object", "object_id": object_id, "fields": fields, "params": params})
        if object_id not in self.objects:
            raise AssertionError(f"unexpected get_object({object_id})")
        return self._resolve(self.objects[object_id], fields)

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.calls.append(
            {"method": "list_objects", "parent_id": parent_id, "edge": edge, "fields": fields, "params": dict(params or {})}
        )
        if (parent_id, edge) not in self.edges:
            raise AssertionError(f"unexpected list_objects({parent_id}, {edge})")
        return self._resolve(self.edges[(parent_id, edge)], dict(params or {}))

    async def get_insights(self, object_id: str, *, fields, params):
        self.calls.append({"method": "get_insights", "object_id": object_id, "fields": list(fields), "params": dict(params)})
        if self.insights_handler is None:
            raise AssertionError("unexpected get_insights")
        return self.insights_handler(object_id, list(fields), dict(params))

    async def get_recommendations(self, account_id: str, **kwargs):
        self.calls.append({"method": "get_recommendations", "account_id": account_id, **kwargs})
        return self._resolve(self.recommendations_payload)


def _patch(monkeypatch: pytest.MonkeyPatch, client: RecordingClient) -> RecordingClient:
    for module in (performance_signals, insights, diagnostics, recommendations):
        monkeypatch.setattr(module, "get_graph_api_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(recommendations, "_RECOMMENDATION_CACHE", {})
    return client


def _calls(client: RecordingClient, method: str) -> list[dict[str, Any]]:
    return [call for call in client.calls if call["method"] == method]


def _permission_error() -> MetaApiError:
    return MetaApiError(message="(#200) Permissions error", status_code=403, code=200)


# ---------------------------------------------------------------------------
# get_opportunity_score
# ---------------------------------------------------------------------------

REC_PAYLOAD = {
    "data": [
        {
            "recommendations": [
                {
                    "type": "CREATIVE_FATIGUE",
                    "recommendation_signature": "sig_small",
                    "object_ids": ["1"],
                    "recommendation_content": {"body": "Refresh creative", "opportunity_score_lift": "3"},
                },
                {
                    "type": "ADVANTAGE_PLUS_AUDIENCE",
                    "recommendation_signature": "sig_big",
                    "object_ids": ["2"],
                    "recommendation_content": {"body": "Use Advantage+ audience", "opportunity_score_lift": "12"},
                },
                {
                    "type": "FRAGMENTATION",
                    "recommendation_signature": "sig_none",
                    "object_ids": ["3"],
                    "recommendation_content": {"body": "Consolidate ad sets"},
                },
            ]
        }
    ]
}


def _history_payload(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": [
            {"date": "2026-11-01", "opportunity_score": "70"},
            {
                "date": "2026-11-02",
                "opportunity_score": "78",
                "changelog": [
                    {
                        "campaign_id": "12345",
                        "score_change": "+8",
                        "campaign_details": [
                            {
                                "ad_object_id": "1001",
                                "ad_object_type": "ad_set",
                                "budget_then": "5000",
                                "budget_now": "6000",
                            }
                        ],
                    }
                ],
            },
        ]
    }


def test_opportunity_score_reads_score_history_and_ranked_recommendations(monkeypatch) -> None:
    client = _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": {"id": "act_42", "name": "Shop", "currency": "USD", "opportunity_score": 74}},
            edges={("act_42", "opportunity_score_history"): _history_payload},
            recommendations_payload=REC_PAYLOAD,
        ),
    )
    monkeypatch.setattr(performance_signals, "_today", lambda: date(2026, 11, 12))

    result = asyncio.run(
        performance_signals.get_opportunity_score(account_id="42", history_days=14, include_history_reasons=True)
    )

    account_read = _calls(client, "get_object")[0]
    assert account_read["object_id"] == "act_42"
    assert "opportunity_score" in account_read["fields"]
    history_call = _calls(client, "list_objects")[0]
    assert history_call["edge"] == "opportunity_score_history"
    assert history_call["params"] == {"from_date": "2026-10-30", "to_date": "2026-11-12", "get_reason": True}
    assert _calls(client, "get_recommendations")[0]["account_id"] == "act_42"

    assert result["scope"] == {"level": "account", "object_id": "act_42"}
    assert result["opportunity_score"] == 74
    assert result["metrics"]["opportunity_score"] == 74
    history = result["history"]
    assert history["available"] is True
    assert [point["opportunity_score"] for point in history["points"]] == [70, 78]
    assert history["summary"]["first"] == 70 and history["summary"]["last"] == 78
    assert history["summary"]["change"] == 8
    detail = history["points"][1]["changelog"][0]["campaign_details"][0]
    assert detail["budget_then"] == 50.0 and detail["budget_now"] == 60.0
    top = result["top_recommendations"]
    assert [item["recommendation_signature"] for item in top["items"]] == ["sig_big", "sig_small", "sig_none"]
    assert top["items"][0]["estimated_score_gain"] == 12.0
    assert top["items"][2]["estimated_score_gain"] is None
    assert result["missing_signals"] == []


def test_opportunity_score_missing_field_is_reported(monkeypatch) -> None:
    _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": {"id": "act_42", "name": "Shop", "currency": "USD"}},
            recommendations_payload=REC_PAYLOAD,
        ),
    )

    result = asyncio.run(performance_signals.get_opportunity_score(account_id="act_42", include_history=False))

    assert result["opportunity_score"] is None
    assert any("opportunity_score" in signal for signal in result["missing_signals"])
    assert len(result["top_recommendations"]["items"]) == 3
    assert result["history"] is None


def test_opportunity_score_history_unsupported_is_reported_not_raised(monkeypatch) -> None:
    _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": {"id": "act_42", "currency": "USD", "opportunity_score": 50}},
            edges={("act_42", "opportunity_score_history"): UnsupportedFeatureError("Unsupported get request")},
            recommendations_payload={"data": []},
        ),
    )

    result = asyncio.run(performance_signals.get_opportunity_score(account_id="act_42"))

    assert result["history"]["available"] is False
    assert any("opportunity_score_history" in signal for signal in result["missing_signals"])


def test_opportunity_score_history_meta_error_is_reported_with_its_message(monkeypatch) -> None:
    error = MetaApiError(
        message="(#100) Tried accessing nonexisting field (opportunity_score_history) on node type (AdAccount)",
        status_code=400,
        code=100,
    )
    _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": {"id": "act_42", "currency": "USD", "opportunity_score": 50}},
            edges={("act_42", "opportunity_score_history"): error},
            recommendations_payload={"data": []},
        ),
    )

    result = asyncio.run(performance_signals.get_opportunity_score(account_id="act_42"))

    assert result["opportunity_score"] == 50
    assert result["history"]["available"] is False
    assert result["history"]["points"] is None
    assert "nonexisting field" in result["history"]["error"]["message"]
    assert result["history"]["error"]["code"] == 100


def test_opportunity_score_permission_error_is_not_swallowed(monkeypatch) -> None:
    _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": _permission_error()},
            recommendations_payload={"data": []},
        ),
    )
    with pytest.raises(MetaApiError, match="Permissions"):
        asyncio.run(performance_signals.get_opportunity_score(account_id="act_42"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": " "},
        {"account_id": "act_1", "history_days": 0},
        {"account_id": "act_1", "history_days": 46},
        {"account_id": "act_1", "top_n": 0},
    ],
)
def test_opportunity_score_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_opportunity_score(**kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# Daily insights helpers shared by trend and anomaly tests
# ---------------------------------------------------------------------------


def _meta_result(indicator: str, value: float | None) -> list[dict[str, Any]]:
    """Meta's results / cost_per_result shape: [{indicator, values: [{value, attribution_windows}]}]."""
    values = [] if value is None else [{"value": str(value), "attribution_windows": ["default"]}]
    return [{"indicator": indicator, "values": values}]


def _day_row(day: str, *, spend: float, impressions: int, clicks: int, purchases: float | None = None,
             purchase_value: float | None = None, result: tuple[str, float] | None = None,
             meta_results: bool = True) -> dict[str, Any]:
    """A daily row. Purchases are also Meta's result (actions:purchase) unless result names another one."""
    row: dict[str, Any] = {
        "date_start": day,
        "date_stop": day,
        "spend": str(spend),
        "impressions": str(impressions),
        "clicks": str(clicks),
    }
    if purchases is not None:
        row["actions"] = [{"action_type": "purchase", "value": str(purchases)}]
    if purchase_value is not None:
        row["action_values"] = [{"action_type": "purchase", "value": str(purchase_value)}]
    if result is not None:
        row["results"] = _meta_result(*result)
    elif purchases is not None and meta_results:
        row["results"] = _meta_result("actions:purchase", purchases)
    return row


def _daily_handler(rows: list[dict[str, Any]]):
    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        window = json.loads(params["time_range"])
        selected = [row for row in rows if window["since"] <= row["date_start"] <= window["until"]]
        return {"data": selected, "paging": {"cursors": {"after": "x"}}}

    return handler


# ---------------------------------------------------------------------------
# get_performance_trend
# ---------------------------------------------------------------------------


def test_trend_requests_daily_rows_and_reports_first_to_last_change(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-01", spend=100, impressions=10000, clicks=100, purchases=5, purchase_value=500),
        _day_row("2026-11-02", spend=110, impressions=10000, clicks=90, purchases=5, purchase_value=450),
        _day_row("2026-11-03", spend=120, impressions=10000, clicks=80, purchases=4, purchase_value=400),
    ]
    client = _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(
        performance_signals.get_performance_trend(
            level="campaign", object_id="cmp_1", since="2026-11-01", until="2026-11-03",
        )
    )

    call = _calls(client, "get_insights")[0]
    assert call["object_id"] == "cmp_1"
    assert call["params"]["level"] == "campaign"
    assert call["params"]["time_increment"] == 1
    assert json.loads(call["params"]["time_range"]) == {"since": "2026-11-01", "until": "2026-11-03"}
    assert {"spend", "impressions", "clicks", "actions", "action_values"} <= set(call["fields"])

    assert result["granularity"] == "day"
    assert [period["period_start"] for period in result["periods"]] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    first = result["periods"][0]["metrics"]
    assert first["cpc"] == pytest.approx(1.0)
    assert first["ctr"] == pytest.approx(0.01)
    assert first["cpm"] == pytest.approx(10.0)
    assert first["cost_per_result"] == pytest.approx(20.0)
    assert first["conversion_rate"] == pytest.approx(0.05)
    assert first["roas"] == pytest.approx(5.0)
    cpc = result["changes"]["cpc"]
    assert cpc["first"] == pytest.approx(1.0) and cpc["last"] == pytest.approx(1.5)
    assert cpc["change"] == pytest.approx(0.5) and cpc["pct_change"] == pytest.approx(0.5)
    assert cpc["direction"] == "up" and cpc["assessment"] == "worse"
    roas = result["changes"]["roas"]
    assert roas["direction"] == "down" and roas["assessment"] == "worse"
    assert set(result["changes"]) == {"cpc", "cpm", "cost_per_result", "roas", "ctr", "conversion_rate"}
    assert result["metric_definitions"]["cost_per_result"]["formula"] == "spend / results"


def test_trend_zero_denominators_yield_null_not_error(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-01", spend=0, impressions=0, clicks=0),
        _day_row("2026-11-02", spend=5, impressions=100, clicks=0),
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(
        performance_signals.get_performance_trend(
            level="ad", object_id="ad_1", since="2026-11-01", until="2026-11-02",
        )
    )

    zero_day = result["periods"][0]["metrics"]
    assert zero_day["ctr"] is None
    assert zero_day["cpc"] is None
    assert zero_day["cpm"] is None
    assert zero_day["conversion_rate"] is None
    assert zero_day["roas"] is None
    no_click_day = result["periods"][1]["metrics"]
    assert no_click_day["ctr"] == 0.0
    assert no_click_day["cpc"] is None
    assert no_click_day["conversion_rate"] is None


def test_trend_missing_delivery_input_makes_ratios_unknown_not_zero(monkeypatch) -> None:
    no_spend = _day_row("2026-11-02", spend=0, impressions=1000, clicks=10, purchases=1, purchase_value=40)
    del no_spend["spend"]
    no_impressions = _day_row("2026-11-03", spend=30, impressions=0, clicks=10)
    del no_impressions["impressions"]
    rows = [
        _day_row("2026-11-01", spend=100, impressions=1000, clicks=10, purchases=2, purchase_value=200),
        no_spend,
        no_impressions,
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(
        performance_signals.get_performance_trend(
            level="campaign", object_id="cmp_1", since="2026-11-01", until="2026-11-03",
            metrics=["cpc", "cpm", "ctr", "cost_per_result", "roas", "conversion_rate"],
        )
    )

    day2 = result["periods"][1]
    assert day2["inputs"]["spend"] is None
    assert day2["metrics"]["cpc"] is None
    assert day2["metrics"]["cpm"] is None
    assert day2["metrics"]["cost_per_result"] is None
    assert day2["metrics"]["roas"] is None
    assert day2["metrics"]["ctr"] == pytest.approx(0.01)
    day3 = result["periods"][2]
    assert day3["inputs"]["impressions"] is None
    assert day3["metrics"]["cpm"] is None and day3["metrics"]["ctr"] is None
    assert day3["metrics"]["cpc"] == pytest.approx(3.0)
    # The absent-action rule is separate: day 3 reports no purchase, so results count as zero.
    assert day3["inputs"]["results"] == 0.0
    assert day3["metrics"]["conversion_rate"] == 0.0
    assert result["changes"]["cpc"]["last_period"] == "2026-11-03"
    policy = result["input_policy"]
    assert "spend" in policy["delivery_inputs"] and "null" in policy["delivery_inputs"]
    assert "zero" in policy["action_inputs"]
    assert any(signal.startswith("spend: 1 period(s)") for signal in result["missing_signals"])
    assert any(signal.startswith("impressions: 1 period(s)") for signal in result["missing_signals"])


def test_weekly_period_with_one_day_missing_spend_is_unknown(monkeypatch) -> None:
    no_spend = _day_row("2026-11-05", spend=0, impressions=1000, clicks=10)
    del no_spend["spend"]
    rows = [_day_row("2026-11-04", spend=10, impressions=1000, clicks=10), no_spend]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(
        performance_signals.get_performance_trend(
            level="adset", object_id="as_1", since="2026-11-04", until="2026-11-05", granularity="week",
            metrics=["cpc"],
        )
    )
    assert result["periods"][0]["inputs"]["spend"] is None
    assert result["periods"][0]["metrics"]["cpc"] is None


def test_anomalies_day_missing_spend_is_not_scored(monkeypatch) -> None:
    rows = _spike_rows()
    del rows[-1]["spend"]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(
        performance_signals.detect_performance_anomalies(
            level="campaign", object_id="cmp_1", metrics=["spend", "cpc"], lookback_days=1, baseline_days=14,
            end_date="2026-11-15",
        )
    )
    assert result["anomalies"] == []
    assert result["metric_status"]["cpc"]["days_without_data"] == 1
    assert "delivery_inputs" in result["input_policy"]


def test_trend_weekly_buckets_are_derived_from_sums_and_marked_partial(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-04", spend=10, impressions=1000, clicks=10, purchases=1, purchase_value=30),
        _day_row("2026-11-05", spend=30, impressions=1000, clicks=30, purchases=1, purchase_value=50),
        _day_row("2026-11-10", spend=20, impressions=2000, clicks=20, purchases=2, purchase_value=40),
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(
        performance_signals.get_performance_trend(
            level="adset", object_id="as_1", since="2026-11-04", until="2026-11-10",
            granularity="week", metrics=["cpc", "roas"],
        )
    )

    periods = result["periods"]
    assert [(period["period_start"], period["period_end"]) for period in periods] == [
        ("2026-11-02", "2026-11-08"), ("2026-11-09", "2026-11-15"),
    ]
    assert periods[0]["partial"] is True and periods[0]["days_with_data"] == 2
    assert periods[0]["inputs"]["spend"] == 40.0
    assert periods[0]["metrics"]["cpc"] == pytest.approx(1.0)
    assert periods[0]["metrics"]["roas"] == pytest.approx(2.0)
    assert set(result["changes"]) == {"cpc", "roas"}
    assert result["changes"]["cpc"]["first_period_partial"] is True


def test_trend_monthly_default_window_uses_yesterday(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(
        objects={"act_42": {"timezone_name": "America/New_York"}}, insights_handler=_daily_handler([]),
    ))
    monkeypatch.setattr(performance_signals, "_now", lambda: datetime(2026, 11, 12, 17, tzinfo=timezone.utc))

    result = asyncio.run(
        performance_signals.get_performance_trend(level="account", object_id="42", granularity="month")
    )

    call = _calls(client, "get_insights")[0]
    assert call["object_id"] == "act_42"
    window = json.loads(call["params"]["time_range"])
    assert window["until"] == "2026-11-11"
    assert window["since"] == (date(2026, 11, 11) - timedelta(days=179)).isoformat()
    assert result["periods"] == []
    assert result["changes"]["cpc"]["direction"] is None
    assert any("no insights rows" in signal.lower() for signal in result["missing_signals"])


def _pin_now(monkeypatch, instant: datetime) -> None:
    monkeypatch.setattr(performance_signals, "_now", lambda: instant)
    monkeypatch.setattr(performance_signals, "_today", lambda: date(2026, 1, 1))  # server date must not be used


def test_trend_default_window_follows_account_time_zone_for_campaign_scope(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(
        objects={"cmp_1": {"id": "cmp_1", "account_id": "42"}, "act_42": {"timezone_name": "Asia/Tokyo"}},
        insights_handler=_daily_handler([]),
    ))
    # 18:00 UTC on Nov 12 is already Nov 13 in Tokyo, so the account's yesterday is Nov 12.
    _pin_now(monkeypatch, datetime(2026, 11, 12, 18, tzinfo=timezone.utc))

    result = asyncio.run(performance_signals.get_performance_trend(level="campaign", object_id="cmp_1"))

    reads = _calls(client, "get_object")
    assert [(call["object_id"], call["fields"]) for call in reads] == [
        ("cmp_1", ["account_id"]), ("act_42", ["timezone_name"]),
    ]
    window = json.loads(_calls(client, "get_insights")[0]["params"]["time_range"])
    assert window["until"] == "2026-11-12"
    assert result["window"]["until"] == "2026-11-12"
    assert result["reporting_timezone"] == {"timezone": "Asia/Tokyo", "source": "ad_account", "today": "2026-11-13"}


def test_anomalies_default_end_follows_account_time_zone_when_server_is_ahead(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(
        objects={"act_42": {"timezone_name": "Pacific/Honolulu"}},
        insights_handler=_daily_handler([]),
    ))
    # 05:00 UTC on Nov 12 is still Nov 11 in Honolulu, so the account's yesterday is Nov 10.
    _pin_now(monkeypatch, datetime(2026, 11, 12, 5, tzinfo=timezone.utc))

    result = asyncio.run(performance_signals.detect_performance_anomalies(level="account", object_id="42"))

    assert [call["object_id"] for call in _calls(client, "get_object")] == ["act_42"]
    assert result["lookback_window"]["until"] == "2026-11-10"
    assert result["reporting_timezone"]["timezone"] == "Pacific/Honolulu"


def test_default_window_falls_back_to_server_date_when_time_zone_unreadable(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(
        objects={"ad_1": {"id": "ad_1", "account_id": "42"}, "act_42": _permission_error()},
        insights_handler=_daily_handler([]),
    ))
    monkeypatch.setattr(performance_signals, "_today", lambda: date(2026, 11, 12))

    result = asyncio.run(performance_signals.get_performance_trend(level="ad", object_id="ad_1"))

    window = json.loads(_calls(client, "get_insights")[0]["params"]["time_range"])
    assert window["until"] == "2026-11-11"
    assert result["reporting_timezone"] == {"timezone": None, "source": "server_date", "today": "2026-11-12"}
    assert any("time zone could not be read" in signal and "Permissions" in signal
               for signal in result["missing_signals"])


def test_explicit_dates_need_no_time_zone_read(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler([])))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", since="2026-11-01", until="2026-11-02",
    ))
    assert _calls(client, "get_object") == []
    assert result["reporting_timezone"]["source"] == "explicit_dates"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"level": "creative", "object_id": "x"},
        {"level": "ad", "object_id": " "},
        {"level": "ad", "object_id": "x", "granularity": "hour"},
        {"level": "ad", "object_id": "x", "metrics": ["cpc", "vibes"]},
        {"level": "ad", "object_id": "x", "since": "2026-11-01"},
        {"level": "ad", "object_id": "x", "since": "2026-11-05", "until": "2026-11-01"},
        {"level": "ad", "object_id": "x", "since": "2025-01-01", "until": "2026-11-01"},
        {"level": "ad", "object_id": "x", "since": "11/01/2026", "until": "2026-11-02"},
    ],
)
def test_trend_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_performance_trend(**kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# detect_performance_anomalies
# ---------------------------------------------------------------------------


def _days(end: date, count: int) -> list[str]:
    return [(end - timedelta(days=count - 1 - index)).isoformat() for index in range(count)]


SPIKE_BASELINE_SPEND = [100, 102, 98, 101, 99, 103, 97, 100, 102, 98, 101, 99, 103, 97]


def _spike_rows() -> list[dict[str, Any]]:
    days = _days(date(2026, 11, 15), 15)
    rows = [
        _day_row(day, spend=spend, impressions=10000, clicks=100, purchases=5, purchase_value=500)
        for day, spend in zip(days[:-1], SPIKE_BASELINE_SPEND, strict=True)
    ]
    rows.append(_day_row(days[-1], spend=1000, impressions=10000, clicks=100, purchases=5, purchase_value=500))
    return rows


def test_anomalies_flat_baseline_does_not_flag_or_divide_by_zero(monkeypatch) -> None:
    rows = [
        _day_row(day, spend=100, impressions=10000, clicks=100, purchases=5, purchase_value=500)
        for day in _days(date(2026, 11, 15), 15)
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(
        performance_signals.detect_performance_anomalies(
            level="campaign", object_id="cmp_1", lookback_days=1, baseline_days=14, end_date="2026-11-15",
        )
    )

    assert result["anomalies"] == []
    assert not any(finding["type"] == "performance_anomaly" for finding in result["findings"])
    assert any("no variation" in signal for signal in result["missing_signals"])
    assert result["metric_status"]["spend"]["status"] == "no_variation"
    assert result["metric_status"]["spend"]["days_differing_from_flat_baseline"] == 0


def test_anomalies_short_history_says_so(monkeypatch) -> None:
    rows = [
        _day_row(day, spend=100 + index, impressions=10000, clicks=100)
        for index, day in enumerate(_days(date(2026, 11, 15), 10))
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(
        performance_signals.detect_performance_anomalies(
            level="ad", object_id="ad_1", lookback_days=1, baseline_days=14, end_date="2026-11-15",
        )
    )

    assert result["anomalies"] == []
    assert any("history" in signal for signal in result["missing_signals"])
    assert result["findings"][0]["type"] == "insufficient_data"
    assert result["baseline_rows"] == 9


def test_anomalies_flags_a_clear_spike_with_evidence(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(_spike_rows())))

    result = asyncio.run(
        performance_signals.detect_performance_anomalies(
            level="campaign", object_id="cmp_1", metrics=["spend"], lookback_days=1, baseline_days=14,
            end_date="2026-11-15",
        )
    )

    call = _calls(client, "get_insights")[0]
    assert json.loads(call["params"]["time_range"]) == {"since": "2026-11-01", "until": "2026-11-15"}
    assert call["params"]["time_increment"] == 1
    assert len(result["anomalies"]) == 1
    anomaly = result["anomalies"][0]
    assert anomaly["day"] == "2026-11-15"
    assert anomaly["metric"] == "spend"
    assert anomaly["value"] == 1000.0
    assert anomaly["baseline_median"] == 100.0
    assert anomaly["baseline_mad"] == 2.0
    assert anomaly["threshold_multiplier"] == 3.5
    assert anomaly["threshold_high"] == pytest.approx(100 + 3.5 * 1.4826 * 2.0)
    assert anomaly["direction"] == "above"
    assert anomaly["baseline_type"] == "all_days"
    assert anomaly["baseline_values"] == [float(value) for value in SPIKE_BASELINE_SPEND]
    finding = next(finding for finding in result["findings"] if finding["type"] == "performance_anomaly")
    assert finding["severity"] == "high"
    assert finding["confidence"] is None
    assert finding["evidence"][0]["inputs"]["baseline_median"] == 100.0
    assert "median" in result["rule"]["description"]


def test_anomalies_never_claims_significance(monkeypatch) -> None:
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(_spike_rows())))
    flagged = asyncio.run(
        performance_signals.detect_performance_anomalies(
            level="campaign", object_id="cmp_1", lookback_days=1, baseline_days=14, end_date="2026-11-15",
        )
    )
    assert flagged["anomalies"]
    rows = [_day_row(day, spend=100, impressions=10, clicks=1) for day in _days(date(2026, 11, 15), 15)]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    flat = asyncio.run(
        performance_signals.detect_performance_anomalies(
            level="campaign", object_id="cmp_1", lookback_days=1, baseline_days=14, end_date="2026-11-15",
        )
    )
    for payload in (flagged, flat):
        assert "significan" not in json.dumps(payload).lower()


def test_anomalies_use_same_weekday_baseline_when_four_or_more_exist(monkeypatch) -> None:
    end = date(2026, 11, 29)
    days = _days(end, 29)
    rows = []
    for day in days[:-1]:
        weekday_spend = 300 if date.fromisoformat(day).weekday() == end.weekday() else 100
        rows.append(_day_row(day, spend=weekday_spend + (len(rows) % 3), impressions=1000, clicks=10))
    rows.append(_day_row(days[-1], spend=301, impressions=1000, clicks=10))
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(
        performance_signals.detect_performance_anomalies(
            level="campaign", object_id="cmp_1", metrics=["spend"], lookback_days=1, baseline_days=28,
            end_date=end.isoformat(),
        )
    )

    assert result["anomalies"] == []
    assert result["metric_status"]["spend"]["baseline_types_used"] == ["same_weekday"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"level": "creative", "object_id": "x"},
        {"level": "ad", "object_id": ""},
        {"level": "ad", "object_id": "x", "lookback_days": 0},
        {"level": "ad", "object_id": "x", "lookback_days": 32},
        {"level": "ad", "object_id": "x", "baseline_days": 6},
        {"level": "ad", "object_id": "x", "baseline_days": 91},
        {"level": "ad", "object_id": "x", "sensitivity": "extreme"},
        {"level": "ad", "object_id": "x", "metrics": ["bogus"]},
        {"level": "ad", "object_id": "x", "end_date": "yesterday"},
    ],
)
def test_anomalies_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.detect_performance_anomalies(**kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# get_auction_ranking_report
# ---------------------------------------------------------------------------


def _ad_row(ad_id: str, *, spend: float, purchases: float, quality: str, engagement: str, conversion: str,
            impressions: int = 10000, clicks: int = 100) -> dict[str, Any]:
    row = _day_row("2026-10-01", spend=spend, impressions=impressions, clicks=clicks, purchases=purchases)
    row["date_stop"] = "2026-10-30"
    row.update({
        "ad_id": ad_id,
        "ad_name": f"Ad {ad_id}",
        "campaign_id": "cmp_1",
        "adset_id": "as_1",
        "quality_ranking": quality,
        "engagement_rate_ranking": engagement,
        "conversion_rate_ranking": conversion,
    })
    if not purchases:
        row.pop("actions", None)
    return row


RANKING_ROWS = [
    _ad_row("creative", spend=300, purchases=10, quality="BELOW_AVERAGE_20", engagement="AVERAGE", conversion="AVERAGE"),
    _ad_row("bid", spend=400, purchases=4, quality="ABOVE_AVERAGE", engagement="AVERAGE", conversion="AVERAGE"),
    _ad_row("postclick", spend=100, purchases=5, quality="AVERAGE", engagement="AVERAGE", conversion="BELOW_AVERAGE_35"),
    _ad_row("unknown", spend=20, purchases=1, quality="UNKNOWN", engagement="UNKNOWN", conversion="UNKNOWN"),
    _ad_row("healthy", spend=500, purchases=30, quality="ABOVE_AVERAGE", engagement="ABOVE_AVERAGE", conversion="AVERAGE"),
]


def test_auction_ranking_report_diagnoses_each_weak_ad(monkeypatch) -> None:
    client = _patch(
        monkeypatch,
        RecordingClient(insights_handler=lambda object_id, fields, params: {"data": RANKING_ROWS}),
    )

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="campaign", object_id="cmp_1"))

    call, cost_call = _ad_level_calls(client)
    assert call["object_id"] == "cmp_1"
    assert call["params"]["level"] == "ad"
    assert call["params"]["date_preset"] == "last_30d"
    assert {"quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking", "spend"} <= set(call["fields"])
    assert "actions" not in call["fields"]
    assert "actions" in cost_call["fields"]
    assert cost_call["params"]["filtering"][0]["value"] == ["creative", "bid", "postclick", "healthy"]

    weak = {ad["ad_id"]: ad for ad in result["weak_ads"]}
    assert set(weak) == {"creative", "bid", "postclick"}
    assert weak["creative"]["diagnosis"] == "creative_quality"
    assert weak["creative"]["below_average_rankings"] == ["quality_ranking"]
    assert weak["postclick"]["diagnosis"] == "conversion_path"
    assert weak["bid"]["diagnosis"] == "bid_and_budget"
    # scope cost per result = 1320 / 50 = 26.4; the bid ad's is 100.
    assert weak["bid"]["cost_per_result_vs_scope"] == pytest.approx(100 / 26.4)
    assert [ad["ad_id"] for ad in result["weak_ads"]] == ["bid", "creative", "postclick"]
    assert result["ranked_ads"][0]["ad_id"] == "creative"
    assert result["ranked_ads"][-1]["ad_id"] == "unknown"
    assert result["unranked_ad_count"] == 1
    assert result["scope_metrics"]["cost_per_result"] == pytest.approx(26.4)
    assert result["rules"]["cost_ratio_threshold"] == 1.5
    types = {finding["type"] for finding in result["findings"]}
    assert types == {"weak_auction_ranking"}
    assert all(finding["evidence"] for finding in result["findings"])


@pytest.mark.parametrize(
    ("quality", "engagement"),
    [("UNKNOWN", "UNKNOWN"), ("UNKNOWN", "AVERAGE"), ("ABOVE_AVERAGE", None)],
)
def test_ranking_conversion_path_needs_known_quality_and_engagement(quality, engagement) -> None:
    rankings = {"quality_ranking": quality, "engagement_rate_ranking": engagement,
                "conversion_rate_ranking": "BELOW_AVERAGE_10"}
    metrics = {"spend": 10.0, "results": 1.0, "cost_per_result": 10.0}

    diagnosis, reason, below = performance_signals._ranking_diagnosis(rankings, metrics, metrics)

    assert diagnosis is None
    assert below == ["conversion_rate_ranking"]
    assert "unknown" in reason


def test_ranking_conversion_path_when_quality_and_engagement_are_known_and_average() -> None:
    rankings = {"quality_ranking": "AVERAGE", "engagement_rate_ranking": "ABOVE_AVERAGE",
                "conversion_rate_ranking": "BELOW_AVERAGE_10"}
    metrics = {"spend": 10.0, "results": 1.0, "cost_per_result": 10.0}
    diagnosis, _, _ = performance_signals._ranking_diagnosis(rankings, metrics, metrics)
    assert diagnosis == "conversion_path"


def _ad_level_calls(client: RecordingClient) -> list[dict[str, Any]]:
    return [call for call in _calls(client, "get_insights") if call["params"]["level"] == "ad"]


def _many_ads(count: int) -> list[dict[str, Any]]:
    """Ad rows already in spend-descending order, as Meta returns them with sort=spend_descending."""
    return [
        _ad_row(f"ad_{index:04d}", spend=float(5000 - index), purchases=2, quality="AVERAGE",
                engagement="AVERAGE", conversion="AVERAGE")
        for index in range(count)
    ]


def _account_ranking_handler(ad_rows: list[dict[str, Any]], *, scope_spend: float, more: bool = True):
    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if params["level"] == "ad":
            payload: dict[str, Any] = {"data": ad_rows[: params["limit"]]}
            if more:
                payload["paging"] = {"cursors": {"after": "next"}, "next": "https://graph.example/next"}
            return payload
        return {"data": [{
            "date_start": "2026-10-13", "date_stop": "2026-11-11", "spend": str(scope_spend),
            "impressions": "5000000", "clicks": "50000",
            "actions": [{"action_type": "purchase", "value": "5000"}],
        }]}

    return handler


def test_auction_ranking_report_account_level_reads_top_spending_ads_in_one_bounded_call(monkeypatch) -> None:
    ad_rows = _many_ads(2500)
    examined_spend = sum(5000 - index for index in range(200))
    client = _patch(monkeypatch, RecordingClient(
        insights_handler=_account_ranking_handler(ad_rows, scope_spend=examined_spend * 2),
    ))

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="account", object_id="42", top_n=5))

    ad_calls = _ad_level_calls(client)
    assert len(ad_calls) == 2
    params = ad_calls[0]["params"]
    assert ad_calls[0]["object_id"] == "act_42"
    assert params["sort"] == ["spend_descending"]
    assert params["limit"] == performance_signals.RANKING_DEFAULT_MAX_ADS == 200
    assert "after" not in params
    assert "actions" not in ad_calls[0]["fields"]
    cost_params = ad_calls[1]["params"]
    assert "sort" not in cost_params
    assert cost_params["limit"] == performance_signals.RANKING_COST_MAX_ADS
    assert len(cost_params["filtering"][0]["value"]) == performance_signals.RANKING_COST_MAX_ADS
    scope_calls = [call for call in _calls(client, "get_insights") if call["params"]["level"] == "account"]
    assert len(scope_calls) == 1
    assert scope_calls[0]["object_id"] == "act_42"
    assert "spend" in scope_calls[0]["fields"]
    assert len(client.calls) == 3

    examined = result["examined"]
    assert examined["ads_examined"] == 200
    assert examined["selection"] == "highest_spend_first"
    assert examined["more_ads_exist"] is True
    assert examined["examined_spend"] == pytest.approx(examined_spend)
    assert examined["scope_spend"] == pytest.approx(examined_spend * 2)
    assert examined["share_of_scope_spend"] == pytest.approx(0.5)
    assert "200 highest-spending ads" in examined["note"]
    assert "50%" in examined["note"]
    assert any("200 highest-spending ads" in signal for signal in result["missing_signals"])
    assert result["ads_analyzed"] == 200
    assert len(result["ranked_ads"]) == 5


def test_auction_ranking_report_max_ads_bounds_the_request(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(
        insights_handler=_account_ranking_handler(_many_ads(50), scope_spend=1000.0, more=False),
    ))
    result = asyncio.run(performance_signals.get_auction_ranking_report(level="account", object_id="42", max_ads=20))
    assert _ad_level_calls(client)[0]["params"]["limit"] == 20
    assert result["examined"]["ads_examined"] == 20
    assert result["examined"]["more_ads_exist"] is False


def test_auction_ranking_report_scope_spend_failure_is_reported_not_raised(monkeypatch) -> None:
    rows = _many_ads(3)

    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if params["level"] == "ad":
            return {"data": rows}
        raise _permission_error()

    _patch(monkeypatch, RecordingClient(insights_handler=handler))
    result = asyncio.run(performance_signals.get_auction_ranking_report(level="campaign", object_id="cmp_1"))
    assert result["examined"]["share_of_scope_spend"] is None
    assert result["examined"]["scope_spend"] is None
    assert result["scope_metrics_basis"] == "examined_ads_with_cost_evidence"
    assert any("scope" in signal and "Permissions" in signal for signal in result["missing_signals"])


def test_auction_ranking_report_flags_rows_not_ordered_by_spend(monkeypatch) -> None:
    rows = list(reversed(_many_ads(3)))
    _patch(monkeypatch, RecordingClient(
        insights_handler=_account_ranking_handler(rows, scope_spend=20000.0, more=True),
    ))
    result = asyncio.run(performance_signals.get_auction_ranking_report(level="account", object_id="42"))
    assert result["examined"]["meta_order_confirmed"] is False
    assert any("not ordered by spend" in signal for signal in result["missing_signals"])


def test_auction_ranking_report_top_n_limits_lists(monkeypatch) -> None:
    _patch(monkeypatch, RecordingClient(insights_handler=lambda object_id, fields, params: {"data": RANKING_ROWS}))
    result = asyncio.run(
        performance_signals.get_auction_ranking_report(level="account", object_id="42", top_n=1, since="2026-10-01",
                                                       until="2026-10-30")
    )
    assert len(result["weak_ads"]) == 1 and len(result["ranked_ads"]) == 1
    assert result["weak_ad_count"] == 3
    assert result["scope"]["object_id"] == "act_42"


def test_auction_ranking_report_no_rows_says_so(monkeypatch) -> None:
    _patch(monkeypatch, RecordingClient(insights_handler=lambda object_id, fields, params: {"data": []}))
    result = asyncio.run(performance_signals.get_auction_ranking_report(level="adset", object_id="as_1"))
    assert result["weak_ads"] == []
    assert result["findings"][0]["type"] == "insufficient_data"


def _unknown_error() -> MetaApiError:
    return MetaApiError(message="An unknown error occurred", status_code=500, code=1)


LEAN_RANKING_KEYS = {
    "ad_id", "ad_name", "campaign_id", "adset_id", "spend", "impressions",
    "quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking", "date_start", "date_stop",
}


def _meta_like_ranking_handler(ad_rows: list[dict[str, Any]], *, scope_spend: float, more: bool = True,
                               cost_error: Exception | None = None, scope_heavy_error: bool = False):
    """Models the live account: the sorted ad-level read fails when it carries action fields, works when lean."""

    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if params["level"] == "ad":
            if "sort" in params:
                if {"actions", "action_values"} & set(fields):
                    raise _unknown_error()
                payload: dict[str, Any] = {
                    "data": [{key: value for key, value in row.items() if key in LEAN_RANKING_KEYS}
                             for row in ad_rows[: params["limit"]]],
                }
                if more:
                    payload["paging"] = {"cursors": {"after": "next"}, "next": "https://graph.example/next"}
                return payload
            if cost_error is not None:
                raise cost_error
            (selector,) = params["filtering"]
            assert selector["field"] == "ad.id" and selector["operator"] == "IN"
            return {"data": [row for row in ad_rows if row["ad_id"] in selector["value"]]}
        if scope_heavy_error and "actions" in fields:
            raise _unknown_error()
        row: dict[str, Any] = {"spend": str(scope_spend), "impressions": "5000000"}
        if "actions" in fields:
            row.update({"clicks": "50000", "actions": [{"action_type": "purchase", "value": "5000"}]})
        return {"data": [row]}

    return handler


def test_auction_ranking_report_sorted_read_is_lean_so_heavy_failure_cannot_happen(monkeypatch) -> None:
    rows = _many_ads(300)
    client = _patch(monkeypatch, RecordingClient(insights_handler=_meta_like_ranking_handler(rows, scope_spend=1e7)))

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="account", object_id="42"))

    sorted_calls = [call for call in _ad_level_calls(client) if "sort" in call["params"]]
    assert len(sorted_calls) == 1
    lean = sorted_calls[0]
    assert lean["params"]["sort"] == ["spend_descending"]
    assert set(lean["fields"]) == {
        "ad_id", "ad_name", "campaign_id", "adset_id", "spend", "impressions",
        "quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking",
    }
    assert result["examined"]["ads_examined"] == 200
    assert result["cost_evidence"]["status"] == "read"


def test_auction_ranking_report_cost_read_is_one_request_for_selected_ads_with_known_rankings(monkeypatch) -> None:
    rows = [
        _ad_row("a1", spend=900, purchases=1, quality="AVERAGE", engagement="AVERAGE", conversion="AVERAGE"),
        _ad_row("a2", spend=800, purchases=9, quality="UNKNOWN", engagement="UNKNOWN", conversion="UNKNOWN"),
        _ad_row("a3", spend=700, purchases=7, quality="BELOW_AVERAGE_10", engagement="UNKNOWN", conversion="UNKNOWN"),
    ]
    client = _patch(monkeypatch, RecordingClient(
        insights_handler=_meta_like_ranking_handler(rows, scope_spend=2400.0, more=False),
    ))

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="campaign", object_id="cmp_1",
                                                                       date_preset="last_7d"))

    cost_calls = [call for call in _ad_level_calls(client) if "filtering" in call["params"]]
    assert len(cost_calls) == 1
    cost = cost_calls[0]
    assert cost["params"]["filtering"] == [{"field": "ad.id", "operator": "IN", "value": ["a1", "a3"]}]
    assert "sort" not in cost["params"]
    assert cost["params"]["date_preset"] == "last_7d"
    assert cost["params"]["limit"] == 2
    assert {"actions", "action_values", "ad_id"} <= set(cost["fields"])
    ads = {ad["ad_id"]: ad for ad in result["ranked_ads"]}
    assert ads["a1"]["results"] == 1.0 and ads["a1"]["cost_per_result"] == 900.0
    assert ads["a2"]["results"] is None and ads["a2"]["cost_per_result"] is None
    assert result["cost_evidence"]["ads_requested"] == 2 and result["cost_evidence"]["ads_returned"] == 2
    assert any("a" in signal and "cost per result" in signal.lower() for signal in result["missing_signals"])


def test_auction_ranking_report_cost_read_failure_keeps_rankings_diagnosis(monkeypatch) -> None:
    rows = [
        _ad_row("weak", spend=900, purchases=0, quality="BELOW_AVERAGE_10", engagement="AVERAGE", conversion="AVERAGE"),
        _ad_row("fine", spend=800, purchases=0, quality="AVERAGE", engagement="AVERAGE", conversion="AVERAGE"),
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_meta_like_ranking_handler(
        rows, scope_spend=1700.0, more=False, cost_error=_unknown_error(),
    )))

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="account", object_id="42"))

    assert [(ad["ad_id"], ad["diagnosis"]) for ad in result["weak_ads"]] == [("weak", "creative_quality")]
    fine = next(ad for ad in result["ranked_ads"] if ad["ad_id"] == "fine")
    assert fine["results"] is None and fine["diagnosis"] is None
    assert result["cost_evidence"]["status"] == "failed"
    assert "unknown error" in result["cost_evidence"]["error"]
    assert any("cost-per-result evidence was not available" in signal.lower() for signal in result["missing_signals"])


def test_auction_ranking_report_says_plainly_when_few_rankings_are_known(monkeypatch) -> None:
    rows = _many_ads(200)
    for index, row in enumerate(rows):
        if index >= 3:
            row.update({"quality_ranking": "UNKNOWN", "engagement_rate_ranking": "UNKNOWN",
                        "conversion_rate_ranking": "UNKNOWN"})
    _patch(monkeypatch, RecordingClient(insights_handler=_meta_like_ranking_handler(rows, scope_spend=1e6)))

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="account", object_id="42"))

    coverage = result["ranking_coverage"]
    assert coverage == {
        "ads_examined": 200, "quality_ranking_known": 3, "engagement_rate_ranking_known": 3,
        "conversion_rate_ranking_known": 3, "all_three_known": 3,
    }
    limited = next(finding for finding in result["findings"] if finding["type"] == "limited_ranking_evidence")
    assert "3 of 200" in limited["summary"]
    assert result["findings"][0]["type"] == "limited_ranking_evidence"


def test_auction_ranking_report_scope_total_falls_back_to_lean_fields(monkeypatch) -> None:
    rows = _many_ads(10)
    client = _patch(monkeypatch, RecordingClient(insights_handler=_meta_like_ranking_handler(
        rows, scope_spend=sum(5000 - index for index in range(10)) * 4, more=False, scope_heavy_error=True,
    )))

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="account", object_id="42"))

    scope_calls = [call for call in _calls(client, "get_insights") if call["params"]["level"] == "account"]
    assert len(scope_calls) == 2
    assert set(scope_calls[1]["fields"]) == {"spend", "impressions"}
    assert result["examined"]["share_of_scope_spend"] == pytest.approx(0.25)
    assert result["scope_metrics_basis"] == "examined_ads_with_cost_evidence"


def test_ranking_zero_results_rule_needs_known_results() -> None:
    rankings = {field: "AVERAGE" for field in performance_signals.RANKING_FIELDS}
    metrics = {"spend": 500.0, "results": None, "cost_per_result": None}
    scope = {"spend": 1000.0, "results": 10.0, "cost_per_result": 100.0}
    assert performance_signals._ranking_diagnosis(rankings, metrics, scope)[0] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"level": "business", "object_id": "1"},
        {"level": "campaign", "object_id": " "},
        {"level": "campaign", "object_id": "1", "top_n": 0},
        {"level": "campaign", "object_id": "1", "since": "2026-10-01"},
        {"level": "campaign", "object_id": "1", "date_preset": None, "since": "2026-10-05", "until": "2026-10-02"},
        {"level": "campaign", "object_id": "1", "max_ads": 0},
        {"level": "campaign", "object_id": "1", "max_ads": 501},
    ],
)
def test_auction_ranking_report_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_auction_ranking_report(**kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# get_advertiser_context
# ---------------------------------------------------------------------------


def _adset_row(adset_id: str, *, objective: str, goal: str, spend: float, actions: dict[str, float],
               values: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "campaign_id": f"cmp_{adset_id}",
        "adset_id": adset_id,
        "adset_name": f"Ad set {adset_id}",
        "objective": objective,
        "optimization_goal": goal,
        "date_start": "2026-10-13",
        "date_stop": "2026-11-11",
        "spend": str(spend),
        "impressions": "100000",
        "clicks": "2000",
        "actions": [{"action_type": key, "value": str(value)} for key, value in actions.items()],
        "action_values": [{"action_type": key, "value": str(value)} for key, value in (values or {}).items()],
    }


CONTEXT_ROWS = [
    _adset_row("as_a", objective="OUTCOME_SALES", goal="OFFSITE_CONVERSIONS", spend=600,
               actions={"link_click": 1500, "landing_page_view": 1200, "add_to_cart": 400,
                        "initiate_checkout": 350, "purchase": 300}, values={"purchase": 9000}),
    _adset_row("as_b", objective="OUTCOME_SALES", goal="OFFSITE_CONVERSIONS", spend=200,
               actions={"link_click": 500, "landing_page_view": 300, "add_to_cart": 100,
                        "initiate_checkout": 50, "purchase": 30}),
    _adset_row("as_c", objective="OUTCOME_TRAFFIC", goal="LINK_CLICKS", spend=200,
               actions={"link_click": 1000, "landing_page_view": 500}),
]


def _context_client(overrides: dict[tuple[str, str], Any] | None = None) -> RecordingClient:
    edges: dict[tuple[str, str], Any] = {
        ("act_42", "adspixels"): {"data": [{"id": "px_1", "name": "Main pixel", "last_fired_time": "2026-11-11T10:00:00+0000"}]},
        ("act_42", "customconversions"): {"data": [{"id": "cc_1", "name": "Big purchase", "custom_event_type": "PURCHASE"}]},
        ("act_42", "promote_pages"): {"data": [{"id": "page_1", "name": "Shop Page"}]},
        ("act_42", "instagram_accounts"): {"data": [{"id": "ig_1", "username": "shop"}], "paging": {"next": "n", "cursors": {"after": "c"}}},
    }
    edges.update(overrides or {})
    return RecordingClient(
        objects={
            "act_42": {
                "id": "act_42",
                "name": "Shop",
                "account_status": 1,
                "disable_reason": 0,
                "currency": "USD",
                "timezone_name": "America/New_York",
                "business": {"id": "biz_1", "name": "Shop Inc"},
                "amount_spent": "123456",
                "spend_cap": "0",
            }
        },
        edges=edges,
        insights_handler=lambda object_id, fields, params: {"data": CONTEXT_ROWS},
    )


def test_advertiser_context_labels_itself_as_derived(monkeypatch) -> None:
    _patch(monkeypatch, _context_client())
    result = asyncio.run(performance_signals.get_advertiser_context(account_id="42"))
    assert result["source"] == "derived"
    assert "not Meta's own" in result["derived_note"]


def test_advertiser_context_builds_each_section_from_account_data(monkeypatch) -> None:
    client = _patch(monkeypatch, _context_client())
    monkeypatch.setattr(performance_signals, "_now", lambda: datetime(2026, 11, 12, 17, tzinfo=timezone.utc))

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", lookback_days=28))

    insights_call = next(call for call in _calls(client, "get_insights") if "filtering" in call["params"])
    assert insights_call["object_id"] == "act_42"
    assert insights_call["params"]["level"] == "adset"
    assert json.loads(insights_call["params"]["time_range"]) == {"since": "2026-10-15", "until": "2026-11-11"}
    assert {"objective", "optimization_goal", "actions"} <= set(insights_call["fields"])
    edges = {call["edge"] for call in _calls(client, "list_objects")}
    assert edges == {"adspixels", "customconversions", "promote_pages", "instagram_accounts"}

    account = result["account"]
    assert account["account_status"] == {"code": 1, "label": "ACTIVE"}
    assert account["disable_reason"] == {"code": 0, "label": "NONE"}
    assert account["business"] == {"id": "biz_1", "name": "Shop Inc"}
    assert account["amount_spent"] == 1234.56

    by_objective = {row["objective"]: row for row in result["spend_by_objective"]}
    assert by_objective["OUTCOME_SALES"]["spend"] == 800.0
    assert by_objective["OUTCOME_SALES"]["spend_share"] == pytest.approx(0.8)
    assert by_objective["OUTCOME_SALES"]["adset_count"] == 2
    by_goal = {row["optimization_goal"]: row for row in result["spend_by_optimization_goal"]}
    assert by_goal["LINK_CLICKS"]["spend"] == 200.0

    steps = {step["step"]: step for step in result["funnel"]["steps"]}
    assert steps["link_click"]["count"] == 3000
    assert steps["landing_page_view"]["count"] == 2000
    assert steps["purchase"]["count"] == 330
    assert steps["view_content"]["count"] is None
    transitions = {(row["from"], row["to"]): row for row in result["funnel"]["drop_off"]}
    assert transitions[("link_click", "landing_page_view")]["drop_off"] == pytest.approx(1 - 2000 / 3000)
    assert transitions[("add_to_cart", "initiate_checkout")]["drop_off"] == pytest.approx(1 - 400 / 500)

    volume = {row["optimization_goal"]: row for row in result["optimization_goal_volume"]}
    conversions = volume["OFFSITE_CONVERSIONS"]
    assert conversions["verdict"] == "not_assessed"
    assert conversions["adset_count"] == 2 and conversions["spend"] == 800.0
    assert "promoted_object" in conversions["reason"]
    assert [row["adset_id"] for row in conversions["top_adsets_by_spend"]] == ["as_a", "as_b"]
    clicks = volume["LINK_CLICKS"]
    assert clicks["threshold_events_per_week"] == 50
    assert clicks["verdict"] == "all_adsets_meet_threshold"
    assert clicks["top_meeting_threshold"][0]["events_per_week"] == pytest.approx(1000 / 28 * 7)
    assert clicks["top_meeting_threshold"][0]["spend"] == 200.0
    assert "adsets" not in clicks

    assert result["datasets"]["items"][0]["id"] == "px_1"
    assert result["custom_conversions"]["count"] == 1
    assert result["pages"]["items"][0]["name"] == "Shop Page"
    assert result["instagram_accounts"]["complete"] is False
    assert result["section_errors"] == {}


def _large_context_rows(count: int) -> list[dict[str, Any]]:
    goals = [
        ("OUTCOME_SALES", "OFFSITE_CONVERSIONS", {"purchase": 10}),
        ("OUTCOME_SALES", "VALUE", {"purchase": 400}),
        ("OUTCOME_TRAFFIC", "LINK_CLICKS", {"link_click": 900}),
        ("OUTCOME_LEADS", "LEAD_GENERATION", {"lead": 5}),
        ("OUTCOME_AWARENESS", "REACH", {}),
    ]
    rows = []
    for index in range(count):
        objective, goal, actions = goals[index % len(goals)]
        row = _adset_row(f"as_{index:04d}", objective=objective, goal=goal, spend=100 + index, actions=actions)
        row["adset_name"] = f"Prospecting broad lookalike ad set number {index} with a long descriptive name"
        rows.append(row)
    return rows


def test_advertiser_context_summarizes_goal_volume_with_top_spenders(monkeypatch) -> None:
    client = _context_client()
    rows = _large_context_rows(300)
    client.insights_handler = lambda object_id, fields, params: {"data": rows}
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", lookback_days=28))

    volume = {row["optimization_goal"]: row for row in result["optimization_goal_volume"]}
    leads = volume["LEAD_GENERATION"]
    # The fake returns rows unsorted; the tool examines the first 250 the sorted read returns.
    assert result["adset_coverage"]["adsets_examined"] == 250 and result["adset_coverage"]["complete"] is False
    assert leads["adset_count"] == 50
    assert leads["adsets_meeting_threshold"] == 0
    assert leads["adsets_below_threshold"] == 50
    below = leads["top_below_threshold"]
    assert len(below) == performance_signals.GOAL_VOLUME_TOP_ADSETS
    assert [row["spend"] for row in below] == sorted((row["spend"] for row in below), reverse=True)
    assert below[0]["adset_id"] == "as_0248"
    assert volume["LINK_CLICKS"]["adsets_meeting_threshold"] == 50
    assert volume["OFFSITE_CONVERSIONS"]["verdict"] == "not_assessed"
    assert len(volume["OFFSITE_CONVERSIONS"]["top_adsets_by_spend"]) == performance_signals.GOAL_VOLUME_TOP_ADSETS
    assert volume["REACH"]["verdict"] == "not_assessed"
    assert len(volume["REACH"]["top_adsets_by_spend"]) <= performance_signals.GOAL_VOLUME_TOP_ADSETS


def _goal_row(adset_id: str, goal: str, actions: dict[str, float], spend: float = 100.0) -> dict[str, Any]:
    return {
        "adset_id": adset_id, "adset_name": adset_id, "optimization_goal": goal,
        "metrics": {"spend": spend, "conversions": actions.get("purchase", actions.get("lead"))},
        "actions_map": actions,
    }


def test_goal_volume_counts_leads_for_lead_generation_not_purchases() -> None:
    rows = [
        _goal_row("purchases_only", "LEAD_GENERATION", {"purchase": 100, "lead": 0}, spend=300),
        _goal_row("pixel_leads", "LEAD_GENERATION", {"offsite_conversion.fb_pixel_lead": 60, "purchase": 5}, spend=200),
    ]

    volume = performance_signals._goal_volume(rows, 7)

    lead = volume[0]
    assert lead["optimization_goal"] == "LEAD_GENERATION"
    assert lead["adsets_meeting_threshold"] == 1 and lead["adsets_below_threshold"] == 1
    assert lead["top_below_threshold"][0]["adset_id"] == "purchases_only"
    assert lead["top_below_threshold"][0]["events"] == 0
    assert lead["top_meeting_threshold"][0]["events"] == 60
    assert "lead" in lead["event_basis"] and "purchase" not in lead["event_basis"]


@pytest.mark.parametrize("goal", ["OFFSITE_CONVERSIONS", "VALUE", "QUALITY_LEAD"])
def test_goal_volume_does_not_judge_goals_whose_event_is_unidentified(goal) -> None:
    rows = [_goal_row("as_1", goal, {"purchase": 500, "lead": 500})]

    entry = performance_signals._goal_volume(rows, 7)[0]

    assert entry["verdict"] == "not_assessed"
    assert "adsets_meeting_threshold" not in entry
    assert entry["reason"]
    assert entry["top_adsets_by_spend"][0]["adset_id"] == "as_1"


def test_advertiser_context_reports_unassessed_goals_as_missing_signals(monkeypatch) -> None:
    _patch(monkeypatch, _context_client())
    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42"))
    assert any("OFFSITE_CONVERSIONS" in signal and "not assessed" in signal for signal in result["missing_signals"])


def test_advertiser_context_response_stays_small_for_300_adsets(monkeypatch) -> None:
    client = _context_client()
    rows = _large_context_rows(300)
    client.insights_handler = lambda object_id, fields, params: {"data": rows}
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42"))

    assert result["metrics"]["adset_rows"] == performance_signals.CONTEXT_MAX_ADSETS
    assert len(json.dumps(result).encode()) < 20_000


def test_funnel_picks_the_alias_per_row_before_summing() -> None:
    rows = [
        {"actions_map": {"link_click": 100, "purchase": 10, "lead": 2}},
        {"actions_map": {"link_click": 100, "offsite_conversion.fb_pixel_purchase": 20,
                         "offsite_conversion.fb_pixel_lead": 3}},
        {"actions_map": {"link_click": 50, "purchase": 5, "omni_purchase": 5}},
    ]

    funnel = performance_signals._funnel(performance_signals._sum_actions(rows))

    steps = {step["step"]: step for step in funnel["steps"]}
    assert steps["purchase"]["count"] == 35
    assert steps["purchase"]["action_types_used"] == ["purchase", "offsite_conversion.fb_pixel_purchase"]
    assert steps["link_click"]["count"] == 250
    drop = next(row for row in funnel["drop_off"] if row["to"] == "purchase")
    assert drop["drop_off"] == pytest.approx(1 - 35 / 250)
    assert funnel["lead_events"]["count"] == 5
    assert funnel["lead_events"]["action_types_used"] == ["lead", "offsite_conversion.fb_pixel_lead"]


@pytest.mark.parametrize(
    ("timezone_name", "instant", "until"),
    [
        ("Asia/Tokyo", datetime(2026, 11, 12, 18, tzinfo=timezone.utc), "2026-11-12"),
        ("Pacific/Honolulu", datetime(2026, 11, 12, 5, tzinfo=timezone.utc), "2026-11-10"),
    ],
)
def test_advertiser_context_window_ends_on_the_account_yesterday(monkeypatch, timezone_name, instant, until) -> None:
    client = _context_client()
    client.objects["act_42"] = {**client.objects["act_42"], "timezone_name": timezone_name}
    _patch(monkeypatch, client)
    _pin_now(monkeypatch, instant)

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", lookback_days=7))

    assert result["window"]["until"] == until
    assert json.loads(_calls(client, "get_insights")[0]["params"]["time_range"])["until"] == until
    assert result["reporting_timezone"]["timezone"] == timezone_name
    assert len(_calls(client, "get_object")) == 1


def test_advertiser_context_section_permission_error_is_reported_not_empty(monkeypatch) -> None:
    _patch(monkeypatch, _context_client({("act_42", "adspixels"): _permission_error()}))

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42"))

    assert result["datasets"]["available"] is False
    assert result["datasets"]["items"] is None
    assert "Permissions" in result["section_errors"]["datasets"]["message"]
    assert any("datasets" in signal for signal in result["missing_signals"])


def test_advertiser_context_account_permission_error_is_not_swallowed(monkeypatch) -> None:
    client = _context_client()
    client.objects["act_42"] = _permission_error()
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError):
        asyncio.run(performance_signals.get_advertiser_context(account_id="act_42"))


@pytest.mark.parametrize(
    "kwargs",
    [{"account_id": ""}, {"account_id": "act_1", "lookback_days": 6}, {"account_id": "act_1", "lookback_days": 91}],
)
def test_advertiser_context_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_advertiser_context(**kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# get_delivery_errors
# ---------------------------------------------------------------------------


def _spend_handler(spend_by_object: dict[str, float], *, page_size: int | None = None, more_pages: bool = False,
                   error: Exception | None = None):
    """Models the account-level spend read: one lean, spend-sorted insights request per page.

    Like Meta, it fails when the sorted read carries action fields, and only objects with delivery have rows.
    """
    ordered = sorted(spend_by_object.items(), key=lambda item: -item[1])

    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        id_field = f"{params['level']}_id"
        assert object_id == "act_42"
        assert params["sort"] == ["spend_descending"]
        if {"actions", "action_values"} & set(fields):
            raise _unknown_error()
        assert fields == [id_field, "spend"]
        if error is not None:
            raise error
        size = page_size or params["limit"]
        offset = int(params.get("after", "0"))
        rows = [{id_field: key, "spend": str(value)} for key, value in ordered[offset: offset + size]]
        payload: dict[str, Any] = {"data": rows}
        if more_pages or offset + size < len(ordered):
            payload["paging"] = {"cursors": {"after": str(offset + size)}, "next": "https://graph.example/next"}
        return payload

    return handler


ACTIVE_ACCOUNT = {"id": "act_42", "name": "Shop", "account_status": 1, "disable_reason": 0, "currency": "USD"}

PROBLEM_ADS = [
    {
        "id": "ad_medium", "name": "Medium", "effective_status": "ACTIVE", "status": "ACTIVE",
        "issues_info": [{"error_code": 1815, "error_summary": "Low delivery", "error_message": "Audience too narrow",
                         "error_type": "SOFT_ERROR", "level": "AD"}],
    },
    {
        "id": "ad_disapproved", "name": "Disapproved", "effective_status": "DISAPPROVED", "status": "ACTIVE",
        "ad_review_feedback": {"global": {"Personal attributes": "Ads can't assert personal attributes."}},
    },
    {"id": "ad_with_issues", "name": "With issues", "effective_status": "WITH_ISSUES", "status": "ACTIVE"},
    {
        "id": "ad_paused", "name": "Paused", "effective_status": "ADSET_PAUSED", "status": "ACTIVE",
        "failed_delivery_checks": [{"check_name": "no_active_creative", "summary": "No creative", "description": "Add one"}],
    },
    {"id": "ad_clean", "name": "Clean", "effective_status": "ACTIVE", "status": "ACTIVE", "issues_info": []},
]


def test_delivery_errors_scans_account_and_reads_problem_fields(monkeypatch) -> None:
    client = _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": ACTIVE_ACCOUNT},
            edges={("act_42", "ads"): {"data": PROBLEM_ADS}},
            insights_handler=_spend_handler({"ad_medium": 50.0}),
        ),
    )

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="42"))

    account_read = _calls(client, "get_object")[0]
    assert account_read["object_id"] == "act_42"
    assert {"account_status", "disable_reason"} <= set(account_read["fields"])
    scan = _calls(client, "list_objects")[0]
    assert scan["edge"] == "ads"
    assert {"issues_info", "failed_delivery_checks", "ad_review_feedback", "effective_status"} <= set(scan["fields"])
    assert scan["params"]["effective_status"] == ["DISAPPROVED", "PENDING_BILLING_INFO", "WITH_ISSUES"]
    (spend_call,) = _calls(client, "get_insights")
    assert spend_call["object_id"] == "act_42"
    assert spend_call["fields"] == ["ad_id", "spend"]
    assert spend_call["params"]["level"] == "ad"
    assert spend_call["params"]["date_preset"] == "last_7d"
    assert spend_call["params"]["sort"] == ["spend_descending"]
    assert "updated_time" in scan["fields"]

    problems = {problem["object_id"]: problem for problem in result["problems"]}
    assert "ad_clean" not in problems and "act_42" not in problems
    medium = problems["ad_medium"]
    assert medium["severity"] == "medium"
    assert medium["reasons"][0] == {
        "source": "issues_info", "code": 1815, "summary": "Low delivery", "message": "Audience too narrow",
        "error_type": "SOFT_ERROR", "issue_level": "AD",
    }
    assert medium["spend"] == 50.0
    review = problems["ad_disapproved"]["reasons"][0]
    assert review["source"] == "ad_review_feedback" and review["code"] == "Personal attributes"
    assert "ad_paused" not in problems
    assert result["scan"]["complete"] is True
    assert result["scan"]["objects_examined"] == 5
    assert result["scan"]["paused_objects_skipped"] == 1
    assert result["scan"]["paused_included"] is False
    assert result["scan"]["active_included"] is False
    assert any("include_active" in note for note in result["scan"]["not_scanned"])
    assert result["missing_signals"] == [
        "2 of 3 problems had no spend in last_7d; they are listed after the problems with recent spend."
    ]
    assert [problem["object_id"] for problem in result["problems"]] == ["ad_disapproved", "ad_with_issues", "ad_medium"]
    assert problems["ad_disapproved"]["spend_status"] == "no_spend_in_window"


def test_delivery_errors_orders_by_severity_then_spend(monkeypatch) -> None:
    account = {**ACTIVE_ACCOUNT, "account_status": 9}
    client = _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": account},
            edges={("act_42", "ads"): {"data": PROBLEM_ADS}},
            insights_handler=_spend_handler(
                {"ad_with_issues": 100.0, "ad_disapproved": 10.0, "ad_medium": 500.0, "ad_paused": 0.0}
            ),
        ),
    )

    result = asyncio.run(performance_signals.get_delivery_errors(
        account_id="act_42", include_paused=True, include_active=True,
    ))

    assert [(problem["object_id"], problem["severity"]) for problem in result["problems"]] == [
        ("act_42", "high"),
        ("ad_with_issues", "high"),
        ("ad_disapproved", "high"),
        ("ad_medium", "medium"),
        ("ad_paused", "low"),
    ]
    assert result["metrics"]["by_severity"] == {"high": 3, "medium": 1, "low": 1}
    assert result["ordering"] == ["severity", "account_first", "recent_spend_desc", "updated_time_desc", "object_id"]
    sent = _calls(client, "list_objects")[0]["params"]["effective_status"]
    assert {"DISAPPROVED", "WITH_ISSUES", "ACTIVE", "PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED"} <= set(sent)
    assert result["scan"]["not_scanned"] == []


def test_delivery_errors_disabled_account_is_critical_with_reason_label(monkeypatch) -> None:
    account = {**ACTIVE_ACCOUNT, "account_status": 2, "disable_reason": 3}
    _patch(
        monkeypatch,
        RecordingClient(objects={"act_42": account}, insights_handler=_spend_handler({})),
    )

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42", level="account"))

    problem = result["problems"][0]
    assert problem["severity"] == "critical"
    codes = {(reason["source"], reason["code"]) for reason in problem["reasons"]}
    assert ("account_status", "DISABLED") in codes
    assert ("disable_reason", "RISK_PAYMENT") in codes
    assert result["scan"]["mode"] == "account_only"


def test_delivery_errors_reports_incomplete_scan(monkeypatch) -> None:
    def endless(params: dict[str, Any]) -> dict[str, Any]:
        page = int(params.get("after", "0"))
        return {
            "data": [{"id": f"ad_{page}_{index}", "effective_status": "ACTIVE"} for index in range(2)],
            "paging": {"cursors": {"after": str(page + 1)}, "next": "https://graph.example/next"},
        }

    client = _patch(
        monkeypatch,
        RecordingClient(objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", "ads"): endless},
                        insights_handler=_spend_handler({})),
    )

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42", limit=4))

    scans = _calls(client, "list_objects")
    assert len(scans) == 2
    assert result["scan"]["complete"] is False
    assert result["scan"]["objects_examined"] == 4
    assert result["scan"]["next_after"] == "2"
    assert result["scan"]["stop_reason"] == "limit_reached"
    assert any("incomplete" in signal and "4" in signal for signal in result["missing_signals"])


def test_delivery_errors_empty_cursor_cycle_stops_as_incomplete(monkeypatch) -> None:
    def cycle(params: dict[str, Any]) -> dict[str, Any]:
        following = "B" if params.get("after") == "A" else "A"
        return {"data": [], "paging": {"cursors": {"after": following}, "next": "https://graph.example/next"}}

    client = _patch(monkeypatch, RecordingClient(objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", "ads"): cycle}))

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42", limit=1))

    assert len(_calls(client, "list_objects")) == 3
    assert result["scan"]["complete"] is False
    assert result["scan"]["stop_reason"] == "pagination_cursor_missing_or_repeated"
    assert result["scan"]["next_after"] is None
    assert any("incomplete" in signal for signal in result["missing_signals"])


def test_delivery_errors_endless_empty_pages_stop_at_the_page_cap(monkeypatch) -> None:
    def endless(params: dict[str, Any]) -> dict[str, Any]:
        page = int(params.get("after", "0"))
        return {"data": [], "paging": {"cursors": {"after": str(page + 1)}, "next": "https://graph.example/next"}}

    client = _patch(monkeypatch, RecordingClient(objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", "ads"): endless}))

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42", limit=5))

    pages = len(_calls(client, "list_objects"))
    assert pages == performance_signals.MAX_DELIVERY_PAGES
    assert result["scan"]["complete"] is False
    assert result["scan"]["stop_reason"] == "page_cap_reached"
    assert result["scan"]["next_after"] == str(pages)
    assert any("pass after=" in signal for signal in result["missing_signals"])


def test_delivery_errors_named_objects_read_each_object(monkeypatch) -> None:
    client = _patch(
        monkeypatch,
        RecordingClient(
            objects={
                "act_42": ACTIVE_ACCOUNT,
                "as_1": {"id": "as_1", "name": "Set", "effective_status": "WITH_ISSUES",
                         "issues_info": [{"error_code": 2, "error_summary": "Budget too low"}]},
                "as_2": {"id": "as_2", "name": "Fine", "effective_status": "ACTIVE"},
                "as_3": {"id": "as_3", "name": "Paused", "effective_status": "PAUSED",
                         "issues_info": [{"error_code": 3, "error_summary": "Old warning"}]},
            },
            insights_handler=_spend_handler({"as_1": 12.5}),
        ),
    )

    result = asyncio.run(
        performance_signals.get_delivery_errors(account_id="act_42", level="adset", object_ids="as_1, as_2, as_3")
    )

    assert _calls(client, "list_objects") == []
    reads = {call["object_id"]: call for call in _calls(client, "get_object")}
    assert set(reads) == {"act_42", "as_1", "as_2", "as_3"}
    assert "issues_info" in reads["as_1"]["fields"]
    assert "ad_review_feedback" not in reads["as_1"]["fields"]
    assert [problem["object_id"] for problem in result["problems"]] == ["as_1"]
    assert result["problems"][0]["spend"] == 12.5
    assert result["scan"] == {
        "mode": "named_objects", "level": "adset", "objects_examined": 3, "complete": True,
        "next_after": None, "stop_reason": None, "pages_fetched": 0, "limit": 500, "effective_status_filter": None,
        "paused_included": False, "active_included": True, "paused_objects_skipped": 1, "not_scanned": [],
    }


def _old_problem_ad(index: int, *, updated_day: int) -> dict[str, Any]:
    return {
        "id": f"ad_{index:04d}", "name": f"Ad {index}", "effective_status": "DISAPPROVED", "status": "ACTIVE",
        "updated_time": f"2026-{1 + updated_day // 28:02d}-{1 + updated_day % 28:02d}T10:00:00+0000",
        "ad_review_feedback": {"global": {"Policy": "Rejected."}},
    }


def test_delivery_errors_ranks_many_problems_by_spend_from_one_sorted_read(monkeypatch) -> None:
    ads = [_old_problem_ad(index, updated_day=index % 250) for index in range(274)]
    spend = {f"other_{index}": 1000.0 - index for index in range(150)}
    spend.update({"ad_0200": 5.0, "ad_0007": 950.5, "ad_0100": 80.0})
    client = _patch(monkeypatch, RecordingClient(
        objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", "ads"): {"data": ads}},
        insights_handler=_spend_handler(spend),
    ))

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42"))

    calls = _calls(client, "get_insights")
    assert len(calls) == 1
    assert [problem["object_id"] for problem in result["problems"][:3]] == ["ad_0007", "ad_0100", "ad_0200"]
    assert [problem["spend"] for problem in result["problems"][:3]] == [950.5, 80.0, 5.0]
    rest = result["problems"][3:]
    assert all(problem["spend_status"] == "no_spend_in_window" for problem in rest)
    stamps = [problem["updated_time"] for problem in rest]
    assert stamps == sorted(stamps, reverse=True)
    assert result["metrics"]["problems_without_recent_spend"] == 271
    assert any("271 of 274" in signal and "no spend" in signal for signal in result["missing_signals"])
    assert result["spend_lookup"]["status"] == "read"
    assert result["spend_lookup"]["requests"] == 1
    assert result["spend_lookup"]["all_spending_objects_read"] is True
    assert "sorted by spend" in result["spend_lookup"]["method"]
    assert result["ordering"][2:4] == ["recent_spend_desc", "updated_time_desc"]
    assert len(json.dumps(result).encode()) < 20_000


def test_delivery_errors_reads_a_second_spend_page_at_most(monkeypatch) -> None:
    ads = [_old_problem_ad(index, updated_day=index) for index in range(3)]
    spend = {f"other_{index}": 10_000.0 - index for index in range(450)}
    spend.update({"ad_0001": 9_700.0, "ad_0002": 1.0})
    client = _patch(monkeypatch, RecordingClient(
        objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", "ads"): {"data": ads}},
        insights_handler=_spend_handler(spend),
    ))

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42"))

    calls = _calls(client, "get_insights")
    assert len(calls) == 2
    assert calls[1]["params"]["after"] == str(performance_signals.DELIVERY_SPEND_PAGE)
    problems = {problem["object_id"]: problem for problem in result["problems"]}
    assert problems["ad_0001"]["spend"] == 9_700.0
    assert problems["ad_0002"]["spend"] is None
    assert problems["ad_0002"]["spend_status"] == "not_in_top_spenders"
    assert [problem["object_id"] for problem in result["problems"]] == ["ad_0001", "ad_0002", "ad_0000"]
    assert result["spend_lookup"]["all_spending_objects_read"] is False
    assert any("not among the" in signal for signal in result["missing_signals"])


def test_delivery_errors_spend_read_failure_falls_back_to_most_recently_updated(monkeypatch) -> None:
    ads = [_old_problem_ad(index, updated_day=day) for index, day in enumerate([3, 40, 12])]
    _patch(monkeypatch, RecordingClient(
        objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", "ads"): {"data": ads}},
        insights_handler=_spend_handler({}, error=_unknown_error()),
    ))

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42"))

    assert [problem["object_id"] for problem in result["problems"]] == ["ad_0001", "ad_0002", "ad_0000"]
    assert all(problem["spend"] is None and problem["spend_status"] == "spend_unavailable"
               for problem in result["problems"])
    assert result["ordering"] == ["severity", "account_first", "updated_time_desc", "object_id"]
    assert result["spend_lookup"]["status"] == "failed"
    assert any("most recently updated" in signal and "unknown error" in signal for signal in result["missing_signals"])


def _problem_ad(index: int) -> dict[str, Any]:
    long_text = "This ad was rejected because it does not follow our Advertising Standards on several points. " * 6
    return {
        "id": f"ad_{index:04d}", "name": f"Problem ad {index} with a reasonably long descriptive name",
        "effective_status": "DISAPPROVED" if index % 2 else "WITH_ISSUES", "status": "ACTIVE",
        "campaign_id": f"cmp_{index % 7}", "adset_id": f"as_{index % 13}",
        "issues_info": [{"error_code": 1487000 + index, "error_summary": "Ad not delivering " * 3,
                         "error_message": long_text, "error_type": "HARD_ERROR", "level": "AD"}],
        "ad_review_feedback": {"global": {"Misleading claims": long_text, "Personal attributes": long_text}},
        "failed_delivery_checks": [{"check_name": "creative_check", "summary": "Check failed",
                                    "description": long_text, "extra_info": None}],
    }


def test_delivery_errors_returns_each_problem_once_and_bounds_inline(monkeypatch) -> None:
    ads = [_problem_ad(index) for index in range(200)]
    _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": ACTIVE_ACCOUNT},
            edges={("act_42", "ads"): {"data": ads}},
            insights_handler=_spend_handler({"ad_0003": 99.0}),
        ),
    )

    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42"))

    assert result["metrics"]["problem_count"] == 200
    assert result["metrics"]["problems_returned"] == performance_signals.DEFAULT_MAX_PROBLEMS
    assert result["metrics"]["problems_truncated"] is True
    assert len(result["problems"]) == performance_signals.DEFAULT_MAX_PROBLEMS
    assert result["problems"][0]["object_id"] == "ad_0003"
    serialized_findings = json.dumps(result["findings"])
    assert all(problem["object_id"] not in serialized_findings for problem in result["problems"][1:])
    assert len(result["findings"]) == 1
    assert "200" in result["findings"][0]["summary"]
    assert any("max_problems" in signal for signal in result["missing_signals"])
    first = result["problems"][0]
    assert "status" not in first
    assert all(len(reason.get("message") or "") <= performance_signals.DELIVERY_TEXT_LIMIT + 3
               for reason in first["reasons"])
    assert len(first["reasons"]) <= performance_signals.MAX_REASONS_PER_PROBLEM
    assert first["reasons_omitted"] == 4 - performance_signals.MAX_REASONS_PER_PROBLEM
    assert all(value is not None for reason in first["reasons"] for value in reason.values())
    assert len(json.dumps(result).encode()) < 20_000


def test_delivery_errors_nested_review_feedback_is_cut_to_short_text(monkeypatch) -> None:
    placement = {"facebook_feed": {"Misleading claims": "x" * 500}, "instagram_stream": {"Other": "y" * 500}}
    _patch(
        monkeypatch,
        RecordingClient(
            objects={
                "act_42": ACTIVE_ACCOUNT,
                "ad_1": {"id": "ad_1", "effective_status": "ACTIVE",
                         "ad_review_feedback": {"placement_specific": placement}},
            },
            insights_handler=_spend_handler({}),
        ),
    )
    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42", object_ids=["ad_1"]))
    message = result["problems"][0]["reasons"][0]["message"]
    assert isinstance(message, str)
    assert message.startswith('{"facebook_feed"')
    assert len(message) == performance_signals.DELIVERY_TEXT_LIMIT + 3


def test_delivery_errors_healthy_account_scan_completes_in_one_page(monkeypatch) -> None:
    client = _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": ACTIVE_ACCOUNT},
            edges={("act_42", "ads"): {"data": []}},
            insights_handler=_spend_handler({}),
        ),
    )
    result = asyncio.run(performance_signals.get_delivery_errors(account_id="act_42"))
    assert len(_calls(client, "list_objects")) == 1
    assert _calls(client, "get_insights") == []
    assert result["scan"]["complete"] is True
    assert result["metrics"]["problem_count"] == 0
    assert result["problems"] == []
    assert result["findings"][0]["type"] == "no_delivery_problems_found"


@pytest.mark.parametrize(
    ("level", "edge", "expected"),
    [("campaign", "campaigns", ["WITH_ISSUES"]), ("adset", "adsets", ["WITH_ISSUES"])],
)
def test_delivery_errors_problem_state_filter_per_level(monkeypatch, level, edge, expected) -> None:
    client = _patch(
        monkeypatch,
        RecordingClient(objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", edge): {"data": []}},
                        insights_handler=_spend_handler({})),
    )
    asyncio.run(performance_signals.get_delivery_errors(account_id="act_42", level=level))
    assert _calls(client, "list_objects")[0]["params"]["effective_status"] == expected


def test_delivery_errors_permission_error_is_not_swallowed(monkeypatch) -> None:
    _patch(
        monkeypatch,
        RecordingClient(objects={"act_42": ACTIVE_ACCOUNT}, edges={("act_42", "ads"): _permission_error()}),
    )
    with pytest.raises(MetaApiError, match="Permissions"):
        asyncio.run(performance_signals.get_delivery_errors(account_id="act_42"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": ""},
        {"account_id": "act_1", "level": "creative"},
        {"account_id": "act_1", "level": "account", "object_ids": ["1"]},
        {"account_id": "act_1", "level": "ad", "object_ids": [str(index) for index in range(51)]},
        {"account_id": "act_1", "level": "ad", "object_ids": ["1"], "after": "cursor"},
        {"account_id": "act_1", "limit": 0},
        {"account_id": "act_1", "limit": 2001},
        {"account_id": "act_1", "spend_date_preset": "last_forever"},
        {"account_id": "act_1", "max_problems": 0},
        {"account_id": "act_1", "max_problems": 201},
    ],
)
def test_delivery_errors_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_delivery_errors(**kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# Meta's own result (phase two, item 3)
# ---------------------------------------------------------------------------


def test_trend_cost_per_result_follows_metas_result_definition(monkeypatch) -> None:
    """A traffic campaign has no purchases or leads; its result is Meta's link_click result."""
    rows = [
        _day_row("2026-11-01", spend=100, impressions=10000, clicks=200, result=("actions:link_click", 50)),
        _day_row("2026-11-02", spend=120, impressions=10000, clicks=240, result=("actions:link_click", 40)),
    ]
    for row in rows:
        row["actions"] = [{"action_type": "link_click", "value": "0"}, {"action_type": "purchase", "value": "3"}]
    client = _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", since="2026-11-01", until="2026-11-02",
    ))

    call = _calls(client, "get_insights")[0]
    assert {"results", "cost_per_result"} <= set(call["fields"])
    first = result["periods"][0]["metrics"]
    assert first["cost_per_result"] == pytest.approx(2.0)  # 100 / 50 link clicks, not 100 / 3 purchases
    assert first["conversion_rate"] == pytest.approx(0.25)
    assert first["roas"] is None
    assert result["periods"][1]["inputs"]["results"] == 40.0
    assert result["result"] == {
        "source": "meta_results_field", "status": "single", "indicator": "actions:link_click",
        "action_type": "link_click", "additive": True, "conversion_value_source": None,
    }
    assert "Meta's own result" in result["results_definition"]
    assert any("no value for the result's action (link_click)" in signal for signal in result["missing_signals"])


def test_trend_without_a_meta_result_does_not_fall_back_to_purchases(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-01", spend=100, impressions=1000, clicks=10, purchases=5, purchase_value=500,
                 meta_results=False),
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="adset", object_id="as_1", since="2026-11-01", until="2026-11-01",
    ))
    metrics = result["periods"][0]["metrics"]
    assert metrics["cost_per_result"] is None and metrics["roas"] is None and metrics["conversion_rate"] is None
    assert result["result"]["status"] == "not_reported"
    assert any("reported no result" in signal and "No other action is substituted" in signal
               for signal in result["missing_signals"])


def test_trend_day_without_result_counts_zero_once_meta_reports_one(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-02", spend=10, impressions=100, clicks=5, result=("actions:lead", 2)),
        _day_row("2026-11-03", spend=10, impressions=100, clicks=5),
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="adset", object_id="as_1", since="2026-11-02", until="2026-11-03", granularity="week",
    ))
    period = result["periods"][0]
    assert period["inputs"]["results"] == 2.0
    assert period["metrics"]["cost_per_result"] == pytest.approx(10.0)


def test_trend_mixed_results_are_not_added_together(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-01", spend=10, impressions=100, clicks=5, result=("actions:lead", 2)),
        _day_row("2026-11-02", spend=10, impressions=100, clicks=5, result=("actions:purchase", 1)),
    ]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", since="2026-11-01", until="2026-11-02",
    ))
    assert all(period["metrics"]["cost_per_result"] is None for period in result["periods"])
    assert result["result"]["status"] == "mixed"
    assert result["result"]["indicators"] == ["actions:lead", "actions:purchase"]
    assert any("more than one result type" in signal for signal in result["missing_signals"])


def test_trend_reach_result_is_daily_only_with_metas_cost(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-03", spend=30, impressions=5000, clicks=5, result=("reach", 4000)),
        _day_row("2026-11-04", spend=30, impressions=5000, clicks=5, result=("reach", 3900)),
    ]
    for row in rows:
        row["cost_per_result"] = _meta_result("reach", 7.5)
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))

    daily = asyncio.run(performance_signals.get_performance_trend(
        level="adset", object_id="as_1", since="2026-11-03", until="2026-11-04",
    ))
    assert daily["periods"][0]["metrics"]["cost_per_result"] == 7.5
    assert daily["periods"][0]["inputs"]["results"] == 4000
    assert daily["periods"][0]["metrics"]["conversion_rate"] is None
    weekly = asyncio.run(performance_signals.get_performance_trend(
        level="adset", object_id="as_1", since="2026-11-03", until="2026-11-04", granularity="week",
    ))
    assert weekly["periods"][0]["inputs"]["results"] is None
    assert weekly["periods"][0]["metrics"]["cost_per_result"] is None
    assert any("not an action count" in signal for signal in weekly["missing_signals"])


def test_account_level_trend_does_not_request_cost_per_result(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler([])))
    asyncio.run(performance_signals.get_performance_trend(
        level="account", object_id="42", since="2026-11-01", until="2026-11-02",
    ))
    fields = _calls(client, "get_insights")[0]["fields"]
    assert "results" in fields and "cost_per_result" not in fields


def test_trend_result_fields_rejected_reads_again_without_them(monkeypatch) -> None:
    rows = [_day_row("2026-11-01", spend=10, impressions=100, clicks=5, purchases=1)]

    def handler(object_id, fields, params):
        if "results" in fields:
            raise MetaApiError(message="(#100) results is not valid", status_code=400, code=100)
        return {"data": rows}

    client = _patch(monkeypatch, RecordingClient(insights_handler=handler))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", since="2026-11-01", until="2026-11-01",
    ))
    assert len(_calls(client, "get_insights")) == 2
    assert result["periods"][0]["metrics"]["cpc"] == pytest.approx(2.0)
    assert result["periods"][0]["metrics"]["cost_per_result"] is None
    assert result["result"]["status"] == "request_failed"
    assert any("rejected the request for its result fields" in signal for signal in result["missing_signals"])


def test_anomalies_cost_per_result_follows_metas_result(monkeypatch) -> None:
    days = _days(date(2026, 11, 15), 15)
    rows = [
        _day_row(day, spend=100, impressions=10000, clicks=100, result=("actions:link_click", 50 + index % 3))
        for index, day in enumerate(days[:-1])
    ]
    rows.append(_day_row(days[-1], spend=100, impressions=10000, clicks=100, result=("actions:link_click", 5)))
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(performance_signals.detect_performance_anomalies(
        level="adset", object_id="as_1", metrics=["cost_per_result"], lookback_days=1, baseline_days=14,
        end_date="2026-11-15",
    ))
    assert [anomaly["metric"] for anomaly in result["anomalies"]] == ["cost_per_result"]
    assert result["anomalies"][0]["value"] == pytest.approx(20.0)
    assert result["result"]["indicator"] == "actions:link_click"


# ---------------------------------------------------------------------------
# Several entities per call (phase two, item 14)
# ---------------------------------------------------------------------------


def _per_object_handler(rows_by_object: dict[str, list[dict[str, Any]]], failing: dict[str, Exception] | None = None):
    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if failing and object_id in failing:
            raise failing[object_id]
        window = json.loads(params["time_range"])
        rows = rows_by_object.get(object_id, [])
        return {"data": [row for row in rows if window["since"] <= row["date_start"] <= window["until"]]}

    return handler


def test_multi_entity_report_names_an_unreadable_entity_and_returns_the_rest(monkeypatch) -> None:
    rows = {
        "111": [_day_row("2026-11-01", spend=10, impressions=100, clicks=5, purchases=1)],
        "333": [_day_row("2026-11-01", spend=30, impressions=300, clicks=15, purchases=3)],
    }
    client = _patch(monkeypatch, RecordingClient(insights_handler=_per_object_handler(
        rows, failing={"222": _permission_error()},
    )))

    trend = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_ids=["111", "222", "333"], since="2026-11-01", until="2026-11-01",
    ))

    assert sorted(call["object_id"] for call in _calls(client, "get_insights")) == ["111", "222", "333"]
    entities = {entity["object_id"]: entity for entity in trend["entities"]}
    assert entities["111"]["status"] == "read" and entities["333"]["status"] == "read"
    assert entities["111"]["totals"]["metrics"]["cost_per_result"] == pytest.approx(10.0)
    assert entities["333"]["changes"]["cpc"]["first"] == pytest.approx(2.0)
    assert entities["222"]["status"] == "unreadable" and "Permissions" in entities["222"]["error"]
    assert trend["coverage"]["entities_unreadable"][0]["object_id"] == "222"
    assert trend["coverage"]["complete"] is False
    assert any(signal.startswith("222 could not be read") for signal in trend["missing_signals"])
    assert trend["scope"] == {"level": "campaign", "object_ids": ["111", "222", "333"]}

    anomalies = asyncio.run(performance_signals.detect_performance_anomalies(
        level="campaign", object_ids="111,222", lookback_days=1, baseline_days=7, end_date="2026-11-08",
    ))
    by_id = {entity["object_id"]: entity for entity in anomalies["entities"]}
    assert by_id["222"]["status"] == "unreadable"
    assert by_id["111"]["status"] == "read" and by_id["111"]["judged"] is False


def test_trend_child_level_selects_top_spenders_then_reads_them_in_one_filtered_request(monkeypatch) -> None:
    daily = {
        "501": [_day_row("2026-11-01", spend=50, impressions=500, clicks=10, result=("actions:lead", 5))],
        "502": [_day_row("2026-11-01", spend=20, impressions=200, clicks=4, result=("actions:link_click", 4))],
    }

    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        assert object_id == "act_42"
        if "sort" in params:
            assert params["sort"] == ["spend_descending"] and params["limit"] == 2
            assert set(fields) == {"campaign_id", "campaign_name", "spend"}
            return {
                "data": [{"campaign_id": "501", "campaign_name": "A", "spend": "50"},
                         {"campaign_id": "502", "campaign_name": "B", "spend": "20"}],
                "paging": {"cursors": {"after": "c"}, "next": "https://graph.example/next"},
            }
        (selector,) = params["filtering"]
        assert selector == {"field": "campaign.id", "operator": "IN", "value": ["501", "502"]}
        assert params["level"] == "campaign" and params["time_increment"] == 1
        return {"data": [{**row, "campaign_id": key} for key, rows in daily.items() for row in rows]}

    client = _patch(monkeypatch, RecordingClient(insights_handler=handler))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="account", object_id="42", child_level="campaign", max_entities=2,
        since="2026-11-01", until="2026-11-01",
    ))

    assert len(_calls(client, "get_insights")) == 2
    entities = {entity["object_id"]: entity for entity in result["entities"]}
    assert entities["501"]["name"] == "A"
    assert entities["501"]["result"]["indicator"] == "actions:lead"
    assert entities["501"]["totals"]["metrics"]["cost_per_result"] == pytest.approx(10.0)
    assert entities["502"]["result"]["indicator"] == "actions:link_click"
    assert result["coverage"]["data_read"] == "one_filtered_request"
    assert result["coverage"]["selection"]["more_children_exist"] is True
    assert result["coverage"]["complete"] is False
    assert any("highest-spending campaign" in signal for signal in result["missing_signals"])
    assert "periods" not in result


def test_child_level_falls_back_to_one_request_per_child_when_the_filtered_read_fails(monkeypatch) -> None:
    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if "sort" in params:
            return {"data": [{"adset_id": "601", "spend": "9"}, {"adset_id": "602", "spend": "8"}]}
        if "filtering" in params:
            raise _unknown_error()
        if object_id == "602":
            raise _permission_error()
        return {"data": [_day_row("2026-11-01", spend=9, impressions=90, clicks=3, purchases=1)]}

    _patch(monkeypatch, RecordingClient(insights_handler=handler))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", child_level="adset", since="2026-11-01", until="2026-11-01",
    ))
    assert result["coverage"]["data_read"] == "one_request_per_entity"
    assert "unknown error" in result["coverage"]["filtered_read_error"]
    statuses = {entity["object_id"]: entity["status"] for entity in result["entities"]}
    assert statuses == {"601": "read", "602": "unreadable"}
    assert any("602 could not be read" in signal for signal in result["missing_signals"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"level": "campaign", "object_id": "1", "object_ids": ["2"]},
        {"level": "campaign", "object_ids": ["cmp_1"]},
        {"level": "account", "object_ids": ["1"]},
        {"level": "campaign", "object_ids": [str(index) for index in range(11)]},
        {"level": "campaign", "object_ids": ["1"], "child_level": "ad"},
        {"level": "ad", "object_id": "1", "child_level": "campaign"},
        {"level": "campaign", "object_id": "1", "child_level": "adset", "max_entities": 11},
        {"level": "campaign"},
    ],
)
def test_multi_entity_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    for tool in (performance_signals.get_performance_trend, performance_signals.detect_performance_anomalies):
        client = _patch(monkeypatch, RecordingClient())
        with pytest.raises(ValidationError):
            asyncio.run(tool(**kwargs))
        assert client.calls == []


def test_multi_entity_trend_response_stays_small(monkeypatch) -> None:
    rows = {
        str(index): [
            _day_row(day, spend=10 + index, impressions=1000, clicks=10, purchases=1)
            for day in _days(date(2026, 11, 30), 30)
        ]
        for index in range(1, 11)
    }
    _patch(monkeypatch, RecordingClient(insights_handler=_per_object_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="ad", object_ids=list(rows), since="2026-11-01", until="2026-11-30",
    ))
    assert result["coverage"]["entities_read"] == 10
    assert len(json.dumps(result).encode()) < 20_000


def test_anomalies_multi_entity_response_stays_small(monkeypatch) -> None:
    days = _days(date(2026, 11, 30), 35)
    rows = {
        str(index): [
            _day_row(day, spend=100 + (offset % 3) + (900 if offset == 34 else 0), impressions=1000, clicks=10,
                     purchases=1 + offset % 2)
            for offset, day in enumerate(days)
        ]
        for index in range(1, 11)
    }
    _patch(monkeypatch, RecordingClient(insights_handler=_per_object_handler(rows)))
    result = asyncio.run(performance_signals.detect_performance_anomalies(
        level="ad", object_ids=list(rows), end_date="2026-11-30",
    ))
    assert result["coverage"]["entities_read"] == 10
    assert result["metrics"]["anomaly_count"] >= 10
    assert len(result["findings"]) <= performance_signals.MULTI_ENTITY_MAX_FINDINGS
    assert {finding["affected_entities"][0]["object_id"] for finding in result["findings"]} <= set(rows)
    assert len(json.dumps(result).encode()) < 20_000


# ---------------------------------------------------------------------------
# Ranking report: Meta's result and several scopes
# ---------------------------------------------------------------------------


def test_ranking_compares_cost_only_within_the_same_meta_result(monkeypatch) -> None:
    rows = [
        _ad_row("leads", spend=400, purchases=0, quality="AVERAGE", engagement="AVERAGE", conversion="AVERAGE"),
        _ad_row("leads_cheap", spend=100, purchases=0, quality="AVERAGE", engagement="AVERAGE", conversion="AVERAGE"),
        _ad_row("clicks", spend=500, purchases=0, quality="AVERAGE", engagement="AVERAGE", conversion="AVERAGE"),
    ]
    rows[0]["results"] = _meta_result("actions:lead", 4)  # 100 per lead
    rows[1]["results"] = _meta_result("actions:lead", 5)  # 20 per lead
    rows[2]["results"] = _meta_result("actions:link_click", 1000)  # 0.5 per click
    _patch(monkeypatch, RecordingClient(insights_handler=_meta_like_ranking_handler(
        rows, scope_spend=1000.0, more=False,
    )))

    result = asyncio.run(performance_signals.get_auction_ranking_report(level="campaign", object_id="cmp_1"))

    ads = {ad["ad_id"]: ad for ad in result["ranked_ads"]}
    assert ads["leads"]["result_indicator"] == "actions:lead"
    assert ads["leads"]["cost_per_result_baseline"] == {"value": pytest.approx(500 / 9),
                                                        "basis": "examined_ads_with_same_result"}
    assert ads["clicks"]["cost_per_result_baseline"]["value"] == pytest.approx(0.5)
    assert ads["leads"]["diagnosis"] == "bid_and_budget"
    assert ads["clicks"]["diagnosis"] is None
    assert result["rules"]["cost_baseline"]
    assert any("different Meta results" in signal for signal in result["missing_signals"])


def test_ranking_cost_read_requests_metas_result_fields(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(insights_handler=_meta_like_ranking_handler(
        _many_ads(3), scope_spend=20000.0, more=False,
    )))
    asyncio.run(performance_signals.get_auction_ranking_report(level="campaign", object_id="cmp_1"))
    cost = next(call for call in _ad_level_calls(client) if "filtering" in call["params"])
    assert {"results", "cost_per_result"} <= set(cost["fields"])
    scope = next(call for call in _calls(client, "get_insights") if call["params"]["level"] == "campaign")
    assert "results" in scope["fields"]


def test_ranking_object_ids_pool_scopes_and_name_an_unreadable_one(monkeypatch) -> None:
    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if object_id == "902":
            raise _permission_error()
        inner = _meta_like_ranking_handler(
            [_ad_row(f"ad_{object_id}", spend=100, purchases=2, quality="BELOW_AVERAGE_10", engagement="AVERAGE",
                     conversion="AVERAGE")],
            scope_spend=100.0, more=False,
        )
        return inner(object_id, fields, params)

    client = _patch(monkeypatch, RecordingClient(insights_handler=handler))
    result = asyncio.run(performance_signals.get_auction_ranking_report(
        level="campaign", object_ids=["901", "902", "903"], max_ads=90,
    ))

    sorted_calls = [call for call in _ad_level_calls(client) if "sort" in call["params"]]
    assert {call["params"]["limit"] for call in sorted_calls} == {30}
    assert {ad["ad_id"] for ad in result["weak_ads"]} == {"ad_901", "ad_903"}
    assert {ad["scope_object_id"] for ad in result["ranked_ads"]} == {"901", "903"}
    statuses = {entity["object_id"]: entity["status"] for entity in result["entities"]}
    assert statuses == {"901": "read", "902": "unreadable", "903": "read"}
    assert result["coverage"]["entities_unreadable"] == ["902"]
    assert any(signal.startswith("902 could not be read") for signal in result["missing_signals"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"level": "campaign", "object_id": "1", "object_ids": ["2"]},
        {"level": "account", "object_ids": ["1"]},
        {"level": "campaign", "object_ids": ["1", "2", "3", "4", "5", "6"]},
        {"level": "adset", "object_ids": ["as_1"]},
    ],
)
def test_ranking_object_ids_validation_errors_make_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_auction_ranking_report(**kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# Advertiser context: Meta's result per ad set, missing spend, and object_ids
# ---------------------------------------------------------------------------


def test_advertiser_context_judges_offsite_conversion_goals_on_metas_result(monkeypatch) -> None:
    rows = [dict(row) for row in CONTEXT_ROWS]
    rows[0]["results"] = _meta_result("actions:offsite_conversion.fb_pixel_purchase", 300)
    rows[1]["results"] = _meta_result("actions:offsite_conversion.fb_pixel_purchase", 30)
    rows[2]["results"] = _meta_result("actions:link_click", 1000)
    client = _context_client()
    client.insights_handler = lambda object_id, fields, params: {"data": rows}
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", lookback_days=28))

    full_read = next(call for call in _calls(client, "get_insights") if "filtering" in call["params"])
    assert "results" in full_read["fields"]
    volume = {row["optimization_goal"]: row for row in result["optimization_goal_volume"]}
    conversions = volume["OFFSITE_CONVERSIONS"]
    assert conversions["verdict"] == "some_adsets_meet_threshold"
    assert conversions["adsets_meeting_threshold"] == 1 and conversions["adsets_below_threshold"] == 1
    assert conversions["result_indicators"] == ["actions:offsite_conversion.fb_pixel_purchase"]
    assert "Meta's own result" in conversions["event_basis"]
    assert not any("OFFSITE_CONVERSIONS" in signal for signal in result["missing_signals"])
    by_goal = {row["optimization_goal"]: row for row in result["spend_by_optimization_goal"]}
    assert by_goal["OFFSITE_CONVERSIONS"]["results"] == 330.0
    assert by_goal["OFFSITE_CONVERSIONS"]["cost_per_result"] == pytest.approx(800 / 330)
    by_objective = {row["objective"]: row for row in result["spend_by_objective"]}
    assert by_objective["OUTCOME_TRAFFIC"]["result_indicator"] == "actions:link_click"


def test_advertiser_context_reach_result_is_not_judged_as_events() -> None:
    row = _goal_row("as_r", "REACH", {})
    row["meta_result"] = {"status": "read", "indicator": "reach", "value": 90000.0}
    entry = performance_signals._goal_volume([row], 7)[0]
    assert entry["verdict"] == "not_assessed"
    assert "not an event count" in entry["reason"]


def test_advertiser_context_missing_spend_is_unknown_not_zero(monkeypatch) -> None:
    rows = [dict(row) for row in CONTEXT_ROWS]
    del rows[1]["spend"]
    client = _context_client()
    client.insights_handler = lambda object_id, fields, params: {"data": rows}
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", lookback_days=28))

    assert result["metrics"]["spend"] is None
    by_objective = {row["objective"]: row for row in result["spend_by_objective"]}
    assert by_objective["OUTCOME_SALES"]["spend"] is None
    assert by_objective["OUTCOME_SALES"]["spend_share"] is None
    assert by_objective["OUTCOME_TRAFFIC"]["spend"] == 200.0
    assert by_objective["OUTCOME_TRAFFIC"]["spend_share"] is None
    volume = {row["optimization_goal"]: row for row in result["optimization_goal_volume"]}
    assert volume["OFFSITE_CONVERSIONS"]["adset_count"] == 2
    assert volume["OFFSITE_CONVERSIONS"]["spend"] is None
    assert volume["OFFSITE_CONVERSIONS"]["adsets_with_unknown_spend"] == 1
    assert any("had no spend" in signal for signal in result["missing_signals"])


def test_advertiser_context_object_ids_keep_only_named_campaigns_and_adsets(monkeypatch) -> None:
    rows = [dict(row) for row in CONTEXT_ROWS]
    for index, row in enumerate(rows):
        row["campaign_id"], row["adset_id"] = f"10{index}", f"20{index}"
    client = _context_client()
    client.insights_handler = lambda object_id, fields, params: {"data": rows}
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(
        account_id="act_42", object_ids=["100", "202", "999"],
    ))

    # Two reads filtered to the named ids (campaign.id IN, adset.id IN) and one for the account's total spend.
    assert len(_calls(client, "get_insights")) == 3
    assert result["scope"]["object_ids"] == ["100", "202", "999"]
    assert result["metrics"]["adset_rows"] == 2
    assert result["metrics"]["spend"] == 800.0
    assert result["object_scope"]["unmatched_object_ids"] == ["999"]
    assert any("999" in signal for signal in result["missing_signals"])
    assert result["datasets"]["items"][0]["id"] == "px_1"


@pytest.mark.parametrize("object_ids", [["cmp_1"], [str(index) for index in range(51)]])
def test_advertiser_context_object_ids_validation_makes_no_calls(monkeypatch, object_ids) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_advertiser_context(account_id="act_1", object_ids=object_ids))
    assert client.calls == []


def test_child_level_with_no_delivering_children_says_so(monkeypatch) -> None:
    client = _patch(monkeypatch, RecordingClient(insights_handler=lambda object_id, fields, params: {"data": []}))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", child_level="ad", since="2026-11-01", until="2026-11-02",
    ))
    assert len(_calls(client, "get_insights")) == 1
    assert result["entities"] == []
    assert result["coverage"]["complete"] is True
    assert any("no ad with insights" in signal for signal in result["missing_signals"])


# ---------------------------------------------------------------------------
# Advertiser context on a large account (live fix: the ad set read must never fail on size)
# ---------------------------------------------------------------------------


def _large_account_rows(count: int) -> list[dict[str, Any]]:
    return [
        _adset_row(str(100000 + index), objective="OUTCOME_SALES", goal="OFFSITE_CONVERSIONS",
                   spend=float(5000 - index), actions={"link_click": 100, "purchase": 10})
        for index in range(count)
    ]


def _large_account_handler(rows: list[dict[str, Any]], *, filtered_error: Exception | None = None,
                           sorted_error: Exception | None = None):
    """Models an account with thousands of ad sets.

    The sorted read fails when it carries action fields (as live); the unsorted full read never ends (pages of
    100 rows, always another page), so a tool that reads it would hit any cap.
    """
    total = sum(float(row["spend"]) for row in rows)

    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if params["level"] == "account":
            return {"data": [{"spend": str(total)}]}
        if "sort" in params:
            if sorted_error is not None:
                raise sorted_error
            if {"actions", "action_values"} & set(fields):
                raise _unknown_error()
            return {
                "data": [{"adset_id": row["adset_id"], "spend": row["spend"]} for row in rows[: params["limit"]]],
                "paging": {"cursors": {"after": "n"}, "next": "https://graph.example/next"},
            }
        if "filtering" in params:
            if filtered_error is not None:
                raise filtered_error
            (selector,) = params["filtering"]
            wanted = set(selector["value"])
            data = [row for row in rows if row["adset_id"] in wanted or row["campaign_id"] in wanted]
            return {"data": data[: params["limit"]]}
        page = int(params.get("after") or 0)
        return {"data": rows[page * 100:(page + 1) * 100],
                "paging": {"cursors": {"after": str(page + 1)}, "next": "https://graph.example/next"}}

    return handler


def test_advertiser_context_large_account_returns_a_labelled_partial_briefing(monkeypatch) -> None:
    rows = _large_account_rows(2500)
    client = _context_client()
    client.insights_handler = _large_account_handler(rows)
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", lookback_days=28))

    insights_calls = _calls(client, "get_insights")
    assert len(insights_calls) == 3
    sorted_call = next(call for call in insights_calls if "sort" in call["params"])
    assert sorted_call["params"]["sort"] == ["spend_descending"]
    assert sorted_call["params"]["limit"] == performance_signals.CONTEXT_MAX_ADSETS
    assert set(sorted_call["fields"]) == {"adset_id", "spend"}
    full_call = next(call for call in insights_calls if "filtering" in call["params"])
    (selector,) = full_call["params"]["filtering"]
    assert selector["field"] == "adset.id" and selector["operator"] == "IN"
    assert len(selector["value"]) == performance_signals.CONTEXT_MAX_ADSETS
    assert {"objective", "optimization_goal", "actions", "results"} <= set(full_call["fields"])
    assert "sort" not in full_call["params"]

    coverage = result["adset_coverage"]
    examined_spend = sum(5000.0 - index for index in range(performance_signals.CONTEXT_MAX_ADSETS))
    account_spend = sum(5000.0 - index for index in range(2500))
    assert coverage["adsets_examined"] == performance_signals.CONTEXT_MAX_ADSETS
    assert coverage["more_adsets_exist"] is True and coverage["complete"] is False
    assert coverage["examined_spend"] == pytest.approx(examined_spend)
    assert coverage["account_spend"] == pytest.approx(account_spend)
    assert coverage["share_of_account_spend"] == pytest.approx(examined_spend / account_spend)
    assert result["metrics"]["adset_rows"] == performance_signals.CONTEXT_MAX_ADSETS
    assert any("highest-spending ad sets" in signal and "were not examined" in signal
               for signal in result["missing_signals"])
    assert result["optimization_goal_volume"][0]["adset_count"] == performance_signals.CONTEXT_MAX_ADSETS
    assert len(json.dumps(result).encode()) < 20_000


def test_advertiser_context_small_account_is_complete(monkeypatch) -> None:
    rows = _large_account_rows(3)
    client = _context_client()

    def handler(object_id, fields, params):
        payload = _large_account_handler(rows)(object_id, fields, params)
        payload.pop("paging", None)
        return payload

    client.insights_handler = handler
    _patch(monkeypatch, client)
    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42"))
    coverage = result["adset_coverage"]
    assert coverage["complete"] is True and coverage["adsets_examined"] == 3
    assert coverage["share_of_account_spend"] == pytest.approx(1.0)
    assert not any("were not examined" in signal for signal in result["missing_signals"])


@pytest.mark.parametrize("failing", ["sorted", "filtered"])
def test_advertiser_context_adset_read_failure_is_reported_not_raised(monkeypatch, failing) -> None:
    rows = _large_account_rows(10)
    client = _context_client()
    error = _permission_error()
    client.insights_handler = _large_account_handler(
        rows, sorted_error=error if failing == "sorted" else None, filtered_error=error if failing == "filtered" else None,
    )
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42"))

    assert len(_calls(client, "get_insights")) <= 3
    coverage = result["adset_coverage"]
    assert coverage["status"] == "failed" and "Permissions" in coverage["error"]
    assert result["metrics"]["adset_rows"] == 0
    assert result["account"]["name"] == "Shop"
    assert result["datasets"]["items"][0]["id"] == "px_1"
    assert any("Ad set insights could not be read" in signal for signal in result["missing_signals"])


def test_advertiser_context_object_ids_read_only_the_named_objects(monkeypatch) -> None:
    rows = _large_account_rows(2500)
    rows[1]["campaign_id"] = "777"
    client = _context_client()
    client.insights_handler = _large_account_handler(rows)
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(
        account_id="act_42", object_ids=["100000", "777", "999"],
    ))

    filtered = [call for call in _calls(client, "get_insights") if "filtering" in call["params"]]
    assert sorted(call["params"]["filtering"][0]["field"] for call in filtered) == ["adset.id", "campaign.id"]
    assert not any("sort" in call["params"] for call in _calls(client, "get_insights"))
    assert len(_calls(client, "get_insights")) == 3
    assert result["metrics"]["adset_rows"] == 2
    assert result["object_scope"]["unmatched_object_ids"] == ["999"]
    assert result["adset_coverage"]["mode"] == "object_ids"


def test_insights_page_cap_says_pages_not_rows(monkeypatch) -> None:
    def handler(object_id, fields, params):
        page = int(params.get("after") or 0)
        return {"data": [_day_row(f"2026-10-{page + 1:02d}", spend=1, impressions=1, clicks=1)],
                "paging": {"cursors": {"after": str(page + 1)}, "next": "https://graph.example/next"}}

    _patch(monkeypatch, RecordingClient(insights_handler=handler))
    with pytest.raises(ValidationError, match="pages"):
        asyncio.run(performance_signals._read_insights_pages("cmp_1", ["spend"], {"level": "campaign"}))


# ---------------------------------------------------------------------------
# Round three: Meta's results field is read only when its shape is unambiguous (G9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    [{"indicator": "actions:lead", "values": [{"value": True}]}],
    [{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": ["1d_click"]},
                                              {"value": "9", "attribution_windows": ["7d_click"]}]}],
    [{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": "not_default"},
                                              {"value": "9", "attribution_windows": ["default"]}]}],
    [{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": ["default"]}]},
     {"indicator": "actions:lead", "values": [{"value": "9", "attribution_windows": ["default"]}]}],
    [{"indicator": {"unexpected": "object"}, "values": [{"value": "2"}]}],
    [{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": 7}]}],
    [{"indicator": "actions:lead", "values": [{"value": "NaN"}]}],
    [{"indicator": "actions:lead", "values": [{"value": "Infinity"}]}],
])
def test_g9_unexpected_result_shape_is_explicitly_unknown(raw) -> None:
    parsed = performance_signals._parse_result_list(raw)
    assert parsed["status"] == "unreadable", parsed
    assert parsed["reason"], parsed


@pytest.mark.parametrize("raw", [
    [{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": ["default"]},
                                              {"value": "9", "attribution_windows": ["default"]}]}],
    [{"indicator": "actions:lead", "values": [{"value": "-3"}]}],
    [{"indicator": "actions:lead", "values": [{"value": float("nan")}]}],
    [{"indicator": "actions:lead", "values": [{"value": None}]}],
    [{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": ["default", 7]}]}],
    [{"indicator": "actions:lead", "values": [{"value": "2"}, "9"]}],
    [{"indicator": "actions:lead", "values": {"value": "2"}}],
    [{"indicator": 7, "values": [{"value": "2"}]}],
    [{"indicator": "  ", "values": [{"value": "2"}]}],
    [{"indicator": True, "values": [{"value": "2"}]}],
    [{"indicator": "actions:lead", "values": [{"value": "2"}]}, {"indicator": "actions:lead", "values": []}],
    [{"indicator": "actions:lead", "values": [{"value": "2"}]}, {"indicator": 3, "values": []}],
    ["actions:lead"],
    {"indicator": "actions:lead"},
])
def test_g9_other_ambiguous_or_malformed_shapes_are_unknown_never_a_crash(raw) -> None:
    parsed = performance_signals._parse_result_list(raw)
    assert parsed["status"] == "unreadable", parsed
    assert parsed["reason"], parsed
    assert "value" not in parsed


@pytest.mark.parametrize(("raw", "expected"), [
    ([{"indicator": "actions:offsite_conversion.fb_pixel_purchase",
       "values": [{"value": "12", "attribution_windows": ["default"]}]}],
     {"status": "read", "indicator": "actions:offsite_conversion.fb_pixel_purchase", "value": 12.0}),
    ([{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": ["1d_click"]},
                                               {"value": "9", "attribution_windows": ["default"]}]}],
     {"status": "read", "indicator": "actions:lead", "value": 9.0}),
    ([{"indicator": "actions:lead", "values": [{"value": "4", "attribution_windows": ["7d_click"]}]}],
     {"status": "read", "indicator": "actions:lead", "value": 4.0}),
    ([{"indicator": "actions:lead", "values": [{"value": 5}]}],
     {"status": "read", "indicator": "actions:lead", "value": 5.0}),
    ([{"indicator": "actions:lead", "values": []}], {"status": "read", "indicator": "actions:lead", "value": None}),
    ([], {"status": "absent"}),
    (None, {"status": "absent"}),
])
def test_g9_confirmed_live_shape_and_unambiguous_shapes_still_read(raw, expected) -> None:
    assert performance_signals._parse_result_list(raw) == expected


def test_g9_mixed_indicators_still_reported_as_mixed() -> None:
    parsed = performance_signals._parse_result_list([
        {"indicator": "actions:lead", "values": [{"value": "2"}]},
        {"indicator": "actions:link_click", "values": [{"value": "9"}]},
    ])
    assert parsed == {"status": "mixed", "indicators": ["actions:lead", "actions:link_click"]}


def test_g9_unreadable_result_reason_reaches_the_trend_report(monkeypatch) -> None:
    rows = [_day_row("2026-11-01", spend=10, impressions=100, clicks=5)]
    rows[0]["results"] = [{"indicator": "actions:lead", "values": [{"value": True}]}]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="1", since="2026-11-01", until="2026-11-01",
    ))
    assert result["result"]["status"] == "unreadable"
    assert result["result"]["unreadable_reasons"]
    assert result["periods"][0]["metrics"]["cost_per_result"] is None
    assert any("could not be read" in signal and "true or false" in signal for signal in result["missing_signals"])


def test_g9_unreadable_row_in_a_readable_window_makes_its_day_unknown(monkeypatch) -> None:
    rows = [
        _day_row("2026-11-01", spend=10, impressions=100, clicks=5, result=("actions:lead", 2)),
        _day_row("2026-11-02", spend=10, impressions=100, clicks=5),
    ]
    rows[1]["results"] = [{"indicator": "actions:lead", "values": [{"value": "Infinity"}]}]
    _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="1", since="2026-11-01", until="2026-11-02",
    ))
    assert result["periods"][0]["metrics"]["cost_per_result"] == pytest.approx(5.0)
    assert result["periods"][1]["metrics"]["cost_per_result"] is None
    assert any("not a finite number" in signal for signal in result["missing_signals"])


def test_g9_unreadable_result_is_not_judged_in_goal_volume() -> None:
    row = _goal_row("as_1", "LEAD_GENERATION", {"lead": 500})
    row["meta_result"] = performance_signals._parse_result_list(
        [{"indicator": "actions:lead", "values": [{"value": "2", "attribution_windows": ["1d_click"]},
                                                  {"value": "9", "attribution_windows": ["7d_click"]}]}]
    )
    entry = performance_signals._goal_volume([row], 7)[0]
    assert entry["verdict"] == "not_assessed"
    assert "attribution" in entry["reason"]


# ---------------------------------------------------------------------------
# Round three: one entity's result definition is never applied to another (G19)
# ---------------------------------------------------------------------------


def test_g19_ranking_does_not_borrow_another_ads_result_definition(monkeypatch) -> None:
    ranked = [
        {"ad_id": str(ad_id), "spend": str(spend), "impressions": "1000",
         **{field: "AVERAGE" for field in performance_signals.RANKING_FIELDS}}
        for ad_id, spend in ((20, 20), (21, 10))
    ]

    def handler(object_id, fields, params):
        return {"data": ranked if params["level"] == "ad" else [{"spend": "30", "impressions": "2000", "clicks": "20"}]}

    async def costs(*args, **kwargs):
        return {
            "20": {"ad_id": "20", "clicks": "10"},
            "21": {"ad_id": "21", "clicks": "10", "results": [{"indicator": "actions:lead", "values": [{"value": "2"}]}]},
        }, None

    _patch(monkeypatch, RecordingClient(insights_handler=handler))
    monkeypatch.setattr(performance_signals, "_ranking_cost_rows", costs)
    result = asyncio.run(performance_signals.get_auction_ranking_report("campaign", "10", max_ads=2))
    unknown = next(ad for ad in result["ranked_ads"] if ad["ad_id"] == "20")
    assert unknown["result_indicator"] is None, unknown
    assert unknown["results"] is None, unknown
    assert unknown["diagnosis"] is None, unknown
    assert unknown["result_status"] == "not_reported"
    known = next(ad for ad in result["ranked_ads"] if ad["ad_id"] == "21")
    assert known["result_indicator"] == "actions:lead" and known["results"] == 2.0
    # The pooled baseline for leads uses only the ad that reports leads: 10 / 2.
    assert known["cost_per_result_baseline"]["value"] == pytest.approx(5.0)
    # The pooled scope total cannot count ad 20's spend against leads it may not optimize for.
    assert result["scope_metrics"]["results"] is None
    assert result["scope_metrics"]["cost_per_result"] is None


def test_g19_unknown_goal_is_not_judged_from_another_entitys_results() -> None:
    rows = [
        {"adset_id": "20", "optimization_goal": "LEAD_GENERATION", "metrics": {"spend": 10},
         "meta_result": {"status": "read", "indicator": "actions:lead", "value": 50}},
        {"adset_id": "21", "optimization_goal": "OFFSITE_CONVERSIONS", "metrics": {"spend": 20},
         "meta_result": {"status": "absent"}},
    ]
    result = performance_signals._goal_volume(rows, 7)
    unknown = next(goal for goal in result if goal["optimization_goal"] == "OFFSITE_CONVERSIONS")
    assert unknown["verdict"] == "not_assessed", unknown
    assert "promoted_object" in unknown["reason"]
    lead = next(goal for goal in result if goal["optimization_goal"] == "LEAD_GENERATION")
    assert lead["verdict"] == "all_adsets_meet_threshold"


def test_g19_same_goal_absent_result_is_not_zero_from_a_sibling() -> None:
    rows = [
        {"adset_id": "20", "optimization_goal": "VALUE", "metrics": {"spend": 10},
         "meta_result": {"status": "read", "indicator": "actions:offsite_conversion.fb_pixel_purchase", "value": 80}},
        {"adset_id": "21", "optimization_goal": "VALUE", "metrics": {"spend": 20}, "meta_result": {"status": "absent"}},
    ]
    entry = performance_signals._goal_volume(rows, 7)[0]
    assert entry["adsets_meeting_threshold"] == 1
    assert entry["adsets_below_threshold"] == 0
    assert entry["adsets_not_judged"] == 1
    assert entry["verdict"] == "judged_adsets_meet_threshold"


def test_g19_absent_result_with_a_known_goal_event_uses_that_ad_sets_own_actions() -> None:
    rows = [
        {"adset_id": "20", "optimization_goal": "OFFSITE_CONVERSIONS", "metrics": {"spend": 10},
         "meta_result": {"status": "read", "indicator": "actions:offsite_conversion.fb_pixel_purchase", "value": 80}},
        {"adset_id": "21", "optimization_goal": "LINK_CLICKS", "metrics": {"spend": 20},
         "actions_map": {"link_click": 30}, "meta_result": {"status": "absent"}},
    ]
    volume = {entry["optimization_goal"]: entry for entry in performance_signals._goal_volume(rows, 7)}
    clicks = volume["LINK_CLICKS"]
    assert clicks["verdict"] == "no_adsets_meet_threshold"
    assert clicks["top_below_threshold"][0]["events"] == 30
    assert "link_click" in clicks["event_basis"]


def test_g19_group_spend_does_not_count_a_sibling_without_results_as_zero(monkeypatch) -> None:
    rows = [dict(row) for row in CONTEXT_ROWS]
    rows[0]["results"] = _meta_result("actions:offsite_conversion.fb_pixel_purchase", 300)
    client = _context_client()
    client.insights_handler = lambda object_id, fields, params: {"data": rows}
    _patch(monkeypatch, client)
    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", lookback_days=28))
    by_objective = {row["objective"]: row for row in result["spend_by_objective"]}
    sales = by_objective["OUTCOME_SALES"]
    assert sales["results"] is None and sales["cost_per_result"] is None
    assert sales["adsets_without_reported_result"] == 1
    assert sales["result_indicator"] == "actions:offsite_conversion.fb_pixel_purchase"


def test_g19_trend_entities_each_keep_their_own_result(monkeypatch) -> None:
    rows = {
        "111": [_day_row("2026-11-01", spend=10, impressions=100, clicks=5, result=("actions:lead", 2)),
                _day_row("2026-11-02", spend=10, impressions=100, clicks=5)],
        "222": [_day_row("2026-11-01", spend=30, impressions=300, clicks=15),
                _day_row("2026-11-02", spend=30, impressions=300, clicks=15)],
    }
    _patch(monkeypatch, RecordingClient(insights_handler=_per_object_handler(rows)))
    trend = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_ids=["111", "222"], since="2026-11-01", until="2026-11-02",
    ))
    entities = {entity["object_id"]: entity for entity in trend["entities"]}
    # The same entity reported leads on another day, so its silent day counts zero leads.
    assert entities["111"]["totals"]["inputs"]["results"] == 2.0
    # The other entity never reported a result, so nothing is borrowed from the first.
    assert entities["222"]["result"]["status"] == "not_reported"
    assert entities["222"]["totals"]["inputs"]["results"] is None


# ---------------------------------------------------------------------------
# Round three: reach as a trend metric, never summed across days
# ---------------------------------------------------------------------------


def _reach_handler(daily: dict[str, list[dict[str, Any]]], *, range_reach: Callable[[str | None, str, str], Any],
                   range_error: Exception | None = None, id_field: str | None = None):
    """Daily rows per object (or per child id under a parent), and one row per time range for reach queries."""

    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if "time_ranges" in params:
            assert "time_increment" not in params and "time_range" not in params and "date_preset" not in params
            assert set(fields) >= {"reach", "date_start", "date_stop"}
            if range_error is not None:
                raise range_error
            data = []
            keys = params["filtering"][0]["value"] if id_field else [None]
            for key in keys:
                for window in params["time_ranges"]:
                    value = range_reach(key or object_id, window["since"], window["until"])
                    if value is None:
                        continue
                    row = {"date_start": window["since"], "date_stop": window["until"], "reach": str(value)}
                    if id_field:
                        row[id_field] = key
                    data.append(row)
            return {"data": data}
        if "sort" in params:
            return {"data": [{id_field: key, "spend": "10"} for key in daily]}
        window = json.loads(params["time_range"])
        if id_field:
            return {"data": [{**row, id_field: key} for key, rows in daily.items() for row in rows
                             if window["since"] <= row["date_start"] <= window["until"]]}
        rows = daily.get(object_id, [])
        return {"data": [row for row in rows if window["since"] <= row["date_start"] <= window["until"]]}

    return handler


def _reach_day(day: str, reach: int, **kwargs: Any) -> dict[str, Any]:
    row = _day_row(day, spend=kwargs.pop("spend", 10), impressions=kwargs.pop("impressions", 1000),
                   clicks=kwargs.pop("clicks", 10), purchases=kwargs.pop("purchases", 1))
    row["reach"] = str(reach)
    return row


def test_trend_daily_reach_comes_from_each_days_row(monkeypatch) -> None:
    rows = [_reach_day("2026-11-01", 400), _reach_day("2026-11-02", 300)]
    client = _patch(monkeypatch, RecordingClient(insights_handler=_reach_handler(
        {"cmp_1": rows}, range_reach=lambda *args: pytest.fail("no range query is needed for days"),
    )))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", metrics=["reach", "cpm"], since="2026-11-01", until="2026-11-02",
    ))
    (call,) = _calls(client, "get_insights")
    assert "reach" in call["fields"]
    assert [period["metrics"]["reach"] for period in result["periods"]] == [400.0, 300.0]
    assert result["changes"]["reach"]["direction"] == "down"
    assert result["changes"]["reach"]["assessment"] == "neutral"
    assert "never summed" in result["reach_policy"]


def test_trend_weekly_reach_is_one_query_per_week_not_a_sum_of_days(monkeypatch) -> None:
    days = _days(date(2026, 11, 15), 12)  # Wed 2026-11-04 .. Sun 2026-11-15: a partial week and a full week
    rows = [_reach_day(day, 100) for day in days]
    client = _patch(monkeypatch, RecordingClient(insights_handler=_reach_handler(
        {"cmp_1": rows}, range_reach=lambda key, since, until: 150,
    )))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", metrics=["reach"], granularity="week",
        since=days[0], until=days[-1],
    ))
    reach_call = next(call for call in _calls(client, "get_insights") if "time_ranges" in call["params"])
    # The partial first week is queried only over its days inside the window.
    assert reach_call["params"]["time_ranges"] == [
        {"since": "2026-11-04", "until": "2026-11-08"}, {"since": "2026-11-09", "until": "2026-11-15"},
    ]
    assert reach_call["params"]["level"] == "campaign"
    assert [period["metrics"]["reach"] for period in result["periods"]] == [150.0, 150.0]
    assert [period["inputs"]["reach"] for period in result["periods"]] == [150.0, 150.0]


def test_trend_reach_query_failure_is_null_with_the_reason(monkeypatch) -> None:
    rows = [_reach_day(day, 100) for day in _days(date(2026, 11, 15), 7)]
    _patch(monkeypatch, RecordingClient(insights_handler=_reach_handler(
        {"cmp_1": rows}, range_reach=lambda *args: 1, range_error=_permission_error(),
    )))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_id="cmp_1", metrics=["reach", "cpc"], granularity="week",
        since="2026-11-09", until="2026-11-15",
    ))
    assert result["periods"][0]["metrics"]["reach"] is None
    assert result["periods"][0]["metrics"]["cpc"] == pytest.approx(1.0)
    assert any("Reach for weeks or months could not be read" in signal and "Permissions" in signal
               for signal in result["missing_signals"])


def test_trend_multi_entity_reach_totals_come_from_a_window_query(monkeypatch) -> None:
    daily = {key: [_reach_day(day, 100) for day in _days(date(2026, 11, 3), 3)] for key in ("111", "222")}
    client = _patch(monkeypatch, RecordingClient(insights_handler=_reach_handler(
        daily, range_reach=lambda key, since, until: {"111": 180, "222": 250}[key],
    )))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_ids=["111", "222"], metrics=["reach", "spend"],
        since="2026-11-01", until="2026-11-03",
    ))
    reach_calls = [call for call in _calls(client, "get_insights") if "time_ranges" in call["params"]]
    assert {call["object_id"] for call in reach_calls} == {"111", "222"}
    assert all(call["params"]["time_ranges"] == [{"since": "2026-11-01", "until": "2026-11-03"}] for call in reach_calls)
    entities = {entity["object_id"]: entity for entity in result["entities"]}
    assert entities["111"]["totals"]["metrics"]["reach"] == 180.0
    assert entities["222"]["totals"]["metrics"]["reach"] == 250.0
    assert entities["111"]["totals"]["metrics"]["spend"] == 30.0
    assert entities["111"]["series"]["reach"] == [100, 100, 100]


def test_trend_child_level_reach_is_one_filtered_query_keyed_by_child(monkeypatch) -> None:
    daily = {key: [_reach_day(day, 100) for day in _days(date(2026, 11, 15), 7)] for key in ("501", "502")}
    client = _patch(monkeypatch, RecordingClient(insights_handler=_reach_handler(
        daily, id_field="campaign_id",
        range_reach=lambda key, since, until: {"501": 500, "502": 600}[key] + (1 if since == until else 0),
    )))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="account", object_id="42", child_level="campaign", max_entities=2, metrics=["reach"],
        granularity="week", since="2026-11-09", until="2026-11-15",
    ))
    (reach_call,) = [call for call in _calls(client, "get_insights") if "time_ranges" in call["params"]]
    assert reach_call["object_id"] == "act_42"
    assert reach_call["params"]["filtering"] == [{"field": "campaign.id", "operator": "IN", "value": ["501", "502"]}]
    entities = {entity["object_id"]: entity for entity in result["entities"]}
    assert entities["501"]["series"]["reach"] == [500]
    assert entities["502"]["totals"]["metrics"]["reach"] == 600.0


def test_anomalies_can_screen_daily_reach(monkeypatch) -> None:
    days = _days(date(2026, 11, 15), 15)
    rows = [_reach_day(day, reach) for day, reach in zip(days, [*SPIKE_BASELINE_SPEND, 1000], strict=True)]
    client = _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler(rows)))
    result = asyncio.run(performance_signals.detect_performance_anomalies(
        level="campaign", object_id="cmp_1", metrics=["reach"], lookback_days=1, baseline_days=14,
        end_date="2026-11-15",
    ))
    assert "reach" in _calls(client, "get_insights")[0]["fields"]
    assert [(anomaly["metric"], anomaly["day"]) for anomaly in result["anomalies"]] == [("reach", "2026-11-15")]


# ---------------------------------------------------------------------------
# Round three: per-period series for several entities, bounded in size
# ---------------------------------------------------------------------------


def test_multi_entity_trend_returns_each_entitys_period_series(monkeypatch) -> None:
    rows = {
        "111": [_day_row("2026-11-01", spend=10, impressions=1000, clicks=10, purchases=1),
                _day_row("2026-11-03", spend=30, impressions=1000, clicks=20, purchases=3)],
        "222": [_day_row(day, spend=20, impressions=2000, clicks=10, purchases=2) for day in _days(date(2026, 11, 3), 3)],
    }
    _patch(monkeypatch, RecordingClient(insights_handler=_per_object_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_ids=["111", "222"], metrics=["spend", "cpc"], since="2026-11-01", until="2026-11-03",
    ))
    assert result["series"]["period_starts"] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert result["series"]["periods_left_out"] == 0
    entities = {entity["object_id"]: entity for entity in result["entities"]}
    assert entities["111"]["series"] == {"spend": [10, None, 30], "cpc": [1, None, 1.5]}
    assert entities["222"]["series"]["spend"] == [20, 20, 20]


def test_multi_entity_weekly_series_marks_partial_periods_and_days(monkeypatch) -> None:
    rows = {"111": [_day_row(day, spend=10, impressions=1000, clicks=10, purchases=1)
                    for day in _days(date(2026, 11, 15), 12)]}
    _patch(monkeypatch, RecordingClient(insights_handler=_per_object_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="campaign", object_ids=["111"], metrics=["spend"], granularity="week",
        since="2026-11-04", until="2026-11-15",
    ))
    assert result["series"]["period_starts"] == ["2026-11-02", "2026-11-09"]
    assert result["series"]["partial_periods"] == ["2026-11-02"]
    assert result["entities"][0]["series"] == {"spend": [50, 70], "days_with_data": [5, 7]}


def test_multi_entity_series_keeps_the_most_recent_periods_and_says_what_was_left_out(monkeypatch) -> None:
    rows = {
        str(index): [
            _day_row(day, spend=10.123456 + index + offset, impressions=1000 + offset, clicks=17, purchases=3)
            for offset, day in enumerate(_days(date(2026, 11, 30), 90))
        ]
        for index in range(1, 6)
    }
    _patch(monkeypatch, RecordingClient(insights_handler=_per_object_handler(rows)))
    result = asyncio.run(performance_signals.get_performance_trend(
        level="ad", object_ids=list(rows), since="2026-09-02", until="2026-11-30",
    ))
    series = result["series"]
    assert 0 < series["periods_returned"] < 90
    assert series["period_starts"][-1] == "2026-11-30"
    assert series["periods_left_out"] == 90 - series["periods_returned"]
    assert all(len(entity["series"]["cpc"]) == series["periods_returned"] for entity in result["entities"])
    assert any("earliest" in signal and "series" in signal for signal in result["missing_signals"])
    assert len(json.dumps(result).encode()) < 20_000
    # Totals and changes still cover the whole window.
    assert result["entities"][0]["changes"]["cpc"]["first_period"] == "2026-09-02"

    asked = asyncio.run(performance_signals.get_performance_trend(
        level="ad", object_ids=list(rows), since="2026-09-02", until="2026-11-30", series_periods=60,
    ))
    assert asked["series"]["periods_returned"] == 60
    assert len(json.dumps(asked).encode()) < performance_signals.SERIES_MAX_BYTES

    none = asyncio.run(performance_signals.get_performance_trend(
        level="ad", object_ids=list(rows), since="2026-09-02", until="2026-11-30", series_periods=0,
    ))
    assert none["series"]["periods_returned"] == 0
    assert all(entity["series"] == {name: [] for name in entity["series"]} for entity in none["entities"])


@pytest.mark.parametrize("kwargs", [
    {"level": "campaign", "object_id": "1", "series_periods": 5},
    {"level": "campaign", "object_ids": ["1"], "series_periods": -1},
    {"level": "campaign", "object_ids": ["1"], "series_periods": 367},
    {"level": "campaign", "object_ids": ["1"], "series_periods": True},
])
def test_series_periods_validation_makes_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_performance_trend(since="2026-11-01", until="2026-11-02", **kwargs))
    assert client.calls == []


# ---------------------------------------------------------------------------
# Round three: advertiser context with an explicit date range and with ad ids
# ---------------------------------------------------------------------------


def test_advertiser_context_explicit_range_is_sent_and_stated(monkeypatch) -> None:
    client = _patch(monkeypatch, _context_client())
    _pin_now(monkeypatch, datetime(2026, 11, 12, 17, tzinfo=timezone.utc))

    result = asyncio.run(performance_signals.get_advertiser_context(
        account_id="act_42", since="2026-10-01", until="2026-10-14",
    ))

    for call in _calls(client, "get_insights"):
        assert json.loads(call["params"]["time_range"]) == {"since": "2026-10-01", "until": "2026-10-14"}
    assert result["window"]["since"] == "2026-10-01" and result["window"]["until"] == "2026-10-14"
    assert result["window"]["days"] == 14 and result["window"]["source"] == "since_until"
    assert "no comparison period" in result["window"]["weekly_volume_basis"]
    clicks = {row["optimization_goal"]: row for row in result["optimization_goal_volume"]}["LINK_CLICKS"]
    assert clicks["top_meeting_threshold"][0]["events_per_week"] == pytest.approx(1000 / 14 * 7)
    assert not any("incomplete" in signal for signal in result["missing_signals"])


def test_advertiser_context_default_window_is_still_thirty_days_to_yesterday(monkeypatch) -> None:
    client = _patch(monkeypatch, _context_client())
    _pin_now(monkeypatch, datetime(2026, 11, 12, 17, tzinfo=timezone.utc))
    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42"))
    assert result["window"] == {
        "since": "2026-10-13", "until": "2026-11-11", "days": 30, "source": "lookback_days",
        "weekly_volume_basis": "events in these 30 days / 30 x 7; there is no comparison period",
    }
    assert json.loads(_calls(client, "get_insights")[0]["params"]["time_range"])["until"] == "2026-11-11"


def test_advertiser_context_range_reaching_today_says_its_last_days_are_incomplete(monkeypatch) -> None:
    _patch(monkeypatch, _context_client())
    _pin_now(monkeypatch, datetime(2026, 11, 12, 17, tzinfo=timezone.utc))
    result = asyncio.run(performance_signals.get_advertiser_context(
        account_id="act_42", since="2026-11-01", until="2026-11-12",
    ))
    assert any("incomplete" in signal and "2026-11-12" in signal for signal in result["missing_signals"])


def test_advertiser_context_short_range_does_not_scale_goal_volume_up_to_a_week(monkeypatch) -> None:
    _patch(monkeypatch, _context_client())
    result = asyncio.run(performance_signals.get_advertiser_context(
        account_id="act_42", since="2026-11-01", until="2026-11-03",
    ))
    assert result["window"]["days"] == 3
    assert {entry["verdict"] for entry in result["optimization_goal_volume"]} == {"not_assessed"}
    assert all("fewer than 7 days" in entry["reason"] for entry in result["optimization_goal_volume"])


@pytest.mark.parametrize("kwargs", [
    {"since": "2026-11-01"},
    {"until": "2026-11-01"},
    {"since": "2026-11-05", "until": "2026-11-01"},
    {"since": "2026-11-01", "until": "2026-13-01"},
    {"since": "2025-01-01", "until": "2026-11-01"},
    {"since": "2026-11-01", "until": "2026-11-05", "lookback_days": 30},
    {"lookback_days": True},
    {"ad_ids": ["1"], "object_ids": ["2"]},
    {"ad_ids": ["ad_1"]},
    {"ad_ids": [str(index) for index in range(51)]},
])
def test_advertiser_context_window_and_ad_ids_validation_makes_no_calls(monkeypatch, kwargs) -> None:
    client = _patch(monkeypatch, RecordingClient())
    with pytest.raises(ValidationError):
        asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", **kwargs))
    assert client.calls == []


def _ad_context_rows() -> list[dict[str, Any]]:
    rows = []
    for ad_id, adset_id, goal, spend, clicks in (("901", "301", "LINK_CLICKS", 60, 600), ("902", "301", "LINK_CLICKS", 40, 300),
                                                 ("903", "302", "OFFSITE_CONVERSIONS", 100, 50)):
        row = _adset_row(adset_id, objective="OUTCOME_TRAFFIC", goal=goal, spend=spend,
                         actions={"link_click": clicks})
        row.update({"ad_id": ad_id, "ad_name": f"Ad {ad_id}"})
        rows.append(row)
    rows[0]["results"] = _meta_result("actions:link_click", 600)
    rows[1]["results"] = _meta_result("actions:link_click", 300)
    return rows


def test_advertiser_context_ad_ids_read_only_the_named_ads(monkeypatch) -> None:
    rows = _ad_context_rows()

    def handler(object_id, fields, params):
        if params["level"] == "account":
            return {"data": [{"spend": "1000"}]}
        assert params["level"] == "ad"
        (selector,) = params["filtering"]
        assert selector == {"field": "ad.id", "operator": "IN", "value": ["901", "902", "903", "999"]}
        return {"data": [row for row in rows if row["ad_id"] in selector["value"]]}

    client = _context_client()
    client.insights_handler = handler
    _patch(monkeypatch, client)

    result = asyncio.run(performance_signals.get_advertiser_context(
        account_id="act_42", ad_ids=["901", "902", "903", "999"], lookback_days=7,
    ))

    calls = _calls(client, "get_insights")
    assert sorted(call["params"]["level"] for call in calls) == ["account", "ad"]
    ad_call = next(call for call in calls if call["params"]["level"] == "ad")
    assert {"ad_id", "results", "optimization_goal"} <= set(ad_call["fields"])
    assert result["scope"]["ad_ids"] == ["901", "902", "903", "999"]
    assert result["metrics"]["ad_rows"] == 3 and result["metrics"]["spend"] == 200.0
    assert result["object_scope"]["unmatched_ad_ids"] == ["999"]
    assert any("999" in signal for signal in result["missing_signals"])
    assert result["ad_coverage"]["share_of_account_spend"] == pytest.approx(0.2)
    assert "adset_coverage" not in result
    by_goal = {row["optimization_goal"]: row for row in result["spend_by_optimization_goal"]}
    assert by_goal["LINK_CLICKS"]["ad_count"] == 2
    assert by_goal["LINK_CLICKS"]["results"] == 900.0
    assert by_goal["LINK_CLICKS"]["cost_per_result"] == pytest.approx(100 / 900)
    volume = {row["optimization_goal"]: row for row in result["optimization_goal_volume"]}
    assert volume["LINK_CLICKS"]["verdict"] == "not_assessed"
    assert volume["LINK_CLICKS"]["adset_ids_of_named_ads"] == ["301"]
    assert "per ad set" in volume["LINK_CLICKS"]["reason"]
    assert result["funnel"]["steps"][0]["count"] == 950
    assert result["datasets"]["items"][0]["id"] == "px_1"


def test_advertiser_context_ad_read_failure_is_reported_not_raised(monkeypatch) -> None:
    def handler(object_id, fields, params):
        if params["level"] == "account":
            return {"data": [{"spend": "1000"}]}
        raise _permission_error()

    client = _context_client()
    client.insights_handler = handler
    _patch(monkeypatch, client)
    result = asyncio.run(performance_signals.get_advertiser_context(account_id="act_42", ad_ids=["901"]))
    assert result["ad_coverage"]["status"] == "failed"
    assert result["metrics"]["ad_rows"] == 0
    assert any(signal.startswith("Ad insights could not be read") for signal in result["missing_signals"])
