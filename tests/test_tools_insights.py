"""Insights tool tests."""

from __future__ import annotations

import asyncio
import pytest

from meta_ads_mcp.tools import insights


class FakeInsightsClient:
    """Fake insights client."""

    async def get_insights(self, object_id: str, *, fields, params):
        assert object_id == "act_123"
        assert params["level"] == "account"
        return {
            "data": [
                {
                    "date_start": "2026-03-01",
                    "date_stop": "2026-03-07",
                    "spend": "100",
                    "impressions": "1000",
                    "clicks": "50",
                    "ctr": "5.0",
                    "cpc": "2.0",
                    "cpm": "100.0",
                    "frequency": "1.2",
                    "actions": [{"action_type": "purchase", "value": "2"}],
                    "action_values": [{"action_type": "purchase", "value": "250"}],
                }
            ]
        }


def test_get_entity_insights_normalizes_rows(monkeypatch) -> None:
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeInsightsClient())
    result = asyncio.run(insights.get_entity_insights(level="account", object_id="act_123"))
    assert result["summary"]["metrics"]["spend"] == 100.0
    assert result["items"][0]["metrics"]["roas"] == 2.5


def test_get_entity_insights_normalizes_numeric_account_ids(monkeypatch) -> None:
    class NumericAccountClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            assert object_id == "act_123"
            return await super().get_insights("act_123", fields=fields, params=params)

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: NumericAccountClient())
    result = asyncio.run(insights.get_entity_insights(level="account", object_id="123"))
    assert result["summary"]["metrics"]["spend"] == 100.0


def test_get_entity_insights_preserves_paging(monkeypatch) -> None:
    class PagingInsightsClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            payload = await super().get_insights(object_id, fields=fields, params=params)
            payload["paging"] = {
                "cursors": {"before": "before_1", "after": "after_1"},
                "next": "https://graph.facebook.com/next",
            }
            return payload

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: PagingInsightsClient())
    result = asyncio.run(insights.get_entity_insights(level="account", object_id="act_123"))
    assert result["paging"]["after"] == "after_1"
    assert result["paging"]["next"] == "https://graph.facebook.com/next"


def test_get_entity_insights_rejects_invalid_date_combinations(monkeypatch) -> None:
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeInsightsClient())
    with pytest.raises(insights.ValidationError):
        asyncio.run(
            insights.get_entity_insights(
                level="account",
                object_id="act_123",
                date_preset="last_7d",
                since="2026-03-01",
                until="2026-03-07",
            )
        )


def test_get_entity_insights_rejects_reversed_date_window_before_api(monkeypatch) -> None:
    class FailIfCalledClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            raise AssertionError("date window validation should happen before the API call")

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FailIfCalledClient())
    with pytest.raises(insights.ValidationError, match="until must be on or after since"):
        asyncio.run(
            insights.get_entity_insights(
                level="account",
                object_id="act_123",
                since="2026-03-07",
                until="2026-03-01",
            )
        )


def test_get_entity_insights_accepts_since_until_without_explicit_date_preset(monkeypatch) -> None:
    class DateRangeClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            assert "date_preset" not in params
            assert params["time_range"] == '{"since": "2026-03-01", "until": "2026-03-07"}'
            return await super().get_insights(object_id, fields=fields, params=params)

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: DateRangeClient())
    result = asyncio.run(
        insights.get_entity_insights(
            level="account",
            object_id="act_123",
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert result["summary"]["metrics"]["spend"] == 100.0


def test_get_entity_insights_action_filter_adds_required_fields(monkeypatch) -> None:
    class ActionFieldClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            assert fields == ["campaign_id", "spend", "impressions", "clicks", "actions", "action_values"]
            return await super().get_insights(object_id, fields=fields, params=params)

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: ActionFieldClient())
    result = asyncio.run(
        insights.get_entity_insights(
            level="account",
            object_id="act_123",
            fields=["campaign_id"],
            action_types=["purchase"],
        )
    )
    assert result["summary"]["action_filter"]["matched"] == ["purchase"]


def test_get_entity_insights_treats_blank_date_inputs_as_missing(monkeypatch) -> None:
    class BlankDateClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            assert params["date_preset"] == "last_7d"
            return await super().get_insights(object_id, fields=fields, params=params)

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: BlankDateClient())
    result = asyncio.run(
        insights.get_entity_insights(
            level="account",
            object_id="act_123",
            date_preset="",
            since="",
            until="",
        )
    )
    assert result["summary"]["metrics"]["spend"] == 100.0


