"""Tests for create_ad_creative_from_inputs and update_ad_creative_enhancements."""

from __future__ import annotations

import asyncio
from typing import Any

import json
from copy import deepcopy

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
    ({"page_id": "111", "link_url": "https://x", "cards": [{"image_hash": "a"}, {"image_hash": "b", "colour": "red"}]}, "unknown key"),
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
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "a", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]}, "repeats the media"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["facebook"]}]}, "needs facebook_positions"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"], "instagram_positions": ["story"]}]}, "does not include instagram"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [
        {"image_hash": "a", "publisher_platforms": ["facebook"], "facebook_positions": ["feed", "story"]},
        {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["story"]},
    ]}, "must not overlap"),
    ({"page_id": "111", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": ["oculus"], "oculus_positions": ["x"]}]}, "unknown key"),
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
    ({"partnership_ad_code": "AQSYNTHETICCODE1", "page_id": "111"}, "sponsor"),
    ({"partnership_ad_code": "AQSYNTHETICCODE1", "page_id": "111", "sponsor_page_id": "2", "link_url": "https://x"}, "does not accept link_url"),
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
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "a"}]}, "repeats the media"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a", "video_id": "1"}, {"image_hash": "b"}]}, "exactly one of"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a", "image_url": "https://x/a.png"}, {"image_hash": "b"}]}, "exactly one of"),
    ({"page_id": "111", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "b", "colour": "red"}]}, "unknown key"),
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
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1"}, "sponsor_page_id"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "sponsor_instagram_account_id": "3"}, "sponsor_page_id"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "sponsor_page_id": "2", "image_hash": "h"}, "does not accept image_hash"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "sponsor_page_id": "2", "message": "m"}, "does not accept message"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "sponsor_page_id": "2", "call_to_action_type": "SHOP_NOW"}, "needs link_url"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "sponsor_page_id": "2", "call_to_action_type": "WHATSAPP_MESSAGE"}, "not built"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "partnership_ad_code": "AQSYNTHETICCODE2", "sponsor_page_id": "2"}, "Use only one"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "object_story_id": "1_2", "sponsor_page_id": "2"}, "Use only one"),
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


# Round four: new forms (click-to-Instagram carousel, click-to-message on an Instagram post,
# display_link, product extensions)


def test_click_to_instagram_carousel_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", instagram_account_id="222", call_to_action_type="INSTAGRAM_MESSAGE",
        message="Ask us", page_welcome_message="Hi there",
        cards=[
            {"image_hash": "h1", "headline": "One", "description": "First"},
            {"image_url": "https://cdn.example.com/t.png", "video_id": "77", "headline": "Two", "call_to_action_type": "instagram_message"},
        ],
    )
    button = {"type": "INSTAGRAM_MESSAGE", "value": {"app_destination": "INSTAGRAM_DIRECT"}}
    assert data == {
        "name": "Creative",
        "object_story_spec": {
            "page_id": "111",
            "instagram_user_id": "222",
            "link_data": {
                "call_to_action": button,
                "child_attachments": [
                    {"image_hash": "h1", "name": "One", "description": "First", "call_to_action": button},
                    {"picture": "https://cdn.example.com/t.png", "video_id": "77", "name": "Two", "call_to_action": button},
                ],
                "message": "Ask us",
                "page_welcome_message": "Hi there",
            },
        },
    }
    assert result["format"] == "carousel"
    assert any("INSTAGRAM_DIRECT" in note for note in result["notes"])


def test_click_to_instagram_carousel_dry_run_sends_nothing(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="DM carousel", page_id="111", instagram_account_id="222",
        call_to_action_type="INSTAGRAM_MESSAGE", cards=[{"image_hash": "a"}, {"image_hash": "b"}], dry_run=True,
    ))
    assert result["sent"] is False
    link_data = result["would_send"]["data"]["object_story_spec"]["link_data"]
    assert "link" not in link_data
    assert [card["call_to_action"]["type"] for card in link_data["child_attachments"]] == ["INSTAGRAM_MESSAGE"] * 2


@pytest.mark.parametrize(
    ("cta", "expected_cta"),
    [
        ("WHATSAPP_MESSAGE", {"type": "WHATSAPP_MESSAGE", "value": {"link": "https://api.whatsapp.com/send", "app_destination": "WHATSAPP"}}),
        ("INSTAGRAM_MESSAGE", {"type": "INSTAGRAM_MESSAGE", "value": {"link": "https://www.instagram.com"}}),
    ],
)
def test_instagram_post_click_to_message_spec(monkeypatch, cta, expected_cta) -> None:
    result, data = _create(
        monkeypatch, source_instagram_media_id="444", page_id="111", instagram_account_id="333", call_to_action_type=cta,
    )
    assert data == {
        "name": "Creative",
        "object_id": "111",
        "source_instagram_media_id": "444",
        "instagram_user_id": "333",
        "call_to_action": expected_cta,
    }
    assert result["format"] == "instagram_post"
    assert any("destination_type" in note for note in result["notes"])


def test_instagram_post_click_to_message_dry_run_sends_nothing(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Post", source_instagram_media_id="444", page_id="111", instagram_account_id="333",
        call_to_action_type="WHATSAPP_MESSAGE", dry_run=True,
    ))
    assert result["sent"] is False
    assert result["would_send"]["data"]["call_to_action"]["value"]["app_destination"] == "WHATSAPP"


DISPLAY_LINK_CASES = [
    (
        {"page_id": "111", "image_hash": "h1", "link_url": "https://example.com/p"},
        lambda data: data["object_story_spec"]["link_data"]["caption"],
    ),
    (
        {"page_id": "111", "video_id": "555", "image_hash": "t", "link_url": "https://example.com/p"},
        lambda data: data["object_story_spec"]["video_data"]["call_to_action"]["value"]["link_caption"],
    ),
    (
        {"page_id": "111", "link_url": "https://example.com/p", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]},
        lambda data: data["object_story_spec"]["link_data"]["caption"],
    ),
    (
        {"page_id": "111", "image_hash": "h1", "link_url": "https://example.com/p", "primary_texts": ["A", "B"]},
        lambda data: data["asset_feed_spec"]["link_urls"][0]["display_url"],
    ),
    (
        {"page_id": "111", "link_url": "https://example.com/p", "placement_customizations": [
            {"image_hash": "a", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}, {"image_hash": "b"},
        ]},
        lambda data: data["asset_feed_spec"]["link_urls"][0]["display_url"],
    ),
    (
        {"page_id": "111", "link_url": "https://example.com/p", "multi_media": [{"image_hash": "a"}, {"video_id": "5"}]},
        lambda data: data["object_story_spec"]["link_data"]["caption"],
    ),
    (
        {"page_id": "111", "link_url": "https://example.com/p", "multi_media": [{"video_id": "5", "thumbnail_hash": "t"}, {"image_hash": "a"}]},
        lambda data: data["object_story_spec"]["video_data"]["call_to_action"]["value"]["link_caption"],
    ),
    (
        {"page_id": "111", "product_set_id": "777", "link_url": "https://shop.example.com"},
        lambda data: data["object_story_spec"]["template_data"]["caption"],
    ),
]


