"""Tests for create_ad_creative_from_inputs and update_ad_creative_enhancements."""

from __future__ import annotations

import asyncio
from typing import Any

import json

import httpx
import pytest

from meta_ads_mcp.errors import MetaApiError, NotFoundError, RateLimitError, ValidationError
from meta_ads_mcp.tools import creative_formats as cf


class FakeClient:
    """Records every call and serves configured objects."""

    def __init__(self, objects: dict[str, dict[str, Any]] | None = None, fail_update: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.objects = objects or {}
        self.fail_update = fail_update

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.calls.append(("create", parent_id, edge, data))
        return {"id": "999"}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields))
        return dict(self.objects.get(object_id, {"id": object_id}))

    async def update_object(self, object_id: str, *, data):
        self.calls.append(("update", object_id, data))
        if self.fail_update is not None:
            raise self.fail_update
        return {"success": True}


def _patch(monkeypatch, client) -> None:
    monkeypatch.setattr(cf, "get_graph_api_client", lambda: client)


def _no_client(monkeypatch) -> None:
    monkeypatch.setattr(cf, "get_graph_api_client", lambda: pytest.fail("No client should be created"))


def _create(monkeypatch, **arguments) -> tuple[dict[str, Any], dict[str, Any]]:
    client = FakeClient()
    _patch(monkeypatch, client)
    result = asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="Creative", **arguments))
    assert len(client.calls) == 1
    _, parent, edge, data = client.calls[0]
    assert (parent, edge) == ("act_123", "adcreatives")
    return result, data


def test_create_ad_creative_from_inputs_dry_run_sends_nothing_and_returns_the_spec(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Spring", page_id="111", image_hash="h1", link_url="https://example.com",
        message="Body", dry_run=True,
    ))
    assert result["dry_run"] is True
    assert result["sent"] is False
    assert result["format"] == "single_image"
    assert result["would_send"] == {
        "method": "POST",
        "path": "act_123/adcreatives",
        "data": {
            "name": "Spring",
            "object_story_spec": {
                "page_id": "111",
                "link_data": {
                    "link": "https://example.com",
                    "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com"}},
                    "image_hash": "h1",
                    "message": "Body",
                },
            },
        },
    }


def test_single_image_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", instagram_account_id="222", image_url="https://cdn.example.com/a.png",
        link_url="https://example.com/p", call_to_action_type="shop_now", message="Body", headline="Head",
        description="Desc", url_tags="utm_source=meta",
    )
    assert data == {
        "name": "Creative",
        "object_story_spec": {
            "page_id": "111",
            "instagram_user_id": "222",
            "link_data": {
                "link": "https://example.com/p",
                "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://example.com/p"}},
                "picture": "https://cdn.example.com/a.png",
                "message": "Body",
                "name": "Head",
                "description": "Desc",
            },
        },
        "url_tags": "utm_source=meta",
    }
    assert result["creative_id"] == "999"
    assert result["format"] == "single_image"
    assert result["action"] == "create_ad_creative_from_inputs"


def test_single_video_spec(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", video_id="555", image_hash="thumb", link_url="https://example.com",
        message="Body", headline="Title", description="More",
    )
    assert data["object_story_spec"] == {
        "page_id": "111",
        "video_data": {
            "video_id": "555",
            "image_hash": "thumb",
            "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com"}},
            "message": "Body",
            "title": "Title",
            "link_description": "More",
        },
    }


def test_carousel_spec(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", link_url="https://example.com", message="Pick one",
        cards=[
            {"image_hash": "h1", "headline": "One", "description": "$1", "link": "https://example.com/1"},
            {"image_url": "https://cdn.example.com/2.png", "video_id": "77", "call_to_action_type": "SHOP_NOW"},
        ],
    )
    assert data["object_story_spec"] == {
        "page_id": "111",
        "link_data": {
            "link": "https://example.com",
            "child_attachments": [
                {
                    "link": "https://example.com/1", "image_hash": "h1", "name": "One", "description": "$1",
                    "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/1"}},
                },
                {
                    "link": "https://example.com", "picture": "https://cdn.example.com/2.png", "video_id": "77",
                    "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://example.com"}},
                },
            ],
            "message": "Pick one",
        },
    }


def test_text_variants_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", image_hash="h1", link_url="https://example.com",
        primary_texts=["A", "B"], headlines="H1, H2", descriptions=["D1"], call_to_action_type="SIGN_UP",
    )
    assert data["object_story_spec"] == {"page_id": "111"}
    assert data["asset_feed_spec"] == {
        "images": [{"hash": "h1"}],
        "bodies": [{"text": "A"}, {"text": "B"}],
        "titles": [{"text": "H1"}, {"text": "H2"}],
        "descriptions": [{"text": "D1"}],
        "link_urls": [{"website_url": "https://example.com"}],
        "call_to_action_types": ["SIGN_UP"],
        "ad_formats": ["SINGLE_IMAGE"],
    }
    assert result["format"] == "text_variants"