def test_get_entity_insights_rejects_unknown_date_preset_before_api(monkeypatch) -> None:
    class FailIfCalledClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            raise AssertionError("date preset validation should happen before the API call")

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FailIfCalledClient())
    with pytest.raises(insights.ValidationError, match="Supported values"):
        asyncio.run(
            insights.get_entity_insights(
                level="account",
                object_id="act_123",
                date_preset="this_week_mon_sun",
            )
        )


def test_get_entity_insights_translates_lifetime_date_alias(monkeypatch) -> None:
    class LifetimeAliasClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            assert params["date_preset"] == "maximum"
            return await super().get_insights(object_id, fields=fields, params=params)

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: LifetimeAliasClient())
    result = asyncio.run(
        insights.get_entity_insights(level="account", object_id="act_123", date_preset="lifetime")
    )
    assert result["summary"]["metrics"]["spend"] == 100.0


def test_get_insights_alias_accepts_time_range(monkeypatch) -> None:
    class TimeRangeClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            assert params["time_range"] == '{"since": "2026-03-01", "until": "2026-03-07"}'
            return await super().get_insights(object_id, fields=fields, params=params)

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: TimeRangeClient())
    result = asyncio.run(
        insights.get_insights(
            level="account",
            object_id="act_123",
            time_range={"since": "2026-03-01", "until": "2026-03-07"},
        )
    )
    assert result["summary"]["metrics"]["spend"] == 100.0


def test_summarize_actions_filters_requested_action_types(monkeypatch) -> None:
    class ActionClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            assert params["date_preset"] == "last_30d"
            payload = await super().get_insights(object_id, fields=fields, params=params)
            payload["data"][0]["actions"].append({"action_type": "onsite_conversion.schedule_appointment", "value": "3"})
            payload["data"][0]["action_values"].append(
                {"action_type": "onsite_conversion.schedule_appointment", "value": "0"}
            )
            return payload

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: ActionClient())
    result = asyncio.run(
        insights.summarize_actions(
            level="account",
            object_id="act_123",
            action_types=["appointment"],
        )
    )
    assert result["action_totals"] == [
        {
            "action_type": "onsite_conversion.schedule_appointment",
            "count": 3.0,
            "value": 0.0,
            "cost_per_action": 100.0 / 3.0,
        }
    ]
    assert result["window"]["date_preset"] == "last_30d"
    assert result["requested_action_types"] == ["appointment"]
    assert result["action_filter_mode"] == "filtered"
    assert "Snowplow" in result["meta_attribution_notice"]


def test_summarize_actions_matches_pixel_purchase_alias(monkeypatch) -> None:
    class PixelPurchaseClient(FakeInsightsClient):
        async def get_insights(self, object_id: str, *, fields, params):
            payload = await super().get_insights(object_id, fields=fields, params=params)
            payload["data"][0]["actions"] = [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "2"}]
            payload["data"][0]["action_values"] = [
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "250"}
            ]
            return payload

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: PixelPurchaseClient())
    result = asyncio.run(
        insights.summarize_actions(
            level="account",
            object_id="act_123",
            action_types=["purchase"],
        )
    )

    assert result["action_totals"] == [
        {
            "action_type": "offsite_conversion.fb_pixel_purchase",
            "count": 2.0,
            "value": 250.0,
            "cost_per_action": 50.0,
        }
    ]
    assert result["summary_metrics"]["roas"] == 2.5


