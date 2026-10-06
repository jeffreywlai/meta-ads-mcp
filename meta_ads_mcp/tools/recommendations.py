"""Recommendation and opportunity tools."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from time import monotonic

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.input_compat import resolve_identifier_alias
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.tool_types import StrictStringList, normalize_recommendation_filters

_RECOMMENDATION_CACHE_TTL_SECONDS = 15.0
_RECOMMENDATION_CACHE_MAX_ENTRIES = 128
_RecommendationCacheKey = tuple[
    str, str, str, str, int, str, tuple[str, ...] | None, tuple[str, ...] | None
]
_RECOMMENDATION_CACHE: dict[
    _RecommendationCacheKey,
    tuple[float, dict[str, object]],
] = {}


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
    normalized["complete"] = not bool(normalized["paging"].get("next"))
    return normalized


def _cache_key(
    *,
    account_id: str,
    campaign_id: str | None,
    limit: int,
    after: str | None,
    recommendation_names: list[str] | None,
    recommendation_stages: list[str] | None,
) -> _RecommendationCacheKey:
    """Scope cached results to the API, token, target, filters, and page."""
    settings = get_settings()
    return (
        settings.access_token or "",
        settings.api_version,
        account_id,
        campaign_id or "",
        limit,
        after or "",
        tuple(recommendation_names) if recommendation_names is not None else None,
        tuple(recommendation_stages) if recommendation_stages is not None else None,
    )


def _get_cached_recommendations(
    key: _RecommendationCacheKey,
) -> dict[str, object] | None:
    """Return a deep-copied cached recommendation payload when it is still fresh."""
    cached = _RECOMMENDATION_CACHE.get(key)
    if cached is None:
        return None
    expires_at, payload = cached
    if expires_at < monotonic():
        _RECOMMENDATION_CACHE.pop(key, None)
        return None
    return deepcopy(payload)


def _store_cached_recommendations(
    key: _RecommendationCacheKey,
    payload: dict[str, object],
) -> None:
    """Store a normalized recommendation payload for a short time."""
    now = monotonic()
    expired_keys = [
        cached_key
        for cached_key, (expires_at, _payload) in _RECOMMENDATION_CACHE.items()
        if expires_at <= now
    ]
    for expired_key in expired_keys:
        _RECOMMENDATION_CACHE.pop(expired_key, None)
    while (
        key not in _RECOMMENDATION_CACHE
        and len(_RECOMMENDATION_CACHE) >= _RECOMMENDATION_CACHE_MAX_ENTRIES
    ):
        _RECOMMENDATION_CACHE.pop(next(iter(_RECOMMENDATION_CACHE)))
    _RECOMMENDATION_CACHE[key] = (
        now + _RECOMMENDATION_CACHE_TTL_SECONDS,
        deepcopy(payload),
    )


async def _recommendation_collection(
    *,
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Fetch recommendations and return a normalized supported/unsupported response."""
    recommendation_names, recommendation_stages = normalize_recommendation_filters(
        recommendation_names, recommendation_stages
    )
    resolved_account_id = _resolve_account_id(account_id)
    after = blank_to_none(after)
    cache_key = _cache_key(
        account_id=resolved_account_id,
        campaign_id=campaign_id,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )
    if not refresh:
        cached = _get_cached_recommendations(cache_key)
        if cached is not None:
            return cached

    client = get_graph_api_client()
    try:
        request_options: dict[str, object] = {"campaign_id": campaign_id}
        if limit != 25:
            request_options["limit"] = limit
        if after:
            request_options["after"] = after
        if recommendation_names is not None:
            request_options["recommendation_names"] = recommendation_names
        if recommendation_stages is not None:
            request_options["recommendation_stages"] = recommendation_stages
        payload = await client.get_recommendations(
            resolved_account_id,
            **request_options,
        )
    except UnsupportedFeatureError as exc:
        result = {
            "supported": False,
            "reason": str(exc),
            "items": [],
            "paging": {"before": None, "after": None, "next": None},
            "complete": True,
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
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Return one filtered opportunity category with stable summary metadata."""
    result = await _recommendation_collection(
        account_id=account_id,
        campaign_id=campaign_id,
        refresh=refresh,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )
    if not result["supported"]:
        return {**result, "category": category}
    items = [item for item in result["items"] if category in item.get("opportunity_categories", [])]
    return {
        "supported": True,
        "category": category,
        "items": items,
        "paging": result.get(
            "paging",
            {"before": None, "after": None, "next": None},
        ),
        "complete": result.get("complete", True),
        "summary": {
            "count": len(items),
            "filtered_from_total": result["summary"]["count"],
            "category_counts": result["summary"].get("category_counts", {}),
        },
    }


@mcp_server.tool()
async def get_recommendations(
    account_id: str | None = None,
    object_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Scan Meta-native recommendations before category-specific tools. Optional recommendation_names and recommendation_stages (MFR/PCR/PFR) filter at Meta; names are validated by Meta."""
    account_id = resolve_identifier_alias(
        account_id,
        object_id,
        primary_name="account_id",
        alias_name="object_id",
    )
    return await _recommendation_collection(
        account_id=account_id,
        campaign_id=campaign_id,
        refresh=refresh,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )


@mcp_server.tool()
async def get_budget_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Find budget, spend, or scaling opportunities. Optional recommendation_names and recommendation_stages (MFR/PCR/PFR) filter at Meta before local category matching."""
    return await _typed_opportunities(
        "budget",
        account_id=account_id,
        campaign_id=campaign_id,
        refresh=refresh,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )


@mcp_server.tool()
async def get_creative_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Find creative asset, copy, or format opportunities. Optional recommendation_names and recommendation_stages (MFR/PCR/PFR) filter at Meta before local category matching."""
    return await _typed_opportunities(
        "creative",
        account_id=account_id,
        campaign_id=campaign_id,
        refresh=refresh,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )


@mcp_server.tool()
async def get_audience_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Find audience or targeting opportunities. Optional recommendation_names and recommendation_stages (MFR/PCR/PFR) filter at Meta before local category matching."""
    return await _typed_opportunities(
        "audience",
        account_id=account_id,
        campaign_id=campaign_id,
        refresh=refresh,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )


@mcp_server.tool()
async def get_delivery_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Find delivery, reach, or learning opportunities. Optional recommendation_names and recommendation_stages (MFR/PCR/PFR) filter at Meta before local category matching."""
    return await _typed_opportunities(
        "delivery",
        account_id=account_id,
        campaign_id=campaign_id,
        refresh=refresh,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )


@mcp_server.tool()
async def get_bidding_opportunities(
    account_id: str | None = None,
    campaign_id: str | None = None,
    refresh: bool = False,
    limit: int = 25,
    after: str | None = None,
    recommendation_names: StrictStringList | None = None,
    recommendation_stages: StrictStringList | None = None,
) -> dict[str, object]:
    """Find bid-cap, cost-cap, or bidding-strategy opportunities. Optional recommendation_names and recommendation_stages (MFR/PCR/PFR) filter at Meta before local category matching."""
    return await _typed_opportunities(
        "bidding",
        account_id=account_id,
        campaign_id=campaign_id,
        refresh=refresh,
        limit=limit,
        after=after,
        recommendation_names=recommendation_names,
        recommendation_stages=recommendation_stages,
    )
