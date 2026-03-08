"""Creative tool tests."""

from __future__ import annotations

import asyncio
import pytest

from meta_ads_mcp.tools import creatives


class FakeCreativeClient:
    """Fake creative client."""

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        return {"data": [{"id": "crt_1", "name": "Creative One"}]}

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        return {"id": "crt_created", "payload": data}

    async def preview_ad(self, **kwargs):
        return {"data": [{"body": "<html>preview</html>"}], "request": kwargs}

    async def upload_ad_image(self, account_id: str, **kwargs):
        return {"images": {"test.png": {"hash": "abc123"}}, "account_id": account_id}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        return {"id": object_id, "name": "Old", "title": "Old title", "body": "Old body", "status": "ACTIVE"}

    async def update_object(self, object_id: str, *, data):
        return {"success": True}

    async def delete_object(self, object_id: str):
        return {"success": True}


def test_preview_ad_supports_existing_ad(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    result = asyncio.run(creatives.preview_ad(ad_id="ad_123"))
    assert result["item"]["request"]["ad_id"] == "ad_123"


def test_upload_creative_asset_passes_account(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    result = asyncio.run(creatives.upload_creative_asset(account_id="123", image_url="https://example.com/x.png"))
    assert result["uploaded"]["account_id"] == "act_123"


def test_create_ad_creative_rejects_deprecated_instagram_actor_id() -> None:
    with pytest.raises(creatives.ValidationError):
        asyncio.run(
            creatives.create_ad_creative(
                account_id="123",
                name="Bad Creative",
                object_story_spec={"instagram_actor_id": "123"},
            )
        )