def test_text_variants_with_video_uses_thumbnail(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", video_id="555", image_url="https://cdn.example.com/t.png",
        link_url="https://example.com", primary_texts=["A", "B"],
    )
    assert data["asset_feed_spec"]["videos"] == [{"video_id": "555", "thumbnail_url": "https://cdn.example.com/t.png"}]
    assert data["asset_feed_spec"]["ad_formats"] == ["SINGLE_VIDEO"]


def test_placement_customization_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", link_url="https://example.com", message="Body",
        placement_customizations=[
            {"image_hash": "feed", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]},
            {"image_hash": "story", "publisher_platforms": "instagram", "instagram_positions": "story, reels"},
            {"image_hash": "fallback"},
        ],
    )
    feed = data["asset_feed_spec"]
    assert feed["images"] == [
        {"hash": "feed", "adlabels": [{"name": "placement_1"}]},
        {"hash": "story", "adlabels": [{"name": "placement_2"}]},
        {"hash": "fallback", "adlabels": [{"name": "placement_3"}]},
    ]
    assert feed["asset_customization_rules"] == [
        {"customization_spec": {"publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}, "image_label": {"name": "placement_1"}},
        {"customization_spec": {"publisher_platforms": ["instagram"], "instagram_positions": ["story", "reels"]}, "image_label": {"name": "placement_2"}},
        {"customization_spec": {}, "image_label": {"name": "placement_3"}},
    ]
    assert feed["optimization_type"] == "PLACEMENT"
    assert feed["ad_formats"] == ["SINGLE_IMAGE"]
    assert feed["bodies"] == [{"text": "Body"}]
    assert feed["link_urls"] == [{"website_url": "https://example.com"}]
    assert any("empty customization_spec" in note for note in result["notes"])


def test_placement_customization_videos_use_video_labels(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", link_url="https://example.com",
        placement_customizations=[
            {"video_id": "1", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]},
            {"video_id": "2", "publisher_platforms": ["instagram"], "instagram_positions": ["stream"]},
        ],
    )
    rules = data["asset_feed_spec"]["asset_customization_rules"]
    assert rules[1] == {"customization_spec": {"publisher_platforms": ["instagram"], "instagram_positions": ["stream"]}, "video_label": {"name": "placement_2"}}
    assert data["asset_feed_spec"]["videos"][0] == {"video_id": "1", "adlabels": [{"name": "placement_1"}]}


def test_existing_page_post_spec(monkeypatch) -> None:
    _, data = _create(monkeypatch, object_story_id="111_222", page_id="111", instagram_account_id="333")
    assert data == {"name": "Creative", "object_story_id": "111_222", "instagram_user_id": "333"}


def test_existing_instagram_post_spec(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, source_instagram_media_id="444", page_id="111", instagram_account_id="333",
        link_url="https://example.com", call_to_action_type="SHOP_NOW",
    )
    assert data == {
        "name": "Creative",
        "object_id": "111",
        "source_instagram_media_id": "444",
        "instagram_user_id": "333",
        "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://example.com"}},
    }


@pytest.mark.parametrize(
    ("cta", "expected_link_data"),
    [
        ("WHATSAPP_MESSAGE", {
            "link": "https://api.whatsapp.com/send",
            "call_to_action": {"type": "WHATSAPP_MESSAGE", "value": {"app_destination": "WHATSAPP"}},
            "page_welcome_message": "Hi",
            "image_hash": "h1",
        }),
        ("MESSAGE_PAGE", {
            "call_to_action": {"type": "MESSAGE_PAGE", "value": {"app_destination": "MESSENGER"}},
            "page_welcome_message": "Hi",
            "image_hash": "h1",
        }),
        ("INSTAGRAM_MESSAGE", {
            "call_to_action": {"type": "INSTAGRAM_MESSAGE", "value": {"app_destination": "INSTAGRAM_DIRECT"}},
            "page_welcome_message": "Hi",
            "image_hash": "h1",
        }),
    ],
)
def test_click_to_message_image_specs(monkeypatch, cta, expected_link_data) -> None:
    _, data = _create(
        monkeypatch, page_id="111", instagram_account_id="333", image_hash="h1",
        call_to_action_type=cta, page_welcome_message="Hi",
    )
    assert data["object_story_spec"] == {"page_id": "111", "instagram_user_id": "333", "link_data": expected_link_data}


def test_click_to_messenger_video_spec(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", video_id="555", image_url="https://cdn.example.com/t.png",
        call_to_action_type="MESSAGE_PAGE", description="Chat with us",
    )
    assert data["object_story_spec"]["video_data"] == {
        "video_id": "555",
        "image_url": "https://cdn.example.com/t.png",
        "call_to_action": {"type": "MESSAGE_PAGE", "value": {"app_destination": "MESSENGER"}},
        "link_description": "Chat with us",
    }


def test_lead_form_image_and_video_specs(monkeypatch) -> None:
    _, image = _create(monkeypatch, page_id="111", image_hash="h1", lead_gen_form_id="888", call_to_action_type="SIGN_UP")
    assert image["object_story_spec"]["link_data"] == {
        "link": "https://fb.me/",
        "call_to_action": {"type": "SIGN_UP", "value": {"lead_gen_form_id": "888"}},
        "image_hash": "h1",
    }
    _, video = _create(monkeypatch, page_id="111", video_id="555", image_hash="t", lead_gen_form_id="888")
    assert video["object_story_spec"]["video_data"]["call_to_action"] == {
        "type": "LEARN_MORE", "value": {"link": "https://fb.me/", "lead_gen_form_id": "888"},
    }


