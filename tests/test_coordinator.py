"""Coordinator / FastMCP server configuration tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp import stdio  # noqa: F401 - ensures tools are registered
from meta_ads_mcp.coordinator import (
    ALWAYS_VISIBLE_TOOLS,
    mcp_server,
    serialize_search_results_compact,
)


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
