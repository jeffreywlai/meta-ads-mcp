"""Campaign management tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from meta_ads_mcp.api_compat import validate_targeting_placements
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaAdsError, ValidationError
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
from meta_ads_mcp.tool_types import (
    BidConstraints,
    CurrencyAmount,
    GraphId,
    StrictCurrencyAmount,
    StringList,
    coerce_csv_string_list,
)


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


ADLABELS_PREVIOUS_INCOMPLETE = (
    "Meta returned more labels than one page; previous lists the first page only. "
    "The update replaced the whole label set."
)
ADLABELS_PREVIOUS_UNREADABLE = "Meta returned labels in a shape this server could not read; previous shows none."


def read_adlabels(value: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Shape labels read from Meta as a list of {id, name}, with a note when the list may not be complete."""
    if value is None:
        return None, None
    note = None
    items = value
    if isinstance(value, dict):
        items = value.get("data")
        if (value.get("paging") or {}).get("next"):
            note = ADLABELS_PREVIOUS_INCOMPLETE
    if not isinstance(items, list):
        return None, ADLABELS_PREVIOUS_UNREADABLE
    labels = [
        {key: str(item[key]) if key == "id" else item[key] for key in ("id", "name") if item.get(key) is not None}
        for item in items
        if isinstance(item, dict)
    ]
    return labels, note

# Values from Meta's ad set reference (POST /act_{id}/adsets) and
# AdAccount.create_ad_set in facebook-python-business-sdk 26.0.2.
ADSET_DESTINATION_TYPES = (
    "WEBSITE", "APP", "MESSENGER", "APPLINKS_AUTOMATIC", "WHATSAPP", "INSTAGRAM_DIRECT", "FACEBOOK",
    "MESSAGING_MESSENGER_WHATSAPP", "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
    "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP", "MESSAGING_INSTAGRAM_DIRECT_WHATSAPP",
    "SHOP_AUTOMATIC", "ON_AD", "ON_POST", "ON_EVENT", "ON_VIDEO", "ON_PAGE", "INSTAGRAM_PROFILE",
    "FACEBOOK_PAGE", "INSTAGRAM_PROFILE_AND_FACEBOOK_PAGE", "INSTAGRAM_LIVE", "FACEBOOK_LIVE", "IMAGINE",
)
ADSET_OPTIMIZATION_SUB_EVENTS = (
    "NONE", "VIDEO_SOUND_ON", "TRIP_CONSIDERATION", "TRAVEL_INTENT", "TRAVEL_INTENT_NO_DESTINATION_INTENT",
    "TRAVEL_INTENT_BUCKET_01", "TRAVEL_INTENT_BUCKET_02", "TRAVEL_INTENT_BUCKET_03",
    "TRAVEL_INTENT_BUCKET_04", "TRAVEL_INTENT_BUCKET_05", "POST_INTERACTION",
)
# Scheduled budget increases (Meta's high demand periods): the budget_schedules
# reference and the budget_schedule_specs parameter on campaign and ad set creation.
BUDGET_SCHEDULE_VALUE_TYPES = ("ABSOLUTE", "MULTIPLIER")
_BUDGET_SCHEDULE_KEYS = ("time_start", "time_end", "budget_value", "budget_value_type")
MAX_BUDGET_SCHEDULE_SPECS = 50
_MIN_HIGH_DEMAND_SECONDS = 3 * 60 * 60
_MAX_SCHEDULED_BUDGET_MULTIPLE = 8


def _enum_value(value: Any, allowed: tuple[str, ...], field_name: str, *, params_hint: bool = False) -> str:
    """Check one of Meta's documented values; case and surrounding spaces are forgiven."""
    text = value.strip().upper() if isinstance(value, str) else ""
    if text not in allowed:
        hint = (
            f" A value Meta added after v26.0 can be sent as {field_name} inside params instead."
            if params_hint
            else ""
        )
        raise ValidationError(f"{field_name} must be one of {list(allowed)}, the values Meta's v26.0 reference lists.{hint}")
    return text


