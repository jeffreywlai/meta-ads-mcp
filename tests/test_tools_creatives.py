"""Creative tool tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import pytest

from meta_ads_mcp.tools import creatives

# Written out, not imported, so a change to the production list fails these tests.
CREATIVE_FIELDS_LITERAL = ["id", "name", "title", "body", "status", "object_story_spec", "asset_feed_spec", "degrees_of_freedom_spec", "url_tags", "image_hash", "thumbnail_url"]


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
        {"object_id": "crt_123", "fields": CREATIVE_FIELDS_LITERAL, "params": None}
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


class _RecordedResponse:
    status_code = 200
    is_error = False
    headers = {"content-type": "application/json"}
    text = ""

    def json(self):
        return {"images": {"creative.png": {"hash": "abc123"}}}


def _record_graph_requests(monkeypatch) -> list[dict[str, object]]:
    """Run the real Graph client with the network replaced; return every request it sends."""
    from meta_ads_mcp import graph_api

    requests: list[dict[str, object]] = []

    class RecordingAsyncClient:
        is_closed = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def request(self, **kwargs):
            requests.append(kwargs)
            return _RecordedResponse()

        async def aclose(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(graph_api, "_CLIENT_POOL", {})
    monkeypatch.setattr(graph_api.httpx, "AsyncClient", RecordingAsyncClient)
    return requests


PNG_BYTES = b"\x89PNG\r\n\x1a\n-synthetic-image"


@pytest.mark.parametrize("name", [None, "Hero image"])
def test_upload_creative_asset_file_inside_folder_sends_the_same_request(monkeypatch, tmp_path, name) -> None:
    """The request Meta receives for a file in the upload folder is the one sent before the folder rule."""
    requests = _record_graph_requests(monkeypatch)
    image = tmp_path / "creative.png"
    image.write_bytes(PNG_BYTES)

    result = asyncio.run(creatives.upload_creative_asset(account_id="123", file_path=str(image), name=name))

    assert result["uploaded"] == {"images": {"creative.png": {"hash": "abc123"}}}
    assert len(requests) == 1
    sent = requests[0]
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/act_123/adimages")
    assert sent["files"] == {"filename": ("creative.png", PNG_BYTES)}
    assert sent["data"] == ({"name": name} if name else {})


def test_upload_creative_asset_image_url_sends_the_same_request(monkeypatch) -> None:
    requests = _record_graph_requests(monkeypatch)

    asyncio.run(
        creatives.upload_creative_asset(account_id="123", image_url="https://example.com/x.png", name="Hero")
    )

    assert len(requests) == 1
    sent = requests[0]
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/act_123/adimages")
    assert sent["files"] is None
    assert sent["data"] == {"url": "https://example.com/x.png", "name": "Hero"}


def _image_upload_folder(monkeypatch, tmp_path):
    """An upload folder inside tmp_path, with a private image next to it and a link to that image inside it."""
    from meta_ads_mcp.config import reload_settings

    folder = tmp_path / "uploads"
    folder.mkdir()
    monkeypatch.setenv("META_UPLOAD_DIR", str(folder))
    reload_settings()
    outside = tmp_path / "private-photo.png"
    outside.write_bytes(PNG_BYTES)
    (folder / "link.png").symlink_to(outside)
    return folder, outside


def test_upload_creative_asset_takes_a_relative_name_from_the_upload_folder(monkeypatch, tmp_path) -> None:
    folder, _ = _image_upload_folder(monkeypatch, tmp_path)
    (folder / "spring").mkdir()
    (folder / "spring" / "hero.JPG").write_bytes(b"synthetic-jpeg")
    requests = _record_graph_requests(monkeypatch)

    asyncio.run(creatives.upload_creative_asset(account_id="123", file_path="spring/hero.JPG"))

    assert [sent["files"] for sent in requests] == [{"filename": ("hero.JPG", b"synthetic-jpeg")}]


def test_upload_creative_asset_refuses_files_outside_the_rules_without_quoting_the_path(monkeypatch, tmp_path) -> None:
    """A path from the model must not be able to send some other file on this machine to Meta."""
    folder, outside = _image_upload_folder(monkeypatch, tmp_path)
    (folder / "notes.txt").write_bytes(b"not an image")
    (folder / "animation.gif").write_bytes(b"GIF89a")
    (folder / "empty.png").write_bytes(b"")
    (folder / "large.png").write_bytes(b"x" * 11)
    monkeypatch.setattr(creatives, "MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    for path, expected in (
        (str(outside), "inside the upload folder"),
        ("../private-photo.png", "inside the upload folder"),
        (str(folder / "link.png"), "inside the upload folder"),
        ("link.png", "inside the upload folder"),
        ("notes.txt", "must end in one of: .jpg, .jpeg, .png"),
        ("animation.gif", "must end in one of: .jpg, .jpeg, .png"),
        ("empty.png", "empty file"),
        ("large.png", "10-byte limit"),
        ("missing.png", "does not point to a file that can be read"),
    ):
        with pytest.raises(creatives.ValidationError, match=expected) as error:
            asyncio.run(creatives.upload_creative_asset(account_id="123", file_path=path))
        message = str(error.value)
        assert "private-photo" not in message
        assert str(tmp_path) not in message
        assert path not in message


def test_upload_creative_asset_names_meta_limit_when_a_file_is_too_large(monkeypatch, tmp_path) -> None:
    folder, _ = _image_upload_folder(monkeypatch, tmp_path)
    (folder / "large.png").write_bytes(b"x" * 11)
    monkeypatch.setattr(creatives, "MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(creatives.ValidationError, match="30 MB"):
        asyncio.run(creatives.upload_creative_asset(account_id="123", file_path="large.png"))


def test_upload_creative_asset_image_limit_is_meta_30_mb() -> None:
    assert creatives.MAX_IMAGE_BYTES == 30 * 1024 * 1024
    assert creatives.IMAGE_FILE_TYPES == {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def test_upload_creative_asset_file_reading_is_off_without_an_upload_folder(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.config import reload_settings

    monkeypatch.delenv("META_UPLOAD_DIR", raising=False)
    reload_settings()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    image = tmp_path / "creative.png"
    image.write_bytes(PNG_BYTES)
    for path in (str(image), "creative.png"):
        with pytest.raises(creatives.ValidationError, match="META_UPLOAD_DIR") as error:
            asyncio.run(creatives.upload_creative_asset(account_id="123", file_path=path))
        assert str(tmp_path) not in str(error.value)


def test_upload_creative_asset_image_url_needs_no_upload_folder(monkeypatch) -> None:
    from meta_ads_mcp.config import reload_settings

    monkeypatch.delenv("META_UPLOAD_DIR", raising=False)
    reload_settings()
    requests = _record_graph_requests(monkeypatch)
    asyncio.run(creatives.upload_creative_asset(account_id="123", image_url="https://example.com/x.png"))
    assert [sent["data"] for sent in requests] == [{"url": "https://example.com/x.png"}]


@pytest.mark.parametrize("routed", [False, True])
def test_upload_creative_asset_refusal_through_the_mcp_server_does_not_quote_the_path(monkeypatch, tmp_path, routed) -> None:
    from fastmcp.exceptions import ToolError

    import meta_ads_mcp.server  # noqa: F401  (registers every tool)
    from meta_ads_mcp.coordinator import mcp_server

    _, outside = _image_upload_folder(monkeypatch, tmp_path)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    name, arguments = "upload_creative_asset", {"account_id": "123", "file_path": str(outside)}
    if routed:
        name, arguments = "call_tool", {"name": name, "arguments": arguments}
    with pytest.raises(ToolError) as error:
        asyncio.run(mcp_server.call_tool(name, arguments))
    rendered = f"{error.value}\n{error.value!r}"
    assert "inside the upload folder" in rendered
    assert "private-photo" not in rendered
    assert str(tmp_path) not in rendered


def test_upload_creative_asset_is_blocked_in_read_only_mode(monkeypatch, tmp_path) -> None:
    from fastmcp.exceptions import ToolError

    import meta_ads_mcp.server  # noqa: F401  (registers every tool)
    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.coordinator import mcp_server

    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    image = tmp_path / "creative.png"
    image.write_bytes(PNG_BYTES)
    for arguments in ({"account_id": "123", "file_path": str(image)}, {"account_id": "123", "image_url": "https://example.com/x.png"}):
        with pytest.raises(ToolError, match="META_READ_ONLY"):
            asyncio.run(mcp_server.call_tool("upload_creative_asset", arguments))


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
        ("get", "crt_1", CREATIVE_FIELDS_LITERAL, None),
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
        ("get", "crt_1", [*CREATIVE_FIELDS_LITERAL, "adlabels.limit(100){id,name}"], None),
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
    monkeypatch.setattr(creatives, "MAX_CHUNKED_VIDEO_BYTES", creatives.MAX_DIRECT_VIDEO_BYTES)
    big = tmp_path / "big.mp4"
    with big.open("wb") as handle:
        handle.truncate(creatives.MAX_DIRECT_VIDEO_BYTES + 1)
    cases = [
        ({}, "exactly one"),
        ({"video_url": "https://x/v.mp4", "file_path": str(big)}, "exactly one"),
        ({"video_url": "ftp://x/v.mp4"}, "http"),
        ({"file_path": str(tmp_path / "missing.mp4")}, "does not point to a file"),
        ({"file_path": str(big)}, "byte limit for video uploads"),
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
        ({"objective": "OUTCOME_APP_PROMOTION"}, "objective must be one of"),
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


# Round-three review fixes (R2 findings G10, G17, G20).


class LaterFailureClient:
    """Creates succeed with id 900; the configured later step fails with the configured error."""

    def __init__(self, *, fail_step: str, error: Exception, objects=None) -> None:
        self.fail_step = fail_step
        self.error = error
        self.objects = objects or {}
        self.calls: list[tuple] = []

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields))
        if self.fail_step == "get":
            raise self.error
        return dict(self.objects.get(object_id, {"id": object_id}))

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, dict(data)))
        if self.fail_step == edge:
            raise self.error
        return {"id": "900"}

    async def update_object(self, object_id, *, data):
        self.calls.append(("update", object_id, dict(data)))
        if self.fail_step == "update":
            raise self.error
        return {"success": True}


def _later_error(kind: str) -> Exception:
    from meta_ads_mcp.errors import MetaApiError, NotFoundError, RateLimitError

    return {
        "rate_limit": lambda: RateLimitError("Synthetic limit", code=4),
        "transport": lambda: MetaApiError(
            message="Meta API request failed before a response was received.",
            is_transient=True, mutation_outcome_unknown=True,
        ),
        "meta": lambda: MetaApiError(message="Invalid parameter", status_code=400, code=100),
        "unexpected": lambda: NotFoundError("Meta object or edge not found"),
    }[kind]()


LATER_ERROR_KINDS = ["rate_limit", "transport", "meta", "unexpected"]


@pytest.mark.parametrize("kind", LATER_ERROR_KINDS)
def test_g10_boost_into_existing_adset_keeps_the_created_creative_id(monkeypatch, kind) -> None:
    from meta_ads_mcp.errors import RateLimitError

    client = LaterFailureClient(fail_step="ads", error=_later_error(kind))
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(Exception) as raised:
        asyncio.run(creatives.boost_instagram_post("1", "40", "50", adset_id="60", page_id="30"))
    message = str(raised.value)
    assert "Creative 900 was created" in message
    assert "delete_creative" in message
    if kind == "transport":
        assert "may have been created" in message
    if kind == "rate_limit":
        assert isinstance(raised.value, RateLimitError)


@pytest.mark.parametrize("kind", LATER_ERROR_KINDS)
def test_g10_video_status_read_failure_keeps_the_video_id(monkeypatch, kind) -> None:
    client = LaterFailureClient(fail_step="get", error=_later_error(kind))
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.upload_ad_video("1", video_url="https://example.invalid/video.mp4"))
    assert result["video_id"] == "900"
    assert result["processing_status"] is None
    assert "could not be read" in result["status_note"]


@pytest.mark.parametrize("tool", ["boost", "video"])
def test_g10_later_rate_limit_preserves_created_object_id(monkeypatch, tool) -> None:
    """Reviewer reproduction: a rate limit after the create must not hide id 900."""
    import json

    from meta_ads_mcp.errors import RateLimitError

    client = LaterFailureClient(
        fail_step="ads" if tool == "boost" else "get", error=RateLimitError("Synthetic limit", code=4),
    )
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    if tool == "boost":
        coro = creatives.boost_instagram_post("1", "40", "50", adset_id="60", page_id="30")
    else:
        coro = creatives.upload_ad_video("1", video_url="https://example.invalid/video.mp4")
    try:
        result = asyncio.run(coro)
    except RateLimitError as error:
        assert "900" in str(error), str(error)
    else:
        assert "900" in json.dumps(result), result


@pytest.mark.parametrize("kind", LATER_ERROR_KINDS)
def test_g10_label_add_failure_discloses_already_updated_fields(monkeypatch, kind) -> None:
    client = LaterFailureClient(
        fail_step="adlabels", error=_later_error(kind),
        objects={"10": {"id": "10", "name": "Old", "adlabels": []}},
    )
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(Exception) as raised:
        asyncio.run(creatives.update_creative("10", name="New", adlabels=[{"id": "20"}], adlabels_operation="add"))
    assert ("update", "10", {"name": "New"}) in client.calls
    message = str(raised.value)
    assert "already updated" in message
    assert "name changed" in message
    if kind == "transport":
        assert "may or may not have been added" in message
    else:
        assert "labels were not added" in message


def test_g10_label_add_rate_limit_discloses_already_updated_fields(monkeypatch) -> None:
    from meta_ads_mcp.errors import RateLimitError

    client = LaterFailureClient(
        fail_step="adlabels", error=RateLimitError("Synthetic limit", code=4),
        objects={"10": {"id": "10", "name": "Old", "adlabels": []}},
    )
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(RateLimitError) as info:
        asyncio.run(creatives.update_creative("10", name="New", adlabels=[{"id": "20"}], adlabels_operation="add"))
    assert ("update", "10", {"name": "New"}) in client.calls
    assert "updated" in str(info.value).lower(), str(info.value)


def test_g10_label_add_only_failure_does_not_claim_other_changes(monkeypatch) -> None:
    from meta_ads_mcp.errors import RateLimitError

    client = LaterFailureClient(
        fail_step="adlabels", error=RateLimitError("Synthetic limit", code=4),
        objects={"10": {"id": "10", "adlabels": []}},
    )
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(RateLimitError) as info:
        asyncio.run(creatives.update_creative("10", adlabels=[{"id": "20"}], adlabels_operation="add"))
    assert "already updated" not in str(info.value)
    assert [call[0] for call in client.calls] == ["get", "create"]


def test_preview_ad_formats_rate_limit_keeps_finished_previews(monkeypatch) -> None:
    from meta_ads_mcp.errors import RateLimitError

    class Limited(RecordingCreativeClient):
        async def preview_ad(self, **kwargs):
            if kwargs.get("ad_format") == "INSTAGRAM_STORY":
                raise RateLimitError("Synthetic limit", code=4)
            return await super().preview_ad(**kwargs)

    client = Limited()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.preview_ad(ad_id="ad_1", ad_formats="MOBILE_FEED_STANDARD,INSTAGRAM_STORY,INSTAGRAM_REELS"))
    assert [item["ad_format"] for item in result["items"]] == ["MOBILE_FEED_STANDARD"]
    assert [failure["ad_format"] for failure in result["failed"]] == ["INSTAGRAM_STORY", "INSTAGRAM_REELS"]
    assert result["failed"][1]["error"].startswith("Not requested")
    assert result["summary"]["complete"] is False
    assert [request[1]["ad_format"] for request in client.requests] == ["MOBILE_FEED_STANDARD"]


class PagedLabelClient:
    def __init__(self, adlabels) -> None:
        self.adlabels = adlabels
        self.calls: list[tuple] = []

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or [])))
        return {"id": object_id, "name": "Old", "adlabels": self.adlabels}

    async def update_object(self, object_id, *, data):
        self.calls.append(("update", object_id, dict(data)))
        return {"success": True}

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, dict(data)))
        return {"success": True}


UNREAD_LABEL_PAGE = {
    "data": [{"id": "20", "name": "remove-me"}],
    "paging": {"next": "https://graph.facebook.com/next", "cursors": {"after": "page-two"}},
}


def test_g17_label_removal_does_not_overwrite_unread_labels(monkeypatch) -> None:
    client = PagedLabelClient(UNREAD_LABEL_PAGE)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(creatives.ValidationError, match="more labels than"):
        asyncio.run(creatives.update_creative("10", adlabels=[{"id": "20"}], adlabels_operation="remove"))
    assert not any(call[0] in {"update", "create"} for call in client.calls), client.calls


def test_g17_label_removal_with_other_field_changes_writes_nothing(monkeypatch) -> None:
    client = PagedLabelClient({"data": [{"id": "20"}], "paging": {"next": "https://graph.facebook.com/next"}})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(creatives.ValidationError):
        asyncio.run(creatives.update_creative("10", name="New", adlabels=[{"id": "20"}], adlabels_operation="remove"))
    assert [call[0] for call in client.calls] == ["get"]


def test_g17_label_read_asks_for_a_full_page_of_labels(monkeypatch) -> None:
    client = PagedLabelClient({"data": [{"id": "1", "name": "keep"}, {"id": "2", "name": "drop"}], "paging": {"cursors": {"after": "x"}}})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.update_creative("10", adlabels=[{"id": "2"}], adlabels_operation="remove"))
    assert client.calls[0] == ("get", "10", [*CREATIVE_FIELDS_LITERAL, "adlabels.limit(100){id,name}"])
    assert client.calls[1] == ("update", "10", {"adlabels": [{"id": "1"}]})
    assert result["previous"]["adlabels"] == [{"id": "1", "name": "keep"}, {"id": "2", "name": "drop"}]
    assert result["adlabels_read_complete"] is True


@pytest.mark.parametrize("operation", ["add", "set"])
def test_g17_incomplete_label_read_is_reported_not_guessed(monkeypatch, operation) -> None:
    client = PagedLabelClient(UNREAD_LABEL_PAGE)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.update_creative("10", adlabels=[{"id": "30"}], adlabels_operation=operation))
    assert result["adlabels_read_complete"] is False
    assert "first" in result["adlabels_note"]
    if operation == "add":
        # The add edge only adds; nothing was replaced, but the full label set is unknown.
        assert client.calls[1] == ("create", "10", "adlabels", {"adlabels": [{"id": "30"}]})
        assert result["current"]["adlabels"] is None
    else:
        assert client.calls[1] == ("update", "10", {"adlabels": [{"id": "30"}]})
        assert result["current"]["adlabels"] == [{"id": "30"}]


def test_g20_video_url_with_login_is_refused_before_any_request(monkeypatch) -> None:
    secret = "synthetic-media-password"
    url = f"https://user:{secret}@example.invalid/asset.mp4"
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    for dry_run in (True, False):
        with pytest.raises(creatives.ValidationError) as raised:
            asyncio.run(creatives.upload_ad_video("10", video_url=url, dry_run=dry_run))
        assert secret not in str(raised.value)
        assert "username or password" in str(raised.value)


# boost_instagram_post: more objectives and Partnership Ad codes


WEBSITE_CTA = {"type": "LEARN_MORE", "value": {"link": "https://shop.example.com/spring"}}
PIXEL = {"pixel_id": "888", "custom_event_type": "PURCHASE"}


def _created(client, edge):
    return next(call[3] for call in client.calls if call[0] == "create" and call[2] == edge)


@pytest.mark.parametrize(
    ("arguments", "objective", "destination", "goal", "promoted"),
    [
        ({"objective": "OUTCOME_TRAFFIC"}, "OUTCOME_TRAFFIC", "INSTAGRAM_PROFILE", "VISIT_INSTAGRAM_PROFILE", None),
        ({"objective": "OUTCOME_TRAFFIC", "destination_type": "website", "call_to_action": WEBSITE_CTA},
         "OUTCOME_TRAFFIC", "WEBSITE", "LINK_CLICKS", None),
        ({"objective": "OUTCOME_ENGAGEMENT", "destination_type": "ON_VIDEO"}, "OUTCOME_ENGAGEMENT", "ON_VIDEO", "THRUPLAY", None),
        ({"objective": "OUTCOME_SALES", "promoted_object": PIXEL, "call_to_action": WEBSITE_CTA},
         "OUTCOME_SALES", "WEBSITE", "OFFSITE_CONVERSIONS", PIXEL),
        ({"objective": "OUTCOME_LEADS", "promoted_object": {"pixel_id": "888", "custom_event_type": "LEAD"}, "call_to_action": WEBSITE_CTA},
         "OUTCOME_LEADS", "WEBSITE", "OFFSITE_CONVERSIONS", {"pixel_id": "888", "custom_event_type": "LEAD"}),
    ],
)
def test_boost_one_step_objectives_set_the_matching_destination_and_goal(
    monkeypatch, arguments, objective, destination, goal, promoted,
) -> None:
    client = FakeOneStepClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.boost_instagram_post(**{**ONE_STEP, **arguments}))
    campaign, adset = _created(client, "campaigns"), _created(client, "adsets")
    assert campaign["objective"] == objective
    assert adset["destination_type"] == destination
    assert adset["optimization_goal"] == goal
    assert adset.get("promoted_object") == promoted
    assert adset["status"] == "PAUSED" and campaign["status"] == "PAUSED"
    assert result["defaults_applied"]["optimization_goal"] == goal
    if "destination_type" not in arguments:
        assert result["defaults_applied"]["destination_type"] == destination


def test_boost_one_step_default_objective_is_unchanged(monkeypatch) -> None:
    client = FakeOneStepClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    asyncio.run(creatives.boost_instagram_post(**ONE_STEP))
    adset = _created(client, "adsets")
    assert (_created(client, "campaigns")["objective"], adset["destination_type"], adset["optimization_goal"]) == (
        "OUTCOME_ENGAGEMENT", "ON_POST", "POST_ENGAGEMENT",
    )
    assert "promoted_object" not in adset


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"objective": "OUTCOME_TRAFFIC", "destination_type": "MESSENGER"}, "destination_type for OUTCOME_TRAFFIC must be one of"),
        ({"objective": "OUTCOME_AWARENESS", "destination_type": "WEBSITE"}, "OUTCOME_AWARENESS takes no destination_type"),
        ({"objective": "OUTCOME_TRAFFIC", "destination_type": "WEBSITE"}, "website link"),
        ({"objective": "OUTCOME_TRAFFIC", "destination_type": "WEBSITE", "call_to_action": {"type": "LEARN_MORE"}}, "website link"),
        ({"objective": "OUTCOME_SALES", "call_to_action": WEBSITE_CTA}, "pixel_id and custom_event_type"),
        ({"objective": "OUTCOME_SALES", "call_to_action": WEBSITE_CTA, "promoted_object": {"pixel_id": "abc", "custom_event_type": "PURCHASE"}}, "pixel_id"),
        ({"objective": "OUTCOME_ENGAGEMENT", "promoted_object": PIXEL}, "promoted_object applies to OUTCOME_SALES and OUTCOME_LEADS"),
        ({"objective": "OUTCOME_SALES", "call_to_action": WEBSITE_CTA, "promoted_object": {**PIXEL, "object_store_url": "https://u:p@x.example/app"}}, "username or password"),
    ],
)
def test_boost_one_step_objective_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(creatives.ValidationError, match=message):
        asyncio.run(creatives.boost_instagram_post(**{**ONE_STEP, **arguments}))


def test_boost_with_adset_rejects_destination_and_promoted_object(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    base = {key: value for key, value in ONE_STEP.items() if key not in {"daily_budget", "targeting"}}
    with pytest.raises(creatives.ValidationError, match="destination_type, promoted_object only apply in one-step mode"):
        asyncio.run(creatives.boost_instagram_post(**base, adset_id="60", destination_type="ON_POST", promoted_object=PIXEL))


def test_boost_with_a_partnership_ad_code_names_the_post_by_code(monkeypatch) -> None:
    client = FakeBoostClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.boost_instagram_post(
        account_id="123", instagram_account_id="ig_1", adset_id="adset_1", page_id="page_1",
        partnership_ad_code="AC-998877", partnership_ad_format=2,
    ))
    creative = client.calls[0][3]
    assert creative == {
        "name": "Partnership ad creative",
        "object_id": "page_1",
        "branded_content": {"instagram_boost_post_access_token": "AC-998877", "ad_format": 2},
        "facebook_branded_content": {"sponsor_page_id": "page_1"},
        "instagram_branded_content": {"sponsor_id": "ig_1"},
    }
    assert "AC-998877" not in str(result)
    assert result["target"]["partnership_ad_code"] == "<given, not shown>"
    assert result["partnership_defaults_applied"] == {
        "sponsor_page_id": "page_1 (page_id)", "sponsor_instagram_account_id": "ig_1 (instagram_account_id)",
    }


def test_boost_partnership_ad_code_uses_given_sponsors(monkeypatch) -> None:
    client = FakeBoostClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    asyncio.run(creatives.boost_instagram_post(
        account_id="123", instagram_account_id="ig_1", adset_id="adset_1", page_id="page_1",
        partnership_ad_code="AC-1", sponsor_page_id="222", sponsor_instagram_account_id="333",
    ))
    creative = client.calls[0][3]
    assert creative["facebook_branded_content"] == {"sponsor_page_id": "222"}
    assert creative["instagram_branded_content"] == {"sponsor_id": "333"}
    assert creative["branded_content"] == {"instagram_boost_post_access_token": "AC-1"}
    assert "source_instagram_media_id" not in creative and "instagram_user_id" not in creative


def test_boost_one_step_plan_with_a_partnership_ad_code_never_shows_the_code(monkeypatch) -> None:
    client = FakeOneStepClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    arguments = {key: value for key, value in ONE_STEP.items() if key != "instagram_media_id"}
    plan = asyncio.run(creatives.boost_instagram_post(**arguments, partnership_ad_code="AC-SECRET-1", plan_only=True))
    assert "AC-SECRET-1" not in str(plan)
    creative = plan["plan"]["requests"][2]["data"]
    assert creative["branded_content"] == {"instagram_boost_post_access_token": "<partnership_ad_code>"}
    asyncio.run(creatives.boost_instagram_post(**arguments, partnership_ad_code="AC-SECRET-1"))
    sent = _created(client, "adcreatives")
    assert sent["branded_content"] == {"instagram_boost_post_access_token": "AC-SECRET-1"}
    assert sent["name"] == "Partnership ad creative"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"instagram_media_id": "555", "partnership_ad_code": "AC-1"}, "exactly one of instagram_media_id or partnership_ad_code"),
        ({}, "exactly one of instagram_media_id or partnership_ad_code"),
        ({"partnership_ad_code": "has space"}, "partnership_ad_code"),
        ({"partnership_ad_code": "AC-1", "instagram_account_id": None}, "instagram_account_id is required"),
    ],
)
def test_boost_partnership_ad_code_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    base = {"account_id": "123", "instagram_account_id": "ig_1", "adset_id": "adset_1", "page_id": "page_1"}
    with pytest.raises(creatives.ValidationError, match=message):
        asyncio.run(creatives.boost_instagram_post(**{**base, **arguments}))


def test_boost_one_step_never_defaults_a_budget(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    arguments = {key: value for key, value in ONE_STEP.items() if key != "daily_budget"}
    for objective in ("OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT"):
        with pytest.raises(creatives.ValidationError, match="does not pick a budget"):
            asyncio.run(creatives.boost_instagram_post(**arguments, objective=objective, plan_only=True))


# upload_ad_video: chunked upload for local files above the direct-upload limit


class ChunkedVideoClient:
    """Plays Meta's side of a chunked advideos upload: start, transfers, finish."""

    def __init__(self, *, chunk: int = 10, fail: dict | None = None, transfer_offsets=None, finish=None) -> None:
        self.chunk = chunk
        self.fail = fail or {}
        self.transfer_offsets = list(transfer_offsets) if transfer_offsets is not None else None
        self.finish = finish if finish is not None else {"success": True}
        self.calls: list[tuple] = []
        self.size = 0

    async def post_ad_video_upload_phase(self, account_id, *, data, files=None):
        phase = data["upload_phase"]
        chunk = files["video_file_chunk"][1] if files else None
        self.calls.append((phase, account_id, dict(data), chunk))
        transfers = sum(1 for call in self.calls if call[0] == "transfer")
        if (phase, transfers if phase == "transfer" else 0) in self.fail:
            raise self.fail[(phase, transfers if phase == "transfer" else 0)]
        if phase == "start":
            self.size = int(data["file_size"])
            return {"upload_session_id": "5001", "video_id": "7001", "start_offset": "0", "end_offset": str(min(self.chunk, self.size))}
        if phase == "transfer":
            if self.transfer_offsets is not None:
                start, end = self.transfer_offsets.pop(0)
                return {"start_offset": str(start), "end_offset": str(end)}
            start = int(data["start_offset"]) + len(chunk)
            return {"start_offset": str(start), "end_offset": str(min(start + self.chunk, self.size))}
        return self.finish

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields, None))
        return {"id": object_id, "status": {"video_status": "processing"}}