def test_partnership_instagram_post_spec(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, source_instagram_media_id="444", page_id="111", sponsor_page_id="222",
        sponsor_instagram_account_id="333", partnership_ad_format=2,
    )
    assert data == {
        "name": "Creative",
        "object_id": "111",
        "source_instagram_media_id": "444",
        "facebook_branded_content": {"sponsor_page_id": "222"},
        "instagram_branded_content": {"sponsor_id": "333"},
        "branded_content": {"ad_format": 2},
    }


def test_partnership_ad_code_spec_is_sent_but_not_echoed(monkeypatch) -> None:
    result, data = _create(monkeypatch, partnership_ad_code="SECRETCODE", page_id="111", sponsor_page_id="222")
    assert data == {
        "name": "Creative",
        "object_id": "111",
        "branded_content": {"instagram_boost_post_access_token": "SECRETCODE"},
        "facebook_branded_content": {"sponsor_page_id": "222"},
    }
    assert "SECRETCODE" not in str(result)


INVALID_COMBINATIONS = [
    ({"page_id": "111", "image_hash": "h", "image_url": "https://x/y.png", "link_url": "https://x"}, "not both"),
    ({"page_id": "111", "image_hash": "h"}, "link_url is required"),
    ({"image_hash": "h", "link_url": "https://x"}, "page_id is required"),
    ({"page_id": "111", "image_hash": "h", "link_url": "example.com"}, "full http"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "call_to_action_type": "BUY_A_BOAT"}, "call-to-action"),
    ({"page_id": "111", "video_id": "5", "link_url": "https://x"}, "thumbnail"),
    ({"page_id": "111", "video_id": "5", "image_hash": "t", "description": "d"}, "link_description"),
    ({"page_id": "111", "video_id": "5", "image_hash": "t", "call_to_action_type": "WHATSAPP_MESSAGE"}, "not built"),
    ({"page_id": "111", "image_hash": "h", "call_to_action_type": "WHATSAPP_MESSAGE", "link_url": "https://x"}, "link_url is not used"),
    ({"page_id": "111", "image_hash": "h", "call_to_action_type": "INSTAGRAM_MESSAGE"}, "instagram_account_id"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "page_welcome_message": "hi"}, "click-to-message"),
    ({"page_id": "111", "image_hash": "h", "lead_gen_form_id": "8", "call_to_action_type": "SHOP_NOW"}, "instant form"),
    ({"page_id": "111", "image_hash": "h", "lead_gen_form_id": "8", "link_url": "https://x"}, "fb.me"),
    ({"page_id": "111", "link_url": "https://x", "cards": [{"image_hash": "a"}]}, "2 to 10"),
    ({"page_id": "111", "link_url": "https://x", "cards": [{"image_hash": "a"}, {"video_id": "1"}]}, "needs image_hash"),
    ({"page_id": "111", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "See more"),
    ({"page_id": "111", "link_url": "https://x", "cards": [{"image_hash": "a"}, {"image_hash": "b", "colour": "red"}]}, "unknown keys"),
    ({"page_id": "111", "link_url": "https://x", "image_hash": "h", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "does not accept"),
    ({"page_id": "111", "link_url": "https://x", "message": "m", "primary_texts": ["a"], "image_hash": "h"}, "not both"),
    ({"page_id": "111", "link_url": "https://x", "primary_texts": ["a"] * 2, "image_hash": "h"}, "repeat"),
    ({"page_id": "111", "link_url": "https://x", "primary_texts": list("abcdef"), "image_hash": "h"}, "at most 5"),
    ({"page_id": "111", "link_url": "https://x", "headlines": ["a", " "], "image_hash": "h"}, "blank"),
    ({"page_id": "111", "link_url": "https://x", "headlines": ["x" * 256], "image_hash": "h"}, "255"),
    ({"page_id": "111", "link_url": "https://x", "primary_texts": ["a"], "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "cannot be combined with cards"),
    ({"page_id": "111", "image_hash": "h", "primary_texts": ["a"], "call_to_action_type": "MESSAGE_PAGE"}, "Click-to-message is not built"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "2 to 10"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b"}]}, "Only one entry"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"video_id": "1", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "all images or all videos"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "a", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "repeats media"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["facebook"]}]}, "needs facebook_positions"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"], "instagram_positions": ["story"]}]}, "does not include instagram"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [
        {"image_hash": "a", "publisher_platforms": ["facebook"], "facebook_positions": ["feed", "story"]},
        {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["story"]},
    ]}, "must not overlap"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["oculus"], "oculus_positions": ["x"]}]}, "unknown keys"),
    ({"object_story_id": "111_222", "message": "new text"}, "does not accept message"),
    ({"object_story_id": "111_222", "link_url": "https://x"}, "does not accept link_url"),
    ({"object_story_id": "abc"}, "digits on both sides"),
    ({"object_story_id": "111_222", "page_id": "999"}, "different Page"),
    ({"source_instagram_media_id": "4", "page_id": "111"}, "instagram_account_id is required"),
    ({"source_instagram_media_id": "4", "instagram_account_id": "3"}, "page_id is required"),
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "image_hash": "h"}, "does not accept image_hash"),
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "call_to_action_type": "SHOP_NOW"}, "needs link_url"),
    ({"source_instagram_media_id": "4", "page_id": "111", "partnership_ad_format": 1}, "sponsor"),
    ({"source_instagram_media_id": "4", "object_story_id": "1_2"}, "Use only one"),
    ({"partnership_ad_code": "c", "page_id": "111"}, "sponsor"),
    ({"partnership_ad_code": "c", "page_id": "111", "sponsor_page_id": "2", "link_url": "https://x"}, "does not accept link_url"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "partnership_ad_format": 4}, "partnership_ad_format"),
    ({"page_id": "111"}, "No creative format selected"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "message": "x" * 1025}, "1024"),
]


