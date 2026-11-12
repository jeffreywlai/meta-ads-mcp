"""Ledger mapping Meta's official Ads MCP server tools to this server's tools.

Meta's hosted server (https://mcp.facebook.com/ads) documents 88 tools by name
and one-line description. Each one has an entry here saying which tool on this
server serves the same need, or why none can.

Status values:

* ``covered``: a tool here does the same job through Meta's public API.
* ``approximate``: a tool here serves the same need by a different method,
  usually a rule applied to public data where Meta's tool gives its own
  internal answer. The note says what differs.
* ``unavailable``: Meta's public API does not expose the capability, so no tool
  is registered for it. ``tools`` may name the nearest real tool.

``verified`` is derived, not declared: ``live`` when every serving tool has been
run against a real ad account and returned data, otherwise ``mocked``. Mocked
tests use a fake client that accepts any endpoint name, so they cannot catch a
wrong endpoint or field. Write tools stay ``mocked`` until a live write test is
explicitly approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ParityStatus = Literal["covered", "approximate", "unavailable"]
Verification = Literal["live", "mocked"]

# Tools run read-only against a real ad account, returning data, on 2026-11-12.
LIVE_VERIFIED_TOOLS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class OfficialToolEntry:
    """How one of Meta's official tools is served here."""

    status: ParityStatus
    tools: tuple[str, ...]
    note: str

    @property
    def verified(self) -> Verification | None:
        """Whether every serving tool has run against a real account."""
        if self.status == "unavailable" or not self.tools:
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
        }


def _covered(*tools: str, note: str) -> OfficialToolEntry:
    return OfficialToolEntry("covered", tools, note)


def _approximate(*tools: str, note: str) -> OfficialToolEntry:
    return OfficialToolEntry("approximate", tools, note)


def _unavailable(*nearest: str, note: str) -> OfficialToolEntry:
    return OfficialToolEntry("unavailable", nearest, note)


_PIXEL_RULE_WRITE_NOTE = (
    "Meta's public reference marks creating, updating and deleting pixel event rules "
    "and parameter extractors as unsupported. Make these changes in Events Manager."
)
_PIXEL_RULE_READ_NOTE = (
    "Meta's reference documents this read, but the live API rejected it for a system-user "
    "token on 2026-11-12 (event rules: code 190 authentication error; extractors: "
    "nonexisting field). View these in Events Manager."
)

