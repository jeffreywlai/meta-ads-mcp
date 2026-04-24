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
        if parent_id == "me" and edge == "accounts":
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
    assert result["suggested_next_tools"]["campaign_health"] == {
        "tool": "get_campaign_optimization_snapshot",
        "arguments": {"campaign_id": "cmp_1"},
    }
    assert result["suggested_next_tools"]["whole_account_health"] == {
        "tool": "get_account_optimization_snapshot",
        "arguments": {"account_id": "act_123"},
    }
    assert result["suggested_next_tools"]["writes_catalog"] == {
        "tool": "list_mutation_tools",
        "arguments": {},
    }


def test_get_account_pages_uses_assigned_pages_for_account_scope(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.get_account_pages(account_id="123"))
    assert result["summary"]["count"] == 1
    assert result["summary"]["source"] == "accounts"
    assert result["summary"]["source_attempts"] == ["accounts"]
    assert result["summary"]["account_id_context"] == "act_123"


def test_list_instagram_accounts_uses_ad_account_scope(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_instagram_accounts(account_id="123"))
    assert result["summary"]["count"] == 1
    assert result["summary"]["source"] == "instagram_accounts"
    assert result["items"][0]["username"] == "test_brand"


def test_list_instagram_accounts_falls_back_to_page_instagram_accounts(monkeypatch) -> None:
    class FallbackInstagramClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge == "instagram_accounts":
                raise UnsupportedFeatureError("instagram_accounts unsupported")
            if parent_id == "me" and edge == "accounts":
                return {
                    "data": [
                        {
                            "id": "page_1",
                            "name": "Test Page",
                            "instagram_business_account": {
                                "id": "ig_2",
                                "username": "fallback_brand",
                                "name": "Fallback Brand",
                            },
                        }
                    ]
                }
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FallbackInstagramClient())
    result = asyncio.run(discovery.list_instagram_accounts(account_id="123"))
    assert result["summary"]["source"] == "accounts.instagram_business_account"
    assert result["summary"]["source_attempts"] == ["instagram_accounts", "accounts.instagram_business_account"]
    assert result["items"][0]["username"] == "fallback_brand"


def test_list_instagram_accounts_default_fallback_uses_me_pages(monkeypatch) -> None:
    class DefaultFallbackInstagramClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge == "instagram_accounts":
                assert parent_id == "act_123"
                raise UnsupportedFeatureError("instagram_accounts unsupported")
            if parent_id == "me" and edge == "accounts":
                return {
                    "data": [
                        {
                            "id": "page_1",
                            "name": "Test Page",
                            "instagram_business_account": {
                                "id": "ig_2",
                                "username": "fallback_brand",
                                "name": "Fallback Brand",
                            },
                        }
                    ]
                }
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: DefaultFallbackInstagramClient())
    result = asyncio.run(discovery.list_instagram_accounts())
    assert result["summary"]["source"] == "accounts.instagram_business_account"
    assert result["summary"]["source_attempts"] == ["instagram_accounts", "accounts.instagram_business_account"]
    assert "account_id_context" not in result["summary"]
    assert result["items"][0]["username"] == "fallback_brand"


def test_list_campaigns_uses_default_account_id(monkeypatch) -> None:
    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_campaigns())
    assert result["summary"]["count"] == 1


def test_list_adsets_uses_default_account_id(monkeypatch) -> None:
    class DefaultAccountClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge == "adsets":
                assert parent_id == "act_123"
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: DefaultAccountClient())
    result = asyncio.run(discovery.list_adsets())
    assert result["summary"]["count"] == 1


def test_list_ads_uses_default_account_id(monkeypatch) -> None:
    class DefaultAccountClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge == "ads":
                assert parent_id == "act_123"
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: DefaultAccountClient())
    result = asyncio.run(discovery.list_ads())
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


def test_list_adsets_rejects_multiple_scopes() -> None:
    with pytest.raises(discovery.ValidationError):
        asyncio.run(discovery.list_adsets(account_id="123", campaign_id="cmp_1"))


def test_list_adsets_includes_schedule_fields_when_present(monkeypatch) -> None:
    class ScheduledAdsetClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge == "adsets":
                return {
                    "data": [
                        {
                            "id": "adset_1",
                            "campaign_id": "cmp_1",
                            "daily_budget": "2500",
                            "currency": "USD",
                            "start_time": "2026-03-01T00:00:00+0000",
                            "end_time": "2026-03-31T00:00:00+0000",
                        }
                    ]
                }
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: ScheduledAdsetClient())
    result = asyncio.run(discovery.list_adsets(campaign_id="cmp_1"))
    assert result["items"][0]["start_time"] == "2026-03-01T00:00:00+0000"
    assert result["items"][0]["end_time"] == "2026-03-31T00:00:00+0000"


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
            if parent_id == "me" and edge == "accounts":
                return {"data": []}
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: EmptyPagesClient())
    result = asyncio.run(discovery.get_account_pages(account_id="123"))
    assert result["summary"]["count"] == 0
    assert result["summary"]["source_attempts"] == ["accounts"]
    assert result["summary"]["account_id_context"] == "act_123"


def test_get_account_pages_raises_when_accounts_lookup_errors(monkeypatch) -> None:
    class ErrorPagesClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if parent_id == "me" and edge == "accounts":
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
