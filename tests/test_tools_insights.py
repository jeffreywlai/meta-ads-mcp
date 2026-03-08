"""Insights tool tests."""

from __future__ import annotations

import asyncio

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
