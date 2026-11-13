"""Operational utility tools for Claude-friendly MCP use."""

from __future__ import annotations

import re

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.build_info import BUILD_IDENTITY
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.coordinator import (
    ALWAYS_VISIBLE_TOOLS,
    OVERFLOW_ARTIFACT_STORE,
    mcp_server,
)
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.official_parity import OFFICIAL_TOOLS, parity_summary
from meta_ads_mcp.overflow import (
    DEFAULT_ARTIFACT_CHUNK_BYTES,
    DEFAULT_ARTIFACT_ITEM_LIMIT,
    DEFAULT_ARTIFACT_RESULT_BYTES,
)

MAX_INLINE_ARTIFACT_RESULT_BYTES = DEFAULT_ARTIFACT_RESULT_BYTES

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
        "get_audience",
        "list_creatives",
        "get_creative",
    ],
    "analysis": [
        "get_insights",
        "get_entity_insights",
        "get_performance_breakdown",
        "summarize_actions",
        "compare_time_ranges",
        "compare_performance",
        "export_insights",
        "create_async_insights_report",
        "create_async_insights_report_batch",
        "get_async_insights_report",
        "summarize_async_insights_report",
    ],
    "activity": [
        "list_change_history",
        "export_change_history",
    ],
    "optimization": [
        "get_account_optimization_snapshot",
        "get_campaign_optimization_snapshot",
        "get_native_optimization_signals",
        "get_account_health_snapshot",
        "detect_auction_overlap",
        "get_ad_feedback_signals",
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
        "get_opportunity_score",
        "get_performance_trend",
        "detect_performance_anomalies",
        "get_auction_ranking_report",
        "get_advertiser_context",
        "get_delivery_errors",
    ],
    "social_feedback": [
        "get_ad_social_context",
        "list_ad_comments",
        "list_comment_replies",
        "list_page_recommendations",
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
        "delete_adset",
        "delete_ad",
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
        "update_ad_creative",
        "delete_creative",
        "create_ab_test",
        "create_lift_study",
        "update_ad_study",
        "cancel_ad_study",
        "create_catalog",
        "update_catalog",
        "create_catalog_product",
        "update_catalog_product",
        "delete_catalog_product",
        "create_product_set",
        "update_product_set",
        "delete_product_set",
        "create_product_feed",
        "update_product_feed",
        "delete_product_feed",
        "create_product_feed_upload",
        "create_feed_rule",
        "delete_feed_rule",
        "connect_catalog_event_source",
        "disconnect_catalog_event_source",
        "update_adset",
        "update_ad",
        "add_audience_users",
        "remove_audience_users",
        "boost_instagram_post",
        "create_ad_creative_from_inputs",
        "update_ad_creative_enhancements",
        "upload_ad_video",
        "delete_ad_image",
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
    "assets": [
        "list_pages",
        "list_ad_images",
        "list_ad_videos",
        "list_creative_ads",
        "list_instagram_media",
        "list_audience_adsets",
        "get_field_reference",
    ],
    "signals": [
        "list_datasets",
        "get_dataset",
        "get_dataset_stats",
        "get_dataset_quality",
        "list_custom_conversions",
    ],
    "experiments": [
        "list_ad_studies",
        "get_ad_study",
        "check_ab_test_eligibility",
    ],
    "catalog": [
        "list_catalogs",
        "list_dpa_eligible_catalogs",
        "get_catalog_diagnostics",
        "get_catalog_dynamic_ads_health",
        "list_catalog_data_sources",
        "list_catalog_products",
        "build_product_set_filter",
        "list_product_sets",
        "list_product_feeds",
        "list_product_feed_uploads",
        "list_feed_rules",
        "list_catalog_event_sources",
        "list_event_source_catalogs",
        "get_catalog_event_source_health",
        "get_catalog_event_source_recommendations",
    ],
    "access": [
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
        "get_v26_notes",
        "get_optimization_playbook",
    ],
    "utility": [
        "health_check",
        "get_capabilities",
        "list_mutation_tools",
        "read_overflow_artifact",
        "delete_overflow_artifact",
    ],
}