@pytest.mark.parametrize(("arguments", "where"), DISPLAY_LINK_CASES)
def test_display_link_is_sent_as_metas_caption(monkeypatch, arguments, where) -> None:
    _, data = _create(monkeypatch, display_link="Example.com", **arguments)
    assert where(data) == "Example.com"
    assert "Example.com" in json.dumps(data)
    assert json.dumps(data).count("Example.com") == 1


def test_display_link_exact_single_image_spec_and_dry_run(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Spring", page_id="111", image_hash="h1", link_url="https://example.com/p",
        display_link="https://example.com", dry_run=True,
    ))
    assert result["would_send"]["data"]["object_story_spec"]["link_data"] == {
        "link": "https://example.com/p",
        "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/p"}},
        "image_hash": "h1",
        "caption": "https://example.com",
    }
    assert result["sent"] is False


PRODUCT_EXTENSIONS = {
    "creative_sourcing_spec": {"associated_product_set_id": "888"},
    "degrees_of_freedom_spec": {"creative_features_spec": {
        "product_extensions": {"enroll_status": "OPT_IN", "action_metadata": {"type": "MANUAL"}},
    }},
}


def test_product_extensions_single_image_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", image_hash="h1", link_url="https://example.com/p", product_extensions_product_set_id="888",
    )
    assert data == {
        "name": "Creative",
        "object_story_spec": {
            "page_id": "111",
            "link_data": {
                "link": "https://example.com/p",
                "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/p"}},
                "image_hash": "h1",
            },
        },
        **PRODUCT_EXTENSIONS,
    }
    assert result["format"] == "single_image"
    assert any("Sales or Traffic" in note for note in result["notes"])


def test_product_extensions_single_video_spec_and_dry_run(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Video", page_id="111", video_id="555", image_hash="t", link_url="https://example.com/p",
        product_extensions_product_set_id="888", dry_run=True,
    ))
    data = result["would_send"]["data"]
    assert {key: data[key] for key in PRODUCT_EXTENSIONS} == PRODUCT_EXTENSIONS
    assert data["object_story_spec"]["video_data"]["video_id"] == "555"
    assert result["sent"] is False


ROUND_FOUR_INVALID = [
    # click-to-message carousels
    ({"page_id": "111", "instagram_account_id": "2", "call_to_action_type": "WHATSAPP_MESSAGE", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "Instagram Direct"),
    ({"page_id": "111", "call_to_action_type": "MESSAGE_PAGE", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "Instagram Direct"),
    ({"page_id": "111", "call_to_action_type": "INSTAGRAM_MESSAGE", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "instagram_account_id"),
    ({"page_id": "111", "instagram_account_id": "2", "call_to_action_type": "INSTAGRAM_MESSAGE", "link_url": "https://x", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "link_url is not used"),
    ({"page_id": "111", "instagram_account_id": "2", "call_to_action_type": "INSTAGRAM_MESSAGE", "cards": [{"image_hash": "a", "link": "https://x"}, {"image_hash": "b"}]}, "cards\\[1\\] link is not used"),
    ({"page_id": "111", "instagram_account_id": "2", "call_to_action_type": "INSTAGRAM_MESSAGE", "cards": [{"image_hash": "a"}, {"image_hash": "b", "call_to_action_type": "SHOP_NOW"}]}, "same messaging surface"),
    ({"page_id": "111", "instagram_account_id": "2", "call_to_action_type": "INSTAGRAM_MESSAGE", "cards": [{"image_hash": "a"}, {"video_id": "7"}]}, "needs image_hash"),
    ({"page_id": "111", "instagram_account_id": "2", "call_to_action_type": "INSTAGRAM_MESSAGE", "display_link": "example.com", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "display_link"),
    ({"page_id": "111", "link_url": "https://x", "cards": [{"image_hash": "a"}, {"image_hash": "b", "call_to_action_type": "INSTAGRAM_MESSAGE"}]}, "top level and on every card"),
    ({"page_id": "111", "link_url": "https://x", "page_welcome_message": "hi", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "INSTAGRAM_MESSAGE"),
    ({"page_id": "111", "instagram_account_id": "2", "call_to_action_type": "INSTAGRAM_MESSAGE", "sponsor_page_id": "5", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "single image or single video"),
    # click-to-message on an existing Instagram post
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "call_to_action_type": "WHATSAPP_MESSAGE", "link_url": "https://x"}, "link_url is not used"),
    ({"source_instagram_media_id": "4", "page_id": "111", "sponsor_page_id": "5", "call_to_action_type": "WHATSAPP_MESSAGE"}, "Partnership"),
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "call_to_action_type": "WHATSAPP_MESSAGE", "page_welcome_message": "hi"}, "does not accept page_welcome_message"),
    ({"source_instagram_media_id": "4", "page_id": "111", "call_to_action_type": "INSTAGRAM_MESSAGE"}, "instagram_account_id is required"),
    # display_link
    ({"page_id": "111", "image_hash": "h", "display_link": "example.com", "call_to_action_type": "WHATSAPP_MESSAGE"}, "display_link needs link_url"),
    ({"page_id": "111", "image_hash": "h", "display_link": "example.com", "lead_gen_form_id": "8"}, "display_link needs link_url"),
    ({"page_id": "111", "video_id": "5", "image_hash": "t", "display_link": "example.com"}, "display_link needs link_url"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "display_link": "Shop now"}, "must be a URL or a domain"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "display_link": "ftp://example.com"}, "must be a URL or a domain"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "display_link": "https://user:pw@example.com"}, "username or password"),
    ({"object_story_id": "111_222", "display_link": "example.com"}, "does not accept display_link"),
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "link_url": "https://x", "display_link": "example.com"}, "does not accept display_link"),
    ({"partnership_ad_code": "AQSYNTHETICCODE1", "page_id": "111", "sponsor_page_id": "2", "display_link": "example.com"}, "does not accept display_link"),
    ({"facebook_partnership_ad_code": "AQSYNTHETICCODE1", "sponsor_page_id": "2", "link_url": "https://x", "display_link": "example.com"}, "does not accept display_link"),
    # product extensions
    ({"page_id": "111", "link_url": "https://x", "product_extensions_product_set_id": "8", "cards": [{"image_hash": "a"}, {"image_hash": "b"}]}, "single image or single video only"),
    ({"page_id": "111", "link_url": "https://x", "product_extensions_product_set_id": "8", "product_set_id": "7"}, "product_set_id builds a catalog carousel"),
    ({"page_id": "111", "link_url": "https://x", "product_extensions_product_set_id": "8", "image_hash": "h", "primary_texts": ["a", "b"]}, "single image or single video only"),
    ({"page_id": "111", "link_url": "https://x", "product_extensions_product_set_id": "8", "multi_media": [{"image_hash": "a"}, {"image_hash": "b"}]}, "single image or single video only"),
    ({"page_id": "111", "image_hash": "h", "product_extensions_product_set_id": "8"}, "needs link_url"),
    ({"page_id": "111", "image_hash": "h", "product_extensions_product_set_id": "8", "call_to_action_type": "WHATSAPP_MESSAGE"}, "needs link_url"),
    ({"page_id": "111", "image_hash": "h", "product_extensions_product_set_id": "8", "lead_gen_form_id": "9"}, "needs link_url"),
    ({"page_id": "111", "image_hash": "h", "link_url": "https://x", "product_extensions_product_set_id": "8", "sponsor_page_id": "2"}, "Partnership"),
    ({"object_story_id": "111_222", "product_extensions_product_set_id": "8"}, "single image or single video only"),
]


