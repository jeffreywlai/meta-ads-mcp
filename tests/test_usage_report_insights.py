"""Regressions from Nick's 2026-10-22 report, with synthetic account data."""

import asyncio
import json

import pytest

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.tools import insights


def test_compact_placement_projection_stays_below_report_acceptance_budget(monkeypatch):
    class Client:
        async def get_insights(self, object_id, *, fields, params):
            assert "actions" in fields
            return {"data": [
                {"date_start": "2026-10-12", "date_stop": "2026-10-16",
                 "publisher_platform": "instagram", "platform_position": "story",
                 "spend": "100.25", "impressions": "10000", "inline_link_clicks": "50",
                 "actions": [{"action_type": "purchase", "value": "3"}] + [
                     {"action_type": f"unrequested_action_{i}", "value": "123"} for i in range(29)
                 ]} for _ in range(21)
            ]}

    monkeypatch.setattr(insights, "get_graph_api_client", Client)
    result = asyncio.run(insights.get_entity_insights(
        level="account", object_id="act_123", since="2026-10-12", until="2026-10-16",
        fields=["spend", "impressions", "inline_link_clicks"],
        breakdowns=["publisher_platform", "platform_position"], flatten_actions=["purchase"],
    ))
    assert len(json.dumps(result).encode()) < 16_000
    assert result["summary"]["metrics"]["spend"] == 2105.25
    for row in result["items"]:
        assert row["purchase"] == 3
        assert "actions_map" not in row and "actions" not in row
        assert "reach" not in row and "clicks" not in row
        assert "spend" not in row["metrics"] and "impressions" not in row["metrics"]
        assert all(value is not None for value in row["metrics"].values())


def test_explicit_requested_null_is_preserved(monkeypatch):
    class Client:
        async def get_insights(self, *args, **kwargs):
            return {"data": [{"spend": "1", "reach": None}]}

    monkeypatch.setattr(insights, "get_graph_api_client", Client)
    result = asyncio.run(insights.get_entity_insights(
        level="account", object_id="act_123", fields=["spend", "reach"],
    ))
    assert result["items"][0]["reach"] is None
    assert "clicks" not in result["items"][0]


def test_async_poll_arguments_preserve_named_purchase_projection(monkeypatch):
    class Client:
        async def create_async_insights_report(self, *args, **kwargs):
            return {"report_run_id": "report_1"}

        async def get_async_report(self, report_run_id, *, fields, limit, after):
            assert report_run_id == "report_1"
            assert fields == ["spend", "actions"]
            return {"status": {"async_status": "Job Completed"}, "rows": {
                "data": [{"spend": "10", "actions": [{"action_type": "purchase", "value": "2"}]}],
            }}

    monkeypatch.setattr(insights, "get_graph_api_client", Client)
    created = asyncio.run(insights.create_async_insights_report(
        level="account", object_id="act_123", fields=["spend"], flatten_actions=["purchase"],
    ))
    result = asyncio.run(insights.get_async_insights_report(**created["poll_arguments"]))
    row = result["rows"]["items"][0]
    assert row["purchase"] == 2
    assert "actions_map" not in row and "actions" not in row
    assert "impressions" not in row
    assert result["rows"]["summary"]["complete"] is True


class ReportClient:
    def __init__(self, total_rows=99_164):
        self.total_rows = total_rows
        self.status_calls = 0
        self.page_calls = 0

    async def get_async_report(self, report_run_id, *, fields, limit, after):
        self.status_calls += 1
        return {"status": {"async_status": "Job Completed"}, "rows": await self.list_objects(
            report_run_id, "insights", fields=fields, params={"limit": limit, "after": after},
        )}

    async def list_objects(self, report_run_id, edge, *, fields, params):
        self.page_calls += 1
        assert fields is None  # Keep the report's breakdown columns.
        start = int(params.get("after") or "0")
        end = min(start + params["limit"], self.total_rows)
        return {"data": [
            {"date_start": f"2026-0{index % 3 + 1}-01", "product_id": str(index), "spend": "0.10"}
            for index in range(start, end)
        ], **({"paging": {"next": "next", "cursors": {"after": str(end)}}} if end < self.total_rows else {})}


def test_large_catalog_report_is_aggregated_with_exact_decimal_sums(monkeypatch):
    client = ReportClient()
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: client)
    result = asyncio.run(insights.summarize_async_insights_report(
        report_run_id="report_1", group_by=["date_start"], metrics=["spend"],
    ))
    assert result["summary"]["complete"] is True
    assert result["summary"]["rows_scanned"] == 99_164
    assert result["items"] == [
        {"date_start": "2026-01-01", "spend": "3305.50"},
        {"date_start": "2026-02-01", "spend": "3305.50"},
        {"date_start": "2026-03-01", "spend": "3305.40"},
    ]
    assert client.status_calls == 1 and client.page_calls == 199
    assert "next_page_arguments" not in result


