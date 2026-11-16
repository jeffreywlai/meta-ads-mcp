"""Web addresses with an embedded login are refused on the older creative tools (design part C3).

upload_creative_asset, create_ad_creative, update_creative and preview_ad now refuse
a URL that carries a username or password (https://user:pass@host/...) anywhere in
their URL-carrying inputs, before any request is built, as the tools added by the
parity PR already do. Ordinary URLs, including ones with an @ in the path or query,
are still sent as before.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.tools import creatives

SECRET = "synthetic-pass-7f3a"
LOGIN_URL = f"https://someone:{SECRET}@cdn.example.invalid/spring.jpg"
# Each with the login somewhere a creative payload puts a URL.
NESTED = {
    "object_story_spec": {"page_id": "1234567890", "link_data": {"link": LOGIN_URL, "message": "Spring"}},
    "asset_feed_spec": {"link_urls": [{"website_url": LOGIN_URL}], "bodies": [{"text": "Spring"}]},
    "params": {"image_url": LOGIN_URL},
}
# Addresses that hold an @ or look unusual but carry no login; all must still be sent.
ORDINARY = [
    "https://shop.example.com/@spring/sale",
    "https://shop.example.com/sale?contact=help@example.com",
    "https://shop.example.com/sale#@top",
    "{{product.url}}",
    "mailto:help@example.com",
]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, data))
        return {"id": "1112223334"}

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id))
        return {"id": object_id, "name": "Old"}

    async def update_object(self, object_id, *, data):
        self.calls.append(("update", object_id, data))
        return {"success": True}

    async def upload_ad_image(self, account_id, **kwargs):
        self.calls.append(("upload", account_id, kwargs))
        return {"images": {"spring.jpg": {"hash": "8a3f0c6d2b1e4f5a"}}}

    async def preview_ad(self, **kwargs):
        self.calls.append(("preview", kwargs))
        return {"body": "<iframe></iframe>"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake that fails to attach must not reach the network."""

    async def refuse(*args, **kwargs):
        raise AssertionError("no request may be sent in this test")

    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: recorder)
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    return recorder


def _refused(call, client: Recorder, name: str) -> str:
    with pytest.raises((creatives.ValidationError, ToolError)) as raised:
        asyncio.run(call)
    message = str(raised.value)
    assert SECRET not in message and "someone" not in message
    assert name in message
    assert "username or password" in message
    assert client.calls == []
    return message


# upload_creative_asset


def test_upload_creative_asset_refuses_a_login_in_image_url(client: Recorder) -> None:
    _refused(creatives.upload_creative_asset(account_id="1234567890", image_url=LOGIN_URL), client, "image_url")
    _refused(mcp_server.call_tool("upload_creative_asset", {"account_id": "1234567890", "image_url": LOGIN_URL}), client, "image_url")
    _refused(
        mcp_server.call_tool("call_tool", {"name": "upload_creative_asset", "arguments": {"account_id": "1234567890", "image_url": LOGIN_URL}}),
        client, "image_url",
    )


@pytest.mark.parametrize("url", ORDINARY[:3])
def test_upload_creative_asset_still_sends_an_ordinary_url(client: Recorder, url: str) -> None:
    asyncio.run(creatives.upload_creative_asset(account_id="1234567890", image_url=url))
    assert client.calls == [("upload", "act_1234567890", {"image_url": url, "name": None})]


# create_ad_creative


@pytest.mark.parametrize("parameter", sorted(NESTED))
def test_create_ad_creative_refuses_a_login_anywhere_in_its_payloads(client: Recorder, parameter: str) -> None:
    arguments = {"account_id": "1234567890", "name": "Spring", parameter: NESTED[parameter]}
    _refused(creatives.create_ad_creative(**arguments), client, parameter)
    _refused(mcp_server.call_tool("create_ad_creative", arguments), client, parameter)


def test_create_ad_creative_refuses_a_login_in_degrees_of_freedom_spec(client: Recorder) -> None:
    spec = {"creative_features_spec": {"standard_enhancements": {"enroll_status": "OPT_OUT", "url": LOGIN_URL}}}
    _refused(
        creatives.create_ad_creative(account_id="1234567890", name="Spring", degrees_of_freedom_spec=spec),
        client, "degrees_of_freedom_spec",
    )


@pytest.mark.parametrize("url", ORDINARY)
def test_create_ad_creative_still_sends_ordinary_urls(client: Recorder, url: str) -> None:
    spec = {"page_id": "1234567890", "link_data": {"link": url, "message": "Write to help@example.com"}}
    asyncio.run(creatives.create_ad_creative(
        account_id="1234567890", name="Spring", object_story_spec=spec, params={"url_tags": "utm_source=meta"},
    ))
    assert client.calls[-1][0] == "create"
    assert client.calls[-1][3]["object_story_spec"] == spec


