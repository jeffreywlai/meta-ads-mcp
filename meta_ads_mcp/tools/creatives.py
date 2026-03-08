"""Creative and experiment tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import normalize_collection
from meta_ads_mcp.schemas import mutation_response


CREATIVE_FIELDS = [
    "id",
    "name",
    "title",
    "body",
    "status",
    "object_story_spec",
    "asset_feed_spec",
    "image_hash",
    "thumbnail_url",
]


def _merge_params(base: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    """Merge optional params."""
    merged = dict(base)
    if extra:
        merged.update(extra)
    return merged


def _ensure_v25_creative_payload(payload: Any) -> None:
    """Reject known deprecated Instagram creative keys for newer API versions."""
    deprecated_keys = {
        "instagram_actor_id": "Use instagram_user_id in object_story_spec instead.",
        "instagram_story_id": "Use instagram_media_id or current Instagram story/media fields instead.",
    }
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in deprecated_keys:
                raise ValidationError(
                    f"{key} is deprecated for newer Marketing API versions. "
                    f"{deprecated_keys[key]}"
                )
            _ensure_v25_creative_payload(value)
    elif isinstance(payload, list):
        for item in payload:
            _ensure_v25_creative_payload(item)


@mcp_server.tool()
async def list_creatives(
    account_id: str,
    limit: int = 50,
    after: str | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """List ad creatives."""
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    payload = await client.list_objects(
        normalize_account_id(account_id),
        "adcreatives",
        fields=fields or CREATIVE_FIELDS,
        params=params,
    )
    return normalize_collection(payload)


@mcp_server.tool()
async def create_ad_creative(
    account_id: str,
    name: str,
    object_story_spec: dict[str, Any] | None = None,
    asset_feed_spec: dict[str, Any] | None = None,
    title: str | None = None,
    body: str | None = None,
    image_hash: str | None = None,
    degrees_of_freedom_spec: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an ad creative."""
    if object_story_spec:
        _ensure_v25_creative_payload(object_story_spec)
    if asset_feed_spec:
        _ensure_v25_creative_payload(asset_feed_spec)
    payload: dict[str, Any] = {"name": name}
    if object_story_spec:
        payload["object_story_spec"] = object_story_spec
    if asset_feed_spec:
        payload["asset_feed_spec"] = asset_feed_spec
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    if image_hash:
        payload["image_hash"] = image_hash
    if degrees_of_freedom_spec:
        payload["degrees_of_freedom_spec"] = degrees_of_freedom_spec
    client = get_graph_api_client()
    created = await client.create_edge_object(
        normalize_account_id(account_id),
        "adcreatives",
        data=_merge_params(payload, params),
    )
    return {
        "ok": True,
        "action": "create_ad_creative",
        "target": {"account_id": normalize_account_id(account_id)},
        "created": created,
    }


@mcp_server.tool()
async def preview_ad(
    ad_id: str | None = None,
    account_id: str | None = None,
    creative_id: str | None = None,
    creative: dict[str, Any] | None = None,
    ad_format: str = "DESKTOP_FEED_STANDARD",
) -> dict[str, Any]:
    """Generate an ad preview for an existing ad or creative payload."""
    if creative:
        _ensure_v25_creative_payload(creative)
    if not ad_id and not creative_id and not creative:
        raise ValidationError("Provide ad_id, creative_id, or creative.")
    if (creative_id or creative) and not account_id and not ad_id:
        raise ValidationError("account_id is required when previewing from creative_id or creative.")
    client = get_graph_api_client()
    result = await client.preview_ad(
        ad_id=ad_id,
        account_id=normalize_account_id(account_id) if account_id else None,
        creative_id=creative_id,
        creative=creative,
        ad_format=ad_format,
    )
    return {"item": result, "summary": {"count": 1}}


@mcp_server.tool()
async def upload_creative_asset(
    account_id: str,
    file_path: str | None = None,
    image_url: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Upload an image asset for creative use."""
    if bool(file_path) == bool(image_url):
        raise ValidationError("Provide exactly one of file_path or image_url.")
    client = get_graph_api_client()
    result = await client.upload_ad_image(
        normalize_account_id(account_id),
        file_path=file_path,
        image_url=image_url,
        name=name,
    )
    return {
        "ok": True,
        "action": "upload_creative_asset",
        "target": {"account_id": normalize_account_id(account_id)},
        "uploaded": result,
    }


@mcp_server.tool()
async def setup_ab_test(
    owner_id: str,
    name: str,
    description: str | None = None,
    ad_study_type: str = "SPLIT_TEST_V2",
    cell_ids: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    observation_type: str | None = None,
    confidence_level: float | None = None,
    hypothesis: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Meta ad study / split test."""
    payload: dict[str, Any] = {"name": name, "type": ad_study_type}
    if description:
        payload["description"] = description
    if cell_ids:
        payload["cell_ids"] = cell_ids
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if observation_type:
        payload["observation_type"] = observation_type
    if confidence_level is not None:
        payload["confidence_level"] = confidence_level
    if hypothesis:
        payload["hypothesis"] = hypothesis
    client = get_graph_api_client()
    created = await client.create_edge_object(owner_id, "ad_studies", data=_merge_params(payload, params))
    return {
        "ok": True,
        "action": "setup_ab_test",
        "target": {"owner_id": owner_id},
        "created": created,
    }


@mcp_server.tool()
async def update_creative(
    creative_id: str,
    name: str | None = None,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    object_story_spec: dict[str, Any] | None = None,
    asset_feed_spec: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update an ad creative."""
    if object_story_spec is not None:
        _ensure_v25_creative_payload(object_story_spec)
    if asset_feed_spec is not None:
        _ensure_v25_creative_payload(asset_feed_spec)
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if status is not None:
        payload["status"] = status
    if object_story_spec is not None:
        payload["object_story_spec"] = object_story_spec
    if asset_feed_spec is not None:
        payload["asset_feed_spec"] = asset_feed_spec
    if params:
        payload.update(params)
    if not payload:
        raise ValidationError("At least one field must be provided for update_creative.")
    client = get_graph_api_client()
    previous = await client.get_object(creative_id, fields=CREATIVE_FIELDS)
    await client.update_object(creative_id, data=payload)
    return mutation_response(
        action="update_creative",
        target={"creative_id": creative_id},
        previous={
            "name": previous.get("name"),
            "title": previous.get("title"),
            "body": previous.get("body"),
            "status": previous.get("status"),
        },
        current=payload,
    )


@mcp_server.tool()
async def delete_creative(creative_id: str) -> dict[str, Any]:
    """Delete an ad creative."""
    client = get_graph_api_client()
    result = await client.delete_object(creative_id)
    return {
        "ok": True,
        "action": "delete_creative",
        "target": {"creative_id": creative_id},
        "result": result,
    }
