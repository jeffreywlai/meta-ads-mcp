"""Campaign management tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from meta_ads_mcp.api_compat import validate_targeting_placements
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.field_reference import FIELD_REFERENCE
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, add_validate_only, merge_graph_payload
from meta_ads_mcp.money import (
    from_minor_units,
    resolve_account_currency,
    to_minor_units,
    validate_positive_amount,
)
from meta_ads_mcp.normalize import blank_to_none
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import BidConstraints, CurrencyAmount, StringList


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


def reject_named_and_params(named: dict[str, Any], params: dict[str, Any] | None, *, money: tuple[str, ...] = ()) -> None:
    """Refuse a field given both as a named parameter and inside params, before any network call."""
    for key, value in named.items():
        if value is not None and params and key in params:
            unit_note = (
                " The named parameter takes major currency units; a params value is sent to Meta as minor units."
                if key in money
                else ""
            )
            raise ValidationError(
                f"{key} was given both as a named parameter and inside params; give it once, "
                f"preferably as the named {key} parameter.{unit_note}"
            )


def _nonempty_object_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    """Check a list of JSON objects, such as frequency_control_specs."""
    if not isinstance(value, list) or any(not isinstance(item, dict) or not item for item in value):
        raise ValidationError(f"{field_name} must be a list of non-empty objects.")
    if not value:
        raise ValidationError(f"{field_name} must not be empty; omit it instead.")
    return value


def validate_adlabels(adlabels: Any) -> list[dict[str, Any]]:
    """Check Meta ad labels: objects naming an existing label by id or a label by name."""
    labels = _nonempty_object_list(adlabels, "adlabels")
    for label in labels:
        if not any(isinstance(label.get(key), str) and label[key].strip() for key in ("id", "name")):
            raise ValidationError('adlabels entries need an "id" or a "name", for example [{"name": "Q4"}].')
    return labels


def _nonblank_text(value: str, field_name: str) -> str:
    """Check a free-text field that must not be blank."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must not be blank.")
    return value


