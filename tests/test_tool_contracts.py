"""Registry contract and smoke tests for all MCP tools."""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

import pytest

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.tools import (
    audiences,
    auth_tools,
    campaigns,
    creatives,
    diagnostics,
    discovery,
    execution,
    insights,
    recommendations,
    targeting,
    utility,
)

REGISTERED_TOOLS = dict(sorted(getattr(mcp_server, "_tools", {}).items()))
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")

SAMPLE_TARGETING = {
    "geo_locations": {"countries": ["US"]},
    "age_min": 25,
    "age_max": 54,
    "genders": [1],
}

SAMPLE_INSIGHTS_ROW = {
    "campaign_id": "cmp_123",
    "campaign_name": "Campaign 123",
    "adset_id": "adset_123",
    "adset_name": "Ad Set 123",
    "ad_id": "ad_123",
    "ad_name": "Ad 123",
    "date_start": "2026-03-01",
    "date_stop": "2026-03-07",
    "spend": "100",
    "impressions": "1000",
    "reach": "800",
    "clicks": "50",
    "ctr": "5.0",
    "cpc": "2.0",
    "cpm": "100.0",
    "frequency": "1.25",
    "actions": [{"action_type": "purchase", "value": "4"}],
    "action_values": [{"action_type": "purchase", "value": "400"}],
}

TOOL_OVERRIDES: dict[str, dict[str, Any]] = {
    "create_campaign": {"daily_budget": 50.0},
    "create_ad_set": {"daily_budget": 25.0},
    "create_custom_audience": {"customer_file_source": "USER_PROVIDED_ONLY"},
    "create_lookalike_audience": {"country": "US", "ratio": 0.02},
    "list_campaigns": {"account_id": "123"},
    "list_adsets": {"account_id": "123"},
    "list_ads": {"account_id": "123"},
    "preview_ad": {"ad_id": "ad_123"},
    "upload_creative_asset": {"image_url": "https://example.com/image.png"},
    "update_campaign": {"name": "Updated Campaign"},
    "update_campaign_budget": {"daily_budget": 40.0},
    "update_adset_budget": {"daily_budget": 35.0},
    "update_custom_audience": {"name": "Updated Audience"},
    "update_creative": {"name": "Updated Creative"},
    "estimate_audience_size": {"account_id": "123"},
    "get_reach_frequency_predictions": {"account_id": "123"},
    "get_recommendations": {"account_id": "123"},
    "get_creative_performance_report": {"account_id": "123"},
    "get_creative_fatigue_report": {"campaign_id": "cmp_123"},
    "get_delivery_risk_report": {"campaign_id": "cmp_123"},
    "get_learning_phase_report": {"campaign_id": "cmp_123"},
    "generate_auth_url": {"scopes": ["ads_management"], "state": "state_123"},
    "refresh_to_long_lived_token": {"access_token": "token_123"},
    "get_token_info": {"input_token": "token_123"},
    "validate_token": {"input_token": "token_123"},
    "get_entity_insights": {"fields": ["spend", "impressions", "clicks"]},
    "get_performance_breakdown": {"fields": ["spend", "impressions"]},
    "compare_performance": {"metrics": ["roas", "cpa", "ctr"]},
    "export_insights": {"format": "json"},
    "create_async_insights_report": {"fields": ["spend", "impressions"]},
    "get_async_insights_report": {"fields": ["spend", "impressions"]},
}


