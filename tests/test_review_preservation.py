"""Real search surface regressions for coordinated configuration subjects."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp import stdio  # noqa: F401 - registers the real tool catalog
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server


def _search_names(query: str) -> list[str]:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))
    return [
        line.split("`", 2)[1]
        for line in result.content[0].text.splitlines()
        if line.startswith("- `")
    ]


@pytest.mark.parametrize("query", [
    "update campaign budget; adset bid amount and adset status must not change",
    "update campaign budget, adset bid amount and adset status must not change",
    "update campaign budget; ad set bid amount, ad set status must not change",
    "update campaign budget, ad set bid amount, and campaign status must not change",
    "update campaign budget, name and objective must not change",
    "update campaign budget and campaign name and campaign objective must not change",
    "update campaign budget; campaign name and campaign objective should stay unchanged",
    "update campaign budget; bid amount to 2 must not change",
    "update campaign budget; bid amount and adset status to paused must not change",
])
def test_search_preserves_only_the_requested_budget(query: str) -> None:
    assert _search_names(query) == ["update_campaign_budget"]


@pytest.mark.parametrize(("query", "expected"), [
    ("update campaign name; campaign objective and campaign status must not change", ["update_campaign"]),
    ('update campaign budget and name to "Winter", objective and status must not change', [
        "update_campaign_budget", "update_campaign",
    ]),
    ("update campaign budget; adset bid amount and adset status must not change; update campaign name", [
        "update_campaign_budget", "update_campaign",
    ]),
    ("update campaign budget, name and objective must not change and update adset bid amount", [
        "update_campaign_budget", "update_adset_bid_amount",
    ]),
    ("update campaign budget, name and objective must not change, update adset bid amount", [
        "update_campaign_budget", "update_adset_bid_amount",
    ]),
    ("update campaign budget; bid amount to 2 and adset status must not change", [
        "update_campaign_budget", "update_adset_bid_amount",
    ]),
    ("update campaign budget; adset bid amount to 2, adset status must not change", [
        "update_campaign_budget", "update_adset_bid_amount",
    ]),
    ("update campaign budget, name and objective must not change, show adset status", [
        "update_campaign_budget", "get_adset",
    ]),
    ("pause ad set status, campaign budget", ["set_adset_status", "update_campaign_budget"]),
    ("update campaign status to paused and campaign budget", ["set_campaign_status", "update_campaign_budget"]),
    ("update campaign budget and adset budget and bid strategy", [
        "update_campaign_budget", "update_adset_budget", "update_adset_bid_strategy",
    ]),
    ("update adset budget and campaign budget bid strategy", [
        "update_adset_budget", "update_campaign_budget", "update_campaign_bid_strategy",
    ]),
])
def test_search_preserves_positive_command_and_value_boundaries(query, expected) -> None:
    assert _search_names(query) == expected


@pytest.mark.parametrize("separator", [" and ", ", ", ", and "])
@pytest.mark.parametrize(("prefix", "suffix", "expected"), [
    ("show campaign budget", "campaign status; update adset budget", [
        "get_campaign", "update_adset_budget",
    ]),
    ("update campaign budget; show adset bid amount", "adset status", [
        "update_campaign_budget", "get_adset",
    ]),
    ("show campaign budget", "ad set status; update adset budget", [
        "get_campaign", "get_adset", "update_adset_budget",
    ]),
])
def test_search_keeps_repeated_read_subjects_under_their_read_verb(
    separator, prefix, suffix, expected,
) -> None:
    assert _search_names(prefix + separator + suffix) == expected


@pytest.mark.parametrize("value", ["LOWEST_COST_WITHOUT_CAP", '"CUSTOM"'])
@pytest.mark.parametrize("prefix", ["", "budget to 100 and "])
def test_search_keeps_fields_after_a_bid_strategy_literal(value, prefix) -> None:
    expected = ["update_campaign_bid_strategy", "update_campaign"]
    if prefix:
        expected.insert(0, "update_campaign_budget")

    assert _search_names(f'update campaign {prefix}bid strategy to {value} and name "Winter"') == expected


@pytest.mark.parametrize("name", ["get_campaign", "update_adset_bid_amount"])
def test_search_does_not_treat_quoted_configuration_values_as_tool_requests(name) -> None:
    assert _search_names(f'update campaign name to "{name}" and budget to 100') == [
        "update_campaign", "update_campaign_budget",
    ]
    assert _search_names(f'find tool "{name}"') == [name]


def test_search_protects_literals_references_and_exact_call_arguments() -> None:
    assert _search_names(
        'update_campaign(campaign_id="123", name="adset bid amount and adset status must not change"); '
        'update campaign budget to 100'
    ) == ["update_campaign", "update_campaign_budget"]
    assert _search_names(
        'update campaign name to "without changing status, and update_adset_bid_amount" and budget to 100'
    ) == ["update_campaign", "update_campaign_budget"]
    assert _search_names("update ad set budget to match campaign budget and ad set status") == [
        "update_adset_budget",
    ]
    assert _search_names(
        'update_campaign(campaign_id=123, name="campaign status must not change"); '
        'update campaign budget'
    ) == ["update_campaign_budget"]


def test_read_only_search_keeps_reads_and_filters_coordinated_changes(monkeypatch) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()

    assert _search_names(
        "show campaign budget and adset status; update campaign budget, name and objective must not change"
    ) == ["get_campaign", "get_adset"]
