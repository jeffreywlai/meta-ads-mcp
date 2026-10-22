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


@pytest.mark.parametrize(("query", "expected"), [
    ("update ad set budget bid amount status campaign budget bid strategy", [
        "update_adset_budget", "update_adset_bid_amount", "set_adset_status",
        "update_campaign_budget", "update_adset_bid_strategy", "update_campaign_bid_strategy",
    ]),
    ("update ad set budget bid amount status campaign budget bid strategy and name", [
        "update_adset_budget", "update_adset_bid_amount", "set_adset_status",
        "update_campaign_budget", "update_adset_bid_strategy", "update_campaign_bid_strategy",
        "update_campaign",
    ]),
    ("bid amount cost cap; pause ad set status; campaign daily budget; bid strategy roas floor", [
        "update_adset_bid_amount", "set_adset_status", "update_campaign_budget", "update_adset_bid_strategy",
    ]),
    ("update campaign daily budget CBO", ["update_campaign_budget"]),
    ("list ads filter by name, get ad creative details, update ad creative, upload image", [
        "list_ads", "get_creative", "update_ad_creative", "upload_creative_asset",
    ]),
])
def test_usage_report_compound_queries_return_the_operation_union(query, expected) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == expected


@pytest.mark.parametrize("max_results", [1, 6, 12])
def test_explicit_search_preserves_every_unique_operation_in_order(monkeypatch, max_results) -> None:
    monkeypatch.setattr(mcp_server.transforms[0], "_max_results", max_results)
    expected = [
        "get_ad", "update_campaign", "list_ads", "update_adset_budget",
        "get_creative", "set_adset_status", "upload_creative_asset",
        "get_entity_insights", "update_ad_creative",
    ]
    query = "; ".join([expected[0], *expected, expected[2]])

    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == expected


@pytest.mark.parametrize("max_results", [1, 3, 6])
def test_fuzzy_search_keeps_its_configured_cap_and_read_safe_candidates(monkeypatch, max_results) -> None:
    from meta_ads_mcp.tool_contracts import build_tool_contracts

    monkeypatch.setattr(mcp_server.transforms[0], "_max_results", max_results)
    result = asyncio.run(mcp_server.call_tool("search_tools", {
        "query": "performance insights campaign delivery",
    }))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    contracts = build_tool_contracts(mcp_server.local_provider._components.values())
    assert len(names) == max_results
    assert len(set(names)) == len(names)
    assert all(contracts[name].effect == "read" for name in names)


@pytest.mark.parametrize(("call", "include_campaign"), [
    ('update_campaign(campaign_id="123", name="do not update campaign budget; update_adset_budget")', True),
    ('update_campaign(campaign_id="123", unknown_argument="update_adset_budget")', False),
])
def test_many_explicit_operations_keep_literal_validation_and_negation(call, include_campaign) -> None:
    expected = [
        "get_ad", "list_ads", "get_creative", "set_adset_status",
        "upload_creative_asset", "get_entity_insights", "update_ad_creative",
    ]
    query = "; ".join([call, *expected, "do not update_campaign_budget"])
    if include_campaign:
        expected.insert(0, "update_campaign")

    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == expected


def test_read_only_search_preserves_many_explicit_reads_and_filters_writes(monkeypatch) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    expected = [
        "list_campaigns", "get_campaign", "list_adsets", "get_adset",
        "list_ads", "get_ad", "get_creative", "list_creatives",
    ]
    query = "; ".join([
        "update_campaign", *expected[:4], "set_adset_status",
        *expected[4:], "update_ad_creative",
    ])

    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == expected


@pytest.mark.parametrize("query", [
    "update ad set budget without changing campaign budget and ad set status",
    "update ad set budget without changing status, campaign budget, or targeting",
    "update ad set budget to match campaign budget",
    "update ad set budget but only show campaign budget",
    "update ad set budget to match campaign budget and ad set status",
    "update ad set budget but only show campaign budget and campaign status",
])
def test_suffix_negation_keeps_later_facets_out_of_live_search(query) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == ["update_adset_budget"]


@pytest.mark.parametrize("query", [
    "update campaign budget based on ad set status",
    "update campaign budget based on ad set status and ad set bid strategy",
])
def test_configuration_reference_does_not_select_referenced_status_write(query) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {
        "query": query,
    }))
    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == ["update_campaign_budget"]


@pytest.mark.parametrize("separator", [" and ", ", ", " then "])
def test_configuration_list_continuations_preserve_campaign_scope(separator) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {
        "query": f"update campaign budget{separator}bid strategy",
    }))
    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == ["update_campaign_budget", "update_campaign_bid_strategy"]


