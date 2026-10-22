"""Discovery tools for accounts and entities."""

from __future__ import annotations

import re
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
from meta_ads_mcp.tool_types import (
    FieldList, StrictStringList, StringList, coerce_strict_csv_string_list, normalize_field_list,
)

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

MONETARY_FIELDS = {
    "daily_budget", "lifetime_budget", "amount_spent", "balance", "bid_amount",
}

DESTINATION_CREATIVE_FIELDS = [
    "link_url", "object_url", "object_store_url", "template_url", "link_deep_link_url",
    "object_story_spec", "asset_feed_spec{link_urls}", "call_to_action",
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
    selected_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve entity currencies by account, then normalize their monetary fields."""
    currency_by_account: dict[str, str] = {}
    for item in items:
        if not any(
            key in item and item.get(key) not in (None, "", "null")
            for key in MONETARY_FIELDS
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
    _normalize_monetary_fields(items)
    if selected_fields is not None:
        for item in items:
            for dependency in ("account_id", "currency"):
                if dependency not in selected_fields:
                    item.pop(dependency, None)
    return items


def _entity_read_fields(fields: FieldList | None, defaults: list[str]) -> list[str]:
    """Include account context only when needed to decode selected money fields."""
    requested_fields = list(normalize_field_list(fields) or defaults)
    if MONETARY_FIELDS.intersection(requested_fields) and "account_id" not in requested_fields:
        requested_fields.append("account_id")
    return requested_fields


def _field_name(field: str) -> str:
    """Return a Graph selection's top-level name."""
    return re.split(r"[.{]", field, maxsplit=1)[0].strip()


def _ad_graph_fields(selected_fields: list[str]) -> list[str]:
    """Replace the local destination_urls projection with creative URL sources."""
    if "destination_urls" not in selected_fields:
        return list(selected_fields)
    graph_fields = [
        field for field in selected_fields
        if field != "destination_urls" and _field_name(field) != "creative"
    ]
    creative_fields = list(DESTINATION_CREATIVE_FIELDS)
    for field in selected_fields:
        if _field_name(field) != "creative" or "{" not in field:
            continue
        # Keep explicitly requested creative details. If two selections for one
        # structured field differ, fetch that whole field rather than dropping either.
        for selection in normalize_field_list(field.partition("{")[2].rsplit("}", 1)[0]) or []:
            name = _field_name(selection)
            position = next((i for i, value in enumerate(creative_fields) if _field_name(value) == name), None)
            if position is None:
                creative_fields.append(selection)
            elif creative_fields[position] != selection:
                creative_fields[position] = name
    graph_fields.append("creative{" + ",".join(creative_fields) + "}")
    return graph_fields


def _destination_urls(creative: dict[str, Any]) -> list[str]:
    """Extract configured destinations, excluding image/video/display URLs."""
    urls: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in urls:
            urls.append(value.strip())

    def add_call_to_action(value: Any) -> None:
        if isinstance(value, dict) and isinstance(value.get("value"), dict):
            add(value["value"].get("link"))

    for field in ("link_url", "object_url", "object_store_url", "template_url", "link_deep_link_url"):
        add(creative.get(field))
    add_call_to_action(creative.get("call_to_action"))
    story = creative.get("object_story_spec") or {}
    for field in ("link_data", "template_data", "video_data"):
        data = story.get(field) or {}
        add(data.get("link"))
        add_call_to_action(data.get("call_to_action"))
        for attachment in data.get("child_attachments") or []:
            add(attachment.get("link"))
            add_call_to_action(attachment.get("call_to_action"))
    asset_feed = creative.get("asset_feed_spec") or {}
    for link in asset_feed.get("link_urls") or []:
        for field in ("website_url", "deeplink_url"):
            add(link.get(field))
    return urls


def _project_ad_destinations(items: list[dict[str, Any]], fields: list[str]) -> None:
    """Keep destination-only responses compact while preserving requested creative data."""
    if "destination_urls" not in fields:
        return
    keep_creative = any(_field_name(field) == "creative" for field in fields)
    for item in items:
        item["destination_urls"] = _destination_urls(item.get("creative") or {})
        if not keep_creative:
            item.pop("creative", None)


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
async def get_ad_account(account_id: str, fields: FieldList | None = None) -> dict[str, Any]:
    """Inspect one account with selectable fields. Monetary amounts use major units of the account currency."""
    selected_fields = normalize_field_list(fields) or None
    requested_fields = list(selected_fields or ACCOUNT_FIELDS)
    if MONETARY_FIELDS.intersection(requested_fields) and "currency" not in requested_fields:
        requested_fields.append("currency")
    client = get_graph_api_client()
    account = await client.get_object(_resolve_account_id(account_id), fields=requested_fields)
    _normalize_monetary_fields([account])
    if selected_fields is not None and "currency" not in selected_fields:
        account.pop("currency", None)
    return {
        "item": account,
        "summary": {"count": 1},
    }


@mcp_server.tool()
async def list_campaigns(
    account_id: str | None = None,
    effective_status: StringList | None = None,
    name_contains: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Discover campaigns with selectable fields, server-side name containment, and status filters. Meta validates optional field availability."""
    requested_fields = normalize_field_list(fields) or CAMPAIGN_FIELDS
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
        fields=requested_fields,
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
async def get_campaign(campaign_id: str, fields: FieldList | None = None) -> dict[str, Any]:
    """Inspect one campaign's configuration with optional fields. Meta validates field availability."""
    requested_fields = _entity_read_fields(fields, CAMPAIGN_FIELDS)
    client = get_graph_api_client()
    campaign = await client.get_object(campaign_id, fields=requested_fields)
    return {
        "item": (await _hydrate_and_normalize_monetary_fields(
            client, [campaign], selected_fields=normalize_field_list(fields) or None,
        ))[0],
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
async def get_adset(adset_id: str, fields: FieldList | None = None) -> dict[str, Any]:
    """Inspect one ad set with selectable fields. Budgets and bids use major units of the account currency; ROAS floors remain raw x10000 integers."""
    client = get_graph_api_client()
    adset = await client.get_object(adset_id, fields=_entity_read_fields(fields, ADSET_FIELDS))
    return {
        "item": (await _hydrate_and_normalize_monetary_fields(
            client, [adset], selected_fields=normalize_field_list(fields) or None,
        ))[0],
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
    fields: FieldList | None = None,
    name_contains_any: StrictStringList | None = None,
    whole_term_match: bool = False,
) -> dict[str, Any]:
    """Discover ads with selectable fields. name_contains_any unions all pages of one Graph CONTAIN query per term, deduplicated by id; limit is the page size. Meta determines candidate case/Unicode matching, then local casefold and optional whole_term_match refine names (punctuation/underscores delimit terms). fields may include destination_urls for compact configured creative links without creative metadata; redirects, catalog substitutions, and url_tags are not resolved. Omitted status uses Meta defaults, not historical inventory."""
    terms = coerce_strict_csv_string_list(name_contains_any)
    if not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms):
        raise ValidationError("name_contains_any must contain nonblank strings.")
    if name_contains_any is not None and not terms:
        raise ValidationError("name_contains_any must contain at least one term.")
    query_terms: dict[str, str] = {}
    for term in terms:
        query_terms.setdefault(term.strip().casefold(), term.strip())
    terms = list(query_terms)
    if terms and blank_to_none(name_contains):
        raise ValidationError("Use name_contains or name_contains_any, not both.")
    if whole_term_match and not terms:
        raise ValidationError("whole_term_match requires name_contains_any.")
    if terms and blank_to_none(after):
        raise ValidationError("name_contains_any retrieves all term pages; omit after.")
    if limit < 1:
        raise ValidationError("limit must be positive.")
    account_id = blank_to_none(account_id)
    campaign_id = blank_to_none(campaign_id)
    adset_id = blank_to_none(adset_id)
    scope_count = sum(value is not None for value in (account_id, campaign_id, adset_id))
    if scope_count > 1:
        raise ValidationError("Provide at most one of account_id, campaign_id, or adset_id.")
    parent_id = adset_id or campaign_id or _resolve_account_id(account_id)
    selected_fields = normalize_field_list(fields) or None
    requested_fields = list(selected_fields or (
        ["id", "name", "status", "effective_status", "campaign_id", "adset_id"]
        if terms else list(AD_FIELDS)
    ))
    if terms:
        for field in ("id", "name"):
            if field not in requested_fields:
                requested_fields.append(field)
    graph_fields = _entity_read_fields(_ad_graph_fields(requested_fields), AD_FIELDS)
    client = get_graph_api_client()
    params: dict[str, Any] = {
        "limit": min(limit, 1000) if terms else limit,
        **_status_filter(effective_status),
        **_name_filter(name_contains),
    }
    if after:
        params["after"] = after
    if terms:
        # Graph combines filters with AND. Separate CONTAIN queries implement OR
        # without scanning every ad in the account or imposing an inventory cap.
        scanned_by_id: dict[str, dict[str, Any]] = {}
        for term in terms:
            term_params = {**params, **_name_filter(query_terms[term])}
            seen_cursors: set[str] = set()
            while True:
                payload = await client.list_objects(parent_id, "ads", fields=graph_fields, params=term_params)
                page = normalize_collection(payload)
                for item in page["items"]:
                    if not item.get("id"):
                        raise ValidationError("Name search cannot deduplicate a Graph result without an ad id. No partial result was returned.")
                    scanned_by_id.setdefault(str(item["id"]), item)
                if not page["paging"].get("next"):
                    break
                cursor = page["paging"].get("after")
                if not cursor or cursor in seen_cursors:
                    raise ValidationError("Name search pagination could not finish: missing or repeated cursor. No partial result was returned.")
                seen_cursors.add(cursor)
                term_params["after"] = cursor
        patterns = {
            term: re.compile(r"(?<![^\W_])" + re.escape(term) + r"(?![^\W_])")
            for term in terms
        } if whole_term_match else {}
        matched = []
        for item in scanned_by_id.values():
            name = str(item.get("name") or "").casefold()
            matches = [term for term in terms if (
                patterns[term].search(name) if whole_term_match else term in name
            )]
            if matches:
                matched.append({**item, "matched_terms": matches})
        normalized = collection_response(matched, summary={
            "count": len(matched), "scanned_count": len(scanned_by_id),
            "name_terms": terms, "match_mode": "whole_term" if whole_term_match else "substring",
            "name_filtering": "graph_contain",
        })
    else:
        payload = await client.list_objects(parent_id, "ads", fields=graph_fields, params=params)
        normalized = normalize_collection(payload)
    normalized["summary"].update({
        "complete": not bool(normalized["paging"].get("next")),
        "effective_status_filter": effective_status or None,
        "visibility": "explicit_status_filter" if effective_status else "meta_default",
    })
    normalized["items"] = await _hydrate_and_normalize_monetary_fields(
        client,
        normalized["items"],
        fallback_account_id=(
            parent_id if campaign_id is None and adset_id is None else None
        ),
        selected_fields=selected_fields,
    )
    _project_ad_destinations(normalized["items"], requested_fields)
    return normalized


@mcp_server.tool()
async def get_ad(
    ad_id: str,
    include_creative_summary: bool = False,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Inspect one ad with selectable fields, optionally including a creative summary. fields may include destination_urls for configured creative links (no redirect/catalog resolution or url_tags expansion). Bid amounts use major units of the account currency."""
    client = get_graph_api_client()
    selected_fields = normalize_field_list(fields) or None
    requested_fields = list(selected_fields or AD_FIELDS)
    if include_creative_summary:
        requested_fields = [field for field in requested_fields if _field_name(field) != "creative"]
        requested_fields.append(
            "creative{id,name,title,body,object_story_id,effective_object_story_id,"
            "effective_instagram_media_id,object_story_spec}"
        )
    ad = await client.get_object(ad_id, fields=_entity_read_fields(_ad_graph_fields(requested_fields), AD_FIELDS))
    item = (await _hydrate_and_normalize_monetary_fields(
        client, [ad], selected_fields=selected_fields,
    ))[0]
    _project_ad_destinations([item], requested_fields)
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
