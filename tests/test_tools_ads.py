"""Ad tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.tools import ads


class FakeAdsClient:
    """Fake ad client."""

    def __init__(self, *, currency: str = "USD") -> None:
        self.created_payload = None
        self.currency = currency

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.created_payload = {"parent_id": parent_id, "edge": edge, "data": data}
        return {"id": "ad_created", "payload": data}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        if object_id == "act_123":
            assert fields == ["currency"]
            return {"id": object_id, "currency": self.currency}
        if object_id == "ad_123":
            return {
                "id": "ad_123",
                "name": "Ad 123",
                "account_id": "act_123",
                "creative": {"id": "crt_123"},
            }
        return {
            "id": "crt_123",
            "name": "Creative 123",
            "image_hash": "hash_123",
            "thumbnail_url": "https://example.com/thumb.png",
            "object_story_spec": {"link_data": {"picture": "https://example.com/story.png"}},
            "asset_feed_spec": {"images": [{"hash": "hash_456", "url": "https://example.com/feed.png"}]},
        }

    async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
        assert account_id == "act_123"
        assert hashes == ["hash_123", "hash_456"]
        return {
            "data": [
                {"hash": "hash_123", "url": "https://cdn.example.com/hash_123.png"},
                {"hash": "hash_456", "permalink_url": "https://cdn.example.com/hash_456.png"},
            ]
        }


def test_create_ad_wraps_creative_id(monkeypatch) -> None:
    client = FakeAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        ads.create_ad(
            account_id="123",
            name="New Ad",
            adset_id="adset_123",
            creative_id="crt_123",
            bid_amount=12.34,
        )
    )
    assert result["created"]["id"] == "ad_created"
    assert client.created_payload["parent_id"] == "act_123"
    assert client.created_payload["data"]["creative"] == {"creative_id": "crt_123"}
    assert client.created_payload["data"]["bid_amount"] == 1234


def test_create_ad_encodes_exact_decimal_bid(monkeypatch) -> None:
    client = FakeAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    asyncio.run(
        ads.create_ad(
            account_id="123",
            name="New Ad",
            adset_id="adset_123",
            creative_id="crt_123",
            bid_amount=19.99,
        )
    )
    assert client.created_payload["data"]["bid_amount"] == 1999


def test_create_ad_encodes_zero_decimal_bid(monkeypatch) -> None:
    client = FakeAdsClient(currency="JPY")
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    asyncio.run(
        ads.create_ad(
            account_id="123",
            name="JPY Ad",
            adset_id="adset_123",
            creative_id="crt_123",
            bid_amount=1250.0,
        )
    )
    assert client.created_payload["data"]["bid_amount"] == 1250


@pytest.mark.parametrize("bid_amount", [0, -1, float("inf")])
def test_create_ad_rejects_invalid_bid_before_client_lookup(
    monkeypatch,
    bid_amount,
) -> None:
    monkeypatch.setattr(
        ads,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(ads.ValidationError, match="bid_amount"):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Invalid Bid",
                adset_id="adset_123",
                creative_id="crt_123",
                bid_amount=bid_amount,
            )
        )


def test_create_ad_accepts_nested_graph_creative_shape(monkeypatch) -> None:
    client = FakeAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        ads.create_ad(
            account_id="123",
            name="Nested creative",
            adset_id="adset_123",
            creative={"creative_id": "crt_123"},
            validate_only=True,
        )
    )
    assert client.created_payload["data"]["creative"] == {"creative_id": "crt_123"}
    assert client.created_payload["data"]["execution_options"] == ["validate_only"]
    assert result["validation_only"] is True


def test_create_ad_rejects_conflicting_creative_inputs(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    with pytest.raises(ads.ValidationError, match="must match"):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Conflict",
                adset_id="adset_123",
                creative_id="crt_1",
                creative={"creative_id": "crt_2"},
            )
        )


def test_create_ad_rejects_ignored_nested_creative_fields(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    with pytest.raises(ads.ValidationError, match="unexpected fields"):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Unsupported creative body",
                adset_id="adset_123",
                creative={"creative_id": "crt_1", "name": "would be ignored"},
            )
        )


@pytest.mark.parametrize(
    "params, expected_field",
    [
        ({"bid_amount": 25}, "bid_amount"),
        ({"execution_options": ["validate_only"]}, "execution_options"),
    ],
)
def test_create_ad_params_cannot_supply_omitted_managed_fields(
    monkeypatch,
    params,
    expected_field,
) -> None:
    monkeypatch.setattr(
        ads,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(ads.ValidationError, match=expected_field):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Ad",
                adset_id="adset_123",
                creative_id="crt_123",
                params=params,
            )
        )


def test_get_ad_image_resolves_candidates(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["creative_id"] == "crt_123"
    assert result["item"]["image_hashes"] == ["hash_123", "hash_456"]
    assert result["item"]["best_image_url"] == "https://example.com/thumb.png"
    assert result["summary"]["resolved_image_count"] == 2


def test_image_rules_preserve_crops_defaults_and_ambiguous_assets() -> None:
    creative = {"asset_feed_spec": {
        "images": [
            {"hash": "portrait1", "adlabels": [{"name": "story"}], "image_crops": {"9x16": [[0, 0], [9, 16]]}},
            {"hash": "portrait2", "adlabels": [{"name": "story"}]},
            {"hash": "square", "adlabels": [{"name": "default"}]},
        ],
        "asset_customization_rules": [
            {"image_label": {"name": "story"}, "is_default": False, "priority": 1,
             "customization_spec": {"instagram_positions": ["story", "stream", "future_position"], "age_min": 25}},
            {"image_label": {"name": "default"}, "is_default": True},
            {"image_label": {"name": "missing"}},
            {"video_label": {"name": "video"}},
        ],
    }}
    rules = ads._configured_image_rules(creative, [{"hash": "portrait1", "url": "https://example.com/portrait", "original_width": 900}])
    assert [image["hash"] for image in rules[0]["images"]] == ["portrait1", "portrait2"]
    assert rules[0]["images"][0]["url"] == "https://example.com/portrait"
    assert rules[0]["images"][0]["image_crops"] == {"9x16": [[0, 0], [9, 16]]}
    assert rules[0]["customization_spec"]["age_min"] == 25
    assert rules[0]["is_default"] is False
    assert [position["platform_position"] for position in rules[0]["configured_reporting_positions"]] == ["story", "feed", "future_position"]
    assert rules[1]["is_default"] is True
    assert rules[2]["images"] == []
    assert rules[2]["mapping_status"] == "unresolved_image_label"
    assert rules[2]["is_default"] is None  # Do not infer a default from absent rules.
    assert rules[3]["mapping_status"] == "no_image_label"


def test_image_rules_are_exposed_without_claiming_actual_delivery(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["configured_image_rules"] == []
    assert result["item"]["placement_delivery_verified"] is False
    assert "not actual delivery" in result["summary"]["placement_note"]


def test_get_ad_image_handles_ad_without_creative(monkeypatch) -> None:
    class NoCreativeClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "ad_123":
                return {"id": "ad_123", "name": "Ad 123", "account_id": "act_123"}
            return await super().get_object(object_id, fields=fields, params=params)

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: NoCreativeClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["creative_id"] is None
    assert result["summary"]["resolved_image_count"] == 0


def test_get_ad_image_skips_resolution_without_account_id(monkeypatch) -> None:
    class NoAccountClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            payload = await super().get_object(object_id, fields=fields, params=params)
            if object_id == "ad_123":
                payload.pop("account_id", None)
            return payload

        async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
            raise AssertionError("should not resolve hashes without account_id")

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: NoAccountClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["account_id"] is None
    assert result["summary"]["resolved_image_count"] == 0


def test_get_ad_image_handles_no_hashes(monkeypatch) -> None:
    class NoHashClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "crt_123":
                return {"id": "crt_123", "name": "Creative 123", "thumbnail_url": "https://example.com/thumb.png"}
            return await super().get_object(object_id, fields=fields, params=params)

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: NoHashClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["image_hashes"] == []
    assert result["item"]["best_image_url"] == "https://example.com/thumb.png"


def test_get_ad_image_deduplicates_duplicate_urls(monkeypatch) -> None:
    class DuplicateUrlClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "crt_123":
                return {
                    "id": "crt_123",
                    "name": "Creative 123",
                    "image_hash": "hash_123",
                    "thumbnail_url": "https://example.com/dup.png",
                    "image_url": "https://example.com/dup.png",
                }
            return await super().get_object(object_id, fields=fields, params=params)

        async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
            return {"data": [{"hash": "hash_123", "url": "https://example.com/dup.png"}]}

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: DuplicateUrlClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert len(result["item"]["image_candidates"]) == 1


def test_get_ad_image_supports_asset_feed_only_images(monkeypatch) -> None:
    class AssetFeedOnlyClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "crt_123":
                return {
                    "id": "crt_123",
                    "name": "Creative 123",
                    "asset_feed_spec": {"images": [{"hash": "hash_999", "url": "https://example.com/feed-only.png"}]},
                }
            return await super().get_object(object_id, fields=fields, params=params)

        async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
            assert hashes == ["hash_999"]
            return {"data": []}

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: AssetFeedOnlyClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["best_image_url"] == "https://example.com/feed-only.png"
