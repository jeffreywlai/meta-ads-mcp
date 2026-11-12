"""The parity ledger must account for every tool Meta's official Ads MCP server serves."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.exceptions import ToolError

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.input_compat import canonical_tool_name, resolve_tool_name
from meta_ads_mcp.official_parity import (
    DOCUMENTED_NOT_SERVED,
    LIVE_VERIFIED_TOOLS,
    OFFICIAL_TOOLS,
    SERVED_STATUSES,
    official_tool_guidance,
    parity_summary,
    resolve_official_tool,
)
from meta_ads_mcp.tools import utility

# The tool names Meta's server returned from tools/list on 2026-11-12.
SERVED_NAMES = [
    "ads_account_get_activity_logs", "ads_activate_entity", "ads_boost_ig_post",
    "ads_catalog_create", "ads_catalog_create_feed_rule", "ads_catalog_create_product_feed",
    "ads_catalog_create_product_feed_upload_session", "ads_catalog_create_product_set",
    "ads_catalog_delete_product", "ads_catalog_event_source_connect",
    "ads_catalog_event_source_disconnect", "ads_catalog_event_source_get",
    "ads_catalog_event_source_get_catalogs", "ads_catalog_event_source_get_health",
    "ads_catalog_event_source_get_recommendations", "ads_catalog_get_businesses",
    "ads_catalog_get_data_sources", "ads_catalog_get_diagnostics",
    "ads_catalog_get_dynamic_ads_health", "ads_catalog_get_feed_rules",
    "ads_catalog_get_product_feed_upload_sessions", "ads_catalog_get_suggested_filter",
    "ads_catalog_list_catalogs", "ads_catalog_list_dpa_eligible_catalogs",
    "ads_catalog_list_partner_integrations", "ads_catalog_list_product_feeds",
    "ads_catalog_list_product_sets", "ads_catalog_list_products", "ads_catalog_product_create",
    "ads_catalog_product_feed_delete", "ads_catalog_product_feed_delete_rule",
    "ads_catalog_product_set_delete", "ads_catalog_update_catalog",
    "ads_catalog_update_feed_rule", "ads_catalog_update_product",
    "ads_catalog_update_product_feed", "ads_catalog_update_product_set", "ads_create_ad",
    "ads_create_ad_set", "ads_create_campaign", "ads_create_creative",
    "ads_create_custom_audience", "ads_creative_delete", "ads_creative_update",
    "ads_creative_upload_local_image", "ads_creative_upload_media",
    "ads_delete_custom_audience", "ads_delete_local_ad_image",
    "ads_experiment_abtest_create_test", "ads_experiment_abtest_get_test",
    "ads_experiment_abtest_update_test", "ads_experiment_check_eligibility",
    "ads_experiment_lift_create_test", "ads_experiment_lift_get_test",
    "ads_experiment_list_tests", "ads_finalize_local_ad_image_upload",
    "ads_get_ad_account_custom_audiences", "ads_get_ad_account_pages", "ads_get_ad_accounts",
    "ads_get_ad_entities", "ads_get_ad_images", "ads_get_ad_preview",
    "ads_get_ad_preview_screenshot", "ads_get_ad_videos", "ads_get_async_result",
    "ads_get_creative_ads", "ads_get_creatives", "ads_get_custom_audience",
    "ads_get_custom_audience_adsets", "ads_get_customconversions", "ads_get_dataset_details",
    "ads_get_dataset_quality", "ads_get_dataset_stats", "ads_get_datasets", "ads_get_errors",
    "ads_get_field_context", "ads_get_help_article", "ads_get_ig_accounts", "ads_get_ig_media",
    "ads_get_opportunity_score", "ads_get_pages_for_business", "ads_get_user_pages",
    "ads_insights_advertiser_context", "ads_insights_anomaly_signal",
    "ads_insights_auction_ranking_benchmarks", "ads_insights_industry_benchmark",
    "ads_insights_performance_trend", "ads_insights_performance_vs_peers", "ads_library_search",
    "ads_log_ui_interaction", "ads_permission_list_assigned_assets",
    "ads_permission_list_assigned_users", "ads_permission_list_business_users",
    "ads_permission_preview_set_asset_permissions", "ads_permission_set_asset_permissions",
    "ads_pixel_event_create", "ads_pixel_event_delete", "ads_pixel_event_read",
    "ads_pixel_event_update", "ads_pixel_parameter_create", "ads_pixel_parameter_delete",
    "ads_pixel_parameter_read", "ads_pixel_parameter_update", "ads_update_custom_audience",
    "ads_update_entity", "offer_report",
]


def _registered() -> set[str]:
    return {name for names in utility.TOOL_GROUPS.values() for name in names}


def test_every_served_official_name_has_an_entry() -> None:
    assert len(SERVED_NAMES) == len(set(SERVED_NAMES)) == 106
    assert set(OFFICIAL_TOOLS) == set(SERVED_NAMES) | DOCUMENTED_NOT_SERVED
    assert not DOCUMENTED_NOT_SERVED & set(SERVED_NAMES)


def test_entries_name_only_registered_tools() -> None:
    registered = _registered()
    for name, entry in OFFICIAL_TOOLS.items():
        if entry.status in SERVED_STATUSES:
            assert entry.tools, f"{name} is {entry.status} but names no tool"
        if entry.status == "app_internal":
            assert not entry.tools, name
        assert set(entry.tools) <= registered, f"{name} names unregistered tools: {set(entry.tools) - registered}"


def test_every_entry_has_a_note_and_a_valid_status() -> None:
    for name, entry in OFFICIAL_TOOLS.items():
        assert entry.status in {"covered", "partial", "missing", "unavailable", "app_internal"}, name
        assert entry.note.strip(), f"{name} needs a note"
        if entry.status != "covered":
            # Anything short of full coverage must say what differs or why.
            assert len(entry.note.strip()) >= 60, f"{name} needs a real explanation"


def test_official_names_do_not_collide_with_our_tool_names() -> None:
    assert not set(OFFICIAL_TOOLS) & _registered()


def test_verified_is_derived_from_live_runs_only() -> None:
    assert LIVE_VERIFIED_TOOLS <= _registered()
    for name, entry in OFFICIAL_TOOLS.items():
        if entry.status not in SERVED_STATUSES:
            assert entry.verified is None, name
        else:
            expected = "live" if all(tool in LIVE_VERIFIED_TOOLS for tool in entry.tools) else "mocked"
            assert entry.verified == expected, name


def test_no_write_tool_is_marked_live_verified() -> None:
    """Live checks so far were read-only, so a write tool can never be listed as verified."""
    assert not LIVE_VERIFIED_TOOLS & set(utility.TOOL_GROUPS["writes"])


def test_summary_counts_add_up() -> None:
    summary = parity_summary()
    assert summary["official_tools"] == 107
    assert summary["served_by_meta"] == 106
    assert sum(summary["by_status"].values()) == 107
    served = summary["by_status"]["covered"] + summary["by_status"]["partial"]
    assert sum(summary["by_verified"].values()) == served


def test_single_target_names_resolve_like_aliases() -> None:
    assert resolve_official_tool("ads_get_opportunity_score") == "get_opportunity_score"
    assert canonical_tool_name("ads_get_opportunity_score") == "get_opportunity_score"
    assert resolve_tool_name("ads_catalog_get_diagnostics", None) == "get_catalog_diagnostics"
    tool = asyncio.run(mcp_server.get_tool("ads_catalog_get_data_sources"))
    assert tool is not None and tool.name == "list_catalog_data_sources"


def test_multi_target_name_error_lists_options() -> None:
    assert resolve_official_tool("ads_update_entity") is None
    with pytest.raises(ValidationError) as error:
        resolve_tool_name("ads_update_entity", None)
    message = str(error.value)
    for option in ("update_campaign", "update_adset", "update_ad"):
        assert option in message


@pytest.mark.parametrize(
    "name",
    ["ads_get_help_article", "ads_catalog_get_businesses", "ads_log_ui_interaction", "ads_pixel_event_read"],
)
def test_unserved_name_error_gives_the_reason(name) -> None:
    assert resolve_official_tool(name) is None
    with pytest.raises(ValidationError, match="no equivalent here"):
        resolve_tool_name(None, name)


def test_guidance_names_the_nearest_tool_and_stays_silent_otherwise() -> None:
    assert "get_auction_ranking_report" in official_tool_guidance("ads_insights_industry_benchmark")
    assert official_tool_guidance("ads_get_opportunity_score") is None
    assert official_tool_guidance("not_an_official_tool") is None


def test_unknown_and_existing_names_are_unchanged() -> None:
    assert canonical_tool_name("list_campaigns") == "list_campaigns"
    assert canonical_tool_name("get_ad_set") == "get_adset"
    assert canonical_tool_name("something_else") == "something_else"


def test_get_capabilities_official_tool_lookup() -> None:
    result = asyncio.run(
        mcp_server.call_tool("get_capabilities", {"official_tool": "ads_catalog_get_suggested_filter"})
    ).structured_content
    assert result["official_tool"]["status"] == "partial"
    assert result["official_tool"]["tools"] == ["build_product_set_filter"]
    assert result["official_tool"]["verified"] in {"live", "mocked"}
    assert result["official_tool"]["served_by_meta"] is True

    missing = asyncio.run(
        mcp_server.call_tool("get_capabilities", {"official_tool": "ads_get_help_article"})
    ).structured_content
    assert missing["official_tool"]["status"] == "unavailable"
    assert missing["official_tool"]["verified"] is None

    not_served = asyncio.run(
        mcp_server.call_tool("get_capabilities", {"official_tool": "ads_update_custom_audience_users"})
    ).structured_content
    assert not_served["official_tool"]["served_by_meta"] is False

    with pytest.raises(ToolError, match="not one of Meta's official"):
        asyncio.run(mcp_server.call_tool("get_capabilities", {"official_tool": "ads_made_up"}))


def test_full_manifest_reports_parity_counts() -> None:
    result = asyncio.run(
        mcp_server.call_tool("get_capabilities", {"include_full_manifest": True})
    ).structured_content
    assert result["official_parity"]["official_tools"] == 107


@pytest.mark.parametrize("routed", [True, False])
def test_official_write_names_are_blocked_in_read_only(monkeypatch, routed) -> None:
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    official = "ads_catalog_create_product_set"
    assert resolve_official_tool(official) == "create_product_set"
    tool = "call_tool" if routed else official
    arguments = {"name": official, "arguments": {}} if routed else {}
    with pytest.raises(ToolError, match="META_READ_ONLY"):
        asyncio.run(mcp_server.call_tool(tool, arguments))
