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


def _day_row(day: str, *, spend: float, impressions: int, clicks: int, purchases: float | None = None,
             purchase_value: float | None = None) -> dict[str, Any]:
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

    insights_call = _calls(client, "get_insights")[0]
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
    assert leads["adset_count"] == 60
    assert leads["adsets_meeting_threshold"] == 0
    assert leads["adsets_below_threshold"] == 60
    below = leads["top_below_threshold"]
    assert len(below) == performance_signals.GOAL_VOLUME_TOP_ADSETS
    assert [row["spend"] for row in below] == sorted((row["spend"] for row in below), reverse=True)
    assert below[0]["adset_id"] == "as_0298"
    assert volume["LINK_CLICKS"]["adsets_meeting_threshold"] == 60
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

    assert result["metrics"]["adset_rows"] == 300
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


def _spend_handler(spend_by_object: dict[str, float]):
    def handler(object_id: str, fields: list[str], params: dict[str, Any]) -> dict[str, Any]:
        if object_id not in spend_by_object:
            return {"data": []}
        return {"data": [{"spend": str(spend_by_object[object_id])}]}

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
    spend_calls = _calls(client, "get_insights")
    assert {call["object_id"] for call in spend_calls} == {"ad_medium", "ad_disapproved", "ad_with_issues"}
    assert all(call["params"]["date_preset"] == "last_7d" and call["fields"] == ["spend"] for call in spend_calls)

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
    assert result["missing_signals"] == []


def test_delivery_errors_orders_by_severity_then_spend(monkeypatch) -> None:
    account = {**ACTIVE_ACCOUNT, "account_status": 9}
    client = _patch(
        monkeypatch,
        RecordingClient(
            objects={"act_42": account},
            edges={("act_42", "ads"): {"data": PROBLEM_ADS}},
            insights_handler=_spend_handler(
                {"act_42": 1000.0, "ad_with_issues": 100.0, "ad_disapproved": 10.0, "ad_medium": 500.0, "ad_paused": 0.0}
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
    assert result["ordering"] == ["severity", "spend_desc", "object_id"]
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
