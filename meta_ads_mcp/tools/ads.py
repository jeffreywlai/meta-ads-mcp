"""Ad creation and creative-image inspection tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, add_validate_only, merge_graph_payload
from meta_ads_mcp.input_compat import resolve_identifier_alias
from meta_ads_mcp.money import (
    resolve_account_currency,
    to_minor_units,
    validate_positive_amount,
)
from meta_ads_mcp.schemas import creation_response

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


@mcp_server.tool()
async def create_ad(
    account_id: str,
    name: str,
    adset_id: str,
    creative_id: str | None = None,
    creative: dict[str, Any] | None = None,
    status: str = "PAUSED",
    bid_amount: float | None = None,
    tracking_specs: list[dict[str, Any]] | None = None,
    validate_only: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or validate an ad from a top-level or nested creative id."""
    if status not in VALID_AD_STATUSES:
        raise ValidationError(f"status must be one of {sorted(VALID_AD_STATUSES)}.")
    if bid_amount is not None:
        validate_positive_amount(bid_amount, field_name="bid_amount")
    nested_creative_id: str | None = None
    if creative is not None:
        unknown_keys = sorted(set(creative) - {"creative_id", "id"})
        if unknown_keys:
            raise ValidationError(
                "creative accepts only creative_id or id when used as an alias; "
                f"unexpected fields: {', '.join(unknown_keys)}."
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
    payload: dict[str, Any] = {
        "name": name,
        "adset_id": adset_id,
        "creative": {"creative_id": resolved_creative_id},
        "status": status,
        "bid_amount": OMIT,
        "tracking_specs": tracking_specs or OMIT,
    }
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


@mcp_server.tool()
async def get_ad_image(ad_id: str) -> dict[str, Any]:
    """Use this when the user wants Claude to inspect the main image assets behind an existing ad."""
    client = get_graph_api_client()
    ad = await client.get_object(ad_id, fields=["id", "name", "account_id", "creative{id}"])
    creative_ref = ad.get("creative") or {}
    creative_id = creative_ref.get("id")
    creative: dict[str, Any] = {}
    if creative_id:
        creative = await client.get_object(creative_id, fields=CREATIVE_IMAGE_FIELDS)

    image_hashes, image_candidates = _extract_hashes_and_candidates(creative)
    account_id = ad.get("account_id")
    resolved_images: list[dict[str, Any]] = []
    if account_id and image_hashes:
        payload = await client.get_ad_images_by_hashes(
            account_id,
            hashes=image_hashes,
            fields=AD_IMAGE_FIELDS,
        )
        resolved_images = payload.get("data", [])
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
        },
        "summary": {
            "count": 1,
            "image_hash_count": len(image_hashes),
            "resolved_image_count": len(resolved_images),
            "candidate_count": len(image_candidates),
        },
    }