def _large_video(monkeypatch, tmp_path, size: int = 25, name: str = "film.mp4"):
    monkeypatch.setattr(creatives, "MAX_DIRECT_VIDEO_BYTES", 8)
    path = tmp_path / name
    path.write_bytes(bytes(range(65, 65 + size)))
    return path


def test_upload_ad_video_large_file_uses_the_chunked_upload(monkeypatch, tmp_path) -> None:
    path = _large_video(monkeypatch, tmp_path)
    client = ChunkedVideoClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)

    result = asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4", title="Spring", description="Launch"))

    content = path.read_bytes()
    assert client.calls == [
        ("start", "act_123", {"upload_phase": "start", "file_size": 25}, None),
        ("transfer", "act_123", {"upload_phase": "transfer", "upload_session_id": "5001", "start_offset": 0}, content[0:10]),
        ("transfer", "act_123", {"upload_phase": "transfer", "upload_session_id": "5001", "start_offset": 10}, content[10:20]),
        ("transfer", "act_123", {"upload_phase": "transfer", "upload_session_id": "5001", "start_offset": 20}, content[20:25]),
        ("finish", "act_123", {"upload_phase": "finish", "upload_session_id": "5001", "title": "Spring", "description": "Launch"}, None),
        ("get", "7001", ["id", "status"], None),
    ]
    assert result["video_id"] == "7001"
    assert result["processing_status"] == "processing"
    assert result["upload"] == {
        "mode": "chunked", "upload_session_id": "5001", "file_name": "film.mp4", "bytes": 25,
        "bytes_confirmed": 25, "transfer_requests": 3,
    }
    assert result["created"] == {"finish": {"success": True}}