@pytest.mark.parametrize(("arguments", "message"), ROUND_FOUR_INVALID)
def test_round_four_invalid_combinations_raise_before_any_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="Creative", **arguments))


def test_mcp_boundary_rejects_non_numeric_product_extensions_set() -> None:
    from meta_ads_mcp.coordinator import mcp_server

    with pytest.raises(Exception, match="pattern|string_pattern_mismatch|should match|product_extensions_product_set_id"):
        asyncio.run(mcp_server.call_tool(
            "create_ad_creative_from_inputs",
            {"account_id": "123", "name": "C", "page_id": "111", "image_hash": "h", "link_url": "https://x.example",
             "product_extensions_product_set_id": "set one", "dry_run": True},
        ))


# H17: an enhancement copy keeps every part of a creative this server built.


class ProjectingClient(FakeClient):
    """Serves the ad's creative with only the fields requested, as Meta does."""

    def __init__(self, creative: dict[str, Any]) -> None:
        super().__init__(objects={"10": AD})
        self.creative = creative

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields))
        if object_id == "20":
            return {key: deepcopy(value) for key, value in self.creative.items() if fields is None or key in fields}
        return dict(self.objects.get(object_id, {"id": object_id}))


ROUND_TRIP_SHAPES = {
    "single_image": {"page_id": "111", "instagram_account_id": "222", "image_hash": "h1", "link_url": "https://example.com/p",
                     "message": "Body", "headline": "Head", "description": "Desc", "url_tags": "utm_source=meta",
                     "display_link": "example.com"},
    "single_video": {"page_id": "111", "video_id": "555", "image_url": "https://cdn.example.com/t.png",
                     "link_url": "https://example.com", "headline": "Title", "display_link": "example.com"},
    "carousel": {"page_id": "111", "link_url": "https://example.com", "message": "Pick", "display_link": "example.com",
                 "cards": [{"image_hash": "h1", "headline": "One"}, {"image_hash": "h2", "video_id": "77", "call_to_action_type": "SHOP_NOW"}]},
    "click_to_instagram_carousel": {"page_id": "111", "instagram_account_id": "222", "call_to_action_type": "INSTAGRAM_MESSAGE",
                                    "page_welcome_message": "Hi", "cards": [{"image_hash": "h1"}, {"image_hash": "h2", "video_id": "77"}]},
    "text_variants": {"page_id": "111", "image_hash": "h1", "link_url": "https://example.com", "primary_texts": ["A", "B"],
                      "headlines": ["H1", "H2"], "display_link": "example.com"},
    "placement_customizations": {"page_id": "111", "link_url": "https://example.com", "message": "Body", "placement_customizations": [
        {"image_hash": "feed", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}, {"image_hash": "fallback"}]},
    "page_post": {"object_story_id": "111_222", "page_id": "111", "instagram_account_id": "333"},
    "instagram_post": {"source_instagram_media_id": "444", "page_id": "111", "instagram_account_id": "333",
                       "link_url": "https://example.com", "call_to_action_type": "SHOP_NOW"},
    "instagram_post_whatsapp": {"source_instagram_media_id": "444", "page_id": "111", "instagram_account_id": "333",
                                "call_to_action_type": "WHATSAPP_MESSAGE"},
    "instagram_post_instagram_direct": {"source_instagram_media_id": "444", "page_id": "111", "instagram_account_id": "333",
                                        "call_to_action_type": "INSTAGRAM_MESSAGE"},
    "instagram_post_messenger": {"source_instagram_media_id": "444", "page_id": "111", "instagram_account_id": "333",
                                 "call_to_action_type": "MESSAGE_PAGE"},
    "click_to_instagram_video": {"page_id": "111", "instagram_account_id": "333", "video_id": "555",
                                 "image_url": "https://cdn.example.com/t.png", "message": "Chat",
                                 "call_to_action_type": "INSTAGRAM_MESSAGE", "page_welcome_message": "Hi"},
    "click_to_whatsapp_image": {"page_id": "111", "image_hash": "h1", "call_to_action_type": "WHATSAPP_MESSAGE", "page_welcome_message": "Hi"},
    "click_to_messenger_video": {"page_id": "111", "video_id": "555", "image_hash": "t", "call_to_action_type": "MESSAGE_PAGE",
                                 "description": "Chat"},
    "lead_form": {"page_id": "111", "image_hash": "h1", "lead_gen_form_id": "888", "call_to_action_type": "SIGN_UP"},
    "partnership_instagram_post": {"source_instagram_media_id": "444", "page_id": "111", "sponsor_page_id": "222",
                                   "sponsor_instagram_account_id": "333", "partnership_ad_format": 2},
    "partnership_ad_code": {"partnership_ad_code": "AQSYNTHETICCODE1", "page_id": "111", "sponsor_page_id": "222",
                            "partnership_ad_format": 1},
    "facebook_partnership_post": {"object_story_id": "900_901", "sponsor_page_id": "222", "partnership_ad_format": 3},
    "facebook_partnership_ad_code": {"facebook_partnership_ad_code": "AQSYNTHETICCODE2", "sponsor_page_id": "222",
                                     "link_url": "https://example.com", "call_to_action_type": "SHOP_NOW"},
    "partnership_new_media": {"page_id": "111", "image_hash": "h1", "link_url": "https://example.com", "sponsor_page_id": "222",
                              "sponsor_instagram_account_id": "333"},
    "multi_media": {"page_id": "111", "link_url": "https://example.com", "primary_texts": ["A", "B"], "headline": "H",
                    "display_link": "example.com",
                    "multi_media": [{"image_hash": "h1"}, {"video_id": "55", "thumbnail_url": "https://cdn.example.com/t.png"}, {"image_hash": "h3"}]},
    "catalog_carousel": {"page_id": "111", "product_set_id": "777", "link_url": "https://shop.example.com",
                         "message": "{{product.name}}", "display_link": "shop.example.com"},
    "product_extensions": {"page_id": "111", "image_hash": "h1", "link_url": "https://example.com", "product_extensions_product_set_id": "888"},
}


