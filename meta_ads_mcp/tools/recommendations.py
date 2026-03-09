"""Recommendation and opportunity tools."""

from __future__ import annotations

from collections import Counter

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import normalize_collection


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve account id from input or default config."""
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


OPPORTUNITY_KEYWORDS = {
    "budget": ("budget", "spend", "pacing", "scale"),
    "creative": ("creative", "image", "video", "asset", "copy", "text", "ad creative"),
    "audience": ("audience", "targeting", "interest", "lookalike", "broad", "geo", "demographic"),
    "delivery": ("delivery", "reach", "frequency", "learning", "overlap", "underdelivery", "auction"),
    "bidding": ("bid", "bidding", "cost cap", "bid cap", "target cost", "target roas", "highest value"),
}

TYPE_CATEGORY_OVERRIDES = {
    "advantage_plus_audience": ["audience"],
    "value_optimization_goal": ["bidding"],
    "fragmentation": ["delivery"],
    "reels_pc_recommendation": ["creative"],
    "aplusc_standard_enhancements_bundle": ["creative"],
    "advantage_plus_catalog_ads": ["creative"],
}


def _flatten_recommendation_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Expand nested recommendation containers into direct recommendation items."""
    flattened: list[dict[str, object]] = []
    for raw_item in items:
        nested = raw_item.get("recommendations")
        if isinstance(nested, list) and nested:
            for nested_item in nested:
                if not isinstance(nested_item, dict):
                    continue
                item = dict(nested_item)
                recommendation_content = nested_item.get("recommendation_content")
                if isinstance(recommendation_content, dict):
                    item.setdefault("title", recommendation_content.get("title"))
                    item.setdefault("message", recommendation_content.get("body"))
                    item.setdefault("lift_estimate", recommendation_content.get("lift_estimate"))
                    item.setdefault("opportunity_score_lift", recommendation_content.get("opportunity_score_lift"))
                flattened.append(item)
            continue
        flattened.append(dict(raw_item))
    return flattened


def _recommendation_text(item: dict[str, object]) -> str:
    """Flatten recommendation text-like fields into one lowercase string."""
    recommendation_content = item.get("recommendation_content")
    content = recommendation_content if isinstance(recommendation_content, dict) else {}
    parts = [
        item.get("recommendation_type"),
        item.get("type"),
        item.get("category"),
        item.get("title"),
        item.get("name"),
        item.get("message"),
        item.get("description"),
        content.get("title"),
        content.get("body"),
        content.get("lift_estimate"),
    ]
    return " ".join(str(part) for part in parts if part).lower().replace("_", " ")


def _opportunity_categories(item: dict[str, object]) -> list[str]:
    """Infer stable opportunity categories from recommendation text and fields."""
    raw_type = str(item.get("type") or item.get("recommendation_type") or "").lower()
    categories = list(TYPE_CATEGORY_OVERRIDES.get(raw_type, []))
    text = _recommendation_text(item)
    categories.extend(
        name for name, keywords in OPPORTUNITY_KEYWORDS.items() if any(keyword in text for keyword in keywords)
    )
    categories = sorted(set(categories))
    if not categories:
        categories.append("other")
    return categories


def _normalize_recommendations(payload: dict[str, object]) -> dict[str, object]:
    """Annotate recommendation items with inferred categories and summary counts."""
    normalized = normalize_collection(payload)
    normalized["items"] = _flatten_recommendation_items(normalized["items"])
    items: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for raw_item in normalized["items"]:
        item = dict(raw_item)
        categories = _opportunity_categories(item)
        item["opportunity_categories"] = categories
        items.append(item)
        counts.update(categories)
    normalized["items"] = items
    normalized["summary"]["count"] = len(items)
    normalized["summary"]["category_counts"] = dict(sorted(counts.items()))
    return normalized


async def _recommendation_collection(
    *,
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Fetch recommendations and return a normalized supported/unsupported response."""
    client = get_graph_api_client()
    try:
        payload = await client.get_recommendations(
            _resolve_account_id(account_id),
            campaign_id=campaign_id,
        )
    except UnsupportedFeatureError as exc:
        return {
            "supported": False,
            "reason": str(exc),
            "items": [],
            "summary": {"count": 0, "category_counts": {}},
        }
    return {"supported": True, **_normalize_recommendations(payload)}


async def _typed_opportunities(
    category: str,
    *,
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Return one filtered opportunity category with stable summary metadata."""
    result = await _recommendation_collection(account_id=account_id, campaign_id=campaign_id)
    if not result["supported"]:
        return {**result, "category": category}
    items = [item for item in result["items"] if category in item.get("opportunity_categories", [])]
    return {
        "supported": True,
        "category": category,
        "items": items,
        "summary": {
            "count": len(items),
            "filtered_from_total": result["summary"]["count"],
            "category_counts": result["summary"].get("category_counts", {}),
        },
    }


@mcp_server.tool()
async def get_recommendations(
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user wants Meta-native recommendations or opportunity surfaces for an account or campaign."""
    return await _recommendation_collection(account_id=account_id, campaign_id=campaign_id)


@mcp_server.tool()
async def get_budget_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user specifically wants budget, spend, or scaling opportunities from Meta's recommendation surface."""
    return await _typed_opportunities("budget", account_id=account_id, campaign_id=campaign_id)


@mcp_server.tool()
async def get_creative_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user wants creative-specific opportunities such as asset, copy, or format improvements."""
    return await _typed_opportunities("creative", account_id=account_id, campaign_id=campaign_id)


@mcp_server.tool()
async def get_audience_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user wants audience or targeting opportunities rather than general recommendations."""
    return await _typed_opportunities("audience", account_id=account_id, campaign_id=campaign_id)


@mcp_server.tool()
async def get_delivery_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user wants delivery, reach, or learning-related opportunities from the recommendation surface."""
    return await _typed_opportunities("delivery", account_id=account_id, campaign_id=campaign_id)


@mcp_server.tool()
async def get_bidding_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user wants bid-cap, cost-cap, or bidding-strategy opportunities from Meta's recommendations."""
    return await _typed_opportunities("bidding", account_id=account_id, campaign_id=campaign_id)
