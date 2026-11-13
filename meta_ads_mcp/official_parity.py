"""Ledger mapping Meta's official Ads MCP server tools to this server's tools.

Meta's hosted server (https://mcp.facebook.com/ads) served 106 tools on
2026-11-12. Its public documentation lists 88 names; 87 of those are served,
19 served tools are undocumented, and one documented tool is not served. Every
served name has an entry here, written from the tool's real description and
parameter list, saying which tool on this server serves the same need.

Status values:

* ``covered``: a tool here does everything Meta's tool does, or more.
* ``partial``: a tool here does part of it. The note says what Meta's tool can
  do that ours cannot, including cases where Meta's tool gives its own internal
  answer and ours applies a stated rule to public data.
* ``missing``: nothing here yet, although Meta's public API appears to allow it.
* ``unavailable``: no public API route is known, so no tool is registered.
  ``tools`` may name the nearest real tool.
* ``app_internal``: plumbing for Meta's own app (upload dialogs, telemetry,
  confirm buttons). There is no capability to match.

``verified`` is derived, not declared: ``live`` when every serving tool has been
run against a real ad account and Meta accepted the request, otherwise
``mocked``. Mocked tests use a fake client that accepts any endpoint name, so
they cannot catch a wrong endpoint or field. Write tools stay ``mocked`` until a
live write test is explicitly approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ParityStatus = Literal["covered", "partial", "missing", "unavailable", "app_internal"]
Verification = Literal["live", "mocked"]
SERVED_STATUSES: frozenset[str] = frozenset({"covered", "partial"})

# Tools run read-only against a real ad account on 2026-11-12 or 2026-11-13, with
# Meta accepting the request. A tool is listed only if every call made in its
# latest run succeeded; a tool that later fails live is removed until it is fixed.
LIVE_VERIFIED_TOOLS: frozenset[str] = frozenset(
    {
        "build_product_set_filter",
        "check_ab_test_eligibility",
        "compare_performance",
        "detect_performance_anomalies",
        "export_change_history",
        "get_ad_account",
        "get_ad_study",
        "get_advertiser_context",
        "get_auction_ranking_report",
        "get_audience",
        "get_catalog_diagnostics",
        "get_catalog_dynamic_ads_health",
        "get_catalog_event_source_health",
        "get_catalog_event_source_recommendations",
        "get_dataset",
        "get_dataset_quality",
        "get_dataset_stats",
        "get_delivery_errors",
        "get_entity_insights",
        "get_field_reference",
        "get_opportunity_score",
        "get_performance_trend",
        "list_ad_accounts",
        "list_ad_images",
        "list_ad_studies",
        "list_ad_videos",
        "list_ads",
        "list_adsets",
        "list_audience_adsets",
        "list_audiences",
        "list_business_users",
        "list_businesses",
        "list_campaigns",
        "list_catalog_assigned_users",
        "list_catalog_attribute_values",
        "list_catalog_data_sources",
        "list_catalog_event_sources",
        "list_catalog_partners",
        "list_catalog_products",
        "list_catalogs",
        "list_change_history",
        "list_creative_ads",
        "list_creatives",
        "list_custom_conversions",
        "list_datasets",
        "list_dpa_eligible_catalogs",
        "list_event_source_catalogs",
        "list_feed_rules",
        "list_instagram_accounts",
        "list_instagram_media",
        "list_pages",
        "list_product_feed_uploads",
        "list_product_feeds",
        "list_product_sets",
        "preview_ad",
        "preview_catalog_access_change",
    }
)

# Documented by Meta but absent from its live server on 2026-11-12.
DOCUMENTED_NOT_SERVED: frozenset[str] = frozenset({"ads_update_custom_audience_users"})


@dataclass(frozen=True, slots=True)
class OfficialToolEntry:
    """How one of Meta's official tools is served here."""

    status: ParityStatus
    tools: tuple[str, ...]
    note: str

    @property
    def verified(self) -> Verification | None:
        """Whether every serving tool has run against a real account."""
        if self.status not in SERVED_STATUSES or not self.tools:
            return None
        return "live" if all(tool in LIVE_VERIFIED_TOOLS for tool in self.tools) else "mocked"

    def to_dict(self, name: str) -> dict[str, object]:
        """Return a JSON-native view for tool responses."""
        return {
            "official_tool": name,
            "status": self.status,
            "verified": self.verified,
            "tools": list(self.tools),
            "note": self.note,
            "served_by_meta": name not in DOCUMENTED_NOT_SERVED,
        }