def _built_spec(monkeypatch, arguments: dict[str, Any]) -> dict[str, Any]:
    client = FakeClient()
    _patch(monkeypatch, client)
    asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="Built", **arguments))
    return client.calls[0][3]


@pytest.mark.parametrize("shape", sorted(ROUND_TRIP_SHAPES))
def test_h17_enhancement_copy_keeps_every_part_of_each_built_creative(monkeypatch, shape) -> None:
    built = _built_spec(monkeypatch, ROUND_TRIP_SHAPES[shape])
    client = ProjectingClient({"id": "20", "account_id": "123", **deepcopy(built)})
    _patch(monkeypatch, client)

    result = asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))

    sent = next(call[3] for call in client.calls if call[0] == "create")
    unchanged = {key: value for key, value in built.items() if key not in {"name", "degrees_of_freedom_spec"}}
    assert {key: value for key, value in sent.items() if key not in {"name", "degrees_of_freedom_spec"}} == unchanged
    expected_features = deepcopy(built.get("degrees_of_freedom_spec", {}))
    expected_features.setdefault("creative_features_spec", {})["image_touchups"] = {"enroll_status": "OPT_OUT"}
    assert sent["degrees_of_freedom_spec"] == expected_features
    assert result["fields_not_copied"] == []
    assert ("update", "10", {"creative": {"creative_id": "999"}}) in client.calls


def test_h17_enhancement_copy_preserves_multi_media_asset_pool(monkeypatch) -> None:
    pool = {
        "images": [{"hash": "secondary-image", "source": "multi_media", "opt_in_status": "opt_in"}],
        "videos": [{"video_id": "50", "original_video_id": "50", "source": "multi_media", "opt_in_status": "opt_in"}],
    }
    client = ProjectingClient({
        "id": "20", "account_id": "123", "name": "Asset pool",
        "object_story_spec": {"page_id": "40", "link_data": {"link": "https://example.invalid", "image_hash": "primary-image"}},
        "media_sourcing_spec": pool,
    })
    _patch(monkeypatch, client)
    result = asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    replacement = next(call[3] for call in client.calls if call[0] == "create")
    assert replacement.get("media_sourcing_spec") == pool, (replacement, result)


def test_h17_every_field_the_builder_writes_is_read_and_copied(monkeypatch) -> None:
    written: set[str] = set()
    for arguments in ROUND_TRIP_SHAPES.values():
        written.update(_built_spec(monkeypatch, arguments))
    written -= {"name", "degrees_of_freedom_spec"}
    assert written <= set(cf.COPYABLE_CREATIVE_FIELDS)
    assert set(cf.COPYABLE_CREATIVE_FIELDS) | {"degrees_of_freedom_spec"} <= set(cf.ENHANCEMENT_READ_FIELDS)


def test_h17_a_returned_field_the_copy_drops_is_named(monkeypatch) -> None:
    client = ProjectingClient({
        "id": "20", "account_id": "123",
        "object_story_spec": {"page_id": "40", "link_data": {"link": "https://example.invalid", "image_hash": "h"}},
        "object_story_id": "40_41",
    })
    _patch(monkeypatch, client)
    result = asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}, dry_run=True))
    assert result["fields_not_copied"] == ["object_story_id"]


def test_enhancement_dry_run_never_shows_a_partnership_ad_code(monkeypatch) -> None:
    built = _built_spec(monkeypatch, ROUND_TRIP_SHAPES["partnership_ad_code"])
    client = ProjectingClient({"id": "20", "account_id": "123", **built})
    _patch(monkeypatch, client)
    result = asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}, dry_run=True))
    assert "AQSYNTHETICCODE1" not in json.dumps(result)
    assert result["would_send"][0]["data"]["branded_content"]["instagram_boost_post_access_token"] == "<partnership_ad_code>"
    assert [call[0] for call in client.calls] == ["get", "get"]


# H1: a Partnership Ad code never comes back inside an error.

CODE = "synthetic-private-code"


def _echoing_error(cycle: bool) -> MetaApiError:
    """A Meta refusal that repeats the code everywhere, behind a cause chain (optionally with a cycle)."""
    inner = httpx.HTTPStatusError(f"upstream said {CODE}", request=httpx.Request("POST", "https://graph.invalid"), response=httpx.Response(400))
    error = MetaApiError(
        f"Invalid authorization code {CODE}", code=100, user_message=f"Rejected {CODE}", user_title=CODE,
        error_data={"blame": CODE, "nested": [CODE]}, details={"error": {"message": CODE}},
    )
    error.__cause__ = inner
    if cycle:
        inner.__context__ = error
    inner.add_note(f"note {CODE}")
    return error


class EchoClient(FakeClient):
    # The cycle is built for direct calls only: the server's rich log handler does not stop on a
    # cyclic error chain (a third-party limit outside this tool), so the server cases use a plain chain.
    def __init__(self, creative: dict[str, Any] | None = None, cycle: bool = True) -> None:
        super().__init__(objects={"10": AD, "20": creative or {}})
        self.cycle = cycle

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, data))
        raise _echoing_error(self.cycle)


def _assert_no_code(exc: BaseException) -> None:
    import traceback

    assert CODE not in "".join(traceback.format_exception(exc))
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert CODE not in repr(current.args)
        assert CODE not in str(current)
        if isinstance(current, MetaApiError):
            assert CODE not in json.dumps(current.to_public_dict())
            assert CODE not in json.dumps(current.details)
        pending.extend([current.__cause__, current.__context__])