RESOURCE_URIS = [
    "meta://docs/object-model",
    "meta://docs/insights-metrics",
    "meta://docs/v25-notes",
    "meta://docs/v26-notes",
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
            "list_audiences",
            "get_account_pages",
            "list_instagram_accounts",
        ],
        "avoid_unless_needed": ["export_insights", "create_async_insights_report"],
        "notes": ["Prefer list tools before single-id get tools when the user has not supplied ids."],
    },
    "inspect_single_entity_performance": {
        "description": "Inspect performance for one account, campaign, ad set, or ad.",
        "recommended_order": ["get_entity_insights", "get_performance_breakdown", "summarize_actions"],
        "avoid_unless_needed": ["export_insights"],
        "notes": [
            "Use summarize_actions for appointments, purchases, leads, or other action counts.",
            "Use export_insights only when the user explicitly wants raw rows or CSV output.",
        ],
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
    "read_change_history": {
        "description": "Read account, campaign, ad set, or ad activity logs, changelogs, audit logs, and change history.",
        "recommended_order": ["export_change_history", "read_overflow_artifact"],
        "avoid_unless_needed": ["get_insights"],
        "notes": [
            "Use this when the user asks who changed budgets, statuses, bids, names, targeting, or other settings.",
            "export_change_history requires explicit since/until and includes descendants by default. Account scope covers all account activity levels and preserves every API-returned event type.",
            "Read every artifact page; inline export items are only a preview. Check complete, coverage, has_more, ai_instructions, and next_call. A complete artifact read is not proof that every Meta page was fetched.",
            "list_change_history returns ONE page, defaults to seven days, and excludes children of object scopes unless include_descendants=true. Follow next_call even when items is empty.",
            "Date-only until includes that entire day in time_zone (UTC by default); timestamp bounds need explicit offsets. Continuation calls preserve frozen effective bounds.",
            "Omit category and uid for all change types. Category is not entity scope; category=AD_SET can omit ad set changes. Unknown events and unresolved parent relationships must stay visible.",
            "Use entity_level for Meta's legacy object_type names. Deletions can be status changes; preserve raw details, old/new availability, actors, and minor currency units.",
            "Completeness is limited to API-visible history for the stated period/filters. Never claim every real-world change or infer no changes from an empty/incomplete response.",
            "Pass business_id when Meta requires it for ad accounts connected to a Business Manager.",
            "Use uid to filter to changes made by a specific Meta user.",
        ],
    },
    "export_reporting_data": {
        "description": "Return raw or file-like reporting output instead of summary analysis.",
        "recommended_order": ["export_insights", "create_async_insights_report", "get_async_insights_report"],
        "avoid_unless_needed": [],
        "notes": ["Prefer summary tools first; exports are the heaviest reporting path."],
    },
    "retrieve_oversized_response": {
        "description": "Retrieve or download the complete oversized response behind an overflow export id.",
        "recommended_order": [
            "read_overflow_artifact",
            "delete_overflow_artifact",
        ],
        "avoid_unless_needed": [],
        "notes": [
            "Read repeatedly with next_offset until complete is true.",
            "Delete the overflow artifact only after all chunks have been retrieved.",
        ],
    },
    "find_optimization_opportunities": {
        "description": "Find optimization opportunities, risks, pacing issues, or fatigue.",
        "recommended_order": [
            "get_account_optimization_snapshot",
            "get_campaign_optimization_snapshot",
            "get_account_health_snapshot",
            "detect_auction_overlap",
            "get_native_optimization_signals",
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
            "Use get_native_optimization_signals for v26 campaign, ad-set, or ad delivery and issue diagnostics.",
            "Use typed opportunity tools only when the user asks for one category specifically.",
        ],
    },
    "check_opportunity_score": {
        "description": "Read Meta's 0-100 account opportunity score, its recent history, and the recommendations that would raise it.",
        "recommended_order": ["get_opportunity_score", "get_recommendations"],
        "avoid_unless_needed": [],
        "notes": ["Recommendations are ranked by Meta's estimated score gain (opportunity_score_lift)."],
    },
    "track_performance_trends_or_anomalies": {
        "description": "See how CPC, CPM, cost per result, ROAS, CTR or conversion rate moved over days, weeks or months, or find unusual days.",
        "recommended_order": ["get_performance_trend", "detect_performance_anomalies", "get_budget_pacing_report"],
        "avoid_unless_needed": ["export_insights"],
        "notes": [
            "detect_performance_anomalies compares each recent day with the object's own baseline using median and median absolute deviation; it is a rule-based screen, not a statistical test.",
        ],
    },
    "compare_ads_to_benchmarks_or_competitors": {
        "description": "Compare ads with ads competing for the same audience using Meta's quality, engagement-rate and conversion-rate rankings.",
        "recommended_order": ["get_auction_ranking_report", "get_ad_feedback_signals"],
        "avoid_unless_needed": [],
        "notes": [
            "Industry benchmarks from similar advertisers are not available through Meta's public API; Meta's rankings are the nearest comparison.",
        ],
    },
    "brief_on_advertiser_context": {
        "description": "Get a briefing on the advertiser: account settings, spend by objective and optimization goal, funnel volumes, datasets, Pages and Instagram identities.",
        "recommended_order": ["get_advertiser_context", "get_account_optimization_snapshot"],
        "avoid_unless_needed": [],
        "notes": ["get_advertiser_context is derived from account data by this server, not Meta's own summary."],
    },
    "troubleshoot_delivery_errors": {
        "description": "Find why the account, campaigns, ad sets or ads are not delivering: disapprovals, review feedback, issues, failed delivery checks, account status.",
        "recommended_order": ["get_delivery_errors", "get_native_optimization_signals", "get_learning_phase_report"],
        "avoid_unless_needed": [],
        "notes": [
            "get_delivery_errors says when its scan stopped before the end; continue with after=scan.next_after.",
        ],
    },
    "read_ad_comments_or_quality_signals": {
        "description": (
            "Handle asks for ad comments, reviews, testimonials, customer feedback, negative feedback, "
            "or quality rankings."
        ),
        "recommended_order": [
            "list_ad_comments",
            "list_page_recommendations",
            "get_ad_social_context",
            "get_ad_feedback_signals",
            "get_creative_performance_report",
            "get_creative_fatigue_report",
        ],
        "avoid_unless_needed": ["get_entity_insights"],
        "notes": [
            "Use list_ad_comments when the user provides an ad id, object_story_id, or Instagram media id.",
            "Use list_page_recommendations for Facebook Page reviews or testimonials.",
            "Customer feedback score and negative-feedback counts are still not exposed as stable public Marketing API fields.",
            "Use get_ad_feedback_signals for Meta ad-quality ranking fields when comments are not enough.",
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
            "get_creative",
            "get_ad_image",
            "preview_ad",
            "upload_creative_asset",
            "create_ad_creative",
            "create_ad",
        ],
        "avoid_unless_needed": [],
        "notes": [
            "Use get_creative when a creative id is already known and full creative fields are needed.",
            "Use get_account_pages and list_instagram_accounts before creative creation when an identity is required.",
        ],
    },
    "research_competitor_ads": {
        "description": "Search public Ads Library data for competitor or market research.",
        "recommended_order": ["search_ads_archive"],
        "avoid_unless_needed": [],
        "notes": ["This still requires Ads Library API app access even though it does not depend on an ad account id."],
    },
    "inspect_pixels_datasets_or_tracking": {
        "description": (
            "Check pixels, datasets, Conversions API setup, event volume, event match quality, "
            "and custom conversions."
        ),
        "recommended_order": [
            "list_datasets",
            "get_dataset",
            "get_dataset_stats",
            "get_dataset_quality",
            "list_custom_conversions",
        ],
        "avoid_unless_needed": [],
        "notes": [
            "Use get_dataset_quality for event match quality and match-key coverage; it flags weak scores and states the threshold used.",
            "get_dataset_stats covers at most 28 days per call and totals hourly counts per event, device, source, or other aggregation.",
        ],
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
    "run_ab_tests_or_lift_studies": {
        "description": "Find, inspect, check eligibility for, create, edit or stop A/B tests (split tests) and conversion lift studies, and read a test's winner or lift.",
        "recommended_order": [
            "list_ad_studies",
            "get_ad_study",
            "check_ab_test_eligibility",
            "create_ab_test",
            "create_lift_study",
            "update_ad_study",
            "cancel_ad_study",
        ],
        "avoid_unless_needed": ["setup_ab_test"],
        "notes": [
            "Use get_ad_study with include_results=true for the split test winner and confidence, or lift and confidence per cell; it says plainly when results are not ready.",
            "check_ab_test_eligibility is a local pre-check, not Meta's verdict; Meta decides when the test is created.",
            "Meta creates studies on a business; pass business_id when the ad account has no owning business.",
            "Lift studies are limited by Meta to approved advertisers.",
        ],
    },
    "manage_product_catalogs_and_product_sets": {
        "description": (
            "Find Meta product catalogs (Commerce Manager shop/product feeds for dynamic or Advantage+ catalog ads), "
            "check whether they can run catalog ads, inspect products and product sets, and build product set filters."
        ),
        "recommended_order": [
            "list_catalogs",
            "list_dpa_eligible_catalogs",
            "get_catalog_dynamic_ads_health",
            "get_catalog_diagnostics",
            "list_catalog_data_sources",
            "list_catalog_products",
            "list_product_sets",
            "build_product_set_filter",
        ],
        "avoid_unless_needed": [
            "create_product_set",
            "update_product_set",
            "create_catalog_product",
            "update_catalog_product",
            "create_catalog",
            "update_catalog",
            "delete_product_set",
            "delete_catalog_product",
        ],
        "notes": [
            "Catalog reads need the catalog_management permission; a permission error means the token lacks it, not that there are no catalogs.",
            "Turn a plain-English product selection into conditions and test it with build_product_set_filter before create_product_set.",
            "Product prices are major units (12.5 means 12.50); new products are created with visibility=staging.",
            "list_dpa_eligible_catalogs and get_catalog_dynamic_ads_health are derived from documented signals and state the rules they use.",
        ],
    },
    "manage_product_catalog_feeds_and_event_sources": {
        "description": (
            "Inspect or change product catalog data feeds, feed uploads and upload errors, feed rules, "
            "and the pixels or apps connected to a product catalog, including match-rate health."
        ),
        "recommended_order": [
            "list_product_feeds",
            "list_product_feed_uploads",
            "list_feed_rules",
            "list_catalog_event_sources",
            "get_catalog_event_source_health",
            "get_catalog_event_source_recommendations",
            "list_event_source_catalogs",
        ],
        "avoid_unless_needed": [
            "create_product_feed",
            "update_product_feed",
            "create_product_feed_upload",
            "create_feed_rule",
            "connect_catalog_event_source",
            "delete_feed_rule",
            "delete_product_feed",
            "disconnect_catalog_event_source",
        ],
        "notes": [
            "These tools work on Meta product catalogs and need the catalog_management permission.",
            "Deleting a feed removes the products it supplied unless another source also supplies them.",
            "Health and recommendations are derived here from event_stats, connected sources and Meta's Dynamic Ads checks.",
        ],
    },
    "find_pages_media_and_asset_usage": {
        "description": (
            "List Pages by user, ad account or business; browse the image and video library; find which ads "
            "use a creative or which ad sets use a custom audience; list boostable Instagram posts; look up "
            "field types and allowed values."
        ),
        "recommended_order": [
            "list_pages",
            "list_ad_images",
            "list_ad_videos",
            "list_creative_ads",
            "list_audience_adsets",
            "list_instagram_media",
            "get_field_reference",
        ],
        "avoid_unless_needed": [],
        "notes": [
            "list_creative_ads and list_audience_adsets scan with a page cap; check summary.complete and continue with summary.next_after.",
            "list_instagram_media marks each post boost_eligible with Meta's reason when it is not; boost with boost_instagram_post.",
            "get_field_reference comes from Meta's SDK field list because Meta no longer serves live field introspection.",
        ],
    },
    "edit_ad_sets_ads_and_customer_lists": {
        "description": (
            "Edit ad set or ad settings with before/after, add or remove people in a customer list audience, "
            "or boost an Instagram post as a paused ad, after explicit user confirmation."
        ),
        "recommended_order": [
            "update_adset",
            "update_ad",
            "add_audience_users",
            "remove_audience_users",
            "boost_instagram_post",
        ],
        "avoid_unless_needed": [],
        "notes": [
            "update_adset rejects budgets and bids; use update_adset_budget, update_adset_bid_amount or update_adset_bid_strategy.",
            "validate_only on update_adset, update_ad and boost_instagram_post has Meta check the change without applying it.",
            "For customer lists prefer file_path to a local CSV so personal data never passes through the model; inline rows are limited to 1,000.",
        ],
    },
    "build_creatives_and_upload_media": {
        "description": (
            "Build a carousel, single image or video, text variants, placement-specific media, click-to-WhatsApp, "
            "Messenger or Instagram Direct, instant form or Partnership Ad creative from named inputs; upload a "
            "video; remove a library image; switch Advantage creative enhancements on a live ad; label creatives."
        ),
        "recommended_order": [
            "upload_creative_asset",
            "upload_ad_video",
            "list_ad_videos",
            "create_ad_creative_from_inputs",
            "preview_ad",
            "update_ad_creative_enhancements",
            "update_creative",
            "delete_ad_image",
        ],
        "avoid_unless_needed": ["create_ad_creative"],
        "notes": [
            "create_ad_creative_from_inputs dry_run returns the exact spec without creating anything.",
            "Wait for list_ad_videos processing_status ready before using an uploaded video.",
            "preview_ad ad_formats previews several placements in one call.",
            "update_ad_creative_enhancements creates a copy of the creative and attaches it, so the ad goes back to review.",
        ],
    },
}

ROUTING_HINTS = {
    intent: list(route["recommended_order"]) + list(route["avoid_unless_needed"])
    for intent, route in INTENT_GUIDE.items()
}

INTENT_SEARCH_SYNONYMS = {
    "comments": "feedback reviews testimonials quality ranking negative feedback",
    "comment": "feedback reviews testimonials quality ranking negative feedback",
    "review": "feedback comments testimonials quality ranking negative feedback",
    "reviews": "feedback comments testimonials quality ranking negative feedback",
    "testimonial": "feedback comments reviews quality ranking",
    "testimonials": "feedback comments reviews quality ranking",
    "customer": "feedback score reviews comments quality ranking",
    "score": "customer feedback quality ranking",
    "appointment": "actions conversions summarize_actions",
    "appointments": "actions conversions summarize_actions",
    "pause": "writes status mutation set_status",
    "budget": "writes budget mutation",
    "create": "writes mutation create",
    "change": "activity changelog audit log history",
    "changes": "activity changelog audit log history",
    "changed": "activity changelog audit log history",
    "changelog": "activity changes audit log history",
    "history": "activity changes changelog audit log",
    "audit": "activity changes changelog history",
    "log": "activity changes changelog history",
    "boost": "boost_instagram_post edit paused ad",
}


def _search_tokens(text: str) -> set[str]:
    """Tokenize routing text for tiny local fuzzy matching."""
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9_]+", text.lower()):
        components = {token, *(part for part in token.split("_") if part)}
        for component in components:
            tokens.add(component)
            if component.endswith("s") and len(component) > 3:
                tokens.add(component[:-1])
            if component in {"adset", "adsets"}:
                tokens.update({"ad", "set", "ad_set"})
    return tokens