@pytest.mark.parametrize(("arguments", "message"), INVALID_COMBINATIONS)
def test_invalid_combinations_raise_before_any_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="Creative", **arguments))


SECRET = "synthetic-media-password"
LOGIN_URL = f"https://user:{SECRET}@example.invalid/asset.png"
LOGIN_URL_CASES = [
    {"page_id": "20", "image_url": LOGIN_URL, "link_url": "https://example.invalid/product"},
    {"page_id": "20", "image_hash": "h", "link_url": LOGIN_URL},
    {"page_id": "20", "image_hash": "h", "link_url": f"https://{SECRET}@example.invalid/"},
    {"page_id": "20", "video_id": "5", "image_url": LOGIN_URL, "link_url": "https://example.invalid", "primary_texts": ["a"]},
    {"page_id": "20", "link_url": "https://example.invalid", "cards": [{"image_hash": "a"}, {"image_url": LOGIN_URL}]},
    {"page_id": "20", "link_url": "https://example.invalid", "cards": [{"image_hash": "a"}, {"image_hash": "b", "link": LOGIN_URL}]},
    {"page_id": "20", "link_url": "https://example.invalid", "placement_customizations": [
        {"image_url": LOGIN_URL, "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}, {"image_hash": "b"},
    ]},
    {"source_instagram_media_id": "4", "page_id": "20", "instagram_account_id": "3", "link_url": LOGIN_URL},
]


@pytest.mark.parametrize("arguments", LOGIN_URL_CASES)
def test_g20_dry_run_request_never_exposes_url_password_creative(monkeypatch, arguments) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as info:
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="10", name="Creative", dry_run=True, **arguments))
    assert SECRET not in str(info.value)
    assert "username or password" in str(info.value)


@pytest.mark.parametrize(
    ("failure", "unknown"),
    [
        (MetaApiError("Synthetic lost response", mutation_outcome_unknown=True), True),
        (httpx.ConnectError("synthetic"), True),
        (RateLimitError("Synthetic limit", code=4), False),
        (MetaApiError("Invalid parameter", code=100), False),
    ],
)
def test_create_failure_states_whether_a_creative_may_exist(monkeypatch, failure, unknown) -> None:
    class Failing(FakeClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            raise failure

    _patch(monkeypatch, Failing())
    with pytest.raises((MetaApiError, RateLimitError)) as info:
        asyncio.run(cf.create_ad_creative_from_inputs(
            account_id="123", name="Spring", page_id="111", image_hash="h", link_url="https://example.com",
        ))
    message = str(info.value)
    if unknown:
        assert "may have created the creative named 'Spring'" in message
        assert info.value.mutation_outcome_unknown is True
    else:
        assert "no creative was created" in message


def test_name_is_required_and_bounded(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="name is required"):
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name=" ", page_id="1", image_hash="h", link_url="https://x"))
    with pytest.raises(ValidationError, match="100"):
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="n" * 101, page_id="1", image_hash="h", link_url="https://x"))


def test_mcp_boundary_rejects_non_numeric_ids() -> None:
    from meta_ads_mcp.coordinator import mcp_server

    with pytest.raises(Exception, match="pattern|string_pattern_mismatch|should match"):
        asyncio.run(mcp_server.call_tool(
            "create_ad_creative_from_inputs",
            {"account_id": "act_123", "name": "C", "page_id": "page_one", "image_hash": "h", "link_url": "https://x", "dry_run": True},
        ))


# update_ad_creative_enhancements


AD = {"id": "10", "name": "Ad", "account_id": "123", "effective_status": "ACTIVE", "creative": {"id": "20"}}
CREATIVE = {
    "id": "20",
    "name": "Spring",
    "account_id": "123",
    "object_story_spec": {"page_id": "111", "link_data": {"link": "https://example.com", "image_hash": "h1"}},
    "object_story_id": "111_999",
    "url_tags": "utm_source=meta",
    "degrees_of_freedom_spec": {
        "creative_features_spec": {"image_touchups": {"enroll_status": "OPT_IN"}, "text_optimizations": {"enroll_status": "OPT_IN"}},
    },
    "effective_object_story_id": "111_999",
}