class UniversalFakeClient:
    """Catch-all fake client used for smoke tests."""

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        if edge == "adaccounts":
            return {
                "data": [
                    {
                        "id": "act_123",
                        "name": "Test Account",
                        "account_status": 1,
                        "currency": "USD",
                        "timezone_name": "America/New_York",
                        "amount_spent": "2500",
                        "balance": "0",
                    }
                ]
            }
        if edge == "campaigns":
            return {
                "data": [
                    {
                        "id": "cmp_123",
                        "name": "Campaign 123",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                        "objective": "OUTCOME_SALES",
                        "daily_budget": "5000",
                        "currency": "USD",
                    }
                ]
            }
        if edge == "adsets":
            return {
                "data": [
                    {
                        "id": "adset_123",
                        "name": "Ad Set 123",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                        "campaign_id": "cmp_123",
                        "daily_budget": "3000",
                        "billing_event": "IMPRESSIONS",
                        "optimization_goal": "OFFSITE_CONVERSIONS",
                        "targeting": SAMPLE_TARGETING,
                    }
                ]
            }
        if edge == "ads":
            return {
                "data": [
                    {
                        "id": "ad_123",
                        "name": "Ad 123",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                        "campaign_id": "cmp_123",
                        "adset_id": "adset_123",
                        "creative": {"id": "crt_123"},
                    }
                ]
            }
        if edge == "customaudiences":
            return {
                "data": [
                    {
                        "id": "aud_123",
                        "name": "Audience 123",
                        "subtype": "CUSTOM",
                        "description": "Test audience",
                        "retention_days": 30,
                    }
                ]
            }
        if edge == "adcreatives":
            return {
                "data": [
                    {
                        "id": "crt_123",
                        "name": "Creative 123",
                        "title": "Creative title",
                        "body": "Creative body",
                        "status": "ACTIVE",
                    }
                ]
            }
        if edge == "reachfrequencypredictions":
            return {"data": [{"id": "rf_123", "status": 1, "daily_impression_curve": []}]}
        if edge == "recommendations":
            return {"data": [{"id": "rec_123", "message": "Increase budget"}]}
        if edge == "previews":
            return {"data": [{"body": "<html>preview</html>"}]}
        if edge == "insights":
            return {"data": [SAMPLE_INSIGHTS_ROW]}
        return {"data": [{"id": f"{edge}_123", "name": f"{edge} item"}]}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        payload = {
            "id": object_id,
            "name": f"Object {object_id}",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "objective": "OUTCOME_SALES",
            "daily_budget": "5000",
            "lifetime_budget": "25000",
            "currency": "USD",
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "start_time": "2026-03-01T00:00:00+0000",
            "end_time": "2026-03-07T00:00:00+0000",
            "title": "Old title",
            "body": "Old body",
            "customer_file_source": "USER_PROVIDED_ONLY",
            "retention_days": 30,
            "description": "Old description",
            "creative": {"id": "crt_123", "name": "Creative 123"},
            "targeting": SAMPLE_TARGETING,
            "account_status": 1,
            "app_id": "app_123",
            "type": "USER",
            "is_valid": True,
            "scopes": ["ads_read", "ads_management"],
        }
        if fields:
            for field in fields:
                payload.setdefault(field, f"{field}_value")
        return payload

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        return {"id": f"{edge}_created", "parent_id": parent_id, "payload": data, "files": files}

    async def update_object(self, object_id: str, *, data):
        return {"success": True, "id": object_id, "payload": data}

    async def delete_object(self, object_id: str):
        return {"success": True, "id": object_id}

    async def get_insights(self, object_id: str, *, fields, params):
        return {"data": [SAMPLE_INSIGHTS_ROW], "paging": {"cursors": {"after": "after_1"}}}

    async def create_async_insights_report(self, object_id: str, *, fields, params):
        return {"report_run_id": "rpt_123", "async_status": "Job Running", "id": "rpt_123"}

    async def get_async_report(self, report_run_id: str, *, fields=None, limit=100, after=None):
        return {
            "status": {
                "id": report_run_id,
                "async_status": "Job Completed",
                "async_percent_completion": 100,
            },
            "rows": {"data": [SAMPLE_INSIGHTS_ROW], "paging": {"cursors": {"after": "after_1"}}},
        }

    async def search_interests(self, *, query: str, limit: int = 25):
        return {"data": [{"id": "int_123", "name": query}]}

    async def search_geo_locations(self, *, query: str, location_types=None, limit: int = 25):
        return {"data": [{"key": "geo_123", "name": query, "type": "country"}]}

    async def estimate_audience_size(self, account_id: str, *, targeting_spec, optimization_goal=None):
        return {"data": [{"users": 12345, "estimate_mau": 12000, "estimate_dau": 4000}]}

    async def get_reach_frequency_predictions(self, account_id: str, *, limit: int = 25):
        return {"data": [{"id": "rf_123", "status": 1}]}

    async def get_recommendations(self, account_id: str, *, campaign_id=None):
        return {"data": [{"id": "rec_123", "message": "Increase budget"}]}

    async def oauth_access_token(self, params):
        return {"access_token": "token_123", "token_type": "bearer", "expires_in": 3600, "params": params}

    async def debug_token(self, *, input_token: str, debug_access_token: str | None = None):
        return {
            "data": {
                "is_valid": True,
                "app_id": "app_123",
                "type": "USER",
                "scopes": ["ads_management"],
                "input_token": input_token,
                "debug_access_token": debug_access_token,
            }
        }

    async def generate_system_user_token(self, system_user_id: str, *, business_app: str, scope, access_token: str | None = None):
        return {
            "access_token": "system_token",
            "token_type": "bearer",
            "system_user_id": system_user_id,
            "business_app": business_app,
            "scope": scope,
            "access_token_used": access_token,
        }

    async def preview_ad(self, **kwargs):
        return {"data": [{"body": "<html>preview</html>"}], "request": kwargs}

    async def upload_ad_image(self, account_id: str, **kwargs):
        return {"account_id": account_id, "images": {"image.png": {"hash": "hash_123"}}, "request": kwargs}


