"""Discovery tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import discovery


class FakeDiscoveryClient:
    """Simple fake API client for discovery tests."""

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        if edge == "campaigns":
            assert parent_id == "act_123"
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
        if edge == "assigned_pages":
            return {"data": []}
        if edge == "client_pages":
            return {"data": [{"id": "page_1", "name": "Test Page"}]}
        if edge == "instagram_accounts":
            assert parent_id == "act_123"
            return {"data": [{"id": "ig_1", "username": "test_brand"}]}
        raise AssertionError(f"Unexpected edge {edge}")


def test_list_campaigns_uses_account_scope(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_campaigns(account_id="123"))
    assert result["summary"]["count"] == 1
    assert result["items"][0]["daily_budget"] == 50.0


def test_get_account_pages_falls_back_to_client_pages(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.get_account_pages(account_id="123"))
    assert result["summary"]["count"] == 1
    assert result["summary"]["source"] == "client_pages"
    assert result["summary"]["source_attempts"] == ["assigned_pages", "client_pages"]


def test_list_instagram_accounts_uses_ad_account_scope(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_instagram_accounts(account_id="123"))
    assert result["summary"]["count"] == 1
    assert result["summary"]["source"] == "instagram_accounts"
    assert result["items"][0]["username"] == "test_brand"
