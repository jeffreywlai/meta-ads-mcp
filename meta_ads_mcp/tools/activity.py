"""Activity and change-history tools."""

from __future__ import annotations

import json
from typing import Any

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none, normalize_collection

DEFAULT_ACTIVITY_FIELDS = [
    "actor_id",
    "actor_name",
    "application_id",
    "application_name",
    "date_time_in_timezone",
    "event_time",
    "event_type",
    "extra_data",
    "object_id",
    "object_name",
    "object_type",
    "translated_event_type",
]

LEVEL_ALIASES = {
    "account": "account",
    "campaign": "campaign",
    "adset": "adset",
    "ad_set": "adset",
    "adgroup": "adset",
    "ad_group": "adset",
    "ad": "ad",
}


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve an ad account id, using the default when omitted."""
    account_id = blank_to_none(account_id)
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


def _normalize_level(level: str | None) -> str | None:
    """Normalize level aliases while preserving omitted levels."""
    level = blank_to_none(level)
    if level is None:
        return None
    normalized = LEVEL_ALIASES.get(level.lower())
    if normalized is None:
        raise ValidationError(f"level must be one of {sorted(set(LEVEL_ALIASES.values()))}.")
    return normalized


def _resolve_scope(
    *,
    level: str | None,
    object_id: str | None,
    account_id: str | None,
    campaign_id: str | None,
    adset_id: str | None,
    ad_id: str | None,
) -> tuple[str, str]:
    """Resolve generic or entity-specific scope inputs into one parent id."""
    normalized_level = _normalize_level(level)
    normalized_object_id = blank_to_none(object_id)
    alias_candidates = [
        ("account", blank_to_none(account_id)),
        ("campaign", blank_to_none(campaign_id)),
        ("adset", blank_to_none(adset_id)),
        ("ad", blank_to_none(ad_id)),
    ]
    provided_aliases = [(candidate_level, candidate_id) for candidate_level, candidate_id in alias_candidates if candidate_id]
    if len(provided_aliases) > 1:
        raise ValidationError("Provide only one of account_id, campaign_id, adset_id, or ad_id.")

    if normalized_object_id is not None:
        if normalized_level is None:
            raise ValidationError("Provide level when using object_id.")
        resolved_object_id = (
            _resolve_account_id(normalized_object_id) if normalized_level == "account" else normalized_object_id
        )
        if provided_aliases:
            alias_level, alias_object_id = provided_aliases[0]
            resolved_alias_id = _resolve_account_id(alias_object_id) if alias_level == "account" else alias_object_id
            if alias_level != normalized_level or resolved_alias_id != resolved_object_id:
                raise ValidationError("Conflicting scope arguments. Use either level/object_id or one entity-specific id.")
        return normalized_level, resolved_object_id

    if provided_aliases:
        alias_level, alias_object_id = provided_aliases[0]
        if normalized_level is not None and normalized_level != alias_level:
            raise ValidationError("Conflicting scope arguments. Use either level/object_id or one entity-specific id.")
        resolved_alias_id = _resolve_account_id(alias_object_id) if alias_level == "account" else alias_object_id
        return alias_level, resolved_alias_id

    if normalized_level in (None, "account"):
        return "account", _resolve_account_id(None)
    raise ValidationError("Provide object_id or the matching entity-specific id for non-account levels.")


def _activity_params(
    *,
    limit: int,
    after: str | None,
    since: str | None,
    until: str | None,
    category: str | None,
    business_id: str | None,
    uid: str | int | None,
) -> dict[str, Any]:
    """Build Graph API parameters for the activities edge."""
    if limit <= 0:
        raise ValidationError("limit must be greater than 0.")
    params: dict[str, Any] = {"limit": limit}
    if after := blank_to_none(after):
        params["after"] = after
    if since := blank_to_none(since):
        params["since"] = since
    if until := blank_to_none(until):
        params["until"] = until
    if category := blank_to_none(category):
        params["category"] = category.upper()
    if business_id := blank_to_none(business_id):
        params["business_id"] = business_id
    if isinstance(uid, str):
        uid = blank_to_none(uid)
    if uid is not None:
        params["uid"] = uid
    return params


def _normalize_activity_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add parsed extra_data when Meta returns JSON encoded details."""
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        normalized = dict(item)
        extra_data = normalized.get("extra_data")
        if isinstance(extra_data, str) and extra_data.strip():
            try:
                normalized["extra_data_parsed"] = json.loads(extra_data)
            except json.JSONDecodeError:
                pass
        normalized_items.append(normalized)
    return normalized_items


@mcp_server.tool()
async def list_change_history(
    level: str | None = None,
    object_id: str | None = None,
    account_id: str | None = None,
    campaign_id: str | None = None,
    adset_id: str | None = None,
    ad_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    category: str | None = None,
    business_id: str | None = None,
    uid: str | int | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """Read Meta Ads activity/change history for an account, campaign, ad set, or ad."""
    resolved_level, parent_id = _resolve_scope(
        level=level,
        object_id=object_id,
        account_id=account_id,
        campaign_id=campaign_id,
        adset_id=adset_id,
        ad_id=ad_id,
    )
    requested_fields = fields or DEFAULT_ACTIVITY_FIELDS
    params = _activity_params(
        limit=limit,
        after=after,
        since=since,
        until=until,
        category=category,
        business_id=business_id,
        uid=uid,
    )
    client = get_graph_api_client()
    payload = await client.list_objects(parent_id, "activities", fields=requested_fields, params=params)
    response = normalize_collection(payload)
    normalized_uid = blank_to_none(uid) if isinstance(uid, str) else uid
    response["items"] = _normalize_activity_items(response["items"])
    response["scope"] = {"level": resolved_level, "object_id": parent_id}
    response["summary"].update(
        {
            "level": resolved_level,
            "object_id": parent_id,
            "date_window": {
                "since": blank_to_none(since),
                "until": blank_to_none(until),
            },
            "category": blank_to_none(category).upper() if blank_to_none(category) else None,
            "business_id": blank_to_none(business_id),
            "uid": normalized_uid,
            "default_window": "Meta returns one week of activity by default when since/until are omitted.",
        }
    )
    return response