@pytest.mark.parametrize(
    "arguments",
    [
        {"partnership_ad_code": CODE, "page_id": "20", "sponsor_page_id": "30"},
        {"partnership_ad_code": f"  {CODE} ", "page_id": "20", "sponsor_page_id": "30"},
        {"facebook_partnership_ad_code": CODE, "sponsor_page_id": "30"},
    ],
)
def test_h1_partnership_code_is_removed_from_a_direct_error(monkeypatch, arguments) -> None:
    client = EchoClient()
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="10", name="Partnership", **arguments))
    assert client.calls[0][3]["branded_content"]  # the code was sent
    _assert_no_code(info.value)
    assert "<partnership_ad_code>" in str(info.value)
    assert "no creative was created" in str(info.value)


@pytest.mark.parametrize(
    "extra",
    [
        {"sponsor_page_id": "30", "facebook_partnership_ad_code": CODE},
        {"sponsor_page_id": "30", "partnership_ad_code": CODE},
    ],
)
@pytest.mark.parametrize("routed", [False, True])
def test_h1_partnership_upstream_error_never_discloses_code(monkeypatch, caplog, extra, routed) -> None:
    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.coordinator import mcp_server

    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    _patch(monkeypatch, EchoClient(cycle=False))
    args = {"account_id": "10", "page_id": "20", "name": "Partnership", **extra}
    name, args = ("call_tool", {"name": "create_ad_creative_from_inputs", "arguments": args}) if routed else ("create_ad_creative_from_inputs", args)
    with pytest.raises(ToolError) as info:
        asyncio.run(mcp_server.call_tool(name, args))
    assert CODE not in str(info.value)
    assert CODE not in caplog.text


def test_h1_code_is_removed_from_an_unexpected_error_too(monkeypatch) -> None:
    class Broken(FakeClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            raise RuntimeError(f"transport dropped {CODE}")

    _patch(monkeypatch, Broken())
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.create_ad_creative_from_inputs(
            account_id="10", name="Partnership", page_id="20", sponsor_page_id="30", partnership_ad_code=CODE,
        ))
    _assert_no_code(info.value)
    assert info.value.mutation_outcome_unknown is True


def test_h1_enhancement_error_never_shows_the_copied_code(monkeypatch) -> None:
    built = {
        "id": "20", "account_id": "123", "name": "Code",
        "object_id": "111", "branded_content": {"instagram_boost_post_access_token": CODE}, "facebook_branded_content": {"sponsor_page_id": "222"},
    }
    client = EchoClient(built)
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    assert client.calls[-1][3]["branded_content"]["instagram_boost_post_access_token"] == CODE
    _assert_no_code(info.value)
    assert "still uses creative 20" in str(info.value)


# H2: validation errors never copy caller text.

PRIVATE = "synthetic.private.customer@example.invalid"


@pytest.mark.parametrize("routed", [False, True])
def test_h2_private_body_validation_does_not_echo_customer_key(caplog, routed) -> None:
    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.coordinator import mcp_server

    args = {"account_id": "10", "name": "Carousel", "page_id": "20", "link_url": "https://example.invalid",
            "cards": [{"image_hash": "a", PRIVATE: "misplaced"}, {"image_hash": "b"}], "dry_run": True}
    name, args = ("call_tool", {"name": "create_ad_creative_from_inputs", "arguments": args}) if routed else ("create_ad_creative_from_inputs", args)
    with pytest.raises(ToolError) as info:
        asyncio.run(mcp_server.call_tool(name, args))
    assert PRIVATE not in str(info.value)
    assert PRIVATE not in caplog.text
    assert "cards[1] has 1 unknown key" in str(info.value)


PRIVATE_PLACEMENTS = [
    # misplaced as a key, in each entry kind
    {"page_id": "1", "link_url": "https://x", "cards": [{"image_hash": "a", PRIVATE: "v"}, {"image_hash": "b"}]},
    {"page_id": "1", "link_url": "https://x", "placement_customizations": [{"image_hash": "a", PRIVATE: "v"}, {"image_hash": "b"}]},
    {"page_id": "1", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"image_hash": "b", PRIVATE: "v"}]},
    # misplaced as a key whose value is a login URL (the login check runs first)
    {"page_id": "1", "link_url": "https://x", "cards": [{"image_hash": "a", PRIVATE: "https://u:p@x.invalid/"}, {"image_hash": "b"}]},
    # misplaced as a value
    {"page_id": "1", "link_url": "https://x", "call_to_action_type": PRIVATE, "image_hash": "h"},
    {"page_id": "1", "link_url": "https://x", "cards": [{"image_hash": "a", "call_to_action_type": PRIVATE}, {"image_hash": "b"}]},
    {"page_id": "1", "link_url": "https://x", "cards": [{"image_hash": "a", "link": PRIVATE}, {"image_hash": "b"}]},
    {"page_id": "1", "link_url": "https://x", "cards": [{"image_hash": "a", "video_id": PRIVATE}, {"image_hash": "b"}]},
    {"page_id": "1", "link_url": "https://x", "placement_customizations": [{"image_hash": PRIVATE}, {"image_hash": PRIVATE, "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]},
    {"page_id": "1", "link_url": "https://x", "placement_customizations": [{"image_hash": "a"}, {"image_hash": "b", "publisher_platforms": [PRIVATE], "facebook_positions": ["feed"]}]},
    {"page_id": "1", "link_url": "https://x", "placement_customizations": [
        {"image_hash": "a", "publisher_platforms": ["facebook"], "facebook_positions": [PRIVATE]},
        {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": [PRIVATE]},
    ]},
    {"page_id": "1", "link_url": "https://x", "placement_customizations": [{"image_url": PRIVATE}, {"image_hash": "b", "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}]},
    {"page_id": "1", "link_url": "https://x", "multi_media": [{"image_hash": PRIVATE}, {"image_hash": PRIVATE}]},
    {"page_id": "1", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"video_id": PRIVATE}]},
    {"page_id": "1", "link_url": "https://x", "multi_media": [{"image_hash": "a"}, {"video_id": "1", "thumbnail_url": PRIVATE}]},
    {"page_id": "1", "image_hash": "h", "link_url": PRIVATE},
    {"page_id": "1", "image_hash": "h", "link_url": "https://x", "display_link": PRIVATE},
    {"page_id": "1", "image_url": PRIVATE, "link_url": "https://x"},
    {"object_story_id": PRIVATE},
    {"page_id": "1", "image_hash": "h", "link_url": "https://x", "lead_gen_form_id": "8"},
    {"page_id": "1", "image_hash": "h", "lead_gen_form_id": "8", "call_to_action_type": "SHOP_NOW"},
    {"page_id": "1", "link_url": "https://x", "primary_texts": [PRIVATE, PRIVATE], "image_hash": "h"},
]


@pytest.mark.parametrize("arguments", PRIVATE_PLACEMENTS)
def test_h2_no_validation_message_copies_caller_text(monkeypatch, arguments) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as info:
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="Creative", **arguments))
    assert "synthetic.private" not in str(info.value).lower()


def test_h2_enhancement_feature_names_are_counted_not_quoted(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={PRIVATE: "OPT_IN", "image_touchups": "OPT_OUT"}))
    assert PRIVATE not in str(info.value)
    assert "1 name" in str(info.value)


# Round five, J1: an enhancement copy keeps every setting Meta accepts on create, and names the rest.


def _projected_enhancement(monkeypatch, extra: dict[str, Any], *, dry_run: bool = False):
    client = ProjectingClient({
        "id": "20", "account_id": "123", "name": "C",
        "object_story_spec": {"page_id": "40", "link_data": {"link": "https://example.invalid", "image_hash": "hash"}},
        **deepcopy(extra),
    })
    _patch(monkeypatch, client)
    result = asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}, dry_run=dry_run))
    return client, result


