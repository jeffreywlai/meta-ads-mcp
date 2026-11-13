"""Ad creation and creative-image inspection tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.field_reference import FIELD_REFERENCE
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, add_validate_only, merge_graph_payload
from meta_ads_mcp.input_compat import resolve_identifier_alias
from meta_ads_mcp.money import (
    resolve_account_currency,
    to_minor_units,
    validate_positive_amount,
)
from meta_ads_mcp.normalize import blank_to_none
from meta_ads_mcp.schemas import collection_response, creation_response, mutation_response
from meta_ads_mcp.tools.campaigns import (
    WholeNumber,
    build_copy_request,
    check_true_or_false,
    check_whole_number,
    params_half_of_time_pair,
    read_adlabels,
    reject_money_in_params,
    reject_named_and_params,
    run_copy,
    validate_adlabels,
    validate_schedule_time,
)
from meta_ads_mcp.tool_types import CurrencyAmount, GraphId, StrictStringList, coerce_strict_csv_string_list

AD_IMAGE_FIELDS = [
    "hash",
    "url",
    "permalink_url",
    "original_width",
    "original_height",
]

CREATIVE_IMAGE_FIELDS = [
    "id",
    "name",
    "image_hash",
    "thumbnail_url",
    "image_url",
    "object_story_spec",
    "asset_feed_spec",
]

VALID_AD_STATUSES = {"ACTIVE", "PAUSED"}
UPDATABLE_AD_STATUSES = {"ACTIVE", "PAUSED", "ARCHIVED"}
_OMITTED_PREVIOUS = "Meta returned no value for it; it may not have been set."
_UNREAD_AD_PREVIOUS = "Not a field this server reads back from an ad."
_CREATIVE_ALIAS_KEYS = ("creative_id", "id")


def _append_image_candidate(
    candidates: list[dict[str, Any]],
    seen_urls: set[str],
    *,
    url: str | None,
    source: str,
    image_hash: str | None = None,
) -> None:
    """Append a deduplicated image candidate URL."""
    if not url or url in seen_urls:
        return
    candidate: dict[str, Any] = {"url": url, "source": source}
    if image_hash:
        candidate["image_hash"] = image_hash
    candidates.append(candidate)
    seen_urls.add(url)


def _extract_hashes_and_candidates(creative: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Collect image hashes and directly available image URLs from a creative."""
    hashes: set[str] = set()
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    image_hash = creative.get("image_hash")
    if image_hash:
        hashes.add(image_hash)

    _append_image_candidate(
        candidates,
        seen_urls,
        url=creative.get("thumbnail_url"),
        source="creative.thumbnail_url",
    )
    _append_image_candidate(
        candidates,
        seen_urls,
        url=creative.get("image_url"),
        source="creative.image_url",
        image_hash=image_hash,
    )

    object_story_spec = creative.get("object_story_spec") or {}
    for key, field_name in (
        ("link_data", "picture"),
        ("template_data", "picture"),
        ("video_data", "image_url"),
        ("photo_data", "url"),
    ):
        story_part = object_story_spec.get(key) or {}
        _append_image_candidate(
            candidates,
            seen_urls,
            url=story_part.get(field_name),
            source=f"object_story_spec.{key}.{field_name}",
            image_hash=story_part.get("image_hash"),
        )
        if story_part.get("image_hash"):
            hashes.add(story_part["image_hash"])

    asset_feed_spec = creative.get("asset_feed_spec") or {}
    for image in asset_feed_spec.get("images", []):
        image_hash = image.get("hash") or image.get("image_hash")
        if image_hash:
            hashes.add(image_hash)
        for field_name in ("url", "image_url", "original_url"):
            _append_image_candidate(
                candidates,
                seen_urls,
                url=image.get(field_name),
                source=f"asset_feed_spec.images.{field_name}",
                image_hash=image_hash,
            )

    return sorted(hashes), candidates