def test_enhancements_copy_creative_change_features_and_attach(monkeypatch) -> None:
    client = FakeClient(objects={"10": AD, "20": CREATIVE})
    _patch(monkeypatch, client)

    result = asyncio.run(cf.update_ad_creative_enhancements(
        ad_id="10", features={"image_touchups": "opt_out", "enhance_cta": "OPT_IN"},
    ))

    get_ad, get_creative, create, attach = client.calls
    assert get_ad[:2] == ("get", "10")
    assert get_creative == ("get", "20", cf.ENHANCEMENT_READ_FIELDS)
    assert create[:3] == ("create", "act_123", "adcreatives")
    assert create[3] == {
        "name": "Spring (enhancements updated)",
        "object_story_spec": CREATIVE["object_story_spec"],
        "url_tags": "utm_source=meta",
        "degrees_of_freedom_spec": {"creative_features_spec": {
            "image_touchups": {"enroll_status": "OPT_OUT"},
            "text_optimizations": {"enroll_status": "OPT_IN"},
            "enhance_cta": {"enroll_status": "OPT_IN"},
        }},
    }
    assert attach == ("update", "10", {"creative": {"creative_id": "999"}})
    assert result["previous"] == {"creative_id": "20", "creative_features": {"image_touchups": "OPT_IN", "text_optimizations": "OPT_IN"}}
    assert result["current"]["creative_id"] == "999"
    assert result["current"]["creative_features"]["image_touchups"] == "OPT_OUT"
    assert result["fields_not_copied"] == ["effective_object_story_id", "object_story_id"]
    assert any("review" in note for note in result["notes"])


def test_enhancements_dry_run_reads_but_writes_nothing(monkeypatch) -> None:
    client = FakeClient(objects={"10": AD, "20": CREATIVE})
    _patch(monkeypatch, client)
    result = asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}, dry_run=True))
    assert [call[0] for call in client.calls] == ["get", "get"]
    assert result["sent"] is False
    assert result["would_send"][0]["path"] == "act_123/adcreatives"
    assert result["would_send"][1] == {"method": "POST", "path": "10", "data": {"creative": {"creative_id": "<new creative id>"}}}
    assert result["proposed_features"]["image_touchups"] == "OPT_OUT"


def test_enhancements_attach_failure_names_the_new_creative(monkeypatch) -> None:
    client = FakeClient(objects={"10": AD, "20": CREATIVE}, fail_update=MetaApiError(message="Ad is archived", status_code=400, code=100))
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError, match="Creative 999 was created but ad 10 still uses creative 20"):
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))


def test_g10_later_rate_limit_preserves_created_object_id_enhancements(monkeypatch) -> None:
    client = FakeClient(objects={"10": AD, "20": CREATIVE}, fail_update=RateLimitError("Synthetic limit", code=4))
    _patch(monkeypatch, client)
    with pytest.raises(RateLimitError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    message = str(info.value)
    assert "999" in message
    assert "still uses creative 20" in message
    assert "Synthetic limit" in message


def test_g10_uncertain_enhancement_attach_does_not_claim_old_creative(monkeypatch) -> None:
    client = FakeClient(
        objects={"10": AD, "20": {"id": "20", "object_story_id": "30_40"}},
        fail_update=MetaApiError("Synthetic lost response", mutation_outcome_unknown=True),
    )
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    message = str(info.value)
    assert "999" in message
    assert "still uses creative 20" not in message
    assert "delete_creative" not in message.split("before")[0]
    assert "unknown" in message
    assert "get_ad" in message
    assert info.value.mutation_outcome_unknown is True


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("synthetic"), RuntimeError("synthetic transport")])
def test_g10_unexpected_attach_failure_names_new_creative_and_unknown_state(monkeypatch, failure) -> None:
    client = FakeClient(objects={"10": AD, "20": CREATIVE}, fail_update=failure)
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    message = str(info.value)
    assert "999" in message
    assert "still uses creative 20" not in message
    assert "unknown" in message
    assert info.value.mutation_outcome_unknown is True


def test_g10_attach_not_found_names_new_creative(monkeypatch) -> None:
    client = FakeClient(objects={"10": AD, "20": CREATIVE}, fail_update=NotFoundError("Meta object or edge not found: 10"))
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    assert "999" in str(info.value)
    assert info.value.mutation_outcome_unknown is False


def test_g10_uncertain_creative_create_says_a_copy_may_exist_and_ad_is_unchanged(monkeypatch) -> None:
    class LostCreate(FakeClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            self.calls.append(("create", parent_id, edge, data))
            raise MetaApiError("Meta API request failed before a response was received.", mutation_outcome_unknown=True)

    client = LostCreate(objects={"10": AD, "20": CREATIVE})
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    message = str(info.value)
    assert "Spring (enhancements updated)" in message
    assert "may" in message
    assert "still uses creative 20" in message
    assert [call[0] for call in client.calls] == ["get", "get", "create"]


def test_enhancements_refuse_a_creative_that_cannot_be_copied(monkeypatch) -> None:
    client = FakeClient(objects={"10": AD, "20": {"id": "20", "name": "Bare", "url_tags": "x=1"}})
    _patch(monkeypatch, client)
    with pytest.raises(ValidationError, match="cannot be built"):
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    assert [call[0] for call in client.calls] == ["get", "get"]


@pytest.mark.parametrize(
    ("features", "message"),
    [
        ({}, "at least one"),
        ({"make_it_pop": "OPT_IN"}, "not one of Meta's"),
        ({"image_touchups": "ON"}, "OPT_IN or OPT_OUT"),
    ],
)
def test_enhancements_validation_errors_make_no_call(monkeypatch, features, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features=features))


