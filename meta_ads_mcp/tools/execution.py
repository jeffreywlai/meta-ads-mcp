"""Controlled execution tools."""

from __future__ import annotations

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.money import (
    from_minor_units,
    resolve_account_currency,
    to_minor_units,
    validate_positive_amount,
)
from meta_ads_mcp.schemas import mutation_response


def _validate_status(status: str) -> str:
    """Validate mutable status input."""
    if status not in {"ACTIVE", "PAUSED"}:
        raise ValidationError("status must be ACTIVE or PAUSED.")
    return status


def _validate_bid_strategy(bid_strategy: str) -> str:
    """Validate bid strategy input."""
    if not bid_strategy or not bid_strategy.strip():
        raise ValidationError("bid_strategy is required.")
    return bid_strategy.strip()


async def _set_status(object_id: str, object_type: str, status: str) -> dict[str, object]:
    """Update a status field."""
    client = get_graph_api_client()
    fields = ["id", "status", "effective_status"]
    previous = await client.get_object(object_id, fields=fields)
    await client.update_object(object_id, data={"status": _validate_status(status)})
    return mutation_response(
        action=f"set_{object_type}_status",
        target={f"{object_type}_id": object_id},
        previous={"status": previous.get("status") or previous.get("effective_status")},
        current={"status": status},
    )


@mcp_server.tool()
async def set_campaign_status(campaign_id: str, status: str) -> dict[str, object]:
    """Use this for a simple campaign pause or resume without changing any other campaign fields."""
    return await _set_status(campaign_id, "campaign", status)


@mcp_server.tool()
async def set_adset_status(adset_id: str, status: str) -> dict[str, object]:
    """Use this for pause ad set, resume ad set, or simple ad set status changes without changing targeting or budget fields."""
    return await _set_status(adset_id, "adset", status)


@mcp_server.tool()
async def set_ad_status(ad_id: str, status: str) -> dict[str, object]:
    """Use this for a simple ad pause or resume without editing the ad creative or placement."""
    return await _set_status(ad_id, "ad", status)


async def _delete_object(object_id: str, object_type: str) -> dict[str, object]:
    """Delete one object through an explicitly destructive tool."""
    result = await get_graph_api_client().delete_object(object_id)
    return {
        "ok": True,
        "action": f"delete_{object_type}",
        "target": {f"{object_type}_id": object_id},
        "result": result,
    }


@mcp_server.tool()
async def delete_adset(adset_id: str) -> dict[str, object]:
    """Use this only when the user explicitly wants to delete an ad set rather than pause it."""
    return await _delete_object(adset_id, "adset")


@mcp_server.tool()
async def delete_ad(ad_id: str) -> dict[str, object]:
    """Use this only when the user explicitly wants to delete an ad rather than pause it."""
    return await _delete_object(ad_id, "ad")


async def _update_budget(
    object_id: str,
    object_type: str,
    *,
    daily_budget: float | None,
    lifetime_budget: float | None,
) -> dict[str, object]:
    """Update a budget field."""
    if (daily_budget is None and lifetime_budget is None) or (
        daily_budget is not None and lifetime_budget is not None
    ):
        raise ValidationError("Provide exactly one of daily_budget or lifetime_budget.")
    field_name, amount = (
        ("daily_budget", daily_budget)
        if daily_budget is not None
        else ("lifetime_budget", lifetime_budget)
    )
    validate_positive_amount(amount, field_name=field_name)

    client = get_graph_api_client()
    previous = await client.get_object(
        object_id,
        fields=["id", "account_id", "daily_budget", "lifetime_budget"],
    )
    currency = await resolve_account_currency(client, previous.get("account_id"))
    data: dict[str, object] = {}
    current: dict[str, object] = {}
    if daily_budget is not None:
        data["daily_budget"] = to_minor_units(
            daily_budget,
            currency,
            field_name="daily_budget",
        )
        current["daily_budget"] = daily_budget
    if lifetime_budget is not None:
        data["lifetime_budget"] = to_minor_units(
            lifetime_budget,
            currency,
            field_name="lifetime_budget",
        )
        current["lifetime_budget"] = lifetime_budget

    previous_response = {
        "daily_budget": from_minor_units(
            previous.get("daily_budget"), currency, field_name="daily_budget"
        ),
        "lifetime_budget": from_minor_units(
            previous.get("lifetime_budget"), currency, field_name="lifetime_budget"
        ),
    }
    await client.update_object(object_id, data=data)
    return mutation_response(
        action=f"update_{object_type}_budget",
        target={f"{object_type}_id": object_id},
        previous=previous_response,
        current=current,
    )


