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
from meta_ads_mcp.tools import ads, campaigns, creatives

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


# create_ad, update_ad and create_ad_set refuse a login in every argument that can carry a web address.


@pytest.fixture
def ad_client(monkeypatch: pytest.MonkeyPatch, client: Recorder) -> Recorder:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    return client


AD = {"account_id": "1234567890", "name": "Spring", "adset_id": "2222222222"}
AD_SET = {
    "account_id": "1234567890", "campaign_id": "2222222222", "name": "Spring", "billing_event": "IMPRESSIONS",
    "optimization_goal": "LINK_CLICKS", "targeting": {"geo_locations": {"countries": ["US"]}},
}
AD_LOGIN_CASES = [
    ("create_ad", ads.create_ad, {**AD, "creative_spec": NESTED["object_story_spec"] | {"name": "Spring"}}, "creative_spec"),
    ("create_ad", ads.create_ad, {**AD, "creative_spec": {"name": "Spring", "object_story_spec": ENCODED_SPEC}},
     "creative_spec"),
    ("create_ad", ads.create_ad, {**AD, "creative_id": "3333333333", "tracking_specs": {"url": LOGIN_URL}},
     "tracking_specs"),
    ("create_ad", ads.create_ad, {**AD, "creative_id": "3333333333", "params": {"url_tags": LOGIN_URL}}, "params"),
    ("update_ad", ads.update_ad, {"ad_id": "4444444444", "tracking_specs": [{"url": LOGIN_URL}]}, "tracking_specs"),
    ("update_ad", ads.update_ad, {"ad_id": "4444444444", "params": {"display_url": LOGIN_URL}}, "params"),
    ("update_ad", ads.update_ad, {"ad_id": "4444444444", "params": {"conversion_specs": [{"url": LOGIN_URL}]}}, "params"),
    ("create_ad_set", campaigns.create_ad_set,
     {**AD_SET, "promoted_object": {"application_id": "5555555555", "object_store_url": LOGIN_URL}},
     "promoted_object.object_store_url"),
    ("create_ad_set", campaigns.create_ad_set, {**AD_SET, "params": {"destination_url": LOGIN_URL}}, "params"),
]


@pytest.mark.parametrize(("tool", "function", "arguments", "name"), AD_LOGIN_CASES)
def test_ad_and_ad_set_tools_refuse_a_login_in_a_web_address(
    ad_client: Recorder, tool: str, function, arguments: dict, name: str
) -> None:
    _refused(function(**arguments), ad_client, name)
    _refused(mcp_server.call_tool(tool, arguments), ad_client, name)
    _refused(mcp_server.call_tool("call_tool", {"name": tool, "arguments": arguments}), ad_client, name)


@pytest.mark.parametrize("url", ORDINARY)
def test_ad_tools_still_send_ordinary_urls(ad_client: Recorder, url: str) -> None:
    spec = {"name": "Spring", "object_story_spec": {"page_id": "1234567890", "link_data": {"link": url}}}
    asyncio.run(ads.create_ad(**AD, creative_spec=spec, params={"url_tags": "utm_source=meta"}))
    assert ad_client.calls[-1][0] == "create"
    assert ad_client.calls[-1][3]["creative"] == spec


# A login address inside longer text (URL parameters, a message) is found too, with the
# catalog tools' scanner: it needs scheme:// followed by an @ before any space, /, ? or #.

IN_TEXT = f"landing=https://someone:{SECRET}@cdn.example.invalid/spring&utm_source=meta"
IN_TEXT_CASES = [
    ("create_ad", ads.create_ad, {**AD, "creative_id": "3333333333", "params": {"url_tags": IN_TEXT}}, "params"),
    ("update_ad", ads.update_ad, {"ad_id": "4444444444", "params": {"url_tags": IN_TEXT}}, "params"),
    ("create_ad_set", campaigns.create_ad_set, {**AD_SET, "params": {"destination_note": f"see {IN_TEXT}"}}, "params"),
    ("create_ad_creative", creatives.create_ad_creative,
     {"account_id": "1234567890", "name": "Spring", "params": {"url_tags": IN_TEXT}}, "params"),
    ("create_ad_creative", creatives.create_ad_creative, {
        "account_id": "1234567890", "name": "Spring",
        "object_story_spec": {"page_id": "1234567890", "link_data": {"link": "https://shop.example.com/",
                                                                    "message": f"Order at {LOGIN_URL} today"}},
    }, "object_story_spec"),
    ("update_creative", creatives.update_creative, {"creative_id": "1234567890", "params": {"url_tags": IN_TEXT}},
     "params"),
    ("preview_ad", creatives.preview_ad, {"account_id": "1234567890", "creative": {"url_tags": IN_TEXT}}, "creative"),
    ("upload_creative_asset", creatives.upload_creative_asset,
     {"account_id": "1234567890", "image_url": f"spring: {LOGIN_URL}"}, "image_url"),
]