@pytest.mark.parametrize(("field", "value"), [
    ("image_crops", {"100x100": [[0, 0], [500, 500]]}),
    ("contextual_multi_ads", {"enroll_status": "OPT_OUT"}),
    ("dynamic_ad_voice", "STORY_OWNER"),
])
def test_enhancement_copy_preserves_other_writable_creative_settings(monkeypatch, field, value) -> None:
    client, result = _projected_enhancement(monkeypatch, {field: value})
    posted = next(call[3] for call in client.calls if call[0] == "create")
    assert posted.get(field) == value, (result, client.calls)
    assert field not in result["fields_not_copied"]


ROUND_TRIP_COPIED_BEFORE_ROUND_FIVE = [
    "object_story_spec", "asset_feed_spec", "object_story_id", "source_instagram_media_id", "instagram_user_id",
    "object_id", "url_tags", "call_to_action", "facebook_branded_content", "instagram_branded_content",
    "product_set_id", "media_sourcing_spec", "branded_content", "creative_sourcing_spec",
]

# One example value per newly copied setting, in the shape Meta's reference gives for it.
NEWLY_COPIED_SETTINGS = {
    "ad_disclaimer_spec": {"title": "TERMS_AND_CONDITIONS", "text": "Terms apply", "url": "https://example.invalid/t"},
    "applink_treatment": "deeplink_with_web_fallback",
    "authorization_category": "POLITICAL",
    "branded_content_sponsor_page_id": "61",
    "bundle_folder_id": "62",
    "categorization_criteria": "brand",
    "category_media_source": "MIXED",
    "contextual_multi_ads": {"enroll_status": "OPT_IN"},
    "destination_set_id": "63",
    "destination_spec": {"destination_type": "WEBSITE"},
    "dynamic_ad_voice": "DYNAMIC",
    "enable_direct_install": True,
    "enable_launch_instant_app": True,
    "existing_post_title": "Old title",
    "format_transformation_spec": [{"format": "da_collection", "data_source": ["catalog"]}],
    "generative_asset_spec": {"music": {"enroll_status": "OPT_IN"}},
    "image_crops": {"191x100": [[0, 0], [191, 100]]},
    "interactive_components_spec": {"components": [{"type": "poll"}]},
    "link_og_id": "64",
    "marketing_message_structured_spec": {"buttons": [{"type": "url"}]},
    "omnichannel_link_spec": {"web": {"url": "https://example.invalid"}},
    "page_welcome_message": "Hello",
    "place_page_set_id": "65",
    "platform_customizations": {"instagram": {"image_hash": "ig-hash"}},
    "playable_asset_id": "66",
    "portrait_customizations": {"specifications": [{"background_color": {"bottom": "000000"}}]},
    "product_suggestion_settings": {"enroll_status": "OPT_IN"},
    "recommender_settings": {"preferred_events": ["Purchase"]},
    "regional_regulation_disclaimer_spec": {"taiwan_finserv": {"taiwan_finserv_funder_id": "67"}},
    "source_facebook_post_id": "68",
    "template_url": "https://example.invalid/template",
    "template_url_spec": {"web": {"url": "https://example.invalid/{{product.id}}"}},
    "use_page_actor_override": True,
    "wamo_whatsapp_identity_spec": {"whatsapp_phone_number_id": "69"},
}


def test_every_newly_copied_setting_has_a_round_trip_case() -> None:
    newly_copied = set(cf.COPYABLE_CREATIVE_FIELDS) - set(ROUND_TRIP_COPIED_BEFORE_ROUND_FIVE) - {"adlabels"}
    assert newly_copied == set(NEWLY_COPIED_SETTINGS)


@pytest.mark.parametrize("field", sorted(NEWLY_COPIED_SETTINGS))
def test_enhancement_copy_keeps_each_newly_covered_setting(monkeypatch, field) -> None:
    client, result = _projected_enhancement(monkeypatch, {field: NEWLY_COPIED_SETTINGS[field]})
    posted = next(call[3] for call in client.calls if call[0] == "create")
    assert posted[field] == NEWLY_COPIED_SETTINGS[field]
    assert result["fields_not_copied"] == []
    read = next(call[2] for call in client.calls if call[:2] == ("get", "20"))
    assert field in read


def test_enhancement_copy_sends_ad_labels_by_id(monkeypatch) -> None:
    labels = [{"id": "71", "name": "Spring", "created_time": "2026-01-01T00:00:00+0000"}, {"name": "Unsaved"}]
    client, result = _projected_enhancement(monkeypatch, {"adlabels": labels})
    posted = next(call[3] for call in client.calls if call[0] == "create")
    assert posted["adlabels"] == [{"id": "71"}, {"name": "Unsaved"}]
    assert result["fields_not_copied"] == []


def test_enhancement_copy_leaves_false_switches_at_their_default(monkeypatch) -> None:
    client, result = _projected_enhancement(monkeypatch, {"use_page_actor_override": False, "enable_direct_install": False})
    posted = next(call[3] for call in client.calls if call[0] == "create")
    assert "use_page_actor_override" not in posted and "enable_direct_install" not in posted
    assert result["fields_not_copied"] == []