def _covered(*tools: str, note: str) -> OfficialToolEntry:
    return OfficialToolEntry("covered", tools, note)


def _partial(*tools: str, note: str) -> OfficialToolEntry:
    return OfficialToolEntry("partial", tools, note)


def _missing(*nearest: str, note: str) -> OfficialToolEntry:
    return OfficialToolEntry("missing", nearest, note)


def _unavailable(*nearest: str, note: str) -> OfficialToolEntry:
    return OfficialToolEntry("unavailable", nearest, note)


def _app_internal(note: str) -> OfficialToolEntry:
    return OfficialToolEntry("app_internal", (), note)


OFFICIAL_TOOLS: dict[str, OfficialToolEntry] = {
    "ads_account_get_activity_logs": _covered(
        "list_change_history", "export_change_history",
        note="Covers Meta's tool. Ours also offers: Retrieves every event across pages with stated coverage; control over including child objects.",
    ),
    "ads_activate_entity": _partial(
        "set_campaign_status", "set_adset_status", "set_ad_status",
        note="Meta's tool can also: Publishes Ads Manager drafts, whole or partial. Why this is open: No public API route known.",
    ),
    "ads_boost_ig_post": _partial(
        "boost_instagram_post", "create_ad_creative_from_inputs",
        note="Meta's tool can also: One-step boosts for Traffic (profile visits, Meta's default), Leads, Sales and App Promotion, and to messaging destinations. Takes a creator-issued ad_code. Defaults budget, targeting and Page. Ours one-step mode takes OUTCOME_ENGAGEMENT or OUTCOME_AWARENESS and requires daily_budget, targeting and page_id. Why this is open: Built differently: two objectives, no default budget or targeting.",
    ),
    "ads_catalog_create": _covered(
        "create_catalog", "create_product_feed", "create_product_feed_upload", "batch_update_catalog_products",
        note="Same capability.",
    ),
    "ads_catalog_create_feed_rule": _covered(
        "create_feed_rule",
        note="Creates an attribute mapping or transformation rule on a feed.",
    ),
    "ads_catalog_create_product_feed": _partial(
        "create_product_feed",
        note="Meta's tool can also: Named feed_type for hotel, flight, vehicle and other non-product feeds; ours via params. Reclassified under the strict rule; code unchanged. Why this is open: Not built.",
    ),
    "ads_catalog_create_product_feed_upload_session": _covered(
        "create_product_feed_upload",
        note="Covers Meta's tool. Ours also offers: Uploads from any URL, with add-or-update-only mode (update_only); Meta's only re-fetches the configured URL.",
    ),
    "ads_catalog_create_product_set": _covered(
        "create_product_set",
        note="Creates a product set from a filter.",
    ),
    "ads_catalog_delete_product": _covered(
        "batch_update_catalog_products", "delete_catalog_product",
        note="Same capability.",
    ),
    "ads_catalog_event_source_connect": _covered(
        "connect_catalog_event_source",
        note="Connects an event source to a catalog.",
    ),
    "ads_catalog_event_source_disconnect": _covered(
        "disconnect_catalog_event_source",
        note="Disconnects an event source from a catalog.",
    ),
    "ads_catalog_event_source_get": _covered(
        "list_catalog_event_sources",
        note="Pixels, apps and offline datasets connected to a catalog.",
    ),
    "ads_catalog_event_source_get_catalogs": _covered(
        "list_event_source_catalogs",
        note="Same capability.",
    ),
    "ads_catalog_event_source_get_health": _covered(
        "get_catalog_event_source_health",
        note="Same capability.",
    ),
    "ads_catalog_event_source_get_recommendations": _partial(
        "get_catalog_event_source_recommendations",
        note="Meta's tool can also: Meta's computed best source (recommended_pixel_id), considering apps and offline datasets as well as pixels. Why this is open: No public API route known.",
    ),
    "ads_catalog_get_businesses": _covered(
        "list_businesses",
        note="Same capability.",
    ),
    "ads_catalog_get_data_sources": _covered(
        "list_catalog_data_sources",
        note="Lists feeds, uploads and API sources for a catalog.",
    ),
    "ads_catalog_get_diagnostics": _covered(
        "get_catalog_diagnostics",
        note="Errors and opportunities ordered by products affected.",
    ),
    "ads_catalog_get_dynamic_ads_health": _partial(
        "get_catalog_dynamic_ads_health",
        note="Meta's tool can also: Meta's named checks (checks) and format-specific product counts (ad_creation_use_case). Why this is open: No public API route known.",
    ),
    "ads_catalog_get_feed_rules": _covered(
        "list_feed_rules",
        note="Lists rules on a feed.",
    ),
    "ads_catalog_get_product_feed_upload_sessions": _covered(
        "list_product_feed_uploads",
        note="Uploads with status, counts and errors ordered by rows affected.",
    ),
    "ads_catalog_get_suggested_filter": _partial(
        "list_catalog_attribute_values", "build_product_set_filter",
        note="Meta's tool can also: Turns a plain-language description into a filter. Why this is open: Built differently: lists actual values for the caller to match.",
    ),
    "ads_catalog_list_catalogs": _covered(
        "list_catalogs", "list_businesses",
        note="Lists catalogs for a business or ad account, or returns one catalog with counts.",
    ),
    "ads_catalog_list_dpa_eligible_catalogs": _partial(
        "list_dpa_eligible_catalogs",
        note="Meta's tool can also: Meta's own eligibility ranking, and tuning by page_id and ad_format. Why this is open: No public API route known.",
    ),
    "ads_catalog_list_partner_integrations": _unavailable(
        note="No public API route is known for partner store integrations (Shopify, WooCommerce and similar) and their sync health.",
    ),
    "ads_catalog_list_product_feeds": _covered(
        "list_product_feeds",
        note="Lists feeds, or one feed with settings and latest upload.",
    ),
    "ads_catalog_list_product_sets": _covered(
        "list_product_sets",
        note="Lists sets in a catalog, one set, or the sets containing a product.",
    ),
    "ads_catalog_list_products": _partial(
        "list_catalog_products", "list_catalog_attribute_values",
        note="Meta's tool can also: Ranks products by product views (top_products). Why this is open: No public API route known.",
    ),
    "ads_catalog_product_create": _covered(
        "batch_update_catalog_products", "create_catalog_product",
        note="Same capability.",
    ),
    "ads_catalog_product_feed_delete": _covered(
        "delete_product_feed",
        note="Deletes a feed; its products are removed unless another source supplies them.",
    ),
    "ads_catalog_product_feed_delete_rule": _covered(
        "delete_feed_rule",
        note="Deletes a feed rule; the feed and products are unaffected.",
    ),
    "ads_catalog_product_set_delete": _covered(
        "delete_product_set",
        note="Deletes a product set; products remain.",
    ),
    "ads_catalog_update_catalog": _partial(
        "update_catalog",
        note="Meta's tool can also: Sets up, test-runs and saves the checkout URL (checkout_base_url, checkout_url_test_only). Why this is open: No public API route known.",
    ),
    "ads_catalog_update_feed_rule": _covered(
        "update_feed_rule",
        note="Same capability.",
    ),
    "ads_catalog_update_product": _covered(
        "batch_update_catalog_products", "update_catalog_product",
        note="Same capability.",
    ),
    "ads_catalog_update_product_feed": _partial(
        "update_product_feed",
        note="Meta's tool can also: Stops a fetch schedule without deleting the feed (clear_replace_schedule, clear_update_schedule). Named parsing options; ours via params. Why this is open: Not built.",
    ),
    "ads_catalog_update_product_set": _covered(
        "update_product_set",
        note="Edits a product set with before and after values.",
    ),
    "ads_create_ad": _partial(
        "create_ad", "duplicate_ad",
        note="Meta's tool can also: Named ad_schedule_start_time and ad_schedule_end_time (ours via params). Why this is open: Not built.",
    ),
    "ads_create_ad_set": _partial(
        "create_ad_set", "duplicate_adset",
        note="Meta's tool can also: Named destination_type (needed for messaging and profile-visit ad sets), pacing_type, is_dynamic_creative, budget_schedule_specs and optimization_sub_event (ours via params). Auto-fills DSA fields and the messaging Page. Why this is open: Not built.",
    ),
    "ads_create_campaign": _partial(
        "create_campaign", "duplicate_campaign",
        note="Meta's tool can also: Named budget_schedule_specs, is_skadnetwork_attribution and campaign_optimization_type (ours via params). Staging into Ads Manager drafts. Why this is open: Not built.",
    ),
    "ads_create_creative": _partial(
        "create_ad_creative_from_inputs", "create_ad_creative", "upload_creative_asset",
        note="Meta's tool can also: Builds these formats from named inputs, which ours does not: asset pool (multi_media), catalog carousel (product_set_id), Facebook Partnership Ads (facebook_partnership_ad), Instagram Partnership Ads from new media, click-to-message carousels and Instagram posts, mixed image and video placement sets. Also sets AI-content disclosure (self_ai_disclosure) and Advantage+ defaults at creation, and downloads image_url into the account. Why this is open: Not built.",
    ),
    "ads_create_custom_audience": _partial(
        "create_custom_audience", "create_lookalike_audience",
        note="Meta's tool can also: Named is_value_based (value-based customer lists) and audience_labels; ours via params. Reclassified under the strict rule; code unchanged since the first pass. Why this is open: Not built.",
    ),
    "ads_creative_delete": _covered(
        "delete_creative",
        note="Same capability.",
    ),
    "ads_creative_update": _covered(
        "update_creative",
        note="Same capability.",
    ),
    "ads_creative_upload_local_image": _app_internal(
        "Part of Meta's own app, with no capability to match. Starts an upload from Meta's in-chat upload app. The capability behind it (upload a local file) is covered for images by upload_creative_asset.",
    ),
    "ads_creative_upload_media": _partial(
        "upload_ad_video", "upload_creative_asset",
        note="Meta's tool can also: Local video files up to 100 MB; ours takes 25 MB from META_UPLOAD_DIR and no chunked upload. AI provenance tag (media_creation_source). Why this is open: Not built.",
    ),
    "ads_delete_custom_audience": _covered(
        "delete_audience",
        note="Deletes a custom audience.",
    ),
    "ads_delete_local_ad_image": _covered(
        "delete_ad_image",
        note="Same capability.",
    ),
    "ads_experiment_abtest_create_test": _partial(
        "create_ab_test", "setup_ab_test",
        note="Meta's tool can also: Chooses the metric that decides the winner (primary_kpi, secondary_kpis). Our code states Meta's documented API offers no such choice; not independently verified. Why this is open: No public API route known.",
    ),
    "ads_experiment_abtest_get_test": _covered(
        "get_ad_study",
        note="Pass include_results=true for the winner and confidence.",
    ),
    "ads_experiment_abtest_update_test": _covered(
        "update_ad_study", "cancel_ad_study",
        note="Use update_ad_study to edit and cancel_ad_study to end or cancel.",
    ),
    "ads_experiment_check_eligibility": _partial(
        "check_ab_test_eligibility",
        note="Meta's tool can also: Meta's own verdict, including Lift study eligibility. Accepts ad ids; ours accepts campaign or ad set only, although create_ab_test now takes ad cells. Why this is open: No public API route known.",
    ),
    "ads_experiment_lift_create_test": _covered(
        "create_lift_study",
        note="Creates a conversion lift study on the owning business.",
    ),
    "ads_experiment_lift_get_test": _covered(
        "get_ad_study",
        note="Pass include_results=true for lift and confidence.",
    ),
    "ads_experiment_list_tests": _covered(
        "list_ad_studies",
        note="A/B tests and lift studies on an account, business or one entity.",
    ),
    "ads_finalize_local_ad_image_upload": _app_internal(
        "Part of Meta's own app, with no capability to match. Finishes that upload.",
    ),
    "ads_get_ad_account_custom_audiences": _covered(
        "list_audiences",
        note="Lists custom audiences, optionally by subtype.",
    ),
    "ads_get_ad_account_pages": _covered(
        "list_pages",
        note="Call list_pages with source='ad_account' and account_id.",
    ),
    "ads_get_ad_accounts": _covered(
        "list_ad_accounts", "get_ad_account",
        note="Lists ad accounts the token can access.",
    ),
    "ads_get_ad_entities": _partial(
        "get_entity_insights", "compare_performance", "list_campaigns", "list_adsets", "list_ads",
        note="Meta's tool can also: Reads unpublished Ads Manager drafts (object_state=draft) and adds benchmark and incrementality context (include_additional_context). Also returns settings and metrics in one row, which ours does in two calls. Why this is open: No public API route known.",
    ),
    "ads_get_ad_images": _covered(
        "list_ad_images",
        note="Lists the account's image library.",
    ),
    "ads_get_ad_preview": _covered(
        "preview_ad",
        note="Covers Meta's tool. Ours also offers: Previews an unsaved creative spec (creative).",
    ),
    "ads_get_ad_preview_screenshot": _app_internal(
        "Part of Meta's own app, with no capability to match. Screenshot sink for Meta's preview card (\"not intended for model use\").",
    ),
    "ads_get_ad_videos": _covered(
        "list_ad_videos",
        note="Same capability.",
    ),
    "ads_get_async_result": _app_internal(
        "Part of Meta's own app, with no capability to match. Collects results of background jobs: MAA_RESPONSE (an answer from a Meta agent) and IMAGE_VARIATIONS (generated image variations with hashes). No served tool starts these jobs, so the capability behind them cannot be assessed.",
    ),
    "ads_get_creative_ads": _covered(
        "list_creative_ads",
        note="Meta has no server-side filter for this, so the tool scans the account's ads and reports how many were examined and whether the scan was complete.",
    ),
    "ads_get_creatives": _covered(
        "get_creative", "list_creatives",
        note="Lists creatives in an ad account.",
    ),
    "ads_get_custom_audience": _covered(
        "get_audience",
        note="Returns one custom or lookalike audience.",
    ),
    "ads_get_custom_audience_adsets": _covered(
        "list_audience_adsets",
        note="Same capability.",
    ),
    "ads_get_customconversions": _covered(
        "list_custom_conversions",
        note="Custom conversions on an ad account.",
    ),
    "ads_get_dataset_details": _covered(
        "get_dataset",
        note="One dataset's settings.",
    ),
    "ads_get_dataset_quality": _partial(
        "get_dataset_quality",
        note="Meta's tool can also: CRM and custom-attribution channels (query_type); ours covers web and offline. Why this is open: No public API route known.",
    ),
    "ads_get_dataset_stats": _covered(
        "get_dataset_stats",
        note="Event volume for up to 28 days, split by event, device or source.",
    ),
    "ads_get_datasets": _covered(
        "list_datasets",
        note="Pixels and datasets on an ad account or business.",
    ),
    "ads_get_errors": _covered(
        "get_delivery_errors",
        note="Covers Meta's tool. Ours also offers: Account status and disable reason, ad review feedback, ordering by severity and spend. Meta's excludes ad rejections and account problems.",
    ),
    "ads_get_field_context": _partial(
        "get_field_reference",
        note="Meta's tool can also: Field descriptions, display names, aliases (spend to amount_spent) and which levels support each metric. Ours (level=metric) gives types, sort syntax and filter operators. Why this is open: Built differently: SDK-derived types, no descriptions or aliases.",
    ),
    "ads_get_help_article": _unavailable(
        note="Meta has no public API for searching the Business Help Center. Search https://www.facebook.com/business/help directly.",
    ),
    "ads_get_ig_accounts": _covered(
        "list_instagram_accounts",
        note="Lists Instagram accounts linked to an ad account.",
    ),
    "ads_get_ig_media": _covered(
        "list_instagram_media",
        note="Lists posts and reels, or stories with source='stories'. Only boost-eligible items by default; each item says whether it can be boosted.",
    ),
    "ads_get_opportunity_score": _covered(
        "get_opportunity_score",
        note="Covers Meta's tool. Ours also offers: Daily score history up to 45 days and per-day change reasons (include_history_reasons).",
    ),
    "ads_get_pages_for_business": _covered(
        "list_pages",
        note="Call list_pages with source='business' and business_id.",
    ),
    "ads_get_user_pages": _covered(
        "list_pages",
        note="Call list_pages with source='user'.",
    ),
    "ads_insights_advertiser_context": _partial(
        "get_advertiser_context",
        note="Meta's tool can also: Takes an explicit date range (date_from/date_to, date_preset) and ad ids. Ours takes campaign or ad set ids and a trailing lookback_days of 7 to 90. The summary itself is Meta's own. Why this is open: Not built.",
    ),
    "ads_insights_anomaly_signal": _partial(
        "detect_performance_anomalies",
        note="Meta's tool can also: Meta's own detector, over entity lists of unstated size. Ours applies its stated median rule to at most 10 entities per call (object_ids, child_level). Why this is open: Built differently: stated rule, up to 10 entities per call.",
    ),
    "ads_insights_auction_ranking_benchmarks": _covered(
        "get_auction_ranking_report", "detect_auction_overlap",
        note="Same capability.",
    ),
    "ads_insights_industry_benchmark": _unavailable(
        "get_auction_ranking_report",
        note="Meta's public API has no endpoint for benchmarks from similar advertisers. get_auction_ranking_report uses Meta's public ranking fields, which compare each ad with ads competing for the same audience.",
    ),
    "ads_insights_performance_trend": _partial(
        "get_performance_trend",
        note="Meta's tool can also: REACH as a trend metric. Period-by-period series for each of several entities; ours returns per-entity totals and change in multi-entity mode. Why this is open: Not built.",
    ),
    "ads_insights_performance_vs_peers": _unavailable(
        "get_auction_ranking_report",
        note="Meta's public API has no endpoint for percentile comparisons against similar advertisers. get_auction_ranking_report is the nearest public signal.",
    ),
    "ads_library_search": _covered(
        "search_ads_archive",
        note="Same capability.",
    ),
    "ads_log_ui_interaction": _app_internal(
        "Part of Meta's own app, with no capability to match. Telemetry from Meta's UI widgets.",
    ),
    "ads_permission_list_assigned_assets": _partial(
        "list_user_assigned_catalogs",
        note="Meta's tool can also: Shows the permissions shared with a partner business; ours lists the shared catalogs only. Name search. Why this is open: Built differently: partner rows without permissions.",
    ),
    "ads_permission_list_assigned_users": _partial(
        "list_catalog_assigned_users",
        note="Meta's tool can also: Explains why a person lacks access (not on business, no permissions, two-factor required) and counts access through Business Asset Groups and business roles. Name search. Why this is open: Built differently: direct assignments only.",
    ),
    "ads_permission_list_business_users": _covered(
        "list_business_users",
        note="Same capability.",
    ),
    "ads_permission_preview_set_asset_permissions": _partial(
        "preview_catalog_access_change",
        note="Meta's tool can also: Previews sharing a catalog with, or removing it from, a partner business. Why this is open: Not built.",
    ),
    "ads_permission_set_asset_permissions": _partial(
        "set_catalog_access",
        note="Meta's tool can also: Shares a catalog with, or removes it from, a partner business (/{catalog_id}/agencies in Meta's SDK). Why this is open: Not built.",
    ),
    "ads_pixel_event_create": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_pixel_event_delete": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_pixel_event_read": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_pixel_event_update": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_pixel_parameter_create": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_pixel_parameter_delete": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_pixel_parameter_read": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_pixel_parameter_update": _unavailable(
        note="Meta's own server limits pixel event rules and parameter extractors to first-party clients. The public reference marks changes as unsupported, and the live API rejected the documented reads on 2026-11-12. Use Events Manager.",
    ),
    "ads_update_custom_audience": _covered(
        "update_custom_audience",
        note="Same capability.",
    ),
    "ads_update_entity": _partial(
        "update_campaign", "update_adset", "update_ad", "update_ad_creative_enhancements", "set_campaign_status", "set_adset_status", "set_ad_status", "update_campaign_budget", "update_adset_budget", "update_adset_bid_amount", "update_campaign_bid_strategy", "update_adset_bid_strategy",
        note="Meta's tool can also: Campaign spend_cap, start_time and stop_time as named update fields (ours via params on update_campaign). One switch for all default Advantage+ enhancements (advantage_plus_creative_defaults). Discarding Ads Manager drafts. Why this is open: Not built.",
    ),
    "offer_report": _app_internal(
        "Part of Meta's own app, with no capability to match. Offers a scheduled-report button in Meta's app.",
    ),
    # Documented by Meta, but not on its live server.
    "ads_update_custom_audience_users": _covered(
        "add_audience_users", "remove_audience_users",
        note="Meta documents this tool but does not serve it; its live audience tools say membership changes are not supported. Here, pick add or remove. Values are normalized and hashed on this machine; pass file_path so customer data never passes through the model.",
    ),
}


