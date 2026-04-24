"""Coordinator / FastMCP server configuration tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp import stdio  # noqa: F401 - ensures tools are registered
from meta_ads_mcp.config import Settings
from meta_ads_mcp.coordinator import (
    ALWAYS_VISIBLE_TOOLS,
    mcp_server,
    serialize_search_results_compact,
)
from meta_ads_mcp.tools import discovery, insights, utility


def test_fastmcp_31_search_transform_is_configured() -> None:
    transforms = getattr(mcp_server, "transforms", [])
    assert transforms
    transform = transforms[0]
    assert type(transform).__name__ == "BM25SearchTransform"
    assert sorted(getattr(transform, "_always_visible", set())) == sorted(ALWAYS_VISIBLE_TOOLS)
    assert getattr(transform, "_search_result_serializer", None) is serialize_search_results_compact


def test_list_tools_exposes_compact_search_surface() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    names = [tool.name for tool in tools]
    assert names == [
        "list_ad_accounts",
        "health_check",
        "get_capabilities",
        "search_tools",
        "call_tool",
    ]


def test_historical_missing_tools_remain_visible_on_compact_surface() -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.list_tools())}
    assert {"health_check", "list_ad_accounts"} <= names


def test_historical_missing_tools_respond_through_tool_layer(monkeypatch) -> None:
    class FakeDiscoveryClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            assert parent_id == "me"
            assert edge == "adaccounts"
            return {"data": [{"id": "act_123", "name": "Test Account", "account_status": 1}]}

    monkeypatch.setattr(
        utility,
        "get_settings",
        lambda: Settings(
            access_token=None,
            api_version="v25.0",
            default_account_id=None,
            app_id=None,
            app_secret=None,
            redirect_uri=None,
            log_level="INFO",
            host="127.0.0.1",
            port=8000,
            request_timeout=30.0,
            max_retries=2,
        ),
    )
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())

    health = asyncio.run(mcp_server.call_tool("health_check", {}))
    accounts = asyncio.run(mcp_server.call_tool("list_ad_accounts", {"limit": 1}))

    assert health.structured_content["status"] == "unhealthy"
    assert accounts.structured_content["items"][0]["id"] == "act_123"


def test_search_routes_feedback_and_action_count_workflows() -> None:
    async def search(query: str) -> str:
        result = await mcp_server.call_tool("search_tools", {"query": query})
        return result.structured_content["result"]

    feedback = asyncio.run(search("feedback reviews testimonials"))
    raw_comments = asyncio.run(search("facebook ad comments"))
    page_reviews = asyncio.run(search("page reviews testimonials"))
    actions = asyncio.run(search("how many appointments campaign trailing 30 days"))
    campaign_lookup = asyncio.run(search("find campaign by name"))
    terse_campaigns = asyncio.run(search("campaigns"))
    terse_actions = asyncio.run(search("appointments last 30 days"))
    terse_pause = asyncio.run(search("pause ad set"))

    assert feedback.splitlines()[1].startswith("- `get_ad_feedback_signals`")
    assert raw_comments.splitlines()[1].startswith("- `list_ad_comments`")
    assert page_reviews.splitlines()[1].startswith("- `list_page_recommendations`")
    assert actions.splitlines()[1].startswith("- `summarize_actions`")
    assert campaign_lookup.splitlines()[1].startswith("- `list_campaigns`")
    assert terse_campaigns.splitlines()[1].startswith("- `list_campaigns`")
    assert terse_actions.splitlines()[1].startswith("- `summarize_actions`")
    assert terse_pause.splitlines()[1].startswith("- `set_adset_status`")


def test_compare_performance_responds_through_tool_layer(monkeypatch) -> None:
    class FakeInsightsClient:
        async def get_insights(self, object_id: str, *, fields, params):
            assert object_id == "act_123"
            assert params["level"] == "campaign"
            return {
                "data": [
                    {
                        "campaign_id": "cmp_1",
                        "campaign_name": "Campaign One",
                        "spend": "100",
                        "impressions": "1000",
                        "clicks": "50",
                    }
                ]
            }

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeInsightsClient())

    result = asyncio.run(
        mcp_server.call_tool(
            "compare_performance",
            {
                "level": "campaign",
                "object_ids": ["act_123"],
                "date_preset": "last_30d",
                "metrics": ["spend", "clicks"],
            },
        )
    )

    summary = result.structured_content["summary"]
    assert summary["successful"] == 1
    assert summary["failed"] == 0


def test_compact_search_serializer_returns_minimal_markdown() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    tools = [
        components["tool:get_entity_insights@"],
        components["tool:compare_performance@"],
    ]
    result = serialize_search_results_compact(tools)
    assert "Matches:" in result
    assert "`get_entity_insights` | req: level, object_id" in result
    assert "`compare_performance` | req: level, object_ids" in result
    assert "properties" not in result
    assert "additionalProperties" not in result
    assert "Next: use `call_tool`" in result


def test_compact_search_serializer_surfaces_required_archive_params() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    result = serialize_search_results_compact([components["tool:search_ads_archive@"]])
    assert "`search_ads_archive` | req: search_terms, ad_reached_countries" in result
    assert "opt: ad_type, limit, fields" in result


def test_compact_search_serializer_surfaces_required_targeting_category_params() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    result = serialize_search_results_compact([components["tool:get_targeting_categories@"]])
    assert "`get_targeting_categories` | req: category_class" in result
    assert "opt: query, account_id, limit" in result