def _closest_intents(query: str, limit: int = 3) -> list[dict[str, object]]:
    """Return best-effort intent matches instead of failing on free text."""
    expanded_query = " ".join([query, *(INTENT_SEARCH_SYNONYMS.get(token, "") for token in _search_tokens(query))])
    query_tokens = _search_tokens(expanded_query)
    matches: list[tuple[float, str, dict[str, object]]] = []
    for intent, route in INTENT_GUIDE.items():
        route_parts = [
            intent,
            route.get("description", ""),
            *route.get("recommended_order", []),
            *route.get("avoid_unless_needed", []),
            *route.get("notes", []),
        ]
        route_text = " ".join(map(str, route_parts))
        route_tokens = _search_tokens(route_text)
        overlap = query_tokens & route_tokens
        score = float(len(overlap))
        if intent == "discover_accounts_or_ids" and query_tokens & {
            "account",
            "accounts",
            "campaign",
            "campaigns",
            "adset",
            "adsets",
            "ad",
            "ads",
        }:
            score += 2.0
        if intent == "writes_after_confirmation" and query_tokens & {
            "pause",
            "resume",
            "enable",
            "disable",
            "status",
            "budget",
            "bid",
            "create",
            "update",
            "delete",
        }:
            score += 3.0
        if intent == "inspect_single_entity_performance" and query_tokens & {
            "appointment",
            "appointments",
            "purchase",
            "purchases",
            "lead",
            "leads",
            "conversion",
            "conversions",
        }:
            score += 2.0
        if query.lower() in route_text.lower():
            score += 3.0
        if score > 0:
            matches.append((score, intent, route))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "intent": intent,
            "score": score,
            "description": route["description"],
            "recommended_order": route["recommended_order"],
        }
        for score, intent, route in matches[:limit]
    ]


