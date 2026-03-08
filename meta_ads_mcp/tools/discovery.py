"""Discovery tools for accounts and entities."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import normalize_budget_value, normalize_collection

ACCOUNT_FIELDS = [
    "id",
    "name",
    "account_id",
    "account_status",
    "currency",
    "timezone_name",
    "amount_spent",
    "balance",
    "business_name",
]

CAMPAIGN_FIELDS = [
    "id",
    "name",
    "status",
    "effective_status",
    "objective",
    "buying_type",
    "bid_strategy",
    "daily_budget",
    "lifetime_budget",
    "special_ad_categories",
]

ADSET_FIELDS = [
    "id",
    "name",
    "status",
    "effective_status",
    "campaign_id",
    "optimization_goal",
    "billing_event",
    "bid_strategy",
    "daily_budget",
    "lifetime_budget",
    "targeting",
]

AD_FIELDS = [
    "id",
    "name",
    "status",
    "effective_status",
    "campaign_id",
    "adset_id",
    "creative",
]


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve an ad account id, using the default when omitted."""
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


def _normalize_budgets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize budget fields in-place."""
    for item in items:
        currency = item.get("currency")
        for key in ("daily_budget", "lifetime_budget", "amount_spent", "balance"):
            if key in item:
                item[key] = normalize_budget_value(item.get(key), currency)
    return items


def _status_filter(effective_status: list[str] | None) -> dict[str, Any]:
    """Build a status filter query fragment."""
    if not effective_status:
        return {}
    return {"effective_status": effective_status}


@mcp_server.tool()
async def list_ad_accounts(limit: int = 25, after: str | None = None) -> dict[str, Any]:
    """List accessible ad accounts."""
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    payload = await client.list_objects("me", "adaccounts", fields=ACCOUNT_FIELDS, params=params)
    normalized = normalize_collection(payload)
    normalized["items"] = _normalize_budgets(normalized["items"])
    return normalized


@mcp_server.tool()
async def get_ad_account(account_id: str) -> dict[str, Any]:
    """Get an ad account."""
    client = get_graph_api_client()
    account = await client.get_object(_resolve_account_id(account_id), fields=ACCOUNT_FIELDS)
    return {"item": _normalize_budgets([account])[0], "summary": {"count": 1}}


@mcp_server.tool()
async def list_campaigns(
    account_id: str | None = None,
    effective_status: list[str] | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """List campaigns for an ad account."""
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit, **_status_filter(effective_status)}
    if after:
        params["after"] = after
    payload = await client.list_objects(
        _resolve_account_id(account_id),
        "campaigns",
        fields=CAMPAIGN_FIELDS,
        params=params,
    )
    normalized = normalize_collection(payload)
    normalized["items"] = _normalize_budgets(normalized["items"])
    return normalized


@mcp_server.tool()
async def get_campaign(campaign_id: str) -> dict[str, Any]:
    """Get a campaign."""
    client = get_graph_api_client()
    campaign = await client.get_object(campaign_id, fields=CAMPAIGN_FIELDS)
    return {"item": _normalize_budgets([campaign])[0], "summary": {"count": 1}}


@mcp_server.tool()
async def list_adsets(
    account_id: str | None = None,
    campaign_id: str | None = None,
    effective_status: list[str] | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """List ad sets by account or campaign."""
    if not account_id and not campaign_id:
        raise ValidationError("Provide account_id or campaign_id.")
    parent_id = campaign_id or _resolve_account_id(account_id)
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit, **_status_filter(effective_status)}
    if after:
        params["after"] = after
    payload = await client.list_objects(parent_id, "adsets", fields=ADSET_FIELDS, params=params)
    normalized = normalize_collection(payload)
    normalized["items"] = _normalize_budgets(normalized["items"])
    return normalized


@mcp_server.tool()
async def get_adset(adset_id: str) -> dict[str, Any]:
    """Get an ad set."""
    client = get_graph_api_client()
    adset = await client.get_object(adset_id, fields=ADSET_FIELDS)
    return {"item": _normalize_budgets([adset])[0], "summary": {"count": 1}}


@mcp_server.tool()
async def list_ads(
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    effective_status: list[str] | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """List ads by account, campaign, or ad set."""
    scope_count = sum(value is not None for value in (account_id, campaign_id, adset_id))
    if scope_count != 1:
        raise ValidationError("Provide exactly one of account_id, campaign_id, or adset_id.")
    parent_id = adset_id or campaign_id or _resolve_account_id(account_id)
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit, **_status_filter(effective_status)}
    if after:
        params["after"] = after
    payload = await client.list_objects(parent_id, "ads", fields=AD_FIELDS, params=params)
    return normalize_collection(payload)


@mcp_server.tool()
async def get_ad(ad_id: str, include_creative_summary: bool = False) -> dict[str, Any]:
    """Get an ad, optionally with creative details."""
    client = get_graph_api_client()
    fields = list(AD_FIELDS)
    if include_creative_summary:
        fields[-1] = "creative{id,name,title,body,object_story_spec}"
    ad = await client.get_object(ad_id, fields=fields)
    return {"item": ad, "summary": {"count": 1}}

