"""Insights tool tests."""

from __future__ import annotations

import asyncio
import json

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

    assert json.loads(json_result["data"])[0]["campaign_id"] == "cmp_1"
    assert csv_result["mime_type"] == "text/csv"
    assert "campaign_id" in csv_result["data"]