@mcp_server.tool()
async def create_campaign(
    account_id: str,
    name: str,
    objective: str,
    status: str = "PAUSED",
    special_ad_categories: StringList | None = None,
    daily_budget: CurrencyAmount | None = None,
    lifetime_budget: CurrencyAmount | None = None,
    buying_type: str | None = None,
    bid_strategy: str | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
    spend_cap: CurrencyAmount | None = None,
    start_time: str | None = None,
    stop_time: str | None = None,
    promoted_object: dict[str, Any] | None = None,
    special_ad_category_country: StringList | None = None,
    adlabels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to create a new campaign shell before adding ad sets or ads. Budgets and spend_cap are in major units of the account currency (Meta's spend cap minimum is about 100 USD). start_time and stop_time take an ISO 8601 time with a UTC offset or Unix seconds. special_ad_category_country lists two-letter country codes. adlabels is a list of {"name": ...} or {"id": ...}. A named parameter and the same key inside params cannot both be given."""
    reject_named_and_params(
        {
            "spend_cap": spend_cap,
            "start_time": start_time,
            "stop_time": stop_time,
            "promoted_object": promoted_object,
            "special_ad_category_country": special_ad_category_country,
            "adlabels": adlabels,
        },
        params,
        money=("spend_cap",),
    )
    if daily_budget is not None and lifetime_budget is not None:
        raise ValidationError("Provide at most one of daily_budget or lifetime_budget.")
    for field_name, value in (
        ("daily_budget", daily_budget),
        ("lifetime_budget", lifetime_budget),
        ("spend_cap", spend_cap),
    ):
        if value is not None:
            validate_positive_amount(value, field_name=field_name)
    new_start = _validate_adset_time(start_time, "start_time")
    new_stop = _validate_adset_time(stop_time, "stop_time")
    if new_start is not None and new_stop is not None and new_stop <= new_start:
        raise ValidationError("stop_time must be after start_time.")
    if promoted_object is not None and not promoted_object:
        raise ValidationError("promoted_object must not be empty; omit it instead.")
    countries = None
    if special_ad_category_country is not None:
        if not special_ad_category_country:
            raise ValidationError("special_ad_category_country must not be empty; omit it instead.")
        countries = [str(code).strip().upper() for code in special_ad_category_country]
        if any(len(code) != 2 or not code.isalpha() for code in countries):
            raise ValidationError("special_ad_category_country takes two-letter country codes such as US or CA.")
    if adlabels is not None:
        validate_adlabels(adlabels)
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
    # New named fields join the payload only when given, so a call that puts
    # them in params (as callers did before they existed) is sent unchanged.
    for key, value in (
        ("spend_cap", spend_cap),
        ("start_time", start_time),
        ("stop_time", stop_time),
        ("promoted_object", promoted_object),
        ("special_ad_category_country", countries),
        ("adlabels", adlabels),
    ):
        if value is not None:
            payload[key] = value
    add_validate_only(payload, validate_only=validate_only)
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    currency = None
    if any(value is not None for value in (daily_budget, lifetime_budget, spend_cap)):
        currency = await resolve_account_currency(client, account_id)
    _encode_budget_field(payload, "daily_budget", daily_budget, currency=currency)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget, currency=currency)
    if spend_cap is not None:
        payload["spend_cap"] = to_minor_units(spend_cap, currency, field_name="spend_cap")
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
    daily_budget: CurrencyAmount | None = None,
    lifetime_budget: CurrencyAmount | None = None,
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
    bid_amount: CurrencyAmount | None = None,
    bid_strategy: str | None = None,
    bid_constraints: BidConstraints | None = None,
    daily_budget: CurrencyAmount | None = None,
    lifetime_budget: CurrencyAmount | None = None,
    promoted_object: dict[str, Any] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
    frequency_control_specs: list[dict[str, Any]] | None = None,
    attribution_spec: list[dict[str, Any]] | None = None,
    adset_schedule: list[dict[str, Any]] | None = None,
    daily_spend_cap: CurrencyAmount | None = None,
    lifetime_spend_cap: CurrencyAmount | None = None,
    daily_min_spend_target: CurrencyAmount | None = None,
    lifetime_min_spend_target: CurrencyAmount | None = None,
    dsa_beneficiary: str | None = None,
    dsa_payor: str | None = None,
    adlabels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create or validate an ad set with targeting, budget, bid strategy, and bid constraints. Budgets, bids, spend caps and minimum spend targets are in major units of the account currency; daily caps and targets need a daily campaign budget, lifetime ones a lifetime campaign budget. frequency_control_specs, attribution_spec and adset_schedule are Meta's lists of objects; adset_schedule (day parting) needs a lifetime budget and pacing_type ["day_parting"] in params. dsa_beneficiary and dsa_payor name who benefits from and who pays for ads shown in the EU. A named parameter and the same key inside params cannot both be given."""
    named_spend = {
        "daily_spend_cap": daily_spend_cap,
        "lifetime_spend_cap": lifetime_spend_cap,
        "daily_min_spend_target": daily_min_spend_target,
        "lifetime_min_spend_target": lifetime_min_spend_target,
    }
    named_other = {
        "frequency_control_specs": frequency_control_specs,
        "attribution_spec": attribution_spec,
        "adset_schedule": adset_schedule,
    }
    reject_named_and_params(
        {**named_other, **named_spend, "dsa_beneficiary": dsa_beneficiary, "dsa_payor": dsa_payor, "adlabels": adlabels},
        params,
        money=_ADSET_SPEND_FIELDS,
    )
    if daily_budget is not None and lifetime_budget is not None:
        raise ValidationError("Provide at most one of daily_budget or lifetime_budget.")
    for field_name, value in (
        ("bid_amount", bid_amount),
        ("daily_budget", daily_budget),
        ("lifetime_budget", lifetime_budget),
        *named_spend.items(),
    ):
        if value is not None:
            validate_positive_amount(value, field_name=field_name)
    if bid_constraints and not bid_strategy:
        raise ValidationError("bid_strategy is required when bid_constraints is provided.")
    for field_name, value in named_other.items():
        if value is not None:
            _nonempty_object_list(value, field_name)
    for field_name, value in (("dsa_beneficiary", dsa_beneficiary), ("dsa_payor", dsa_payor)):
        if value is not None:
            _nonblank_text(value, field_name)
    if adlabels is not None:
        validate_adlabels(adlabels)
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
    # New named fields join the payload only when given, so a call that puts
    # them in params (as callers did before they existed) is sent unchanged.
    for key, value in (
        *named_other.items(),
        *named_spend.items(),
        ("dsa_beneficiary", dsa_beneficiary),
        ("dsa_payor", dsa_payor),
        ("adlabels", adlabels),
    ):
        if value is not None:
            payload[key] = value
    add_validate_only(payload, validate_only=validate_only)
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    currency = None
    if any(value is not None for value in (bid_amount, daily_budget, lifetime_budget, *named_spend.values())):
        currency = await resolve_account_currency(client, account_id)
    if bid_amount is not None:
        payload["bid_amount"] = to_minor_units(
            bid_amount,
            currency,
            field_name="bid_amount",
        )
    _encode_budget_field(payload, "daily_budget", daily_budget, currency=currency)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget, currency=currency)
    for field_name, value in named_spend.items():
        if value is not None:
            payload[field_name] = to_minor_units(value, currency, field_name=field_name)
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


