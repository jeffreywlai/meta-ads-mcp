"""Recommendation tool tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError, ValidationError as FastMCPValidationError

from meta_ads_mcp import stdio  # noqa: F401 - registers discovery and proxy tools
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.tools import recommendations, utility


class FakeRecommendationsClient:
    """Fake recommendations client."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_recommendations(self, account_id: str, *, campaign_id=None):
        self.calls += 1
        return {
            "data": [
                {"id": "rec_1", "message": "Increase budget on strong ad sets", "campaign_id": campaign_id},
                {"id": "rec_2", "title": "Refresh creative image assets", "campaign_id": campaign_id},
                {"id": "rec_3", "description": "Broaden audience targeting", "campaign_id": campaign_id},
                {"id": "rec_4", "message": "Fix delivery and learning limitations", "campaign_id": campaign_id},
                {"id": "rec_5", "recommendation_type": "BID_CAP_ADJUSTMENT", "campaign_id": campaign_id},
            ]
        }


def test_get_recommendations_returns_supported_collection(monkeypatch) -> None:
    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FakeRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123", campaign_id="cmp_123"))
    assert result["supported"] is True
    assert result["items"][0]["campaign_id"] == "cmp_123"
    assert result["summary"]["category_counts"]["budget"] == 1
    assert "opportunity_categories" in result["items"][0]


def test_get_recommendations_accepts_account_object_id_alias(monkeypatch) -> None:
    recommendations._RECOMMENDATION_CACHE.clear()
    client = FakeRecommendationsClient()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: client)
    result = asyncio.run(recommendations.get_recommendations(object_id="act_123"))
    assert result["supported"] is True
    assert client.calls == 1


def test_typed_opportunity_tools_filter_by_category(monkeypatch) -> None:
    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FakeRecommendationsClient())

    budget = asyncio.run(recommendations.get_budget_opportunities(account_id="123"))
    creative = asyncio.run(recommendations.get_creative_opportunities(account_id="123"))
    audience = asyncio.run(recommendations.get_audience_opportunities(account_id="123"))
    delivery = asyncio.run(recommendations.get_delivery_opportunities(account_id="123"))
    bidding = asyncio.run(recommendations.get_bidding_opportunities(account_id="123"))

    assert budget["items"][0]["id"] == "rec_1"
    assert creative["items"][0]["id"] == "rec_2"
    assert audience["items"][0]["id"] == "rec_3"
    assert delivery["items"][0]["id"] == "rec_4"
    assert bidding["items"][0]["id"] == "rec_5"
    assert budget["summary"]["filtered_from_total"] == 5


def test_get_recommendations_flattens_nested_recommendation_payloads(monkeypatch) -> None:
    class NestedRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            return {
                "data": [
                    {
                        "recommendations": [
                            {
                                "id": "rec_nested_1",
                                "type": "VALUE_OPTIMIZATION_GOAL",
                                "recommendation_content": {
                                    "body": "Duplicate ad sets to maximize value of conversions.",
                                    "lift_estimate": "7% higher ROAS",
                                },
                            },
                            {
                                "id": "rec_nested_2",
                                "type": "REELS_PC_RECOMMENDATION",
                                "recommendation_content": {
                                    "body": "Add fullscreen vertical video.",
                                },
                            },
                        ]
                    }
                ]
            }

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: NestedRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123"))
    bidding = asyncio.run(recommendations.get_bidding_opportunities(account_id="123"))
    creative = asyncio.run(recommendations.get_creative_opportunities(account_id="123"))
    assert result["summary"]["count"] == 2
    assert result["items"][0]["body"] == "Duplicate ad sets to maximize value of conversions."
    assert "message" not in result["items"][0]
    assert "recommendation_content" not in result["items"][0]
    assert bidding["items"][0]["id"] == "rec_nested_1"
    assert creative["items"][0]["id"] == "rec_nested_2"


def test_get_recommendations_dedupes_duplicate_text_wrappers(monkeypatch) -> None:
    class DuplicateTextClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            return {
                "data": [
                    {
                        "id": "rec_1",
                        "message": "Increase budget on strong ad sets",
                        "recommendation_content": {
                            "body": "Increase budget on strong ad sets",
                            "title": "Increase budget",
                        },
                    }
                ]
            }

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: DuplicateTextClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123"))
    item = result["items"][0]
    assert item["title"] == "Increase budget"
    assert item["body"] == "Increase budget on strong ad sets"
    assert "message" not in item
    assert "recommendation_content" not in item