def _patch_clients(monkeypatch: pytest.MonkeyPatch) -> UniversalFakeClient:
    """Patch all client factories to return a single fake client."""
    client = UniversalFakeClient()
    for module in [
        audiences,
        auth_tools,
        campaigns,
        creatives,
        diagnostics,
        discovery,
        execution,
        insights,
        recommendations,
        targeting,
        utility,
    ]:
        monkeypatch.setattr(module, "get_graph_api_client", lambda *args, _client=client, **kwargs: _client)

    async def fake_diag_get_entity_insights(**kwargs):
        return {
            "items": [
                {
                    "campaign_id": "cmp_123",
                    "adset_id": "adset_123",
                    "ad_id": "ad_123",
                    "spend": 100.0,
                    "roas": 4.0,
                    "reach": 800,
                    "metrics": {
                        "spend": 100.0,
                        "impressions": 1000,
                        "clicks": 50,
                        "frequency": 1.25,
                        "ctr": 0.05,
                        "cpc": 2.0,
                        "cpm": 100.0,
                        "conversions": 4.0,
                        "conversion_value": 400.0,
                        "cpa": 25.0,
                        "roas": 4.0,
                    },
                }
            ],
            "summary": {
                "count": 1,
                "metrics": {
                    "spend": 100.0,
                    "impressions": 1000,
                    "clicks": 50,
                    "frequency": 1.25,
                    "ctr": 0.05,
                    "cpc": 2.0,
                    "cpm": 100.0,
                    "conversions": 4.0,
                    "conversion_value": 400.0,
                    "cpa": 25.0,
                    "roas": 4.0,
                },
            },
        }

    async def fake_diag_child_insights(object_id: str, *, level: str, **kwargs):
        return [
            {
                "id": f"{level}_1",
                f"{level}_id": f"{level}_1",
                "campaign_id": "cmp_123",
                "adset_id": "adset_123",
                "ad_id": "ad_123",
                "spend": 100.0,
                "roas": 4.0,
                "metrics": {
                    "spend": 100.0,
                    "impressions": 1000,
                    "clicks": 50,
                    "frequency": 1.25,
                    "ctr": 0.05,
                    "cpc": 2.0,
                    "cpm": 100.0,
                    "conversions": 4.0,
                    "conversion_value": 400.0,
                    "cpa": 25.0,
                    "roas": 4.0,
                },
            },
            {
                "id": f"{level}_2",
                f"{level}_id": f"{level}_2",
                "campaign_id": "cmp_456",
                "adset_id": "adset_456",
                "ad_id": "ad_456",
                "spend": 60.0,
                "roas": 1.5,
                "metrics": {
                    "spend": 60.0,
                    "impressions": 500,
                    "clicks": 20,
                    "frequency": 1.1,
                    "ctr": 0.04,
                    "cpc": 3.0,
                    "cpm": 120.0,
                    "conversions": 1.0,
                    "conversion_value": 90.0,
                    "cpa": 60.0,
                    "roas": 1.5,
                },
            },
        ]

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_diag_get_entity_insights)
    monkeypatch.setattr(diagnostics, "_child_insights", fake_diag_child_insights)
    monkeypatch.setenv("META_APP_ID", "app_123")
    monkeypatch.setenv("META_APP_SECRET", "secret_123")
    monkeypatch.setenv("META_REDIRECT_URI", "https://example.com/callback")
    reload_settings()
    return client


def _required_value(param_name: str) -> Any:
    """Return a sample value for a required parameter."""
    mapping = {
        "account_id": "123",
        "campaign_id": "cmp_123",
        "adset_id": "adset_123",
        "ad_id": "ad_123",
        "creative_id": "crt_123",
        "audience_id": "aud_123",
        "origin_audience_id": "aud_origin_123",
        "system_user_id": "sys_123",
        "report_run_id": "rpt_123",
        "owner_id": "act_123",
        "object_id": "cmp_123",
        "object_ids": ["cmp_123", "cmp_456"],
        "level": "campaign",
        "name": "Test Name",
        "objective": "OUTCOME_SALES",
        "billing_event": "IMPRESSIONS",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "targeting": SAMPLE_TARGETING,
        "targeting_spec": SAMPLE_TARGETING,
        "query": "running",
        "code": "oauth_code",
        "scope": ["ads_management"],
        "status": "PAUSED",
        "breakdown": "country",
        "current_since": "2026-03-01",
        "current_until": "2026-03-07",
        "previous_since": "2026-02-22",
        "previous_until": "2026-02-28",
    }
    if param_name not in mapping:
        raise AssertionError(f"No sample value configured for required parameter: {param_name}")
    return mapping[param_name]