def tool_routing_markdown() -> str:
    """Return a compact markdown routing guide for Claude."""
    lines = [
        "# Tool Routing Guide",
        "",
        "FastMCP 3.4.8 tool search is enabled. If the exact tool is not visible, use `search_tools` and then `call_tool`.",
        "",
        "Use `get_capabilities(intent=...)` for a compact routing response.",
        "Use `get_capabilities(include_full_manifest=true)` only when the compact response is insufficient.",
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
        "build": BUILD_IDENTITY,
        "fastmcp_version_target": "3.4.8",
        "api_version": settings.api_version,
        "read_only": settings.read_only,
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
        "build": BUILD_IDENTITY,
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
async def get_capabilities(
    intent: str | None = None,
    include_full_manifest: bool = False,
    tool_name: str | None = None,
    official_tool: str | None = None,
) -> dict[str, object]:
    """Get routing guidance or a named tool's complete input schema via tool_name; the full manifest lists workflows, not parameter schemas. Pass official_tool with a tool name from Meta's own Ads MCP server (ads_...) to learn which tool here serves it."""
    if official_tool is not None:
        if not official_tool.strip() or intent is not None or include_full_manifest or tool_name is not None:
            raise ValidationError("Provide official_tool alone to look up one of Meta's official tool names.")
        entry = OFFICIAL_TOOLS.get(official_tool.strip())
        if entry is None:
            raise ValidationError(
                f"'{official_tool}' is not one of Meta's official Ads MCP tool names. "
                "Use search_tools to find a tool by what it does."
            )
        return {
            "server": _server_metadata(),
            "official_tool": entry.to_dict(official_tool.strip()),
            "official_parity": parity_summary(),
        }
    if tool_name is not None:
        if not tool_name.strip() or intent is not None or include_full_manifest:
            raise ValidationError("Provide tool_name alone to inspect one tool's input schema.")
        tool = await mcp_server.get_tool(tool_name.strip())
        if tool is None:
            raise ValidationError(f"Unknown tool: {tool_name}.")
        return {
            "server": _server_metadata(),
            "tool": {"name": tool.name, "description": tool.description, "input_schema": tool.parameters},
        }
    if intent is not None:
        route = INTENT_GUIDE.get(intent)
        if route is None:
            closest = _closest_intents(intent)
            matches = await mcp_server.call_tool("search_tools", {"query": intent})
            return {
                "server": _server_metadata(),
                "unmatched_intent": intent,
                "closest_intents": closest,
                "tool_matches": "\n".join(item.text for item in matches.content if item.type == "text"),
                "suggested_search": {"tool": "search_tools", "arguments": {"query": intent}},
                "resources": RESOURCE_URIS,
                "valid_intents": sorted(INTENT_GUIDE),
                "notes": [
                    "No exact intent key matched, so this response returns fuzzy routing candidates.",
                    "Prefer tool_matches from the live tool search; closest_intents are legacy workflow hints.",
                ],
            }
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
                "When the exact tool is not visible, use search_tools and then call_tool because FastMCP 3.4.8 tool search is enabled.",
            ],
        }

    auth = {
        "required": ["META_ACCESS_TOKEN"],
        "optional": [
            "META_DEFAULT_ACCOUNT_ID",
            "META_APP_ID",
            "META_APP_SECRET",
            "META_REDIRECT_URI",
            "META_EXPORT_DIR",
            "META_EXPORT_TTL_SECONDS",
            "META_EXPORT_MAX_FILES",
            "META_EXPORT_MAX_BYTES",
            "META_READ_ONLY",
        ],
    }
    notes = [
        "Use discovery and diagnostics before write operations.",
        "FastMCP 3.4.8 tool search is enabled, so the server may expose search_tools and call_tool instead of the entire tool catalog up front.",
        "compare_performance reuses the insights surface and avoids extra lookups when names are already present in insights rows.",
        "export_insights is a convenience wrapper over the core insights surface.",
        "Oversized tool responses return an opaque export_id; read_overflow_artifact returns parsed item pages by default or original JSON bytes with offset=0. Follow the issued continuation and delete_overflow_artifact when finished.",
        "For async reports, pass all returned poll_arguments on every fetch to preserve flatten_actions; summarize_async_insights_report aggregates additive metrics across bounded page ranges.",
        "Pass intent to get_capabilities for a compact routing response instead of the full manifest.",
        "Use get_account_pages before creative creation when a Page or Instagram-linked asset is needed.",
        "Use list_instagram_accounts when creative setup requires an Instagram identity rather than a Facebook Page.",
        "Use get_recommendations once for a broad Meta-native opportunity scan, and use typed opportunity tools only for category-specific follow-up.",
        "Use summarize_actions for appointment, lead, purchase, or custom action counts instead of returning full actions arrays.",
        "Use list_ad_comments for raw ad comments and list_page_recommendations for Page reviews; customer feedback score is not exposed as a stable public Marketing API field.",
        "Use export_change_history for complete account/campaign/adset/ad change history in an explicit period. Read all artifact pages and any next_call export parts. list_change_history is only a single page; always disclose coverage gaps and active filters.",
        "Use list_mutation_tools when the user asks what the MCP can change.",
        "search_ads_archive is public research data and does not depend on an ad account id, but the app still needs Ads Library API access.",
        "Write operations still depend on the token having ads_management-level permissions.",
    ]
    tool_groups = TOOL_GROUPS
    if get_settings().read_only:
        from meta_ads_mcp.read_only import read_only_tool_names

        allowed = read_only_tool_names()
        tool_groups = {group: [name for name in names if name in allowed] for group, names in TOOL_GROUPS.items()}
        notes.append("META_READ_ONLY is enabled; advertising mutations and token mutations are disabled, including through call_tool.")
    if include_full_manifest:
        return {
            "server": _server_metadata(),
            "auth": auth,
            "tool_groups": tool_groups,
            "routing_hints": ROUTING_HINTS,
            "intent_guide": INTENT_GUIDE,
            "resources": RESOURCE_URIS,
            "official_parity": parity_summary(),
            "notes": notes,
        }

    return {
        "server": _server_metadata(),
        "auth": auth,
        "valid_intents": sorted(INTENT_GUIDE),
        "tool_group_counts": {group: len(tools) for group, tools in tool_groups.items()},
        "recommended_start": {
            "if_auth_or_connectivity_is_unclear": "health_check",
            "if_the_needed_tool_is_not_visible": "search_tools",
            "if_you_need_intent_specific_routing": "get_capabilities(intent=...)",
            "if_you_need_the_entire_manifest": "get_capabilities(include_full_manifest=true)",
        },
        "resources": RESOURCE_URIS,
        "notes": notes,
    }