@pytest.mark.parametrize(("tool", "function", "arguments", "name"), IN_TEXT_CASES)
def test_a_login_address_inside_longer_text_is_refused(
    ad_client: Recorder, tool: str, function, arguments: dict, name: str
) -> None:
    _refused(function(**arguments), ad_client, name)
    _refused(mcp_server.call_tool(tool, arguments), ad_client, name)


def test_the_boost_tool_refuses_a_login_address_inside_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: pytest.fail("no client may be created"))
    base = {"account_id": "1234567890", "instagram_media_id": "1234567890", "instagram_account_id": "1234567890",
            "adset_id": "2222222222", "page_id": "3333333333"}
    cta = {"type": "LEARN_MORE", "value": {"link": "https://shop.example.com/", "link_caption": f"via {LOGIN_URL}"}}
    for arguments, name in (({"call_to_action": cta}, "call_to_action"), ({"url_tags": IN_TEXT}, "url_tags")):
        with pytest.raises(creatives.ValidationError) as raised:
            asyncio.run(creatives.boost_instagram_post(**base, **arguments))
        message = str(raised.value)
        assert name in message and "username or password" in message
        assert SECRET not in message and "someone" not in message


# Ordinary text with an @ or a colon that is not a login address; each must still be sent.
ORDINARY_TEXT = [
    "write to help@example.com",
    "https://example.com/@handle",
    "https://example.com/?to=a@b.com",
    "@brand",
    "Sale: 20% off @ our store",
    "utm_source=meta&ref=https://example.com/@brand&mail=a@b.com",
    # Scheme-relative look-alikes that are not a login with a password at a boundary.
    "a//b:c@d",
    "see // me:x@example.com",
    "path=//cdn.example.com/img@2x.png",
    "write to //contact@example.com today",
    "ratio 16//9: now @ home",
    # A space still ends an address (parsers keep spaces), and a newline before a handle
    # after an address with no password is not read as a login with a password.
    "Visit //someone:pa ss@cdn.example.invalid",
    "landing=https://someone:pa ss@cdn.example.invalid/",
    "Shop at https://shop.example.com\n@brand",
]


@pytest.mark.parametrize("text", ORDINARY_TEXT)
def test_ordinary_text_with_an_at_sign_is_still_sent(ad_client: Recorder, text: str) -> None:
    spec = {"page_id": "1234567890", "link_data": {"link": "https://shop.example.com/", "message": text}}
    asyncio.run(creatives.create_ad_creative(
        account_id="1234567890", name="Spring", object_story_spec=spec, params={"url_tags": text},
    ))
    assert ad_client.calls[-1][3]["object_story_spec"] == spec
    assert ad_client.calls[-1][3]["url_tags"] == text
    asyncio.run(ads.create_ad(**AD, creative_id="3333333333", params={"url_tags": text}))
    assert ad_client.calls[-1][3]["url_tags"] == text
    draft = {"object_story_spec": spec, "url_tags": text}
    asyncio.run(creatives.preview_ad(account_id="1234567890", creative=draft))
    assert ad_client.calls[-1][1]["creative"] == draft


# A scheme-relative login (//user:pass@host) inside longer text: // at the start or after a
# boundary character, a password in the user part, and the @ before any space, /, ? or #.

RELATIVE_LOGIN = f"//someone:{SECRET}@cdn.example.invalid/path"
RELATIVE_TEXTS = [
    f"redirect={RELATIVE_LOGIN}&utm_campaign={{{{campaign.id}}}}",
    f"Visit {RELATIVE_LOGIN}",
    f"Visit //someone:{SECRET}@cdn.example.invalid",
    f"utm_source=meta&next={RELATIVE_LOGIN}",
    f'go "{RELATIVE_LOGIN}"',
    f"links: ({RELATIVE_LOGIN})",
    f"a,{RELATIVE_LOGIN};b",
]


@pytest.mark.parametrize("text", RELATIVE_TEXTS)
def test_a_scheme_relative_login_inside_text_is_refused(ad_client: Recorder, text: str) -> None:
    link_data = {"link": "https://shop.example.com/", "message": text}
    cases = [
        (creatives.create_ad_creative(account_id="1234567890", name="Spring", params={"url_tags": text}), "params"),
        (creatives.create_ad_creative(
            account_id="1234567890", name="Spring",
            object_story_spec={"page_id": "1234567890", "link_data": link_data},
        ), "object_story_spec"),
        (creatives.preview_ad(account_id="1234567890", creative={
            "object_story_spec": json.dumps({"page_id": "1234567890", "link_data": link_data}),
        }), "creative"),
        (ads.create_ad(**AD, creative_id="3333333333", params={"url_tags": text}), "params"),
        (ads.update_ad(ad_id="4444444444", params={"url_tags": text}), "params"),
        (campaigns.create_ad_set(**AD_SET, params={"destination_note": text}), "params"),
    ]
    for call, name in cases:
        _refused(call, ad_client, name)
    arguments = {"account_id": "1234567890", "name": "Spring", "params": {"url_tags": text}}
    _refused(mcp_server.call_tool("call_tool", {"name": "create_ad_creative", "arguments": arguments}), ad_client, "params")


