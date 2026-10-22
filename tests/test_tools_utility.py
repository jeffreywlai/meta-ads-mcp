"""Utility tool tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastmcp import Client
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
import pytest

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.tools import utility
from meta_ads_mcp import overflow
from meta_ads_mcp.overflow import OverflowArtifactStore


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


def test_get_capabilities_returns_live_named_tool_schema() -> None:
    result = asyncio.run(utility.get_capabilities(tool_name="get_entity_insights"))
    tool = asyncio.run(mcp_server.get_tool("get_entity_insights"))
    assert result["tool"]["input_schema"] == tool.parameters
    assert "flatten_actions" in result["tool"]["input_schema"]["properties"]


@pytest.mark.parametrize(("query", "expected"), [("creative fatigue report", "get_creative_fatigue_report"), ("list_ads", "list_ads")])
def test_free_text_capabilities_reuse_live_search(query, expected) -> None:
    result = asyncio.run(utility.get_capabilities(intent=query))
    assert f"`{expected}`" in result["tool_matches"]


@pytest.mark.parametrize("options", [{"tool_name": ""}, {"tool_name": "get_ad", "intent": "list_ads"}])
def test_named_schema_rejects_ambiguous_request(options) -> None:
    with pytest.raises(ValidationError, match="tool_name alone"):
        asyncio.run(utility.get_capabilities(**options))


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
    assert result["server"]["fastmcp_version_target"] == "3.4.8"
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
    assert result["suggested_search"] == {
        "tool": "search_tools",
        "arguments": {"query": "customer feedback product reviews testimonials"},
    }


def test_get_capabilities_routes_terse_intents() -> None:
    pause = asyncio.run(utility.get_capabilities(intent="pause ad set"))
    campaigns = asyncio.run(utility.get_capabilities(intent="campaigns"))
    appointments = asyncio.run(utility.get_capabilities(intent="appointments last 30 days"))

    assert pause["closest_intents"][0]["intent"] == "writes_after_confirmation"
    assert campaigns["closest_intents"][0]["intent"] == "discover_accounts_or_ids"
    assert appointments["closest_intents"][0]["intent"] == "inspect_single_entity_performance"


def test_get_capabilities_routes_audience_metadata_through_discovery() -> None:
    result = asyncio.run(
        utility.get_capabilities(intent="lightweight audience metadata")
    )

    assert result["closest_intents"][0]["intent"] == "discover_accounts_or_ids"


def test_get_capabilities_has_overflow_retrieval_intent() -> None:
    fuzzy = asyncio.run(
        utility.get_capabilities(intent="retrieve oversized response")
    )
    exact = asyncio.run(
        utility.get_capabilities(intent="retrieve_oversized_response")
    )

    assert fuzzy["closest_intents"][0]["intent"] == "retrieve_oversized_response"
    assert exact["selected_intent"]["recommended_order"] == [
        "read_overflow_artifact",
        "delete_overflow_artifact",
    ]


def test_overflow_reader_defaults_to_parsed_items_and_retains_byte_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    payload = {"rows": {"items": [{"id": str(index)} for index in range(200)],
                         "summary": {"count": 200}}}
    store = OverflowArtifactStore(tmp_path)
    monkeypatch.setattr(utility, "OVERFLOW_ARTIFACT_STORE", store)
    export_id, _ = store.create(ToolResult(structured_content=payload), tool_name="async_report")
    first = asyncio.run(utility.read_overflow_artifact(export_id, item_limit=50))
    assert first["mode"] == "items"
    assert first["item_path"] == ["rows", "items"]
    assert first["next_item_offset"] == 50
    second = asyncio.run(utility.read_overflow_artifact(export_id, item_offset=50, item_limit=50))
    assert second["items"][0]["id"] == "50"
    raw = asyncio.run(utility.read_overflow_artifact(export_id, offset=0))
    assert raw["offset"] == 0
    assert json.loads(raw["data"])["tool_result"]["structured_content"] == payload
    with pytest.raises(ValidationError, match="not both"):
        asyncio.run(utility.read_overflow_artifact(export_id, offset=0, item_offset=0))


def test_overflow_reader_defaults_to_bytes_without_a_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    monkeypatch.setattr(utility, "OVERFLOW_ARTIFACT_STORE", store)
    export_id, _ = store.create(ToolResult(content=[TextContent(type="text", text="original")]), tool_name="text_tool")
    chunk = asyncio.run(utility.read_overflow_artifact(export_id))
    assert json.loads(chunk["data"])["tool_result"]["content"][0]["text"] == "original"
    with pytest.raises(ValidationError, match="no items/rows"):
        asyncio.run(utility.read_overflow_artifact(export_id, item_offset=0))


def test_overflow_reader_issues_only_the_final_safe_byte_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    monkeypatch.setattr(utility, "OVERFLOW_ARTIFACT_STORE", store)
    export_id, _ = store.create(
        ToolResult(content=[TextContent(type="text", text='\\"' * 50_000)]), tool_name="text_tool",
    )
    chunk = asyncio.run(utility.read_overflow_artifact(export_id, offset=0))
    assert chunk["next_offset"] < 44_000
    assert len(overflow._serialized_mcp_result(overflow.compact_overflow_chunk_result(chunk))) <= 63_000
    with pytest.raises(ValidationError, match="issued next_offset"):
        asyncio.run(utility.read_overflow_artifact(export_id, offset=44_000))


def test_proxy_keeps_43kb_list_inline_through_a_real_mcp_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from meta_ads_mcp.coordinator import RESPONSE_LIMITING_MIDDLEWARE
    from meta_ads_mcp.tools import discovery

    ads = [{"id": str(index), "name": f"Ad {index}",
            "creative": {"id": f"creative_{index}", "body": "x" * 450}}
           for index in range(81)]

    class FakeAdsClient:
        async def list_objects(self, *_args, **_kwargs):
            return {"data": ads}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeAdsClient())
    monkeypatch.setattr(RESPONSE_LIMITING_MIDDLEWARE.artifact_store, "export_directory", tmp_path)

    async def run():
        async with Client(mcp_server) as client:
            tools = await client.list_tools()
            proxy = next(tool for tool in tools if tool.name == "call_tool")
            assert proxy.outputSchema is None
            return await client.call_tool("call_tool", {
                "name": "list_ads",
                "arguments": {"account_id": "act_123", "limit": 200,
                              "fields": ["id", "name", "creative"]},
            })

    result = asyncio.run(run())
    assert result.is_error is False
    assert result.structured_content is None
    inline = json.loads(result.content[0].text)
    assert inline["items"] == ads
    assert 40_000 < len(result.content[0].text.encode()) < 50_000
    assert len(overflow._serialized_mcp_result(result)) <= 64_000
    assert not list(tmp_path.glob("overflow-*.json"))


@pytest.mark.parametrize("archive_fails", [False, True])
def test_direct_client_receives_schema_valid_overflow_notices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, archive_fails: bool,
) -> None:
    from meta_ads_mcp.coordinator import RESPONSE_LIMITING_MIDDLEWARE
    from meta_ads_mcp.tools import discovery

    accounts = [{"id": f"act_{index}", "name": "x" * 1_000} for index in range(200)]

    class FakeAccountsClient:
        async def list_objects(self, *_args, **_kwargs):
            return {"data": accounts}

    artifact_directory = tmp_path / "artifacts"
    if archive_fails:
        artifact_directory.write_text("blocked")
    monkeypatch.setattr(discovery, "get_graph_api_client", FakeAccountsClient)
    store = RESPONSE_LIMITING_MIDDLEWARE.artifact_store
    monkeypatch.setattr(store, "export_directory", artifact_directory)

    async def run():
        async with Client(mcp_server) as client:
            tools = await client.list_tools()
            tool = next(tool for tool in tools if tool.name == "list_ad_accounts")
            assert tool.outputSchema == {"type": "object", "additionalProperties": True}
            return await client.call_tool("list_ad_accounts", {"limit": 200})

    result = asyncio.run(run())
    assert result.is_error is False
    assert result.structured_content["overflow"] == result.meta["overflow"]
    assert result.structured_content["message"] == result.content[0].text
    assert result.meta["overflow"]["archived"] is not archive_fails
    assert len(overflow._serialized_mcp_result(result)) <= 64_000
    if archive_fails:
        assert "could not be archived" in result.content[0].text
        assert "export_id" not in result.meta["overflow"]
    else:
        export_id = result.meta["overflow"]["export_id"]
        artifact = json.loads(store._artifact_path(export_id).read_text())
        assert artifact["tool_result"]["structured_content"]["items"] == accounts


def test_get_capabilities_has_feedback_intent() -> None:
    result = asyncio.run(utility.get_capabilities(intent="read_ad_comments_or_quality_signals"))
    assert result["selected_intent"]["recommended_order"][0] == "list_ad_comments"
    assert any("comments" in note for note in result["selected_intent"]["notes"])


def test_list_mutation_tools_returns_write_catalog() -> None:
    result = asyncio.run(utility.list_mutation_tools())
    assert result["count"] == len(utility.TOOL_GROUPS["writes"])
    assert "set_campaign_status" in result["common_paths"]["pause_or_enable"]
    assert result["safety_notes"]
