"""Discovery tools for accounts and entities."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.money import from_minor_units, resolve_account_currency
from meta_ads_mcp.normalize import (
    blank_to_none,
    normalize_collection,
)
from meta_ads_mcp.schemas import collection_response
from meta_ads_mcp.tool_types import FieldList, StringList

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
    "account_id",
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
    "account_id",
    "name",
    "status",
    "effective_status",
    "campaign_id",
    "optimization_goal",
    "billing_event",
    "bid_amount",
    "bid_strategy",
    "bid_constraints",
    "daily_budget",
    "lifetime_budget",
    "start_time",
    "end_time",
    "targeting",
    "promoted_object",
]

AD_FIELDS = [
    "id",
    "account_id",
    "name",
    "status",
    "effective_status",
    "campaign_id",
    "adset_id",
    "bid_amount",
    "creative",
]

PAGE_FIELDS = [
    "id",
    "name",
    "category",
    "link",
    "tasks",
    "instagram_business_account",
]

INSTAGRAM_ACCOUNT_FIELDS = [
    "id",
    "name",
    "username",
    "profile_picture_url",
    "ig_id",
]


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve an ad account id, using the default when omitted."""
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


def _normalize_monetary_fields(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize every Graph minor-unit monetary field in-place."""
    for item in items:
        currency = item.get("currency")
        for key in (
            "daily_budget",
            "lifetime_budget",
            "amount_spent",
            "balance",
            "bid_amount",
        ):
            if key in item:
                item[key] = from_minor_units(item.get(key), currency, field_name=key)
    return items


async def _hydrate_and_normalize_monetary_fields(
    client: Any,
    items: list[dict[str, Any]],
    *,
    fallback_account_id: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve entity currencies by account, then normalize their monetary fields."""
    currency_by_account: dict[str, str] = {}
    monetary_fields = {
        "daily_budget",
        "lifetime_budget",
        "amount_spent",
        "balance",
        "bid_amount",
    }
    for item in items:
        if not any(
            key in item and item.get(key) not in (None, "", "null")
            for key in monetary_fields
        ):
            if item.get("account_id"):
                item["account_id"] = normalize_account_id(str(item["account_id"]))
            continue
        raw_account_id = item.get("account_id") or fallback_account_id
        if not raw_account_id:
            raise ValidationError(
                "Meta did not return account_id, so monetary fields cannot be normalized."
            )
        account_id = normalize_account_id(str(raw_account_id))
        currency = currency_by_account.get(account_id)
        if currency is None:
            currency = await resolve_account_currency(client, account_id)
            currency_by_account[account_id] = currency
        item["account_id"] = account_id
        item["currency"] = currency
    return _normalize_monetary_fields(items)


def _status_filter(effective_status: list[str] | None) -> dict[str, Any]:
    """Build a status filter query fragment."""
    if not effective_status:
        return {}
    return {"effective_status": effective_status}


def _name_filter(name_contains: str | None) -> dict[str, Any]:
    """Build one Graph-native name containment filter."""
    normalized = blank_to_none(name_contains)
    if normalized is None:
        return {}
    return {
        "filtering": [
            {
                "field": "name",
                "operator": "CONTAIN",
                "value": normalized,
            }
        ]
    }


def _page_params(limit: int, after: str | None) -> dict[str, Any]:
    """Build pagination params for page discovery."""
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    return params


def _campaign_suggested_next_tools(items: list[dict[str, Any]], account_id: str) -> dict[str, Any]:
    """Return compact routing hints after campaign discovery."""
    campaign_ids = [str(item.get("id")) for item in items[:5] if item.get("id")]
    if not campaign_ids:
        return {}
    first_campaign_id = campaign_ids[0]
    return {
        "campaign_health": {
            "tool": "get_campaign_optimization_snapshot",
            "arguments": {"campaign_id": first_campaign_id},
        },
        "compare_visible_campaigns": {
            "tool": "compare_performance",
            "arguments": {"level": "campaign", "object_ids": campaign_ids},
        },
        "whole_account_health": {
            "tool": "get_account_optimization_snapshot",
            "arguments": {"account_id": account_id},
        },
        "writes_catalog": {"tool": "list_mutation_tools", "arguments": {}},
    }


@mcp_server.tool()
async def list_ad_accounts(limit: int = 25, after: str | None = None) -> dict[str, Any]:
    """Use this first when the user needs to discover which ad accounts are available."""
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    payload = await client.list_objects("me", "adaccounts", fields=ACCOUNT_FIELDS, params=params)
    normalized = normalize_collection(payload)
    normalized["items"] = _normalize_monetary_fields(normalized["items"])
    return normalized


@mcp_server.tool()
async def get_ad_account(account_id: str) -> dict[str, Any]:
    """Use this when the user already has one account id and wants core metadata for that account."""
    client = get_graph_api_client()
    account = await client.get_object(_resolve_account_id(account_id), fields=ACCOUNT_FIELDS)
    return {
        "item": _normalize_monetary_fields([account])[0],
        "summary": {"count": 1},
    }


@mcp_server.tool()
async def list_campaigns(
    account_id: str | None = None,
    effective_status: StringList | None = None,
    name_contains: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """Discover campaign names and ids, with server-side name containment and status filters."""
    client = get_graph_api_client()
    account_id = blank_to_none(account_id)
    resolved_account_id = _resolve_account_id(account_id)
    params: dict[str, Any] = {
        "limit": limit,
        **_status_filter(effective_status),
        **_name_filter(name_contains),
    }
    if after:
        params["after"] = after
    payload = await client.list_objects(
        resolved_account_id,
        "campaigns",
        fields=CAMPAIGN_FIELDS,
        params=params,
    )
    normalized = normalize_collection(payload)
    normalized["items"] = await _hydrate_and_normalize_monetary_fields(
        client,
        normalized["items"],
        fallback_account_id=resolved_account_id,
    )
    suggestions = _campaign_suggested_next_tools(normalized["items"], resolved_account_id)
    if suggestions:
        normalized["suggested_next_tools"] = suggestions
    return normalized


@mcp_server.tool()
async def get_campaign(campaign_id: str) -> dict[str, Any]:
    """Use this when the user already has a campaign id and wants the current campaign configuration."""
    client = get_graph_api_client()
    campaign = await client.get_object(campaign_id, fields=CAMPAIGN_FIELDS)
    return {
        "item": (await _hydrate_and_normalize_monetary_fields(client, [campaign]))[0],
        "summary": {"count": 1},
    }


@mcp_server.tool()
async def list_adsets(
    account_id: str | None = None,
    campaign_id: str | None = None,
    effective_status: StringList | None = None,
    name_contains: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """Discover ad sets under one scope or the default account, with server-side name and status filters."""
    account_id = blank_to_none(account_id)
    campaign_id = blank_to_none(campaign_id)
    if account_id and campaign_id:
        raise ValidationError("Provide only one of account_id or campaign_id.")
    parent_id = campaign_id or _resolve_account_id(account_id)
    client = get_graph_api_client()
    params: dict[str, Any] = {
        "limit": limit,
        **_status_filter(effective_status),
        **_name_filter(name_contains),
    }
    if after:
        params["after"] = after
    payload = await client.list_objects(parent_id, "adsets", fields=ADSET_FIELDS, params=params)
    normalized = normalize_collection(payload)
    normalized["items"] = await _hydrate_and_normalize_monetary_fields(
        client,
        normalized["items"],
        fallback_account_id=parent_id if not campaign_id else None,
    )
    return normalized


@mcp_server.tool()
async def get_adset(adset_id: str) -> dict[str, Any]:
    """Use this when the user already has an ad set id and wants its current settings."""
    client = get_graph_api_client()
    adset = await client.get_object(adset_id, fields=ADSET_FIELDS)
    return {
        "item": (await _hydrate_and_normalize_monetary_fields(client, [adset]))[0],
        "summary": {"count": 1},
    }


@mcp_server.tool()
async def list_ads(
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    effective_status: StringList | None = None,
    name_contains: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """Discover ads under one scope, optionally filtering names and statuses server-side."""
    account_id = blank_to_none(account_id)
    campaign_id = blank_to_none(campaign_id)
    adset_id = blank_to_none(adset_id)
    scope_count = sum(value is not None for value in (account_id, campaign_id, adset_id))
    if scope_count > 1:
        raise ValidationError("Provide at most one of account_id, campaign_id, or adset_id.")
    parent_id = adset_id or campaign_id or _resolve_account_id(account_id)
    client = get_graph_api_client()
    params: dict[str, Any] = {
        "limit": limit,
        **_status_filter(effective_status),
        **_name_filter(name_contains),
    }
    if after:
        params["after"] = after
    payload = await client.list_objects(parent_id, "ads", fields=AD_FIELDS, params=params)
    normalized = normalize_collection(payload)
    normalized["items"] = await _hydrate_and_normalize_monetary_fields(
        client,
        normalized["items"],
        fallback_account_id=(
            parent_id if campaign_id is None and adset_id is None else None
        ),
    )
    return normalized


@mcp_server.tool()
async def get_ad(ad_id: str, include_creative_summary: bool = False) -> dict[str, Any]:
    """Use this when the user already has an ad id and wants ad metadata, optionally with a lightweight creative summary."""
    client = get_graph_api_client()
    fields = list(AD_FIELDS)
    if include_creative_summary:
        fields.remove("creative")
        fields.append(
            "creative{id,name,title,body,object_story_id,effective_object_story_id,"
            "effective_instagram_media_id,object_story_spec}"
        )
    ad = await client.get_object(ad_id, fields=fields)
    item = (await _hydrate_and_normalize_monetary_fields(client, [ad]))[0]
    return {"item": item, "summary": {"count": 1}}


@mcp_server.tool()
async def get_account_pages(
    account_id: str = "me",
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Use this before creative creation when the user needs Facebook Pages available to the account."""
    client = get_graph_api_client()
    requested_fields = fields or PAGE_FIELDS
    params = _page_params(limit, after)
    payload = await client.list_objects("me", "accounts", fields=requested_fields, params=params)
    normalized = normalize_collection(payload)
    normalized["summary"].update({"source": "accounts", "source_attempts": ["accounts"]})
    if account_id != "me":
        normalized["summary"]["account_id_context"] = normalize_account_id(account_id)
    return normalized


@mcp_server.tool()
async def list_instagram_accounts(
    account_id: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Use this before creative creation when the user needs Instagram identities linked to the account."""
    client = get_graph_api_client()
    account_id = blank_to_none(account_id)
    params = _page_params(limit, after)
    source_attempts = ["instagram_accounts"]
    try:
        payload = await client.list_objects(
            _resolve_account_id(account_id),
            "instagram_accounts",
            fields=fields or INSTAGRAM_ACCOUNT_FIELDS,
            params=params,
        )
        normalized = normalize_collection(payload)
        for item in normalized["items"]:
            if "profile_picture_url" in item or "profile_pic" in item:
                profile_picture_url = (
                    item.get("profile_picture_url") or item.get("profile_pic")
                )
                item["profile_picture_url"] = profile_picture_url
                item["profile_pic"] = profile_picture_url
        normalized["summary"].update({"source": "instagram_accounts", "source_attempts": source_attempts})
        return normalized
    except UnsupportedFeatureError:
        source_attempts.append("accounts.instagram_business_account")

    pages = await get_account_pages(
        account_id=account_id or "me",
        limit=limit,
        after=after,
        fields=[
            "id",
            "name",
            "instagram_business_account{id,ig_id,username,name,profile_picture_url}",
        ],
    )
    items: list[dict[str, Any]] = []
    for page in pages["items"]:
        instagram_account = page.get("instagram_business_account") or {}
        instagram_id = instagram_account.get("id") or instagram_account.get("ig_id")
        if not instagram_id:
            continue
        profile_picture_url = (
            instagram_account.get("profile_picture_url")
            or instagram_account.get("profile_pic")
        )
        items.append(
            {
                "id": instagram_id,
                "ig_id": instagram_account.get("ig_id") or instagram_id,
                "username": instagram_account.get("username"),
                "name": instagram_account.get("name") or page.get("name"),
                "profile_picture_url": profile_picture_url,
                "profile_pic": profile_picture_url,
                "page_id": page.get("id"),
                "page_name": page.get("name"),
            }
        )
    return collection_response(
        items,
        paging=pages["paging"],
        summary={
            "count": len(items),
            "source": "accounts.instagram_business_account",
            "source_attempts": source_attempts,
        },
    )
