"""Operational utility tools for Claude-friendly MCP use."""

from __future__ import annotations

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import ALWAYS_VISIBLE_TOOLS
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client

TOOL_GROUPS = {
    "discovery": [
        "list_ad_accounts",
        "get_ad_account",
        "get_account_pages",
        "list_instagram_accounts",
        "list_campaigns",
        "get_campaign",
        "list_adsets",
        "get_adset",
        "list_ads",
        "get_ad",
        "list_audiences",
        "list_creatives",
    ],
    "analysis": [
        "get_entity_insights",
        "get_performance_breakdown",
        "compare_time_ranges",
        "compare_performance",
        "export_insights",
        "create_async_insights_report",
        "get_async_insights_report",
    ],
    "optimization": [
        "get_account_optimization_snapshot",
        "get_campaign_optimization_snapshot",
        "get_budget_pacing_report",
        "get_creative_performance_report",
        "get_creative_fatigue_report",
        "get_audience_performance_report",
        "get_delivery_risk_report",
        "get_learning_phase_report",
        "get_recommendations",
        "get_budget_opportunities",
        "get_creative_opportunities",
        "get_audience_opportunities",
        "get_delivery_opportunities",
        "get_bidding_opportunities",
    ],
    "planning": [
        "search_interests",
        "get_interest_suggestions",
        "validate_interests",
        "search_geo_locations",
        "get_targeting_categories",
        "search_behaviors",
        "search_demographics",
        "estimate_audience_size",
        "get_reach_frequency_predictions",
    ],
    "writes": [
        "create_campaign",
        "update_campaign",
        "delete_campaign",
        "create_ad_set",
        "set_campaign_status",
        "set_adset_status",
        "set_ad_status",
        "update_campaign_budget",
        "update_adset_budget",
        "update_adset_bid_amount",
        "update_campaign_bid_strategy",
        "update_adset_bid_strategy",
        "create_custom_audience",
        "create_lookalike_audience",
        "update_custom_audience",
        "delete_audience",
        "create_ad_creative",
        "create_ad",
        "update_creative",
        "delete_creative",
    ],
    "creative_ops": [
        "get_ad_image",
        "preview_ad",
        "upload_creative_asset",
        "setup_ab_test",
    ],
    "research": [
        "search_ads_archive",
    ],
    "auth": [
        "generate_auth_url",
        "exchange_code_for_token",
        "refresh_to_long_lived_token",
        "generate_system_user_token",
        "get_token_info",
        "validate_token",
    ],
    "docs": [
        "get_meta_object_model",
        "get_metrics_reference",
        "get_v25_notes",
        "get_optimization_playbook",
    ],
    "utility": [
        "health_check",
        "get_capabilities",
    ],
}

RESOURCE_URIS = [
    "meta://docs/object-model",
    "meta://docs/insights-metrics",
    "meta://docs/v25-notes",
    "meta://docs/optimization-playbook",
    "meta://docs/tool-routing",
]

