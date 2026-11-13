"""Creative tool tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import pytest

from meta_ads_mcp.tools import creatives


class FakeCreativeClient:
    """Fake creative client."""

    def __init__(self) -> None:
        self.get_calls: list[dict[str, object]] = []
        self.updated_payloads: list[tuple[str, dict[str, object]]] = []

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        return {"data": [{"id": "crt_1", "name": "Creative One"}]}

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        return {"id": "crt_created", "payload": data}

    async def preview_ad(self, **kwargs):
        return {"data": [{"body": "<html>preview</html>"}], "request": kwargs}

    async def upload_ad_image(self, account_id: str, **kwargs):
        return {"images": {"test.png": {"hash": "abc123"}}, "account_id": account_id}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.get_calls.append({"object_id": object_id, "fields": fields, "params": params})
        return {"id": object_id, "name": "Old", "title": "Old title", "body": "Old body", "status": "ACTIVE", "creative": {"id": "crt_previous"}}

    async def update_object(self, object_id: str, *, data):
        self.updated_payloads.append((object_id, data))
        return {"success": True}

    async def delete_object(self, object_id: str):
        return {"success": True}


def test_preview_ad_supports_existing_ad(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    result = asyncio.run(creatives.preview_ad(ad_id="ad_123"))
    assert result["item"]["request"]["ad_id"] == "ad_123"


def test_get_creative_reads_full_default_spec(monkeypatch) -> None:
    client = FakeCreativeClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    result = asyncio.run(creatives.get_creative(creative_id="crt_123"))

    assert result["item"]["id"] == "crt_123"
    assert client.get_calls == [
        {"object_id": "crt_123", "fields": creatives.CREATIVE_FIELDS, "params": None}
    ]
    assert "url_tags" in creatives.CREATIVE_FIELDS
    assert "degrees_of_freedom_spec" in creatives.CREATIVE_FIELDS


def test_get_creative_normalizes_and_validates_id(monkeypatch) -> None:
    client = FakeCreativeClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    asyncio.run(creatives.get_creative(creative_id=" crt_123 "))
    assert client.get_calls[0]["object_id"] == "crt_123"
    with pytest.raises(creatives.ValidationError, match="creative_id is required"):
        asyncio.run(creatives.get_creative(creative_id=" "))


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


def test_creative_params_cannot_supply_omitted_typed_fields(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())

    with pytest.raises(creatives.ValidationError, match="object_story_spec"):
        asyncio.run(
            creatives.create_ad_creative(
                account_id="123",
                name="Creative",
                params={"object_story_spec": {"instagram_actor_id": "bypass"}},
            )
        )
    with pytest.raises(creatives.ValidationError, match="cell_ids"):
        asyncio.run(
            creatives.setup_ab_test(
                owner_id="act_123",
                name="Study",
                params={"cell_ids": ["cell_1"]},
            )
        )
    with pytest.raises(creatives.ValidationError, match="object_story_spec"):
        asyncio.run(
            creatives.update_creative(
                creative_id="crt_123",
                params={"object_story_spec": {"instagram_actor_id": "bypass"}},
            )
        )


def test_replacement_creative_preserves_placement_rules_and_other_assets(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: FakeCreativeClient())
    original = {
        "images": [
            {"hash": "feed_image", "adlabels": [{"name": "feed"}]},
            {"hash": "old_portrait", "adlabels": [{"name": "story"}]},
        ],
        "bodies": [{"text": "Keep this body"}],
        "link_urls": [{"website_url": "https://example.com/product?ref=ad"}],
        "asset_customization_rules": [{
            "customization_spec": {"publisher_platforms": ["instagram"], "instagram_positions": ["story"]},
            "image_label": {"name": "story"},
            "priority": 1,
        }],
    }
    replacement = deepcopy(original)
    replacement["images"][1]["hash"] = "uploaded_portrait"
    story = {"page_id": "page_123", "link_data": {"link": "https://example.com/product"}}
    freedom = {"creative_features_spec": {"standard_enhancements": {"enroll_status": "OPT_OUT"}}}

    result = asyncio.run(creatives.create_ad_creative(
        account_id="123", name="Replacement", object_story_spec=story,
        asset_feed_spec=replacement, degrees_of_freedom_spec=freedom,
        params={"url_tags": "utm_source=meta"},
    ))

    payload = result["created"]["payload"]
    assert payload["asset_feed_spec"] == replacement
    assert payload["asset_feed_spec"]["asset_customization_rules"] == original["asset_customization_rules"]
    assert payload["asset_feed_spec"]["images"][0] == original["images"][0]
    assert original["images"][1]["hash"] == "old_portrait"
    assert payload["object_story_spec"] == story
    assert payload["degrees_of_freedom_spec"] == freedom
    assert payload["url_tags"] == "utm_source=meta"


def test_update_ad_creative_repoints_only_the_selected_ad(monkeypatch) -> None:
    client = FakeCreativeClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    result = asyncio.run(creatives.update_ad_creative(ad_id=" ad_123 ", creative_id=" crt_new "))

    assert client.get_calls == [{"object_id": "ad_123", "fields": ["id", "creative"], "params": None}]
    assert client.updated_payloads == [("ad_123", {"creative": {"creative_id": "crt_new"}})]
    assert result["previous"] == {"creative_id": "crt_previous"}
    assert result["current"] == {"creative_id": "crt_new"}


@pytest.mark.parametrize(("ad_id", "creative_id"), [(" ", "crt_new"), ("ad_123", " ")])
def test_update_ad_creative_validates_ids_before_fetch(monkeypatch, ad_id, creative_id) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("must not fetch"))

    with pytest.raises(creatives.ValidationError, match="ad_id and creative_id are required"):
        asyncio.run(creatives.update_ad_creative(ad_id=ad_id, creative_id=creative_id))


class FakeBoostClient:
    """Records creative and ad creation for boost_instagram_post."""

    def __init__(self, *, promoted_object=None, fail_ad: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.promoted_object = promoted_object
        self.fail_ad = fail_ad

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields))
        return {"id": object_id, "promoted_object": self.promoted_object}

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.calls.append(("create", parent_id, edge, data))
        if edge == "adcreatives":
            return {"id": "crt_boost"}
        if self.fail_ad is not None:
            raise self.fail_ad
        return {"id": "ad_boost"}


def test_boost_instagram_post_creates_paused_ad(monkeypatch) -> None:
    client = FakeBoostClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    cta = {"type": "LEARN_MORE", "value": {"link": "https://example.com"}}

    result = asyncio.run(creatives.boost_instagram_post(
        account_id="123", adset_id="adset_1", instagram_media_id="media_1",
        instagram_account_id="ig_1", page_id="page_1", name="Boost post", call_to_action=cta,
    ))

    creative_call, ad_call = client.calls
    assert creative_call[1:3] == ("act_123", "adcreatives")
    assert creative_call[3] == {
        "name": "Boost post creative",
        "object_id": "page_1",
        "instagram_user_id": "ig_1",
        "source_instagram_media_id": "media_1",
        "call_to_action": cta,
    }
    assert ad_call[1:3] == ("act_123", "ads")
    assert ad_call[3] == {
        "name": "Boost post",
        "adset_id": "adset_1",
        "creative": {"creative_id": "crt_boost"},
        "status": "PAUSED",
    }
    assert result["creative_id"] == "crt_boost"
    assert result["ad_id"] == "ad_boost"
    assert result["status"] == "PAUSED"
    assert result["validation_only"] is False
    assert result["action"] == "boost_instagram_post"


def test_boost_instagram_post_reads_page_from_adset_promoted_object(monkeypatch) -> None:
    client = FakeBoostClient(promoted_object={"page_id": "page_9"})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    asyncio.run(creatives.boost_instagram_post(
        account_id="123", adset_id="adset_1", instagram_media_id="media_1", instagram_account_id="ig_1",
    ))
    assert client.calls[0] == ("get", "adset_1", ["id", "promoted_object"])
    assert client.calls[1][3]["object_id"] == "page_9"
    assert client.calls[2][3]["name"] == "Instagram post media_1"


def test_boost_instagram_post_without_page_raises_before_creating(monkeypatch) -> None:
    client = FakeBoostClient(promoted_object={"pixel_id": "px"})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(creatives.ValidationError, match="page_id"):
        asyncio.run(creatives.boost_instagram_post(
            account_id="123", adset_id="adset_1", instagram_media_id="media_1", instagram_account_id="ig_1",
        ))
    assert [call[0] for call in client.calls] == ["get"]


def test_boost_instagram_post_validate_only_creates_nothing(monkeypatch) -> None:
    client = FakeBoostClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.boost_instagram_post(
        account_id="123", adset_id="adset_1", instagram_media_id="media_1",
        instagram_account_id="ig_1", page_id="page_1", validate_only=True,
    ))
    assert len(client.calls) == 1
    _, parent, edge, data = client.calls[0]
    assert (parent, edge) == ("act_123", "ads")
    assert data["execution_options"] == ["validate_only"]
    assert data["status"] == "PAUSED"
    assert data["creative"] == {
        "object_id": "page_1", "instagram_user_id": "ig_1", "source_instagram_media_id": "media_1",
    }
    assert result["validation_only"] is True
    assert result["creative_id"] is None and result["ad_id"] is None


def test_boost_instagram_post_ad_failure_names_the_created_creative(monkeypatch) -> None:
    from meta_ads_mcp.errors import MetaApiError

    client = FakeBoostClient(fail_ad=MetaApiError(message="Invalid ad set", status_code=400, code=100))
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError, match="crt_boost"):
        asyncio.run(creatives.boost_instagram_post(
            account_id="123", adset_id="adset_1", instagram_media_id="media_1",
            instagram_account_id="ig_1", page_id="page_1",
        ))


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"account_id": " "}, "account_id"),
        ({"adset_id": ""}, "adset_id"),
        ({"instagram_media_id": " "}, "instagram_media_id"),
        ({"instagram_account_id": ""}, "instagram_account_id"),
        ({"call_to_action": {"value": {"link": "https://x"}}}, "call_to_action"),
        ({"call_to_action": {"type": "LEARN_MORE", "instagram_actor_id": "1"}}, "instagram_actor_id"),
    ],
)
def test_boost_instagram_post_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    base = {
        "account_id": "123", "adset_id": "adset_1", "instagram_media_id": "media_1",
        "instagram_account_id": "ig_1", "page_id": "page_1",
    }
    with pytest.raises(creatives.ValidationError, match=message):
        asyncio.run(creatives.boost_instagram_post(**{**base, **arguments}))


# Phase two: old-style calls to master tools send exactly what they sent before.


class RecordingCreativeClient(FakeCreativeClient):
    """Records every request in order, including previews and edge writes."""

    def __init__(self, *, adlabels=None, fail_formats=()) -> None:
        super().__init__()
        self.requests: list[tuple] = []
        self.adlabels = adlabels
        self.fail_formats = set(fail_formats)

    async def preview_ad(self, **kwargs):
        self.requests.append(("preview", kwargs))
        if kwargs.get("ad_format") in self.fail_formats:
            from meta_ads_mcp.errors import MetaApiError

            raise MetaApiError(message="Unsupported ad format", status_code=400, code=100)
        return {"data": [{"body": f"<html>{kwargs.get('ad_format')}</html>"}]}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.requests.append(("get", object_id, list(fields or []), params))
        payload = await super().get_object(object_id, fields=fields, params=params)
        if self.adlabels is not None:
            payload["adlabels"] = self.adlabels
        return payload

    async def update_object(self, object_id: str, *, data):
        self.requests.append(("update", object_id, dict(data)))
        return await super().update_object(object_id, data=data)

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.requests.append(("create", parent_id, edge, dict(data)))
        return {"success": True}


def test_preview_ad_single_format_call_is_unchanged(monkeypatch) -> None:
    client = RecordingCreativeClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    by_ad = asyncio.run(creatives.preview_ad(ad_id="ad_1"))
    by_creative = asyncio.run(creatives.preview_ad(account_id="123", creative_id="crt_1", ad_format="MOBILE_FEED_STANDARD"))

    assert client.requests == [
        ("preview", {"ad_id": "ad_1", "account_id": None, "creative_id": None, "creative": None, "ad_format": "DESKTOP_FEED_STANDARD"}),
        ("preview", {"ad_id": None, "account_id": "act_123", "creative_id": "crt_1", "creative": None, "ad_format": "MOBILE_FEED_STANDARD"}),
    ]
    assert by_ad == {"item": {"data": [{"body": "<html>DESKTOP_FEED_STANDARD</html>"}]}, "summary": {"count": 1}}
    assert by_creative["item"]["data"][0]["body"] == "<html>MOBILE_FEED_STANDARD</html>"


def test_update_creative_without_labels_is_unchanged(monkeypatch) -> None:
    client = RecordingCreativeClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    result = asyncio.run(creatives.update_creative(
        creative_id="crt_1", name="New", status="ACTIVE", params={"adlabels": [{"name": "legacy"}]},
    ))

    assert client.requests == [
        ("get", "crt_1", creatives.CREATIVE_FIELDS, None),
        ("update", "crt_1", {"name": "New", "status": "ACTIVE", "adlabels": [{"name": "legacy"}]}),
    ]
    assert result == {
        "ok": True,
        "action": "update_creative",
        "target": {"creative_id": "crt_1"},
        "previous": {"name": "Old", "title": "Old title", "body": "Old body", "status": "ACTIVE"},
        "current": {"name": "New", "status": "ACTIVE", "adlabels": [{"name": "legacy"}]},
    }
