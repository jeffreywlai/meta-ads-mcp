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


def test_get_capabilities_lists_new_helpers() -> None:
    result = asyncio.run(utility.get_capabilities())
    assert "health_check" in result["tool_groups"]["utility"]
    assert "compare_performance" in result["tool_groups"]["analysis"]
    assert "export_insights" in result["tool_groups"]["analysis"]
    assert result["routing_hints"]["compare_multiple_entities"] == ["compare_performance"]
