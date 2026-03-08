"""Ad creation and creative-image inspection tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id


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
    creative_id: str,
    status: str = "PAUSED",
    bid_amount: float | None = None,
    tracking_specs: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an ad from an existing creative and attach it to an ad set."""
    if status not in VALID_AD_STATUSES:
        raise ValidationError(f"status must be one of {sorted(VALID_AD_STATUSES)}.")
    payload: dict[str, Any] = {
        "name": name,
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": status,
    }
    if bid_amount is not None:
        payload["bid_amount"] = int(bid_amount * 100)
    if tracking_specs:
        payload["tracking_specs"] = tracking_specs
    if params:
        payload.update(params)

    client = get_graph_api_client()
    created = await client.create_edge_object(
        normalize_account_id(account_id),
        "ads",
        data=payload,
    )
    return {
        "ok": True,
        "action": "create_ad",
        "target": {"account_id": normalize_account_id(account_id), "adset_id": adset_id},
        "created": created,
    }


@mcp_server.tool()
async def get_ad_image(ad_id: str) -> dict[str, Any]:
    """Resolve the primary image URLs for an ad's creative."""
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
