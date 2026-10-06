"""Coordinator / FastMCP server configuration tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
from fastmcp.exceptions import ToolError

from meta_ads_mcp import stdio  # noqa: F401 - ensures tools are registered
from meta_ads_mcp.config import Settings, reload_settings
from meta_ads_mcp.coordinator import (
    ALWAYS_VISIBLE_TOOLS,
    MAX_TOOL_RESPONSE_BYTES,
    RESPONSE_LIMIT_HINT,
    RESPONSE_LIMITING_MIDDLEWARE,
    mcp_server,
    serialize_search_results_compact,
)
from meta_ads_mcp.errors import MetaApiError
from meta_ads_mcp.tools import diagnostics, discovery, insights, utility


def _run_isolated_import(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["META_READ_ONLY"] = "false"
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_installed_fastmcp_bootstraps_and_searches_in_clean_process() -> None:
    result = _run_isolated_import("""
        import asyncio
        from meta_ads_mcp import stdio

        result = asyncio.run(stdio.mcp_server.call_tool(
            "search_tools", {"query": "create creative"},
        ))
        assert "- `create_ad_creative`" in result.content[0].text.splitlines()[1]
    """)
    assert result.returncode == 0, result.stderr


def test_missing_fastmcp_reports_required_dependency_in_clean_process() -> None:
    result = _run_isolated_import("""
        import builtins

        real_import = builtins.__import__
        def without_fastmcp(name, *args, **kwargs):
            if name == "fastmcp":
                raise ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
            return real_import(name, *args, **kwargs)
        builtins.__import__ = without_fastmcp

        import meta_ads_mcp.stdio
    """)
    assert result.returncode != 0
    assert "ImportError: FastMCP is required to run Meta Ads MCP." in result.stderr
    assert "uv sync" in result.stderr
    assert "AttributeError" not in result.stderr
    assert "TypeError" not in result.stderr


@pytest.mark.parametrize("module_name", ["fastmcp", "meta_ads_mcp.error_middleware"])
def test_missing_other_required_module_keeps_original_import_error(module_name: str) -> None:
    result = _run_isolated_import(f"""
        import builtins

        real_import = builtins.__import__
        def missing_dependency(name, *args, **kwargs):
            if name == {module_name!r}:
                raise ModuleNotFoundError(
                    "No module named 'other_required_dependency'",
                    name="other_required_dependency",
                )
            return real_import(name, *args, **kwargs)
        builtins.__import__ = missing_dependency

        import meta_ads_mcp.stdio
    """)
    assert result.returncode != 0
    assert "ModuleNotFoundError: No module named 'other_required_dependency'" in result.stderr
    assert "FastMCP is required" not in result.stderr
    assert "AttributeError" not in result.stderr
    assert "TypeError" not in result.stderr


def test_fastmcp_symbol_import_error_is_not_relabelled_as_missing_package() -> None:
    result = _run_isolated_import("""
        import builtins

        real_import = builtins.__import__
        def incompatible_fastmcp(name, *args, **kwargs):
            if name == "fastmcp":
                raise ImportError("cannot import name 'Context' from 'fastmcp'")
            return real_import(name, *args, **kwargs)
        builtins.__import__ = incompatible_fastmcp

        import meta_ads_mcp.stdio
    """)
    assert result.returncode != 0
    assert "ImportError: cannot import name 'Context' from 'fastmcp'" in result.stderr
    assert "FastMCP is required" not in result.stderr


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
    assert mcp_server.middleware[-4] is RESPONSE_LIMITING_MIDDLEWARE
    assert type(mcp_server.middleware[-3]).__name__ == "StructuredMetaErrorMiddleware"
    assert type(mcp_server.middleware[-2]).__name__ == "ReadOnlyAdvertisingMiddleware"
    assert type(mcp_server.middleware[-1]).__name__ == "ToolParameterHelpMiddleware"


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
    assert "`get_entity_insights` | req: level (string), object_id (string)" in result
    assert "`compare_performance` | req: level (string), object_ids (string or list[string])" in result
    assert "fields (string or list[string] or null)" in result
    assert "filtering (list[object] or null)" in result
    assert "limit (integer)" in result
    assert "fetch_all (boolean)" in result
    assert "properties" not in result
    assert "additionalProperties" not in result
    assert "Next: use `call_tool`" in result
    for tool in tools:
        line = next(line for line in result.splitlines() if f"`{tool.name}`" in line)
        for name in tool.parameters["properties"]:
            assert name in line
        assert "+" not in line


def test_every_registered_tool_has_all_parameter_names_and_types_in_search() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    for key, tool in components.items():
        if not key.startswith("tool:"):
            continue
        result = serialize_search_results_compact([tool])
        for name in tool.parameters.get("properties", {}):
            assert f"{name} (" in result.splitlines()[1]
        assert "+" not in result


@pytest.mark.parametrize("routed", [False, True])
def test_unknown_arguments_list_accepted_parameters_before_fetch(monkeypatch, routed) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: pytest.fail("must not fetch"))
    arguments = {"account_id": "act_123", "not_a_parameter": True}
    if routed:
        arguments = {"name": "list_ads", "arguments": arguments}
    with pytest.raises(ToolError, match="Accepted parameters: account_id, campaign_id, adset_id"):
        asyncio.run(mcp_server.call_tool("call_tool" if routed else "list_ads", arguments))


@pytest.mark.parametrize("routed", [False, True])
def test_every_registered_tool_lists_all_accepted_keywords_before_execution(routed) -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    for key, component in components.items():
        if not key.startswith("tool:"):
            continue
        arguments = {"not_a_parameter": "must-not-be-echoed"}
        if routed:
            arguments = {"name": component.name, "arguments": arguments}
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(mcp_server.call_tool("call_tool" if routed else component.name, arguments))
        expected = ", ".join(component.parameters.get("properties", {})) or "(none)"
        assert f"Accepted parameters: {expected}." in str(exc_info.value)
        assert "must-not-be-echoed" not in str(exc_info.value)


@pytest.mark.parametrize("routed", [False, True])
def test_keyword_help_preserves_tool_name_and_argument_aliases(monkeypatch, routed) -> None:
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: pytest.fail("must not fetch"))
    arguments = {"not_a_parameter": True}
    if routed:
        arguments = {"tool_name": "list_ad_sets", "arguments": json.dumps(arguments)}
    with pytest.raises(ToolError, match="Unknown parameters for list_adsets") as exc_info:
        asyncio.run(mcp_server.call_tool("call_tool" if routed else "list_ad_sets", arguments))
    tool = asyncio.run(mcp_server.get_tool("list_adsets"))
    assert f"Accepted parameters: {', '.join(tool.parameters['properties'])}." in str(exc_info.value)


@pytest.mark.parametrize("routed", [False, True])
def test_read_only_policy_rejects_names_before_keyword_help(monkeypatch, routed) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    for name in ("create_adset", "get_unknown_action"):
        arguments = {"not_a_parameter": True}
        if routed:
            arguments = {"tool_name": name, "arguments": json.dumps(arguments)}
        with pytest.raises(ToolError, match="META_READ_ONLY") as exc_info:
            asyncio.run(mcp_server.call_tool("call_tool" if routed else name, arguments))
        assert "Accepted parameters:" not in str(exc_info.value)


HISTORICAL_F10_ARGUMENTS = [
    ("get_creative_fatigue_report", {"account_id": "act_123"}, True),
    ("get_creative_fatigue_report", {"level": "account", "object_id": "act_123"}, True),
    ("get_creative_fatigue_report", {
        "level": "campaign", "object_id": "cmp_123", "window_days": 28,
        "date_preset": "last_30d", "since": "2026-07-01", "until": "2026-08-12",
        "min_spend": 1, "limit": 50, "include_low_confidence": True, "refresh": True,
        "adset_id": "x", "ad_id": "x", "breakdowns": ["publisher_platform"],
    }, False),
    ("get_creative_fatigue_report", {
        "level": "campaign", "object_id": "cmp_123", "previous_since": "2026-06-01",
        "previous_until": "2026-06-30", "current_since": "2026-07-13", "current_until": "2026-08-12",
        "min_impressions": 1000, "threshold": 0.1, "time_increment": 7, "format": "json",
    }, False),
    ("list_ads", {
        "account_id": "act_123", "effective_status": ["ACTIVE"], "name_contains": "Ada",
        "limit": 200, "fields": ["id", "name"],
    }, True),
    ("get_insights", {
        "level": "account", "object_id": "act_123", "since": "2025-04-17", "until": "2026-09-16",
        "breakdowns": ["publisher_platform", "platform_position"],
        "fields": ["spend", "impressions", "inline_link_clicks", "purchase_roas"],
        "filtering": [{"field": "ad.name", "operator": "CONTAIN", "value": "NAME_PREFIX"}],
        "action_attribution_windows": ["7d_click"],
    }, True),
    ("get_entity_insights", {
        "level": "ad", "object_id": "ad_123", "since": "2026-03-17", "until": "2026-07-28",
        "breakdowns": ["publisher_platform", "platform_position"],
        "fields": ["spend", "impressions", "clicks", "actions", "action_values"],
        "action_attribution_windows": ["7d_click"], "action_columns": ["purchase"],
        "bogus_param_to_list": True,
    }, False),
    ("list_ads", {
        "account_id": "act_123", "name_contains": "NAME_PREFIX", "limit": 100,
        "fields": ["id", "creative{asset_feed_spec{images}}"],
    }, True),
    ("get_insights", {
        "level": "adset", "object_id": "adset_123", "since": "2025-04-17", "until": "2025-06-30",
        "fields": ["ad_id", "ad_name", "spend", "impressions", "inline_link_clicks"],
        "breakdowns": ["publisher_platform", "platform_position"], "insights_level": "ad",
    }, False),
]


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize(("name", "arguments", "now_supported"), HISTORICAL_F10_ARGUMENTS)
def test_historical_f10_argument_sets_are_supported_or_list_accepted_keywords(
    monkeypatch, routed, name, arguments, now_supported,
) -> None:
    client_creations = []

    class RecordedClient:
        async def list_objects(self, parent_id, edge, *, fields, params):
            return {"data": []}

        async def get_insights(self, object_id, *, fields, params):
            return {"data": []}

    def get_client():
        client_creations.append(True)
        return RecordedClient()

    for module in (discovery, insights, diagnostics):
        monkeypatch.setattr(module, "get_graph_api_client", get_client)
    tool = "call_tool" if routed else name
    call_arguments = {"name": name, "arguments": arguments} if routed else arguments
    if now_supported:
        result = asyncio.run(mcp_server.call_tool(tool, call_arguments))
        assert result.structured_content is not None
        assert client_creations
    else:
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(mcp_server.call_tool(tool, call_arguments))
        schema = asyncio.run(mcp_server.get_tool(name)).parameters
        assert f"Accepted parameters: {', '.join(schema['properties'])}." in str(exc_info.value)
        for rejected in set(arguments) - set(schema["properties"]):
            assert rejected in str(exc_info.value)
        assert not client_creations


def test_compact_search_serializer_surfaces_required_archive_params() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    result = serialize_search_results_compact([components["tool:search_ads_archive@"]])
    assert "`search_ads_archive` | req: search_terms (string), ad_reached_countries (string or list[string])" in result
    assert "opt: ad_type (string), limit (integer), fields (string or list[string] or null)" in result


def test_compact_search_serializer_surfaces_required_targeting_category_params() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    result = serialize_search_results_compact([components["tool:get_targeting_categories@"]])
    assert "`get_targeting_categories` | req: category_class (string)" in result
    assert "opt: query (string or null), account_id (string or null), limit (integer)" in result


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


def test_placement_search_prefers_canonical_insights_but_exact_alias_still_works() -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {
        "query": "insights breakdown by placement platform_position for an ad over a date range",
    }))
    assert "`get_entity_insights`" in result.content[0].text
    assert "`get_insights`" not in result.content[0].text
    exact = asyncio.run(mcp_server.call_tool("search_tools", {"query": "get_insights"}))
    assert "- `get_insights`" in exact.content[0].text.splitlines()[1]
