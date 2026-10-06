"""Docs tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import docs


def test_get_metrics_reference_returns_content() -> None:
    result = asyncio.run(docs.get_metrics_reference())
    assert result["name"] == "insights_metrics"
    assert "Core metrics" in result["content"]


def test_v26_notes_tool_and_resource_share_current_guidance() -> None:
    result = asyncio.run(docs.get_v26_notes())
    assert result["name"] == "v26_notes"
    assert result["content"] == docs.resource_v26_notes()
    assert "recommendation_names" in result["content"]
    assert "recommendation_stages" in result["content"]
    assert "placement_path" in result["content"]
    assert "media_optimization_spec" in result["content"]
    assert "not proof" in result["content"]


def test_tool_routing_resource_returns_routing_guide() -> None:
    result = docs.resource_tool_routing()
    assert "Tool Routing Guide" in result
    assert "find_optimization_opportunities" in result