def test_summarize_actions_reports_explicit_window_without_default_preset(monkeypatch) -> None:
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeInsightsClient())
    result = asyncio.run(
        insights.summarize_actions(
            level="account",
            object_id="act_123",
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert result["window"] == {
        "date_preset": None,
        "since": "2026-03-01",
        "until": "2026-03-07",
    }
    assert result["requested_action_types"] == []
    assert result["action_filter_mode"] == "all"


def test_compare_performance_ranks_multiple_objects(monkeypatch) -> None:
    async def fake_get_entity_insights(*, object_id: str, **_: object) -> dict[str, object]:
        metrics = {
            "cmp_1": {"spend": 100.0, "ctr": 0.05, "cpc": 2.0, "roas": 1.2},
            "cmp_2": {"spend": 80.0, "ctr": 0.08, "cpc": 1.5, "roas": 1.8},
        }
        return {"summary": {"count": 1, "metrics": metrics[object_id]}}

    async def fake_object_name(object_id: str) -> str:
        return {"cmp_1": "Campaign One", "cmp_2": "Campaign Two"}[object_id]

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(insights, "_object_name", fake_object_name)

    result = asyncio.run(
        insights.compare_performance(level="campaign", object_ids=["cmp_1", "cmp_2"])
    )

    assert result["summary"]["successful"] == 2
    assert result["summary"]["failed"] == 0
    assert result["summary"]["rankings"]["roas"][0]["object_id"] == "cmp_2"
    assert result["summary"]["rankings"]["cpc"][0]["object_id"] == "cmp_2"


def test_compare_performance_uses_name_from_insights_rows(monkeypatch) -> None:
    async def fake_get_entity_insights(*, object_id: str, **_: object) -> dict[str, object]:
        return {
            "items": [
                {
                    "campaign_id": object_id,
                    "campaign_name": f"Campaign {object_id}",
                    "metrics": {"spend": 100.0, "roas": 2.0},
                }
            ],
            "summary": {"count": 1, "metrics": {"spend": 100.0, "roas": 2.0}},
        }

    async def fail_object_name(_: str) -> str:
        raise AssertionError("_object_name should not be called when the insights row already has a name")

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(insights, "_object_name", fail_object_name)

    result = asyncio.run(
        insights.compare_performance(level="campaign", object_ids=["cmp_1"], fields=["spend"])
    )

    assert result["items"][0]["object_name"] == "Campaign cmp_1"


def test_compare_performance_handles_mixed_success(monkeypatch) -> None:
    async def fake_get_entity_insights(*, object_id: str, **_: object) -> dict[str, object]:
        if object_id == "cmp_bad":
            raise insights.ValidationError("bad object")
        return {
            "items": [{"campaign_id": object_id, "campaign_name": f"Campaign {object_id}"}],
            "summary": {"count": 1, "metrics": {"spend": 100.0, "roas": 2.0}},
        }

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(insights, "_object_name", lambda object_id: object_id)
    result = asyncio.run(
        insights.compare_performance(level="campaign", object_ids=["cmp_good", "cmp_bad"])
    )
    assert result["summary"]["successful"] == 1
    assert result["summary"]["failed"] == 1
    assert any(item["object_id"] == "cmp_bad" and "error" in item for item in result["items"])


def test_get_performance_breakdown_ranks_segments(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        return {
            "items": [
                {"country": "US", "metrics": {"spend": 100.0, "roas": 1.0}},
                {"country": "CA", "metrics": {"spend": 200.0, "roas": 2.0}},
                {"country": "GB", "metrics": {"spend": 50.0, "roas": 0.5}},
            ],
            "paging": {"before": None, "after": "after_1", "next": "next"},
            "summary": {"count": 3, "metrics": {"spend": 350.0}},
        }

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        insights.get_performance_breakdown(level="campaign", object_id="cmp_1", breakdown="country")
    )
    assert result["items"][0]["country"] == "CA"
    assert result["summary"]["top_segments"][0]["country"] == "CA"
    assert result["paging"]["after"] == "after_1"


def test_compare_time_ranges_compares_previous_zero_metrics(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_get_entity_insights(*, since: str, until: str, **_: object) -> dict[str, object]:
        calls.append((since, until))
        metrics = {"spend": 100.0, "ctr": 0.04} if since == "2026-03-01" else {"spend": 0.0, "ctr": 0.0}
        return {"summary": {"metrics": metrics}}

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        insights.compare_time_ranges(
            level="campaign",
            object_id="cmp_1",
            current_since="2026-03-01",
            current_until="2026-03-07",
            previous_since="2026-02-22",
            previous_until="2026-02-28",
        )
    )
    assert calls == [("2026-03-01", "2026-03-07"), ("2026-02-22", "2026-02-28")]
    assert result["comparison"]["spend"]["previous"] == 0.0
    assert result["comparison"]["spend"]["pct_delta"] is None


def test_compare_time_ranges_includes_metric_evidence(monkeypatch) -> None:
    async def fake_get_entity_insights(*, since: str, until: str, **_: object) -> dict[str, object]:
        return {
            "summary": {
                "metrics": {
                    "spend": 100.0,
                    "clicks": 10,
                    "impressions": 1000,
                    "conversions": 2.0,
                    "conversion_value": 300.0,
                    "ctr": 0.01,
                    "cpc": 10.0,
                    "cpm": 100.0,
                    "cvr": 0.2,
                    "cpa": 50.0,
                    "roas": 3.0,
                }
            }
        }

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        insights.compare_time_ranges(
            level="campaign",
            object_id="cmp_1",
            current_since="2026-03-01",
            current_until="2026-03-07",
            previous_since="2026-02-22",
            previous_until="2026-02-28",
        )
    )
    assert any(item["metric"] == "roas" for item in result["evidence"])


def test_compare_time_ranges_rejects_invalid_date_strings(monkeypatch) -> None:
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeInsightsClient())
    with pytest.raises(insights.ValidationError):
        asyncio.run(
            insights.compare_time_ranges(
                level="campaign",
                object_id="cmp_1",
                current_since="2026-03-01",
                current_until="not-a-date",
                previous_since="2026-02-22",
                previous_until="2026-02-28",
            )
        )