def resolve_official_tool(name: str) -> str | None:
    """Return this server's tool for an official name served by exactly one tool."""
    entry = OFFICIAL_TOOLS.get(name)
    if entry is None or entry.status not in SERVED_STATUSES or len(entry.tools) != 1:
        return None
    return entry.tools[0]


def official_tool_guidance(name: str) -> str | None:
    """Explain an official name that cannot be called as a single tool here."""
    entry = OFFICIAL_TOOLS.get(name)
    if entry is None or resolve_official_tool(name) is not None:
        return None
    if entry.status in SERVED_STATUSES:
        return (
            f"'{name}' is a Meta official tool served here by several tools: "
            f"{', '.join(entry.tools)}. {entry.note}"
        )
    nearest = f" Nearest tool here: {', '.join(entry.tools)}." if entry.tools else ""
    return f"'{name}' is a Meta official tool with no equivalent here. {entry.note}{nearest}"


def parity_summary() -> dict[str, object]:
    """Count entries by status and verification."""
    by_status: dict[str, int] = {
        "covered": 0, "partial": 0, "missing": 0, "unavailable": 0, "app_internal": 0,
    }
    by_verified: dict[str, int] = {"live": 0, "mocked": 0}
    for entry in OFFICIAL_TOOLS.values():
        by_status[entry.status] += 1
        if entry.verified is not None:
            by_verified[entry.verified] += 1
    return {
        "official_tools": len(OFFICIAL_TOOLS),
        "served_by_meta": len(OFFICIAL_TOOLS) - len(DOCUMENTED_NOT_SERVED),
        "by_status": by_status,
        "by_verified": by_verified,
    }