@pytest.mark.parametrize("text", ["//contact@example.com", f"//someone:{SECRET}@cdn.example.invalid/path",
                                  " //contact@example.com "])
def test_a_whole_scheme_relative_address_with_a_user_name_is_still_refused(ad_client: Recorder, text: str) -> None:
    _refused(creatives.create_ad_creative(account_id="1234567890", name="Spring", params={"url_tags": text}),
             ad_client, "params")
    _refused(ads.create_ad(**AD, creative_id="3333333333", params={"url_tags": text}), ad_client, "params")


# URL parsers delete tab, CR and LF before reading an address, so a login split by one is
# still a login to them; the check reads the text the same way.
def _split_logins(ch: str) -> list[str]:
    host = "cdn.example.invalid/path"
    return [
        f"Visit //some{ch}one:{SECRET}@{host}",
        f"Visit //someone:{SECRET[:9]}{ch}{SECRET[9:]}@{host}",
        f"Visit //{ch}someone:{SECRET}@{host}",
        f"Visit /{ch}/someone:{SECRET}@{host}",
        f"redirect=//someone:{SECRET}{ch}@{host}&utm_source=meta",
        f"landing=https://some{ch}one:{SECRET}@{host}",
        f"landing=https://someone:{SECRET[:9]}{ch}{SECRET[9:]}@{host}",
        f"landing=https://{ch}someone:{SECRET}@{host}",
        f"landing=ht{ch}tps://someone:{SECRET}@{host}",
        f"landing=https{ch}://someone:{SECRET}@{host}",
        f"landing=https:/{ch}/someone:{SECRET}@{host}",
    ]


@pytest.mark.parametrize("text", [text for ch in ("\t", "\r", "\n") for text in _split_logins(ch)])
def test_a_login_split_by_a_tab_or_line_break_is_refused(ad_client: Recorder, text: str) -> None:
    link_data = {"link": "https://shop.example.com/", "message": text}
    _refused(creatives.create_ad_creative(account_id="1234567890", name="Spring", params={"url_tags": text}),
             ad_client, "params")
    _refused(creatives.create_ad_creative(
        account_id="1234567890", name="Spring", object_story_spec={"page_id": "1234567890", "link_data": link_data},
    ), ad_client, "object_story_spec")
    _refused(ads.update_ad(ad_id="4444444444", params={"url_tags": text}), ad_client, "params")
    for part in (SECRET[:9], SECRET[9:]):
        with pytest.raises(creatives.ValidationError) as raised:
            asyncio.run(ads.create_ad(**AD, creative_id="3333333333", params={"url_tags": text}))
        assert part not in str(raised.value)


def test_the_scheme_relative_scan_of_a_long_text_finishes_quickly() -> None:
    import time

    from meta_ads_mcp.url_secrets import refuse_url_login_in_text

    text = "=//a:" * 200_000 + "x" * 200_000 + "@ " + "a" * 1_000_000
    started = time.monotonic()
    refuse_url_login_in_text("=// " * 200_000 + "@", "params", scheme_relative=True)
    with pytest.raises(creatives.ValidationError):
        refuse_url_login_in_text(text, "params", scheme_relative=True)
    assert time.monotonic() - started < 5


def test_the_catalog_tools_check_is_unchanged_by_the_scheme_relative_rule() -> None:
    """The scheme-relative rule is an opt-in of the creative and ad tools; the catalog check is as it was."""
    from meta_ads_mcp.tools.catalog_feeds import _refuse_url_logins as catalog_check
    from meta_ads_mcp.url_secrets import refuse_url_login_in_text

    for text in RELATIVE_TEXTS[:4]:
        catalog_check(text, "params")
        refuse_url_login_in_text(text, "params")
        with pytest.raises(creatives.ValidationError):
            refuse_url_login_in_text(text, "params", scheme_relative=True)
    # A whole scheme-relative address with a user name is refused by the catalog check, as before.
    for text in ("//contact@example.com", RELATIVE_LOGIN):
        with pytest.raises(creatives.ValidationError):
            catalog_check(text, "params")
    # The catalog check reads text as given: a login split by a tab or line break inside longer
    # text is not found there (left as it was; reported), while the creative and ad check finds it.
    split = f"landing=https://someone:{SECRET[:9]}\t{SECRET[9:]}@cdn.example.invalid/"
    catalog_check(split, "params")
    with pytest.raises(creatives.ValidationError):
        refuse_url_login_in_text(split, "params", scheme_relative=True)
    # Ordinary text both checks accept.
    for text in ("Shop at https://shop.example.com\n@brand", "write to //contact@example.com today"):
        catalog_check(text, "params")
        refuse_url_login_in_text(text, "params", scheme_relative=True)
