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


# preview_ad ad_formats


def test_preview_ad_formats_previews_each_placement_and_reports_refusals(monkeypatch) -> None:
    client = RecordingCreativeClient(fail_formats={"INSTAGRAM_REELS"})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    result = asyncio.run(creatives.preview_ad(
        ad_id="ad_1", ad_formats="mobile_feed_standard, INSTAGRAM_STORY, INSTAGRAM_REELS, MOBILE_FEED_STANDARD",
    ))

    assert [request[1]["ad_format"] for request in client.requests] == ["MOBILE_FEED_STANDARD", "INSTAGRAM_STORY", "INSTAGRAM_REELS"]
    assert all(request[1]["ad_id"] == "ad_1" for request in client.requests)
    assert [item["ad_format"] for item in result["items"]] == ["MOBILE_FEED_STANDARD", "INSTAGRAM_STORY"]
    assert result["failed"] == [{"ad_format": "INSTAGRAM_REELS", "error": "Unsupported ad format", "code": 100}]
    assert result["summary"]["complete"] is False
    assert result["summary"]["failed_count"] == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"ad_id": "ad_1", "ad_formats": [" "]}, "nonblank"),
        ({"ad_id": "ad_1", "ad_formats": ["A"], "ad_format": "MOBILE_FEED_STANDARD"}, "not both"),
        ({"ad_id": "ad_1", "ad_formats": [f"F{index}" for index in range(11)]}, "at most 10"),
    ],
)
def test_preview_ad_formats_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(creatives.ValidationError, match=message):
        asyncio.run(creatives.preview_ad(**arguments))


# update_creative ad labels


def test_update_creative_labels_set_replaces_all(monkeypatch) -> None:
    client = RecordingCreativeClient(adlabels=[{"id": "1", "name": "old"}])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.update_creative(creative_id="crt_1", adlabels=[{"name": "spring"}]))
    assert client.requests == [
        ("get", "crt_1", [*creatives.CREATIVE_FIELDS, "adlabels{id,name}"], None),
        ("update", "crt_1", {"adlabels": [{"name": "spring"}]}),
    ]
    assert result["previous"]["adlabels"] == [{"id": "1", "name": "old"}]
    assert result["current"]["adlabels"] == [{"name": "spring"}]
    assert result["adlabels_operation"] == "set"


def test_update_creative_labels_add_uses_the_adlabels_edge(monkeypatch) -> None:
    client = RecordingCreativeClient(adlabels=[{"id": "1", "name": "old"}])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.update_creative(
        creative_id="crt_1", name="Renamed", adlabels=[{"id": "2"}], adlabels_operation="add",
    ))
    assert client.requests[1:] == [
        ("update", "crt_1", {"name": "Renamed"}),
        ("create", "crt_1", "adlabels", {"adlabels": [{"id": "2"}]}),
    ]
    assert result["current"]["adlabels"] == [{"id": "1", "name": "old"}, {"id": "2"}]


def test_update_creative_labels_add_only_sends_no_object_update(monkeypatch) -> None:
    client = RecordingCreativeClient(adlabels=[])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    asyncio.run(creatives.update_creative(creative_id="crt_1", adlabels=[{"name": "new"}], adlabels_operation="add"))
    assert [request[0] for request in client.requests] == ["get", "create"]


def test_update_creative_labels_remove_sets_the_remaining_labels(monkeypatch) -> None:
    client = RecordingCreativeClient(adlabels={"data": [{"id": "1", "name": "keep"}, {"id": "2", "name": "drop"}]})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.update_creative(creative_id="crt_1", adlabels=[{"name": "drop"}], adlabels_operation="remove"))
    assert client.requests[1] == ("update", "crt_1", {"adlabels": [{"id": "1"}]})
    assert result["current"]["adlabels"] == [{"id": "1"}]


def test_update_creative_labels_remove_unknown_label_writes_nothing(monkeypatch) -> None:
    client = RecordingCreativeClient(adlabels=[{"id": "1", "name": "keep"}])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(creatives.ValidationError, match="not on creative"):
        asyncio.run(creatives.update_creative(creative_id="crt_1", adlabels=[{"name": "missing"}], adlabels_operation="remove"))
    assert [request[0] for request in client.requests] == ["get"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"adlabels": [{"name": "a"}], "adlabels_operation": "merge"}, "adlabels_operation"),
        ({"adlabels_operation": "add"}, "needs adlabels"),
        ({"adlabels": [{"name": "a", "id": "1"}]}, "exactly one"),
        ({"adlabels": [{"id": "abc"}]}, "numeric"),
        ({"adlabels": [], "adlabels_operation": "remove"}, "at least one"),
        ({"adlabels": [{"name": "a"}], "params": {"adlabels": [{"name": "b"}]}}, "adlabels"),
        ({"adlabels": [{"name": "a"}], "adlabels_operation": "add", "params": {"adlabels": [{"name": "b"}]}}, "adlabels"),
    ],
)
def test_update_creative_label_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(creatives.ValidationError, match=message):
        asyncio.run(creatives.update_creative(creative_id="crt_1", **arguments))