@pytest.mark.parametrize(("query", "expected"), [
    ("update campaign budget; bid amount must not change", ["update_campaign_budget"]),
    ("update campaign budget; bid amount should not change", ["update_campaign_budget"]),
    ("update adset budget and bid amount must not change", ["update_adset_budget"]),
    ("update campaign budget and name", ["update_campaign_budget", "update_campaign"]),
    ("update campaign status and objective", ["set_campaign_status", "update_campaign"]),
    ("update campaign name and budget", ["update_campaign", "update_campaign_budget"]),
    ("update campaign budget and name should not change", ["update_campaign_budget"]),
    ("update adset budget and campaign name", ["update_adset_budget", "update_campaign"]),
    ("update campaign budget; bid amount must not change; show campaign name", [
        "update_campaign_budget", "get_campaign",
    ]),
    ("update campaign budget; bid amount shouldn't change; update campaign name", [
        "update_campaign_budget", "update_campaign",
    ]),
    ("update campaign budget; bid amount does not change", ["update_campaign_budget"]),
    ("update campaign budget; bid amount did not change", ["update_campaign_budget"]),
    ("update campaign budget; bid amount should remain unchanged", ["update_campaign_budget"]),
    ("update campaign budget; bid amount is unchanged", ["update_campaign_budget"]),
    ("update campaign budget; bid amount must never change", ["update_campaign_budget"]),
    ("update campaign budget; bid amount and status must not change", ["update_campaign_budget"]),
    ("update campaign budget; campaign name and objective must not change", ["update_campaign_budget"]),
    ("update campaign budget; campaign status and objective should not change", ["update_campaign_budget"]),
    ("update campaign budget; campaign name and objective should stay unchanged", ["update_campaign_budget"]),
    ("update campaign budget; bid amount unchanged", ["update_campaign_budget"]),
    ("update campaign budget; campaign name and objective unchanged", ["update_campaign_budget"]),
    ("update campaign budget; campaign name needs to stay unchanged", ["update_campaign_budget"]),
    ("update campaign budget; bid amount, must not change", ["update_campaign_budget"]),
    ("update campaign budget; bid amount: must not change", ["update_campaign_budget"]),
    ("update campaign budget; bid amount should remain unchanged and campaign status", ["update_campaign_budget"]),
    ("update campaign budget to 100 without changing name and objective", ["update_campaign_budget"]),
    ("update campaign budget to 100 and name", ["update_campaign_budget", "update_campaign"]),
    ("update campaign budget to 100, name", ["update_campaign_budget", "update_campaign"]),
    ("update campaign budget to 100, and name", ["update_campaign_budget", "update_campaign"]),
    ("update campaign status to paused and objective", ["set_campaign_status", "update_campaign"]),
    ("update campaign status to paused, objective", ["set_campaign_status", "update_campaign"]),
    ('update campaign name to "Winter", budget', ["update_campaign", "update_campaign_budget"]),
    ('update campaign name to "Budget and Status" and objective', ["update_campaign"]),
])
def test_configuration_search_preserves_requested_fields_and_postfix_negation(query, expected) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == expected


@pytest.mark.parametrize("query", [
    "update campaign budget must not change",
    "set adset status should not change",
    "do not update campaign budget and name",
    "compare campaign budget and name",
    "show campaign status and objective",
])
def test_configuration_search_does_not_expose_negated_or_read_only_writes(query) -> None:
    from meta_ads_mcp.tool_contracts import build_tool_contracts

    contracts = build_tool_contracts(mcp_server.local_provider._components.values())
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert all(contracts[name].effect == "read" for name in names)


@pytest.mark.parametrize("query", [
    'update_campaign(campaign_id="123", name="budget; bid amount must not change, and objective")',
    "update_campaign(campaign_id='123', name='budget; bid amount must not change, and objective')",
])
def test_configuration_search_treats_negation_and_fields_in_exact_call_values_as_literals(query) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {
        "query": query,
    }))

    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == ["update_campaign"]


def test_explicit_read_clause_remains_a_read_in_a_configuration_workflow() -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {
        "query": "update ad set budget and show campaign budget",
    }))
    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == ["update_adset_budget", "get_campaign"]


def test_semicolon_starts_a_new_request_after_an_excluded_reference() -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {
        "query": "update ad set budget to match campaign budget and ad set status; show campaign budget",
    }))
    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == ["update_adset_budget", "get_campaign"]


@pytest.mark.parametrize(("query", "expected"), [
    ("update campaign status to paused and campaign budget", ["set_campaign_status", "update_campaign_budget"]),
    ("pause ad set status, campaign budget", ["set_adset_status", "update_campaign_budget"]),
])
def test_explicit_entity_continuations_survive_a_valued_status_command(query, expected) -> None:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))
    names = [line.split("`", 2)[1] for line in result.content[0].text.splitlines() if line.startswith("- `")]
    assert names == expected


@pytest.mark.parametrize(("name", "fields"), [
    ("update_campaign", ["daily_budget", "lifetime_budget"]),
    ("create_campaign", ["daily_budget", "lifetime_budget"]),
    ("create_ad_set", ["daily_budget", "lifetime_budget", "bid_amount"]),
    ("create_ad", ["bid_amount"]),
    ("update_adset_budget", ["daily_budget", "lifetime_budget"]),
    ("update_campaign_budget", ["daily_budget", "lifetime_budget"]),
    ("update_adset_bid_amount", ["bid_amount"]),
    ("update_adset_bid_strategy", ["bid_amount"]),
])
def test_budget_and_bid_machine_schemas_state_account_currency_units(name, fields) -> None:
    tool = asyncio.run(mcp_server.get_tool(name))

    for field in fields:
        description = json.dumps(tool.parameters["properties"][field])
        assert "major units" in description
        assert "ad account currency" in description
        assert "12.50" in description


@pytest.mark.parametrize("name", ["create_ad_set", "update_adset_bid_strategy"])
def test_roas_machine_schemas_state_the_raw_scale(name) -> None:
    tool = asyncio.run(mcp_server.get_tool(name))

    description = json.dumps(tool.parameters["properties"]["bid_constraints"])
    assert "roas_average_floor" in description
    assert "10000" in description
    assert "30000 means 3.0 ROAS" in description