def test_enhancement_read_covers_every_readable_and_creatable_creative_field() -> None:
    # AdCreative fields that are readable (SDK 26.0.2 Field list) and accepted by POST /act_{id}/adcreatives
    # (SDK create_ad_creative parameters or Meta's reference). Fields Meta fills in from the copied
    # content are listed separately, with the reason they are not sent.
    readable_and_creatable = {
        "actor_id", "ad_disclaimer_spec", "adlabels", "applink_treatment", "asset_feed_spec", "authorization_category",
        "body", "branded_content", "branded_content_sponsor_page_id", "bundle_folder_id", "call_to_action",
        "categorization_criteria", "category_media_source", "contextual_multi_ads", "creative_sourcing_spec",
        "degrees_of_freedom_spec", "destination_set_id", "destination_spec", "dynamic_ad_voice", "enable_direct_install",
        "enable_launch_instant_app", "existing_post_title", "facebook_branded_content", "format_transformation_spec",
        "generative_asset_spec", "image_crops", "image_hash", "image_url", "instagram_branded_content",
        "instagram_permalink_url", "instagram_user_id", "interactive_components_spec", "link_og_id", "link_url",
        "marketing_message_structured_spec", "media_sourcing_spec", "name", "object_id", "object_story_id",
        "object_story_spec", "object_type", "object_url", "omnichannel_link_spec", "page_welcome_message",
        "place_page_set_id", "platform_customizations", "playable_asset_id", "portrait_customizations", "product_set_id",
        "product_suggestion_settings", "recommender_settings", "regional_regulation_disclaimer_spec",
        "source_facebook_post_id", "source_instagram_media_id", "template_url", "template_url_spec", "thumbnail_url",
        "title", "url_tags", "use_page_actor_override", "wamo_whatsapp_identity_spec",
    }
    copied = set(cf.COPYABLE_CREATIVE_FIELDS) | {"name", "degrees_of_freedom_spec"}
    rebuilt = set(cf.FIELDS_REBUILT_FROM_COPIED_CONTENT)
    assert readable_and_creatable <= copied | rebuilt
    assert not copied & rebuilt
    assert set(cf.COPYABLE_CREATIVE_FIELDS) | set(cf.READ_ONLY_CREATIVE_SETTINGS) <= set(cf.ENHANCEMENT_READ_FIELDS)
    assert not rebuilt & set(cf.ENHANCEMENT_READ_FIELDS)