def _configured_image_rules(creative: dict[str, Any], resolved_images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join explicit image labels to configuration rules, never inferred delivery."""
    asset_feed = creative.get("asset_feed_spec") or {}
    resolved_by_hash = {image["hash"]: image for image in resolved_images if image.get("hash")}
    rules = []
    for rule in asset_feed.get("asset_customization_rules", []):
        label = (rule.get("image_label") or {}).get("name")
        images = [
            {**image, **resolved_by_hash.get(image.get("hash") or image.get("image_hash"), {})}
            for image in asset_feed.get("images", [])
            if label and label in [item.get("name") for item in (image.get("adlabels") or [])]
        ]
        spec = rule.get("customization_spec") or {}
        positions = [
            {
                "publisher_platform": key.removesuffix("_positions"),
                "configured_position": position,
                "platform_position": "feed" if key == "instagram_positions" and position == "stream" else position,
            }
            for key, values in spec.items()
            if key.endswith("_positions") and isinstance(values, list)
            for position in values
        ]
        rules.append({
            "image_label": label,
            "images": images,
            "mapping_status": "matched" if images else "unresolved_image_label" if label else "no_image_label",
            "is_default": rule.get("is_default"),
            "priority": rule.get("priority"),
            "customization_spec": spec,
            "configured_reporting_positions": positions,
        })
    return rules


@mcp_server.tool()
async def create_ad(
    account_id: str,
    name: str,
    adset_id: str,
    creative_id: str | None = None,
    creative: dict[str, Any] | None = None,
    status: str = "PAUSED",
    bid_amount: CurrencyAmount | None = None,
    tracking_specs: list[dict[str, Any]] | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
    conversion_domain: str | None = None,
    adlabels: list[dict[str, Any]] | None = None,
    creative_spec: dict[str, Any] | None = None,
    ad_schedule_start_time: str | None = None,
    ad_schedule_end_time: str | None = None,
    display_sequence: WholeNumber | None = None,
    engagement_audience: bool | None = None,
) -> dict[str, Any]:
    """Create or validate an ad from a top-level or nested creative id, or from an inline creative_spec (Meta's ad creative fields, such as name and object_story_spec) instead of an existing creative. conversion_domain is the domain only (example.com), not a URL. adlabels is a list of {"name": ...} or {"id": ...}. ad_schedule_start_time and ad_schedule_end_time give this ad its own run window (ISO 8601 with a UTC offset, or Unix seconds); without them it follows the campaign's schedule, and Meta offers them only for Sales and App promotion campaigns. display_sequence is the ad's position in its campaign's sequence (a whole number from 0); engagement_audience true makes an audience of people who engage with this ad. A named parameter and the same key inside params cannot both be given. bid_amount is refused inside params, where Meta would read it in minor units; use the named bid_amount."""
    schedule_times = {"ad_schedule_start_time": ad_schedule_start_time, "ad_schedule_end_time": ad_schedule_end_time}
    named_later = {"display_sequence": display_sequence, "engagement_audience": engagement_audience}
    reject_named_and_params(
        {"conversion_domain": conversion_domain, "adlabels": adlabels, **schedule_times, **named_later}, params
    )
    reject_money_in_params(params, ("bid_amount",))
    if status not in VALID_AD_STATUSES:
        raise ValidationError(f"status must be one of {sorted(VALID_AD_STATUSES)}.")
    if display_sequence is not None:
        check_whole_number(display_sequence, "display_sequence", minimum=0)
    if engagement_audience is not None:
        check_true_or_false(engagement_audience, "engagement_audience")
    later_fields = {**schedule_times, **named_later}
    schedule_start = validate_schedule_time(ad_schedule_start_time, "ad_schedule_start_time")
    schedule_end = validate_schedule_time(ad_schedule_end_time, "ad_schedule_end_time")
    # One bound named and the other inside params: compare the pair as it will be sent.
    schedule_start = schedule_start or params_half_of_time_pair(ad_schedule_end_time, params, "ad_schedule_start_time")
    schedule_end = schedule_end or params_half_of_time_pair(ad_schedule_start_time, params, "ad_schedule_end_time")
    if schedule_start is not None and schedule_end is not None and schedule_end <= schedule_start:
        raise ValidationError("ad_schedule_end_time must be after ad_schedule_start_time.")
    if bid_amount is not None:
        validate_positive_amount(bid_amount, field_name="bid_amount")
    if conversion_domain is not None:
        if not isinstance(conversion_domain, str) or not conversion_domain.strip():
            raise ValidationError("conversion_domain must not be blank.")
        if any(character in conversion_domain for character in ":/?# "):
            raise ValidationError("conversion_domain takes the domain only, such as example.com, not a URL.")
    if adlabels is not None:
        validate_adlabels(adlabels)
    if creative_spec is not None:
        if creative_id is not None or creative is not None:
            raise ValidationError("Provide exactly one of creative_id, creative or creative_spec.")
        if not isinstance(creative_spec, dict) or not creative_spec:
            raise ValidationError("creative_spec must not be empty.")
        if {"creative_id", "id"} & set(creative_spec):
            raise ValidationError("creative_spec is for a new creative; to use an existing one, use creative_id.")
        return await _create_ad(
            account_id, name, adset_id, creative_spec, status, bid_amount, tracking_specs,
            conversion_domain, adlabels, validate_only, params, later_fields,
        )
    nested_creative_id: str | None = None
    if creative is not None:
        if not isinstance(creative, dict):
            raise ValidationError('creative must be an object such as {"creative_id": "123"}; or use creative_id.')
        # Counted, never quoted: a misplaced value (an email or a token) can arrive as a key.
        count = sum(1 for key in creative if key not in _CREATIVE_ALIAS_KEYS)
        if count:
            raise ValidationError(
                "creative accepts only creative_id or id when used as an alias; it has "
                f"{count} unknown {'key' if count == 1 else 'keys'} (not repeated here, since a misplaced "
                f"value can arrive as a key). Accepted keys: {', '.join(_CREATIVE_ALIAS_KEYS)}."
            )
        nested_id = creative.get("id")
        nested_creative_value = creative.get("creative_id")
        if any(
            value is not None and not isinstance(value, str)
            for value in (nested_id, nested_creative_value)
        ):
            raise ValidationError("creative.creative_id and creative.id must be strings.")
        nested_creative_id = resolve_identifier_alias(
            nested_creative_value,
            nested_id,
            primary_name="creative.creative_id",
            alias_name="creative.id",
        )
    resolved_creative_id = resolve_identifier_alias(
        creative_id,
        nested_creative_id,
        primary_name="creative_id",
        alias_name="creative.creative_id",
        required=True,
    )
    return await _create_ad(
        account_id, name, adset_id, {"creative_id": resolved_creative_id}, status, bid_amount,
        tracking_specs, conversion_domain, adlabels, validate_only, params, later_fields,
    )


async def _create_ad(
    account_id: str,
    name: str,
    adset_id: str,
    creative: dict[str, Any],
    status: str,
    bid_amount: float | None,
    tracking_specs: list[dict[str, Any]] | None,
    conversion_domain: str | None,
    adlabels: list[dict[str, Any]] | None,
    validate_only: bool,
    params: dict[str, Any] | None,
    later_fields: dict[str, Any],
) -> dict[str, Any]:
    """Send one validated ad creation; later_fields are named fields sent only when given."""
    payload: dict[str, Any] = {
        "name": name,
        "adset_id": adset_id,
        "creative": creative,
        "status": status,
        "bid_amount": OMIT,
        "tracking_specs": tracking_specs or OMIT,
    }
    # New named fields join the payload only when given, so a call that puts
    # them in params (as callers did before they existed) is sent unchanged.
    for key, value in (("conversion_domain", conversion_domain), ("adlabels", adlabels), *later_fields.items()):
        if value is not None:
            payload[key] = value
    add_validate_only(payload, validate_only=validate_only)
    merge_graph_payload(payload, params)
    client = get_graph_api_client()
    account_id = normalize_account_id(account_id)
    currency = (
        await resolve_account_currency(client, account_id)
        if bid_amount is not None
        else None
    )
    if bid_amount is not None:
        payload["bid_amount"] = to_minor_units(
            bid_amount,
            currency,
            field_name="bid_amount",
        )
    payload = merge_graph_payload(payload, params)

    created = await client.create_edge_object(
        account_id,
        "ads",
        data=payload,
    )
    return creation_response(
        action="create_ad",
        target={"account_id": account_id, "adset_id": adset_id},
        result=created,
        validate_only=validate_only,
    )


async def _get_ad_image(
    ad_id: str,
    client: Any,
    creatives: dict[str, dict[str, Any]],
    images: dict[tuple[str, str], dict[str, Any] | None],
) -> dict[str, Any]:
    """Read one ad, reusing creative and account-scoped image lookups in this call."""
    ad = await client.get_object(ad_id, fields=["id", "name", "account_id", "creative{id}"])
    creative_ref = ad.get("creative") or {}
    creative_id = creative_ref.get("id")
    creative: dict[str, Any] = {}
    if creative_id:
        if creative_id not in creatives:
            creatives[creative_id] = await client.get_object(creative_id, fields=CREATIVE_IMAGE_FIELDS)
        creative = creatives[creative_id]

    image_hashes, image_candidates = _extract_hashes_and_candidates(creative)
    account_id = ad.get("account_id")
    resolved_images: list[dict[str, Any]] = []
    if account_id and image_hashes:
        cache_account = normalize_account_id(account_id)
        missing = [image_hash for image_hash in image_hashes if (cache_account, image_hash) not in images]
        if missing:
            payload = await client.get_ad_images_by_hashes(account_id, hashes=missing, fields=AD_IMAGE_FIELDS)
            if (payload.get("paging") or {}).get("next"):
                raise ValidationError("Image lookup returned more pages; a complete image result could not be obtained.")
            found = {image["hash"]: image for image in payload.get("data", []) if image.get("hash")}
            for image_hash in missing:
                images[(cache_account, image_hash)] = found.get(image_hash)
        resolved_images = [
            image for image_hash in image_hashes
            if (image := images[(cache_account, image_hash)]) is not None
        ]
        existing_urls = {candidate["url"] for candidate in image_candidates}
        for image in resolved_images:
            for field_name in ("url", "permalink_url"):
                _append_image_candidate(
                    image_candidates,
                    existing_urls,
                    url=image.get(field_name),
                    source=f"adimages.{field_name}",
                    image_hash=image.get("hash"),
                )

    return {
        "item": {
            "ad_id": ad.get("id"),
            "ad_name": ad.get("name"),
            "account_id": account_id,
            "creative_id": creative_id,
            "creative_name": creative.get("name"),
            "image_hashes": image_hashes,
            "resolved_images": resolved_images,
            "image_candidates": image_candidates,
            "best_image_url": image_candidates[0]["url"] if image_candidates else None,
            "thumbnail_url": creative.get("thumbnail_url"),
            "configured_image_rules": _configured_image_rules(creative, resolved_images),
            "placement_delivery_verified": False,
            "unresolved_image_hashes": sorted(set(image_hashes) - {image.get("hash") for image in resolved_images}),
        },
        "summary": {
            "count": 1,
            "image_hash_count": len(image_hashes),
            "resolved_image_count": len(resolved_images),
            "candidate_count": len(image_candidates),
            "placement_note": "Rules describe configured image eligibility, not actual delivery. Unmatched labels and unknown positions are retained; no default image is guessed.",
        },
    }


@mcp_server.tool()
async def get_ad_image(ad_id: str | None = None, ad_ids: StrictStringList | None = None) -> dict[str, Any]:
    """Inspect image assets, crops, and configured placement rules for one ad_id or up to 100 ad_ids. Bulk calls reuse creative/image lookups; configuration is not proof of delivery."""
    if (ad_id is not None) == (ad_ids is not None):
        raise ValidationError("Provide exactly one of ad_id or ad_ids.")
    ids = [ad_id] if ad_id is not None else coerce_strict_csv_string_list(ad_ids)
    if not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value.strip() for value in ids):
        raise ValidationError("Ad IDs must be nonblank strings.")
    ids = list(dict.fromkeys(value.strip() for value in ids))
    if len(ids) > 100:
        raise ValidationError("ad_ids supports at most 100 distinct ads per call.")
    client = get_graph_api_client()
    creatives: dict[str, dict[str, Any]] = {}
    images: dict[tuple[str, str], dict[str, Any] | None] = {}
    results = [await _get_ad_image(value, client, creatives, images) for value in ids]
    if ad_id is not None:
        return results[0]
    return collection_response(
        [result["item"] for result in results],
        summary={
            "count": len(results), "complete": True, "unique_creative_count": len(creatives),
            "placement_note": results[0]["summary"]["placement_note"],
        },
    )


_AD_PARAMS_REJECTED = {
    "bid_amount": "bid_amount is a money field in minor units and is not supported by update_ad.",
    "bid_info": "bid_info is a legacy bid field in minor units and is not supported by update_ad.",
    "adset_id": "adset_id cannot be changed on an existing ad; Meta does not allow it.",
    "creative": "Use creative_id instead of params.creative.",
}


@mcp_server.tool()
async def update_ad(
    ad_id: str,
    name: str | None = None,
    creative_id: str | None = None,
    tracking_specs: list[dict[str, Any]] | None = None,
    status: str | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
    adlabels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Edit an ad's name, creative, tracking specs, status (ACTIVE, PAUSED or ARCHIVED) or labels, with before/after. ARCHIVED stops delivery for good: an archived ad cannot be restored to active from this server. adlabels (a list of {"name": ...} or {"id": ...}) replaces the ad's whole label set, so include any label to keep. Use validate_only to have Meta check the change without applying it. A changed field whose prior value Meta did not return is named in previous_unavailable."""
    ad_id = blank_to_none(ad_id)
    if ad_id is None:
        raise ValidationError("ad_id is required.")
    reject_named_and_params({"adlabels": adlabels}, params)
    if adlabels is not None:
        validate_adlabels(adlabels)
    if creative_id is not None and blank_to_none(creative_id) is None:
        raise ValidationError("creative_id must be a nonblank creative id when provided.")
    for key, message in _AD_PARAMS_REJECTED.items():
        if params and key in params:
            raise ValidationError(message)
    if status is not None and status not in UPDATABLE_AD_STATUSES:
        raise ValidationError(f"status must be one of {sorted(UPDATABLE_AD_STATUSES)}; delete with delete_ad.")
    resolved_creative_id = blank_to_none(creative_id)
    payload: dict[str, Any] = {
        "name": OMIT if name is None else name,
        "creative": OMIT if resolved_creative_id is None else {"creative_id": resolved_creative_id},
        "tracking_specs": OMIT if tracking_specs is None else tracking_specs,
        "status": OMIT if status is None else status,
    }
    # adlabels joins the payload only when given, so a params-only call is sent unchanged.
    if adlabels is not None:
        payload["adlabels"] = adlabels
    add_validate_only(payload, validate_only=validate_only)
    payload = merge_graph_payload(payload, params)
    changes = {key: value for key, value in payload.items() if key != "execution_options"}
    if not changes:
        raise ValidationError("At least one field must be provided for update_ad.")
    readable = FIELD_REFERENCE["ad"]
    read_fields = [
        "creative{id}" if key == "creative" else key
        for key in changes
        if key in readable
    ]
    not_read = [key for key in changes if key not in readable]
    client = get_graph_api_client()
    previous = await client.get_object(ad_id, fields=["id", *read_fields])
    result = await client.update_object(ad_id, data=payload)

    unavailable: dict[str, str] = {}
    incomplete: dict[str, str] = {}

    def _public(values: dict[str, Any], *, before: bool) -> dict[str, Any]:
        shaped: dict[str, Any] = {}
        for key in changes:
            public_key = "creative_id" if key == "creative" else key
            if before and key not in readable:
                unavailable[public_key] = _UNREAD_AD_PREVIOUS
                continue
            if before and key not in values:
                unavailable[public_key] = _OMITTED_PREVIOUS
                continue
            if key == "creative":
                creative = values.get("creative")
                if before:
                    shaped["creative_id"] = creative.get("id") if isinstance(creative, dict) else creative
                else:
                    shaped["creative_id"] = resolved_creative_id
            elif key == "adlabels" and before:
                shaped[key], note = read_adlabels(values.get(key))
                if note:
                    incomplete[key] = note
            else:
                shaped[key] = values.get(key)
        return shaped

    response = mutation_response(
        action="update_ad",
        target={"ad_id": ad_id},
        previous=_public(previous, before=True),
        current=_public(changes, before=False),
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
    return response


@mcp_server.tool()
async def duplicate_ad(
    ad_id: GraphId,
    adset_id: GraphId | None = None,
    status_option: str = "PAUSED",
    new_name: str | None = None,
    rename_strategy: str | None = None,
    rename_prefix: str | None = None,
    rename_suffix: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy an ad with Meta's copy endpoint and return the new ad id. The copy is PAUSED unless status_option is ACTIVE or INHERITED_FROM_SOURCE, and it keeps the same creative. adset_id puts the copy in another ad set; omitted, it stays in the original ad set. new_name renames the copy after it is made; rename_strategy, rename_prefix and rename_suffix control Meta's renaming, which by default appends " - Copy". dry_run returns the exact requests and sends nothing."""
    source_id = blank_to_none(ad_id) if isinstance(ad_id, str) else None
    if source_id is None:
        raise ValidationError("ad_id is required.")
    data = build_copy_request(
        deep_copy=None, status_option=status_option, rename_strategy=rename_strategy,
        rename_prefix=rename_prefix, rename_suffix=rename_suffix, new_name=new_name,
        parent=("adset_id", adset_id),
    )
    return await run_copy(
        get_graph_api_client, action="duplicate_ad", object_type="ad",
        source_id=source_id, data=data, new_name=new_name, dry_run=dry_run,
    )
