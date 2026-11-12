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

# Tools run read-only against a real ad account on 2026-11-12, with Meta
# accepting the request. A tool is listed only if every call made in that run
# succeeded.
LIVE_VERIFIED_TOOLS: frozenset[str] = frozenset(
    {
        # Tools that existed before the parity work
        "compare_performance",
        "export_change_history",
        "get_ad_account",
        "get_audience",
        "get_entity_insights",
        "list_ad_accounts",
        "list_ads",
        "list_adsets",
        "list_audiences",
        "list_campaigns",
        "list_change_history",
        "list_creatives",
        "list_instagram_accounts",
        "preview_ad",
        # Pages, media and asset usage
        "get_field_reference",
        "list_ad_images",
        "list_ad_videos",
        "list_audience_adsets",
        "list_creative_ads",
        "list_instagram_media",
        "list_pages",
        # Reporting
        "detect_performance_anomalies",
        "get_advertiser_context",
        "get_delivery_errors",
        "get_opportunity_score",
        "get_performance_trend",
        # Experiments
        "check_ab_test_eligibility",
        "get_ad_study",
        "list_ad_studies",
        # Pixels and datasets
        "get_dataset",
        "get_dataset_quality",
        "get_dataset_stats",
        "list_custom_conversions",
        "list_datasets",
        # Product catalogs
        "build_product_set_filter",
        "get_catalog_diagnostics",
        "get_catalog_dynamic_ads_health",
        "get_catalog_event_source_health",
        "get_catalog_event_source_recommendations",
        "list_catalog_data_sources",
        "list_catalog_event_sources",
        "list_catalog_products",
        "list_catalogs",
        "list_dpa_eligible_catalogs",
        "list_event_source_catalogs",
        "list_feed_rules",
        "list_product_feed_uploads",
        "list_product_feeds",
        "list_product_sets",
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
    "ads_account_get_activity_logs": _covered("list_change_history", "export_change_history",
        note="Covers Meta's tool. Ours also offers: Retrieves every event across pages with stated coverage; control over including child objects.",
    ),
    "ads_activate_entity": _partial("set_campaign_status", "set_adset_status", "set_ad_status",
        note="Meta's tool can also: Publishes staged Ads Manager drafts, all or a chosen subset (object_ids, allow_partial_publish, publish_as_active). For live objects the two are equivalent.",
    ),
    "ads_boost_ig_post": _partial("boost_instagram_post",
        note="Meta's tool can also: Creates campaign, ad set and ad in one call with defaults (about $5/day for 6 days, duration_days, targeting, objective, destination and goal pairing); returns a plan first (confirmed=false); builds Partnership Ads (sponsor_id, ad_code); url_tags. Ours needs an existing ad set.",
    ),
    "ads_catalog_create": _partial("create_catalog", "create_product_feed", "create_product_feed_upload", "create_catalog_product",
        note="Meta's tool can also: One call creates the catalog and loads data from a feed URL with credentials (feed_username, feed_password, FTP/SFTP), inline items (items) or an uploaded file (feed_file_content). Ours cannot upload a file's contents.",
    ),
    "ads_catalog_create_feed_rule": _covered("create_feed_rule",
        note="Creates an attribute mapping or transformation rule on a feed.",
    ),
    "ads_catalog_create_product_feed": _covered("create_product_feed",
        note="Registers a feed, optionally fetched from a URL on a schedule.",
    ),
    "ads_catalog_create_product_feed_upload_session": _covered("create_product_feed_upload",
        note="Covers Meta's tool. Ours also offers: Uploads from any URL, with add-or-update-only mode (update_only); Meta's only re-fetches the configured URL.",
    ),
    "ads_catalog_create_product_set": _covered("create_product_set",
        note="Creates a product set from a filter.",
    ),
    "ads_catalog_delete_product": _partial("delete_catalog_product",
        note="Meta's tool can also: Up to 25 SKUs per call, with the feed-overwrite warning. Ours deletes one product by Meta product ID.",
    ),
    "ads_catalog_event_source_connect": _covered("connect_catalog_event_source",
        note="Connects an event source to a catalog.",
    ),
    "ads_catalog_event_source_disconnect": _covered("disconnect_catalog_event_source",
        note="Disconnects an event source from a catalog.",
    ),
    "ads_catalog_event_source_get": _covered("list_catalog_event_sources",
        note="Pixels, apps and offline datasets connected to a catalog.",
    ),
    "ads_catalog_event_source_get_catalogs": _partial("list_event_source_catalogs",
        note="Meta's tool can also: Compares product match rates across the connected catalogs (include_match_rates, event_name).",
    ),
    "ads_catalog_event_source_get_health": _partial("get_catalog_event_source_health",
        note="Meta's tool can also: Scopes to one source (event_source_id) and reports its match rate across all catalogs next to this catalog's rate.",
    ),
    "ads_catalog_event_source_get_recommendations": _partial("get_catalog_event_source_recommendations",
        note="Meta's tool can also: Meta's computed best source (recommended_pixel_id), including apps and offline datasets. Ours ranks the business's pixels only.",
    ),
    "ads_catalog_get_businesses": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Lists the businesses the user can access, with name search.",
    ),
    "ads_catalog_get_data_sources": _covered("list_catalog_data_sources",
        note="Lists feeds, uploads and API sources for a catalog.",
    ),
    "ads_catalog_get_diagnostics": _covered("get_catalog_diagnostics",
        note="Errors and opportunities ordered by products affected.",
    ),
    "ads_catalog_get_dynamic_ads_health": _partial("get_catalog_dynamic_ads_health",
        note="Meta's tool can also: Meta's named checks (checks) and a format-specific product-count check (ad_creation_use_case).",
    ),
    "ads_catalog_get_feed_rules": _covered("list_feed_rules",
        note="Lists rules on a feed.",
    ),
    "ads_catalog_get_product_feed_upload_sessions": _covered("list_product_feed_uploads",
        note="Uploads with status, counts and errors ordered by rows affected.",
    ),
    "ads_catalog_get_suggested_filter": _partial("build_product_set_filter",
        note="Meta's tool can also: Turns plain words into a filter resolved against the catalog's actual attribute values. Ours needs structured conditions written by the calling model.",
    ),
    "ads_catalog_list_catalogs": _covered("list_catalogs",
        note="Lists catalogs for a business or ad account, or returns one catalog with counts.",
    ),
    "ads_catalog_list_dpa_eligible_catalogs": _partial("list_dpa_eligible_catalogs",
        note="Meta's tool can also: Meta's own eligibility and ranking, tuned by ad_url, pixel_id, page_id, ad_format.",
    ),
    "ads_catalog_list_partner_integrations": _unavailable(
        note="No public API route is known for partner store integrations (Shopify, WooCommerce and similar) and their sync health.",
    ),
    "ads_catalog_list_product_feeds": _covered("list_product_feeds",
        note="Lists feeds, or one feed with settings and latest upload.",
    ),
    "ads_catalog_list_product_sets": _covered("list_product_sets",
        note="Lists sets in a catalog, one set, or the sets containing a product.",
    ),
    "ads_catalog_list_products": _partial("list_catalog_products",
        note="Meta's tool can also: Lists only products hit by a diagnostic (error_type); one row per product group (view=product_groups); ranks by product views (top_products).",
    ),
    "ads_catalog_product_create": _partial("create_catalog_product",
        note="Meta's tool can also: Up to 25 items per call with per-item results and product videos (video_urls). Ours creates one per call.",
    ),
    "ads_catalog_product_feed_delete": _covered("delete_product_feed",
        note="Deletes a feed; its products are removed unless another source supplies them.",
    ),
    "ads_catalog_product_feed_delete_rule": _covered("delete_feed_rule",
        note="Deletes a feed rule; the feed and products are unaffected.",
    ),
    "ads_catalog_product_set_delete": _covered("delete_product_set",
        note="Deletes a product set; products remain.",
    ),
    "ads_catalog_update_catalog": _partial("update_catalog",
        note="Meta's tool can also: Sets up, test-runs and saves the checkout URL (setup_checkout_url, checkout_base_url, checkout_url_test_only, checkout_validation_result_id).",
    ),
    "ads_catalog_update_feed_rule": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Edits a feed rule's parameters in place.",
    ),
    "ads_catalog_update_product": _partial("update_catalog_product",
        note="Meta's tool can also: Up to 25 items per call by SKU (retailer_id), product videos, and a warning when a feed will overwrite the edit (SOURCE_MAY_OVERWRITE). Ours updates one product by Meta product ID.",
    ),
    "ads_catalog_update_product_feed": _partial("update_product_feed",
        note="Meta's tool can also: Stops a scheduled fetch without deleting the feed (clear_replace_schedule, clear_update_schedule); named parsing options (ours via params).",
    ),
    "ads_catalog_update_product_set": _covered("update_product_set",
        note="Edits a product set with before and after values.",
    ),
    "ads_create_ad": _partial("create_ad",
        note="Meta's tool can also: Inline creative spec in creative; named source_ad_id, conversion_domain, adlabels, ad_schedule_start_time/ad_schedule_end_time. Ours accepts only a creative ID in creative (meta_ads_mcp/tools/ads.py:175) and the rest via params.",
    ),
    "ads_create_ad_set": _partial("create_ad_set",
        note="Meta's tool can also: About 90 named fields, including frequency_control_specs, attribution_spec, adset_schedule (day-parting), spend caps, source_adset_id, and dsa_beneficiary/dsa_payor (auto-filled for EU targeting). Ours reaches all of these via params, with money in minor units there. Meta also checks the goal against the objective and fills in the messaging Page.",
    ),
    "ads_create_campaign": _partial("create_campaign",
        note="Meta's tool can also: Named source_campaign_id, campaign_spend_cap, start and stop times, promoted_object, special_ad_category_country, budget_schedule_specs, adlabels (ours via params). Stages into Ads Manager drafts. Later rejects ad set budgets under a campaign budget.",
    ),
    "ads_create_creative": _partial("create_ad_creative", "upload_creative_asset",
        note="Meta's tool can also: Builds ten formats from named inputs and checks the combination: static carousel (cards), 1–5 text variants (primary_texts, headlines, descriptions), per-placement media (placement_customizations), asset pool (multi_media), catalog carousel (product_set_id), boosted Page post (object_story_id), Instagram post (source_instagram_media_id), Facebook and Instagram Partnership Ads, click-to-message buttons (call_to_action_type), image download from image_url, AI-content disclosure (self_ai_disclosure), Advantage+ defaults. Ours takes raw object_story_spec/asset_feed_spec.",
    ),
    "ads_create_custom_audience": _covered("create_custom_audience", "create_lookalike_audience",
        note="Use create_lookalike_audience for lookalikes and create_custom_audience for every other subtype.",
    ),
    "ads_creative_delete": _covered("delete_creative",
        note="Same capability.",
    ),
    "ads_creative_update": _partial("update_creative",
        note="Meta's tool can also: Ad labels with set, add or remove (adlabels, adlabels_operation); ours via params, replace only.",
    ),
    "ads_creative_upload_local_image": _app_internal(
        "Part of Meta's own app, with no capability to match. Starts an upload from Meta's in-chat upload app. The capability behind it (upload a local file) is covered for images by upload_creative_asset.",
    ),
    "ads_creative_upload_media": _partial("upload_creative_asset",
        note="Meta's tool can also: Uploads videos from a URL (media_type=VIDEO); records AI provenance (media_creation_source). Ours uploads images only.",
    ),
    "ads_delete_custom_audience": _covered("delete_audience",
        note="Deletes a custom audience.",
    ),
    "ads_delete_local_ad_image": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Deletes an image from the account library by hash.",
    ),
    "ads_experiment_abtest_create_test": _partial("create_ab_test", "setup_ab_test",
        note="Meta's tool can also: Ad-level (creative) tests; winning metric (primary_kpi) and extra reported metrics (secondary_kpis); test budget share (budget_percentage). Ours accepts campaign or ad set cells only.",
    ),
    "ads_experiment_abtest_get_test": _covered("get_ad_study",
        note="Pass include_results=true for the winner and confidence.",
    ),
    "ads_experiment_abtest_update_test": _covered("update_ad_study", "cancel_ad_study",
        note="Use update_ad_study to edit and cancel_ad_study to end or cancel.",
    ),
    "ads_experiment_check_eligibility": _partial("check_ab_test_eligibility",
        note="Meta's tool can also: Meta's verdict, including Lift study eligibility for the account (auto-picks the top 10 campaigns by spend). Ours is a local A/B pre-check only.",
    ),
    "ads_experiment_lift_create_test": _covered("create_lift_study",
        note="Creates a conversion lift study on the owning business.",
    ),
    "ads_experiment_lift_get_test": _covered("get_ad_study",
        note="Pass include_results=true for lift and confidence.",
    ),
    "ads_experiment_list_tests": _covered("list_ad_studies",
        note="A/B tests and lift studies on an account, business or one entity.",
    ),
    "ads_finalize_local_ad_image_upload": _app_internal(
        "Part of Meta's own app, with no capability to match. Finishes that upload.",
    ),
    "ads_get_ad_account_custom_audiences": _covered("list_audiences",
        note="Lists custom audiences, optionally by subtype.",
    ),
    "ads_get_ad_account_pages": _covered("list_pages",
        note="Call list_pages with source='ad_account' and account_id.",
    ),
    "ads_get_ad_accounts": _covered("list_ad_accounts", "get_ad_account",
        note="Lists ad accounts the token can access.",
    ),
    "ads_get_ad_entities": _partial("list_campaigns", "list_adsets", "list_ads", "get_entity_insights", "compare_performance",
        note="Meta's tool can also: Sorts entities server-side by a metric (sort, e.g. impressions_descending) and returns settings and metrics in one row. Reads unpublished Ads Manager drafts (object_state=draft). Adds benchmark and incrementality context (include_additional_context) and extra metrics by purpose (reporting_purpose).",
    ),
    "ads_get_ad_images": _covered("list_ad_images",
        note="Lists the account's image library.",
    ),
    "ads_get_ad_preview": _covered("preview_ad",
        note="Covers Meta's tool. Ours also offers: Previews an unsaved creative spec (creative).",
    ),
    "ads_get_ad_preview_screenshot": _app_internal(
        "Part of Meta's own app, with no capability to match. Screenshot sink for Meta's preview card (\"not intended for model use\").",
    ),
    "ads_get_ad_videos": _partial("list_ad_videos",
        note="Meta's tool can also: Looks up videos by ID (video_ids) or title. Ours only lists the library; the HD source URL is reachable via fields.",
    ),
    "ads_get_async_result": _app_internal(
        "Part of Meta's own app, with no capability to match. Collects results of background jobs: MAA_RESPONSE (an answer from a Meta agent) and IMAGE_VARIATIONS (generated image variations with hashes). No served tool starts these jobs, so the capability behind them cannot be assessed.",
    ),
    "ads_get_creative_ads": _covered("list_creative_ads",
        note="Meta has no server-side filter for this, so the tool scans the account's ads and reports how many were examined and whether the scan was complete.",
    ),
    "ads_get_creatives": _covered("list_creatives", "get_creative",
        note="Lists creatives in an ad account.",
    ),
    "ads_get_custom_audience": _covered("get_audience",
        note="Returns one custom or lookalike audience.",
    ),
    "ads_get_custom_audience_adsets": _partial("list_audience_adsets",
        note="Meta's tool can also: Finds ad sets that target the audience. Ours works back from the ads that use it, so an ad set with no ads is missed, which matters before deleting an audience.",
    ),
    "ads_get_customconversions": _covered("list_custom_conversions",
        note="Custom conversions on an ad account.",
    ),
    "ads_get_dataset_details": _covered("get_dataset",
        note="One dataset's settings.",
    ),
    "ads_get_dataset_quality": _partial("get_dataset_quality",
        note="Meta's tool can also: Returns offline, CRM and custom-attribution channels as well as web (query_type). Ours reads web only (meta_ads_mcp/tools/datasets.py:386).",
    ),
    "ads_get_dataset_stats": _covered("get_dataset_stats",
        note="Event volume for up to 28 days, split by event, device or source.",
    ),
    "ads_get_datasets": _covered("list_datasets",
        note="Pixels and datasets on an ad account or business.",
    ),
    "ads_get_errors": _covered("get_delivery_errors",
        note="Covers Meta's tool. Ours also offers: Account status and disable reason, ad review feedback, ordering by severity and spend. Meta's excludes ad rejections and account problems.",
    ),
    "ads_get_field_context": _partial("get_field_reference",
        note="Meta's tool can also: Covers metrics as well as settings, with display names, aliases (spend to amount_spent), filter operators, sortability and descriptions. Ours covers campaign, ad set and ad settings only, with no metrics and no descriptions.",
    ),
    "ads_get_help_article": _unavailable(
        note="Meta has no public API for searching the Business Help Center. Search https://www.facebook.com/business/help directly.",
    ),
    "ads_get_ig_accounts": _covered("list_instagram_accounts",
        note="Lists Instagram accounts linked to an ad account.",
    ),
    "ads_get_ig_media": _covered("list_instagram_media",
        note="Lists posts and reels, or stories with source='stories'. Only boost-eligible items by default; each item says whether it can be boosted.",
    ),
    "ads_get_opportunity_score": _covered("get_opportunity_score",
        note="Covers Meta's tool. Ours also offers: Daily score history up to 45 days and per-day change reasons (include_history_reasons).",
    ),
    "ads_get_pages_for_business": _covered("list_pages",
        note="Call list_pages with source='business' and business_id.",
    ),
    "ads_get_user_pages": _covered("list_pages",
        note="Call list_pages with source='user'.",
    ),
    "ads_insights_advertiser_context": _partial("get_advertiser_context",
        note="Meta's tool can also: Scopes to named campaigns, ad sets or ads (entity_ids) and an explicit window (date_preset, date_from/date_to). Its content is Meta's own summary; the one-line description does not allow a content comparison.",
    ),
    "ads_insights_anomaly_signal": _partial("detect_performance_anomalies",
        note="Meta's tool can also: Scans the whole account or a list of entities in one call (entity_ids) with Meta's own detector.",
    ),
    "ads_insights_auction_ranking_benchmarks": _partial("get_auction_ranking_report", "detect_auction_overlap",
        note="Meta's tool can also: Takes a list of entities (entity_ids). The description suggests auction data beyond the public quality rankings; cannot confirm.",
    ),
    "ads_insights_industry_benchmark": _unavailable("get_auction_ranking_report",
        note="Meta's public API has no endpoint for benchmarks from similar advertisers. get_auction_ranking_report uses Meta's public ranking fields, which compare each ad with ads competing for the same audience.",
    ),
    "ads_insights_performance_trend": _partial("get_performance_trend",
        note="Meta's tool can also: Several entities per call (entity_ids) grouped by level (analysis_level); REACH as a metric. Cost per result follows each ad set's own result; ours counts only purchase or lead actions (RESULTS_DEFINITION, meta_ads_mcp/tools/performance_signals.py:55).",
    ),
    "ads_insights_performance_vs_peers": _unavailable("get_auction_ranking_report",
        note="Meta's public API has no endpoint for percentile comparisons against similar advertisers. get_auction_ranking_report is the nearest public signal.",
    ),
    "ads_library_search": _partial("search_ads_archive",
        note="Meta's tool can also: Searches by Page (page_ids) and by active status (ad_active_status); keywords optional. Ours requires keywords and countries.",
    ),
    "ads_log_ui_interaction": _app_internal(
        "Part of Meta's own app, with no capability to match. Telemetry from Meta's UI widgets.",
    ),
    "ads_permission_list_assigned_assets": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Lists the catalogs assigned to a person, system user or partner business.",
    ),
    "ads_permission_list_assigned_users": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Lists who has access to a catalog and with what permissions; diagnoses one person's access (access_status: not on business, no permissions, two-factor required).",
    ),
    "ads_permission_list_business_users": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Lists a business's people, system users or partner businesses, with name and email search.",
    ),
    "ads_permission_preview_set_asset_permissions": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Read-only preview of a catalog access change (previous and proposed permissions, warnings).",
    ),
    "ads_permission_set_asset_permissions": _missing(
        note="Not built yet; Meta's public API appears to allow it. Meta's tool: Grants, changes or removes catalog access for a person or partner business.",
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
    "ads_update_custom_audience": _partial("update_custom_audience",
        note="Meta's tool can also: Edits a website audience's rule; ours via params.",
    ),
    "ads_update_entity": _partial("update_campaign", "update_adset", "update_ad", "update_campaign_budget", "update_adset_budget", "update_adset_bid_amount", "update_campaign_bid_strategy", "update_adset_bid_strategy",
        note="Meta's tool can also: Turns Advantage+ creative enhancements on or off on a live ad (advantage_plus_creative_defaults, _features, _opt_out_features). Archives (status=ARCHIVED). Changes ad set spend caps and minimum spend targets. Discards staged draft edits with a preview and confirmation_token. Ours blocks ad set spend caps (campaigns.py:286-319) and allows only ACTIVE or PAUSED on ad sets and ads (campaigns.py:292, ads.py:40).",
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