@pytest.mark.parametrize(
    "query",
    [
        "build a carousel creative", "upload a video", "click to whatsapp creative", "turn off advantage creative enhancements",
        "build a catalog carousel creative", "multi media creative", "facebook partnership ad creative", "asset pool creative",
    ],
)
def test_creative_building_requests_route_to_the_new_intent(query) -> None:
    from meta_ads_mcp.tools import utility

    result = asyncio.run(utility.get_capabilities(intent=query))
    assert result["closest_intents"][0]["intent"] == "build_creatives_and_upload_media"


def test_creative_writes_appear_in_mutation_common_paths(monkeypatch) -> None:
    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.tools import utility

    monkeypatch.delenv("META_READ_ONLY", raising=False)
    reload_settings()
    result = asyncio.run(utility.list_mutation_tools())
    assert result["common_paths"]["creative_building"] == ["create_ad_creative_from_inputs", "update_ad_creative_enhancements"]
    assert result["common_paths"]["media_library"] == ["upload_creative_asset", "upload_ad_video", "delete_ad_image"]


# Round three: asset pool, catalog carousel, Facebook Partnership Ads, partnership from new media, image URLs


def test_multi_media_image_pool_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", instagram_account_id="222", link_url="https://example.com",
        call_to_action_type="SHOP_NOW", primary_texts=["A", "B"], headlines=["H1", "H2"], description="D",
        multi_media=[{"image_hash": "h1"}, {"image_url": "https://cdn.example.com/2.png"}, {"image_hash": "h3"}],
    )
    assert data == {
        "name": "Creative",
        "object_story_spec": {
            "page_id": "111",
            "instagram_user_id": "222",
            "link_data": {
                "link": "https://example.com",
                "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://example.com"}},
                "image_hash": "h1",
                "message": "A",
                "name": "H1",
                "description": "D",
            },
        },
        "media_sourcing_spec": {
            "images": [
                {"hash": "h1", "source": "multi_media", "opt_in_status": "opt_in"},
                {"url": "https://cdn.example.com/2.png", "source": "multi_media", "opt_in_status": "opt_in"},
                {"hash": "h3", "source": "multi_media", "opt_in_status": "opt_in"},
            ],
            "bodies": [{"text": "A"}, {"text": "B"}],
            "titles": [{"text": "H1"}, {"text": "H2"}],
        },
    }
    assert result["format"] == "multi_media"


def test_multi_media_mixed_pool_with_video_primary_spec(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", link_url="https://example.com", message="Body", headline="Title",
        multi_media=[
            {"video_id": "55", "thumbnail_url": "https://cdn.example.com/t.png"},
            {"image_hash": "h2"},
            {"video_id": "66"},
        ],
    )
    assert data == {
        "name": "Creative",
        "object_story_spec": {
            "page_id": "111",
            "video_data": {
                "video_id": "55",
                "image_url": "https://cdn.example.com/t.png",
                "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com"}},
                "message": "Body",
                "title": "Title",
            },
        },
        "media_sourcing_spec": {
            "images": [{"hash": "h2", "source": "multi_media", "opt_in_status": "opt_in"}],
            "videos": [
                {
                    "video_id": "55", "original_video_id": "55", "source": "multi_media", "opt_in_status": "opt_in",
                    "thumbnail_url": "https://cdn.example.com/t.png", "thumbnail_source": "custom",
                },
                {"video_id": "66", "original_video_id": "66", "source": "multi_media", "opt_in_status": "opt_in"},
            ],
        },
    }


def test_multi_media_dry_run_returns_spec_and_sends_nothing(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Pool", page_id="111", link_url="https://example.com",
        multi_media=[{"image_hash": "h1"}, {"image_hash": "h2"}], dry_run=True,
    ))
    assert result["sent"] is False
    assert result["format"] == "multi_media"
    assert result["would_send"]["data"]["media_sourcing_spec"]["images"][1]["hash"] == "h2"


def test_catalog_carousel_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", instagram_account_id="222", product_set_id="777",
        link_url="https://shop.example.com", call_to_action_type="SHOP_NOW",
        message="Shop {{product.name}}", headline="{{product.price}}", description="{{product.description}}",
        url_tags="utm_source=meta",
    )
    assert data == {
        "name": "Creative",
        "product_set_id": "777",
        "object_story_spec": {
            "page_id": "111",
            "instagram_user_id": "222",
            "template_data": {
                "link": "https://shop.example.com",
                "call_to_action": {"type": "SHOP_NOW"},
                "message": "Shop {{product.name}}",
                "name": "{{product.price}}",
                "description": "{{product.description}}",
            },
        },
        "url_tags": "utm_source=meta",
    }
    assert result["format"] == "catalog_carousel"


