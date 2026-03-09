"""Operational utility tools for Claude-friendly MCP use."""

from __future__ import annotations

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
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
]

ROUTING_HINTS = {
    "check_connectivity_or_auth": ["health_check"],
    "discover_accounts_or_ids": [
        "list_ad_accounts",
        "get_account_pages",
        "list_instagram_accounts",
        "list_campaigns",
        "list_adsets",
        "list_ads",
    ],
    "inspect_single_entity_performance": ["get_entity_insights", "get_performance_breakdown"],
    "compare_multiple_entities": ["compare_performance"],
    "compare_current_vs_previous_window": ["compare_time_ranges"],
    "export_reporting_data": ["export_insights", "create_async_insights_report", "get_async_insights_report"],
    "find_optimization_opportunities": [
        "get_account_optimization_snapshot",
        "get_campaign_optimization_snapshot",
        "get_budget_pacing_report",
        "get_creative_fatigue_report",
        "get_delivery_risk_report",
        "get_recommendations",
        "get_budget_opportunities",
        "get_creative_opportunities",
        "get_audience_opportunities",
        "get_delivery_opportunities",
        "get_bidding_opportunities",
    ],
    "plan_targeting_or_audiences": [
        "search_interests",
        "get_interest_suggestions",
        "validate_interests",
        "search_geo_locations",
        "get_targeting_categories",
        "search_behaviors",
        "search_demographics",
        "estimate_audience_size",
        "get_reach_frequency_predictions",
        "list_audiences",
    ],
    "creative_workflows": [
        "get_account_pages",
        "list_instagram_accounts",
        "list_creatives",
        "get_ad_image",
        "preview_ad",
        "upload_creative_asset",
        "create_ad_creative",
        "create_ad",
    ],
    "research_competitor_ads": ["search_ads_archive"],
    "writes_after_confirmation": [
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
async def get_capabilities() -> dict[str, object]:
    """Use this when Claude needs to inspect the current tool groups, routing hints, and available resources."""
    settings = get_settings()
    return {
        "server": {
            "name": "Meta Ads FastMCP",
            "api_version": settings.api_version,
            "optimization_first": True,
            "primary_transport": "stdio",
            "secondary_transport": "streamable-http",
        },
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
        "resources": RESOURCE_URIS,
        "notes": [
            "Use discovery and diagnostics before write operations.",
            "compare_performance reuses the insights surface and avoids extra lookups when names are already present in insights rows.",
            "export_insights is a convenience wrapper over the core insights surface.",
            "Use get_account_pages before creative creation when a Page or Instagram-linked asset is needed.",
            "Use list_instagram_accounts when creative setup requires an Instagram identity rather than a Facebook Page.",
            "Use typed opportunity tools when the user asks specifically about budget, audience, creative, delivery, or bidding opportunities.",
            "search_ads_archive is public research data and does not depend on an ad account id, but the app still needs Ads Library API access.",
            "Write operations still depend on the token having ads_management-level permissions.",
        ],
    }