INTENT_GUIDE = {
    "check_connectivity_or_auth": {
        "description": "Verify token and Meta connectivity before using account-specific tools.",
        "recommended_order": ["health_check"],
        "avoid_unless_needed": ["export_insights", "create_async_insights_report"],
        "notes": ["Use this first when the failure mode is unclear."],
    },
    "discover_accounts_or_ids": {
        "description": "Find accessible accounts, ids, Pages, Instagram identities, or entity lists.",
        "recommended_order": [
            "list_ad_accounts",
            "list_campaigns",
            "list_adsets",
            "list_ads",
            "get_account_pages",
            "list_instagram_accounts",
        ],
        "avoid_unless_needed": ["export_insights", "create_async_insights_report"],
        "notes": ["Prefer list tools before single-id get tools when the user has not supplied ids."],
    },
    "inspect_single_entity_performance": {
        "description": "Inspect performance for one account, campaign, ad set, or ad.",
        "recommended_order": ["get_entity_insights", "get_performance_breakdown"],
        "avoid_unless_needed": ["export_insights"],
        "notes": ["Use export_insights only when the user explicitly wants raw rows or CSV output."],
    },
    "compare_multiple_entities": {
        "description": "Rank multiple campaigns, ad sets, or ads across the same window.",
        "recommended_order": ["compare_performance"],
        "avoid_unless_needed": ["get_entity_insights"],
        "notes": ["Prefer this over manual repeated insights calls when comparing several ids."],
    },
    "compare_current_vs_previous_window": {
        "description": "Compare one entity across two time windows.",
        "recommended_order": ["compare_time_ranges"],
        "avoid_unless_needed": ["export_insights"],
        "notes": ["Use this before asking for diagnostics when the question is purely time-window comparison."],
    },
    "export_reporting_data": {
        "description": "Return raw or file-like reporting output instead of summary analysis.",
        "recommended_order": ["export_insights", "create_async_insights_report", "get_async_insights_report"],
        "avoid_unless_needed": [],
        "notes": ["Prefer summary tools first; exports are the heaviest reporting path."],
    },
    "find_optimization_opportunities": {
        "description": "Find optimization opportunities, risks, pacing issues, or fatigue.",
        "recommended_order": [
            "get_account_optimization_snapshot",
            "get_campaign_optimization_snapshot",
            "get_recommendations",
            "get_budget_pacing_report",
            "get_creative_fatigue_report",
            "get_delivery_risk_report",
        ],
        "avoid_unless_needed": [
            "get_budget_opportunities",
            "get_creative_opportunities",
            "get_audience_opportunities",
            "get_delivery_opportunities",
            "get_bidding_opportunities",
        ],
        "notes": [
            "Call get_recommendations once for a broad Meta-native opportunity scan.",
            "Use typed opportunity tools only when the user asks for one category specifically.",
        ],
    },
    "plan_targeting_or_audiences": {
        "description": "Research interests, geos, broad targeting categories, behaviors, demographics, and reach estimates.",
        "recommended_order": [
            "search_interests",
            "get_interest_suggestions",
            "validate_interests",
            "search_geo_locations",
            "get_targeting_categories",
            "search_behaviors",
            "search_demographics",
            "estimate_audience_size",
        ],
        "avoid_unless_needed": ["get_reach_frequency_predictions"],
        "notes": ["Reach-frequency predictions are more entitlement-sensitive than the basic planning tools."],
    },
    "creative_workflows": {
        "description": "Discover Pages or Instagram identities, inspect creatives, preview ads, and create ad assets.",
        "recommended_order": [
            "get_account_pages",
            "list_instagram_accounts",
            "list_creatives",
            "get_ad_image",
            "preview_ad",
            "upload_creative_asset",
            "create_ad_creative",
            "create_ad",
        ],
        "avoid_unless_needed": [],
        "notes": ["Use get_account_pages and list_instagram_accounts before creative creation when an identity is required."],
    },
    "research_competitor_ads": {
        "description": "Search public Ads Library data for competitor or market research.",
        "recommended_order": ["search_ads_archive"],
        "avoid_unless_needed": [],
        "notes": ["This still requires Ads Library API app access even though it does not depend on an ad account id."],
    },
    "writes_after_confirmation": {
        "description": "Mutate campaign, ad set, ad, creative, or audience state after explicit user confirmation.",
        "recommended_order": [
            "set_campaign_status",
            "set_adset_status",
            "set_ad_status",
            "update_campaign_budget",
            "update_adset_budget",
            "update_adset_bid_amount",
            "update_campaign_bid_strategy",
            "update_adset_bid_strategy",
            "create_campaign",
            "update_campaign",
            "create_ad_set",
            "create_ad",
        ],
        "avoid_unless_needed": [],
        "notes": ["Write operations require ads_management-level permissions and should follow discovery or diagnostics reads."],
    },
}

ROUTING_HINTS = {
    intent: list(route["recommended_order"]) + list(route["avoid_unless_needed"])
    for intent, route in INTENT_GUIDE.items()
}


def tool_routing_markdown() -> str:
    """Return a compact markdown routing guide for Claude."""
    lines = [
        "# Tool Routing Guide",
        "",
        "FastMCP 3.1 tool search is enabled. If the exact tool is not visible, use `search_tools` and then `call_tool`.",
        "",
        "Use `get_capabilities(intent=...)` for a compact routing response.",
        "",
    ]
    for intent, route in INTENT_GUIDE.items():
        lines.append(f"## {intent}")
        lines.append(route["description"])
        lines.append("")
        lines.append("Primary tools:")
        lines.extend(f"- `{tool}`" for tool in route["recommended_order"])
        if route["avoid_unless_needed"]:
            lines.append("")
            lines.append("Use only when needed:")
            lines.extend(f"- `{tool}`" for tool in route["avoid_unless_needed"])
        if route["notes"]:
            lines.append("")
            lines.append("Notes:")
            lines.extend(f"- {note}" for note in route["notes"])
        lines.append("")
    return "\n".join(lines).strip()


