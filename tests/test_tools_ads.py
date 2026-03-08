"""Ad tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import ads


class FakeAdsClient:
    """Fake ad client."""

    def __init__(self) -> None:
        self.created_payload = None

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.created_payload = {"parent_id": parent_id, "edge": edge, "data": data}
        return {"id": "ad_created", "payload": data}

    async def get_object(self, object_id: str, *, fields=None, params=None):
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


def test_get_ad_image_resolves_candidates(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["creative_id"] == "crt_123"
    assert result["item"]["image_hashes"] == ["hash_123", "hash_456"]
    assert result["item"]["best_image_url"] == "https://example.com/thumb.png"
    assert result["summary"]["resolved_image_count"] == 2
