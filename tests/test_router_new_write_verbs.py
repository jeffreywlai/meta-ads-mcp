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


# Round-two review G16: "add", "remove" and "boost" also have arithmetic and
# reporting meanings. A tool-name noun later in the request ("audience",
# "instagram post") must not turn such a request into a write-only result.

ANALYTICAL_QUERIES = [
    "add up audience performance",
    "remove outliers from audience performance",
    "boost in instagram post engagement",
    # Beyond the reproduction: other particles, reporting qualifiers and noun uses.
    "add together audience results by country",
    "add audience performance to the weekly report",
    "remove the outliers from the custom audience spend",
    "remove audience overlap from the summary",
    "boost of instagram post reach last month",
    "boost instagram post engagement totals by week",
    "cancel out the ad study noise in the results",
    "duplicate campaign spend in the export",
    "connect the dots between catalog event source volume and sales",
    "copy of the ad study results",
]


def _decision(query: str):
    from meta_ads_mcp.intent_routing import StructuredIntentRouter
    from meta_ads_mcp.tool_contracts import build_tool_contracts

    contracts = build_tool_contracts(list(mcp_server.local_provider._components.values()))
    return StructuredIntentRouter().decide(query, tool_contracts=contracts)


@pytest.mark.parametrize("query", ANALYTICAL_QUERIES)
def test_g16_analytical_requests_never_offer_mutations(query) -> None:
    decision = _decision(query)
    assert decision.suppress_mutations is True, sorted(decision.compatible_tools)


ALL_SUBJECT_VERB_WRITE_TOOLS = NEW_VERB_WRITE_TOOLS | PHASE_TWO_VERB_WRITE_TOOLS


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", ANALYTICAL_QUERIES)
def test_analytical_requests_return_read_tools_only(monkeypatch, query, locked) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true" if locked else "false")
    reload_settings()
    found = _search(query)
    assert found, "a read request should still find read tools"
    assert not ALL_SUBJECT_VERB_WRITE_TOOLS & set(found), found


@pytest.mark.parametrize(
    "query",
    [
        "add up audience performance and show the campaign insights",
        "show the campaign insights and add up audience performance",
        "remove outliers from audience performance, then compare campaigns",
        "compare campaigns, then remove outliers from audience performance",
        "boost in instagram post engagement and list the ads",
        "list the ads and boost in instagram post engagement",
    ],
)
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
def test_analytical_clauses_stay_read_safe_in_either_order(monkeypatch, query, locked) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true" if locked else "false")
    reload_settings()
    assert _decision(query).suppress_mutations is True
    found = _search(query)
    assert found
    assert not ALL_SUBJECT_VERB_WRITE_TOOLS & set(found), found


@pytest.mark.parametrize(
    "query,expected",
    [
        ("add up audience performance and add users to the custom audience", "add_audience_users"),
        ("add users to the custom audience and add up audience performance", "add_audience_users"),
        ("remove outliers from audience performance, then remove customers from the audience", "remove_audience_users"),
        ("remove customers from the audience, then remove outliers from audience performance", "remove_audience_users"),
        ("boost in instagram post engagement and boost the instagram post", "boost_instagram_post"),
        ("boost the instagram post and boost in instagram post engagement", "boost_instagram_post"),
    ],
)
def test_a_real_write_clause_is_still_offered_next_to_an_analytical_one(monkeypatch, query, expected) -> None:
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    assert expected in _search(query)[:2]
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    assert expected not in _search(query)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("boost the instagram post with the best engagement", "boost_instagram_post"),
        ("duplicate the campaign with the highest spend", "duplicate_campaign"),
        ("remove customers from the audience with the lowest reach", "remove_audience_users"),
    ],
)
def test_a_command_whose_target_is_described_by_a_number_is_still_a_command(query, expected) -> None:
    assert expected in _decision(query).compatible_tools