@pytest.mark.parametrize(("field", "value"), [
    ("messenger_sponsored_message", "{\"text\": \"hi\"}"),
    ("collaborative_ads_lsb_image_bank_id", "81"),
    ("link_destination_display_url", "example.invalid/display"),
    ("product_data", [{"product_id": "82", "product_source": "CATALOG"}]),
])
def test_a_setting_the_copy_cannot_carry_is_named_in_dry_run_and_stops_a_real_run(monkeypatch, field, value) -> None:
    client, result = _projected_enhancement(monkeypatch, {field: value}, dry_run=True)
    assert field in result["fields_not_copied"]
    assert result["settings_that_cannot_be_copied"] == [field]
    assert any("refuse" in note for note in result["notes"])
    assert [call[0] for call in client.calls] == ["get", "get"]

    client = ProjectingClient({
        "id": "20", "account_id": "123", "name": "C", field: value,
        "object_story_spec": {"page_id": "40", "link_data": {"link": "https://example.invalid", "image_hash": "hash"}},
    })
    _patch(monkeypatch, client)
    with pytest.raises(ValidationError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    assert field in str(info.value)
    assert "nothing was created or attached" in str(info.value).lower()
    assert [call[0] for call in client.calls] == ["get", "get"]


def test_dry_run_without_lost_settings_reports_none(monkeypatch) -> None:
    _, result = _projected_enhancement(monkeypatch, {}, dry_run=True)
    assert result["settings_that_cannot_be_copied"] == []


def test_refused_creative_read_stops_without_a_narrower_copy(monkeypatch) -> None:
    class Refusing(ProjectingClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "20":
                self.calls.append(("get", object_id, fields))
                raise MetaApiError("(#100) Tried accessing nonexisting field (dynamic_ad_voice)", status_code=400, code=100)
            return await super().get_object(object_id, fields=fields, params=params)

    client = Refusing({"id": "20"})
    _patch(monkeypatch, client)
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.update_ad_creative_enhancements(ad_id="10", features={"image_touchups": "OPT_OUT"}))
    message = str(info.value)
    assert "creative 20" in message
    assert "nothing was created or attached" in message.lower()
    assert "dynamic_ad_voice" in message
    assert [call[0] for call in client.calls] == ["get", "get"]
    assert info.value.code == 100


# Round five, J5: every encoded form of a Partnership Ad code is removed from an error.

ENCODED_SECRET = "R4-secret+code/with=suffix"


def _code_forms(secret: str) -> list[str]:
    from urllib.parse import quote, quote_plus

    return [quote(secret, safe=""), quote_plus(secret), quote(secret), quote(secret, safe="").lower()]


@pytest.mark.parametrize("tool", ["create_ad_creative_from_inputs", "update_ad_creative_enhancements"])
@pytest.mark.parametrize("routed", [False, True])
def test_encoded_partnership_code_is_removed_from_upstream_error(monkeypatch, caplog, tool, routed) -> None:
    from urllib.parse import quote

    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.coordinator import mcp_server

    encoded = quote(ENCODED_SECRET, safe="")

    class Echo(FakeClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            raise MetaApiError("Rejected code " + encoded, code=100, user_message="Invalid " + encoded)

    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    _patch(monkeypatch, Echo(objects={
        "20": {"id": "20", "account_id": "10", "creative": {"id": "30"}},
        "30": {"id": "30", "account_id": "10", "object_id": "40", "branded_content": {"instagram_boost_post_access_token": ENCODED_SECRET}},
    }))
    args = (
        {"account_id": "10", "name": "C", "page_id": "40", "sponsor_page_id": "50", "partnership_ad_code": ENCODED_SECRET}
        if tool == "create_ad_creative_from_inputs" else {"ad_id": "20", "features": {"image_touchups": "OPT_OUT"}}
    )
    name, args = ("call_tool", {"name": tool, "arguments": args}) if routed else (tool, args)
    with pytest.raises(ToolError) as error:
        asyncio.run(mcp_server.call_tool(name, args))
    assert encoded not in str(error.value)
    assert encoded not in caplog.text


@pytest.mark.parametrize("form", range(4))
@pytest.mark.parametrize("given", [ENCODED_SECRET, f"  {ENCODED_SECRET} "])
def test_each_encoded_code_form_is_scrubbed_from_every_error_field(monkeypatch, form, given) -> None:
    import traceback

    shown = _code_forms(ENCODED_SECRET)[form]

    class Echo(FakeClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            error = MetaApiError(f"bad {shown}", code=100, user_message=f"bad {shown}", error_data={"blame": shown})
            error.__cause__ = RuntimeError(f"upstream {shown}")
            raise error

    _patch(monkeypatch, Echo())
    with pytest.raises(MetaApiError) as info:
        asyncio.run(cf.create_ad_creative_from_inputs(
            account_id="10", name="C", page_id="40", sponsor_page_id="50", partnership_ad_code=given,
        ))
    text = "".join(traceback.format_exception(info.value)) + json.dumps(info.value.to_public_dict())
    assert shown not in text
    assert "<partnership_ad_code>" in str(info.value)


# Round five additions: an Instagram Direct button on a single video, a Messenger button on an Instagram post.


def test_click_to_instagram_direct_video_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, page_id="111", instagram_account_id="333", video_id="555", image_url="https://cdn.example.com/t.png",
        message="Chat with us", page_welcome_message="Hi there", call_to_action_type="INSTAGRAM_MESSAGE",
    )
    assert data == {
        "name": "Creative",
        "object_story_spec": {
            "page_id": "111",
            "instagram_user_id": "333",
            "video_data": {
                "video_id": "555",
                "image_url": "https://cdn.example.com/t.png",
                "page_welcome_message": "Hi there",
                "call_to_action": {"type": "INSTAGRAM_MESSAGE", "value": {"app_destination": "INSTAGRAM_DIRECT"}},
                "message": "Chat with us",
            },
        },
    }
    assert result["format"] == "single_video"
    assert any("INSTAGRAM_DIRECT" in note and "IMPRESSIONS" in note for note in result["notes"])


def test_click_to_instagram_direct_video_dry_run_sends_nothing(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Video", page_id="111", instagram_account_id="333", video_id="555", image_hash="t",
        call_to_action_type="INSTAGRAM_MESSAGE", dry_run=True,
    ))
    assert result["sent"] is False
    assert result["would_send"]["data"]["object_story_spec"]["video_data"]["call_to_action"] == {
        "type": "INSTAGRAM_MESSAGE", "value": {"app_destination": "INSTAGRAM_DIRECT"},
    }


def test_messenger_button_on_instagram_post_spec(monkeypatch) -> None:
    result, data = _create(
        monkeypatch, source_instagram_media_id="444", page_id="111", instagram_account_id="333", call_to_action_type="MESSAGE_PAGE",
    )
    assert data == {
        "name": "Creative",
        "object_id": "111",
        "source_instagram_media_id": "444",
        "instagram_user_id": "333",
        "call_to_action": {"type": "MESSAGE_PAGE", "value": {"app_destination": "MESSENGER"}},
    }
    assert result["format"] == "instagram_post"
    assert any("MESSENGER" in note and "destination_type" in note for note in result["notes"])


def test_messenger_button_on_instagram_post_dry_run_sends_nothing(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(cf.create_ad_creative_from_inputs(
        account_id="123", name="Post", source_instagram_media_id="444", page_id="111", instagram_account_id="333",
        call_to_action_type="MESSAGE_PAGE", dry_run=True,
    ))
    assert result["sent"] is False
    assert result["would_send"]["data"]["call_to_action"] == {"type": "MESSAGE_PAGE", "value": {"app_destination": "MESSENGER"}}


ROUND_FIVE_INVALID = [
    # Instagram Direct button on a single video
    ({"page_id": "111", "video_id": "5", "image_hash": "t", "call_to_action_type": "INSTAGRAM_MESSAGE"}, "instagram_account_id"),
    ({"page_id": "111", "instagram_account_id": "3", "video_id": "5", "image_hash": "t", "call_to_action_type": "INSTAGRAM_MESSAGE", "link_url": "https://x"}, "link_url is not used"),
    ({"page_id": "111", "instagram_account_id": "3", "video_id": "5", "call_to_action_type": "INSTAGRAM_MESSAGE"}, "thumbnail"),
    ({"page_id": "111", "instagram_account_id": "3", "video_id": "5", "image_hash": "t", "call_to_action_type": "INSTAGRAM_MESSAGE", "headline": "H"}, "headline"),
    ({"page_id": "111", "instagram_account_id": "3", "video_id": "5", "image_hash": "t", "call_to_action_type": "INSTAGRAM_MESSAGE", "description": "D"}, "description"),
    ({"page_id": "111", "instagram_account_id": "3", "video_id": "5", "image_hash": "t", "call_to_action_type": "INSTAGRAM_MESSAGE", "lead_gen_form_id": "8"}, "lead_gen_form_id"),
    ({"page_id": "111", "instagram_account_id": "3", "video_id": "5", "image_hash": "t", "call_to_action_type": "INSTAGRAM_MESSAGE", "sponsor_page_id": "9"}, "Partnership"),
    ({"page_id": "111", "instagram_account_id": "3", "video_id": "5", "image_hash": "t", "call_to_action_type": "INSTAGRAM_MESSAGE", "display_link": "example.com"}, "display_link"),
    # A WhatsApp button on a single video stays refused
    ({"page_id": "111", "video_id": "5", "image_hash": "t", "call_to_action_type": "WHATSAPP_MESSAGE"}, "no video request"),
    # A Messenger button on an existing Instagram post
    ({"source_instagram_media_id": "4", "page_id": "111", "call_to_action_type": "MESSAGE_PAGE"}, "instagram_account_id is required"),
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "call_to_action_type": "MESSAGE_PAGE", "link_url": "https://x"}, "link_url is not used"),
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "call_to_action_type": "MESSAGE_PAGE", "page_welcome_message": "hi"}, "does not accept page_welcome_message"),
    ({"source_instagram_media_id": "4", "page_id": "111", "sponsor_page_id": "5", "call_to_action_type": "MESSAGE_PAGE"}, "Partnership"),
    ({"source_instagram_media_id": "4", "page_id": "111", "instagram_account_id": "3", "call_to_action_type": "MESSAGE_PAGE", "lead_gen_form_id": "8"}, "lead_gen_form_id is built for a single image or single video"),
]


@pytest.mark.parametrize(("arguments", "message"), ROUND_FIVE_INVALID)
def test_round_five_invalid_combinations_raise_before_any_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(cf.create_ad_creative_from_inputs(account_id="123", name="Creative", **arguments))