def test_catalog_carousel_dry_run_sends_nothing(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Catalog", page_id="111", product_set_id="777", link_url="https://shop.example.com", dry_run=True,
    ))
    assert result["sent"] is False
    assert result["would_send"]["data"]["object_story_spec"]["template_data"]["call_to_action"] == {"type": "LEARN_MORE"}


def test_facebook_partnership_ad_code_spec_is_sent_but_not_echoed(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, facebook_partnership_ad_code="FBSECRETCODE", sponsor_page_id="222", sponsor_instagram_account_id="333",
        partnership_ad_format=1, link_url="https://example.com", call_to_action_type="SHOP_NOW",
    )
    assert data == {
        "name": "Creative",
        "branded_content": {"facebook_boost_post_access_token": "FBSECRETCODE", "ad_format": 1},
        "facebook_branded_content": {"sponsor_page_id": "222"},
        "instagram_branded_content": {"sponsor_id": "333"},
        "call_to_action": {"type": "SHOP_NOW", "value": {"link": "https://example.com"}},
    }
    assert "FBSECRETCODE" not in json.dumps(result)
    assert result["format"] == "facebook_partnership_ad_code"


def test_facebook_partnership_ad_code_dry_run_hides_code(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="FB", facebook_partnership_ad_code="FBSECRETCODE", sponsor_page_id="222", dry_run=True,
    ))
    assert "FBSECRETCODE" not in json.dumps(result)
    assert result["would_send"]["data"]["branded_content"] == {"facebook_boost_post_access_token": "<partnership_ad_code>"}


def test_facebook_partnership_existing_post_spec(monkeypatch) -> None:
    result, data = _create(monkeypatch, object_story_id="900_901", sponsor_page_id="222", partnership_ad_format=3)
    assert data == {
        "name": "Creative",
        "object_story_id": "900_901",
        "facebook_branded_content": {"sponsor_page_id": "222"},
        "branded_content": {"ad_format": 3},
    }
    assert result["format"] == "facebook_partnership_post"


def test_existing_page_post_without_sponsor_is_unchanged(monkeypatch) -> None:
    result, data = _create(monkeypatch, object_story_id="111_222", page_id="111")
    assert data == {"name": "Creative", "object_story_id": "111_222"}
    assert result["format"] == "page_post"


@pytest.mark.parametrize(
    ("arguments", "media_key", "expected_media"),
    [
        ({"image_hash": "h1"}, "link_data", {"image_hash": "h1"}),
        ({"video_id": "555", "image_hash": "t"}, "video_data", {"video_id": "555", "image_hash": "t"}),
    ],
)
def test_partnership_from_new_media_spec(monkeypatch, arguments, media_key, expected_media) -> None:
    _, data = _create(
        monkeypatch, page_id="111", link_url="https://example.com", sponsor_page_id="222",
        sponsor_instagram_account_id="333", **arguments,
    )
    assert data["facebook_branded_content"] == {"sponsor_page_id": "222"}
    assert data["instagram_branded_content"] == {"sponsor_id": "333"}
    assert data["object_story_spec"]["page_id"] == "111"
    for key, value in expected_media.items():
        assert data["object_story_spec"][media_key][key] == value
    assert set(data) == {"name", "object_story_spec", "facebook_branded_content", "instagram_branded_content"}


def test_text_variants_accept_image_url(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", image_url="https://cdn.example.com/a.png", link_url="https://example.com",
        primary_texts=["A", "B"],
    )
    assert data["asset_feed_spec"]["images"] == [{"url": "https://cdn.example.com/a.png"}]
    assert data["asset_feed_spec"]["ad_formats"] == ["SINGLE_IMAGE"]


def test_placement_customization_accepts_image_url(monkeypatch) -> None:
    _, data = _create(
        monkeypatch, page_id="111", link_url="https://example.com",
        placement_customizations=[
            {"image_url": "https://cdn.example.com/feed.png", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]},
            {"image_hash": "fallback"},
        ],
    )
    assert data["asset_feed_spec"]["images"] == [
        {"url": "https://cdn.example.com/feed.png", "adlabels": [{"name": "placement_1"}]},
        {"hash": "fallback", "adlabels": [{"name": "placement_2"}]},
    ]