def test_get_recommendations_handles_unsupported_surface(monkeypatch) -> None:
    class UnsupportedRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            raise recommendations.UnsupportedFeatureError("unsupported")

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: UnsupportedRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123"))
    assert result["supported"] is False
    assert result["items"] == []


def test_typed_opportunity_tools_preserve_unsupported_surface(monkeypatch) -> None:
    class UnsupportedRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            raise recommendations.UnsupportedFeatureError("unsupported")

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: UnsupportedRecommendationsClient())
    result = asyncio.run(recommendations.get_budget_opportunities(account_id="123"))
    assert result["supported"] is False
    assert result["category"] == "budget"


def test_recommendation_tools_reuse_short_lived_cache(monkeypatch) -> None:
    recommendations._RECOMMENDATION_CACHE.clear()
    client = FakeRecommendationsClient()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: client)

    broad = asyncio.run(recommendations.get_recommendations(account_id="123"))
    budget = asyncio.run(recommendations.get_budget_opportunities(account_id="123"))

    assert broad["supported"] is True
    assert budget["summary"]["filtered_from_total"] == 5
    assert client.calls == 1


def test_recommendation_refresh_bypasses_cache(monkeypatch) -> None:
    recommendations._RECOMMENDATION_CACHE.clear()
    client = FakeRecommendationsClient()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: client)

    asyncio.run(recommendations.get_recommendations(account_id="123"))
    asyncio.run(recommendations.get_budget_opportunities(account_id="123", refresh=True))

    assert client.calls == 2


def test_recommendation_paging_is_usable_and_cache_is_cursor_scoped(
    monkeypatch,
) -> None:
    class PagingRecommendationsClient(FakeRecommendationsClient):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[tuple[int, str | None]] = []

        async def get_recommendations(
            self,
            account_id: str,
            *,
            campaign_id=None,
            limit=25,
            after=None,
        ):
            self.calls += 1
            self.requests.append((limit, after))
            suffix = after or "first"
            return {
                "data": [
                    {
                        "id": f"rec_{suffix}",
                        "message": "Increase budget on strong ad sets",
                    }
                ],
                "paging": {
                    "cursors": {"after": f"next_{suffix}"},
                    "next": "next",
                },
            }

    recommendations._RECOMMENDATION_CACHE.clear()
    client = PagingRecommendationsClient()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: client)

    first = asyncio.run(
        recommendations.get_recommendations(account_id="123", limit=10)
    )
    first_again = asyncio.run(
        recommendations.get_recommendations(account_id="123", limit=10)
    )
    first_with_blank_cursor = asyncio.run(
        recommendations.get_recommendations(
            account_id="123",
            limit=10,
            after=" ",
        )
    )
    second = asyncio.run(
        recommendations.get_budget_opportunities(
            account_id="123",
            limit=10,
            after="next_first",
        )
    )

    assert first["paging"]["after"] == "next_first"
    assert first["complete"] is False
    assert first_again["items"][0]["id"] == "rec_first"
    assert first_with_blank_cursor["items"][0]["id"] == "rec_first"
    assert second["paging"]["after"] == "next_next_first"
    assert second["complete"] is False
    assert client.requests == [(10, None), (10, "next_first")]


def test_recommendation_terminal_after_cursor_is_complete(monkeypatch) -> None:
    class TerminalRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, **kwargs):
            return {
                "data": [{"id": "rec_terminal", "message": "Increase budget"}],
                "paging": {"cursors": {"after": "END_CURSOR"}},
            }

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(
        recommendations,
        "get_graph_api_client",
        lambda: TerminalRecommendationsClient(),
    )
    result = asyncio.run(recommendations.get_recommendations(account_id="123"))

    assert result["paging"]["after"] == "END_CURSOR"
    assert result["complete"] is True