def test_upload_ad_video_small_file_still_uses_one_request(monkeypatch, tmp_path) -> None:
    client = FakeVideoClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    (tmp_path / "clip.mp4").write_bytes(b"12345678")
    monkeypatch.setattr(creatives, "MAX_DIRECT_VIDEO_BYTES", 8)
    result = asyncio.run(creatives.upload_ad_video("123", file_path="clip.mp4"))
    assert client.calls[0] == ("create", "act_123", "advideos", {}, {"source": ("clip.mp4", b"12345678")})
    assert result["upload"]["mode"] == "direct"


def test_upload_ad_video_chunked_dry_run_sends_nothing(monkeypatch, tmp_path) -> None:
    _large_video(monkeypatch, tmp_path)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    result = asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4", name="Film", dry_run=True))
    assert result["sent"] is False
    would_send = result["would_send"]
    assert would_send["upload"] == "chunked"
    assert would_send["host"] == "graph-video.facebook.com"
    assert would_send["source"] == {"file_name": "film.mp4", "bytes": 25}
    assert [request["data"]["upload_phase"] for request in would_send["requests"]] == ["start", "transfer", "finish"]
    assert would_send["requests"][0]["data"] == {"upload_phase": "start", "file_size": 25}
    assert would_send["requests"][2]["data"] == {"upload_phase": "finish", "upload_session_id": "<from start>", "name": "Film"}


