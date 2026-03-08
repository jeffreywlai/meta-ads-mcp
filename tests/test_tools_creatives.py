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


def test_preview_ad_supports_creative_payload(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    result = asyncio.run(
        creatives.preview_ad(
            account_id="123",
            creative={"object_story_spec": {"link_data": {"message": "Hello"}}},
        )
    )
    assert result["item"]["request"]["account_id"] == "act_123"


def test_preview_ad_requires_account_for_creative_inputs() -> None:
    with pytest.raises(creatives.ValidationError):
        asyncio.run(creatives.preview_ad(creative_id="crt_123"))


def test_upload_creative_asset_passes_account(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    result = asyncio.run(creatives.upload_creative_asset(account_id="123", image_url="https://example.com/x.png"))
    assert result["uploaded"]["account_id"] == "act_123"


def test_upload_creative_asset_supports_file_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    file_path = tmp_path / "creative.png"
    file_path.write_bytes(b"png")
    result = asyncio.run(creatives.upload_creative_asset(account_id="123", file_path=str(file_path)))
    assert result["uploaded"]["account_id"] == "act_123"


def test_setup_ab_test_shapes_payload(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    result = asyncio.run(
        creatives.setup_ab_test(
            owner_id="act_123",
            name="Study",
            description="Test study",
            cell_ids=["cell_1", "cell_2"],
            confidence_level=0.9,
        )
    )
    assert result["created"]["payload"]["type"] == "SPLIT_TEST_V2"
    assert result["created"]["payload"]["cell_ids"] == ["cell_1", "cell_2"]


def test_update_creative_returns_previous_and_current(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    result = asyncio.run(
        creatives.update_creative(creative_id="crt_123", name="New", title="New title")
    )
    assert result["previous"]["name"] == "Old"
    assert result["current"]["name"] == "New"
    assert result["current"]["title"] == "New title"


def test_update_creative_requires_at_least_one_field(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    with pytest.raises(creatives.ValidationError):
        asyncio.run(creatives.update_creative(creative_id="crt_123"))


def test_create_ad_creative_rejects_deprecated_instagram_actor_id() -> None:
    with pytest.raises(creatives.ValidationError):
        asyncio.run(
            creatives.create_ad_creative(
                account_id="123",
                name="Bad Creative",
                object_story_spec={"instagram_actor_id": "123"},
            )
        )