def _server_metadata() -> dict[str, object]:
    """Build common server metadata for capability responses."""
    settings = get_settings()
    return {
        "name": "Meta Ads FastMCP",
        "fastmcp_version_target": "3.1.0",
        "api_version": settings.api_version,
        "optimization_first": True,
        "primary_transport": "stdio",
        "secondary_transport": "streamable-http",
        "tool_search_enabled": True,
        "always_visible_tools": ALWAYS_VISIBLE_TOOLS,
        "dynamic_search_tools": ["search_tools", "call_tool"],
    }


@mcp_server.tool()
async def health_check() -> dict[str, object]:
    """Use this first when auth or connectivity is uncertain before trying account-specific tools."""
    settings = get_settings()
    checks = {
        "access_token_present": bool(settings.access_token),
        "api_version": settings.api_version,
        "default_account_id": settings.default_account_id,
        "app_credentials_present": bool(settings.app_id and settings.app_secret),
    }
    if not settings.access_token:
        return {
            "status": "unhealthy",
            "checks": checks,
            "meta_api_connection": "not_attempted",
            "message": "META_ACCESS_TOKEN is not configured.",
        }

    client = get_graph_api_client()
    try:
        payload = await client.list_objects(
            "me",
            "adaccounts",
            fields=["id", "name", "account_status"],
            params={"limit": 3},
        )
        accounts = payload.get("data", [])
        return {
            "status": "healthy",
            "checks": checks,
            "meta_api_connection": "connected",
            "accessible_account_count_sample": len(accounts),
            "accessible_accounts_sample": accounts,
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "checks": checks,
            "meta_api_connection": "failed",
            "message": str(exc),
        }


@mcp_server.tool()
async def get_capabilities(intent: str | None = None) -> dict[str, object]:
    """Use this when Claude is unsure which tool to use. Pass intent for a compact routing response instead of the full manifest."""
    if intent is not None:
        route = INTENT_GUIDE.get(intent)
        if route is None:
            raise ValidationError(
                f"Unknown intent '{intent}'. Valid intents: {', '.join(sorted(INTENT_GUIDE))}."
            )
        return {
            "server": _server_metadata(),
            "selected_intent": {
                "intent": intent,
                **route,
            },
            "resources": RESOURCE_URIS,
            "valid_intents": sorted(INTENT_GUIDE),
            "notes": [
                "Use the recommended_order list first and only fall back to avoid_unless_needed when the primary tools cannot answer the question.",
                "For opportunity scans, prefer get_recommendations once before typed opportunity tools.",
                "When the exact tool is not visible, use search_tools and then call_tool because FastMCP 3.1 tool search is enabled.",
            ],
        }

    settings = get_settings()
    return {
        "server": _server_metadata(),
        "auth": {
            "required": ["META_ACCESS_TOKEN"],
            "optional": [
                "META_DEFAULT_ACCOUNT_ID",
                "META_APP_ID",
                "META_APP_SECRET",
                "META_REDIRECT_URI",
            ],
        },
        "tool_groups": TOOL_GROUPS,
        "routing_hints": ROUTING_HINTS,
        "intent_guide": INTENT_GUIDE,
        "resources": RESOURCE_URIS,
        "notes": [
            "Use discovery and diagnostics before write operations.",
            "FastMCP 3.1 tool search is enabled, so the server may expose search_tools and call_tool instead of the entire tool catalog up front.",
            "compare_performance reuses the insights surface and avoids extra lookups when names are already present in insights rows.",
            "export_insights is a convenience wrapper over the core insights surface.",
            "Pass intent to get_capabilities for a compact routing response instead of the full manifest.",
            "Use get_account_pages before creative creation when a Page or Instagram-linked asset is needed.",
            "Use list_instagram_accounts when creative setup requires an Instagram identity rather than a Facebook Page.",
            "Use get_recommendations once for a broad Meta-native opportunity scan, and use typed opportunity tools only for category-specific follow-up.",
            "search_ads_archive is public research data and does not depend on an ad account id, but the app still needs Ads Library API access.",
            "Write operations still depend on the token having ads_management-level permissions.",
        ],
    }