def test_compare_time_ranges_handles_empty_result_windows(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        return {"summary": {"metrics": {}}}

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        insights.compare_time_ranges(
            level="campaign",
            object_id="cmp_1",
            current_since="2026-03-01",
            current_until="2026-03-07",
            previous_since="2026-02-22",
            previous_until="2026-02-28",
        )
    )
    assert result["metrics"] == {}
    assert result["previous_metrics"] == {}


def test_export_insights_supports_json_and_csv(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        return {
            "items": [
                {
                    "campaign_id": "cmp_1",
                    "campaign_name": "Campaign One",
                    "spend": 100.0,
                    "metrics": {"spend": 100.0, "roas": 2.5},
                }
            ],
            "summary": {"count": 1, "metrics": {"spend": 100.0, "roas": 2.5}},
        }

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)

    json_result = asyncio.run(
        insights.export_insights(level="campaign", object_id="cmp_1", format="json")
    )
    csv_result = asyncio.run(
        insights.export_insights(level="campaign", object_id="cmp_1", format="csv")
    )

    assert json_result["rows"][0]["campaign_id"] == "cmp_1"
    assert json_result["scope"] == {"level": "campaign", "object_id": "cmp_1"}
    assert csv_result["mime_type"] == "text/csv"
    assert "campaign_id" in csv_result["data"]


