"""Controlled execution tools."""

from __future__ import annotations

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import ZERO_DECIMAL_CURRENCIES, normalize_budget_value
from meta_ads_mcp.schemas import mutation_response


def _bid_minor_units(value: float, currency: str | None = None) -> int:
    """Encode a human bid value for the API."""
    if currency and currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return int(value)
    return int(value * 100)


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
    """Use this for a simple ad set pause or resume without changing targeting or budget fields."""
    return await _set_status(adset_id, "adset", status)


@mcp_server.tool()
async def set_ad_status(ad_id: str, status: str) -> dict[str, object]:
    """Use this for a simple ad pause or resume without editing the ad creative or placement."""
    return await _set_status(ad_id, "ad", status)


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

    client = get_graph_api_client()
    previous = await client.get_object(
        object_id,
        fields=["id", "daily_budget", "lifetime_budget", "currency"],
    )
    currency = previous.get("currency")
    data: dict[str, object] = {}
    current: dict[str, object] = {}
    if daily_budget is not None:
        data["daily_budget"] = (
            int(daily_budget)
            if currency and currency.upper() in ZERO_DECIMAL_CURRENCIES
            else int(daily_budget * 100)
        )
        current["daily_budget"] = daily_budget
    if lifetime_budget is not None:
        data["lifetime_budget"] = (
            int(lifetime_budget)
            if currency and currency.upper() in ZERO_DECIMAL_CURRENCIES
            else int(lifetime_budget * 100)
        )
        current["lifetime_budget"] = lifetime_budget

    await client.update_object(object_id, data=data)
    return mutation_response(
        action=f"update_{object_type}_budget",
        target={f"{object_type}_id": object_id},
        previous={
            "daily_budget": normalize_budget_value(previous.get("daily_budget"), previous.get("currency")),
            "lifetime_budget": normalize_budget_value(previous.get("lifetime_budget"), previous.get("currency")),
        },
        current=current,
    )


async def _update_bid_amount(
    object_id: str,
    object_type: str,
    *,
    bid_amount: float,
) -> dict[str, object]:
    """Update a single bid amount field."""
    if bid_amount <= 0:
        raise ValidationError("bid_amount must be greater than 0.")

    client = get_graph_api_client()
    previous = await client.get_object(
        object_id,
        fields=["id", "bid_amount", "currency"],
    )
    encoded_bid_amount = _bid_minor_units(bid_amount, previous.get("currency"))
    await client.update_object(object_id, data={"bid_amount": encoded_bid_amount})
    return mutation_response(
        action=f"update_{object_type}_bid_amount",
        target={f"{object_type}_id": object_id},
        previous={
            "bid_amount": normalize_budget_value(previous.get("bid_amount"), previous.get("currency")),
        },
        current={"bid_amount": bid_amount},
    )


async def _update_bid_strategy(
    object_id: str,
    object_type: str,
    *,
    bid_strategy: str,
    bid_amount: float | None = None,
) -> dict[str, object]:
    """Update bid strategy and optionally a supporting bid amount."""
    validated_bid_strategy = _validate_bid_strategy(bid_strategy)
    if bid_amount is not None and bid_amount <= 0:
        raise ValidationError("bid_amount must be greater than 0 when provided.")

    client = get_graph_api_client()
    previous = await client.get_object(
        object_id,
        fields=["id", "bid_strategy", "bid_amount", "currency"],
    )
    payload: dict[str, object] = {"bid_strategy": validated_bid_strategy}
    current: dict[str, object] = {"bid_strategy": validated_bid_strategy}
    if bid_amount is not None:
        payload["bid_amount"] = _bid_minor_units(bid_amount, previous.get("currency"))
        current["bid_amount"] = bid_amount

    await client.update_object(object_id, data=payload)
    return mutation_response(
        action=f"update_{object_type}_bid_strategy",
        target={f"{object_type}_id": object_id},
        previous={
            "bid_strategy": previous.get("bid_strategy"),
            "bid_amount": normalize_budget_value(previous.get("bid_amount"), previous.get("currency")),
        },
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
    bid_amount: float | None = None,
) -> dict[str, object]:
    """Use this when the user wants to adjust campaign bidding strategy with an optional bid amount override."""
    return await _update_bid_strategy(
        campaign_id,
        "campaign",
        bid_strategy=bid_strategy,
        bid_amount=bid_amount,
    )


@mcp_server.tool()
async def update_adset_bid_strategy(
    adset_id: str,
    bid_strategy: str,
    bid_amount: float | None = None,
) -> dict[str, object]:
    """Use this when the user wants to adjust ad set bidding strategy with an optional bid amount override."""
    return await _update_bid_strategy(
        adset_id,
        "adset",
        bid_strategy=bid_strategy,
        bid_amount=bid_amount,
    )