def _tool_kwargs(name: str, fn) -> dict[str, Any]:
    """Build smoke-test kwargs for one tool."""
    kwargs = dict(TOOL_OVERRIDES.get(name, {}))
    for param in inspect.signature(fn).parameters.values():
        if param.name in kwargs:
            continue
        if param.default is not inspect._empty:
            continue
        kwargs[param.name] = _required_value(param.name)
    return kwargs


def test_registered_tools_have_claude_friendly_contracts() -> None:
    """Every tool should present a clear callable contract."""
    assert REGISTERED_TOOLS, "No tools were registered."
    problems: list[str] = []
    for name, fn in REGISTERED_TOOLS.items():
        if not SNAKE_CASE.match(name):
            problems.append(f"{name}: tool name must be snake_case")
        if not inspect.getdoc(fn):
            problems.append(f"{name}: missing docstring")
        signature = inspect.signature(fn)
        if signature.return_annotation is inspect._empty:
            problems.append(f"{name}: missing return annotation")
        for param in signature.parameters.values():
            if param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
                problems.append(f"{name}: uses unsupported variadic parameter {param.name}")
            if param.annotation is inspect._empty:
                problems.append(f"{name}: parameter {param.name} lacks an annotation")
            if not SNAKE_CASE.match(param.name):
                problems.append(f"{name}: parameter {param.name} must be snake_case")
    assert not problems, "\n".join(problems)


def test_capabilities_manifest_matches_registered_tools() -> None:
    """The capability tool should stay in sync with the actual registry."""
    declared = {tool for tools in utility.TOOL_GROUPS.values() for tool in tools}
    assert declared == set(REGISTERED_TOOLS)


@pytest.mark.parametrize("tool_name", sorted(REGISTERED_TOOLS))
def test_every_tool_smoke_runs(monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    """Every registered tool should be directly callable with Claude-friendly args."""
    _patch_clients(monkeypatch)
    fn = REGISTERED_TOOLS[tool_name]
    result = asyncio.run(fn(**_tool_kwargs(tool_name, fn)))
    assert isinstance(result, dict)
    assert result


@pytest.mark.parametrize(
    ("fn", "kwargs"),
    [
        (
            campaigns.create_campaign,
            {
                "account_id": "123",
                "name": "Bad Campaign",
                "objective": "OUTCOME_SALES",
                "daily_budget": 10.0,
                "lifetime_budget": 20.0,
            },
        ),
        (
            campaigns.create_ad_set,
            {
                "account_id": "123",
                "campaign_id": "cmp_123",
                "name": "Bad Ad Set",
                "billing_event": "IMPRESSIONS",
                "optimization_goal": "OFFSITE_CONVERSIONS",
                "targeting": SAMPLE_TARGETING,
                "daily_budget": 10.0,
                "lifetime_budget": 20.0,
            },
        ),
        (creatives.preview_ad, {}),
        (creatives.upload_creative_asset, {"account_id": "123"}),
        (diagnostics.get_creative_performance_report, {}),
        (diagnostics.get_creative_fatigue_report, {}),
        (diagnostics.get_delivery_risk_report, {}),
        (diagnostics.get_learning_phase_report, {}),
        (insights.export_insights, {"level": "campaign", "object_id": "cmp_123", "format": "xml"}),
        (
            insights.get_entity_insights,
            {
                "level": "campaign",
                "object_id": "cmp_123",
                "date_preset": "last_7d",
                "since": "2026-03-01",
                "until": "2026-03-07",
            },
        ),
        (
            audiences.create_lookalike_audience,
            {
                "account_id": "123",
                "name": "Bad LAL",
                "origin_audience_id": "aud_123",
            },
        ),
        (execution.update_campaign_budget, {"campaign_id": "cmp_123"}),
        (execution.set_campaign_status, {"campaign_id": "cmp_123", "status": "DELETED"}),
    ],
)
def test_edge_case_validations_raise(monkeypatch: pytest.MonkeyPatch, fn, kwargs: dict[str, Any]) -> None:
    """Risky or ambiguous tool inputs should fail fast with a validation error."""
    _patch_clients(monkeypatch)
    with pytest.raises(ValidationError):
        asyncio.run(fn(**kwargs))
