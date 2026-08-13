"""Campaign management tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.api_compat import validate_targeting_placements
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, add_validate_only, merge_graph_payload
from meta_ads_mcp.money import (
    from_minor_units,
    resolve_account_currency,
    to_minor_units,
    validate_positive_amount,
)
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import StringList


def _encode_budget_field(
    payload: dict[str, Any],
    field_name: str,
    value: float | None,
    *,
    currency: str | None,
) -> None:
    """Encode a budget field into minor currency units."""
    payload[field_name] = (
        OMIT
        if value is None
        else to_minor_units(value, currency, field_name=field_name)
    )


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
    for field_name, value in (
        ("daily_budget", daily_budget),
        ("lifetime_budget", lifetime_budget),
    ):
        if value is not None:
            validate_positive_amount(value, field_name=field_name)
    payload: dict[str, Any] = {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": special_ad_categories or [],
        "buying_type": buying_type or OMIT,
        "bid_strategy": bid_strategy or OMIT,
        "daily_budget": OMIT,
        "lifetime_budget": OMIT,
    }
    add_validate_only(payload, validate_only=validate_only)
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    currency = None
    if daily_budget is not None or lifetime_budget is not None:
        currency = await resolve_account_currency(client, account_id)
    _encode_budget_field(payload, "daily_budget", daily_budget, currency=currency)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget, currency=currency)
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
    for field_name, value in (
        ("daily_budget", daily_budget),
        ("lifetime_budget", lifetime_budget),
    ):
        if value is not None:
            validate_positive_amount(value, field_name=field_name)
    if all(
        value is None
        for value in (name, status, objective, daily_budget, lifetime_budget)
    ) and not params:
        raise ValidationError("At least one field must be provided for update_campaign.")
    payload: dict[str, Any] = {
        "name": name if name is not None else OMIT,
        "status": status if status is not None else OMIT,
        "objective": objective if objective is not None else OMIT,
        "daily_budget": OMIT,
        "lifetime_budget": OMIT,
    }
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    previous = await client.get_object(
        campaign_id,
        fields=[
            "id",
            "account_id",
            "name",
            "status",
            "objective",
            "daily_budget",
            "lifetime_budget",
        ],
    )
    has_money = any(
        value is not None
        for value in (
            daily_budget,
            lifetime_budget,
            previous.get("daily_budget"),
            previous.get("lifetime_budget"),
        )
    )
    currency = (
        await resolve_account_currency(client, previous.get("account_id"))
        if has_money
        else None
    )
    current: dict[str, Any] = {}
    if name is not None:
        current["name"] = name
    if status is not None:
        current["status"] = status
    if objective is not None:
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
    previous_response = {
        "name": previous.get("name"),
        "status": previous.get("status"),
        "objective": previous.get("objective"),
        "daily_budget": from_minor_units(
            previous.get("daily_budget"), currency, field_name="daily_budget"
        ),
        "lifetime_budget": from_minor_units(
            previous.get("lifetime_budget"), currency, field_name="lifetime_budget"
        ),
    }
    await client.update_object(campaign_id, data=payload)
    return mutation_response(
        action="update_campaign",
        target={"campaign_id": campaign_id},
        previous=previous_response,
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
    for field_name, value in (
        ("bid_amount", bid_amount),
        ("daily_budget", daily_budget),
        ("lifetime_budget", lifetime_budget),
    ):
        if value is not None:
            validate_positive_amount(value, field_name=field_name)
    if bid_constraints and not bid_strategy:
        raise ValidationError("bid_strategy is required when bid_constraints is provided.")
    validate_targeting_placements(targeting)
    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": name,
        "billing_event": billing_event,
        "optimization_goal": optimization_goal,
        "targeting": targeting,
        "status": status,
        "bid_amount": OMIT,
        "bid_strategy": bid_strategy or OMIT,
        "bid_constraints": bid_constraints or OMIT,
        "promoted_object": promoted_object or OMIT,
        "start_time": start_time or OMIT,
        "end_time": end_time or OMIT,
        "daily_budget": OMIT,
        "lifetime_budget": OMIT,
    }
    add_validate_only(payload, validate_only=validate_only)
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    currency = None
    if any(value is not None for value in (bid_amount, daily_budget, lifetime_budget)):
        currency = await resolve_account_currency(client, account_id)
    if bid_amount is not None:
        payload["bid_amount"] = to_minor_units(
            bid_amount,
            currency,
            field_name="bid_amount",
        )
    _encode_budget_field(payload, "daily_budget", daily_budget, currency=currency)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget, currency=currency)
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
