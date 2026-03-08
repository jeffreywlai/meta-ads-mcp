"""Discovery tool tests."""

from __future__ import annotations

import asyncio
import pytest

from meta_ads_mcp.tools import discovery
from meta_ads_mcp.errors import UnsupportedFeatureError


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
        if edge == "adsets":
            return {"data": [{"id": "adset_1", "campaign_id": "cmp_1", "daily_budget": "2500", "currency": "USD"}]}
        if edge == "ads":
            return {"data": [{"id": "ad_1", "name": "Ad One"}]}
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


def test_list_campaigns_uses_default_account_id(monkeypatch) -> None:
    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_campaigns())
    assert result["summary"]["count"] == 1


def test_list_campaigns_requires_account_when_no_default(monkeypatch) -> None:
    monkeypatch.delenv("META_DEFAULT_ACCOUNT_ID", raising=False)
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    with pytest.raises(discovery.ValidationError):
        asyncio.run(discovery.list_campaigns())


def test_list_adsets_supports_campaign_scope(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_adsets(campaign_id="cmp_1"))
    assert result["summary"]["count"] == 1
    assert result["items"][0]["daily_budget"] == 25.0


def test_list_adsets_supports_account_scope(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_adsets(account_id="123"))
    assert result["summary"]["count"] == 1


def test_list_ads_requires_exactly_one_scope() -> None:
    with pytest.raises(discovery.ValidationError):
        asyncio.run(discovery.list_ads(account_id="123", campaign_id="cmp_1"))


def test_get_account_pages_supports_me_accounts_branch(monkeypatch) -> None:
    class MePagesClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if parent_id == "me" and edge == "accounts":
                return {"data": [{"id": "page_me", "name": "My Page"}], "paging": {"cursors": {"after": "after_1"}}}
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: MePagesClient())
    result = asyncio.run(discovery.get_account_pages())
    assert result["summary"]["source"] == "accounts"
    assert result["items"][0]["id"] == "page_me"
    assert result["paging"]["after"] == "after_1"


def test_get_account_pages_returns_empty_when_both_fallbacks_empty(monkeypatch) -> None:
    class EmptyPagesClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge in {"assigned_pages", "client_pages"}:
                return {"data": []}
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: EmptyPagesClient())
    result = asyncio.run(discovery.get_account_pages(account_id="123"))
    assert result["summary"]["count"] == 0
    assert result["summary"]["source_attempts"] == ["assigned_pages", "client_pages"]


def test_get_account_pages_raises_when_both_fallbacks_error(monkeypatch) -> None:
    class ErrorPagesClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge in {"assigned_pages", "client_pages"}:
                raise UnsupportedFeatureError(f"{edge} unsupported")
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: ErrorPagesClient())
    with pytest.raises(UnsupportedFeatureError):
        asyncio.run(discovery.get_account_pages(account_id="123"))


def test_list_instagram_accounts_handles_empty_result_and_paging(monkeypatch) -> None:
    class EmptyInstagramClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge == "instagram_accounts":
                return {"data": [], "paging": {"cursors": {"after": "after_ig"}}}
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: EmptyInstagramClient())
    result = asyncio.run(discovery.list_instagram_accounts(account_id="123"))
    assert result["summary"]["count"] == 0
    assert result["paging"]["after"] == "after_ig"