# upload_ad_video


class FakeVideoClient:
    def __init__(self, status=None, fail_status: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.status = status if status is not None else {"video_status": "processing"}
        self.fail_status = fail_status

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, dict(data), files))
        return {"id": "777"}

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields))
        if self.fail_status is not None:
            raise self.fail_status
        return {"id": object_id, "status": self.status}


def test_upload_ad_video_from_url_returns_id_and_processing_status(monkeypatch) -> None:
    client = FakeVideoClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.upload_ad_video(account_id="123", video_url="https://cdn.example.com/v.mp4", title="Spring"))
    assert client.calls[0] == ("create", "act_123", "advideos", {"file_url": "https://cdn.example.com/v.mp4", "title": "Spring"}, None)
    assert client.calls[1] == ("get", "777", ["id", "status"])
    assert result["video_id"] == "777"
    assert result["processing_status"] == "processing"
    assert result["action"] == "upload_ad_video"


def test_upload_ad_video_from_small_file_sends_source(monkeypatch, tmp_path) -> None:
    client = FakeVideoClient(status={"video_status": "ready"})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video-bytes")
    result = asyncio.run(creatives.upload_ad_video(account_id="act_123", file_path=str(path), name="Clip"))
    _, _, _, data, files = client.calls[0]
    assert data == {"name": "Clip"}
    assert files == {"source": ("clip.mp4", b"video-bytes")}
    assert result["processing_status"] == "ready"


def test_upload_ad_video_dry_run_sends_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"12345")
    result = asyncio.run(creatives.upload_ad_video(account_id="123", file_path=str(path), dry_run=True))
    assert result["sent"] is False
    assert result["would_send"] == {"method": "POST", "path": "act_123/advideos", "data": {"source": {"file_name": "clip.mp4", "bytes": 5}}}


def test_upload_ad_video_status_read_failure_still_returns_the_id(monkeypatch) -> None:
    from meta_ads_mcp.errors import MetaApiError

    client = FakeVideoClient(fail_status=MetaApiError(message="Unsupported get request", status_code=400, code=100))
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.upload_ad_video(account_id="123", video_url="https://cdn.example.com/v.mp4"))
    assert result["video_id"] == "777"
    assert result["processing_status"] is None
    assert "could not be read" in result["status_note"]


def test_upload_ad_video_validation_makes_no_call(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    big = tmp_path / "big.mp4"
    with big.open("wb") as handle:
        handle.truncate(creatives.MAX_DIRECT_VIDEO_BYTES + 1)
    cases = [
        ({}, "exactly one"),
        ({"video_url": "https://x/v.mp4", "file_path": str(big)}, "exactly one"),
        ({"video_url": "ftp://x/v.mp4"}, "http"),
        ({"file_path": str(tmp_path / "missing.mp4")}, "does not point to a file"),
        ({"file_path": str(big)}, "chunked upload"),
        ({"video_url": "https://x/v.mp4", "title": "t" * 255}, "255"),
    ]
    for arguments, message in cases:
        with pytest.raises(creatives.ValidationError, match=message):
            asyncio.run(creatives.upload_ad_video(account_id="123", **arguments))


# delete_ad_image


class FakeImageClient:
    def __init__(self, images) -> None:
        self.images = images
        self.calls: list[tuple] = []

    async def get_ad_images_by_hashes(self, account_id, *, hashes, fields=None):
        self.calls.append(("list", account_id, "adimages", {"hashes": hashes}))
        return {"data": self.images}

    async def delete_edge_object(self, parent_id, edge, *, data):
        self.calls.append(("delete", parent_id, edge, data))
        return {"success": True}


def test_delete_ad_image_reads_first_and_returns_previous(monkeypatch) -> None:
    client = FakeImageClient([{"hash": "h1", "name": "Hero", "url": "https://x/h1.png"}])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.delete_ad_image(account_id="123", image_hash="h1"))
    assert client.calls == [
        ("list", "act_123", "adimages", {"hashes": ["h1"]}),
        ("delete", "act_123", "adimages", {"hash": "h1"}),
    ]
    assert result["previous"]["name"] == "Hero"
    assert result["current"] == {"hash": "h1", "in_account_library": False}