OFFICIAL_TOOLS: dict[str, OfficialToolEntry] = {
    # Reporting
    "ads_get_ad_entities": _covered(
        "list_campaigns", "list_adsets", "list_ads", "get_entity_insights", "compare_performance",
        note="Use the list tools to find entities and get_entity_insights or compare_performance for metrics, breakdowns, filters and date ranges.",
    ),
    "ads_get_opportunity_score": _covered(
        "get_opportunity_score",
        note="Returns the 0 to 100 account score, its daily history and the top recommendations by estimated score gain.",
    ),
    "ads_insights_advertiser_context": _approximate(
        "get_advertiser_context",
        note="A briefing derived here from account settings, spend mix, funnel events and tracking setup. It is not Meta's own business summary.",
    ),
    "ads_insights_anomaly_signal": _approximate(
        "detect_performance_anomalies",
        note="Flags days far from the entity's own recent baseline using a stated median-based rule. It is this server's rule, not Meta's detector.",
    ),
    "ads_insights_auction_ranking_benchmarks": _approximate(
        "get_auction_ranking_report",
        note="Built on Meta's public quality, engagement-rate and conversion-rate rankings. Meta's tool may use auction data the public API does not expose.",
    ),
    "ads_insights_industry_benchmark": _unavailable(
        "get_auction_ranking_report",
        note="Meta's public API has no endpoint for benchmarks from similar advertisers. The ranking fields in get_auction_ranking_report compare each ad with ads competing for the same audience, which is the nearest public signal.",
    ),
    "ads_insights_performance_trend": _covered(
        "get_performance_trend",
        note="CPC, CPM, cost per result, ROAS, CTR and conversion rate per day, week or month with first-to-last change.",
    ),
    # Ad creation and management
    "ads_get_ad_accounts": _covered("list_ad_accounts", note="Lists ad accounts the token can access."),
    "ads_get_ad_account_pages": _covered("list_pages", note="Call list_pages with source='ad_account' and account_id."),
    "ads_get_pages_for_business": _covered("list_pages", note="Call list_pages with source='business' and business_id."),
    "ads_get_user_pages": _covered("list_pages", note="Call list_pages with source='user'."),
    "ads_get_field_context": _approximate(
        "get_field_reference",
        note="Field types, updatability and allowed values come from a table generated from Meta's SDK, because Meta removed live field metadata in Graph API v25. Descriptions are not included.",
    ),
    "ads_create_campaign": _covered("create_campaign", note="Creates a campaign, paused by default."),
    "ads_create_ad_set": _covered("create_ad_set", note="Creates an ad set, paused by default."),
    "ads_create_ad": _covered("create_ad", note="Creates an ad, paused by default."),
    "ads_update_entity": _covered(
        "update_campaign", "update_adset", "update_ad",
        note="Pick the tool for the object type. Budgets and bids have dedicated tools: update_campaign_budget, update_adset_budget, update_adset_bid_amount and the bid strategy tools.",
    ),
    "ads_activate_entity": _covered(
        "set_campaign_status", "set_adset_status", "set_ad_status",
        note="Pick the tool for the object type and pass status='ACTIVE'. Activating starts spending budget.",
    ),
    "ads_create_creative": _covered("create_ad_creative", note="Creates a reusable creative; upload images first with upload_creative_asset."),
    "ads_get_creatives": _covered("list_creatives", note="Lists creatives in an ad account."),
    "ads_get_creative_ads": _covered(
        "list_creative_ads",
        note="Meta has no server-side filter for this, so the tool scans the account's ads and reports how many were examined and whether the scan was complete.",
    ),
    "ads_get_ad_images": _covered("list_ad_images", note="Lists the account's image library."),
    "ads_get_ad_videos": _covered("list_ad_videos", note="Lists the account's video library."),
    "ads_get_ad_preview": _covered("preview_ad", note="Renders a preview for an ad, creative id or draft creative in a chosen placement format."),
    "ads_library_search": _covered("search_ads_archive", note="Searches the public Meta Ad Library."),
    "ads_get_ig_accounts": _covered("list_instagram_accounts", note="Lists Instagram accounts linked to an ad account."),
    "ads_get_ig_media": _covered(
        "list_instagram_media",
        note="Lists posts and reels, or stories with source='stories'. Only boost-eligible items by default; each item says whether it can be boosted.",
    ),
    "ads_boost_ig_post": _covered(
        "boost_instagram_post",
        note="Creates a creative from an existing Instagram post and a paused ad under an existing ad set.",
    ),
    "ads_get_ad_account_custom_audiences": _covered("list_audiences", note="Lists custom audiences, optionally by subtype."),
    "ads_get_custom_audience": _covered("get_audience", note="Returns one custom or lookalike audience."),
    "ads_get_custom_audience_adsets": _covered(
        "list_audience_adsets",
        note="Meta exposes the ads that use an audience, not the ad sets, so the tool groups those ads by ad set.",
    ),
    "ads_create_custom_audience": _covered(
        "create_custom_audience", "create_lookalike_audience",
        note="Use create_lookalike_audience for lookalikes and create_custom_audience for every other subtype.",
    ),
    "ads_update_custom_audience": _covered("update_custom_audience", note="Updates audience name, description and other metadata."),
    "ads_update_custom_audience_users": _covered(
        "add_audience_users", "remove_audience_users",
        note="Pick add or remove. Values are normalized and hashed on this machine; pass file_path so customer data never passes through the model.",
    ),
    "ads_delete_custom_audience": _covered("delete_audience", note="Deletes a custom audience."),
    # Catalogs
    "ads_catalog_create": _covered(
        "create_catalog",
        note="Creates the catalog. Load products afterwards with create_product_feed, create_product_feed_upload or create_catalog_product.",
    ),
    "ads_catalog_update_catalog": _covered("update_catalog", note="Edits catalog settings with before and after values."),
    "ads_catalog_list_catalogs": _covered("list_catalogs", note="Lists catalogs for a business or ad account, or returns one catalog with counts."),
    "ads_catalog_list_dpa_eligible_catalogs": _approximate(
        "list_dpa_eligible_catalogs",
        note="Readiness is judged here from product counts, product sets, connected event sources and must-fix diagnostics, with the rules stated in the response. It is not Meta's own eligibility ranking.",
    ),
    "ads_catalog_get_diagnostics": _covered("get_catalog_diagnostics", note="Errors and opportunities ordered by products affected."),
    "ads_catalog_get_dynamic_ads_health": _approximate(
        "get_catalog_dynamic_ads_health",
        note="Health is judged here from product counts, diagnostics and connected event sources, with the rules stated in the response.",
    ),
    "ads_catalog_get_data_sources": _covered("list_catalog_data_sources", note="Lists feeds, uploads and API sources for a catalog."),
    "ads_catalog_list_products": _covered("list_catalog_products", note="Lists products in a catalog or set, filtered, or returns one product."),
    "ads_catalog_product_create": _covered("create_catalog_product", note="Creates a product; new products start in staging visibility."),
    "ads_catalog_update_product": _covered("update_catalog_product", note="Edits product fields with before and after values."),
    "ads_catalog_delete_product": _covered("delete_catalog_product", note="Deletes a product."),
    "ads_catalog_get_suggested_filter": _approximate(
        "build_product_set_filter",
        note="Takes structured conditions, not plain English: the calling model translates the request into conditions. The tool then builds a valid filter and counts the products that match.",
    ),
    "ads_catalog_create_product_set": _covered("create_product_set", note="Creates a product set from a filter."),
    "ads_catalog_update_product_set": _covered("update_product_set", note="Edits a product set with before and after values."),
    "ads_catalog_list_product_sets": _covered("list_product_sets", note="Lists sets in a catalog, one set, or the sets containing a product."),
    "ads_catalog_product_set_delete": _covered("delete_product_set", note="Deletes a product set; products remain."),
    "ads_catalog_create_product_feed": _covered("create_product_feed", note="Registers a feed, optionally fetched from a URL on a schedule."),
    "ads_catalog_update_product_feed": _covered("update_product_feed", note="Edits feed name, defaults or schedules with before and after values."),
    "ads_catalog_list_product_feeds": _covered("list_product_feeds", note="Lists feeds, or one feed with settings and latest upload."),
    "ads_catalog_create_product_feed_upload_session": _covered("create_product_feed_upload", note="Starts an upload for a feed from a URL."),
    "ads_catalog_get_product_feed_upload_sessions": _covered("list_product_feed_uploads", note="Uploads with status, counts and errors ordered by rows affected."),
    "ads_catalog_product_feed_delete": _covered("delete_product_feed", note="Deletes a feed; its products are removed unless another source supplies them."),
    "ads_catalog_create_feed_rule": _covered("create_feed_rule", note="Creates an attribute mapping or transformation rule on a feed."),
    "ads_catalog_get_feed_rules": _covered("list_feed_rules", note="Lists rules on a feed."),
    "ads_catalog_product_feed_delete_rule": _covered("delete_feed_rule", note="Deletes a feed rule; the feed and products are unaffected."),
    "ads_catalog_event_source_get": _covered("list_catalog_event_sources", note="Pixels, apps and offline datasets connected to a catalog."),
    "ads_catalog_event_source_get_catalogs": _covered(
        "list_event_source_catalogs",
        note="Meta has no direct edge for this, so the tool checks the business's owned catalogs and reports how many it scanned.",
    ),
    "ads_catalog_event_source_get_health": _approximate(
        "get_catalog_event_source_health",
        note="Uses Meta's 28-day catalog event statistics and dynamic ads checks. The match rate figure and its threshold are calculated here; Meta does not publish either.",
    ),
    "ads_catalog_event_source_get_recommendations": _approximate(
        "get_catalog_event_source_recommendations",
        note="Candidates are the business's pixels, ranked here by Meta's dynamic ads checks and recent activity. Apps are not considered.",
    ),
    "ads_catalog_event_source_connect": _covered("connect_catalog_event_source", note="Connects an event source to a catalog."),
    "ads_catalog_event_source_disconnect": _covered("disconnect_catalog_event_source", note="Disconnects an event source from a catalog."),
    # Signals and datasets
    "ads_get_datasets": _covered("list_datasets", note="Pixels and datasets on an ad account or business."),
    "ads_get_dataset_details": _covered("get_dataset", note="One dataset's settings."),
    "ads_get_dataset_stats": _covered("get_dataset_stats", note="Event volume for up to 28 days, split by event, device or source."),
    "ads_get_dataset_quality": _covered(
        "get_dataset_quality",
        note="Event match quality, match-key coverage and freshness for web events. The cutoff used to flag a weak score is this server's choice and is stated in the response.",
    ),
    "ads_pixel_event_read": _unavailable(note=_PIXEL_RULE_READ_NOTE),
    "ads_pixel_event_create": _unavailable(note=_PIXEL_RULE_WRITE_NOTE),
    "ads_pixel_event_update": _unavailable(note=_PIXEL_RULE_WRITE_NOTE),
    "ads_pixel_event_delete": _unavailable(note=_PIXEL_RULE_WRITE_NOTE),
    "ads_pixel_parameter_read": _unavailable(note=_PIXEL_RULE_READ_NOTE),
    "ads_pixel_parameter_create": _unavailable(note=_PIXEL_RULE_WRITE_NOTE),
    "ads_pixel_parameter_update": _unavailable(note=_PIXEL_RULE_WRITE_NOTE),
    "ads_pixel_parameter_delete": _unavailable(note=_PIXEL_RULE_WRITE_NOTE),
    "ads_get_customconversions": _covered("list_custom_conversions", note="Custom conversions on an ad account."),
    # Help and troubleshooting
    "ads_get_errors": _covered(
        "get_delivery_errors",
        note="Delivery-blocking problems across an account or for named objects, ordered by severity and spend. Use get_native_optimization_signals for one object's full signals.",
    ),
    "ads_get_help_article": _unavailable(
        note="Meta has no public API for searching the Business Help Center. Search https://www.facebook.com/business/help directly.",
    ),
    # A/B tests and lift studies
    "ads_experiment_list_tests": _covered("list_ad_studies", note="A/B tests and lift studies on an account, business or one entity."),
    "ads_experiment_check_eligibility": _approximate(
        "check_ab_test_eligibility",
        note="Meta has no public eligibility endpoint. This is a local pre-check against Meta's documented limits, not Meta's verdict.",
    ),
    "ads_experiment_abtest_create_test": _covered(
        "create_ab_test",
        note="Creates the test on the business that owns the ad account, which is the only route Meta documents.",
    ),
    "ads_experiment_abtest_get_test": _covered("get_ad_study", note="Pass include_results=true for the winner and confidence."),
    "ads_experiment_abtest_update_test": _covered(
        "update_ad_study", "cancel_ad_study",
        note="Use update_ad_study to edit and cancel_ad_study to end or cancel.",
    ),
    "ads_experiment_lift_create_test": _covered("create_lift_study", note="Creates a conversion lift study on the owning business."),
    "ads_experiment_lift_get_test": _covered("get_ad_study", note="Pass include_results=true for lift and confidence."),
    # Activity logs
    "ads_account_get_activity_logs": _covered(
        "list_change_history", "export_change_history",
        note="list_change_history reads one page; export_change_history retrieves every event with explicit coverage.",
    ),
}


