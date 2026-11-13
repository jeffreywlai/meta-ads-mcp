"""Tool search must offer the write tools whose names start with newer verbs.

The router only offers a write tool when a request starts with a write verb.
Six tools start with verbs it did not know: add, remove, boost, cancel,
connect and disconnect.

Cost: building the routing contracts for the whole catalog takes most of a
second, and tool search rebuilds them each time read-only mode is switched on
or off. So routing decisions use contracts built once per catalog, read-only
expectations are checked on the routing decision for the read-only catalog
(tool search filters the catalog to read-only tools before it routes, see
IntentAwareBM25SearchTransform._search), and a smaller set of end-to-end
searches runs in each mode in one test per mode.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.intent_routing import StructuredIntentRouter
from meta_ads_mcp.read_only import read_only_tool_names
from meta_ads_mcp.tool_contracts import build_tool_contracts

NEW_VERB_WRITE_TOOLS = {
    "add_audience_users",
    "remove_audience_users",
    "boost_instagram_post",
    "cancel_ad_study",
    "connect_catalog_event_source",
    "disconnect_catalog_event_source",
}


@pytest.fixture(autouse=True)
def unlocked_by_default(monkeypatch):
    """Searches run with writes allowed unless a test switches read-only mode on."""
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()


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


_CONTRACTS: dict[tuple, dict] = {}


def _contracts(*, locked: bool = False) -> dict:
    """Routing contracts for the registered catalog, built once per catalog.

    locked gives the read-only catalog: the tools search keeps in read-only
    mode before it routes.
    """
    tools = list(mcp_server.local_provider._components.values())
    key = tuple((str(getattr(tool, "name", "")), id(tool)) for tool in tools)
    if key not in _CONTRACTS:
        _CONTRACTS.clear()
        _CONTRACTS[key] = build_tool_contracts(tools)
    contracts = _CONTRACTS[key]
    if locked:
        allowed = read_only_tool_names()
        contracts = {name: contract for name, contract in contracts.items() if name in allowed}
    return contracts


def _decision(query: str, *, locked: bool = False):
    return StructuredIntentRouter().decide(query, tool_contracts=_contracts(locked=locked))


def _read_only_offers(query: str) -> frozenset[str]:
    """Every tool search can return for the query in read-only mode."""
    return _decision(query, locked=True).compatible_tools


@pytest.mark.parametrize("query", ANALYTICAL_QUERIES)
def test_g16_analytical_requests_never_offer_mutations(query) -> None:
    decision = _decision(query)
    assert decision.suppress_mutations is True, sorted(decision.compatible_tools)


ALL_SUBJECT_VERB_WRITE_TOOLS = NEW_VERB_WRITE_TOOLS | PHASE_TWO_VERB_WRITE_TOOLS


def _assert_read_tools_only(query: str, locked: bool) -> None:
    """Unlocked: a real search. Read-only: the routing decision for the read-only catalog."""
    if locked:
        offered = _read_only_offers(query)
        assert offered, "a read request should still find read tools"
        assert not ALL_SUBJECT_VERB_WRITE_TOOLS & offered, sorted(offered)
        return
    found = _search(query)
    assert found, "a read request should still find read tools"
    assert not ALL_SUBJECT_VERB_WRITE_TOOLS & set(found), found


def _assert_command_offered(query: str, expected: str, locked: bool, *, top: int = 3) -> None:
    if locked:
        assert expected not in _read_only_offers(query)
        return
    assert expected in _decision(query).compatible_tools, sorted(_decision(query).compatible_tools)
    assert expected in _search(query)[:top], _search(query)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", ANALYTICAL_QUERIES)
def test_analytical_requests_return_read_tools_only(query, locked) -> None:
    _assert_read_tools_only(query, locked)


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
def test_analytical_clauses_stay_read_safe_in_either_order(query, locked) -> None:
    assert _decision(query).suppress_mutations is True
    _assert_read_tools_only(query, locked)


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
def test_a_real_write_clause_is_still_offered_next_to_an_analytical_one(query, expected) -> None:
    assert expected in _search(query)[:2]
    assert expected not in _read_only_offers(query)


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


# Round-three review H12: a reporting destination after the preposition ("to the
# report") or a count noun in the object ("event source counts") makes the
# request about numbers, even when the object names what a write tool acts on.

H12_REVIEWER_QUERIES = [
    "add audience users to the report",
    "remove audience users from the report",
    "connect catalog event source counts to the chart",
    "disconnect the pixel from the report",
]

H12_MORE_REPORTING_QUERIES = [
    "add audience users to the weekly dashboard",
    "add the customer counts to the spreadsheet",
    "add instagram post counts to the table",
    "add the audience size to the report",
    "add audience users into the graph",
    "remove customers from the summary table",
    "remove audience user numbers from the export",
    "remove the pixel from the chart",
    "connect the pixel to the dashboard",
    "connect event source counts to the report",
    "disconnect the dataset from the dashboard",
    "duplicate the campaign into the report",
    "copy the ad set into the spreadsheet",
    "clone the ad onto the slide deck",
    "copy the campaign to the presentation",
    "boost instagram post counts by week",
    "cancel ad study counts for last month",
    "add customers to the report audience tab",
    "add audience users in the report",
    "add audience users to the Weekly Report",
    "Add Audience Performance To The Report",
    "boost instagram post CTR",
    "remove audience users off the dashboard",
]


@pytest.mark.parametrize("query", H12_REVIEWER_QUERIES)
def test_h12_reporting_objects_are_read_safe_after_prepositions(query) -> None:
    decision = _decision(query)
    assert decision.suppress_mutations is True, sorted(decision.compatible_tools)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", H12_REVIEWER_QUERIES + H12_MORE_REPORTING_QUERIES)
def test_h12_reporting_destinations_and_counts_offer_read_tools_only(query, locked) -> None:
    assert _decision(query).suppress_mutations is True
    _assert_read_tools_only(query, locked)


# Commands whose wording is close to the reporting requests above.
H12_COMMANDS = [
    ("add customers to the audience", "add_audience_users"),
    ("add customers from the spreadsheet to the audience", "add_audience_users"),
    ("add customers from the report to the custom audience", "add_audience_users"),
    ("add users to the report viewers audience", "add_audience_users"),
    ("remove customers from the dashboard audience", "remove_audience_users"),
    ("remove the customers listed in the report from the audience", "remove_audience_users"),
    ("connect the pixel to the catalog", "connect_catalog_event_source"),
    ("disconnect the pixel shown in the report from the catalog", "disconnect_catalog_event_source"),
    ("duplicate the campaign listed in the report", "duplicate_campaign"),
    ("copy the ad set into another campaign", "duplicate_adset"),
    ("boost the instagram post with the most reach", "boost_instagram_post"),
    ("add the customers in the spreadsheet to the custom audience", "add_audience_users"),
]


@pytest.mark.parametrize(("query", "expected"), H12_COMMANDS)
def test_h12_commands_near_reporting_wording_still_offer_the_write_tool(query, expected) -> None:
    _assert_command_offered(query, expected, locked=False)
    _assert_command_offered(query, expected, locked=True)


# Round-three review H13: a reporting word inside an entity's name is the name,
# not the thing acted on.

H13_REVIEWER_COMMANDS = [
    ('duplicate campaign named "Reach"', "duplicate_campaign"),
    ('copy the ad set called "Performance"', "duplicate_adset"),
    ('clone the ad named "Spend"', "duplicate_ad"),
    ('boost the Instagram post called "Reach"', "boost_instagram_post"),
]

H13_MORE_COMMANDS = [
    ("duplicate the campaign called Spend Test", "duplicate_campaign"),
    ("copy the campaign titled 'Performance Max'", "duplicate_campaign"),
    ('clone the ad set named "Weekly Report"', "duplicate_adset"),
    ('boost the instagram post called "Engagement Totals"', "boost_instagram_post"),
    ('add customers to the audience named "Spend Report"', "add_audience_users"),
    ('remove users from the audience called "Reach Chart"', "remove_audience_users"),
    ('connect the pixel to the catalog named "Performance Report"', "connect_catalog_event_source"),
    ('disconnect the pixel from the catalog called "Dashboard"', "disconnect_catalog_event_source"),
    ('cancel the ad study named "Reach Test"', "cancel_ad_study"),
    ('duplicate campaign "Spend"', "duplicate_campaign"),
    ("duplicate the campaign labelled Results", "duplicate_campaign"),
    ("duplicate the Reach campaign", "duplicate_campaign"),
    ("copy the performance ad set", "duplicate_adset"),
    ("boost the highest engagement instagram post", "boost_instagram_post"),
    # An unquoted Title-case name written after the entity.
    ("Duplicate the campaign Reach Test", "duplicate_campaign"),
    ("boost the instagram post Reach", "boost_instagram_post"),
    ("copy the ad set Spend into another campaign", "duplicate_adset"),
    # A description of the entity, not what is acted on.
    ("cancel the brand lift study measuring reach", "cancel_ad_study"),
    ("cancel the study tracking spend", "cancel_ad_study"),
    ("cancel the running study measuring reach", "cancel_ad_study"),
]


@pytest.mark.parametrize(("query", "expected"), H13_REVIEWER_COMMANDS)
def test_h13_reporting_words_in_entity_names_do_not_hide_commands(query, expected) -> None:
    decision = _decision(query)
    assert expected in decision.compatible_tools, sorted(decision.compatible_tools)


@pytest.mark.parametrize(("query", "expected"), H13_REVIEWER_COMMANDS + H13_MORE_COMMANDS)
def test_h13_named_entities_offer_the_write_tool_and_read_only_hides_it(query, expected) -> None:
    _assert_command_offered(query, expected, locked=False)
    _assert_command_offered(query, expected, locked=True)


# Round-four review J15: a reporting container noun ending the object phrase
# ("remove audience users column") is what the verb acts on, as it is after a
# preposition ("to the column"), and so is one after a layout preposition ("as
# a column", "per row"). Commands whose destination is the audience keep working.

J15_REVIEWER_QUERIES = [
    "remove audience users column",
    "remove audience users row",
    "add audience users worksheet",
]

J15_MORE_REPORTING_QUERIES = [
    "add audience users column",
    "remove the audience users columns",
    "add a custom audience users row",
    "add audience users tab",
    "remove audience users sheet",
    "add the audience users cell",
    "remove audience members graph",
    "add audience users dashboard",
    "add the audience users workbook",
    "remove audience users doc",
    "add audience users plot",
    "add audience user rows",
    "remove customer rows",
    "Add Audience Users Column",
    "remove the audience users scorecard",
    "add audience users readout",
    "remove audience users recap",
    "add audience users deck",
    "add audience users presentation",
    "remove audience users visualization",
    "add audience users document",
    "remove the custom audience users tabs",
    "add customer list users column",
    "boost instagram post slide",
    "duplicate the campaign row",
    "duplicate the campaign slide",
    "duplicate the ad set rows",
    "copy the ad set tab",
    "copy the ad set worksheet",
    "clone the ad cell",
    "connect the pixel column",
    "disconnect the event source worksheet",
    "cancel ad study visualization",
    # After a layout preposition.
    "add audience users as a column",
    "add audience users per row",
    "add audience users by column",
    "add audience users for each row",
    "add audience users for the report",
    "remove audience users at row 3",
    "add audience users as a new sheet",
    "copy the campaign as a row",
]

J15_COMMANDS = [
    ("remove customers from the audience", "remove_audience_users"),
    ("add users to the customer list", "add_audience_users"),
    # A container that is where the customers come from, not what is changed.
    ("add the customer spreadsheet to the audience", "add_audience_users"),
    ("add the users row to the custom audience", "add_audience_users"),
    ("add the customer rows to the audience", "add_audience_users"),
    ("add the customer list rows to the audience", "add_audience_users"),
    ("remove the customer worksheet from the audience", "remove_audience_users"),
    ("remove the customers in the sheet from the custom audience", "remove_audience_users"),
    ("remove users in the column from the audience", "remove_audience_users"),
    ("remove the people in the spreadsheet tab from the audience", "remove_audience_users"),
    ("add customers in the spreadsheet to the custom audience", "add_audience_users"),
    ("add the contacts from the sheet to the customer list", "add_audience_users"),
    ("add audience users from the worksheet", "add_audience_users"),
    ("remove customers at row 3 from the audience", "remove_audience_users"),
    ("add customers by email to the audience", "add_audience_users"),
    ("add customers as members of the audience", "add_audience_users"),
    # A container that describes which object is meant, or is part of its name.
    ("remove audience users listed in the column", "remove_audience_users"),
    ("duplicate the campaign shown in the row", "duplicate_campaign"),
    ("copy the ad set listed in the worksheet", "duplicate_adset"),
    ("clone the ad listed in the deck", "duplicate_ad"),
    ("boost the instagram post shown in the slide", "boost_instagram_post"),
    ("cancel the ad study shown in the slide", "cancel_ad_study"),
    ("connect the pixel shown in the dashboard to the catalog", "connect_catalog_event_source"),
    ('duplicate the campaign named "Row"', "duplicate_campaign"),
    ("duplicate the Column campaign", "duplicate_campaign"),
    ("batch update catalog products by SKU", "batch_update_catalog_products"),
]


@pytest.mark.parametrize("query", J15_REVIEWER_QUERIES)
def test_report_container_nouns_do_not_offer_a_write(query) -> None:
    decision = _decision(query)
    assert decision.suppress_mutations is True, decision


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", J15_REVIEWER_QUERIES + J15_MORE_REPORTING_QUERIES)
def test_j15_report_containers_ending_the_object_offer_read_tools_only(query, locked) -> None:
    assert _decision(query).suppress_mutations is True, sorted(_decision(query).compatible_tools)
    _assert_read_tools_only(query, locked)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize(("query", "expected"), J15_COMMANDS)
def test_j15_commands_near_report_containers_still_offer_the_write_tool(query, expected, locked) -> None:
    _assert_command_offered(query, expected, locked)


# End-to-end: real tool searches in each mode, one switch of mode per test. The
# cases above check read-only mode on the routing decision for the read-only
# catalog; these confirm that search gives the same answer.

END_TO_END_READ_QUERIES = [
    *ANALYTICAL_QUERIES[:4],
    "show the campaign insights and add up audience performance",
    *H12_REVIEWER_QUERIES,
    *H12_MORE_REPORTING_QUERIES[:4],
    *J15_REVIEWER_QUERIES,
    *J15_MORE_REPORTING_QUERIES[::6],
]

END_TO_END_COMMANDS = [
    ("add up audience performance and add users to the custom audience", "add_audience_users"),
    *H12_COMMANDS[:4],
    *H13_REVIEWER_COMMANDS,
    *H13_MORE_COMMANDS[:4],
    *J15_COMMANDS[::4],
]


# Names the search result's closing lines mention ("Next: use `call_tool` ...").
SEARCH_TEXT_NAMES = {"call_tool", "get_capabilities"}


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
def test_end_to_end_search_in_each_mode(monkeypatch, locked) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true" if locked else "false")
    reload_settings()
    for query in END_TO_END_READ_QUERIES:
        found = _search(query)
        assert found, query
        assert not ALL_SUBJECT_VERB_WRITE_TOOLS & set(found), (query, found)
        if locked:
            assert set(found) - SEARCH_TEXT_NAMES <= _read_only_offers(query), (query, found)
    for query, expected in END_TO_END_COMMANDS:
        found = _search(query)
        if locked:
            assert expected not in found, (query, found)
            assert set(found) - SEARCH_TEXT_NAMES <= _read_only_offers(query), (query, found)
        else:
            assert expected in found[:3], (query, found)
