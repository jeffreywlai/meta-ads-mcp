"""Tool search must offer the write tools whose names start with newer verbs.

The router only offers a write tool when a request starts with a write verb.
Six tools start with verbs it did not know: add, remove, boost, cancel,
connect and disconnect.

Cost: building the routing contracts for the whole catalog takes most of a
second, and tool search rebuilds them each time read-only mode is switched on
or off. So routing decisions use contracts built once per catalog, and every
query a read-only case checks runs through the real tool search in read-only
mode in one batch, switching mode once (see read_only_searches); each case then
checks its own query's search result, and the routing decision for the
read-only catalog as well.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from meta_ads_mcp import intent_routing, stdio  # noqa: F401 - import registers all tools
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


@contextmanager
def _read_only_mode() -> Iterator[None]:
    """Run tool search with META_READ_ONLY=true, then restore the previous mode."""
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("META_READ_ONLY", "true")
            reload_settings()
            yield
    finally:
        reload_settings()


# Real read-only search results by query, filled by the read_only_searches batch.
_READ_ONLY_SEARCHES: dict[str, list[str]] = {}


def _read_only_search(query: str) -> list[str]:
    """What the real tool search returns for the query in read-only mode.

    The read_only_searches batch has normally run the query already; a query
    it did not cover is searched here, in read-only mode, on its own.
    """
    if query not in _READ_ONLY_SEARCHES:
        with _read_only_mode():
            _READ_ONLY_SEARCHES[query] = _search(query)
    return _READ_ONLY_SEARCHES[query]


@pytest.fixture(scope="module")
def read_only_searches() -> dict[str, list[str]]:
    """Run every query of READ_ONLY_QUERIES through the real search in read-only mode, switching mode once."""
    pending = [query for query in dict.fromkeys(READ_ONLY_QUERIES) if query not in _READ_ONLY_SEARCHES]
    if pending:
        with _read_only_mode():
            for query in pending:
                _READ_ONLY_SEARCHES[query] = _search(query)
    return _READ_ONLY_SEARCHES


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


NEW_VERB_READ_ONLY_QUERIES = ("boost instagram post", "cancel ad study", "add users to a custom audience")


@pytest.mark.usefixtures("read_only_searches")
def test_read_only_mode_hides_the_new_write_tools() -> None:
    for query in NEW_VERB_READ_ONLY_QUERIES:
        assert not NEW_VERB_WRITE_TOOLS & set(_read_only_search(query))


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


PHASE_TWO_READ_ONLY_QUERIES = ("duplicate campaign", "batch update catalog products", "copy this campaign")


@pytest.mark.usefixtures("read_only_searches")
def test_read_only_mode_hides_the_duplicate_and_batch_tools() -> None:
    for query in PHASE_TWO_READ_ONLY_QUERIES:
        assert not PHASE_TWO_VERB_WRITE_TOOLS & set(_read_only_search(query))


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
    """Every tool the routing decision for the read-only catalog allows search to return."""
    return _decision(query, locked=True).compatible_tools


@pytest.mark.parametrize("query", ANALYTICAL_QUERIES)
def test_g16_analytical_requests_never_offer_mutations(query) -> None:
    decision = _decision(query)
    assert decision.suppress_mutations is True, sorted(decision.compatible_tools)


ALL_SUBJECT_VERB_WRITE_TOOLS = NEW_VERB_WRITE_TOOLS | PHASE_TWO_VERB_WRITE_TOOLS


# Names the search result's closing lines mention ("Next: use `call_tool` ...").
SEARCH_TEXT_NAMES = {"call_tool", "get_capabilities"}


def _assert_read_tools_only(query: str, locked: bool) -> None:
    """A real search in the given mode returns read tools; read-only also checks the decision."""
    found = _read_only_search(query) if locked else _search(query)
    assert found, "a read request should still find read tools"
    assert not ALL_SUBJECT_VERB_WRITE_TOOLS & set(found), found
    if locked:
        offered = _read_only_offers(query)
        assert not ALL_SUBJECT_VERB_WRITE_TOOLS & offered, sorted(offered)
        assert set(found) - SEARCH_TEXT_NAMES <= offered, (found, sorted(offered))


def _assert_command_offered(query: str, expected: str, locked: bool, *, top: int = 3) -> None:
    if locked:
        found = _read_only_search(query)
        assert expected not in found, found
        assert expected not in _read_only_offers(query)
        return
    assert expected in _decision(query).compatible_tools, sorted(_decision(query).compatible_tools)
    assert expected in _search(query)[:top], _search(query)


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", ANALYTICAL_QUERIES)
def test_analytical_requests_return_read_tools_only(query, locked) -> None:
    _assert_read_tools_only(query, locked)


ANALYTICAL_CLAUSE_QUERIES = [
    "add up audience performance and show the campaign insights",
    "show the campaign insights and add up audience performance",
    "remove outliers from audience performance, then compare campaigns",
    "compare campaigns, then remove outliers from audience performance",
    "boost in instagram post engagement and list the ads",
    "list the ads and boost in instagram post engagement",
]


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("query", ANALYTICAL_CLAUSE_QUERIES)
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
def test_analytical_clauses_stay_read_safe_in_either_order(query, locked) -> None:
    assert _decision(query).suppress_mutations is True
    _assert_read_tools_only(query, locked)


MIXED_CLAUSE_COMMANDS = [
    ("add up audience performance and add users to the custom audience", "add_audience_users"),
    ("add users to the custom audience and add up audience performance", "add_audience_users"),
    ("remove outliers from audience performance, then remove customers from the audience", "remove_audience_users"),
    ("remove customers from the audience, then remove outliers from audience performance", "remove_audience_users"),
    ("boost in instagram post engagement and boost the instagram post", "boost_instagram_post"),
    ("boost the instagram post and boost in instagram post engagement", "boost_instagram_post"),
]


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("query,expected", MIXED_CLAUSE_COMMANDS)
def test_a_real_write_clause_is_still_offered_next_to_an_analytical_one(query, expected) -> None:
    assert expected in _search(query)[:2]
    _assert_command_offered(query, expected, locked=True)


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


@pytest.mark.usefixtures("read_only_searches")
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


@pytest.mark.usefixtures("read_only_searches")
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


@pytest.mark.usefixtures("read_only_searches")
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


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", J15_REVIEWER_QUERIES + J15_MORE_REPORTING_QUERIES)
def test_j15_report_containers_ending_the_object_offer_read_tools_only(query, locked) -> None:
    assert _decision(query).suppress_mutations is True, sorted(_decision(query).compatible_tools)
    _assert_read_tools_only(query, locked)


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize(("query", "expected"), J15_COMMANDS)
def test_j15_commands_near_report_containers_still_offer_the_write_tool(query, expected, locked) -> None:
    _assert_command_offered(query, expected, locked)


# Round-five review K13: a part of a report named after an entity ("the report's
# audience section", "the audience section of the dashboard") is a place in the
# report, not the audience or campaign. The owning report is written as a
# possessive or after "of"; when the phrase ends in an entity noun ("the
# dashboard's audience"), the report only says which entity is meant.

K13_REVIEWER_QUERIES = [
    "add audience users to the report's audience section",
    "remove audience users from the dashboard's audience section",
    "copy the campaign into the report's campaign section",
]

K13_MORE_REPORTING_QUERIES = [
    "add audience users to the audience section of the dashboard",
    "remove customers from the audience part of the report",
    "add customers to the spreadsheet's audience section",
    "add users to the reports' audience section",
    "disconnect the pixel from the report’s pixel section",
    "connect the pixel to the dashboard's pixel area",
    "copy the ad set into the dashboard's ad set panel",
    "duplicate the campaign into the campaign block of the slide deck",
    "clone the ad into the presentation's ad section",
    "add customers to the weekly report's audience summary",
    "remove customers from the custom audience part of the dashboard",
    "add audience users into the workbook's customer area",
    # The object is the part of the report.
    "remove the report's audience section",
    "remove the audience section of the report",
    "cancel the ad study section of the report",
    "duplicate the campaign section of the weekly report",
    # After a layout preposition.
    "add audience users for the report's audience section",
    "add audience users as the audience section of the dashboard",
]

K13_COMMANDS = [
    # A named audience or catalog.
    ("add users to the audience 'Report Readers'", "add_audience_users"),
    ('add customers to the audience named "Report Section"', "add_audience_users"),
    ("connect the pixel to the catalog named 'Dashboard Section'", "connect_catalog_event_source"),
    # A report, spreadsheet or export that is where the customers come from.
    ("add customers from the report's export to the audience", "add_audience_users"),
    ("add the spreadsheet's customers to the custom audience", "add_audience_users"),
    ("remove the customers in the report's first tab from the audience", "remove_audience_users"),
    ("remove the people in the sheet's first tab from the audience", "remove_audience_users"),
    ("add the customers from the export's audience section to the custom audience", "add_audience_users"),
    ("add the customer list rows from the report's audience section to the audience", "add_audience_users"),
    # A report that says which entity is meant.
    ("add users to the dashboard's audience", "add_audience_users"),
    ("remove customers from the export's audience", "remove_audience_users"),
    ("duplicate the dashboard's top campaign", "duplicate_campaign"),
    ("copy the report's best ad set into another campaign", "duplicate_adset"),
    ("connect the pixel of the report to the catalog", "connect_catalog_event_source"),
    ("disconnect the pixel from the catalog of the report", "disconnect_catalog_event_source"),
    ("add customers to the audience of the brand campaign", "add_audience_users"),
    ("add the users of the dashboard to the audience", "add_audience_users"),
    ("remove the users of the report from the audience", "remove_audience_users"),
]


@pytest.mark.parametrize("query", K13_REVIEWER_QUERIES)
def test_reporting_sections_do_not_offer_mutations(query) -> None:
    decision = _decision(query)
    assert decision.suppress_mutations is True, sorted(decision.compatible_tools)


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", K13_REVIEWER_QUERIES + K13_MORE_REPORTING_QUERIES)
def test_k13_parts_of_a_report_offer_read_tools_only(query, locked) -> None:
    assert _decision(query).suppress_mutations is True, sorted(_decision(query).compatible_tools)
    _assert_read_tools_only(query, locked)


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize(("query", "expected"), K13_COMMANDS)
def test_k13_commands_that_mention_a_report_still_offer_the_write_tool(query, expected, locked) -> None:
    _assert_command_offered(query, expected, locked)


# Round-six review L10: the last word of a report-owned phrase is judged after
# setting aside an id written after it. "the dashboard's audience 1234567890"
# names an audience by id; "the report's audience section 2" is still a place
# in the report. A short bare number is a position (page 2, section 3); a
# number after "id" or "#", an act_ id, or a long number is an id.

L10_REVIEWER_COMMANDS = [
    ("duplicate the dashboard's top campaign 1234567890", "duplicate_campaign"),
    ("add users to the dashboard's audience 1234567890", "add_audience_users"),
    ("disconnect the pixel from the report's catalog 1234567890", "disconnect_catalog_event_source"),
]

L10_MORE_COMMANDS = [
    ("add users to the report's audience id 1234567890", "add_audience_users"),
    ("add customers to the dashboard's audience with id 1234567890", "add_audience_users"),
    ("remove customers from the report's audience #1234567890", "remove_audience_users"),
    ("add customers to the report's audience ID: 42", "add_audience_users"),
    ("remove users from the dashboard's audience #77", "remove_audience_users"),
    ("connect the pixel to the dashboard's catalog 1234567890.", "connect_catalog_event_source"),
    ("add users to the dashboard's audience 1234567890!", "add_audience_users"),
    ("copy the report's best ad set 1234567890 into another campaign", "duplicate_adset"),
    ("clone the dashboard's ad 1234567890", "duplicate_ad"),
    ("duplicate the report's campaigns ids 1234567890, 9876543210", "duplicate_campaign"),
    ("remove users from the spreadsheet's audience 'Spring Buyers'", "remove_audience_users"),
    ("disconnect the pixel 1234567890 from the report's catalog 9876543210", "disconnect_catalog_event_source"),
    ("connect the dashboard's pixel 1234567890 to the catalog 9876543210", "connect_catalog_event_source"),
    ("add users to the audience 1234567890 of the dashboard", "add_audience_users"),
]

L10_MORE_REPORTING_QUERIES = [
    "move the audience section to page 2 of the report",
    "add audience users to the report's audience section 2",
    "remove customers from the dashboard's audience section 3",
    "copy the campaign into the report's campaign section 4",
    "add audience users to the report's audience section #2",
    "remove the report's audience section 2",
    "remove the dashboard's audience section 12.",
    "add audience users to the audience section 2 of the dashboard",
    "add customers to the audience part 2 of the report",
    "add users to page 2 of the report",
    "add audience users to row 12 of the dashboard",
    "copy the campaign into slide 4 of the deck",
    "duplicate the campaign section 2 of the weekly report",
    "add customers to the report's audience section id 2",
]


@pytest.mark.parametrize(("query", "expected"), L10_REVIEWER_COMMANDS)
def test_l10_an_explicit_entity_id_does_not_turn_a_write_into_report_editing(query, expected) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))
    text = "\n".join(item.text for item in result.content if item.type == "text")
    assert f"`{expected}`" in text


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize(("query", "expected"), L10_REVIEWER_COMMANDS + L10_MORE_COMMANDS)
def test_l10_an_entity_named_by_id_after_a_report_still_offers_the_write_tool(query, expected, locked) -> None:
    _assert_command_offered(query, expected, locked)


@pytest.mark.parametrize(
    ("phrase", "kept"),
    [
        ("the dashboard's audience 1234567890", "audience"),
        ("the dashboard's account act_1234", "account"),
        ("the report's audience ID: 42", "audience"),
        ("the report's audience #7", "audience"),
        ("the report's campaigns ids 1234567890, 9876543210", "campaigns"),
        ("the report's audience section 2", "2"),
        ("page 12", "12"),
        ("the transact_123 audience", "audience"),
    ],
)
def test_l10_the_id_after_an_entity_is_set_aside_but_a_position_is_not(phrase, kept) -> None:
    words = intent_routing._phrase_words(f"add {phrase}")[1:]
    assert intent_routing._without_trailing_id(words)[-1].text == kept


@pytest.mark.usefixtures("read_only_searches")
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
@pytest.mark.parametrize("query", L10_MORE_REPORTING_QUERIES)
def test_l10_report_edits_that_mention_a_number_offer_read_tools_only(query, locked) -> None:
    assert _decision(query).suppress_mutations is True, sorted(_decision(query).compatible_tools)
    _assert_read_tools_only(query, locked)


# End-to-end: real tool searches in each mode, switching mode inside the test,
# independently of the read_only_searches batch the cases above share.

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


# Every query a read-only case checks. read_only_searches runs them all through
# the real search in read-only mode at once; a query missing here is still
# searched for real, on its own, at the cost of switching mode for it.
READ_ONLY_QUERIES = [
    *NEW_VERB_READ_ONLY_QUERIES,
    *PHASE_TWO_READ_ONLY_QUERIES,
    *ANALYTICAL_QUERIES,
    *ANALYTICAL_CLAUSE_QUERIES,
    *(query for query, _expected in MIXED_CLAUSE_COMMANDS),
    *H12_REVIEWER_QUERIES,
    *H12_MORE_REPORTING_QUERIES,
    *(query for query, _expected in H12_COMMANDS),
    *(query for query, _expected in H13_REVIEWER_COMMANDS + H13_MORE_COMMANDS),
    *J15_REVIEWER_QUERIES,
    *J15_MORE_REPORTING_QUERIES,
    *(query for query, _expected in J15_COMMANDS),
    *K13_REVIEWER_QUERIES,
    *K13_MORE_REPORTING_QUERIES,
    *(query for query, _expected in K13_COMMANDS),
    *(query for query, _expected in L10_REVIEWER_COMMANDS + L10_MORE_COMMANDS),
    *L10_MORE_REPORTING_QUERIES,
]
