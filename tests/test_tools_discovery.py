"""Discovery tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import discovery


class FakeDiscoveryClient:
    """Simple fake API client for discovery tests."""

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        assert parent_id == "act_123"
        assert edge == "campaigns"
        return {
            "data": [
                {
                    "id": "cmp_1",
                    "name": "Campaign One",
                    "daily_budget": "5000",
                    "currency": "USD",
                }
            ]
        }


def test_list_campaigns_uses_account_scope(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_campaigns(account_id="123"))
    assert result["summary"]["count"] == 1
    assert result["items"][0]["daily_budget"] == 50.0