def test_recommendation_cache_prunes_expired_and_caps_live_entries(
    monkeypatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(recommendations, "monotonic", lambda: now)
    recommendations._RECOMMENDATION_CACHE.clear()
    payload = {"supported": True, "items": [], "summary": {"count": 0}}

    for index in range(recommendations._RECOMMENDATION_CACHE_MAX_ENTRIES + 20):
        recommendations._store_cached_recommendations(
            ("token", "v26.0", "act_123", "", 25, f"cursor_{index}", None, None),
            payload,
        )

    assert (
        len(recommendations._RECOMMENDATION_CACHE)
        == recommendations._RECOMMENDATION_CACHE_MAX_ENTRIES
    )
    now += recommendations._RECOMMENDATION_CACHE_TTL_SECONDS + 1
    recommendations._store_cached_recommendations(
        ("token", "v26.0", "act_123", "", 25, "fresh", None, None),
        payload,
    )
    assert list(recommendations._RECOMMENDATION_CACHE) == [
        ("token", "v26.0", "act_123", "", 25, "fresh", None, None)
    ]


RECOMMENDATION_TOOLS = (
    "get_recommendations",
    "get_budget_opportunities",
    "get_creative_opportunities",
    "get_audience_opportunities",
    "get_delivery_opportunities",
    "get_bidding_opportunities",
)


@pytest.mark.parametrize("tool_name", RECOMMENDATION_TOOLS)
def test_recommendation_tools_forward_native_filters_without_mapping_categories(
    monkeypatch, tool_name,
) -> None:
    requests = []

    class FilteredClient:
        async def get_recommendations(self, account_id, **kwargs):
            requests.append((account_id, kwargs))
            return {
                "data": [{"id": "rec_1", "message": "budget creative audience delivery bidding"}],
                "paging": {"next": "next", "cursors": {"after": "NEXT"}},
            }

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FilteredClient())
    result = asyncio.run(getattr(recommendations, tool_name)(
        account_id="123", campaign_id="cmp_1", limit=10, after=" PAGE ",
        recommendation_names=" FUTURE_META_RECOMMENDATION, BUDGET_LIMITED ",
        recommendation_stages=" MFR, PCR, PFR ",
    ))

    assert requests == [("act_123", {
        "campaign_id": "cmp_1", "limit": 10, "after": "PAGE",
        "recommendation_names": ["FUTURE_META_RECOMMENDATION", "BUDGET_LIMITED"],
        "recommendation_stages": ["MFR", "PCR", "PFR"],
    })]
    assert result["items"][0]["id"] == "rec_1"
    assert result["paging"]["after"] == "NEXT"
    assert result["complete"] is False


@pytest.mark.parametrize("unsupported", [False, True])
@pytest.mark.parametrize("dimension", [
    "account_id", "campaign_id", "limit", "after", "recommendation_names",
    "recommendation_stages", "api_version", "access_token",
])
def test_recommendation_cache_is_scoped_to_every_request_context(
    monkeypatch, dimension, unsupported,
) -> None:
    calls = []
    settings = SimpleNamespace(access_token="test-token-a", api_version="v26.0")
    monkeypatch.setattr(recommendations, "get_settings", lambda: settings)

    class CacheClient:
        async def get_recommendations(self, account_id, **kwargs):
            calls.append((account_id, kwargs))
            if unsupported:
                raise recommendations.UnsupportedFeatureError("unsupported")
            return {"data": [{"id": f"rec_{len(calls)}", "message": "Increase budget"}]}

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: CacheClient())
    options = {
        "account_id": "123", "campaign_id": "cmp_1", "limit": 10, "after": "PAGE_A",
        "recommendation_names": ["BUDGET_LIMITED"], "recommendation_stages": ["MFR"],
    }
    first = asyncio.run(recommendations.get_recommendations(**options))
    assert asyncio.run(recommendations.get_recommendations(**options)) == first
    assert len(calls) == 1
    alternate = {
        "account_id": "456", "campaign_id": "cmp_2", "limit": 20, "after": "PAGE_B",
        "recommendation_names": ["AB_TEST"], "recommendation_stages": ["PCR"],
        "api_version": "v25.0", "access_token": "test-token-b",
    }[dimension]
    if dimension in {"api_version", "access_token"}:
        setattr(settings, dimension, alternate)
    else:
        options[dimension] = alternate
    second = asyncio.run(recommendations.get_recommendations(**options))
    assert second["supported"] is not unsupported
    assert asyncio.run(recommendations.get_recommendations(**options)) == second
    assert len(calls) == 2


