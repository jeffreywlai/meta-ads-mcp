"""Creative and experiment tools."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import FieldList, StringList

CREATIVE_FIELDS = [
    "id",
    "name",
    "title",
    "body",
    "status",
    "object_story_spec",
    "asset_feed_spec",
    "degrees_of_freedom_spec",
    "url_tags",
    "image_hash",
    "thumbnail_url",
]


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
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Use this when the user needs creative ids or lightweight creative metadata before previewing or creating ads."""
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
async def get_creative(
    creative_id: str,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Get, fetch, look up, show, or inspect what is in one creative by id, including full details for url_tags, asset_feed_spec, object_story_spec, and degrees_of_freedom_spec."""
    creative_id = blank_to_none(creative_id)
    if creative_id is None:
        raise ValidationError("creative_id is required.")
    client = get_graph_api_client()
    creative = await client.get_object(creative_id, fields=fields or CREATIVE_FIELDS)
    return {"item": creative, "summary": {"count": 1}}


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
    """Build a reusable creative, including a replacement with an edited asset_feed_spec and preserved placement customization rules; attach it to an existing ad with update_ad_creative."""
    if object_story_spec:
        _ensure_v25_creative_payload(object_story_spec)
    if asset_feed_spec:
        _ensure_v25_creative_payload(asset_feed_spec)
    payload: dict[str, Any] = {
        "name": name,
        "object_story_spec": object_story_spec or OMIT,
        "asset_feed_spec": asset_feed_spec or OMIT,
        "title": title or OMIT,
        "body": body or OMIT,
        "image_hash": image_hash or OMIT,
        "degrees_of_freedom_spec": degrees_of_freedom_spec or OMIT,
    }
    client = get_graph_api_client()
    created = await client.create_edge_object(
        normalize_account_id(account_id),
        "adcreatives",
        data=merge_graph_payload(payload, params),
    )
    return {
        "ok": True,
        "action": "create_ad_creative",
        "target": {"account_id": normalize_account_id(account_id)},
        "created": created,
    }


@mcp_server.tool()
async def update_ad_creative(ad_id: str, creative_id: str) -> dict[str, Any]:
    """Replace the creative attached to an existing ad with a prepared creative_id. For placement image changes, inspect get_creative, upload_creative_asset, and create_ad_creative with the edited asset_feed_spec first, then verify the ad with get_ad."""
    ad_id = blank_to_none(ad_id)
    creative_id = blank_to_none(creative_id)
    if ad_id is None or creative_id is None:
        raise ValidationError("ad_id and creative_id are required.")
    client = get_graph_api_client()
    previous = await client.get_object(ad_id, fields=["id", "creative"])
    previous_creative = previous.get("creative")
    previous_creative_id = (
        previous_creative.get("id")
        if isinstance(previous_creative, dict)
        else previous_creative
    )
    await client.update_object(ad_id, data={"creative": {"creative_id": creative_id}})
    return mutation_response(
        action="update_ad_creative",
        target={"ad_id": ad_id},
        previous={"creative_id": previous_creative_id},
        current={"creative_id": creative_id},
    )


@mcp_server.tool()
async def preview_ad(
    ad_id: str | None = None,
    account_id: str | None = None,
    creative_id: str | None = None,
    creative: dict[str, Any] | None = None,
    ad_format: str = "DESKTOP_FEED_STANDARD",
) -> dict[str, Any]:
    """Use this when the user wants a rendered preview for an existing ad, creative id, or draft creative payload."""
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
    """Use this when the user needs an image asset uploaded before creating or updating a creative."""
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
    cell_ids: StringList | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    observation_type: str | None = None,
    confidence_level: float | None = None,
    hypothesis: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use this when the user explicitly wants to create a Meta split test or ad study object."""
    payload: dict[str, Any] = {
        "name": name,
        "type": ad_study_type,
        "description": description or OMIT,
        "cell_ids": cell_ids or OMIT,
        "start_time": start_time or OMIT,
        "end_time": end_time or OMIT,
        "observation_type": observation_type or OMIT,
        "confidence_level": OMIT if confidence_level is None else confidence_level,
        "hypothesis": hypothesis or OMIT,
    }
    client = get_graph_api_client()
    created = await client.create_edge_object(
        owner_id,
        "ad_studies",
        data=merge_graph_payload(payload, params),
    )
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
    """Use this when the user already has a creative id and wants to change creative metadata or payload fields."""
    if object_story_spec is not None:
        _ensure_v25_creative_payload(object_story_spec)
    if asset_feed_spec is not None:
        _ensure_v25_creative_payload(asset_feed_spec)
    payload: dict[str, Any] = {
        "name": OMIT,
        "title": OMIT,
        "body": OMIT,
        "status": OMIT,
        "object_story_spec": OMIT,
        "asset_feed_spec": OMIT,
    }
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
    payload = merge_graph_payload(payload, params)
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
    """Use this only when the user explicitly wants to delete a creative instead of leaving it unused."""
    client = get_graph_api_client()
    result = await client.delete_object(creative_id)
    return {
        "ok": True,
        "action": "delete_creative",
        "target": {"creative_id": creative_id},
        "result": result,
    }


@mcp_server.tool()
async def boost_instagram_post(
    account_id: str,
    adset_id: str,
    instagram_media_id: str,
    instagram_account_id: str,
    name: str | None = None,
    call_to_action: dict[str, Any] | None = None,
    validate_only: bool = False,
    page_id: str | None = None,
) -> dict[str, Any]:
    """Boost an existing Instagram post, reel or story: creates a creative from it and a PAUSED ad in an existing ad set, returning both ids. Check list_instagram_media boost_eligible first. page_id defaults to the ad set's promoted_object page. validate_only checks the ad with Meta and creates nothing."""
    ids = {
        "account_id": blank_to_none(account_id),
        "adset_id": blank_to_none(adset_id),
        "instagram_media_id": blank_to_none(instagram_media_id),
        "instagram_account_id": blank_to_none(instagram_account_id),
    }
    for field_name, value in ids.items():
        if value is None:
            raise ValidationError(f"{field_name} is required.")
    if call_to_action is not None:
        if not isinstance(call_to_action, dict) or not blank_to_none(str(call_to_action.get("type") or "")):
            raise ValidationError("call_to_action must be an object with a type, for example {\"type\": \"LEARN_MORE\", \"value\": {\"link\": \"https://...\"}}.")
        _ensure_v25_creative_payload(call_to_action)
    resolved_account_id = normalize_account_id(ids["account_id"])
    media_id = ids["instagram_media_id"]
    ad_name = blank_to_none(name) or f"Instagram post {media_id}"
    client = get_graph_api_client()
    resolved_page_id = blank_to_none(page_id)
    if resolved_page_id is None:
        adset = await client.get_object(ids["adset_id"], fields=["id", "promoted_object"])
        promoted_object = adset.get("promoted_object")
        if isinstance(promoted_object, dict):
            resolved_page_id = blank_to_none(str(promoted_object.get("page_id") or ""))
        if resolved_page_id is None:
            raise ValidationError(
                "page_id is required: Meta's Instagram post creative needs the Facebook Page id, "
                "and the ad set's promoted_object has no page_id."
            )
    creative_spec: dict[str, Any] = {
        "object_id": resolved_page_id,
        "instagram_user_id": ids["instagram_account_id"],
        "source_instagram_media_id": media_id,
    }
    if call_to_action is not None:
        creative_spec["call_to_action"] = call_to_action
    target = {
        "account_id": resolved_account_id,
        "adset_id": ids["adset_id"],
        "instagram_media_id": media_id,
        "instagram_account_id": ids["instagram_account_id"],
        "page_id": resolved_page_id,
    }
    if validate_only:
        validation = await client.create_edge_object(
            resolved_account_id,
            "ads",
            data={
                "name": ad_name,
                "adset_id": ids["adset_id"],
                "creative": creative_spec,
                "status": "PAUSED",
                "execution_options": ["validate_only"],
            },
        )
        response = creation_response(
            action="boost_instagram_post",
            target=target,
            result=validation,
            validate_only=True,
        )
        response.update({"creative_id": None, "ad_id": None, "status": "PAUSED"})
        return response
    creative = await client.create_edge_object(
        resolved_account_id,
        "adcreatives",
        data={"name": f"{ad_name} creative", **creative_spec},
    )
    creative_id = str(creative.get("id") or "")
    if not creative_id:
        raise ValidationError("Meta did not return a creative id, so no ad was created.")
    try:
        ad = await client.create_edge_object(
            resolved_account_id,
            "ads",
            data={
                "name": ad_name,
                "adset_id": ids["adset_id"],
                "creative": {"creative_id": creative_id},
                "status": "PAUSED",
            },
        )
    except MetaApiError as exc:
        exc.message = (
            f"Creative {creative_id} was created but the ad was not: {exc.message} "
            "Delete the creative with delete_creative or retry the ad with create_ad."
        )
        raise
    response = creation_response(
        action="boost_instagram_post",
        target=target,
        result={"creative": creative, "ad": ad},
    )
    response.update({"creative_id": creative_id, "ad_id": str(ad.get("id") or "") or None, "status": "PAUSED"})
    return response