def resolve_official_tool(name: str) -> str | None:
    """Return this server's tool for an official name served by exactly one tool."""
    entry = OFFICIAL_TOOLS.get(name)
    if entry is None or entry.status == "unavailable" or len(entry.tools) != 1:
        return None
    return entry.tools[0]


def official_tool_guidance(name: str) -> str | None:
    """Explain an official name that cannot be called as a single tool here."""
    entry = OFFICIAL_TOOLS.get(name)
    if entry is None or resolve_official_tool(name) is not None:
        return None
    if entry.status == "unavailable":
        nearest = f" Nearest tool here: {', '.join(entry.tools)}." if entry.tools else ""
        return f"'{name}' is a Meta official tool with no equivalent here. {entry.note}{nearest}"
    return (
        f"'{name}' is a Meta official tool served here by several tools: "
        f"{', '.join(entry.tools)}. {entry.note}"
    )


def parity_summary() -> dict[str, object]:
    """Count entries by status and verification."""
    by_status: dict[str, int] = {"covered": 0, "approximate": 0, "unavailable": 0}
    by_verified: dict[str, int] = {"live": 0, "mocked": 0}
    for entry in OFFICIAL_TOOLS.values():
        by_status[entry.status] += 1
        if entry.verified is not None:
            by_verified[entry.verified] += 1
    return {"official_tools": len(OFFICIAL_TOOLS), "by_status": by_status, "by_verified": by_verified}