def test_aggregation_csv_inputs_work_through_mcp(monkeypatch):
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: ReportClient(3))
    result = asyncio.run(mcp_server.call_tool("summarize_async_insights_report", {
        "report_run_id": "report_1", "group_by": "date_start", "metrics": "spend,impressions",
    })).structured_content
    assert result["summary"]["metrics"] == ["spend", "impressions"]
    assert all(row["spend"] == "0.10" and row["impressions"] is None for row in result["items"])


def test_bounded_aggregation_resume_does_not_claim_entire_report(monkeypatch):
    client = ReportClient(8)
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: client)
    first = asyncio.run(insights.summarize_async_insights_report(
        report_run_id="report_1", metrics=["spend"], page_size=5, max_pages=1,
    ))
    assert first["items"] == [{"spend": "0.50"}]
    assert first["summary"]["complete"] is False
    second = asyncio.run(insights.summarize_async_insights_report(**first["next_page_arguments"]))
    assert second["items"] == [{"spend": "0.30"}]
    assert second["summary"]["page_range_complete"] is True
    assert second["summary"]["complete"] is False
    assert second["summary"]["totals_scope"] == "returned_pages"


def test_aggregation_follows_empty_page_with_advancing_cursor(monkeypatch):
    class Client:
        async def get_async_report(self, *args, **kwargs):
            return {"status": {"async_status": "Job Completed"}, "rows": {
                "data": [], "paging": {"next": "next", "cursors": {"after": "page2"}},
            }}

        async def list_objects(self, report_run_id, edge, *, fields, params):
            assert params["after"] == "page2"
            return {"data": [{"spend": "1.23"}]}

    monkeypatch.setattr(insights, "get_graph_api_client", Client)
    result = asyncio.run(insights.summarize_async_insights_report(report_run_id="r"))
    assert result["items"] == [{"spend": "1.23"}]
    assert result["summary"]["pages_fetched"] == 2
    assert result["summary"]["complete"] is True


@pytest.mark.parametrize("paging", [
    {"next": "next"}, {"next": "next", "cursors": {"after": "same"}},
])
def test_aggregation_rejects_unusable_pagination(monkeypatch, paging):
    class Client:
        async def get_async_report(self, *args, **kwargs):
            return {"status": {"async_status": "Job Completed"},
                    "rows": {"data": [{"spend": "1"}], "paging": paging}}

    monkeypatch.setattr(insights, "get_graph_api_client", Client)
    with pytest.raises(ValidationError, match="missing/repeated cursor"):
        asyncio.run(insights.summarize_async_insights_report(report_run_id="r", after="same"))


def test_aggregation_missing_amount_is_unknown_and_nonadditive_metric_rejected(monkeypatch):
    class Client:
        async def get_async_report(self, *args, **kwargs):
            return {"status": {"async_status": "Job Completed"},
                    "rows": {"data": [{"spend": "1"}, {}]}}

    monkeypatch.setattr(insights, "get_graph_api_client", Client)
    result = asyncio.run(insights.summarize_async_insights_report(report_run_id="r"))
    assert result["items"] == [{"spend": None}]
    with pytest.raises(ValidationError, match="additive metrics"):
        asyncio.run(insights.summarize_async_insights_report(report_run_id="r", metrics=["reach"]))


def test_aggregation_preserves_integers_and_money_beyond_float_precision(monkeypatch):
    class Client:
        async def get_async_report(self, *args, **kwargs):
            return {"status": {"async_status": "Job Completed"}, "rows": {"data": [
                {"spend": "100000000000000000000000000000000.01", "impressions": "900719925474099300000000000000000"},
                {"spend": "0.01", "impressions": "1"},
            ]}}

    monkeypatch.setattr(insights, "get_graph_api_client", Client)
    result = asyncio.run(insights.summarize_async_insights_report(
        report_run_id="r", metrics=["spend", "impressions"],
    ))
    assert result["items"] == [{"spend": "100000000000000000000000000000000.02", "impressions": 900719925474099300000000000000001}]
    assert json.loads(json.dumps(result))["items"] == result["items"]


def test_aggregation_deadline_preserves_resume_cursor(monkeypatch):
    class SlowClient(ReportClient):
        async def list_objects(self, *args, **kwargs):
            if self.page_calls:
                await asyncio.sleep(1)
            return await super().list_objects(*args, **kwargs)

    client = SlowClient(8)
    monkeypatch.setattr(insights, "get_graph_api_client", lambda: client)
    result = asyncio.run(insights.summarize_async_insights_report(
        report_run_id="r", page_size=5, timeout_seconds=0.01,
    ))
    assert result["summary"]["stop_reason"] == "timeout"
    assert result["summary"]["rows_scanned"] == 5
    assert result["next_page_arguments"]["after"] == "5"
