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
        note="Meta's tool can also: In one step: App Promotion; messaging destinations (Instagram Direct, Messenger, WhatsApp) with their message buttons; instant forms (ON_AD) and calls (PHONE_CALL); a choice of optimization goal, billing event, bid strategy and bid cap; lifetime or campaign-level budget; start time; special ad categories; buying type; the Page found from the Instagram account. Ours in one step covers Engagement, Awareness, Traffic (profile or website), and Sales and Leads (website with a pixel). It fixes billing, bidding and empty special ad categories (creatives.py:954-978, :1135-1152) and requires page_id (:1129). All of these are reachable in several calls: create_campaign, then create_ad_set, then boost_instagram_post with adset_id. On purpose, ours sets no default budget or targeting (:1120-1124). Now built: partnership_ad_code, destination_type, promoted_object and three more objectives (:1265-1267, :954-972). Why this is open: Not built.",
    ),
    "ads_catalog_create": _partial(
        "create_catalog", "create_product_feed", "create_product_feed_upload", "batch_update_catalog_products",
        note="Meta's tool can also: An SFTP feed URL. Ours accepts http, https and ftp for schedules (SCHEDULE_URL_SCHEMES, catalog_feeds.py:131) and http or https for one-off uploads (:1099). Convenience: one call instead of several. Built differently: the feed file inline as base64; ours reads it from the upload folder so its rows do not pass through the conversation (catalog_feeds.py:1068-1077). Why this is open: Not built.",
    ),
    "ads_catalog_create_feed_rule": _covered(
        "create_feed_rule",
        note="Creates an attribute mapping or transformation rule on a feed.",
    ),
    "ads_catalog_create_product_feed": _partial(
        "create_product_feed",
        note="Meta's tool can also: An SFTP schedule URL (catalog_feeds.py:131). feed_type and the parsing options are now named (catalog_feeds.py:918-937). Why this is open: Not built.",
    ),
    "ads_catalog_create_product_feed_upload_session": _covered(
        "create_product_feed_upload",
        note="Covers Meta's tool. Ours also offers: Uploads from any URL, with add-or-update-only mode (update_only); Meta's only re-fetches the configured URL.",
    ),
    "ads_catalog_create_product_set": _partial(
        "create_product_set",
        note="Meta's tool can also: Named retailer_id on creation; ours reaches it only through params (catalogs.py:2383-2393), although update_product_set names it (:2403). Found in this pass; code unchanged. Why this is open: Not built.",
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
        note="Meta's tool can also: Meta's computed best source (recommended_pixel_id), which also weighs apps and offline datasets as well as pixels. Why this is open: No public API route known.",
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
        note="Meta's tool can also: Meta's named checks (checks, with_issue_only) and format-specific product counts (ad_creation_use_case). Ours combines diagnostics, product counts and event sources, and states that Meta has no single catalog-ads health endpoint (catalogs.py:1246). Convenience: takes a product set id alone; ours requires catalog_id (:1149). Why this is open: No public API route known.",
    ),
    "ads_catalog_get_feed_rules": _covered(
        "list_feed_rules",
        note="Lists rules on a feed.",
    ),
    "ads_catalog_get_product_feed_upload_sessions": _partial(
        "list_product_feed_uploads",
        note="Meta's tool can also: One sample invalid value per error (include_samples), and paging through errors (error_limit, error_cursor). Ours reads the first 100 errors of an upload, without samples (UPLOAD_ERROR_FIELDS, catalog_feeds.py:44; :649). The second pass listed both but marked the row equivalent. Why this is open: Not built.",
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
        note="Meta's tool can also: Meta's own eligible-catalog set and recommendation. Ours ranks from documented catalog data and states that Meta publishes no eligibility endpoint (catalogs.py:1022). page_id and ad_format are now built (catalogs.py:827-828, :839-845). Why this is open: No public API route known.",
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
        note="Meta's tool can also: An SFTP schedule URL (catalog_feeds.py:131). Stops a fetch schedule without deleting the feed (clear_replace_schedule, clear_update_schedule); our docstring says Meta's public API documents no way to clear a schedule (catalog_feeds.py:987), so that part has no public API route known. Convenience: changes a schedule's timing without restating its URL; ours requires the URL (:509-512). The parsing options are now named (:975-987). Why this is open: Not built.",
    ),
    "ads_catalog_update_product_set": _covered(
        "update_product_set",
        note="Edits a product set with before and after values.",
    ),
    "ads_create_ad": _partial(
        "create_ad", "duplicate_ad",
        note="Meta's tool can also: Named display_sequence and engagement_audience; ours reaches them only through params (ads.py:167-183). Convenience: adset_spec creates the ad set in the same call. Drafts (no public API route known). ad_schedule_start_time and ad_schedule_end_time are now named (ads.py:181-182). Why this is open: Not built.",
    ),
    "ads_create_ad_set": _partial(
        "create_ad_set", "duplicate_adset",
        note="Meta's tool can also: 66 further named fields. source_adset_id is served by duplicate_adset; the other 65 reach Meta only through params (campaigns.py:542-575). Those with a plain advertiser use: existing_customer_budget_percentage, multi_optimization_goal_weight, is_incremental_attribution_enabled, campaign_attribution, tune_for_category, cost_bidding_mode, conversion_locations, value rules (value_rules_spec, value_rule_set_id, conversion_value_expression_spec), placement_soft_opt_out, creative_sequence, time-based ad rotation (time_based_ad_rotation_id_blocks, time_based_ad_rotation_intervals), rf_prediction_id, daily_imps and lifetime_imps, min_budget_spend_percentage and max_budget_spend_percentage, saved_audience_id, brand_safety_config, calling_settings, marketing_goal. The rest look like Meta's internal fields (creative diversity scores, guidance estimates, metrics metadata). Also ABSOLUTE budget schedules (campaigns.py:136). Convenience: fills in the EU DSA beneficiary and payor from the business name, and the messaging Page. Drafts (no public API route known). destination_type, pacing_type, is_dynamic_creative, budget_schedule_specs and optimization_sub_event are now named (:570-574). Why this is open: Not built.",
    ),
    "ads_create_campaign": _partial(
        "create_campaign", "duplicate_campaign",
        note="Meta's tool can also: Named is_skadnetwork_attribution, campaign_optimization_type (NONE, ICO_ONLY), is_using_l3_schedule, iterative_split_test_configs and topline_id; ours reaches them only through params (campaigns.py:254-273). Fixed-amount (ABSOLUTE) budget schedules, which ours refuses until Meta's unit is confirmed (campaigns.py:136, :207-214). Staging into Ads Manager drafts (no public API route known). budget_schedule_specs with MULTIPLIER is now named (:272). Why this is open: Not built.",
    ),
    "ads_create_creative": _partial(
        "create_ad_creative_from_inputs", "create_ad_creative", "upload_creative_asset",
        note="Meta's tool can also: Click-to-message static carousels, which ours refuses (creative_formats.py:388, :402). Click-to-message buttons on an existing Instagram post (refused, :619). Instagram Partnership Ads from new media as a carousel; ours builds them from a single image or video only (:857-861). Images and videos mixed in one placement customization (refused, :517-521). display_link, Meta's caption field, which ours lacks (:898-929). Requesting Product Extensions at creation (objective, conversion_location, product_extensions_product_set_id). AI-content disclosure self_ai_disclosure (no public API route known). Conveniences: Advantage+ switches on the builder (ours: raw degrees_of_freedom_spec on create_ad_creative, creatives.py:134, or update_ad_creative_enhancements afterwards); image_url downloaded into the account (ours sends the URL, creative_formats.py:149-153, or uploads it first with upload_creative_asset); default thumbnails for video cards and pool videos. Asset pools, catalog carousels and Facebook Partnership Ads are now built (:926-928, :662-830). Why this is open: Not built.",
    ),
    "ads_create_custom_audience": _covered(
        "create_custom_audience", "create_lookalike_audience",
        note="Same capability.",
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
    "ads_creative_upload_media": _covered(
        "upload_ad_video", "upload_creative_asset",
        note="Same capability.",
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
        note="Meta's tool can also: Chooses the metric that decides the winner (primary_kpi, secondary_kpis). Our code states that Meta's documented API offers no such choice (experiments.py:1482, :1540); this has not been checked independently. Why this is open: No public API route known.",
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
        note="Meta's tool can also: Meta's own verdict, including Lift study eligibility, and an account-level check with no ids (over the top campaigns by spend). Ours is a local pre-check and states that Meta has no public eligibility endpoint (experiments.py:1307-1315). Ad ids are now accepted (level=ad, :1318). Why this is open: No public API route known.",
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
        note="Meta's tool can also: Reads unpublished Ads Manager drafts (object_state=draft). Adds benchmark and incrementality context (include_additional_context). Convenience: settings and metrics in one row; ours uses the list tools for settings and get_entity_insights (insights.py:812) for metrics. Why this is open: No public API route known.",
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
        note="Meta's tool can also: CRM and custom-attribution channels (query_type); ours covers web and offline (DATASET_QUALITY_CHANNELS, datasets.py:104). Why this is open: No public API route known.",
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
        note="Meta's tool can also: Field descriptions, display names, aliases (spend to amount_spent), and which levels support each metric. Ours (level=metric, assets.py:1008) gives types, sort syntax and filter operators. Why this is open: Built differently: SDK-derived types, no descriptions or aliases.",
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
        note="Meta's tool can also: A lifetime window: ours takes lookback_days 7 to 90, or since and until covering at most 366 days (performance_signals.py:3318-3330, :358). Meta's summary is its own; ours is derived from account data, and the two cannot be compared from code. Explicit dates and ad ids are now built (performance_signals.py:3337-3345). Why this is open: Built differently: windows of at most 366 days, summary derived from account data.",
    ),
    "ads_insights_anomaly_signal": _partial(
        "detect_performance_anomalies",
        note="Meta's tool can also: Meta's own detector, over entity lists of unstated size. Ours applies its stated median rule to at most 10 entities per call (MAX_ENTITIES, performance_signals.py:955; tool at :1835). Why this is open: Built differently: stated rule, up to 10 entities per call.",
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
        note="Meta's tool can also: Trends over the full available history; ours covers at most 366 days per call (performance_signals.py:42, :358). Convenience: entity lists of unstated size; ours takes up to 10 (:955). Reach as a metric and a period series for each entity are now built (:1401-1403). Why this is open: Built differently: at most 366 days and 10 entities per call.",
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
        "list_user_assigned_catalogs", "list_catalog_partners",
        note="Meta's tool can also: In the partner view, catalogs shared with the partner through a Business Asset Group. Ours checks each owned catalog's direct partner list (business_access.py:505-530). Conveniences: each catalog's permissions in the same call (ours: list_catalog_partners for each catalog, business_access.py:946); name search. Why this is open: Built differently: direct partner shares, read catalog by catalog.",
    ),
    "ads_permission_list_assigned_users": _partial(
        "list_catalog_assigned_users", "list_catalog_partners",
        note="Meta's tool can also: Explains why a person lacks access: not on the business, the business lacks the asset, no permissions, or two-factor required. Counts access through Business Asset Groups and business roles. Partner checks report pending requests. Ours lists direct assignments only (business_access.py:404). Convenience: name search. Partner rows with their tasks are now built (business_access.py:946-953). Why this is open: Built differently: direct assignments only.",
    ),
    "ads_permission_list_business_users": _covered(
        "list_business_users",
        note="Same capability.",
    ),
    "ads_permission_preview_set_asset_permissions": _covered(
        "preview_catalog_access_change",
        note="Same capability.",
    ),
    "ads_permission_set_asset_permissions": _covered(
        "set_catalog_access",
        note="Same capability.",
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
    "ads_update_custom_audience": _partial(
        "update_custom_audience",
        note="Meta's tool can also: Named audience_labels on update; ours reaches it only through params (audiences.py:290-298). Found in this pass; the code is unchanged since the second pass. Why this is open: Not built.",
    ),
    "ads_update_entity": _partial(
        "update_campaign", "update_adset", "update_ad", "update_ad_creative_enhancements", "set_campaign_status", "set_adset_status", "set_ad_status", "update_campaign_budget", "update_adset_budget", "update_adset_bid_amount", "update_campaign_bid_strategy", "update_adset_bid_strategy", "delete_campaign", "delete_adset", "delete_ad",
        note="Meta's tool can also: Discards Ads Manager draft changes. Convenience: one switch (advantage_plus_creative_defaults=OFF) turns off whichever default Advantage+ enhancements apply to the ad; ours names features one by one (creative_formats.py:1094-1114). Now named on ours: campaign spend_cap, start_time and stop_time, and labels on campaign, ad set and ad updates (campaigns.py:380-383, :795, ads.py:420). Why this is open: No public API route known.",
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