_ADSET_MONEY_FIELD_TOOLS = {
    "daily_budget": "update_adset_budget",
    "lifetime_budget": "update_adset_budget",
    "bid_amount": "update_adset_bid_amount",
    "bid_info": "update_adset_bid_amount",
    "bid_strategy": "update_adset_bid_strategy",
    "bid_constraints": "update_adset_bid_strategy",
}
# Ad set spend caps and minimum spend targets: Meta stores them in the account
# currency's minor units, like budgets. update_adset takes them as named
# parameters in major units; inside params they would bypass that conversion.
_ADSET_SPEND_FIELDS = (
    "daily_spend_cap",
    "lifetime_spend_cap",
    "daily_min_spend_target",
    "lifetime_min_spend_target",
)
_ADSET_UNSUPPORTED_MONEY_PARAMS = {
    "budget_schedule_specs": (
        "budget_schedule_specs carries budget amounts in minor units and is not supported by update_adset."
    ),
}
_UPDATABLE_ADSET_STATUSES = {"ACTIVE", "PAUSED", "ARCHIVED"}
_OMITTED_PREVIOUS = "Meta returned no value for it; it may not have been set."
_UNREAD_ADSET_PREVIOUS = "Not a field this server reads back from an ad set."
_ADSET_TIME_FORMAT = "an ISO 8601 time with a UTC offset (2026-12-01T00:00:00-05:00) or Unix seconds"


def _validate_adset_time(value: str | None, field_name: str) -> datetime | None:
    """Check an ad set schedule time; None when absent or end_time 0 (Meta's no-end-date value)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be {_ADSET_TIME_FORMAT}.")
    text = value.strip()
    if text.isdigit():
        seconds = int(text)
        if seconds == 0 and field_name == "end_time":
            return None
        if seconds == 0:
            raise ValidationError(f"{field_name} must be a positive Unix time.")
        return datetime.fromtimestamp(seconds, timezone.utc)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be {_ADSET_TIME_FORMAT}.") from exc
    if parsed.tzinfo is None:
        raise ValidationError(
            f"{field_name} needs a UTC offset or Z (for example 2026-12-01T00:00:00-05:00) "
            "so the schedule does not shift by the account's time zone."
        )
    return parsed


def _stored_adset_time(value: Any) -> datetime | None:
    """Read a stored ad set time such as 2026-12-01T00:00:00+0000; None when absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@mcp_server.tool()
