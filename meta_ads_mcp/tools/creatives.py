"""Creative and experiment tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, RateLimitError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.local_files import read_upload_file
from meta_ads_mcp.money import resolve_account_currency, to_minor_units, validate_positive_amount
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import AccountId, CurrencyAmount, FieldList, GraphId, StringList, coerce_csv_string_list

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


MAX_PREVIEW_FORMATS = 10
DEFAULT_PREVIEW_FORMAT = "DESKTOP_FEED_STANDARD"


def _preview_formats(ad_formats: Any, ad_format: str) -> list[str]:
    """Validate the ad_formats list before any preview request."""
    values = coerce_csv_string_list(ad_formats)
    if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValidationError("ad_formats must list nonblank ad format names, for example MOBILE_FEED_STANDARD.")
    if ad_format != DEFAULT_PREVIEW_FORMAT:
        raise ValidationError("Use ad_format for one placement or ad_formats for several, not both.")
    formats = list(dict.fromkeys(value.strip().upper() for value in values))
    if len(formats) > MAX_PREVIEW_FORMATS:
        raise ValidationError(f"ad_formats accepts at most {MAX_PREVIEW_FORMATS} formats per call.")
    return formats


async def _preview_many(
    *,
    ad_id: str | None,
    account_id: str | None,
    creative_id: str | None,
    creative: dict[str, Any] | None,
    formats: list[str],
) -> dict[str, Any]:
    """Request one preview per format, keeping the formats Meta refused."""
    client = get_graph_api_client()
    items: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for ad_format in formats:
        try:
            preview = await client.preview_ad(
                ad_id=ad_id,
                account_id=account_id,
                creative_id=creative_id,
                creative=creative,
                ad_format=ad_format,
            )
        except MetaApiError as exc:
            failed.append({"ad_format": ad_format, "error": exc.message, "code": exc.code})
            continue
        except UnsupportedFeatureError as exc:
            failed.append({"ad_format": ad_format, "error": str(exc), "code": None})
            continue
        items.append({"ad_format": ad_format, "preview": preview})
    return {
        "items": items,
        "failed": failed,
        "summary": {
            "count": len(items),
            "requested_formats": formats,
            "failed_count": len(failed),
            "complete": not failed,
        },
    }


@mcp_server.tool()
async def preview_ad(
    ad_id: str | None = None,
    account_id: str | None = None,
    creative_id: str | None = None,
    creative: dict[str, Any] | None = None,
    ad_format: str = "DESKTOP_FEED_STANDARD",
    ad_formats: StringList | None = None,
) -> dict[str, Any]:
    """Use this when the user wants a rendered preview for an existing ad, creative id, or draft creative payload. ad_formats previews up to 10 placements in one call (one Meta request each) and reports any format Meta refused."""
    if creative:
        _ensure_v25_creative_payload(creative)
    if not ad_id and not creative_id and not creative:
        raise ValidationError("Provide ad_id, creative_id, or creative.")
    if (creative_id or creative) and not account_id and not ad_id:
        raise ValidationError("account_id is required when previewing from creative_id or creative.")
    if ad_formats is not None:
        formats = _preview_formats(ad_formats, ad_format)
        return await _preview_many(
            ad_id=ad_id,
            account_id=normalize_account_id(account_id) if account_id else None,
            creative_id=creative_id,
            creative=creative,
            formats=formats,
        )
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


MAX_DIRECT_VIDEO_BYTES = 25 * 1024 * 1024
# Container formats Meta lists as supported for ad videos.
VIDEO_FILE_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".gif": "image/gif",
}
VIDEO_STATUS_FIELDS = ["id", "status"]
AD_IMAGE_READ_FIELDS = ["id", "hash", "name", "url", "permalink_url", "width", "height", "status", "created_time"]


def _video_processing_status(video: dict[str, Any]) -> str | None:
    """Return Meta's overall video_status (ready, processing, error, expired) when present."""
    status = video.get("status")
    if isinstance(status, dict) and isinstance(status.get("video_status"), str):
        return status["video_status"]
    return None