@mcp_server.tool()
async def list_mutation_tools() -> dict[str, object]:
    """Use this when the user asks what Meta Ads state this MCP can create, pause, update, or delete."""
    if get_settings().read_only:
        return {"tool_group": "writes", "count": 0, "tools": [], "message": "META_READ_ONLY disables mutations."}
    return {
        "tool_group": "writes",
        "count": len(TOOL_GROUPS["writes"]),
        "tools": TOOL_GROUPS["writes"],
        "common_paths": {
            "pause_or_enable": ["set_campaign_status", "set_adset_status", "set_ad_status"],
            "budget_changes": ["update_campaign_budget", "update_adset_budget"],
            "bid_changes": ["update_campaign_bid_strategy", "update_adset_bid_strategy", "update_adset_bid_amount"],
            "creation": ["create_campaign", "create_ad_set", "create_ad", "create_ad_creative"],
            "deletion": ["delete_campaign", "delete_adset", "delete_ad", "delete_creative"],
            "audiences": ["create_custom_audience", "create_lookalike_audience", "update_custom_audience", "delete_audience"],
            "experiments": ["create_ab_test", "create_lift_study", "update_ad_study", "cancel_ad_study"],
            "product_catalogs": ["create_catalog", "update_catalog"],
            "catalog_products": ["create_catalog_product", "update_catalog_product", "delete_catalog_product"],
            "product_sets": ["create_product_set", "update_product_set", "delete_product_set"],
            "product_catalog_feeds": [
                "create_product_feed", "update_product_feed", "delete_product_feed",
                "create_product_feed_upload", "create_feed_rule", "delete_feed_rule",
            ],
            "product_catalog_event_sources": ["connect_catalog_event_source", "disconnect_catalog_event_source"],
            "general_edits": ["update_adset", "update_ad"],
            "customer_list_users": ["add_audience_users", "remove_audience_users"],
            "instagram_boost": ["boost_instagram_post"],
            "creative_building": ["create_ad_creative_from_inputs", "update_ad_creative_enhancements"],
            "media_library": ["upload_creative_asset", "upload_ad_video", "delete_ad_image"],
        },
        "safety_notes": [
            "Ask for explicit confirmation before spend-affecting changes.",
            "Read current state first with discovery or diagnostics tools.",
            "Token must have ads_management-level permissions.",
        ],
    }


