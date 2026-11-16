"""The four MCP hints for every tool, kept in one table and applied at start-up.

A client reads these hints to decide which calls it may run without asking the
person and which it should retry. The tool files carry no annotations; this
table is the only place they are set.

- readOnlyHint: the tool changes nothing at Meta and nothing on this machine.
  It must equal what the name rules in ``tool_contracts._tool_effect`` say
  ("read"), because that function reads this hint first and intent routing
  relies on its answer.
- destructiveHint: the worst the tool can do deletes, removes, disconnects,
  cancels, archives or overwrites something that already exists.
- idempotentHint: running the same call twice leaves things as one call did.
  False for a tool that makes a new object each time it runs or uploads rows.
- openWorldHint: the tool has a web address the caller supplies fetched, or
  returns public content from outside the person's own assets.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from mcp.types import ToolAnnotations


def _hints(read_only: bool, destructive: bool, idempotent: bool, open_world: bool) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


# Changes nothing.
READ = _hints(True, False, True, False)
# Changes nothing, and returns public content from outside the person's assets.
READ_PUBLIC = _hints(True, False, True, True)
# Makes something new each time it runs; loses nothing that exists.
ADD = _hints(False, False, False, False)
# Makes something new from a web address the caller supplies, which Meta fetches.
ADD_FROM_WEB = _hints(False, False, False, True)
# Switches a link on; running it again changes nothing more.
SWITCH_ON = _hints(False, False, True, False)
# Deletes, removes, archives or sets an absolute value over an existing one.
OVERWRITE = _hints(False, True, True, False)
# Overwrites, and Meta fetches a web address the caller supplies.
OVERWRITE_FROM_WEB = _hints(False, True, True, True)
# Can lose existing data and does something new on every run.
OVERWRITE_EACH_RUN = _hints(False, True, False, False)
# Can lose existing data, does something new on every run, fetches a caller's web address.
OVERWRITE_EACH_RUN_FROM_WEB = _hints(False, True, False, True)


_TABLE: dict[str, ToolAnnotations] = {
    # Accounts, structure and discovery
    "list_ad_accounts": READ,
    "get_ad_account": READ,
    "list_campaigns": READ,
    "get_campaign": READ,
    "list_adsets": READ,
    "get_adset": READ,
    "list_ads": READ,
    "get_ad": READ,
    "get_account_pages": READ,
    "list_instagram_accounts": READ,
    "list_pages": READ,
    "list_businesses": READ,
    "list_business_users": READ,
    "get_field_reference": READ,
    # Campaigns, ad sets and ads
    "create_campaign": ADD,
    "update_campaign": OVERWRITE,
    "delete_campaign": OVERWRITE,
    "create_ad_set": ADD,
    "update_adset": OVERWRITE,
    "delete_adset": OVERWRITE,
    "create_ad": ADD,
    "update_ad": OVERWRITE,
    "delete_ad": OVERWRITE,
    "duplicate_campaign": ADD,
    "duplicate_adset": ADD,
    "duplicate_ad": ADD,
    # ACTIVE, PAUSED or ARCHIVED; ARCHIVED cannot be undone from this server.
    "set_campaign_status": OVERWRITE,
    "set_adset_status": OVERWRITE,
    "set_ad_status": OVERWRITE,
    "update_campaign_budget": OVERWRITE,
    "update_adset_budget": OVERWRITE,
    "update_adset_bid_amount": OVERWRITE,
    "update_campaign_bid_strategy": OVERWRITE,
    "update_adset_bid_strategy": OVERWRITE,
    # Creatives, images and videos
    "list_creatives": READ,
    "get_creative": READ,
    "list_creative_ads": READ,
    "get_ad_image": READ,
    "preview_ad": READ,
    "list_ad_images": READ,
    "list_ad_videos": READ,
    "list_instagram_media": READ,
    "create_ad_creative": ADD,
    "create_ad_creative_from_inputs": ADD,
    "update_ad_creative": OVERWRITE,
    "update_creative": OVERWRITE,
    # Makes a new copy of the creative on every run and attaches it to the ad.
    "update_ad_creative_enhancements": OVERWRITE_EACH_RUN,
    "delete_creative": OVERWRITE,
    "upload_creative_asset": ADD_FROM_WEB,
    "upload_ad_video": ADD_FROM_WEB,
    "delete_ad_image": OVERWRITE,
    "boost_instagram_post": ADD,
    # Audiences
    "list_audiences": READ,
    "get_audience": READ,
    "list_audience_adsets": READ,
    "create_custom_audience": ADD,
    "create_lookalike_audience": ADD,
    "update_custom_audience": OVERWRITE,
    "delete_audience": OVERWRITE,
    "add_audience_users": ADD,
    "remove_audience_users": OVERWRITE_EACH_RUN,
    # Tokens: each call asks Meta for a new token and stores nothing here.
    "generate_auth_url": READ,
    "exchange_code_for_token": ADD,
    "refresh_to_long_lived_token": ADD,
    "generate_system_user_token": ADD,
    "get_token_info": READ,
    "validate_token": READ,
    # Catalog access
    "list_catalog_assigned_users": READ,
    "list_user_assigned_catalogs": READ,
    "list_catalog_partners": READ,
    "preview_catalog_access_change": READ,
    "set_catalog_access": OVERWRITE,
    # Catalogs, products and product sets
    "list_catalogs": READ,
    "list_dpa_eligible_catalogs": READ,
    "get_catalog_diagnostics": READ,
    "get_catalog_dynamic_ads_health": READ,
    "list_catalog_data_sources": READ,
    "create_catalog": ADD,
    "update_catalog": OVERWRITE,
    "list_catalog_products": READ,
    # allow_upsert=true overwrites the product that already has this retailer_id.
    "create_catalog_product": OVERWRITE_EACH_RUN,
    "update_catalog_product": OVERWRITE,
    "delete_catalog_product": OVERWRITE,
    "batch_update_catalog_products": OVERWRITE_EACH_RUN,
    "build_product_set_filter": READ,
    "list_catalog_attribute_values": READ,
    "list_product_sets": READ,
    "create_product_set": ADD,
    "update_product_set": OVERWRITE,
    "delete_product_set": OVERWRITE,
    # Catalog feeds and event sources
    "list_product_feeds": READ,
    "create_product_feed": ADD_FROM_WEB,
    "update_product_feed": OVERWRITE_FROM_WEB,
    "delete_product_feed": OVERWRITE,
    # Without update_only=true, items missing from the file may be deleted.
    "create_product_feed_upload": OVERWRITE_EACH_RUN_FROM_WEB,
    "list_product_feed_uploads": READ,
    "list_feed_rules": READ,
    "create_feed_rule": ADD,
    "update_feed_rule": OVERWRITE,
    "delete_feed_rule": OVERWRITE,
    "list_catalog_event_sources": READ,
    "list_event_source_catalogs": READ,
    "get_catalog_event_source_health": READ,
    "get_catalog_event_source_recommendations": READ,
    "connect_catalog_event_source": SWITCH_ON,
    "disconnect_catalog_event_source": OVERWRITE,
    # Pixels and datasets
    "list_datasets": READ,
    "get_dataset": READ,
    "get_dataset_stats": READ,
    "get_dataset_quality": READ,
    "list_custom_conversions": READ,
    # Reporting. The async report tools start a report job at Meta but change no
    # advertising state; they are reads in routing and allowed in read-only mode.
    "get_entity_insights": READ,
    "get_insights": READ,
    "summarize_actions": READ,
    "get_performance_breakdown": READ,
    "compare_time_ranges": READ,
    "compare_performance": READ,
    "export_insights": READ,
    "create_async_insights_report": READ,
    "create_async_insights_report_batch": READ,
    "get_async_insights_report": READ,
    "summarize_async_insights_report": READ,
    # Change history. export_change_history saves an expiring export file that this
    # server manages, like any oversized response; it changes no advertising state.
    "list_change_history": READ,
    "export_change_history": READ,
    # Optimization and diagnostics
    "get_native_optimization_signals": READ,
    "get_account_optimization_snapshot": READ,
    "get_account_health_snapshot": READ,
    "get_campaign_optimization_snapshot": READ,
    "detect_auction_overlap": READ,
    "get_budget_pacing_report": READ,
    "get_creative_performance_report": READ,
    "get_ad_feedback_signals": READ,
    "get_creative_fatigue_report": READ,
    "get_audience_performance_report": READ,
    "get_delivery_risk_report": READ,
    "get_learning_phase_report": READ,
    "get_performance_trend": READ,
    "detect_performance_anomalies": READ,
    "get_auction_ranking_report": READ,
    "get_advertiser_context": READ,
    "get_delivery_errors": READ,
    "get_opportunity_score": READ,
    "get_recommendations": READ,
    "get_budget_opportunities": READ,
    "get_creative_opportunities": READ,
    "get_audience_opportunities": READ,
    "get_delivery_opportunities": READ,
    "get_bidding_opportunities": READ,
    # A/B tests and lift studies
    "list_ad_studies": READ,
    "get_ad_study": READ,
    "check_ab_test_eligibility": READ,
    "setup_ab_test": ADD,
    "create_ab_test": ADD,
    "create_lift_study": ADD,
    "update_ad_study": OVERWRITE,
    "cancel_ad_study": OVERWRITE,
    # Research, social feedback and targeting
    "search_ads_archive": READ_PUBLIC,
    "get_ad_social_context": READ,
    "list_ad_comments": READ,
    "list_comment_replies": READ,
    "list_page_recommendations": READ,
    "search_interests": READ,
    "get_interest_suggestions": READ,
    "validate_interests": READ,
    "search_geo_locations": READ,
    "search_behaviors": READ,
    "get_targeting_categories": READ,
    "search_demographics": READ,
    "estimate_audience_size": READ,
    "get_reach_frequency_predictions": READ,
    # Server utilities and reference notes
    "health_check": READ,
    "get_capabilities": READ,
    "list_mutation_tools": READ,
    "read_overflow_artifact": READ,
    # Deletes a saved export file on this machine; allowed in read-only mode.
    "delete_overflow_artifact": OVERWRITE,
    "get_meta_object_model": READ,
    "get_metrics_reference": READ,
    "get_v25_notes": READ,
    "get_v26_notes": READ,
    "get_optimization_playbook": READ,
}

TOOL_ANNOTATIONS: Mapping[str, ToolAnnotations] = _TABLE

# search_tools only searches the catalogue.
SEARCH_TOOL_ANNOTATIONS = READ


def reachable_through_call_tool(*, read_only: bool) -> set[str]:
    """The tools call_tool can run: all of them, or only those read-only mode allows."""
    names = set(TOOL_ANNOTATIONS)
    if read_only:
        from meta_ads_mcp.read_only import read_only_tool_names

        names &= read_only_tool_names()
    return names


def call_tool_annotations(*, read_only: bool) -> ToolAnnotations:
    """call_tool carries the hints of the most dangerous tool it can reach."""
    reachable = [TOOL_ANNOTATIONS[name] for name in reachable_through_call_tool(read_only=read_only)]
    return _hints(
        read_only=all(hints.readOnlyHint for hints in reachable),
        destructive=any(hints.destructiveHint for hints in reachable),
        idempotent=all(hints.idempotentHint for hints in reachable),
        open_world=any(hints.openWorldHint for hints in reachable),
    )


def describe_hints(annotations: ToolAnnotations | None) -> str:
    """The four hints as short plain text for the compact search output."""
    if annotations is None:
        return ""

    def word(value: bool | None) -> str:
        return "yes" if value else "no"

    return (
        f"read-only: {word(annotations.readOnlyHint)}, "
        f"destructive: {word(annotations.destructiveHint)}, "
        f"idempotent: {word(annotations.idempotentHint)}, "
        f"open-world: {word(annotations.openWorldHint)}"
    )


def _registered_tools(server: Any) -> Iterable[Any]:
    from fastmcp.tools import Tool

    # FastMCP has no synchronous public listing; read the registry in its order.
    return [c for c in list(server.local_provider._components.values()) if isinstance(c, Tool)]


def apply_tool_annotations(server: Any) -> None:
    """Give each registered tool its table entry, keeping registration order.

    Uses FastMCP's public remove_tool and add_tool on the server's local provider:
    each tool is replaced by a copy that differs only in its annotations. Every
    tool is removed and added back in turn, so the order stays as registered. A
    tool with no entry keeps its own annotations; tests/test_tool_annotations.py
    fails for it.
    """
    tools = list(_registered_tools(server))
    if all(TOOL_ANNOTATIONS.get(tool.name, tool.annotations) == tool.annotations for tool in tools):
        return
    provider = server.local_provider
    for tool in tools:
        annotations = TOOL_ANNOTATIONS.get(tool.name)
        provider.remove_tool(tool.name)
        if annotations is None or tool.annotations == annotations:
            provider.add_tool(tool)
        else:
            provider.add_tool(tool.model_copy(update={"annotations": annotations}))