def test_upload_ad_video_chunked_failure_reports_session_and_bytes(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.errors import RateLimitError

    _large_video(monkeypatch, tmp_path)
    client = ChunkedVideoClient(fail={("transfer", 2): RateLimitError("Synthetic limit", code=4)})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(RateLimitError) as raised:
        asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4"))
    message = str(raised.value)
    for expected in ("upload session 5001", "video 7001", "10 of 25 bytes", "transfer step", "2 transfer requests", "Synthetic limit"):
        assert expected in message, message
    assert [call[0] for call in client.calls] == ["start", "transfer", "transfer"]


def test_upload_ad_video_chunked_lost_finish_says_check_before_retrying(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _large_video(monkeypatch, tmp_path)
    lost = MetaApiError(message="Meta API request failed before a response was received.", is_transient=True, mutation_outcome_unknown=True)
    client = ChunkedVideoClient(fail={("finish", 0): lost})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4"))
    message = str(raised.value)
    assert "25 of 25 bytes" in message
    assert "may have been accepted" in message
    assert "list_ad_videos(video_ids=['7001'])" in message


def test_upload_ad_video_chunked_start_failure_says_nothing_was_sent(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _large_video(monkeypatch, tmp_path)
    client = ChunkedVideoClient(fail={("start", 0): MetaApiError(message="Invalid parameter", code=100)})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError, match="nothing was transferred"):
        asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4"))


@pytest.mark.parametrize(
    ("offsets", "reason"),
    [
        ([(0, 10)], "did not move forward"),
        ([(10, 40)], "outside the 25-byte file"),
        ([(10, 5)], "outside the 25-byte file"),
    ],
)
def test_upload_ad_video_chunked_stops_on_offsets_it_cannot_follow(monkeypatch, tmp_path, offsets, reason) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _large_video(monkeypatch, tmp_path)
    client = ChunkedVideoClient(transfer_offsets=offsets)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4"))
    assert reason in str(raised.value)
    assert "upload session 5001" in str(raised.value)
    assert [call[0] for call in client.calls] == ["start", "transfer"]


def test_upload_ad_video_chunked_finish_without_success_is_not_reported_as_uploaded(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _large_video(monkeypatch, tmp_path)
    client = ChunkedVideoClient(finish={"success": False})
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError, match="did not confirm"):
        asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4"))


def test_upload_ad_video_chunked_transfer_request_cap(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _large_video(monkeypatch, tmp_path)
    monkeypatch.setattr(creatives, "MAX_CHUNKED_TRANSFER_REQUESTS", 2)
    client = ChunkedVideoClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError, match="2-request limit"):
        asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4"))
    assert [call[0] for call in client.calls] == ["start", "transfer", "transfer"]


def test_upload_ad_video_refuses_files_above_the_chunked_limit(monkeypatch, tmp_path) -> None:
    _large_video(monkeypatch, tmp_path)
    monkeypatch.setattr(creatives, "MAX_CHUNKED_VIDEO_BYTES", 20)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    for dry_run in (False, True):
        with pytest.raises(creatives.ValidationError, match="20-byte limit"):
            asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4", dry_run=dry_run))


def test_upload_ad_video_chunk_size_above_the_read_cap_is_refused(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _large_video(monkeypatch, tmp_path)
    monkeypatch.setattr(creatives, "MAX_VIDEO_CHUNK_BYTES", 5)
    client = ChunkedVideoClient(chunk=10)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError, match="larger than this server reads at once"):
        asyncio.run(creatives.upload_ad_video("123", file_path="film.mp4"))
    assert [call[0] for call in client.calls] == ["start"]


def test_upload_ad_video_swapped_parent_folder_cannot_escape_the_upload_folder(monkeypatch, tmp_path) -> None:
    """A folder inside the upload folder replaced by a link after the check must not be followed."""
    from meta_ads_mcp.config import reload_settings

    root = tmp_path / "uploads"
    nested = root / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (nested / "clip.mp4").write_bytes(b"inside")
    (outside / "clip.mp4").write_bytes(b"synthetic-outside-secret")
    monkeypatch.setenv("META_UPLOAD_DIR", str(root))
    reload_settings()
    original_open = creatives.os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and str(path) in {"nested", str(nested)}:
            swapped = True
            nested.rename(root / "saved-nested")
            nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(creatives.os, "open", swap_before_open)
    client = FakeVideoClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    try:
        asyncio.run(creatives.upload_ad_video("123", file_path="nested/clip.mp4"))
    except creatives.ValidationError as error:
        assert "outside" not in str(error)
    assert swapped
    assert all(call[4] != {"source": ("clip.mp4", b"synthetic-outside-secret")} for call in client.calls if call[0] == "create")


def test_g20_boost_call_to_action_link_with_login_is_refused(monkeypatch) -> None:
    secret = "synthetic-link-password"
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    cta = {"type": "LEARN_MORE", "value": {"link": f"https://user:{secret}@example.invalid/page"}}
    base = {"account_id": "1", "instagram_media_id": "40", "instagram_account_id": "50"}
    for arguments in ({"adset_id": "60", "page_id": "30"}, {**ONE_STEP, "plan_only": True}):
        with pytest.raises(creatives.ValidationError) as raised:
            asyncio.run(creatives.boost_instagram_post(**{**base, **arguments, "call_to_action": cta}))
        assert secret not in str(raised.value)


# Round-four review fixes (R3 findings H1 to H5).


def _call_tool(name: str, arguments: dict, routed: bool):
    from meta_ads_mcp.coordinator import mcp_server

    if routed:
        return asyncio.run(mcp_server.call_tool("call_tool", {"name": name, "arguments": arguments}))
    return asyncio.run(mcp_server.call_tool(name, arguments))


class EchoCodeClient:
    """Meta refuses one edge and repeats the code it was sent, in every error field."""

    def __init__(self, *, fail_edge: str = "adcreatives", cycle: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail_edge = fail_edge
        self.cycle = cycle

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields))
        return {"id": object_id, "currency": "USD", "promoted_object": {"page_id": "20"}}

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        from meta_ads_mcp.errors import MetaApiError

        self.calls.append(("create", parent_id, edge, data))
        if edge != self.fail_edge:
            return {"id": "900"}
        code = "synthetic-private-code"
        cause = ValueError(f"upstream said {code}")
        error = MetaApiError(
            f"Invalid authorization code {code}", code=100, user_message=f"Rejected {code}",
            user_title=f"Code {code}", details={"error": {"message": f"bad {code}", "list": [code]}},
            error_data={"blame": code},
        )
        error.__cause__ = cause
        if self.cycle:
            # The cleaning must end on a cycle. (Not used through the MCP server: its
            # traceback logger, rich, does not end on a cycle itself.)
            cause.__context__ = error
        raise error


H1_CASES = [
    {"instagram_account_id": "40", "adset_id": "50", "partnership_ad_code": "synthetic-private-code"},
    {"instagram_account_id": "40", "adset_id": "50", "partnership_ad_code": "synthetic-private-code", "validate_only": True},
    {"instagram_account_id": "40", "daily_budget": 12.5, "targeting": {"geo_locations": {"countries": ["US"]}},
     "partnership_ad_code": "synthetic-private-code"},
]


@pytest.mark.parametrize("extra", H1_CASES)
@pytest.mark.parametrize("routed", [False, True])
def test_h1_partnership_upstream_error_never_discloses_code(monkeypatch, caplog, extra, routed) -> None:
    from fastmcp.exceptions import ToolError

    client = EchoCodeClient(fail_edge="ads" if extra.get("validate_only") else "adcreatives")
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(ToolError) as info:
        _call_tool("boost_instagram_post", {"account_id": "10", "page_id": "20", **extra}, routed)
    assert "synthetic-private-code" not in str(info.value)
    assert "synthetic-private-code" not in caplog.text
    assert any(call[0] == "create" for call in client.calls)


def test_h1_direct_call_error_chain_and_fields_never_hold_the_code(monkeypatch) -> None:
    from meta_ads_mcp.errors import MetaApiError

    client = EchoCodeClient(cycle=True)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(creatives.boost_instagram_post(
            account_id="10", page_id="20", instagram_account_id="40", adset_id="50",
            partnership_ad_code="synthetic-private-code",
        ))
    error = info.value
    for item in (error, error.__cause__, error.__cause__.__context__):
        assert "synthetic-private-code" not in repr(item.args) + str(item)
    assert "synthetic-private-code" not in repr(error.to_public_dict())
    assert "synthetic-private-code" not in repr(error.details)
    assert "<partnership_ad_code>" in error.message


def test_h1_code_url_encoded_in_an_error_is_removed_too(monkeypatch) -> None:
    from meta_ads_mcp.errors import MetaApiError

    class Encoded(EchoCodeClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            raise MetaApiError("bad token=AC%2F1%2B2")

    monkeypatch.setattr(creatives, "get_graph_api_client", Encoded)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(creatives.boost_instagram_post(
            account_id="10", page_id="20", instagram_account_id="40", adset_id="50", partnership_ad_code="AC/1+2",
        ))
    assert "AC%2F1%2B2" not in str(info.value)


@pytest.mark.parametrize("routed", [False, True])
def test_h2_video_body_error_does_not_echo_private_filename(monkeypatch, tmp_path, caplog, routed) -> None:
    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.errors import RateLimitError

    secret = "private.person@r3.invalid"
    (tmp_path / f"{secret}.mp4").write_bytes(b"0123456789")
    monkeypatch.setattr(creatives, "MAX_DIRECT_VIDEO_BYTES", 1)

    class Refused:
        async def post_ad_video_upload_phase(self, *args, **kwargs):
            raise RateLimitError("Synthetic limit", code=4)

    monkeypatch.setattr(creatives, "get_graph_api_client", Refused)
    with pytest.raises(ToolError) as error:
        _call_tool("upload_ad_video", {"account_id": "10", "file_path": str(tmp_path / f"{secret}.mp4")}, routed)
    assert secret not in str(error.value)
    assert secret not in caplog.text


@pytest.mark.parametrize("step", ["start", "transfer", "finish", "no_session"])
def test_h2_no_chunked_upload_error_names_the_file_even_when_meta_repeats_it(monkeypatch, tmp_path, step) -> None:
    from meta_ads_mcp.errors import MetaApiError

    secret = "private-family-holiday"
    (tmp_path / f"{secret}.mp4").write_bytes(b"0123456789")
    monkeypatch.setattr(creatives, "MAX_DIRECT_VIDEO_BYTES", 1)

    class Client:
        async def post_ad_video_upload_phase(self, account_id, *, data, files=None):
            phase = data["upload_phase"]
            if step == "no_session" and phase == "start":
                return {}
            if phase == step:
                raise MetaApiError(f"Could not process {secret}.mp4", user_message=f"{secret}.mp4 refused")
            if phase == "start":
                return {"upload_session_id": "1", "video_id": "2", "start_offset": "0", "end_offset": "10"}
            if phase == "transfer":
                return {"start_offset": "10", "end_offset": "10"}
            return {"success": True}

    monkeypatch.setattr(creatives, "get_graph_api_client", Client)
    with pytest.raises(MetaApiError) as error:
        asyncio.run(creatives.upload_ad_video("10", file_path=f"{secret}.mp4"))
    assert secret not in str(error.value)
    assert secret not in repr(error.value.to_public_dict())


async def _mcp_call(name, arguments):
    from meta_ads_mcp.coordinator import mcp_server

    return await mcp_server.call_tool(name, arguments)


@pytest.mark.parametrize(
    ("field", "value"),
    [("daily_budget", True), ("daily_budget", False), ("duration_days", True), ("partnership_ad_format", True)],
)
def test_h3_boost_budget_bool_is_rejected_at_mcp_boundary(monkeypatch, field, value) -> None:
    from fastmcp.exceptions import ToolError, ValidationError as MCPValidationError

    client = FakeOneStepClient()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    arguments = {"account_id": "10", "instagram_media_id": "30", "instagram_account_id": "40", "page_id": "20",
                 "daily_budget": 12.5, "targeting": {"geo_locations": {"countries": ["US"]}}, "sponsor_page_id": "22",
                 field: value}
    with pytest.raises((ToolError, MCPValidationError)):
        asyncio.run(_mcp_call("boost_instagram_post", arguments))
    assert client.calls == []


class OffsetScriptClient:
    """Answers start with the given range, and each transfer with the next scripted range."""

    def __init__(self, initial, answers) -> None:
        self.initial = initial
        self.answers = list(answers)
        self.calls: list[tuple] = []

    async def post_ad_video_upload_phase(self, account_id, *, data, files=None):
        self.calls.append((data["upload_phase"], dict(data)))
        if data["upload_phase"] == "start":
            return {"upload_session_id": "session-90", "video_id": "91",
                    "start_offset": str(self.initial[0]), "end_offset": str(self.initial[1])}
        if data["upload_phase"] == "transfer":
            start, end = self.answers.pop(0)
            return {"start_offset": str(start), "end_offset": str(end)}
        return {"success": True}

    async def get_object(self, object_id, *, fields=None, params=None):
        return {"id": object_id, "status": {"video_status": "processing"}}


def _ten_byte_video(monkeypatch, tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"0123456789")
    monkeypatch.setattr(creatives, "MAX_DIRECT_VIDEO_BYTES", 1)


@pytest.mark.parametrize("terminal", [(0, 0), (2, 2), (5, 5)])
def test_h4_chunked_upload_cannot_finish_before_all_bytes_confirmed(monkeypatch, tmp_path, terminal) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _ten_byte_video(monkeypatch, tmp_path)
    client = OffsetScriptClient((0, 5), [terminal])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(creatives.upload_ad_video("10", file_path="video.mp4"))
    assert not any(call[0] == "finish" for call in client.calls), client.calls
    message = str(raised.value)
    for expected in ("upload session session-90", "video 91", "of 10 bytes", "transfer step"):
        assert expected in message, message


@pytest.mark.parametrize("initial,answers", [((3, 10), [(10, 10)]), ((0, 3), [(6, 10), (10, 10)])])
def test_h4_chunked_upload_must_not_skip_file_ranges(monkeypatch, tmp_path, initial, answers) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _ten_byte_video(monkeypatch, tmp_path)
    client = OffsetScriptClient(initial, answers)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(creatives.upload_ad_video("10", file_path="video.mp4"))
    assert not any(call[0] == "finish" for call in client.calls), client.calls
    assert "would never be sent" in str(raised.value)


@pytest.mark.parametrize(
    ("initial", "answers", "confirmed"),
    [
        ((0, 0), [], 0),  # a fresh session that claims to be done already
        ((0, 4), [(4, 8), (2, 10)], 4),  # a step backwards after progress
        ((0, 4), [(4, 8), (8, 8)], 8),  # stops short of the file size
        ((0, 4), [(5, 10)], 0),  # a gap of one byte
    ],
)
def test_h4_chunked_upload_offset_rules_beyond_the_reproduction(monkeypatch, tmp_path, initial, answers, confirmed) -> None:
    from meta_ads_mcp.errors import MetaApiError

    _ten_byte_video(monkeypatch, tmp_path)
    client = OffsetScriptClient(initial, answers)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(creatives.upload_ad_video("10", file_path="video.mp4"))
    assert not any(call[0] == "finish" for call in client.calls)
    assert f"Meta confirmed {confirmed} of 10 bytes" in str(raised.value)


def test_h4_offsets_advancing_chunk_by_chunk_still_finish(monkeypatch, tmp_path) -> None:
    _ten_byte_video(monkeypatch, tmp_path)
    client = OffsetScriptClient((0, 4), [(4, 8), (8, 10), (10, 10)])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(creatives.upload_ad_video("10", file_path="video.mp4"))
    assert [call[1].get("start_offset") for call in client.calls if call[0] == "transfer"] == [0, 4, 8]
    assert result["upload"]["bytes_confirmed"] == 10
    assert result["upload"]["transfer_requests"] == 3


def test_h5_chunked_upload_detects_growing_file(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.errors import MetaApiError

    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")
    monkeypatch.setattr(creatives, "MAX_DIRECT_VIDEO_BYTES", 1)

    class Growth(OffsetScriptClient):
        async def post_ad_video_upload_phase(self, account_id, *, data, files=None):
            if data["upload_phase"] == "transfer":
                with path.open("ab") as handle:
                    handle.write(b"more-data")
            return await super().post_ad_video_upload_phase(account_id, data=data, files=files)

    client = Growth((0, 10), [(10, 10)])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(creatives.upload_ad_video("10", file_path="video.mp4"))
    message = str(raised.value)
    assert not any(call[0] == "finish" for call in client.calls)
    for expected in ("changed while it was being uploaded", "upload session session-90", "video 91", "10 of 10 bytes", "finish step"):
        assert expected in message, message


def test_h5_file_rewritten_between_chunks_stops_before_the_next_transfer(monkeypatch, tmp_path) -> None:
    import os

    from meta_ads_mcp.errors import MetaApiError

    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")
    monkeypatch.setattr(creatives, "MAX_DIRECT_VIDEO_BYTES", 1)

    class Rewrite(OffsetScriptClient):
        async def post_ad_video_upload_phase(self, account_id, *, data, files=None):
            if data["upload_phase"] == "transfer":
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
            return await super().post_ad_video_upload_phase(account_id, data=data, files=files)

    client = Rewrite((0, 5), [(5, 10)])
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError, match="changed while it was being uploaded") as raised:
        asyncio.run(creatives.upload_ad_video("10", file_path="video.mp4"))
    assert [call[0] for call in client.calls] == ["start", "transfer"]
    assert "5 of 10 bytes" in str(raised.value)


def test_h5_small_file_that_grows_while_read_is_not_sent(monkeypatch, tmp_path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"12345678")
    original = creatives._read_video_bytes

    def read_then_grow(video, offset, length):
        content = original(video, offset, length)
        with path.open("ab") as handle:
            handle.write(b"tail")
        return content

    monkeypatch.setattr(creatives, "_read_video_bytes", read_then_grow)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(creatives.ValidationError, match="nothing was sent"):
        asyncio.run(creatives.upload_ad_video("10", file_path="clip.mp4"))


def test_nested_login_url_error_never_quotes_a_pasted_key(monkeypatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    secret_key = "someone@example.invalid"
    cta = {"type": "LEARN_MORE", "value": {secret_key: "https://u:p@example.invalid/x"}}
    with pytest.raises(creatives.ValidationError) as raised:
        asyncio.run(creatives.boost_instagram_post(
            account_id="1", instagram_media_id="40", instagram_account_id="50", adset_id="60", page_id="30", call_to_action=cta,
        ))
    assert secret_key not in str(raised.value)
    assert "call_to_action.value.<key>" in str(raised.value)


def test_delete_ad_image_not_found_does_not_quote_the_hash(monkeypatch) -> None:
    class Empty:
        async def get_ad_images_by_hashes(self, account_id, *, hashes, fields):
            return {"data": []}

    monkeypatch.setattr(creatives, "get_graph_api_client", Empty)
    with pytest.raises(creatives.ValidationError) as raised:
        asyncio.run(creatives.delete_ad_image("10", "pasted-private-value"))
    assert "pasted-private-value" not in str(raised.value)
