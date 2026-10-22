"""Discovery tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.errors import UnsupportedFeatureError
from meta_ads_mcp.tools import discovery


@pytest.mark.parametrize("fields", [["id", "name"], "id,name"])
def test_list_ads_accepts_lean_fields_without_currency_lookups(monkeypatch, fields) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    class LeanClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            assert fields == ["id", "name"]
            return {"data": [{"id": "ad1", "name": "Test"}],
                    "paging": {"next": "next", "cursors": {"after": "cursor"}}}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: LeanClient())
    result = asyncio.run(mcp_server.call_tool("call_tool", {
        "name": "list_ads", "arguments": {"account_id": "act_123", "fields": fields},
    })).structured_content
    assert result["items"] == [{"id": "ad1", "name": "Test"}]
    assert result["summary"]["complete"] is False
    assert result["summary"]["visibility"] == "meta_default"
    assert result["summary"]["effective_status_filter"] is None


def test_list_ads_discloses_explicit_status_selection(monkeypatch) -> None:
    class StatusClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            assert params["effective_status"] == ["ACTIVE"]
            return {"data": []}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: StatusClient())
    result = asyncio.run(discovery.list_ads(account_id="act_123", effective_status=["ACTIVE"]))
    assert result["summary"]["complete"] is True
    assert result["summary"]["effective_status_filter"] == ["ACTIVE"]
    assert result["summary"]["visibility"] == "explicit_status_filter"


@pytest.mark.parametrize("whole_term", [True, False])
def test_multi_name_search_pages_graph_filters_and_deduplicates_union(monkeypatch, whole_term) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    calls = []

    class NameClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            calls.append(dict(params))
            assert parent_id == "cmp_1"
            assert fields == ["id", "name"]
            assert params["effective_status"] == ["ACTIVE"]
            assert len(params["filtering"]) == 1
            assert params["filtering"][0]["field"] == "name"
            assert params["filtering"][0]["operator"] == "CONTAIN"
            if params["filtering"][0]["value"] == "grâce":
                assert "after" not in params  # Each OR term starts its own cursor chain.
                return {"data": [{"id": "3", "name": "grâce-Ada"}]}
            assert params["filtering"][0]["value"] == "ADA"
            if not params.get("after"):
                return {"data": [{"id": "1", "name": "VIDEO_ADA-v1"}, {"id": "2", "name": "Adam"}],
                        "paging": {"next": "next", "cursors": {"after": "page2"}}}
            assert params["after"] == "page2"
            return {"data": [{"id": "3", "name": "grâce-Ada"}]}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: NameClient())
    result = asyncio.run(mcp_server.call_tool("list_ads", {
        "campaign_id": "cmp_1", "fields": ["id", "name"], "effective_status": ["ACTIVE"],
        "name_contains_any": "ADA, grâce", "whole_term_match": whole_term,
    })).structured_content
    assert len(calls) == 3
    assert result["summary"]["scanned_count"] == 3
    assert result["summary"]["complete"] is True
    assert {item["id"] for item in result["items"]} == ({"1", "3"} if whole_term else {"1", "2", "3"})
    assert result["items"][-1]["matched_terms"] == ["ada", "grâce"]
    assert result["paging"] == {"before": None, "after": None, "next": None}


@pytest.mark.parametrize("arguments", [
    {"name_contains_any": []}, {"name_contains_any": [""]},
    {"name_contains_any": ["Ada"], "name_contains": "Ada"}, {"whole_term_match": True},
    {"name_contains_any": ["Ada"], "after": "page2"},
])
def test_multi_name_search_validates_before_client(monkeypatch, arguments) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(discovery.ValidationError):
        asyncio.run(discovery.list_ads(account_id="123", **arguments))


@pytest.mark.parametrize("failure", ["missing_cursor", "repeated_cursor", "missing_id"])
def test_multi_name_search_never_returns_a_partial_inventory(monkeypatch, failure) -> None:
    class IncompleteClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            return {"data": [{"name": "Ada"} if failure == "missing_id" else {"id": "1", "name": "Ada"}],
                    "paging": {"next": "next", "cursors": {"after": None if failure == "missing_cursor" else "same"}}}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: IncompleteClient())
    with pytest.raises(discovery.ValidationError, match="pagination|without an ad id"):
        asyncio.run(discovery.list_ads(account_id="123", name_contains_any=["Ada"]))


@pytest.mark.parametrize("count", [0, 1000, 1200])
def test_multi_name_search_has_no_thousand_ad_inventory_cap(monkeypatch, count) -> None:
    class CompleteClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            assert params["limit"] == 1000
            assert params["filtering"] == [{"field": "name", "operator": "CONTAIN", "value": "Ada"}]
            start = 1000 if params.get("after") else 0
            page = {"data": [{"id": str(index), "name": "Ada"} for index in range(start, min(start + 1000, count))]}
            if count > 1000 and not start:
                page["paging"] = {"next": "next", "cursors": {"after": "page2"}}
            return page

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: CompleteClient())
    result = asyncio.run(discovery.list_ads(account_id="123", name_contains_any=["Ada"], limit=5000))
    assert len(result["items"]) == count
    assert result["summary"]["complete"] is True
    assert result["summary"]["scanned_count"] == count


def test_multi_name_search_follows_empty_pages_with_advancing_cursor(monkeypatch) -> None:
    class EmptyPageClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            if "after" not in params:
                return {"data": [], "paging": {"next": "next", "cursors": {"after": "page2"}}}
            return {"data": [{"id": "1", "name": "Ada"}]}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: EmptyPageClient())
    result = asyncio.run(discovery.list_ads(account_id="123", name_contains_any=["Ada"]))
    assert [item["id"] for item in result["items"]] == ["1"]
    assert result["summary"]["complete"] is True


def test_custom_bid_fields_include_currency_dependency(monkeypatch) -> None:
    class BidClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            assert fields == ["id", "bid_amount", "account_id"]
            return {"data": [{"id": "ad1", "bid_amount": "150", "account_id": "123"}]}

        async def get_object(self, object_id, *, fields):
            assert object_id == "act_123"
            assert fields == ["currency"]
            return {"currency": "USD"}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: BidClient())
    result = asyncio.run(discovery.list_ads(adset_id="as1", fields="id,bid_amount"))
    assert result["items"] == [{"id": "ad1", "bid_amount": 1.5}]


@pytest.mark.parametrize(("name", "id_key"), [
    ("get_adset", "adset_id"), ("get_ad", "ad_id"), ("get_campaign", "campaign_id"),
    ("get_ad_account", "account_id"),
])
@pytest.mark.parametrize("fields", [["id", "name"], "id,name"])
def test_entity_getters_accept_lean_fields_through_wrapper(monkeypatch, name, id_key, fields) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    class LeanClient:
        async def get_object(self, object_id, *, fields):
            assert fields == ["id", "name"]
            return {"id": object_id, "name": "Selected"}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: LeanClient())
    result = asyncio.run(mcp_server.call_tool("call_tool", {
        "name": name, "arguments": {id_key: "123", "fields": fields},
    })).structured_content
    assert result["item"] == {"id": "act_123" if name == "get_ad_account" else "123", "name": "Selected"}


@pytest.mark.parametrize(("tool", "id_key", "money_field"), [
    (discovery.get_adset, "adset_id", "daily_budget"),
    (discovery.get_ad, "ad_id", "bid_amount"),
    (discovery.get_campaign, "campaign_id", "lifetime_budget"),
])
@pytest.mark.parametrize(("currency", "expected"), [("USD", 12.5), ("JPY", 1250)])
def test_selected_money_fields_keep_currency_conversion_without_dependency_columns(
    monkeypatch, tool, id_key, money_field, currency, expected,
) -> None:
    class MoneyClient:
        async def get_object(self, object_id, *, fields):
            if object_id == "act_456":
                assert fields == ["currency"]
                return {"currency": currency}
            assert fields == ["id", money_field, "account_id"]
            return {"id": "123", money_field: "1250", "account_id": "456"}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: MoneyClient())
    result = asyncio.run(tool(**{id_key: "123", "fields": ["id", money_field]}))
    assert result["item"] == {"id": "123", money_field: expected}


def test_selected_account_money_fields_use_hidden_currency_dependency(monkeypatch) -> None:
    class AccountClient:
        async def get_object(self, object_id, *, fields):
            assert object_id == "act_123"
            assert fields == ["id", "amount_spent", "balance", "currency"]
            return {"id": object_id, "amount_spent": "12345", "balance": "1250", "currency": "USD"}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: AccountClient())
    result = asyncio.run(discovery.get_ad_account("123", fields=["id", "amount_spent", "balance"]))
    assert result["item"] == {"id": "act_123", "amount_spent": 123.45, "balance": 12.5}


@pytest.fixture
def destination_creative():
    return {
        "link_url": "https://shop.example/product?color=blue&ref=ad",
        "object_url": "https://shop.example/product?color=blue&ref=ad",
        "image_url": "https://cdn.example/image.jpg",
        "url_tags": "utm_source=meta",
        "object_story_spec": {
            "link_data": {
                "link": "https://shop.example/link",
                "message": "Large copy " * 1000,
                "child_attachments": [{"link": "https://shop.example/carousel", "picture": "https://cdn.example/card.jpg"}],
                "call_to_action": {"value": {"link": "https://shop.example/cta"}},
            },
            "template_data": {"link": "https://shop.example/{{product.id}}"},
            "video_data": {"call_to_action": {"value": {"link": "https://shop.example/video"}}},
            "photo_data": {"url": "https://cdn.example/photo.jpg"},
        },
        "asset_feed_spec": {"link_urls": [
            {"website_url": "https://shop.example/asset", "deeplink_url": "myapp://product/1", "display_url": "display.example"},
            {"website_url": "https://shop.example/link"},
        ]},
    }


@pytest.mark.parametrize("tool_name", ["list_ads", "get_ad"])
def test_destination_urls_projection_is_compact_and_excludes_asset_urls(monkeypatch, destination_creative, tool_name) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    import json

    class DestinationClient:
        def _item(self, fields):
            assert "destination_urls" not in fields
            assert len([field for field in fields if field.startswith("creative{")]) == 1
            assert fields[:2] == ["id", "name"]
            return {"id": "ad1", "name": "Ada", "creative": destination_creative}

        async def list_objects(self, parent_id, edge, *, fields, params):
            assert params["filtering"] == [{"field": "name", "operator": "CONTAIN", "value": "Ada"}]
            assert params["effective_status"] == ["ACTIVE"]
            return {"data": [self._item(fields)]}

        async def get_object(self, object_id, *, fields):
            return self._item(fields)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: DestinationClient())
    arguments = {"fields": ["id", "name", "destination_urls"]}
    arguments.update({"account_id": "123", "name_contains_any": ["Ada"], "effective_status": ["ACTIVE"]}
                     if tool_name == "list_ads" else {"ad_id": "ad1"})
    result = asyncio.run(mcp_server.call_tool("call_tool", {
        "name": tool_name, "arguments": arguments,
    })).structured_content
    item = result["items"][0] if tool_name == "list_ads" else result["item"]
    assert item["destination_urls"] == [
        "https://shop.example/product?color=blue&ref=ad", "https://shop.example/link",
        "https://shop.example/cta", "https://shop.example/carousel",
        "https://shop.example/{{product.id}}", "https://shop.example/video",
        "https://shop.example/asset", "myapp://product/1",
    ]
    assert "creative" not in item
    assert len(json.dumps(result)) < 1200


def test_destination_projection_preserves_explicit_creative_selection(monkeypatch, destination_creative) -> None:
    class CreativeClient:
        async def get_object(self, object_id, *, fields):
            assert len(fields) == 2
            assert "name" in fields[1]
            assert "asset_feed_spec," in fields[1]  # Both images and links are fetched.
            assert fields[1].count("asset_feed_spec") == 1
            return {"id": object_id, "creative": destination_creative}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: CreativeClient())
    result = asyncio.run(discovery.get_ad(
        "ad1", fields="id,creative{name,asset_feed_spec{images}},destination_urls",
    ))
    assert result["item"]["creative"] == destination_creative
    assert "https://shop.example/asset" in result["item"]["destination_urls"]


def test_destination_urls_are_empty_when_meta_exposes_no_configured_links(monkeypatch) -> None:
    class NoLinkClient:
        async def get_object(self, object_id, *, fields):
            return {"id": object_id, "creative": {"id": "creative1"}}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: NoLinkClient())
    result = asyncio.run(discovery.get_ad("ad1", fields=["id", "destination_urls"]))
    assert result["item"] == {"id": "ad1", "destination_urls": []}


class FakeDiscoveryClient:
    """Simple fake API client for discovery tests."""

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        if edge == "campaigns":
            assert parent_id == "act_123"
            return {
                "data": [
                    {
                        "id": "cmp_1",
                        "account_id": "123",
                        "name": "Campaign One",
                        "daily_budget": "5000",
                    }
                ]
            }
        if parent_id == "me" and edge == "accounts":
            return {"data": [{"id": "page_1", "name": "Test Page"}]}
        if edge == "instagram_accounts":
            assert parent_id == "act_123"
            return {
                "data": [
                    {
                        "id": "ig_1",
                        "username": "test_brand",
                        "profile_picture_url": "https://example.com/direct-profile.jpg",
                    }
                ]
            }
        if edge == "adsets":
            return {
                "data": [
                    {
                        "id": "adset_1",
                        "account_id": "123",
                        "campaign_id": "cmp_1",
                        "daily_budget": "2500",
                        "bid_amount": "1250",
                    }
                ]
            }
        if edge == "ads":
            return {
                "data": [
                    {
                        "id": "ad_1",
                        "account_id": "123",
                        "name": "Ad One",
                        "bid_amount": "750",
                    }
                ]
            }
        raise AssertionError(f"Unexpected edge {edge}")

    async def get_object(self, object_id: str, *, fields=None, params=None):
        if object_id == "act_123":
            assert fields == ["currency"]
            return {"id": object_id, "currency": "USD"}
        if object_id == "cmp_1":
            assert "account_id" in fields
            assert "currency" not in fields
            return {
                "id": object_id,
                "account_id": "123",
                "daily_budget": "5000",
            }
        if object_id == "ad_1":
            assert "account_id" in fields
            assert "bid_amount" in fields
            return {
                "id": object_id,
                "account_id": "123",
                "bid_amount": "750",
                "creative": {"id": "crt_1"},
            }
        assert object_id == "adset_1"
        assert "account_id" in fields
        assert "currency" not in fields
        return {
            "id": object_id,
            "account_id": "123",
            "daily_budget": "2500",
            "bid_amount": "1250",
        }


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


@pytest.mark.parametrize(
    ("tool", "kwargs", "edge"),
    [
        (discovery.list_campaigns, {"account_id": "123"}, "campaigns"),
        (discovery.list_adsets, {"campaign_id": "cmp_1"}, "adsets"),
        (discovery.list_ads, {"adset_id": "adset_1"}, "ads"),
    ],
)
def test_entity_list_name_filter_uses_graph_native_filtering(
    monkeypatch,
    tool,
    kwargs,
    edge,
) -> None:
    calls: list[dict[str, object]] = []

    class CapturingClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, actual_edge: str, *, fields=None, params=None):
            calls.append({"parent_id": parent_id, "edge": actual_edge, "params": params})
            return await super().list_objects(parent_id, actual_edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: CapturingClient())
    asyncio.run(tool(name_contains="Experiment", **kwargs))
    assert calls[0]["edge"] == edge
    assert calls[0]["params"]["filtering"] == [
        {"field": "name", "operator": "CONTAIN", "value": "Experiment"}
    ]


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
    assert result["items"][0]["profile_picture_url"] == "https://example.com/direct-profile.jpg"
    assert result["items"][0]["profile_pic"] == "https://example.com/direct-profile.jpg"
    assert "profile_picture_url" in discovery.INSTAGRAM_ACCOUNT_FIELDS
    assert "profile_pic" not in discovery.INSTAGRAM_ACCOUNT_FIELDS


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
                                "profile_picture_url": "https://example.com/profile.jpg",
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
    assert result["items"][0]["profile_picture_url"] == "https://example.com/profile.jpg"
    assert result["items"][0]["profile_pic"] == "https://example.com/profile.jpg"


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


def test_list_instagram_accounts_treats_blank_account_as_default(monkeypatch) -> None:
    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_instagram_accounts(account_id=" "))
    assert result["summary"]["source"] == "instagram_accounts"
    assert result["items"][0]["username"] == "test_brand"


def test_list_campaigns_uses_default_account_id(monkeypatch) -> None:
    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_campaigns())
    assert result["summary"]["count"] == 1


def test_list_campaigns_treats_blank_account_as_default(monkeypatch) -> None:
    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_campaigns(account_id="  "))
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


def test_list_adsets_docstring_mentions_default_account() -> None:
    assert "default account" in (discovery.list_adsets.__doc__ or "")


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


def test_list_ads_treats_blank_scope_values_as_missing(monkeypatch) -> None:
    class CampaignScopedClient(FakeDiscoveryClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if edge == "ads":
                assert parent_id == "cmp_1"
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: CampaignScopedClient())
    result = asyncio.run(discovery.list_ads(account_id="  ", campaign_id="cmp_1"))
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
    assert result["items"][0]["bid_amount"] == 12.5
    assert result["items"][0]["currency"] == "USD"
    assert "account_id" in discovery.ADSET_FIELDS
    assert "currency" not in discovery.ADSET_FIELDS


def test_get_adset_normalizes_every_monetary_field(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())

    result = asyncio.run(discovery.get_adset(adset_id="adset_1"))

    assert result["item"]["daily_budget"] == 25.0
    assert result["item"]["bid_amount"] == 12.5


def test_get_campaign_resolves_account_currency(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.get_campaign(campaign_id="cmp_1"))
    assert result["item"]["daily_budget"] == 50.0
    assert result["item"]["account_id"] == "act_123"
    assert result["item"]["currency"] == "USD"


def test_ad_reads_include_and_normalize_bid_amount(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    listed = asyncio.run(discovery.list_ads(adset_id="adset_1"))
    fetched = asyncio.run(discovery.get_ad(ad_id="ad_1"))
    assert listed["items"][0]["bid_amount"] == 7.5
    assert fetched["item"]["bid_amount"] == 7.5
    assert "account_id" in discovery.AD_FIELDS
    assert "bid_amount" in discovery.AD_FIELDS


def test_discovery_normalizes_zero_decimal_monetary_fields() -> None:
    items = discovery._normalize_monetary_fields(
        [
            {
                "daily_budget": "5000",
                "lifetime_budget": "15000",
                "bid_amount": "1250",
                "currency": "JPY",
            }
        ]
    )

    assert items[0]["daily_budget"] == 5000.0
    assert items[0]["lifetime_budget"] == 15000.0
    assert items[0]["bid_amount"] == 1250.0
    assert "account_id" in discovery.CAMPAIGN_FIELDS
    assert "currency" not in discovery.CAMPAIGN_FIELDS


def test_list_adsets_treats_blank_account_as_missing(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())
    result = asyncio.run(discovery.list_adsets(account_id=" ", campaign_id="cmp_1"))
    assert result["summary"]["count"] == 1


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
                            "account_id": "123",
                            "campaign_id": "cmp_1",
                            "daily_budget": "2500",
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