@mcp_server.tool()
async def upload_ad_video(
    account_id: AccountId,
    video_url: str | None = None,
    file_path: str | None = None,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload a video to the ad account's video library from a public http(s) URL (video_url) or a local file of at most 25 MB (file_path), and return the video id and Meta's processing status. Meta processes the video after upload; check list_ad_videos(video_ids=...) until processing_status is ready before using it in a creative. Larger local files need Meta's chunked upload, which this tool does not do; host the file and use video_url. dry_run returns what would be sent and sends nothing."""
    account = blank_to_none(account_id)
    if account is None:
        raise ValidationError("account_id is required.")
    url = blank_to_none(video_url)
    path_text = blank_to_none(file_path)
    if bool(url) == bool(path_text):
        raise ValidationError("Provide exactly one of video_url or file_path.")
    title = blank_to_none(title)
    if title is not None and len(title) >= 255:
        raise ValidationError("title must be under 255 characters.")
    data: dict[str, Any] = {}
    files: dict[str, Any] | None = None
    shown: dict[str, Any]
    if url:
        if not url.startswith(("http://", "https://")):
            raise ValidationError("video_url must be a public http:// or https:// URL.")
        data["file_url"] = url
        shown = {"file_url": url}
    else:
        # Only from the operator's upload folder: a path from the model must not be able
        # to send some other file on this machine to Meta. Files above the limit need
        # Meta's chunked upload, which this tool does not do; host them and pass video_url.
        local_video = read_upload_file(
            path_text,
            allowed_types=VIDEO_FILE_TYPES,
            max_bytes=MAX_DIRECT_VIDEO_BYTES,
            too_large_hint=(
                "Larger files need Meta's chunked upload, which this tool does not do. "
                "Host the file and pass video_url."
            ),
        )
        shown = {"source": {"file_name": local_video.name, "bytes": len(local_video.content)}}
    for key, value in (("name", blank_to_none(name)), ("title", title), ("description", blank_to_none(description))):
        if value is not None:
            data[key] = value
            shown[key] = value
    resolved_account = normalize_account_id(account)
    target = {"account_id": resolved_account}
    if dry_run:
        return {
            "ok": True,
            "action": "upload_ad_video",
            "dry_run": True,
            "sent": False,
            "target": target,
            "would_send": {"method": "POST", "path": f"{resolved_account}/advideos", "data": shown},
        }
    if path_text:
        files = {"source": (local_video.name, local_video.content)}
    client = get_graph_api_client()
    uploaded = await client.create_edge_object(resolved_account, "advideos", data=data, files=files)
    video_id = str(uploaded.get("id") or uploaded.get("video_id") or "") or None
    processing_status: str | None = None
    status_detail: Any = None
    status_note: str
    if video_id is None:
        status_note = "Meta returned no video id; check list_ad_videos before uploading again."
    else:
        try:
            video = await client.get_object(video_id, fields=VIDEO_STATUS_FIELDS)
            status_detail = video.get("status")
            processing_status = _video_processing_status(video)
            status_note = (
                "Video is ready to use." if processing_status == "ready"
                else "Meta is still processing or reported a problem; check list_ad_videos(video_ids=...) before using it."
            )
        except MetaApiError as exc:
            status_note = f"The upload succeeded but its status could not be read: {exc.message}"
    response = creation_response(action="upload_ad_video", target=target, result=uploaded)
    response.update({
        "video_id": video_id,
        "processing_status": processing_status,
        "status": status_detail,
        "status_note": status_note,
    })
    return response


@mcp_server.tool()
async def delete_ad_image(
    account_id: AccountId,
    image_hash: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Use this only when the user explicitly wants an image removed from the ad account's image library. Reads the image by hash first and returns it as previous; Meta removes it from the account library (its documentation calls this dissociating the image). Nothing is deleted when the hash is not found. dry_run reads the image and sends nothing."""
    account = blank_to_none(account_id)
    image_hash = blank_to_none(image_hash)
    if account is None or image_hash is None:
        raise ValidationError("account_id and image_hash are required.")
    resolved_account = normalize_account_id(account)
    client = get_graph_api_client()
    listing = await client.get_ad_images_by_hashes(resolved_account, hashes=[image_hash], fields=AD_IMAGE_READ_FIELDS)
    matches = [item for item in listing.get("data", []) if isinstance(item, dict) and item.get("hash") == image_hash]
    if not matches:
        raise ValidationError(f"No image with hash {image_hash} was found in {resolved_account}; nothing was deleted.")
    previous = matches[0]
    target = {"account_id": resolved_account, "image_hash": image_hash}
    if dry_run:
        return {
            "ok": True,
            "action": "delete_ad_image",
            "dry_run": True,
            "sent": False,
            "target": target,
            "previous": previous,
            "would_send": {"method": "DELETE", "path": f"{resolved_account}/adimages", "data": {"hash": image_hash}},
        }
    result = await client.delete_edge_object(resolved_account, "adimages", data={"hash": image_hash})
    response = mutation_response(
        action="delete_ad_image",
        target=target,
        previous=previous,
        current={"hash": image_hash, "in_account_library": False},
    )
    response["result"] = result
    return response


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


_ADLABEL_OPERATIONS = ("set", "add", "remove")


def _normalize_adlabels(adlabels: Any) -> list[dict[str, str]]:
    """Each label is {"id": digits} or {"name": text}."""
    if not isinstance(adlabels, list):
        raise ValidationError('adlabels must be a list of labels such as [{"name": "spring"}] or [{"id": "123"}].')
    labels: list[dict[str, str]] = []
    for index, label in enumerate(adlabels, start=1):
        if not isinstance(label, dict) or set(label) - {"id", "name"} or len(label) != 1:
            raise ValidationError(f"adlabels[{index}] must have exactly one of id or name.")
        key, value = next(iter(label.items()))
        text = blank_to_none(value) if isinstance(value, str) else None
        if text is None or (key == "id" and not text.isdigit()):
            expected = "a numeric label id" if key == "id" else "nonblank"
            raise ValidationError(f"adlabels[{index}] {key} must be {expected}.")
        labels.append({key: text})
    return labels


def _label_matches(existing: dict[str, Any], wanted: dict[str, str]) -> bool:
    key, value = next(iter(wanted.items()))
    return str(existing.get(key) or "") == value


def _existing_labels(previous: dict[str, Any]) -> list[dict[str, Any]]:
    raw = previous.get("adlabels")
    if isinstance(raw, dict):
        raw = raw.get("data")
    return [label for label in raw or [] if isinstance(label, dict)]


async def _update_creative_labels(
    client: Any,
    creative_id: str,
    payload: dict[str, Any],
    labels: list[dict[str, str]],
    operation: str,
) -> dict[str, Any]:
    """Apply a label set, add or remove after reading the creative's current labels."""
    previous = await client.get_object(creative_id, fields=[*CREATIVE_FIELDS, "adlabels{id,name}"])
    previous_labels = _existing_labels(previous)
    shown_previous = [{key: str(label[key]) for key in ("id", "name") if label.get(key)} for label in previous_labels]
    if operation == "remove":
        missing = [label for label in labels if not any(_label_matches(existing, label) for existing in previous_labels)]
        if missing:
            raise ValidationError(f"These labels are not on creative {creative_id}, so nothing was changed: {missing}.")
        remaining = [label for label in previous_labels if not any(_label_matches(label, wanted) for wanted in labels)]
        expected = [{"id": str(label["id"])} if label.get("id") else {"name": str(label.get("name"))} for label in remaining]
        payload["adlabels"] = expected
    elif operation == "add":
        expected = [*shown_previous, *labels]
    else:
        expected = labels
    if payload:
        await client.update_object(creative_id, data=payload)
    if operation == "add":
        try:
            await client.create_edge_object(creative_id, "adlabels", data={"adlabels": labels})
        except MetaApiError as exc:
            if payload:
                exc.message = f"The other fields of creative {creative_id} were updated but the labels were not added: {exc.message}"
            raise
    response = mutation_response(
        action="update_creative",
        target={"creative_id": creative_id},
        previous={
            "name": previous.get("name"),
            "title": previous.get("title"),
            "body": previous.get("body"),
            "status": previous.get("status"),
            "adlabels": shown_previous,
        },
        current={**payload, "adlabels": expected},
    )
    response["adlabels_operation"] = operation
    return response


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
    adlabels: list[dict[str, str]] | None = None,
    adlabels_operation: str | None = None,
) -> dict[str, Any]:
    """Use this when the user already has a creative id and wants to change creative metadata or payload fields. adlabels with adlabels_operation set (replace all labels, the default), add, or remove changes the creative's ad labels; each label is {"name": ...} or {"id": ...}."""
    if object_story_spec is not None:
        _ensure_v25_creative_payload(object_story_spec)
    if asset_feed_spec is not None:
        _ensure_v25_creative_payload(asset_feed_spec)
    operation: str | None = None
    labels: list[dict[str, str]] | None = None
    if adlabels is not None:
        labels = _normalize_adlabels(adlabels)
        operation = (blank_to_none(adlabels_operation) or "set").lower()
        if operation not in _ADLABEL_OPERATIONS:
            raise ValidationError(f"adlabels_operation must be one of {', '.join(_ADLABEL_OPERATIONS)}.")
        if operation in {"add", "remove"} and not labels:
            raise ValidationError(f"adlabels must name at least one label to {operation}.")
    elif blank_to_none(adlabels_operation) is not None:
        raise ValidationError("adlabels_operation needs adlabels.")
    payload: dict[str, Any] = {
        "name": OMIT,
        "title": OMIT,
        "body": OMIT,
        "status": OMIT,
        "object_story_spec": OMIT,
        "asset_feed_spec": OMIT,
    }
    if labels is not None:
        payload["adlabels"] = labels if operation == "set" else OMIT
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
    if not payload and operation not in {"add", "remove"}:
        raise ValidationError("At least one field must be provided for update_creative.")
    client = get_graph_api_client()
    if labels is not None and operation is not None:
        return await _update_creative_labels(client, creative_id, payload, labels, operation)
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


# One-step boost: objective -> ad set destination and goal, from the objective
# mapping table on Meta's campaign reference page. Only these two are built.
BOOST_OBJECTIVES: dict[str, dict[str, Any]] = {
    "OUTCOME_ENGAGEMENT": {"destination_type": "ON_POST", "optimization_goal": "POST_ENGAGEMENT", "promoted_page": False},
    "OUTCOME_AWARENESS": {"destination_type": None, "optimization_goal": "REACH", "promoted_page": True},
}
BOOST_DEFAULT_OBJECTIVE = "OUTCOME_ENGAGEMENT"
BOOST_DEFAULT_DURATION_DAYS = 6
BOOST_MAX_DURATION_DAYS = 365
BOOST_BILLING_EVENT = "IMPRESSIONS"
BOOST_BID_STRATEGY = "LOWEST_COST_WITHOUT_CAP"
BOOST_PARTNERSHIP_AD_FORMATS = (1, 2, 3)
_DELETE_TOOL = {"campaign": "delete_campaign", "ad set": "delete_adset", "creative": "delete_creative", "ad": "delete_ad"}


def _boost_created_message(created: list[tuple[str, str]], failed_step: str, reason: str) -> str:
    """Name every object already created so it can be found and removed."""
    if not created:
        return f"Creating the {failed_step} failed and nothing was created: {reason}"
    listed = "; ".join(f"{kind} {object_id} (PAUSED, remove with {_DELETE_TOOL[kind]})" for kind, object_id in created)
    return (
        f"Creating the {failed_step} failed: {reason} Already created, all paused so nothing is spending: {listed}. "
        "Delete them in reverse order (ad, creative, ad set, campaign) or reuse them."
    )


async def _boost_one_step(
    client_factory: Any,
    *,
    account_id: str,
    media_id: str,
    instagram_account_id: str,
    page_id: str | None,
    ad_name: str,
    creative_spec: dict[str, Any],
    daily_budget: float | None,
    duration_days: int | None,
    targeting: dict[str, Any] | None,
    objective: str | None,
    plan_only: bool,
) -> dict[str, Any]:
    """Create campaign, ad set, creative and ad, all paused, or return the plan."""
    if daily_budget is None:
        raise ValidationError("daily_budget (account currency, major units) is required when adset_id is omitted; this tool does not pick a budget for you.")
    validate_positive_amount(daily_budget, field_name="daily_budget")
    if not isinstance(targeting, dict) or not targeting:
        raise ValidationError("targeting is required when adset_id is omitted, for example {\"geo_locations\": {\"countries\": [\"US\"]}}.")
    resolved_objective = (blank_to_none(objective) or BOOST_DEFAULT_OBJECTIVE).upper()
    if resolved_objective not in BOOST_OBJECTIVES:
        raise ValidationError(
            f"objective must be one of {', '.join(BOOST_OBJECTIVES)} in one-step mode; other objectives need their own "
            "destination and goal, so create the campaign and ad set first and pass adset_id."
        )
    days = BOOST_DEFAULT_DURATION_DAYS if duration_days is None else duration_days
    if not isinstance(days, int) or not 1 <= days <= BOOST_MAX_DURATION_DAYS:
        raise ValidationError(f"duration_days must be a whole number from 1 to {BOOST_MAX_DURATION_DAYS}.")
    if page_id is None:
        raise ValidationError("page_id is required when adset_id is omitted: the creative needs the Facebook Page linked to the Instagram account.")
    mapping = BOOST_OBJECTIVES[resolved_objective]
    client = client_factory()
    currency = await resolve_account_currency(client, account_id)
    budget_minor = to_minor_units(daily_budget, currency, field_name="daily_budget")
    end_time = (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()
    campaign_data: dict[str, Any] = {
        "name": f"{ad_name} campaign",
        "objective": resolved_objective,
        "status": "PAUSED",
        "special_ad_categories": [],
        "is_adset_budget_sharing_enabled": False,
    }
    adset_data: dict[str, Any] = {
        "name": f"{ad_name} ad set",
        "campaign_id": "<campaign id>",
        "daily_budget": budget_minor,
        "billing_event": BOOST_BILLING_EVENT,
        "optimization_goal": mapping["optimization_goal"],
        "bid_strategy": BOOST_BID_STRATEGY,
        "targeting": targeting,
        "end_time": end_time,
        "status": "PAUSED",
    }
    if mapping["destination_type"]:
        adset_data["destination_type"] = mapping["destination_type"]
    if mapping["promoted_page"]:
        adset_data["promoted_object"] = {"page_id": page_id}
    creative_data = {"name": f"{ad_name} creative", **creative_spec}
    ad_data = {"name": ad_name, "adset_id": "<ad set id>", "creative": {"creative_id": "<creative id>"}, "status": "PAUSED"}
    defaults = {
        "objective": resolved_objective if objective is None else None,
        "duration_days": days if duration_days is None else None,
        "optimization_goal": mapping["optimization_goal"],
        "destination_type": mapping["destination_type"],
        "billing_event": BOOST_BILLING_EVENT,
        "bid_strategy": BOOST_BID_STRATEGY,
        "special_ad_categories": [],
        "is_adset_budget_sharing_enabled": False,
        "start_time": "when Meta creates the ad set (no start_time sent)",
        "end_time": end_time,
        "status": "PAUSED for all four objects",
        "names": f"derived from '{ad_name}'",
    }
    defaults = {key: value for key, value in defaults.items() if value is not None or key == "destination_type"}
    plan = {
        "currency": currency,
        "daily_budget": {"major_units": daily_budget, "sent_minor_units": budget_minor},
        "requests": [
            {"step": "campaign", "method": "POST", "path": f"{account_id}/campaigns", "data": campaign_data},
            {"step": "ad set", "method": "POST", "path": f"{account_id}/adsets", "data": adset_data},
            {"step": "creative", "method": "POST", "path": f"{account_id}/adcreatives", "data": creative_data},
            {"step": "ad", "method": "POST", "path": f"{account_id}/ads", "data": ad_data},
        ],
        "defaults_applied": defaults,
    }
    target = {"account_id": account_id, "instagram_media_id": media_id, "instagram_account_id": instagram_account_id, "page_id": page_id}
    if plan_only:
        return {
            "ok": True,
            "action": "boost_instagram_post",
            "mode": "one_step",
            "plan_only": True,
            "sent": False,
            "target": target,
            "plan": plan,
            "note": "Nothing was created. Call again without plan_only to create these four objects, all paused.",
        }
    created: list[tuple[str, str]] = []
    results: dict[str, Any] = {}

    async def create(step: str, edge: str, data: dict[str, Any]) -> str:
        try:
            result = await client.create_edge_object(account_id, edge, data=data)
        except (MetaApiError, RateLimitError) as exc:
            reason = exc.message
            if getattr(exc, "mutation_outcome_unknown", False):
                reason = f"{reason} Meta may still have created the {step}; check before retrying."
            exc.message = _boost_created_message(created, step, reason)
            raise
        except Exception as exc:
            if not created:
                raise
            raise MetaApiError(message=_boost_created_message(created, step, str(exc) or type(exc).__name__)) from exc
        object_id = str(result.get("id") or "")
        if not object_id:
            raise ValidationError(_boost_created_message(created, step, "Meta returned no id."))
        created.append((step, object_id))
        results[step] = result
        return object_id

    campaign_id = await create("campaign", "campaigns", campaign_data)
    adset_id = await create("ad set", "adsets", {**adset_data, "campaign_id": campaign_id})
    creative_id = await create("creative", "adcreatives", creative_data)
    ad_id = await create("ad", "ads", {**ad_data, "adset_id": adset_id, "creative": {"creative_id": creative_id}})
    response = creation_response(action="boost_instagram_post", target=target, result=results)
    response.update({
        "mode": "one_step",
        "campaign_id": campaign_id,
        "adset_id": adset_id,
        "creative_id": creative_id,
        "ad_id": ad_id,
        "status": "PAUSED",
        "defaults_applied": defaults,
        "daily_budget": plan["daily_budget"],
        "currency": currency,
        "note": "All four objects are paused. Activate the campaign, ad set and ad with set_*_status when ready.",
    })
    return response


@mcp_server.tool()
async def boost_instagram_post(
    account_id: str,
    instagram_media_id: str,
    instagram_account_id: str,
    adset_id: str | None = None,
    name: str | None = None,
    call_to_action: dict[str, Any] | None = None,
    validate_only: bool = False,
    page_id: str | None = None,
    daily_budget: CurrencyAmount | None = None,
    duration_days: int | None = None,
    targeting: dict[str, Any] | None = None,
    objective: str | None = None,
    plan_only: bool = False,
    url_tags: str | None = None,
    sponsor_page_id: GraphId | None = None,
    sponsor_instagram_account_id: GraphId | None = None,
    partnership_ad_format: int | None = None,
) -> dict[str, Any]:
    """Boost an existing Instagram post, reel or story. With adset_id: creates a creative from it and a PAUSED ad in that ad set (page_id defaults to the ad set's promoted_object page; validate_only checks the ad with Meta). Without adset_id (one-step): creates a campaign, ad set, creative and ad, all PAUSED, from daily_budget (major units), targeting, objective (OUTCOME_ENGAGEMENT default, or OUTCOME_AWARENESS) and duration_days (default 6), stating every default; plan_only returns the plan and creates nothing. If a later step fails the error names every object already created. Partnership Ad: sponsor_page_id and/or sponsor_instagram_account_id, optional partnership_ad_format 1, 2 or 3. Check list_instagram_media boost_eligible first."""
    ids = {
        "account_id": blank_to_none(account_id),
        "instagram_media_id": blank_to_none(instagram_media_id),
        "instagram_account_id": blank_to_none(instagram_account_id),
    }
    for field_name, value in ids.items():
        if value is None:
            raise ValidationError(f"{field_name} is required.")
    ids["adset_id"] = blank_to_none(adset_id)
    one_step = ids["adset_id"] is None
    if one_step and validate_only:
        raise ValidationError("validate_only needs an existing adset_id; in one-step mode use plan_only to see what would be created.")
    if not one_step:
        extra = [
            label for label, value in (
                ("daily_budget", daily_budget), ("duration_days", duration_days), ("targeting", targeting),
                ("objective", objective),
            ) if value is not None
        ]
        if plan_only:
            extra.append("plan_only")
        if extra:
            raise ValidationError(f"{', '.join(extra)} only apply in one-step mode (without adset_id).")
    if call_to_action is not None:
        if not isinstance(call_to_action, dict) or not blank_to_none(str(call_to_action.get("type") or "")):
            raise ValidationError("call_to_action must be an object with a type, for example {\"type\": \"LEARN_MORE\", \"value\": {\"link\": \"https://...\"}}.")
        _ensure_v25_creative_payload(call_to_action)
    if partnership_ad_format is not None:
        if partnership_ad_format not in BOOST_PARTNERSHIP_AD_FORMATS:
            raise ValidationError("partnership_ad_format must be 1 (both identities), 2 (first identity only) or 3 (automatic).")
        if not (blank_to_none(sponsor_page_id) or blank_to_none(sponsor_instagram_account_id)):
            raise ValidationError("partnership_ad_format needs sponsor_page_id or sponsor_instagram_account_id.")
    if targeting is not None:
        _ensure_v25_creative_payload(targeting)
    resolved_account_id = normalize_account_id(ids["account_id"])
    media_id = ids["instagram_media_id"]
    ad_name = blank_to_none(name) or f"Instagram post {media_id}"
    partnership: dict[str, Any] = {}
    if blank_to_none(sponsor_page_id):
        partnership["facebook_branded_content"] = {"sponsor_page_id": blank_to_none(sponsor_page_id)}
    if blank_to_none(sponsor_instagram_account_id):
        partnership["instagram_branded_content"] = {"sponsor_id": blank_to_none(sponsor_instagram_account_id)}
    if partnership_ad_format is not None:
        partnership["branded_content"] = {"ad_format": partnership_ad_format}
    resolved_page_id = blank_to_none(page_id)
    if one_step:
        creative_spec: dict[str, Any] = {
            "object_id": resolved_page_id,
            "instagram_user_id": ids["instagram_account_id"],
            "source_instagram_media_id": media_id,
        }
        if call_to_action is not None:
            creative_spec["call_to_action"] = call_to_action
        if blank_to_none(url_tags):
            creative_spec["url_tags"] = blank_to_none(url_tags)
        creative_spec.update(partnership)
        return await _boost_one_step(
            get_graph_api_client,
            account_id=resolved_account_id,
            media_id=media_id,
            instagram_account_id=ids["instagram_account_id"],
            page_id=resolved_page_id,
            ad_name=ad_name,
            creative_spec=creative_spec,
            daily_budget=daily_budget,
            duration_days=duration_days,
            targeting=targeting,
            objective=objective,
            plan_only=plan_only,
        )
    client = get_graph_api_client()
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
    if blank_to_none(url_tags):
        creative_spec["url_tags"] = blank_to_none(url_tags)
    creative_spec.update(partnership)
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