def test_native_filters_do_not_reuse_unfiltered_or_explicit_empty_filter_cache(monkeypatch) -> None:
    calls = []

    class FilterClient:
        async def get_recommendations(self, account_id, **kwargs):
            calls.append(kwargs)
            return {"data": []}

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FilterClient())
    for options in (
        {}, {"recommendation_names": ["BUDGET_LIMITED"]},
        {"recommendation_stages": ["MFR"]}, {"recommendation_names": []},
        {"recommendation_stages": []},
    ):
        asyncio.run(recommendations.get_recommendations(account_id="123", **options))
        asyncio.run(recommendations.get_recommendations(account_id="123", **options))
    assert calls == [
        {"campaign_id": None},
        {"campaign_id": None, "recommendation_names": ["BUDGET_LIMITED"]},
        {"campaign_id": None, "recommendation_stages": ["MFR"]},
        {"campaign_id": None, "recommendation_names": []},
        {"campaign_id": None, "recommendation_stages": []},
    ]


@pytest.mark.parametrize("options, expected_message", [
    ({"recommendation_names": [""]}, "nonblank strings"),
    ({"recommendation_names": "BUDGET_LIMITED,,AB_TEST"}, "nonblank strings"),
    ({"recommendation_names": " "}, "nonblank strings"),
    ({"recommendation_names": [123]}, "nonblank strings"),
    ({"recommendation_names": {"name": "AB_TEST"}}, "nonblank strings"),
    ({"recommendation_stages": ["MFR", "unknown"]}, "MFR, PCR, or PFR"),
    ({"recommendation_stages": "mfr"}, "MFR, PCR, or PFR"),
    ({"recommendation_stages": "MFR,"}, "nonblank strings"),
])
def test_native_recommendation_filters_reject_invalid_direct_input_before_client(
    monkeypatch, options, expected_message,
) -> None:
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: pytest.fail("client created"))
    with pytest.raises(recommendations.ValidationError, match=expected_message):
        asyncio.run(recommendations.get_recommendations(account_id="123", **options))


@pytest.mark.parametrize("tool_name", RECOMMENDATION_TOOLS)
@pytest.mark.parametrize("routed", [False, True])
def test_native_recommendation_filters_work_through_mcp_coercion(monkeypatch, tool_name, routed) -> None:
    requests = []

    class FilterClient:
        async def get_recommendations(self, account_id, **kwargs):
            requests.append(kwargs)
            return {"data": []}

    recommendations._RECOMMENDATION_CACHE.clear()
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FilterClient())
    arguments = {
        "account_id": "123", "recommendation_names": " BUDGET_LIMITED, AB_TEST ",
        "recommendation_stages": " MFR, PCR ",
    }
    result = asyncio.run(mcp_server.call_tool(
        "call_tool" if routed else tool_name,
        {"name": tool_name, "arguments": arguments} if routed else arguments,
    ))
    assert result.structured_content["supported"] is True
    assert requests == [{
        "campaign_id": None, "recommendation_names": ["BUDGET_LIMITED", "AB_TEST"],
        "recommendation_stages": ["MFR", "PCR"],
    }]


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("options", [
    {"recommendation_names": "BUDGET_LIMITED,,AB_TEST"},
    {"recommendation_names": [123]},
    {"recommendation_stages": "UNKNOWN"},
])
def test_invalid_native_recommendation_filters_fail_through_mcp_before_client(
    monkeypatch, routed, options,
) -> None:
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: pytest.fail("client created"))
    arguments = {"account_id": "123", **options}
    with pytest.raises((ToolError, FastMCPValidationError)):
        asyncio.run(mcp_server.call_tool(
            "call_tool" if routed else "get_recommendations",
            {"name": "get_recommendations", "arguments": arguments} if routed else arguments,
        ))


@pytest.mark.parametrize("tool_name", RECOMMENDATION_TOOLS)
def test_recommendation_filter_schemas_and_search_are_discoverable(tool_name) -> None:
    capabilities = asyncio.run(utility.get_capabilities(tool_name=tool_name))
    tool = capabilities["tool"]
    properties = tool["input_schema"]["properties"]
    for parameter in ("recommendation_names", "recommendation_stages"):
        assert properties[parameter]["default"] is None
        assert {part.get("type") for part in properties[parameter]["anyOf"]} == {
            "string", "array", "null",
        }
    assert "MFR/PCR/PFR" in tool["description"]
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": tool_name}))
    text = "\n".join(content.text for content in result.content if content.type == "text")
    assert "recommendation_names" in text
    assert "recommendation_stages" in text