async def update_adset(
    adset_id: str,
    name: str | None = None,
    targeting: dict[str, Any] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    optimization_goal: str | None = None,
    billing_event: str | None = None,
    promoted_object: dict[str, Any] | None = None,
    status: str | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
    daily_spend_cap: CurrencyAmount | None = None,
    lifetime_spend_cap: CurrencyAmount | None = None,
    daily_min_spend_target: CurrencyAmount | None = None,
    lifetime_min_spend_target: CurrencyAmount | None = None,
) -> dict[str, Any]:
    """Edit an ad set's name, targeting, schedule, optimization goal, billing event, promoted object, status, spend caps or minimum spend targets, with before/after. Spend caps and minimum spend targets are in major units of the account currency (12.50 means 12.50, not cents); Meta applies daily ones only under a daily campaign budget and lifetime ones under a lifetime campaign budget. status ARCHIVED stops delivery for good: an archived ad set cannot be restored to active from this server. Budgets and bids are rejected here: use update_adset_budget, update_adset_bid_amount or update_adset_bid_strategy. targeting replaces the whole spec. A changed field whose prior value Meta did not return is named in previous_unavailable."""
    adset_id = blank_to_none(adset_id)
    if adset_id is None:
        raise ValidationError("adset_id is required.")
    for field_name, tool_name in _ADSET_MONEY_FIELD_TOOLS.items():
        if params and field_name in params:
            raise ValidationError(f"{field_name} cannot be changed with update_adset; use {tool_name}.")
    for field_name in _ADSET_SPEND_FIELDS:
        if params and field_name in params:
            raise ValidationError(
                f"{field_name} in params would be read as minor units; use the named {field_name} "
                "parameter, which takes major units of the account currency."
            )
    for field_name, message in _ADSET_UNSUPPORTED_MONEY_PARAMS.items():
        if params and field_name in params:
            raise ValidationError(message)
    spend = {
        "daily_spend_cap": daily_spend_cap,
        "lifetime_spend_cap": lifetime_spend_cap,
        "daily_min_spend_target": daily_min_spend_target,
        "lifetime_min_spend_target": lifetime_min_spend_target,
    }
    spend = {key: value for key, value in spend.items() if value is not None}
    for field_name, value in spend.items():
        validate_positive_amount(value, field_name=field_name)
    if status is not None and status not in _UPDATABLE_ADSET_STATUSES:
        raise ValidationError(
            f"status must be one of {sorted(_UPDATABLE_ADSET_STATUSES)}; delete with delete_adset."
        )
    if targeting is not None:
        validate_targeting_placements(targeting)
    new_start = _validate_adset_time(start_time, "start_time")
    new_end = _validate_adset_time(end_time, "end_time")
    if new_start is not None and new_end is not None and new_end <= new_start:
        raise ValidationError("end_time must be after start_time.")
    typed = {
        "name": name,
        "targeting": targeting,
        "start_time": start_time,
        "end_time": end_time,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "promoted_object": promoted_object,
        "status": status,
        **{field_name: spend.get(field_name) for field_name in _ADSET_SPEND_FIELDS},
    }
    payload: dict[str, Any] = {key: OMIT if value is None else value for key, value in typed.items()}
    add_validate_only(payload, validate_only=validate_only)
    payload = merge_graph_payload(payload, params)
    changes = {key: value for key, value in payload.items() if key != "execution_options"}
    if not changes:
        raise ValidationError("At least one field must be provided for update_adset.")
    readable = FIELD_REFERENCE["adset"]
    read_fields = [key for key in changes if key in readable]
    not_read = [key for key in changes if key not in readable]
    # A one-sided schedule edit is checked against the stored other bound.
    other_bound = None
    if new_start is not None and end_time is None:
        other_bound = "end_time"
    elif new_end is not None and start_time is None:
        other_bound = "start_time"
    bound_reads = [other_bound] if other_bound and other_bound not in read_fields else []
    account_read = ["account_id"] if spend else []
    client = get_graph_api_client()
    previous = await client.get_object(adset_id, fields=["id", *read_fields, *bound_reads, *account_read])
    currency = await resolve_account_currency(client, previous.get("account_id")) if spend else None
    for field_name, value in spend.items():
        payload[field_name] = to_minor_units(value, currency, field_name=field_name)
    previous_values = {key: previous[key] for key in read_fields if key in previous}
    for field_name in _ADSET_SPEND_FIELDS:
        if field_name in previous_values:
            previous_values[field_name] = from_minor_units(
                previous_values[field_name], currency, field_name=field_name
            )
    unavailable = {key: _OMITTED_PREVIOUS for key in read_fields if key not in previous}
    unavailable.update({key: _UNREAD_ADSET_PREVIOUS for key in not_read})
    stored = _stored_adset_time(previous.get(other_bound)) if other_bound else None
    if stored is not None and other_bound == "end_time" and new_start is not None and new_start >= stored:
        raise ValidationError(
            f"start_time must be before the ad set's stored end_time {previous.get('end_time')}; "
            "nothing was changed. Pass a later end_time as well."
        )
    if stored is not None and other_bound == "start_time" and new_end is not None and new_end <= stored:
        raise ValidationError(
            f"end_time must be after the ad set's stored start_time {previous.get('start_time')}; "
            "nothing was changed."
        )
    result = await client.update_object(adset_id, data=payload)
    response = mutation_response(
        action="update_adset",
        target={"adset_id": adset_id},
        previous=previous_values,
        current=changes,
    )
    response.update({
        "validation_only": validate_only,
        "applied": not validate_only,
        "previous_not_read": not_read,
        "result": result,
    })
    if unavailable:
        response["previous_unavailable"] = unavailable
    if spend:
        response["money_units"] = f"major units of the ad account currency ({currency})"
    return response
