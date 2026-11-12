"""Tests for reporting and troubleshooting tools in performance_signals."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
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
    client = _patch(monkeypatch, RecordingClient(insights_handler=_daily_handler([])))
    monkeypatch.setattr(performance_signals, "_today", lambda: date(2026, 11, 12))

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
