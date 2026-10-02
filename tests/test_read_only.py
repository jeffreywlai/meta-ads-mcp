"""Read-only advertising mode must protect execution, not only discovery."""

import asyncio

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import MiddlewareContext
from mcp.types import CallToolRequestParams

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.read_only import ReadOnlyAdvertisingMiddleware, read_only_tool_names
from meta_ads_mcp.tools import insights, utility

BLOCKED_TOOLS = utility.TOOL_GROUPS["writes"] + [
    "upload_creative_asset", "setup_ab_test", "exchange_code_for_token",
    "refresh_to_long_lived_token", "generate_system_user_token",
]


@pytest.fixture
def read_only(monkeypatch):
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()


@pytest.mark.parametrize("name", BLOCKED_TOOLS + ["create_adset"])
@pytest.mark.parametrize("routed", [True, False])
def test_mutations_are_blocked_before_arguments_or_execution(read_only, name, routed) -> None:
    tool = "call_tool" if routed else name
    arguments = {"name": name, "arguments": {}} if routed else {}
    with pytest.raises(ToolError, match="META_READ_ONLY"):
        asyncio.run(mcp_server.call_tool(tool, arguments))


def test_unknown_tools_fail_closed(read_only) -> None:
    async def should_not_run(context):
        pytest.fail("Unknown tool must not execute")

    context = MiddlewareContext(message=CallToolRequestParams(name="get_unknown_action", arguments={}))
    with pytest.raises(ToolError, match="not permitted"):
        asyncio.run(ReadOnlyAdvertisingMiddleware().on_call_tool(context, should_not_run))


def test_policy_covers_the_existing_catalog_explicitly() -> None:
    catalog = {name for names in utility.TOOL_GROUPS.values() for name in names}
    assert read_only_tool_names() & catalog == catalog - set(BLOCKED_TOOLS)


def test_read_only_search_hides_mutations_and_manifest_discloses_mode(read_only) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": "create an ad"}))
    assert "`create_ad`" not in result.content[0].text
    result = asyncio.run(mcp_server.call_tool("get_capabilities", {"include_full_manifest": True})).structured_content
    assert result["server"]["read_only"] is True
    assert result["tool_groups"]["writes"] == []
    assert "exchange_code_for_token" not in result["tool_groups"]["auth"]
    mutations = asyncio.run(mcp_server.call_tool("list_mutation_tools", {})).structured_content
    assert mutations["count"] == 0


def test_read_only_keeps_routed_reads_and_async_reporting_jobs(read_only, monkeypatch) -> None:
    class ReadClient:
        async def get_insights(self, object_id, *, fields, params):
            return {"data": [{"spend": "10"}]}

        async def create_async_insights_report(self, object_id, *, fields, params):
            return {"report_run_id": "job-1"}

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: ReadClient())
    for name in ("get_entity_insights", "create_async_insights_report"):
        result = asyncio.run(mcp_server.call_tool("call_tool", {
            "name": name, "arguments": {"level": "ad", "object_id": "ad_1"},
        }))
        assert result.structured_content is not None


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)])
def test_read_only_config_values(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("META_READ_ONLY", value)
    assert reload_settings().read_only is expected


def test_read_only_typo_does_not_silently_disable_protection(monkeypatch) -> None:
    with monkeypatch.context() as context:
        context.setenv("META_READ_ONLY", "ture")
        with pytest.raises(ValueError, match="META_READ_ONLY"):
            reload_settings()
    reload_settings()
