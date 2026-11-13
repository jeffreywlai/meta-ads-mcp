"""Tool search must offer the write tools whose names start with newer verbs.

The router only offers a write tool when a request starts with a write verb.
Six tools start with verbs it did not know: add, remove, boost, cancel,
connect and disconnect.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server

NEW_VERB_WRITE_TOOLS = {
    "add_audience_users",
    "remove_audience_users",
    "boost_instagram_post",
    "cancel_ad_study",
    "connect_catalog_event_source",
    "disconnect_catalog_event_source",
}


def _search(query: str) -> list[str]:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))
    text = "\n".join(item.text for item in result.content if item.type == "text")
    return list(dict.fromkeys(re.findall(r"`([a-z][a-z0-9_]*)`", text)))


@pytest.mark.parametrize(
    "query,expected",
    [
        ("add users to a custom audience", "add_audience_users"),
        ("add customers to my customer list audience", "add_audience_users"),
        ("remove customers from a customer list audience", "remove_audience_users"),
        ("boost instagram post", "boost_instagram_post"),
        ("boost this Instagram post as an ad", "boost_instagram_post"),
        ("cancel ad study", "cancel_ad_study"),
        ("cancel the running studies", "cancel_ad_study"),
        ("connect catalog event source", "connect_catalog_event_source"),
        ("disconnect pixel from catalog", "disconnect_catalog_event_source"),
        ("please connect the pixel to the product catalog", "connect_catalog_event_source"),
    ],
)
def test_requests_starting_with_a_new_write_verb_offer_the_write_tool(query, expected) -> None:
    assert _search(query)[0] == expected


@pytest.mark.parametrize(
    "query",
    [
        "show instagram posts I could boost",
        "list ad sets I could add to a test",
        "which audiences should I remove",
        "find studies I can cancel",
        "list event sources connected to the catalog",
        # The verb appears first but the request is about something else entirely.
        "add up the spend by campaign for last week",
        "remove outliers and compare campaign performance",
        "connect the dots between spend and purchases",
    ],
)
def test_other_requests_never_offer_the_new_write_tools(query) -> None:
    assert not NEW_VERB_WRITE_TOOLS & set(_search(query))


@pytest.mark.parametrize("query", ["do not cancel the ad study", "never boost the instagram post"])
def test_negated_requests_do_not_offer_the_write_tool(query) -> None:
    assert not NEW_VERB_WRITE_TOOLS & set(_search(query))


def test_read_only_mode_hides_the_new_write_tools(monkeypatch) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    for query in ("boost instagram post", "cancel ad study", "add users to a custom audience"):
        assert not NEW_VERB_WRITE_TOOLS & set(_search(query))


@pytest.mark.parametrize(
    "query,expected",
    [("create a product set", "create_product_set"), ("delete a product feed", "delete_product_feed"),
     ("pause campaign", "set_campaign_status")],
)
def test_existing_write_verbs_still_route_as_before(query, expected) -> None:
    assert expected in _search(query)


PHASE_TWO_VERB_WRITE_TOOLS = {
    "duplicate_campaign",
    "duplicate_adset",
    "duplicate_ad",
    "batch_update_catalog_products",
}


@pytest.mark.parametrize(
    "query,expected",
    [
        ("duplicate campaign", "duplicate_campaign"),
        ("duplicate this campaign with its ad sets", "duplicate_campaign"),
        ("duplicate the ad set into another campaign", "duplicate_adset"),
        ("duplicate adset", "duplicate_adset"),
        ("duplicate the ad", "duplicate_ad"),
        ("copy this campaign", "duplicate_campaign"),
        ("clone the ad set", "duplicate_adset"),
        ("batch update catalog products", "batch_update_catalog_products"),
        ("batch edit the products in my catalog by SKU", "batch_update_catalog_products"),
    ],
)
def test_duplicate_and_batch_requests_offer_the_write_tool(query, expected) -> None:
    assert expected in _search(query)[:2]


@pytest.mark.parametrize(
    "query",
    [
        "show campaigns I could duplicate",
        "list duplicate ads by name",
        "copy of last week's spend report",
        "batch report on catalog diagnostics",
        "clone the performance summary into a table",
    ],
)
def test_other_requests_never_offer_the_duplicate_or_batch_tools(query) -> None:
    assert not PHASE_TWO_VERB_WRITE_TOOLS & set(_search(query))


def test_read_only_mode_hides_the_duplicate_and_batch_tools(monkeypatch) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    for query in ("duplicate campaign", "batch update catalog products", "copy this campaign"):
        assert not PHASE_TWO_VERB_WRITE_TOOLS & set(_search(query))
