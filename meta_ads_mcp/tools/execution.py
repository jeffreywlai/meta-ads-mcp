"""Controlled execution tools."""

from __future__ import annotations

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import normalize_budget_value
from meta_ads_mcp.schemas import mutation_response


def _validate_status(status: str) -> str:
    """Validate mutable status input."""
    if status not in {"ACTIVE", "PAUSED"}:
        raise ValidationError("status must be ACTIVE or PAUSED.")
    return status


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
    """Pause or enable a campaign."""
    return await _set_status(campaign_id, "campaign", status)


@mcp_server.tool()
async def set_adset_status(adset_id: str, status: str) -> dict[str, object]:
    """Pause or enable an ad set."""
    return await _set_status(adset_id, "adset", status)


@mcp_server.tool()
async def set_ad_status(ad_id: str, status: str) -> dict[str, object]:
    """Pause or enable an ad."""
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
    data: dict[str, object] = {}
    current: dict[str, object] = {}
    if daily_budget is not None:
        data["daily_budget"] = int(daily_budget * 100)
        current["daily_budget"] = daily_budget
    if lifetime_budget is not None:
        data["lifetime_budget"] = int(lifetime_budget * 100)
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


@mcp_server.tool()
async def update_campaign_budget(
    campaign_id: str,
    daily_budget: float | None = None,
    lifetime_budget: float | None = None,
) -> dict[str, object]:
    """Update a campaign budget."""
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
    """Update an ad set budget."""
    return await _update_budget(
        adset_id,
        "adset",
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
    )