# update_creative


@pytest.mark.parametrize("parameter", sorted(NESTED))
def test_update_creative_refuses_a_login_anywhere_in_its_payloads(client: Recorder, parameter: str) -> None:
    arguments = {"creative_id": "1234567890", parameter: NESTED[parameter]}
    _refused(creatives.update_creative(**arguments), client, parameter)
    _refused(mcp_server.call_tool("update_creative", arguments), client, parameter)


@pytest.mark.parametrize("url", ORDINARY)
def test_update_creative_still_sends_ordinary_urls(client: Recorder, url: str) -> None:
    spec = {"page_id": "1234567890", "link_data": {"link": url}}
    asyncio.run(creatives.update_creative(creative_id="1234567890", object_story_spec=spec))
    assert client.calls[-1] == ("update", "1234567890", {"object_story_spec": spec})


# preview_ad


def test_preview_ad_refuses_a_login_in_a_draft_creative(client: Recorder) -> None:
    creative = NESTED["object_story_spec"]
    draft = {"object_story_spec": creative}
    _refused(creatives.preview_ad(account_id="1234567890", creative=draft), client, "creative")
    _refused(mcp_server.call_tool("preview_ad", {"account_id": "1234567890", "creative": draft}), client, "creative")
    _refused(
        creatives.preview_ad(account_id="1234567890", creative=draft, ad_formats=["MOBILE_FEED_STANDARD"]),
        client, "creative",
    )


@pytest.mark.parametrize("url", ORDINARY)
def test_preview_ad_still_previews_ordinary_urls(client: Recorder, url: str) -> None:
    draft = {"object_story_spec": {"page_id": "1234567890", "link_data": {"link": url}}}
    asyncio.run(creatives.preview_ad(account_id="1234567890", creative=draft))
    assert client.calls[-1][0] == "preview"
    assert client.calls[-1][1]["creative"] == draft


# A sub-object sent as JSON text is examined like the object itself (review finding A6).

ENCODED_SPEC = json.dumps(NESTED["object_story_spec"])


def test_preview_ad_refuses_a_login_in_a_json_encoded_sub_object(client: Recorder) -> None:
    draft = {"object_story_spec": ENCODED_SPEC}
    _refused(creatives.preview_ad(account_id="1234567890", creative=draft), client, "creative")
    _refused(mcp_server.call_tool("preview_ad", {"account_id": "1234567890", "creative": draft}), client, "creative")
    _refused(
        mcp_server.call_tool("call_tool", {"name": "preview_ad", "arguments": {"account_id": "1234567890", "creative": draft}}),
        client, "creative",
    )


@pytest.mark.parametrize(
    "encoded",
    [
        ENCODED_SPEC,
        f"  {ENCODED_SPEC}\n",
        json.dumps([{"website_url": LOGIN_URL}]),
        json.dumps({"link_data": json.dumps({"link": LOGIN_URL})}),
    ],
    ids=["object", "padded", "list", "json-inside-json"],
)
def test_create_ad_creative_refuses_a_login_in_json_text_inside_params(client: Recorder, encoded: str) -> None:
    arguments = {"account_id": "1234567890", "name": "Spring", "params": {"object_story_spec": encoded}}
    _refused(creatives.create_ad_creative(**arguments), client, "params")
    _refused(mcp_server.call_tool("create_ad_creative", arguments), client, "params")


def test_update_creative_refuses_a_login_in_json_text_inside_a_spec(client: Recorder) -> None:
    spec = {"page_id": "1234567890", "link_data": json.dumps({"link": LOGIN_URL})}
    _refused(creatives.update_creative(creative_id="1234567890", object_story_spec=spec), client, "object_story_spec")


@pytest.mark.parametrize("url", ORDINARY)
def test_json_text_without_a_login_is_sent_unchanged(client: Recorder, url: str) -> None:
    encoded = json.dumps({"page_id": "1234567890", "link_data": {"link": url}})
    draft = {"object_story_spec": encoded}
    asyncio.run(creatives.preview_ad(account_id="1234567890", creative=draft))
    assert client.calls[-1][1]["creative"] == {"object_story_spec": encoded}


@pytest.mark.parametrize(
    "text",
    ["{not json", "[1, 2", "{}", "[]", "{\"a\": 1}", "[" * 5000 + "]" * 5000, json.dumps({"x": "y" * 300_000})],
    ids=["broken-object", "broken-list", "empty-object", "empty-list", "plain", "very-deep", "very-long"],
)
def test_json_text_that_cannot_or_need_not_be_examined_is_sent_unchanged(client: Recorder, text: str) -> None:
    draft = {"object_story_spec": {"page_id": "1234567890"}, "title": text}
    asyncio.run(creatives.preview_ad(account_id="1234567890", creative=draft))
    assert client.calls[-1][1]["creative"]["title"] == text
