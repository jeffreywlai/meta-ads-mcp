"""Coordinator / FastMCP server configuration tests."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp.exceptions import ToolError

from meta_ads_mcp import stdio  # noqa: F401 - ensures tools are registered
from meta_ads_mcp.config import Settings
from meta_ads_mcp.coordinator import (
    ALWAYS_VISIBLE_TOOLS,
    MAX_TOOL_RESPONSE_BYTES,
    RESPONSE_LIMIT_HINT,
    RESPONSE_LIMITING_MIDDLEWARE,
    mcp_server,
    serialize_search_results_compact,
)
from meta_ads_mcp.errors import MetaApiError
from meta_ads_mcp.tools import discovery, insights, utility


def test_fastmcp_347_search_transform_is_configured() -> None:
    transforms = getattr(mcp_server, "transforms", [])
    assert transforms
    transform = transforms[0]
    assert type(transform).__name__ == "IntentAwareBM25SearchTransform"
    assert sorted(getattr(transform, "_always_visible", set())) == sorted(ALWAYS_VISIBLE_TOOLS)
    assert getattr(transform, "_search_result_serializer", None) is serialize_search_results_compact


def test_response_size_guard_is_configured() -> None:
    middleware = RESPONSE_LIMITING_MIDDLEWARE
    assert type(middleware).__name__ == "ArchivedResponseLimitingMiddleware"
    assert middleware.max_size == MAX_TOOL_RESPONSE_BYTES
    assert middleware.truncation_suffix == RESPONSE_LIMIT_HINT
    assert mcp_server.middleware[-2] is RESPONSE_LIMITING_MIDDLEWARE
    assert type(mcp_server.middleware[-1]).__name__ == "StructuredMetaErrorMiddleware"


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


def test_call_tool_proxy_accepts_alias_name_and_stringified_arguments(monkeypatch) -> None:
    class AliasDiscoveryClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            assert parent_id == "act_123"
            assert edge == "adsets"
            assert params["limit"] == 1
            return {
                "data": [
                    {"id": "adset_1", "account_id": "123", "name": "Alias result"}
                ]
            }

        async def get_object(self, object_id: str, *, fields=None, params=None):
            assert object_id == "act_123"
            assert fields == ["currency"]
            return {"currency": "USD"}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: AliasDiscoveryClient())
    result = asyncio.run(
        mcp_server.call_tool(
            "call_tool",
            {
                "tool_name": "list_ad_sets",
                "arguments": json.dumps({"account_id": "123", "limit": 1}),
            },
        )
    )
    assert result.structured_content["items"][0]["id"] == "adset_1"

    direct = asyncio.run(
        mcp_server.call_tool(
            "list_ad_sets",
            {"account_id": "123", "limit": 1},
        )
    )
    assert direct.structured_content["items"][0]["id"] == "adset_1"


def test_call_tool_proxy_schema_documents_both_compatible_envelopes() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.list_tools())}
    properties = tools["call_tool"].parameters["properties"]
    assert {"name", "tool_name", "arguments"} <= set(properties)
    argument_types = properties["arguments"]["anyOf"]
    assert {schema.get("type") for schema in argument_types} == {"object", "string", "null"}


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


def test_tool_layer_returns_allowlisted_structured_meta_errors(monkeypatch) -> None:
    class FailingDiscoveryClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            raise MetaApiError(
                "Invalid parameter",
                code=100,
                user_message="Check the requested filter.",
                details={"access_token": "must-not-leak"},
            )

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FailingDiscoveryClient())
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(mcp_server.call_tool("list_campaigns", {"account_id": "123"}))

    payload = json.loads(str(exc_info.value))["error"]
    assert payload["code"] == 100
    assert payload["user_message"] == "Check the requested filter."
    assert "access_token" not in str(exc_info.value)


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


def test_live_search_routes_new_workflow_language_to_exact_tools() -> None:
    cases = {
        "submit multiple breakdown reports": "create_async_insights_report_batch",
        "delete an ad set": "delete_adset",
        "create target ROAS ad set with bid constraints": "create_ad_set",
        "create creative": "create_ad_creative",
        "insights with flattened purchase and purchase value columns": "get_entity_insights",
    }
    for query, expected in cases.items():
        result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))
        assert f"- `{expected}`" in result.content[0].text.splitlines()[1]