async def _update_bid_amount(
    object_id: str,
    object_type: str,
    *,
    bid_amount: float,
) -> dict[str, object]:
    """Update a single bid amount field."""
    validate_positive_amount(bid_amount, field_name="bid_amount")

    client = get_graph_api_client()
    previous = await client.get_object(
        object_id,
        fields=["id", "account_id", "bid_amount"],
    )
    currency = await resolve_account_currency(client, previous.get("account_id"))
    encoded_bid_amount = to_minor_units(
        bid_amount,
        currency,
        field_name="bid_amount",
    )
    previous_response = {
        "bid_amount": from_minor_units(
            previous.get("bid_amount"), currency, field_name="bid_amount"
        ),
    }
    await client.update_object(object_id, data={"bid_amount": encoded_bid_amount})
    return mutation_response(
        action=f"update_{object_type}_bid_amount",
        target={f"{object_type}_id": object_id},
        previous=previous_response,
        current={"bid_amount": bid_amount},
    )


async def _update_bid_strategy(
    object_id: str,
    object_type: str,
    *,
    bid_strategy: str,
    bid_amount: float | None = None,
    bid_constraints: dict[str, object] | None = None,
) -> dict[str, object]:
    """Update bid strategy and optionally a supporting bid amount."""
    validated_bid_strategy = _validate_bid_strategy(bid_strategy)
    if bid_amount is not None:
        validate_positive_amount(bid_amount, field_name="bid_amount")

    client = get_graph_api_client()
    previous = await client.get_object(
        object_id,
        fields=["id", "account_id", "bid_strategy", "bid_amount", "bid_constraints"],
    )
    has_bid_amount = bid_amount is not None or previous.get("bid_amount") is not None
    currency = (
        await resolve_account_currency(client, previous.get("account_id"))
        if has_bid_amount
        else None
    )
    payload: dict[str, object] = {"bid_strategy": validated_bid_strategy}
    current: dict[str, object] = {"bid_strategy": validated_bid_strategy}
    if bid_amount is not None:
        payload["bid_amount"] = to_minor_units(
            bid_amount,
            currency,
            field_name="bid_amount",
        )
        current["bid_amount"] = bid_amount
    if bid_constraints is not None:
        payload["bid_constraints"] = bid_constraints
        current["bid_constraints"] = bid_constraints

    previous_response = {
        "bid_strategy": previous.get("bid_strategy"),
        "bid_amount": from_minor_units(
            previous.get("bid_amount"), currency, field_name="bid_amount"
        ),
        "bid_constraints": previous.get("bid_constraints"),
    }
    await client.update_object(object_id, data=payload)
    return mutation_response(
        action=f"update_{object_type}_bid_strategy",
        target={f"{object_type}_id": object_id},
        previous=previous_response,
        current=current,
    )


@mcp_server.tool()
async def update_campaign_budget(
    campaign_id: str,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
) -> dict[str, object]:
    """Use this when the user wants to change only campaign budget, not other campaign configuration."""
    return await _update_budget(
        campaign_id,
        "campaign",
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
    )


@mcp_server.tool()
async def update_adset_budget(
    adset_id: str,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
) -> dict[str, object]:
    """Use this when the user wants to change only ad set budget, not other ad set settings."""
    return await _update_budget(
        adset_id,
        "adset",
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
    )


@mcp_server.tool()
async def update_adset_bid_amount(adset_id: str, bid_amount: float) -> dict[str, object]:
    """Use this when the user wants to change only the ad set bid amount, not status, targeting, or budget."""
    return await _update_bid_amount(adset_id, "adset", bid_amount=bid_amount)


@mcp_server.tool()
async def update_campaign_bid_strategy(
    campaign_id: str,
    bid_strategy: str,
) -> dict[str, object]:
    """Use this when the user wants to adjust the campaign-level bidding strategy."""
    validated_bid_strategy = _validate_bid_strategy(bid_strategy)
    client = get_graph_api_client()
    previous = await client.get_object(campaign_id, fields=["id", "bid_strategy"])
    await client.update_object(
        campaign_id,
        data={"bid_strategy": validated_bid_strategy},
    )
    return mutation_response(
        action="update_campaign_bid_strategy",
        target={"campaign_id": campaign_id},
        previous={"bid_strategy": previous.get("bid_strategy")},
        current={"bid_strategy": validated_bid_strategy},
    )


@mcp_server.tool()
async def update_adset_bid_strategy(
    adset_id: str,
    bid_strategy: str,
    bid_amount: float | None = None,
    bid_constraints: dict[str, object] | None = None,
) -> dict[str, object]:
    """Use this when the user wants to adjust ad set bidding strategy with an optional bid amount override."""
    return await _update_bid_strategy(
        adset_id,
        "adset",
        bid_strategy=bid_strategy,
        bid_amount=bid_amount,
        bid_constraints=bid_constraints,
    )
