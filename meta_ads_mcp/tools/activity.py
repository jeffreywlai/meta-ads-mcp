"""Activity and change-history tools."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import httpx

from meta_ads_mcp.activity_scope import validate_activity_scope_arguments
from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaAdsError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.tool_types import FieldList

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

@dataclass(frozen=True, slots=True)
class ActivityScope:
    """Resolved activity query scope."""

    level: str
    object_id: str
    account_id: str
    parent_id: str
    object_filter_id: str | None


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve an ad account id, using the default when omitted."""
    account_id = blank_to_none(account_id)
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


def _configured_account_id(account_id: str | None) -> str | None:
    """Resolve an explicit or configured account id without requiring one."""
    if account_id := blank_to_none(account_id):
        return normalize_account_id(account_id)
    if default_account_id := blank_to_none(get_settings().default_account_id):
        return normalize_account_id(default_account_id)
    return None


async def _resolve_object_account_id(
    *,
    object_id: str,
    account_id: str | None,
    client: Any,
) -> str:
    """Resolve and verify an object's owning account."""
    configured_account_id = _configured_account_id(account_id)
    try:
        payload = await client.get_object(object_id, fields=["account_id"])
    except (MetaAdsError, httpx.HTTPError) as exc:
        if configured_account_id is not None:
            return configured_account_id
        raise ValidationError(
            f"account_id could not be derived from object '{object_id}'. "
            "Pass account_id explicitly or set META_DEFAULT_ACCOUNT_ID. "
            f"Ownership lookup failed: {exc}"
        ) from exc
    derived_account_id = payload.get("account_id")
    if derived_account_id is None or not str(derived_account_id).strip():
        if configured_account_id is not None:
            return configured_account_id
        raise ValidationError(
            f"account_id could not be derived from object '{object_id}'. "
            "Pass account_id explicitly or set META_DEFAULT_ACCOUNT_ID."
        )
    normalized_derived_account_id = normalize_account_id(str(derived_account_id).strip())
    if (
        configured_account_id is not None
        and configured_account_id != normalized_derived_account_id
    ):
        raise ValidationError(
            f"Object '{object_id}' belongs to account "
            f"'{normalized_derived_account_id}', but the supplied or default "
            f"account resolves to '{configured_account_id}'."
        )
    return normalized_derived_account_id


async def _resolve_scope(
    *,
    level: str | None,
    object_id: str | None,
    account_id: str | None,
    campaign_id: str | None,
    adset_id: str | None,
    ad_id: str | None,
    client: Any,
) -> ActivityScope:
    """Resolve user scope into the ad-account activities edge plus optional object filter."""
    try:
        validated = validate_activity_scope_arguments(
            level=level,
            object_id=object_id,
            account_id=account_id,
            campaign_id=campaign_id,
            adset_id=adset_id,
            ad_id=ad_id,
        )
    except ValueError as error:
        raise ValidationError(str(error)) from error
    normalized_level = validated.level
    normalized_object_id = validated.object_id
    normalized_account_id = validated.account_id
    provided_object_aliases = (
        [validated.object_alias]
        if validated.object_alias is not None
        else []
    )

    if normalized_object_id is not None:
        if normalized_level == "account":
            resolved_account_id = _resolve_account_id(normalized_object_id)
            return ActivityScope(
                level="account",
                object_id=resolved_account_id,
                account_id=resolved_account_id,
                parent_id=resolved_account_id,
                object_filter_id=None,
            )
        resolved_account_id = await _resolve_object_account_id(
            object_id=normalized_object_id,
            account_id=normalized_account_id,
            client=client,
        )
        return ActivityScope(
            level=normalized_level,
            object_id=normalized_object_id,
            account_id=resolved_account_id,
            parent_id=resolved_account_id,
            object_filter_id=normalized_object_id,
        )

    if provided_object_aliases:
        alias_level, alias_object_id = provided_object_aliases[0]
        resolved_account_id = await _resolve_object_account_id(
            object_id=alias_object_id,
            account_id=normalized_account_id,
            client=client,
        )
        return ActivityScope(
            level=alias_level,
            object_id=alias_object_id,
            account_id=resolved_account_id,
            parent_id=resolved_account_id,
            object_filter_id=alias_object_id,
        )

    if normalized_level in (None, "account"):
        resolved_account_id = _resolve_account_id(normalized_account_id)
        return ActivityScope(
            level="account",
            object_id=resolved_account_id,
            account_id=resolved_account_id,
            parent_id=resolved_account_id,
            object_filter_id=None,
        )
    raise AssertionError("validated activity scope was not resolvable")


def _activity_params(
    *,
    limit: int,
    after: str | None,
    since: str | None,
    until: str | None,
    category: str | None,
    business_id: str | None,
    uid: str | int | None,
    object_filter_id: str | None,
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
    if object_filter_id := blank_to_none(object_filter_id):
        params["oid"] = object_filter_id
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
    fields: FieldList | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """Read Meta Ads activity/change history for an account, campaign, ad set, or ad."""
    client = get_graph_api_client()
    scope = await _resolve_scope(
        level=level,
        object_id=object_id,
        account_id=account_id,
        campaign_id=campaign_id,
        adset_id=adset_id,
        ad_id=ad_id,
        client=client,
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
        object_filter_id=scope.object_filter_id,
    )
    payload = await client.list_objects(scope.parent_id, "activities", fields=requested_fields, params=params)
    response = normalize_collection(payload)
    normalized_uid = blank_to_none(uid) if isinstance(uid, str) else uid
    response["items"] = _normalize_activity_items(response["items"])
    response["scope"] = {
        "level": scope.level,
        "object_id": scope.object_id,
        "account_id": scope.account_id,
    }
    response["summary"].update(
        {
            "level": scope.level,
            "object_id": scope.object_id,
            "account_id": scope.account_id,
            "object_filter_id": scope.object_filter_id,
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
