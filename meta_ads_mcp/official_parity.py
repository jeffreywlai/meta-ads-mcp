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
        "get_adset",
        "get_advertiser_context",
        "get_auction_ranking_report",
        "get_audience",
        "get_campaign",
        "get_catalog_diagnostics",
        "get_catalog_dynamic_ads_health",
        "get_catalog_event_source_health",
        "get_catalog_event_source_recommendations",
        "get_creative",
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

# Write tools run on 2026-11-13 against a throwaway ad account (everything created
# paused and then deleted; nothing spent) and a test catalog, with Meta accepting
# the request and a read-back agreeing where one was possible. Not here:
# update_ad_creative_enhancements (Meta accepted its copy of an existing-post
# creative and refused its copy of image, video and asset-pool creatives),
# delete_feed_rule (Meta answers that the endpoint is retired), and the tools not
# exercised.
LIVE_VERIFIED_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "add_audience_users",
        "batch_update_catalog_products",
        "boost_instagram_post",
        "cancel_ad_study",
        "create_ab_test",
        "create_ad",
        "create_ad_creative_from_inputs",
        "create_ad_set",
        "create_campaign",
        "create_catalog",
        "create_catalog_product",
        "create_custom_audience",
        "create_feed_rule",
        "create_product_feed",
        "create_product_feed_upload",
        "create_product_set",
        "delete_ad",
        "delete_ad_image",
        "delete_adset",
        "delete_audience",
        "delete_campaign",
        "delete_catalog_product",
        "delete_creative",
        "delete_product_feed",
        "delete_product_set",
        "duplicate_ad",
        "duplicate_adset",
        "duplicate_campaign",
        "remove_audience_users",
        "set_ad_status",
        "set_adset_status",
        "set_campaign_status",
        "update_ad",
        "update_ad_creative",
        "update_ad_study",
        "update_adset",
        "update_adset_bid_amount",
        "update_adset_bid_strategy",
        "update_adset_budget",
        "update_campaign",
        "update_campaign_bid_strategy",
        "update_campaign_budget",
        "update_catalog",
        "update_catalog_product",
        "update_creative",
        "update_custom_audience",
        "update_feed_rule",
        "update_product_feed",
        "update_product_set",
        "upload_ad_video",
        "upload_creative_asset",
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
        verified = LIVE_VERIFIED_TOOLS | LIVE_VERIFIED_WRITE_TOOLS
        return "live" if all(tool in verified for tool in self.tools) else "mocked"

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
        note="Meta's tool can also: Publishes Ads Manager drafts, whole or in part (object_ids, allow_partial_publish, publish_as_active). Activating live objects is covered (execution.py:53, :59, :65). Why this is open: No public API route known.",
    ),
    "ads_boost_ig_post": _partial(
        "boost_instagram_post", "create_ad_creative_from_inputs",
        note="Meta's tool can also: In one step: buying_type RESERVED, which ours refuses because it needs a reach and frequency prediction, and points to create_campaign, create_ad_set and adset_id (creatives.py:1552-1559). Objective, destination and goal pairs that Meta's description names and ours does not offer in one step: engagement on an Instagram profile (PROFILE_AND_PAGE_ENGAGEMENT), leads in Messenger (LEAD_GENERATION), awareness with a website or messaging destination, and the profile-visit goal PROFILE_VISIT for Partnership Ads (ours: :1183-1215). The billing events POST_ENGAGEMENT and VIDEO_VIEWS, which a search of Meta's billing table found are not documented as valid; ours allows IMPRESSIONS, plus LINK_CLICKS, THRUPLAY or two-second continuous video views for the matching goals. Each is reachable in several calls: create_campaign, create_ad_set, then boost_instagram_post with adset_id. Kept on purpose: ours sets no default budget or targeting (:1499-1513). Convenience: separate campaign, ad set and ad names; ours derives all three from name (:1750, :1762, :1784-1785). Built since the third pass, in one step: app promotion, messaging destinations with their buttons, instant forms (ON_AD), calls (PHONE_CALL), a choice of goal, billing event, bid strategy and bid cap, lifetime or campaign budget, start and end time, special ad categories, and the Page found from the Instagram account (:1183-1215, :1480-1593, :1596-1647, parameters at :1925-1935). Why this is open: Built differently: RESERVED buying only through adset_id [the other pairs and billing events: no public API route known, by our code's note that it offers only what Meta's pages show, creatives.py:1162-1176].",
    ),
    "ads_catalog_create": _partial(
        "create_catalog", "create_product_feed", "create_product_feed_upload", "batch_update_catalog_products",
        note="Meta's tool can also: An immediate fetch from an FTP address when the catalog is created: ours fetches FTP only on a schedule (SCHEDULE_URL_SCHEMES, catalog_feeds.py:148) and starts a one-time upload only from an http or https address (:1263-1266). An SFTP address [no public API route known]. Built differently: the feed file inline as base64; ours reads it from the upload folder so its rows do not pass through the conversation (catalog_feeds.py:1218-1229, tool at :1233). Convenience: one call instead of several. Why this is open: No public API route known: Meta documents an FTP address only for a feed schedule, not for a one-time upload, and does not document SFTP [inline base64 file: built differently].",
    ),
    "ads_catalog_create_feed_rule": _covered(
        "create_feed_rule",
        note="Creates an attribute mapping or transformation rule on a feed.",
    ),
    "ads_catalog_create_product_feed": _partial(
        "create_product_feed",
        note="Meta's tool can also: An SFTP schedule address; ours accepts http, https and ftp (SCHEDULE_URL_SCHEMES, catalog_feeds.py:148). feed_type, the parsing options and supplementary feed fields are named (:1083-1102). Why this is open: No public API route known.",
    ),
    "ads_catalog_create_product_feed_upload_session": _partial(
        "create_product_feed_upload",
        note="Meta's tool can also: An immediate refresh of a feed whose source is an FTP address: ours starts a one-time upload only from an http or https address or a local file (catalog_feeds.py:1244-1249, :1263-1266). An SFTP source [no public API route known]. Convenience: re-fetches the configured source without restating its address and login; ours needs them each time. Ours also takes any http or https address with a basic-auth login, add-or-update-only mode and a local file, which Meta's does not (:1233-1242). Why this is open: No public API route known: Meta documents an FTP address only for a feed schedule, not for a one-time upload, and does not document SFTP.",
    ),
    "ads_catalog_create_product_set": _covered(
        "create_product_set",
        note="Same capability.",
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
        note="Meta's tool can also: Meta's computed best source (recommended_pixel_id), which also weighs apps and offline datasets. Ours ranks the business's unconnected pixels with evidence (catalog_feeds.py:1835-1840). Why this is open: No public API route known.",
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
        note="Meta's tool can also: Meta's named checks (checks, with_issue_only) and format-specific product counts (ad_creation_use_case). Ours combines diagnostics, product counts and event sources, and states that Meta has no single catalog-ads health endpoint (catalogs.py:1213, :1307). Convenience: takes a product set id alone; ours requires catalog_id (:1209-1212). Why this is open: No public API route known.",
    ),
    "ads_catalog_get_feed_rules": _covered(
        "list_feed_rules",
        note="Lists rules on a feed.",
    ),
    "ads_catalog_get_product_feed_upload_sessions": _partial(
        "list_product_feed_uploads",
        note="Meta's tool can also: A copy of the invalid value in each error sample. Ours returns one sample per error with its row number and the item's retailer id, and our code notes that Meta's public API gives no copy of the value (catalog_feeds.py:53-57, docstring :1325). Paging through errors is now built, up to 1000 per upload with error_limit and error_after (:60, :1316-1338). Meta's remediation advice is its own text. Why this is open: No public API route known.",
    ),
    "ads_catalog_get_suggested_filter": _partial(
        "list_catalog_attribute_values", "build_product_set_filter",
        note="Meta's tool can also: Turns a plain-language description into a filter. Ours lists the values that occur for an attribute (catalogs.py:2524-2532) and builds and tests a filter from named conditions (:2455). Why this is open: Built differently: lists actual values for the caller to match.",
    ),
    "ads_catalog_list_catalogs": _covered(
        "list_catalogs", "list_businesses",
        note="Lists catalogs for a business or ad account, or returns one catalog with counts.",
    ),
    "ads_catalog_list_dpa_eligible_catalogs": _partial(
        "list_dpa_eligible_catalogs",
        note="Meta's tool can also: Meta's own eligible-catalog set and recommendation. Ours ranks from documented catalog data and states that Meta publishes no eligibility endpoint (catalogs.py:888, :1081-1082). ad_url, pixel_id, page_id and ad_format are built (:880-887). Why this is open: No public API route known.",
    ),
    "ads_catalog_list_partner_integrations": _unavailable(
        note="No public API route is known for partner store integrations (Shopify, WooCommerce and similar) and their sync health.",
    ),
    "ads_catalog_list_product_feeds": _partial(
        "list_product_feeds",
        note="Meta's tool can also: Reads a feed's feed_type and override_value, which ours does not request (FEED_FIELDS, catalog_feeds.py:70-90); the owner's search found no public route for reading them. supplementary_feeds is now read when Meta allows it (:64-69, :1060-1078). Convenience: field selection. Why this is open: No public API route known.",
    ),
    "ads_catalog_list_product_sets": _covered(
        "list_product_sets",
        note="Lists sets in a catalog, one set, or the sets containing a product.",
    ),
    "ads_catalog_list_products": _partial(
        "list_catalog_products", "list_catalog_attribute_values",
        note="Meta's tool can also: Ranks products by product views (top_products). Ours lists, filters and reads diagnostics per product (catalogs.py:1424-1436). Why this is open: No public API route known.",
    ),
    "ads_catalog_product_create": _covered(
        "batch_update_catalog_products", "create_catalog_product",
        note="Same capability.",
    ),
    "ads_catalog_product_feed_delete": _covered(
        "delete_product_feed",
        note="Deletes a feed; its products are removed unless another source supplies them.",
    ),
    "ads_catalog_product_feed_delete_rule": _unavailable(
        "delete_feed_rule",
        note="Meta's reference documents DELETE on a feed rule, but the live API (v26.0, tested 2026-11-13) answers that the endpoint is deprecated (code 100, subcode 33). delete_feed_rule sends the documented request and, on that answer, says the rule was not deleted and points to update_feed_rule, delete_product_feed and Commerce Manager.",
    ),
    "ads_catalog_product_set_delete": _covered(
        "delete_product_set",
        note="Deletes a product set; products remain.",
    ),
    "ads_catalog_update_catalog": _partial(
        "update_catalog",
        note="Meta's tool can also: Sets up, test-runs and saves the checkout URL (checkout_base_url, checkout_url_test_only) (spec :41). Ours renames, or sends other settings through params (catalogs.py:1361-1366). Why this is open: No public API route known.",
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
        note="Meta's tool can also: An SFTP schedule address (catalog_feeds.py:148). Stops a fetch schedule without deleting the feed (clear_replace_schedule, clear_update_schedule); our docstring says Meta's public API documents no way to clear one (:1152). Changes a schedule's timing without restating its URL; ours requires the URL (:513, :526-529). All three are on the owner's list of capabilities searched for and not found. Why this is open: No public API route known.",
    ),
    "ads_catalog_update_product_set": _covered(
        "update_product_set",
        note="Edits a product set with before and after values.",
    ),
    "ads_create_ad": _partial(
        "create_ad", "duplicate_ad",
        note="Meta's tool can also: Stages the ad into the Ads Manager draft when the account is eligible (spec :39-40). Convenience: adset_spec creates the ad set in the same call. Every other field of Meta's tool is named on ours; display_sequence and engagement_audience were added (ads.py:187-188, checked at :199-202). Why this is open: No public API route known.",
    ),
    "ads_create_ad_set": _partial(
        "create_ad_set", "duplicate_adset",
        note="Meta's tool can also: 49 of Meta's fields are not named on ours. value_rules_applied is sent as true automatically with value_rule_set_id (campaigns.py:841-843); the other 48 reach Meta only through params (:915, :940). Seven of the 48 were chosen by the owner but have no public API route known: cost_bidding_mode, conversion_locations, value_rules_spec, conversion_value_expression_spec, saved_audience_id, calling_settings and marketing_goal. The other 41 include placement, saved_audience, campaign_spec (create the campaign in the same call), conversion_goal_id, brand_audience_id, include_in_ad_study_id and fields that look internal to Meta (creative diversity scores, guidance estimates, metrics metadata). The named destination_type refuses LEAD_FORM_MESSENGER, which Meta's tool lists, and points to params [no public API route known: Meta's tables spell it two ways and contradict each other]. PHONE_CALL is a named value, from Meta's call ads guide. Fixed-amount budget schedules (campaigns.py:289-296). Conveniences: fills in the EU DSA beneficiary and payor from the business name, and the messaging Page from the account's main Page. Drafts [no public API route known]. 16 fields were added since the third pass, among them existing_customer_budget_percentage, multi_optimization_goal_weight, creative_sequence, time-based ad rotation and brand_safety_config (campaigns.py:777-792, checked at :396-435). Why this is open: Built differently: the remaining fields are left to params by the owner's choice.",
    ),
    "ads_create_campaign": _partial(
        "create_campaign", "duplicate_campaign",
        note="Meta's tool can also: Staging into Ads Manager drafts [no public API route known, spec :39-40]. Fixed-amount (ABSOLUTE) budget schedules are offered: a live test on 2026-11-13 showed the value is in minor units, as the tool sends it. Every other field of Meta's tool is now named on ours: is_skadnetwork_attribution, campaign_optimization_type, is_using_l3_schedule, iterative_split_test_configs and topline_id were added (campaigns.py:458-462, checked at :335-350). Why this is open: No public API route known (Ads Manager drafts).",
    ),
    "ads_create_creative": _partial(
        "create_ad_creative_from_inputs", "create_ad_creative", "upload_creative_asset",
        note="Meta's tool can also: Click-to-message static carousels for WhatsApp and Messenger (refused, creative_formats.py:496-501). A click-to-Instagram-Direct carousel in which some cards link to a website; ours makes every card open the chat (:510-517). A WhatsApp button on a single video; ours offers Messenger and Instagram Direct, and Meta shows no request for WhatsApp. Instant forms opened by GET_OFFER, CONTACT, GET_IN_TOUCH or BOOK_TRAVEL; ours accepts the six buttons Meta's lead ads guide lists (:60-61, :326-331). WhatsApp placements (whatsapp_positions) and Oculus in placement_customizations; ours takes Facebook, Instagram, Messenger, Audience Network and Threads (:73-80, :662-669). Instagram Partnership Ads from new media as a carousel; ours builds them from one image or video (:1025-1030). Images and videos mixed in one placement customization (refused, :631-637). AI-content disclosure self_ai_disclosure (spec :42). Conveniences: Advantage+ switches on the builder (ours: raw degrees_of_freedom_spec on create_ad_creative, creatives.py:243, or update_ad_creative_enhancements afterwards); image_url downloaded into the account (ours sends the URL as picture, creative_formats.py:197-201, or uploads it first with upload_creative_asset); default thumbnails for video cards and the main pool video (ours requires them, :476-477, :929-933); Meta picks the Product Extensions product set when none is named (ours requires product_extensions_product_set_id, :1043-1052); the Partnership Ad creator given as a handle. Built since the third pass: display_link (:150-161), Product Extensions (:998-1018, :1046-1057), click-to-Instagram-Direct carousels (:494-534), WhatsApp, Instagram Direct and Messenger buttons on an existing Instagram post, and an Instagram Direct button on a single video. Why this is open: No public API route known: by the owner's search for the WhatsApp and Messenger carousels, carousel Partnership Ads and mixed placements; by the parity spec for AI disclosure; by our code's reading of Meta's guides for the rest.",
    ),
    "ads_create_custom_audience": _partial(
        "create_custom_audience", "create_lookalike_audience",
        note="Meta's tool can also: The audience labels app_installers and customer_leads, which Meta's description lists and ours refuses; our code notes that neither Meta's guide nor the SDK lists them (AUDIENCE_LABELS, audiences.py:138-148). Convenience: a lookalike without naming countries; ours requires country or countries (audiences.py:56-61). is_value_based and the other labels are named (audiences.py:169, :205-206). Why this is open: No public API route known.",
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
        note="Meta's tool can also: Chooses the metric that decides the winner (primary_kpi, secondary_kpis). Our code states that Meta's documented API offers no such choice (experiments.py:1482, :1540), and the owner's search found none. Why this is open: No public API route known.",
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
        note="Meta's tool can also: Meta's own verdict, including Lift study eligibility, and an account-level check with no ids (over the top campaigns by spend). Ours is a local pre-check and states that Meta has no public eligibility endpoint (experiments.py:1307-1315). Ad ids are accepted (level=ad, :1318). Why this is open: No public API route known.",
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
        note="Meta's tool can also: Reads unpublished Ads Manager drafts (object_state=draft). Adds benchmark and incrementality context (include_additional_context). Convenience: settings and metrics in one row; ours reads settings with the list tools and metrics with get_entity_insights (insights.py:812). Why this is open: No public API route known.",
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
        note="Meta's tool can also: CRM and custom-attribution channels (query_type); ours covers web and offline (DATASET_QUALITY_CHANNELS, datasets.py:104; tool at :491). Why this is open: No public API route known.",
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
        note="Meta's tool can also: Field descriptions, display names, aliases (spend to amount_spent), and which levels support each metric. Ours (level=metric, assets.py:1008-1013) gives types, value shapes, sort syntax and filter operators, and points to Meta's reference for descriptions. Why this is open: Built differently: SDK-derived types, no descriptions or aliases.",
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
        note="Meta's tool can also: A lifetime window: ours takes lookback_days 7 to 90, or since and until covering at most 366 days (performance_signals.py:3463-3464, :393-394). Meta's summary is its own; ours is derived from account data and says so (:3481). Scoping to campaigns, ad sets or ads is built (object_ids, ad_ids, :3473-3479). Why this is open: Built differently: windows of at most 366 days, summary derived from account data.",
    ),
    "ads_insights_anomaly_signal": _partial(
        "detect_performance_anomalies",
        note="Meta's tool can also: Meta's own detector, over entity lists of unstated size. Ours applies its stated median rule to at most 10 entities per call (MAX_ENTITIES, performance_signals.py:1035; tool at :1915). Why this is open: Built differently: stated rule, up to 10 entities per call.",
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
        note="Meta's tool can also: Trends over the full available history; ours covers at most 366 days per call (MAX_DAILY_WINDOW_DAYS, performance_signals.py:43, :393-394). Convenience: entity lists of unstated size; ours takes up to 10 (:1035). Reach and a period series for each entity are built (:1471-1483). Why this is open: Built differently: at most 366 days and 10 entities per call.",
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
        note="Meta's tool can also: In the partner view, catalogs shared with the partner through a Business Asset Group. Ours checks each owned catalog's direct partner list (business_access.py:560-580). Conveniences: each catalog's permissions in the same call (ours: list_catalog_partners for each catalog, :1011); name search. Why this is open: Built differently: direct partner shares, read catalog by catalog.",
    ),
    "ads_permission_list_assigned_users": _partial(
        "list_catalog_assigned_users", "list_catalog_partners",
        note="Meta's tool can also: Explains why a person lacks access: not on the business, the business lacks the asset, no permissions, or two-factor required. Counts access through Business Asset Groups and business roles. Partner checks report pending requests. Ours lists direct assignments only (business_access.py:445). Convenience: name search. Partner rows with their tasks are built (:451-463, :1011-1018). Why this is open: Built differently: direct assignments only.",
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
    "ads_update_custom_audience": _covered(
        "update_custom_audience",
        note="Same capability.",
    ),
    "ads_update_entity": _partial(
        "update_campaign", "update_adset", "update_ad", "update_ad_creative_enhancements", "set_campaign_status", "set_adset_status", "set_ad_status", "update_campaign_budget", "update_adset_budget", "update_adset_bid_amount", "update_campaign_bid_strategy", "update_adset_bid_strategy", "delete_campaign", "delete_adset", "delete_ad",
        note="Meta's tool can also: Discards Ads Manager draft changes (discard_draft_changes). Convenience: one switch (advantage_plus_creative_defaults=OFF) turns off whichever default Advantage+ enhancements apply to the ad; ours names features one by one (creative_formats.py:1414-1420). Campaign spend_cap, start_time, stop_time and labels on campaign, ad set and ad updates are named (campaigns.py:581-584, :1072, ads.py:433). Why this is open: No public API route known.",
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