def test_delete_ad_image_not_found_deletes_nothing(monkeypatch) -> None:
    client = FakeImageClient([])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(creatives.ValidationError, match="nothing was deleted"):
        asyncio.run(creatives.delete_ad_image(account_id="123", image_hash="h1"))
    assert [call[0] for call in client.calls] == ["list"]


def test_delete_ad_image_dry_run_sends_nothing(monkeypatch) -> None:
    client = FakeImageClient([{"hash": "h1"}])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.delete_ad_image(account_id="123", image_hash="h1", dry_run=True))
    assert [call[0] for call in client.calls] == ["list"]
    assert result["sent"] is False
    assert result["would_send"] == {"method": "DELETE", "path": "act_123/adimages", "data": {"hash": "h1"}}


# boost_instagram_post one-step mode


class FakeOneStepClient:
    def __init__(self, fail_on: str | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_on = fail_on
        self.error = error

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields))
        return {"id": object_id, "currency": "USD"}

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, data))
        if edge == self.fail_on:
            from meta_ads_mcp.errors import MetaApiError

            raise self.error or MetaApiError(message="Invalid parameter", status_code=400, code=100)
        return {"id": {"campaigns": "1", "adsets": "2", "adcreatives": "3", "ads": "4"}[edge]}


ONE_STEP = {
    "account_id": "123",
    "instagram_media_id": "555",
    "instagram_account_id": "666",
    "page_id": "777",
    "daily_budget": 12.5,
    "targeting": {"geo_locations": {"countries": ["AU"]}},
}


def test_boost_one_step_creates_everything_paused(monkeypatch) -> None:
    client = FakeOneStepClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    result = asyncio.run(creatives.boost_instagram_post(**ONE_STEP, name="Spring post", url_tags="utm_source=ig"))

    creates = [call for call in client.calls if call[0] == "create"]
    assert [call[2] for call in creates] == ["campaigns", "adsets", "adcreatives", "ads"]
    assert all(call[3]["status"] == "PAUSED" for call in creates if call[2] != "adcreatives")
    campaign, adset, creative, ad = (call[3] for call in creates)
    assert campaign == {
        "name": "Spring post campaign", "objective": "OUTCOME_ENGAGEMENT", "status": "PAUSED",
        "special_ad_categories": [], "is_adset_budget_sharing_enabled": False,
    }
    assert adset["campaign_id"] == "1"
    assert adset["daily_budget"] == 1250
    assert adset["optimization_goal"] == "POST_ENGAGEMENT"
    assert adset["destination_type"] == "ON_POST"
    assert adset["billing_event"] == "IMPRESSIONS"
    assert adset["targeting"] == {"geo_locations": {"countries": ["AU"]}}
    assert "end_time" in adset
    assert creative == {
        "name": "Spring post creative", "object_id": "777", "instagram_user_id": "666",
        "source_instagram_media_id": "555", "url_tags": "utm_source=ig",
    }
    assert ad == {"name": "Spring post", "adset_id": "2", "creative": {"creative_id": "3"}, "status": "PAUSED"}
    assert (result["campaign_id"], result["adset_id"], result["creative_id"], result["ad_id"]) == ("1", "2", "3", "4")
    assert result["status"] == "PAUSED"
    assert result["defaults_applied"]["objective"] == "OUTCOME_ENGAGEMENT"
    assert result["defaults_applied"]["duration_days"] == 6
    assert result["daily_budget"] == {"major_units": 12.5, "sent_minor_units": 1250}


def test_boost_one_step_awareness_promotes_the_page(monkeypatch) -> None:
    client = FakeOneStepClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.boost_instagram_post(**ONE_STEP, objective="outcome_awareness", duration_days=3))
    adset = next(call[3] for call in client.calls if call[0] == "create" and call[2] == "adsets")
    assert adset["optimization_goal"] == "REACH"
    assert adset["promoted_object"] == {"page_id": "777"}
    assert "destination_type" not in adset
    assert "objective" not in result["defaults_applied"]


