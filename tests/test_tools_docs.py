"""Docs tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import docs


def test_get_metrics_reference_returns_content() -> None:
    result = asyncio.run(docs.get_metrics_reference())
    assert result["name"] == "insights_metrics"
    assert "Core metrics" in result["content"]