@mcp_server.tool()
async def read_overflow_artifact(
    export_id: str,
    offset: int | None = None,
    max_bytes: int = DEFAULT_ARTIFACT_CHUNK_BYTES,
    item_offset: int | None = None,
    item_limit: int = DEFAULT_ARTIFACT_ITEM_LIMIT,
) -> dict[str, object]:
    """Read parsed items/rows by default; use offset=0 for complete original JSON bytes.

    Follow next_item_offset for item pages or next_offset for byte chunks. Only 0
    and recently issued continuations are accepted; guessed offsets are rejected.
    Both modes preserve the artifact. A single oversized item explains how to
    retrieve it in byte mode without skipping it. complete refers only to reading
    this artifact; inspect context.tool_result.structured_content.coverage and
    next_call for additional source pages or history export parts.
    """
    if offset is not None and item_offset is not None:
        raise ValidationError("Use offset for bytes or item_offset for items, not both.")
    return OVERFLOW_ARTIFACT_STORE.read(
        export_id,
        offset=0 if offset is None else offset,
        max_bytes=max_bytes,
        item_offset=item_offset,
        item_limit=item_limit,
        prefer_items=offset is None and item_offset is None,
        max_result_bytes=MAX_INLINE_ARTIFACT_RESULT_BYTES,
    )


@mcp_server.tool()
async def delete_overflow_artifact(export_id: str) -> dict[str, object]:
    """Delete an oversized response artifact after the caller has finished retrieving it."""
    return OVERFLOW_ARTIFACT_STORE.delete(export_id)
