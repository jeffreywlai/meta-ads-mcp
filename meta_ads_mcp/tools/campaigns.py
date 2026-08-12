"""Campaign management tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import add_validate_only, merge_graph_payload
from meta_ads_mcp.normalize import ZERO_DECIMAL_CURRENCIES, normalize_budget_value
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import StringList


def _budget_minor_units(value: float, currency: str | None = None) -> int:
    """Encode a human budget value for the API."""
    if currency and currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return int(value)
    return int(value * 100)


def _encode_budget_field(
    payload: dict[str, Any],
    field_name: str,
    value: float | None,
    *,
    currency: str | None = None,
) -> None:
    """Encode a budget field into minor currency units."""
    if value is not None:
        payload[field_name] = _budget_minor_units(value, currency)


@mcp_server.tool()
async def create_campaign(
    account_id: str,
    name: str,
    objective: str,
    status: str = "PAUSED",
    special_ad_categories: StringList | None = None,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    buying_type: str | None = None,
    bid_strategy: str | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to create a new campaign shell before adding ad sets or ads."""
    if daily_budget is not None and lifetime_budget is not None:
        raise ValidationError("Provide at most one of daily_budget or lifetime_budget.")
    payload: dict[str, Any] = {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": special_ad_categories or [],
    }
    _encode_budget_field(payload, "daily_budget", daily_budget)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget)
    if buying_type:
        payload["buying_type"] = buying_type
    if bid_strategy:
        payload["bid_strategy"] = bid_strategy
    add_validate_only(payload, validate_only=validate_only)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    result = await client.create_edge_object(
        account_id,
        "campaigns",
        data=merge_graph_payload(payload, params),
    )
    return creation_response(
        action="create_campaign",
        target={"account_id": account_id},
        result=result,
        validate_only=validate_only,
    )


@mcp_server.tool()
async def update_campaign(
    campaign_id: str,
    name: str | None = None,
    status: str | None = None,
    objective: str | None = None,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use this when the user already has a campaign id and wants to change core campaign settings."""
    if daily_budget is not None and lifetime_budget is not None:
        raise ValidationError("Provide at most one of daily_budget or lifetime_budget.")
    client = get_graph_api_client()
    previous = await client.get_object(
        campaign_id,
        fields=["id", "name", "status", "objective", "daily_budget", "lifetime_budget", "currency"],
    )
    currency = previous.get("currency")
    payload: dict[str, Any] = {}
    current: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
        current["name"] = name
    if status is not None:
        payload["status"] = status
        current["status"] = status
    if objective is not None:
        payload["objective"] = objective
        current["objective"] = objective
    _encode_budget_field(payload, "daily_budget", daily_budget, currency=currency)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget, currency=currency)
    if daily_budget is not None:
        current["daily_budget"] = daily_budget
    if lifetime_budget is not None:
        current["lifetime_budget"] = lifetime_budget
    payload = merge_graph_payload(payload, params)
    if params:
        current.update(params)
    if not payload:
        raise ValidationError("At least one field must be provided for update_campaign.")
    await client.update_object(campaign_id, data=payload)
    return mutation_response(
        action="update_campaign",
        target={"campaign_id": campaign_id},
        previous={
            "name": previous.get("name"),
            "status": previous.get("status"),
            "objective": previous.get("objective"),
            "daily_budget": normalize_budget_value(previous.get("daily_budget"), previous.get("currency")),
            "lifetime_budget": normalize_budget_value(previous.get("lifetime_budget"), previous.get("currency")),
        },
        current=current,
    )


@mcp_server.tool()
async def delete_campaign(campaign_id: str) -> dict[str, Any]:
    """Use this only when the user explicitly wants to delete a campaign rather than pause it."""
    client = get_graph_api_client()
    result = await client.delete_object(campaign_id)
    return {
        "ok": True,
        "action": "delete_campaign",
        "target": {"campaign_id": campaign_id},
        "result": result,
    }


@mcp_server.tool()
async def create_ad_set(
    account_id: str,
    campaign_id: str,
    name: str,
    billing_event: str,
    optimization_goal: str,
    targeting: dict[str, Any],
    status: str = "PAUSED",
    bid_amount: float | None = None,
    bid_strategy: str | None = None,
    bid_constraints: dict[str, Any] | None = None,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
    promoted_object: dict[str, Any] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or validate an ad set with targeting, budget, bid strategy, and bid constraints."""
    if daily_budget is not None and lifetime_budget is not None:
        raise ValidationError("Provide at most one of daily_budget or lifetime_budget.")
    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": name,
        "billing_event": billing_event,
        "optimization_goal": optimization_goal,
        "targeting": targeting,
        "status": status,
    }
    if bid_amount is not None:
        payload["bid_amount"] = int(bid_amount * 100)
    if bid_strategy:
        payload["bid_strategy"] = bid_strategy
    if bid_constraints:
        if not bid_strategy:
            raise ValidationError("bid_strategy is required when bid_constraints is provided.")
        payload["bid_constraints"] = bid_constraints
    _encode_budget_field(payload, "daily_budget", daily_budget)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget)
    if promoted_object:
        payload["promoted_object"] = promoted_object
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    add_validate_only(payload, validate_only=validate_only)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    result = await client.create_edge_object(
        account_id,
        "adsets",
        data=merge_graph_payload(payload, params),
    )
    return creation_response(
        action="create_ad_set",
        target={"account_id": account_id, "campaign_id": campaign_id},
        result=result,
        validate_only=validate_only,
    )
