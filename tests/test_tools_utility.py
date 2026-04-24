"""Utility tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.tools import utility


class FakeUtilityClient:
    """Fake API client for health checks."""

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        assert parent_id == "me"
        assert edge == "adaccounts"
        assert fields == ["id", "name", "account_status"]
        assert params == {"limit": 3}
        return {
            "data": [
                {"id": "act_123", "name": "Test Account", "account_status": 1},
            ]
        }


def test_health_check_returns_healthy_status(monkeypatch) -> None:
    monkeypatch.setattr(utility, "get_graph_api_client", lambda: FakeUtilityClient())
    result = asyncio.run(utility.health_check())
    assert result["status"] == "healthy"
    assert result["accessible_account_count_sample"] == 1


def test_health_check_reports_missing_token(monkeypatch) -> None:
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    reload_settings()
    result = asyncio.run(utility.health_check())
    assert result["status"] == "unhealthy"
    assert result["meta_api_connection"] == "not_attempted"


def test_get_capabilities_returns_compact_summary_by_default() -> None:
    result = asyncio.run(utility.get_capabilities())
    assert "tool_groups" not in result
    assert "routing_hints" not in result
    assert "intent_guide" not in result
    assert "find_optimization_opportunities" in result["valid_intents"]
    assert result["tool_group_counts"]["analysis"] >= 1
    assert result["tool_group_counts"]["optimization"] >= 1
    assert result["recommended_start"]["if_the_needed_tool_is_not_visible"] == "search_tools"
    assert result["recommended_start"]["if_you_need_the_entire_manifest"] == "get_capabilities(include_full_manifest=true)"
    assert any("Ads Library API access" in note for note in result["notes"])
    assert "meta://docs/tool-routing" in result["resources"]
    assert result["server"]["fastmcp_version_target"] == "3.1.0"
    assert result["server"]["tool_search_enabled"] is True
    assert result["server"]["dynamic_search_tools"] == ["search_tools", "call_tool"]


def test_get_capabilities_can_return_full_manifest() -> None:
    result = asyncio.run(utility.get_capabilities(include_full_manifest=True))
    assert "health_check" in result["tool_groups"]["utility"]
    assert "list_mutation_tools" in result["tool_groups"]["utility"]
    assert "summarize_actions" in result["tool_groups"]["analysis"]
    assert "get_ad_feedback_signals" in result["tool_groups"]["optimization"]
    assert "list_ad_comments" in result["tool_groups"]["social_feedback"]
    assert "list_page_recommendations" in result["tool_groups"]["social_feedback"]
    assert "compare_performance" in result["tool_groups"]["analysis"]
    assert "export_insights" in result["tool_groups"]["analysis"]
    assert "get_account_pages" in result["tool_groups"]["discovery"]
    assert "list_instagram_accounts" in result["tool_groups"]["discovery"]
    assert "create_ad" in result["tool_groups"]["writes"]
    assert "update_adset_bid_amount" in result["tool_groups"]["writes"]
    assert "get_targeting_categories" in result["tool_groups"]["planning"]
    assert "get_budget_opportunities" in result["tool_groups"]["optimization"]
    assert "search_ads_archive" in result["tool_groups"]["research"]
    assert result["routing_hints"]["compare_multiple_entities"][0] == "compare_performance"
    assert "get_bidding_opportunities" in result["routing_hints"]["find_optimization_opportunities"]


def test_get_capabilities_can_return_compact_intent_guide() -> None:
    result = asyncio.run(utility.get_capabilities(intent="find_optimization_opportunities"))
    assert result["selected_intent"]["intent"] == "find_optimization_opportunities"
    assert result["selected_intent"]["recommended_order"][0] == "get_account_optimization_snapshot"
    assert "get_budget_opportunities" in result["selected_intent"]["avoid_unless_needed"]
    assert "meta://docs/tool-routing" in result["resources"]


def test_get_capabilities_falls_forward_on_unknown_intent() -> None:
    result = asyncio.run(utility.get_capabilities(intent="customer feedback product reviews testimonials"))
    assert result["unmatched_intent"] == "customer feedback product reviews testimonials"
    assert result["closest_intents"][0]["intent"] == "read_ad_comments_or_quality_signals"
    assert result["suggested_search"]["tool"] == "search_tools"


def test_get_capabilities_routes_terse_intents() -> None:
    pause = asyncio.run(utility.get_capabilities(intent="pause ad set"))
    campaigns = asyncio.run(utility.get_capabilities(intent="campaigns"))
    appointments = asyncio.run(utility.get_capabilities(intent="appointments last 30 days"))

    assert pause["closest_intents"][0]["intent"] == "writes_after_confirmation"
    assert campaigns["closest_intents"][0]["intent"] == "discover_accounts_or_ids"
    assert appointments["closest_intents"][0]["intent"] == "inspect_single_entity_performance"


def test_get_capabilities_has_feedback_intent() -> None:
    result = asyncio.run(utility.get_capabilities(intent="read_ad_comments_or_quality_signals"))
    assert result["selected_intent"]["recommended_order"][0] == "list_ad_comments"
    assert any("comments" in note for note in result["selected_intent"]["notes"])


def test_list_mutation_tools_returns_write_catalog() -> None:
    result = asyncio.run(utility.list_mutation_tools())
    assert result["count"] == len(utility.TOOL_GROUPS["writes"])
    assert "set_campaign_status" in result["common_paths"]["pause_or_enable"]
    assert result["safety_notes"]