def test_export_insights_truncates_large_inline_payloads_by_default(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        items = [{"campaign_id": f"cmp_{index}", "metrics": {"spend": float(index)}} for index in range(150)]
        return {
            "items": items,
            "summary": {"count": len(items), "metrics": {"spend": 11175.0}},
        }

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        insights.export_insights(level="campaign", object_id="cmp_1", format="json")
    )

    assert result["record_count"] == 150
    assert result["returned_count"] == 100
    assert result["truncated"] is True
    assert len(result["rows"]) == 100
    assert "allow_large_output=true" in result["next_step"]


def test_export_insights_can_allow_large_output(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        items = [{"campaign_id": f"cmp_{index}", "metrics": {"spend": float(index)}} for index in range(120)]
        return {
            "items": items,
            "summary": {"count": len(items), "metrics": {"spend": 7140.0}},
        }

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        insights.export_insights(
            level="campaign",
            object_id="cmp_1",
            format="json",
            limit=120,
            allow_large_output=True,
        )
    )

    assert result["record_count"] == 120
    assert result["returned_count"] == 120
    assert result["truncated"] is False
    assert len(result["rows"]) == 120


def test_export_insights_handles_empty_results(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        return {"items": [], "summary": {"count": 0, "metrics": {}}, "paging": {}}

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    json_result = asyncio.run(insights.export_insights(level="campaign", object_id="cmp_1", format="json"))
    csv_result = asyncio.run(insights.export_insights(level="campaign", object_id="cmp_1", format="csv"))
    assert json_result["rows"] == []
    assert csv_result["data"] == ""


def test_export_insights_csv_escapes_nested_rows(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        return {
            "items": [
                {
                    "campaign_id": "cmp_1",
                    "country": "US",
                    "actions": [{"action_type": "purchase", "value": "1"}],
                    "metrics": {"spend": 10.0, "roas": 2.0},
                    "headline": 'He said "hello"',
                }
            ],
            "summary": {"count": 1, "metrics": {"spend": 10.0}},
        }

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(insights.export_insights(level="campaign", object_id="cmp_1", format="csv"))
    assert '"[{""action_type"": ""purchase"", ""value"": ""1""}]"' in result["data"]
    assert '"He said ""hello"""' in result["data"]


def test_export_insights_respects_inline_limit_for_csv(monkeypatch) -> None:
    async def fake_get_entity_insights(**_: object) -> dict[str, object]:
        items = [{"campaign_id": f"cmp_{index}", "metrics": {"spend": float(index)}} for index in range(3)]
        return {"items": items, "summary": {"count": 3, "metrics": {"spend": 3.0}}}

    monkeypatch.setattr(insights, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        insights.export_insights(level="campaign", object_id="cmp_1", format="csv", inline_limit=2)
    )
    assert result["record_count"] == 3
    assert result["returned_count"] == 2
    assert result["truncated"] is True
    assert "cmp_2" not in result["data"]


class FakeAsyncInsightsClient:
    """Fake async insights client."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def get_async_report(self, report_run_id: str, *, fields=None, limit=100, after=None):
        assert report_run_id == "rpt_123"
        return self.payload

    async def create_async_insights_report(self, object_id: str, *, fields, params):
        return {"report_run_id": "rpt_created", "async_status": "Job Running"}


def test_create_async_insights_report_returns_poll_hint(monkeypatch) -> None:
    monkeypatch.setattr(
        insights,
        "get_graph_api_client",
        lambda: FakeAsyncInsightsClient({"report_run_id": "rpt_created", "async_status": "Job Running"}),
    )
    result = asyncio.run(insights.create_async_insights_report(level="campaign", object_id="cmp_1"))
    assert result["report_run_id"] == "rpt_created"
    assert result["requested_fields"] == insights.DEFAULT_INSIGHTS_FIELDS
    assert "get_async_insights_report" in result["poll_hint"]


def test_create_async_insights_report_accepts_since_until_without_explicit_date_preset(monkeypatch) -> None:
    class DateRangeAsyncClient(FakeAsyncInsightsClient):
        async def create_async_insights_report(self, object_id: str, *, fields, params):
            assert "date_preset" not in params
            assert params["time_range"] == '{"since": "2026-03-01", "until": "2026-03-07"}'
            return await super().create_async_insights_report(object_id, fields=fields, params=params)

    monkeypatch.setattr(
        insights,
        "get_graph_api_client",
        lambda: DateRangeAsyncClient({"report_run_id": "rpt_created", "async_status": "Job Running"}),
    )
    result = asyncio.run(
        insights.create_async_insights_report(
            level="campaign",
            object_id="cmp_1",
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert result["report_run_id"] == "rpt_created"


def test_create_async_insights_report_rejects_reversed_date_window(monkeypatch) -> None:
    monkeypatch.setattr(
        insights,
        "get_graph_api_client",
        lambda: FakeAsyncInsightsClient({"report_run_id": "rpt_created", "async_status": "Job Running"}),
    )
    with pytest.raises(insights.ValidationError, match="until must be on or after since"):
        asyncio.run(
            insights.create_async_insights_report(
                level="campaign",
                object_id="cmp_1",
                since="2026-03-07",
                until="2026-03-01",
            )
        )


def test_get_async_insights_report_handles_in_progress_state(monkeypatch) -> None:
    payload = {"status": {"id": "rpt_123", "async_status": "Job Running", "async_percent_completion": 50}, "rows": []}
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeAsyncInsightsClient(payload))
    result = asyncio.run(insights.get_async_insights_report(report_run_id="rpt_123"))
    assert result["status"]["async_status"] == "Job Running"
    assert result["rows"]["items"] == []
    assert result["rows"]["paging"]["after"] is None


def test_get_async_insights_report_does_not_force_default_fields(monkeypatch) -> None:
    class NoDefaultFieldsClient(FakeAsyncInsightsClient):
        async def get_async_report(self, report_run_id: str, *, fields=None, limit=100, after=None):
            assert fields is None
            return {"status": {"id": report_run_id, "async_status": "Job Running"}, "rows": []}

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: NoDefaultFieldsClient({}))
    result = asyncio.run(insights.get_async_insights_report(report_run_id="rpt_123"))
    assert result["status"]["async_status"] == "Job Running"


def test_get_async_insights_report_handles_completed_rows_and_paging(monkeypatch) -> None:
    payload = {
        "status": {"id": "rpt_123", "async_status": "Job Completed", "async_percent_completion": 100},
        "rows": {
            "data": [
                {
                    "campaign_id": "cmp_1",
                    "date_start": "2026-03-01",
                    "date_stop": "2026-03-07",
                    "spend": "100",
                    "impressions": "1000",
                    "clicks": "50",
                    "actions": [{"action_type": "purchase", "value": "2"}],
                    "action_values": [{"action_type": "purchase", "value": "250"}],
                }
            ],
            "paging": {"cursors": {"after": "after_1"}, "next": "next"},
        },
    }
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeAsyncInsightsClient(payload))
    result = asyncio.run(insights.get_async_insights_report(report_run_id="rpt_123"))
    assert result["rows"]["summary"]["count"] == 1
    assert result["rows"]["paging"]["after"] == "after_1"
    assert result["rows"]["items"][0]["metrics"]["roas"] == 2.5


def test_get_async_insights_report_preserves_error_fields(monkeypatch) -> None:
    payload = {
        "status": {
            "id": "rpt_123",
            "async_status": "Job Failed",
            "async_percent_completion": 100,
            "error_code": 1,
            "error_message": "bad report",
            "error_subcode": 99,
            "error_user_title": "Oops",
            "error_user_msg": "Try again",
        },
        "rows": [],
    }
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeAsyncInsightsClient(payload))
    result = asyncio.run(insights.get_async_insights_report(report_run_id="rpt_123"))
    assert result["status"]["error_message"] == "bad report"
    assert result["rows"]["summary"]["count"] == 0
