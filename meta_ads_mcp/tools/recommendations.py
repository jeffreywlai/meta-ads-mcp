"""Recommendation and opportunity tools."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from time import monotonic

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import normalize_collection

_RECOMMENDATION_CACHE_TTL_SECONDS = 15.0
_RECOMMENDATION_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, object]]] = {}


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


def _dedupe_recommendation_item(raw_item: dict[str, object]) -> dict[str, object]:
    """Promote useful recommendation content fields and drop duplicate text wrappers."""
    item = dict(raw_item)
    recommendation_content = item.get("recommendation_content")
    content = recommendation_content if isinstance(recommendation_content, dict) else {}

    promoted_title = item.get("title") or content.get("title")
    promoted_body = item.get("body") or content.get("body") or item.get("message") or item.get("description")
    promoted_lift_estimate = item.get("lift_estimate") or content.get("lift_estimate")
    promoted_opportunity_score_lift = item.get("opportunity_score_lift") or content.get("opportunity_score_lift")

    if promoted_title:
        item["title"] = promoted_title
    if promoted_body:
        item["body"] = promoted_body
    if promoted_lift_estimate:
        item["lift_estimate"] = promoted_lift_estimate
    if promoted_opportunity_score_lift:
        item["opportunity_score_lift"] = promoted_opportunity_score_lift

    if item.get("message") == item.get("body"):
        item.pop("message", None)
    if item.get("description") == item.get("body"):
        item.pop("description", None)

    if content:
        remaining_content = {
            key: value
            for key, value in content.items()
            if (
                (key == "title" and value != item.get("title"))
                or (key == "body" and value != item.get("body"))
                or (key == "lift_estimate" and value != item.get("lift_estimate"))
                or (key == "opportunity_score_lift" and value != item.get("opportunity_score_lift"))
                or key not in {"title", "body", "lift_estimate", "opportunity_score_lift"}
            )
        }
        if remaining_content:
            item["recommendation_content"] = remaining_content
        else:
            item.pop("recommendation_content", None)

    return item


def _recommendation_text(item: dict[str, object]) -> str:
    """Flatten recommendation text-like fields into one lowercase string."""
    parts = [
        item.get("recommendation_type"),
        item.get("type"),
        item.get("category"),
        item.get("title"),
        item.get("name"),
        item.get("body"),
        item.get("message"),
        item.get("description"),
        item.get("lift_estimate"),
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
        item = _dedupe_recommendation_item(raw_item)
        categories = _opportunity_categories(item)
        item["opportunity_categories"] = categories
        items.append(item)
        counts.update(categories)
    normalized["items"] = items
    normalized["summary"]["count"] = len(items)
    normalized["summary"]["category_counts"] = dict(sorted(counts.items()))
    return normalized


def _cache_key(*, account_id: str, campaign_id: str | None) -> tuple[str, str, str]:
    """Build a short-lived cache key scoped to the current token and target ids."""
    return (
        get_settings().access_token or "",
        account_id,
        campaign_id or "",
    )


def _get_cached_recommendations(key: tuple[str, str, str]) -> dict[str, object] | None:
    """Return a deep-copied cached recommendation payload when it is still fresh."""
    cached = _RECOMMENDATION_CACHE.get(key)
    if cached is None:
        return None
    expires_at, payload = cached
    if expires_at < monotonic():
        _RECOMMENDATION_CACHE.pop(key, None)
        return None
    return deepcopy(payload)


def _store_cached_recommendations(key: tuple[str, str, str], payload: dict[str, object]) -> None:
    """Store a normalized recommendation payload for a short time."""
    _RECOMMENDATION_CACHE[key] = (
        monotonic() + _RECOMMENDATION_CACHE_TTL_SECONDS,
        deepcopy(payload),
    )


async def _recommendation_collection(
    *,
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Fetch recommendations and return a normalized supported/unsupported response."""
    resolved_account_id = _resolve_account_id(account_id)
    cache_key = _cache_key(account_id=resolved_account_id, campaign_id=campaign_id)
    if not refresh:
        cached = _get_cached_recommendations(cache_key)
        if cached is not None:
            return cached

    client = get_graph_api_client()
    try:
        payload = await client.get_recommendations(
            resolved_account_id,
            campaign_id=campaign_id,
        )
    except UnsupportedFeatureError as exc:
        result = {
            "supported": False,
            "reason": str(exc),
            "items": [],
            "summary": {"count": 0, "category_counts": {}},
        }
        _store_cached_recommendations(cache_key, result)
        return result
    result = {"supported": True, **_normalize_recommendations(payload)}
    _store_cached_recommendations(cache_key, result)
    return result


async def _typed_opportunities(
    category: str,
    *,
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Return one filtered opportunity category with stable summary metadata."""
    result = await _recommendation_collection(account_id=account_id, campaign_id=campaign_id, refresh=refresh)
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
    refresh: bool = False,
) -> dict[str, object]:
    """Use this for a broad Meta-native opportunity scan. Prefer this once before category-specific opportunity tools."""
    return await _recommendation_collection(account_id=account_id, campaign_id=campaign_id, refresh=refresh)


@mcp_server.tool()
async def get_budget_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Use this only when the user specifically wants budget, spend, or scaling opportunities from Meta's recommendation surface."""
    return await _typed_opportunities("budget", account_id=account_id, campaign_id=campaign_id, refresh=refresh)


@mcp_server.tool()
async def get_creative_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Use this only when the user wants creative-specific opportunities such as asset, copy, or format improvements."""
    return await _typed_opportunities("creative", account_id=account_id, campaign_id=campaign_id, refresh=refresh)


@mcp_server.tool()
async def get_audience_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Use this only when the user wants audience or targeting opportunities rather than general recommendations."""
    return await _typed_opportunities("audience", account_id=account_id, campaign_id=campaign_id, refresh=refresh)


@mcp_server.tool()
async def get_delivery_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Use this only when the user wants delivery, reach, or learning-related opportunities from the recommendation surface."""
    return await _typed_opportunities("delivery", account_id=account_id, campaign_id=campaign_id, refresh=refresh)


@mcp_server.tool()
async def get_bidding_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Use this only when the user wants bid-cap, cost-cap, or bidding-strategy opportunities from Meta's recommendations."""
    return await _typed_opportunities("bidding", account_id=account_id, campaign_id=campaign_id, refresh=refresh)