ROUND_THREE_INVALID = [
    # asset pool
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a"}]}, "2 to 10"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": f"h{i}"} for i in range(11)]}, "2 to 10"),
    ({"page_id": "111", "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}]}, "link_url is required"),
    ({"link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}]}, "page_id is required"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "a"}]}, "repeats media"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a", "video_id": "1"}, {"image_hash": "b"}]}, "exactly one of"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a", "image_url": "https://x/a.png"}, {"image_hash": "b"}]}, "exactly one of"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "b", "colour": "red"}]}, "unknown keys"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a", "thumbnail_url": "https://x/t.png"}, {"image_hash": "b"}]}, "thumbnail"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"video_id": "1"}, {"image_hash": "b"}]}, "thumbnail"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "b"}, {"video_id": "1", "thumbnail_hash": "t"}]}, "thumbnail_url"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "b"}, {"video_id": "abc"}]}, "numeric"),
    ({"page_id": "111", "link_url": "https://x", "image_hash": "h", "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}]}, "does not accept image_hash"),
    ({"page_id": "111", "link_url": "https://x", "cards": [{"image_hash": "a"}, {"image_hash": "b"}], "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}]}, "Use only one"),
    ({"page_id": "111", "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}], "call_to_action_type": "WHATSAPP_MESSAGE"}, "not built"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}], "lead_gen_form_id": "8"}, "lead_gen_form_id"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}], "sponsor_page_id": "2"}, "Partnership"),
    # catalog carousel
    ({"page_id": "111", "product_set_id": "7"}, "link_url is required"),
    ({"product_set_id": "7", "link_url": "https://x"}, "page_id is required"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://x", "image_hash": "h"}, "does not accept image_hash"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://x", "video_id": "5"}, "does not accept video_id"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://x", "primary_texts": ["a"]}, "cannot be combined with product_set_id"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://www.facebook.com/shop"}, "Facebook URL"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://x", "call_to_action_type": "MESSAGE_PAGE"}, "not built"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://x", "lead_gen_form_id": "8"}, "lead_gen_form_id"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://x", "sponsor_page_id": "2"}, "Partnership"),
    ({"page_id": "111", "product_set_id": "7", "link_url": "https://x", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "Use only one"),
    # Facebook Partnership Ads
    ({"facebook_partnership_ad_code": "c"}, "sponsor_page_id"),
    ({"facebook_partnership_ad_code": "c", "sponsor_instagram_account_id": "3"}, "sponsor_page_id"),
    ({"facebook_partnership_ad_code": "c", "sponsor_page_id": "2", "image_hash": "h"}, "does not accept image_hash"),
    ({"facebook_partnership_ad_code": "c", "sponsor_page_id": "2", "message": "m"}, "does not accept message"),
    ({"facebook_partnership_ad_code": "c", "sponsor_page_id": "2", "call_to_action_type": "SHOP_NOW"}, "needs link_url"),
    ({"facebook_partnership_ad_code": "c", "sponsor_page_id": "2", "call_to_action_type": "WHATSAPP_MESSAGE"}, "not built"),
    ({"facebook_partnership_ad_code": "c", "partnership_ad_code": "d", "sponsor_page_id": "2"}, "Use only one"),
    ({"facebook_partnership_ad_code": "c", "object_story_id": "1_2", "sponsor_page_id": "2"}, "Use only one"),
    ({"object_story_id": "1_2", "sponsor_instagram_account_id": "3"}, "sponsor_page_id"),
    ({"object_story_id": "1_2", "partnership_ad_format": 1}, "sponsor_page_id"),
    ({"object_story_id": "1_2", "sponsor_page_id": "2", "link_url": "https://x"}, "does not accept link_url"),
    ({"object_story_id": "1_2", "sponsor_page_id": "2", "image_hash": "h"}, "does not accept image_hash"),
    # Partnership from new media
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "sponsor_page_id": "2", "partnership_ad_format": 1}, "partnership_ad_format"),
    ({"page_id": "111", "link_url": "https://x", "sponsor_page_id": "2", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "single image or single video"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "sponsor_page_id": "2", "primary_texts": ["a", "b"]}, "single image or single video"),
    ({"page_id": "111", "link_url": "https://x", "sponsor_page_id": "2", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "single image or single video"),
    # image URLs in asset feeds
    ({"page_id": "111", "link_url": "https://x", "primary_texts": ["a"], "image_hash": "h", "image_url": "https://x/y.png"}, "not both"),
    ({"page_id": "111", "link_url": "https://x", "primary_texts": ["a"], "image_url": "ftp://x/y.png"}, "full http"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_url": "https://x/a.png", "image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "exactly one of"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_url": "x.png"}, {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "full http"),
    # Mixed placement sets stay refused: Meta's guide documents one ad format per asset feed.
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_url": "https://x/a.png"}, {"video_id": "1", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "all images or all videos"),
]


@pytest.mark.parametrize(("arguments", "message"), ROUND_THREE_INVALID)
def test_round_three_invalid_combinations_raise_before_any_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="Creative", **arguments))


def test_multi_media_login_url_is_refused(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as info:
        asyncio.run(cf.create_ad_creative_from_inputs(
            account_id="123", name="Creative", page_id="111", link_url="https://x", dry_run=True,
            multi_media=[{"image_hash": "a"}, {"video_id": "1", "thumbnail_url": LOGIN_URL}],
        ))
    assert SECRET not in str(info.value)


def test_mcp_boundary_accepts_new_format_inputs() -> None:
    from meta_ads_mcp.coordinator import mcp_server

    result = asyncio.run(mcp_server.call_tool(
        "create_ad_creative_from_inputs",
        {"account_id": "123", "name": "C", "page_id": "111", "product_set_id": "777", "link_url": "https://x.example", "dry_run": True},
    ))
    assert "template_data" in str(result)