def test_boost_plan_only_sends_nothing(monkeypatch) -> None:
    client = FakeOneStepClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.boost_instagram_post(**ONE_STEP, plan_only=True))
    assert [call[0] for call in client.calls] == ["get"]
    assert result["sent"] is False
    assert [request["step"] for request in result["plan"]["requests"]] == ["campaign", "ad set", "creative", "ad"]
    assert result["plan"]["requests"][1]["data"]["daily_budget"] == 1250
    assert result["plan"]["defaults_applied"]["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"


def test_boost_one_step_failure_after_campaign_creation_names_what_was_created(monkeypatch) -> None:
    from meta_ads_mcp.errors import MetaApiError

    client = FakeOneStepClient(fail_on="adsets")
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(creatives.boost_instagram_post(**ONE_STEP))
    message = raised.value.message
    assert "Creating the ad set failed" in message
    assert "campaign 1 (PAUSED, remove with delete_campaign)" in message
    assert "Invalid parameter" in message
    assert [call[2] for call in client.calls if call[0] == "create"] == ["campaigns", "adsets"]


def test_boost_one_step_failure_at_the_ad_names_all_three_objects(monkeypatch) -> None:
    from meta_ads_mcp.errors import RateLimitError

    client = FakeOneStepClient(fail_on="ads", error=RateLimitError("Rate limited", retry_after_seconds=60))
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(RateLimitError) as raised:
        asyncio.run(creatives.boost_instagram_post(**ONE_STEP))
    message = raised.value.message
    for expected in ("campaign 1", "ad set 2", "creative 3", "delete_adset", "delete_creative", "all paused"):
        assert expected in message


def test_boost_partnership_inputs_reach_the_creative(monkeypatch) -> None:
    client = FakeBoostClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    asyncio.run(creatives.boost_instagram_post(
        account_id="123", adset_id="adset_1", instagram_media_id="media_1", instagram_account_id="ig_1",
        page_id="page_1", sponsor_page_id="222", sponsor_instagram_account_id="333", partnership_ad_format=1,
    ))
    creative = client.calls[0][3]
    assert creative["facebook_branded_content"] == {"sponsor_page_id": "222"}
    assert creative["instagram_branded_content"] == {"sponsor_id": "333"}
    assert creative["branded_content"] == {"ad_format": 1}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"daily_budget": None}, "daily_budget"),
        ({"targeting": None}, "targeting is required"),
        ({"objective": "OUTCOME_SALES"}, "objective must be one of"),
        ({"duration_days": 0}, "duration_days"),
        ({"duration_days": 366}, "duration_days"),
        ({"page_id": None}, "page_id is required"),
        ({"validate_only": True}, "plan_only"),
        ({"daily_budget": -1}, "greater than 0"),
        ({"partnership_ad_format": 2}, "sponsor"),
        ({"partnership_ad_format": 5, "sponsor_page_id": "1"}, "partnership_ad_format"),
    ],
)
def test_boost_one_step_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(creatives.ValidationError, match=message):
        asyncio.run(creatives.boost_instagram_post(**{**ONE_STEP, **arguments}))


def test_boost_with_adset_rejects_one_step_inputs(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(creatives.ValidationError, match="only apply in one-step mode"):
        asyncio.run(creatives.boost_instagram_post(**ONE_STEP, adset_id="adset_1"))


def test_upload_ad_video_only_reads_files_from_the_upload_folder(monkeypatch, tmp_path) -> None:
    """A path from the model must not be able to send some other file on this machine to Meta."""
    from meta_ads_mcp.config import reload_settings

    allowed = tmp_path / "uploads"
    allowed.mkdir()
    monkeypatch.setenv("META_UPLOAD_DIR", str(allowed))
    reload_settings()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    outside = tmp_path / "private-recording.mp4"
    outside.write_bytes(b"private")
    (allowed / "link.mp4").symlink_to(outside)
    (allowed / "notes.txt").write_bytes(b"not a video")
    for path, expected in (
        (str(outside), "inside the upload folder"),
        ("../private-recording.mp4", "inside the upload folder"),
        ("link.mp4", "inside the upload folder"),
        ("notes.txt", "must end in"),
    ):
        for dry_run in (False, True):
            with pytest.raises(creatives.ValidationError, match=expected) as error:
                asyncio.run(creatives.upload_ad_video(account_id="123", file_path=path, dry_run=dry_run))
            assert "private-recording" not in str(error.value)


def test_upload_ad_video_file_reading_is_off_without_an_upload_folder(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.config import reload_settings

    monkeypatch.delenv("META_UPLOAD_DIR", raising=False)
    reload_settings()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"video")
    with pytest.raises(creatives.ValidationError, match="META_UPLOAD_DIR"):
        asyncio.run(creatives.upload_ad_video(account_id="123", file_path=str(target)))