def _nonblank_string_list(value: Any, field_name: str) -> list[str]:
    """Check a non-empty list of non-blank strings."""
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{field_name} must be a list of non-blank strings, for example [\"day_parting\"].")
    if not value:
        raise ValidationError(f"{field_name} must not be empty; omit it instead.")
    return [item.strip() for item in value]


def _schedule_seconds(value: Any, field_name: str) -> int:
    """Read a high demand period bound (ISO 8601 with a UTC offset, or Unix seconds) as Unix seconds."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    parsed = _validate_adset_time(value, field_name) if isinstance(value, str) else None
    if parsed is None:
        raise ValidationError(f"{field_name} must be {_ADSET_TIME_FORMAT}.")
    return int(parsed.timestamp())


def check_budget_schedule_specs(
    specs: Any,
    *,
    daily_budget: float | None,
    lifetime_budget: float | None,
) -> list[dict[str, Any]]:
    """Validate scheduled budget increases before any network call; amounts stay in major units here."""
    _nonempty_object_list(specs, "budget_schedule_specs")
    if len(specs) > MAX_BUDGET_SCHEDULE_SPECS:
        raise ValidationError(f"budget_schedule_specs takes at most {MAX_BUDGET_SCHEDULE_SPECS} periods.")
    if lifetime_budget is not None:
        raise ValidationError("budget_schedule_specs works with a daily budget only, not lifetime_budget.")
    checked: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        label = f"budget_schedule_specs[{index}]"
        unknown = sorted(set(spec) - set(_BUDGET_SCHEDULE_KEYS))
        if unknown:
            raise ValidationError(
                f"{label} takes only time_start, time_end, budget_value and budget_value_type "
                f"(Meta supports one-time periods only); unexpected: {', '.join(unknown)}."
            )
        missing = [key for key in _BUDGET_SCHEDULE_KEYS if key not in spec]
        if missing:
            raise ValidationError(f"{label} is missing {', '.join(missing)}.")
        value_type = _enum_value(spec["budget_value_type"], BUDGET_SCHEDULE_VALUE_TYPES, f"{label}.budget_value_type")
        start = _schedule_seconds(spec["time_start"], f"{label}.time_start")
        end = _schedule_seconds(spec["time_end"], f"{label}.time_end")
        if end - start < _MIN_HIGH_DEMAND_SECONDS:
            raise ValidationError(f"{label} must end at least 3 hours after it starts.")
        value = spec["budget_value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{label}.budget_value must be a number, not {type(value).__name__}.")
        if value_type == "ABSOLUTE":
            validate_positive_amount(value, field_name=f"{label}.budget_value")
            if daily_budget is not None and daily_budget + value > _MAX_SCHEDULED_BUDGET_MULTIPLE * daily_budget:
                raise ValidationError(
                    f"{label}.budget_value {value} would take the daily budget {daily_budget} past 8 times "
                    "its size, which Meta does not allow. budget_value is the increase in major units "
                    "(20 means 20.00), not cents."
                )
        else:
            if value != value or value <= 0 or not float(value).is_integer():
                raise ValidationError(
                    f"{label}.budget_value must be a positive whole number for MULTIPLIER "
                    "(Meta's example: 140 means 140 percent)."
                )
            value = int(value)
        checked.append({"time_start": start, "time_end": end, "budget_value": value, "budget_value_type": value_type})
    return checked


def budget_schedule_needs_currency(checked: list[dict[str, Any]] | None) -> bool:
    """True when a checked schedule carries an absolute amount to convert."""
    return any(spec["budget_value_type"] == "ABSOLUTE" for spec in checked or [])


def encode_budget_schedule_specs(checked: list[dict[str, Any]], currency: str | None) -> list[dict[str, Any]]:
    """Convert absolute increases to Meta's minor units; multipliers are sent unchanged."""
    return [
        {
            **spec,
            "budget_value": to_minor_units(
                spec["budget_value"], currency, field_name=f"budget_schedule_specs[{index}].budget_value"
            ),
        }
        if spec["budget_value_type"] == "ABSOLUTE"
        else dict(spec)
        for index, spec in enumerate(checked)
    ]


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
    spend_cap: StrictCurrencyAmount | None = None,
    start_time: str | None = None,
    stop_time: str | None = None,
    promoted_object: dict[str, Any] | None = None,
    special_ad_category_country: StringList | None = None,
    adlabels: list[dict[str, Any]] | None = None,
    budget_schedule_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to create a new campaign shell before adding ad sets or ads. Budgets and spend_cap are in major units of the account currency (Meta's spend cap minimum is about 100 USD). start_time and stop_time take an ISO 8601 time with a UTC offset or Unix seconds. special_ad_category_country lists two-letter country codes. adlabels is a list of {"name": ...} or {"id": ...}. budget_schedule_specs schedules budget increases for busy periods (daily budgets only, at most 50 periods of at least 3 hours, account permission required): each item has time_start and time_end (ISO 8601 with a UTC offset, or Unix seconds; sent as Unix seconds), budget_value_type ABSOLUTE or MULTIPLIER, and budget_value, which for ABSOLUTE is the increase in major units of the account currency (20 means 20.00; converted to minor units) and for MULTIPLIER is a whole number sent unchanged (Meta's example: 140 means 140 percent). A named parameter and the same key inside params cannot both be given."""
    reject_named_and_params(
        {
            "spend_cap": spend_cap,
            "start_time": start_time,
            "stop_time": stop_time,
            "promoted_object": promoted_object,
            "special_ad_category_country": special_ad_category_country,
            "adlabels": adlabels,
            "budget_schedule_specs": budget_schedule_specs,
        },
        params,
        money=("spend_cap", "budget_schedule_specs"),
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
    schedule = (
        None
        if budget_schedule_specs is None
        else check_budget_schedule_specs(
            budget_schedule_specs, daily_budget=daily_budget, lifetime_budget=lifetime_budget
        )
    )
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
        ("budget_schedule_specs", schedule),
    ):
        if value is not None:
            payload[key] = value
    add_validate_only(payload, validate_only=validate_only)
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    currency = None
    if (
        any(value is not None for value in (daily_budget, lifetime_budget, spend_cap))
        or budget_schedule_needs_currency(schedule)
    ):
        currency = await resolve_account_currency(client, account_id)
    _encode_budget_field(payload, "daily_budget", daily_budget, currency=currency)
    _encode_budget_field(payload, "lifetime_budget", lifetime_budget, currency=currency)
    if spend_cap is not None:
        payload["spend_cap"] = to_minor_units(spend_cap, currency, field_name="spend_cap")
    if schedule is not None:
        payload["budget_schedule_specs"] = encode_budget_schedule_specs(schedule, currency)
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
    spend_cap: StrictCurrencyAmount | None = None,
    start_time: str | None = None,
    stop_time: str | None = None,
    adlabels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use this when the user already has a campaign id and wants to change core campaign settings. Budgets and spend_cap are in major units of the account currency (Meta's spend cap minimum is about 100 USD). start_time and stop_time take an ISO 8601 time with a UTC offset or Unix seconds; one given alone is checked against the stored other one. adlabels (a list of {"name": ...} or {"id": ...}) replaces the campaign's whole label set, so include any label to keep. The prior value of each named field is returned in previous. A named parameter and the same key inside params cannot both be given."""
    named = {"spend_cap": spend_cap, "start_time": start_time, "stop_time": stop_time, "adlabels": adlabels}
    reject_named_and_params(named, params, money=("spend_cap",))
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
    if adlabels is not None:
        validate_adlabels(adlabels)
    if all(
        value is None
        for value in (name, status, objective, daily_budget, lifetime_budget, *named.values())
    ) and not params:
        raise ValidationError("At least one field must be provided for update_campaign.")
    payload: dict[str, Any] = {
        "name": name if name is not None else OMIT,
        "status": status if status is not None else OMIT,
        "objective": objective if objective is not None else OMIT,
        "daily_budget": OMIT,
        "lifetime_budget": OMIT,
    }
    # New named fields join the payload and the pre-read only when given, so a
    # call that puts them in params (as callers did before) is sent unchanged.
    named_given = [key for key, value in named.items() if value is not None]
    for key in named_given:
        payload[key] = named[key]
    merge_graph_payload(payload, params)
    other_bound = None
    if new_start is not None and stop_time is None:
        other_bound = "stop_time"
    elif new_stop is not None and start_time is None:
        other_bound = "start_time"
    extra_reads = [*named_given, *([other_bound] if other_bound else [])]
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
            *extra_reads,
        ],
    )
    stored = _stored_adset_time(previous.get(other_bound)) if other_bound else None
    if stored is not None and other_bound == "stop_time" and new_start is not None and new_start >= stored:
        raise ValidationError(
            f"start_time must be before the campaign's stored stop_time {previous.get('stop_time')}; "
            "nothing was changed. Pass a later stop_time as well."
        )
    if stored is not None and other_bound == "start_time" and new_stop is not None and new_stop <= stored:
        raise ValidationError(
            f"stop_time must be after the campaign's stored start_time {previous.get('start_time')}; "
            "nothing was changed."
        )
    has_money = any(
        value is not None
        for value in (
            daily_budget,
            lifetime_budget,
            previous.get("daily_budget"),
            previous.get("lifetime_budget"),
            spend_cap,
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
    if spend_cap is not None:
        payload["spend_cap"] = to_minor_units(spend_cap, currency, field_name="spend_cap")
    for key in named_given:
        current[key] = named[key]
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
    unavailable: dict[str, str] = {}
    incomplete: dict[str, str] = {}
    for key in named_given:
        if key not in previous:
            previous_response[key] = None
            unavailable[key] = _OMITTED_PREVIOUS
        elif key == "spend_cap":
            previous_response[key] = from_minor_units(previous.get(key), currency, field_name=key)
        elif key == "adlabels":
            previous_response[key], note = read_adlabels(previous.get(key))
            if note:
                incomplete[key] = note
        else:
            previous_response[key] = previous.get(key)
    await client.update_object(campaign_id, data=payload)
    response = mutation_response(
        action="update_campaign",
        target={"campaign_id": campaign_id},
        previous=previous_response,
        current=current,
    )
    if unavailable:
        response["previous_unavailable"] = unavailable
    if incomplete:
        response["previous_incomplete"] = incomplete
    if spend_cap is not None:
        response["money_units"] = f"major units of the ad account currency ({currency})"
    return response


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
    daily_spend_cap: StrictCurrencyAmount | None = None,
    lifetime_spend_cap: StrictCurrencyAmount | None = None,
    daily_min_spend_target: StrictCurrencyAmount | None = None,
    lifetime_min_spend_target: StrictCurrencyAmount | None = None,
    dsa_beneficiary: str | None = None,
    dsa_payor: str | None = None,
    adlabels: list[dict[str, Any]] | None = None,
    destination_type: str | None = None,
    pacing_type: StringList | None = None,
    is_dynamic_creative: bool | None = None,
    optimization_sub_event: str | None = None,
    budget_schedule_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create or validate an ad set with targeting, budget, bid strategy, and bid constraints. Budgets, bids, spend caps and minimum spend targets are in major units of the account currency; daily caps and targets need a daily campaign budget, lifetime ones a lifetime campaign budget. destination_type says where ads send people (for example WEBSITE, MESSENGER, WHATSAPP, INSTAGRAM_DIRECT, ON_POST, INSTAGRAM_PROFILE); messaging and profile goals need it. frequency_control_specs, attribution_spec and adset_schedule are Meta's lists of objects; adset_schedule (day parting) needs a lifetime budget and the pacing_type parameter set to ["day_parting"]. is_dynamic_creative true makes a dynamic creative ad set (one ad, dynamic creative only). optimization_sub_event refines the goal, for example VIDEO_SOUND_ON. budget_schedule_specs schedules budget increases for busy periods (daily budgets only, at most 50 periods of at least 3 hours): each item has time_start and time_end (ISO 8601 with a UTC offset, or Unix seconds; sent as Unix seconds), budget_value_type ABSOLUTE or MULTIPLIER, and budget_value, which for ABSOLUTE is the increase in major units of the account currency (converted to minor units) and for MULTIPLIER a whole number sent unchanged (Meta's example: 140 means 140 percent). dsa_beneficiary and dsa_payor name who benefits from and who pays for ads shown in the EU. A named parameter and the same key inside params cannot both be given."""
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
    named_delivery = {
        "destination_type": destination_type,
        "pacing_type": pacing_type,
        "is_dynamic_creative": is_dynamic_creative,
        "optimization_sub_event": optimization_sub_event,
        "budget_schedule_specs": budget_schedule_specs,
    }
    reject_named_and_params(
        {
            **named_other, **named_spend, "dsa_beneficiary": dsa_beneficiary, "dsa_payor": dsa_payor,
            "adlabels": adlabels, **named_delivery,
        },
        params,
        money=(*_ADSET_SPEND_FIELDS, "budget_schedule_specs"),
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
    if destination_type is not None:
        named_delivery["destination_type"] = _enum_value(
            destination_type, ADSET_DESTINATION_TYPES, "destination_type", params_hint=True
        )
    if pacing_type is not None:
        named_delivery["pacing_type"] = _nonblank_string_list(coerce_csv_string_list(pacing_type), "pacing_type")
    if is_dynamic_creative is not None and not isinstance(is_dynamic_creative, bool):
        raise ValidationError("is_dynamic_creative must be true or false.")
    if optimization_sub_event is not None:
        named_delivery["optimization_sub_event"] = _enum_value(
            optimization_sub_event, ADSET_OPTIMIZATION_SUB_EVENTS, "optimization_sub_event", params_hint=True
        )
    schedule = (
        None
        if budget_schedule_specs is None
        else check_budget_schedule_specs(
            budget_schedule_specs, daily_budget=daily_budget, lifetime_budget=lifetime_budget
        )
    )
    named_delivery["budget_schedule_specs"] = schedule
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
        *named_delivery.items(),
    ):
        if value is not None:
            payload[key] = value
    add_validate_only(payload, validate_only=validate_only)
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    currency = None
    if (
        any(value is not None for value in (bid_amount, daily_budget, lifetime_budget, *named_spend.values()))
        or budget_schedule_needs_currency(schedule)
    ):
        currency = await resolve_account_currency(client, account_id)
    if schedule is not None:
        payload["budget_schedule_specs"] = encode_budget_schedule_specs(schedule, currency)
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


def validate_schedule_time(value: str | None, field_name: str) -> datetime | None:
    """Check a schedule time for another tool module: ISO 8601 with a UTC offset, or Unix seconds."""
    return _validate_adset_time(value, field_name)


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
    daily_spend_cap: StrictCurrencyAmount | None = None,
    lifetime_spend_cap: StrictCurrencyAmount | None = None,
    daily_min_spend_target: StrictCurrencyAmount | None = None,
    lifetime_min_spend_target: StrictCurrencyAmount | None = None,
    adlabels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Edit an ad set's name, targeting, schedule, optimization goal, billing event, promoted object, status, spend caps, minimum spend targets or labels, with before/after. Spend caps and minimum spend targets are in major units of the account currency (12.50 means 12.50, not cents); Meta applies daily ones only under a daily campaign budget and lifetime ones under a lifetime campaign budget. status ARCHIVED stops delivery for good: an archived ad set cannot be restored to active from this server. Budgets and bids are rejected here: use update_adset_budget, update_adset_bid_amount or update_adset_bid_strategy. targeting replaces the whole spec. adlabels (a list of {"name": ...} or {"id": ...}) replaces the ad set's whole label set, so include any label to keep. A changed field whose prior value Meta did not return is named in previous_unavailable."""
    adset_id = blank_to_none(adset_id)
    if adset_id is None:
        raise ValidationError("adset_id is required.")
    reject_named_and_params({"adlabels": adlabels}, params)
    if adlabels is not None:
        validate_adlabels(adlabels)
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
    # adlabels joins the payload only when given, so a params-only call is sent unchanged.
    if adlabels is not None:
        payload["adlabels"] = adlabels
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
    incomplete: dict[str, str] = {}
    if "adlabels" in previous_values:
        previous_values["adlabels"], note = read_adlabels(previous_values["adlabels"])
        if note:
            incomplete["adlabels"] = note
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
    if incomplete:
        response["previous_incomplete"] = incomplete
    if spend:
        response["money_units"] = f"major units of the ad account currency ({currency})"
    return response


# Meta's copies edge on campaigns, ad sets and ads (POST /{id}/copies).
COPY_STATUS_OPTIONS = ("PAUSED", "ACTIVE", "INHERITED_FROM_SOURCE")
COPY_RENAME_STRATEGIES = ("DEEP_RENAME", "ONLY_TOP_LEVEL_RENAME", "NO_RENAME")
_NEW_COPY_PLACEHOLDER = "<id of the new copy>"


def _required_id(value: str | None, field_name: str) -> str:
    """Check a required object id that must not be blank."""
    text = blank_to_none(value) if isinstance(value, str) else None
    if text is None:
        raise ValidationError(f"{field_name} is required.")
    return text


def build_copy_request(
    *,
    deep_copy: bool | None,
    status_option: str,
    rename_strategy: str | None,
    rename_prefix: str | None,
    rename_suffix: str | None,
    new_name: str | None,
    start_time: str | None = None,
    end_time: str | None = None,
    parent: tuple[str, str | None] | None = None,
) -> dict[str, Any]:
    """Validate copy options and build the body for POST /{id}/copies."""
    if status_option not in COPY_STATUS_OPTIONS:
        raise ValidationError(f"status_option must be one of {list(COPY_STATUS_OPTIONS)}.")
    if rename_strategy is not None and rename_strategy not in COPY_RENAME_STRATEGIES:
        raise ValidationError(f"rename_strategy must be one of {list(COPY_RENAME_STRATEGIES)}.")
    if new_name is not None:
        _nonblank_text(new_name, "new_name")
    new_start = _validate_adset_time(start_time, "start_time")
    new_end = _validate_adset_time(end_time, "end_time")
    if new_start is not None and new_end is not None and new_end <= new_start:
        raise ValidationError("end_time must be after start_time.")
    data: dict[str, Any] = {}
    if deep_copy:
        data["deep_copy"] = True
    data["status_option"] = status_option
    rename_options = {
        key: value
        for key, value in (
            ("rename_strategy", rename_strategy),
            ("rename_prefix", rename_prefix),
            ("rename_suffix", rename_suffix),
        )
        if value is not None
    }
    if rename_options:
        data["rename_options"] = rename_options
    if start_time is not None:
        data["start_time"] = start_time
    if end_time is not None:
        data["end_time"] = end_time
    if parent is not None and parent[1] is not None:
        data[parent[0]] = _required_id(parent[1], parent[0])
    return data


async def run_copy(
    client_factory: Any,
    *,
    action: str,
    object_type: str,
    source_id: str,
    data: dict[str, Any],
    new_name: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Send a copy request (or show it under dry_run), then apply new_name to the copy."""
    id_key = f"{object_type}_id"
    response: dict[str, Any] = {
        "ok": True,
        "action": action,
        "target": {id_key: source_id},
        "dry_run": dry_run,
        "status_option": data["status_option"],
    }
    requests = [{"method": "POST", "path": f"{source_id}/copies", "data": data}]
    if new_name is not None:
        requests.append({"method": "POST", "path": _NEW_COPY_PLACEHOLDER, "data": {"name": new_name}})
    if dry_run:
        response.update({"sent": False, "requests": requests})
        return response
    client = client_factory()
    result = await client.create_edge_object(source_id, "copies", data=data)
    raw_new_id = result.get(f"copied_{object_type}_id") if isinstance(result, dict) else None
    new_id = str(raw_new_id) if raw_new_id not in (None, "") else None
    copied = result.get("ad_object_ids") if isinstance(result, dict) else None
    response.update({
        "sent": True,
        "new_id": new_id,
        "new_ids": {id_key: new_id},
        "copied_objects": [
            {
                "type": item.get("ad_object_type"),
                "source_id": None if item.get("source_id") is None else str(item.get("source_id")),
                "copied_id": None if item.get("copied_id") is None else str(item.get("copied_id")),
            }
            for item in (copied if isinstance(copied, list) else [])
            if isinstance(item, dict)
        ],
        "result": result,
    })
    if new_id is None:
        response["warning"] = (
            "Meta's response did not include the new copy's id; find the copy under its parent "
            "before acting on it."
        )
    if new_name is None:
        return response
    rename: dict[str, Any] = {"requested": new_name, "applied": False}
    if new_id is None:
        rename["reason"] = "The copy's id is unknown, so it was not renamed."
    else:
        try:
            await client.update_object(new_id, data={"name": new_name})
        except MetaAdsError as exc:
            public = getattr(exc, "to_public_dict", None)
            rename["error"] = public() if callable(public) else {"type": type(exc).__name__}
        except Exception as exc:  # The copy exists; keep its id in the answer whatever the rename did.
            rename["error"] = {"type": type(exc).__name__}
        else:
            rename["applied"] = True
        if not rename["applied"]:
            rename["next_step"] = (
                f"The copy {new_id} exists with Meta's default name; rename it with update_{object_type}."
            )
    response["rename"] = rename
    return response


@mcp_server.tool()
async def duplicate_campaign(
    campaign_id: GraphId,
    deep_copy: bool = False,
    status_option: str = "PAUSED",
    new_name: str | None = None,
    rename_strategy: str | None = None,
    rename_prefix: str | None = None,
    rename_suffix: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy a campaign with Meta's copy endpoint and return the new ids. The copy is PAUSED unless status_option is ACTIVE or INHERITED_FROM_SOURCE. deep_copy also copies its ad sets and ads; Meta allows at most 3 ads in total for a deep copy made this way. start_time and end_time (deep copy only) set the copied ad sets' schedule. new_name renames the copy after it is made; rename_strategy (DEEP_RENAME, ONLY_TOP_LEVEL_RENAME, NO_RENAME), rename_prefix and rename_suffix control Meta's renaming, which by default appends " - Copy". dry_run returns the exact requests and sends nothing."""
    source_id = _required_id(campaign_id, "campaign_id")
    if not deep_copy and (start_time is not None or end_time is not None):
        raise ValidationError("start_time and end_time apply to a campaign copy only with deep_copy=true.")
    data = build_copy_request(
        deep_copy=deep_copy, status_option=status_option, rename_strategy=rename_strategy,
        rename_prefix=rename_prefix, rename_suffix=rename_suffix, new_name=new_name,
        start_time=start_time, end_time=end_time,
    )
    return await run_copy(
        get_graph_api_client, action="duplicate_campaign", object_type="campaign",
        source_id=source_id, data=data, new_name=new_name, dry_run=dry_run,
    )


@mcp_server.tool()
async def duplicate_adset(
    adset_id: GraphId,
    campaign_id: GraphId | None = None,
    deep_copy: bool = False,
    status_option: str = "PAUSED",
    new_name: str | None = None,
    rename_strategy: str | None = None,
    rename_prefix: str | None = None,
    rename_suffix: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy an ad set with Meta's copy endpoint and return the new ids. The copy is PAUSED unless status_option is ACTIVE or INHERITED_FROM_SOURCE. campaign_id puts the copy under another campaign, whose settings such as budget it then follows; omitted, it stays in the original campaign. deep_copy also copies its ads; Meta allows at most 3 ads for a deep copy made this way. start_time and end_time (ISO 8601 with a UTC offset, or Unix seconds; end_time 0 means no end) override the copied schedule. new_name renames the copy after it is made; rename_strategy, rename_prefix and rename_suffix control Meta's renaming. dry_run returns the exact requests and sends nothing."""
    source_id = _required_id(adset_id, "adset_id")
    data = build_copy_request(
        deep_copy=deep_copy, status_option=status_option, rename_strategy=rename_strategy,
        rename_prefix=rename_prefix, rename_suffix=rename_suffix, new_name=new_name,
        start_time=start_time, end_time=end_time, parent=("campaign_id", campaign_id),
    )
    return await run_copy(
        get_graph_api_client, action="duplicate_adset", object_type="adset",
        source_id=source_id, data=data, new_name=new_name, dry_run=dry_run,
    )
