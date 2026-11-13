"""Creative and experiment tools."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, RateLimitError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.money import resolve_account_currency, to_minor_units, validate_positive_amount
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import AccountId, CurrencyAmount, FieldList, GraphId, StringList, coerce_csv_string_list
from meta_ads_mcp.url_secrets import refuse_url_credentials

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


def _failure_reason(exc: BaseException) -> str:
    """The error text a caller can act on, without assuming an error type."""
    message = getattr(exc, "message", None)
    return str(message if isinstance(message, str) and message else exc) or type(exc).__name__


def _outcome_unknown(exc: BaseException) -> bool:
    return bool(getattr(exc, "mutation_outcome_unknown", False))


def _with_context(exc: Exception, prefix: str) -> Exception:
    """Put what already happened in front of a later failure, whatever its type.

    Meta and rate-limit errors keep their type (and retry guidance); anything else is
    wrapped in a MetaApiError so the message still reaches the caller.
    """
    if isinstance(exc, (MetaApiError, RateLimitError)):
        exc.message = f"{prefix} {exc.message}"
        return exc
    return MetaApiError(message=f"{prefix} {_failure_reason(exc)}")


def _refuse_url_logins(value: Any, name: str) -> None:
    """Refuse a login embedded in any URL inside a nested input, before any request is built."""
    if isinstance(value, str):
        if "://" in value or "@" in value:
            refuse_url_credentials(value, name)
    elif isinstance(value, dict):
        for key, item in value.items():
            _refuse_url_logins(item, f"{name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refuse_url_logins(item, f"{name}[{index}]")


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
    for position, ad_format in enumerate(formats):
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
        except RateLimitError as exc:
            # Keep the previews already rendered; stop asking while Meta is throttling.
            failed.append({"ad_format": ad_format, "error": exc.message, "code": exc.code, "rate_limited": True})
            failed.extend(
                {"ad_format": remaining, "error": "Not requested: Meta rate-limited an earlier format in this call.", "code": None}
                for remaining in formats[position + 1:]
            )
            break
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
# Meta's Ads Guide gives 4 GB as the largest video file for ads.
MAX_CHUNKED_VIDEO_BYTES = 4_000_000_000
# Meta chooses each chunk's size; this server refuses to hold more than this in memory at once.
MAX_VIDEO_CHUNK_BYTES = 256 * 1024 * 1024
# Separate from the file size: a cap on requests, so a stalled session cannot run forever.
MAX_CHUNKED_TRANSFER_REQUESTS = 10_000
VIDEO_UPLOAD_HOST = "graph-video.facebook.com"
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


def _video_upload_folder() -> Path:
    """The folder named by META_UPLOAD_DIR, with local_files' rules and messages."""
    configured = get_settings().upload_directory
    if not configured or not configured.strip():
        raise ValidationError(
            "Reading local files is off. Set the META_UPLOAD_DIR environment variable to the "
            "one folder this server may read uploads from, put the file there, and try again."
        )
    try:
        folder = Path(configured.strip()).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValidationError("The folder named by META_UPLOAD_DIR does not exist or cannot be read.") from None
    if not folder.is_dir():
        raise ValidationError("META_UPLOAD_DIR must name a folder.")
    return folder


@dataclass(slots=True)
class _OpenVideo:
    """A local video opened for upload: the open descriptor is what gets read."""

    descriptor: int
    name: str
    size: int


def _open_upload_video(file_path: str, parameter: str = "file_path") -> _OpenVideo:
    """Open a video inside the upload folder without reading it all into memory.

    Same rules and messages as local_files.read_upload_file (folder from META_UPLOAD_DIR,
    links resolved and checked, type from the real name, size checked on the open file),
    which reads the whole file and so cannot serve multi-gigabyte uploads. In addition,
    every folder below the upload folder is opened relative to its parent and refuses
    links, so a folder swapped for a link after the check cannot redirect the read.
    """
    if not isinstance(file_path, str) or not file_path.strip() or "\x00" in file_path:
        raise ValidationError(f"{parameter} must be a non-blank local file path.")
    unreadable = f"{parameter} does not point to a file that can be read."
    folder = _video_upload_folder()
    candidate = Path(file_path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = folder / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValidationError(unreadable) from None
    if not resolved.is_relative_to(folder):
        raise ValidationError(f"{parameter} must be inside the upload folder set by META_UPLOAD_DIR.")
    if not resolved.stem or resolved.suffix.lower() not in VIDEO_FILE_TYPES:
        raise ValidationError(f"{parameter} must end in one of: {', '.join(VIDEO_FILE_TYPES)}.")
    parts = resolved.relative_to(folder).parts
    if not parts:
        raise ValidationError(unreadable)
    directory: int | None = None
    descriptor: int | None = None
    try:
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        details = os.fstat(descriptor)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise ValidationError(unreadable) from None
    finally:
        if directory is not None:
            os.close(directory)
    problem = None
    if not stat.S_ISREG(details.st_mode):
        problem = unreadable
    elif details.st_size == 0:
        problem = f"{parameter} points to an empty file."
    elif details.st_size > MAX_CHUNKED_VIDEO_BYTES:
        problem = f"{parameter} is larger than this server's {MAX_CHUNKED_VIDEO_BYTES}-byte limit for video uploads (Meta's limit for ad videos is 4 GB)."
    if problem is not None:
        os.close(descriptor)
        raise ValidationError(problem)
    return _OpenVideo(descriptor=descriptor, name=resolved.name, size=details.st_size)


def _read_video_bytes(video: _OpenVideo, offset: int, length: int) -> bytes:
    """Read exactly length bytes at offset from the opened file, or refuse."""
    pieces: list[bytes] = []
    remaining = length
    position = offset
    while remaining > 0:
        piece = os.pread(video.descriptor, remaining, position)
        if not piece:
            raise ValidationError("file_path changed size while it was being uploaded.")
        pieces.append(piece)
        remaining -= len(piece)
        position += len(piece)
    return b"".join(pieces)


def _offset(value: Any) -> int | None:
    """Meta returns offsets as numeric strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class _ChunkedUploadStopped(Exception):
    """Meta's chunked-upload answer could not be followed safely."""


async def _chunked_video_upload(
    client: Any,
    account: str,
    video: _OpenVideo,
    meta_fields: dict[str, Any],
) -> dict[str, Any]:
    """Run start, transfer and finish on the advideos edge; on failure name the session and what was sent."""
    try:
        started = await client.post_ad_video_upload_phase(
            account, data={"upload_phase": "start", "file_size": video.size},
        )
    except Exception as exc:
        prefix = f"Starting the chunked upload of {video.name} failed, so nothing was transferred."
        if _outcome_unknown(exc):
            prefix = (
                f"Starting the chunked upload of {video.name} may or may not have opened an upload session; "
                "no video bytes were sent."
            )
        wrapped = _with_context(exc, f"{prefix} Reason:")
        if wrapped is exc:
            raise
        raise wrapped from exc
    session_id = str(started.get("upload_session_id") or "") if isinstance(started, dict) else ""
    video_id = str(started.get("video_id") or "") if isinstance(started, dict) else ""
    if not session_id:
        raise MetaApiError(message=f"Meta did not return an upload session for {video.name}; nothing was transferred.")
    progress = {"phase": "transfer", "confirmed": 0, "requests": 0}

    def stopped_message(reason: str) -> str:
        message = (
            f"The chunked upload of {video.name} stopped during the {progress['phase']} step: upload session {session_id}, "
            f"video {video_id or 'not returned'}. Meta confirmed {progress['confirmed']} of {video.size} bytes "
            f"({progress['requests']} transfer requests sent)."
        )
        if progress["phase"] == "finish":
            message += (
                " The finish may have been accepted even so; check "
                f"list_ad_videos(video_ids=['{video_id}']) before uploading again."
                if progress.get("unknown")
                else " The session was not finished, so the video is not ready; upload the file again to retry."
            )
        else:
            message += " The session was not finished, so the video is not ready; upload the file again to retry."
        return f"{message} Reason: {reason}"

    try:
        start = _offset(started.get("start_offset"))
        end = _offset(started.get("end_offset"))
        while True:
            if start is None or end is None or not 0 <= start <= end <= video.size:
                raise _ChunkedUploadStopped(
                    f"Meta asked for bytes {started.get('start_offset') if start is None else start} to "
                    f"{started.get('end_offset') if end is None else end}, outside the {video.size}-byte file."
                )
            progress["confirmed"] = start
            if start == end:
                break
            if end - start > MAX_VIDEO_CHUNK_BYTES:
                raise _ChunkedUploadStopped(
                    f"Meta asked for a {end - start}-byte chunk, larger than this server reads at once ({MAX_VIDEO_CHUNK_BYTES} bytes)."
                )
            if progress["requests"] >= MAX_CHUNKED_TRANSFER_REQUESTS:
                raise _ChunkedUploadStopped(
                    f"The upload reached this server's {MAX_CHUNKED_TRANSFER_REQUESTS}-request limit for one video."
                )
            chunk = _read_video_bytes(video, start, end - start)
            progress["requests"] += 1
            answer = await client.post_ad_video_upload_phase(
                account,
                data={"upload_phase": "transfer", "upload_session_id": session_id, "start_offset": start},
                files={"video_file_chunk": (video.name, chunk)},
            )
            next_start = _offset(answer.get("start_offset")) if isinstance(answer, dict) else None
            next_end = _offset(answer.get("end_offset")) if isinstance(answer, dict) else None
            if next_start is not None and next_end is not None and next_start != next_end and next_start <= start:
                raise _ChunkedUploadStopped(
                    f"Meta's next chunk (from byte {next_start}) did not move forward from byte {start}."
                )
            started = answer if isinstance(answer, dict) else {}
            start, end = next_start, next_end
        progress["phase"] = "finish"
        finished = await client.post_ad_video_upload_phase(
            account, data={"upload_phase": "finish", "upload_session_id": session_id, **meta_fields},
        )
        if not (isinstance(finished, dict) and finished.get("success") is True):
            raise _ChunkedUploadStopped("Meta did not confirm the finish (no success: true in its answer).")
    except _ChunkedUploadStopped as stop:
        raise MetaApiError(message=stopped_message(str(stop))) from None
    except ValidationError as exc:
        raise MetaApiError(message=stopped_message(str(exc))) from None
    except Exception as exc:
        progress["unknown"] = _outcome_unknown(exc)
        message = stopped_message(_failure_reason(exc))
        if isinstance(exc, (MetaApiError, RateLimitError)):
            exc.message = message
            raise
        raise MetaApiError(message=message) from exc
    return {
        "video_id": video_id or None,
        "finish": finished,
        "upload": {
            "mode": "chunked",
            "upload_session_id": session_id,
            "file_name": video.name,
            "bytes": video.size,
            "bytes_confirmed": progress["confirmed"],
            "transfer_requests": progress["requests"],
        },
    }


def _chunked_plan(account: str, video: _OpenVideo, meta_fields: dict[str, Any]) -> dict[str, Any]:
    """What a chunked upload sends, for dry_run."""
    path = f"{account}/advideos"
    return {
        "method": "POST",
        "path": path,
        "host": VIDEO_UPLOAD_HOST,
        "upload": "chunked",
        "source": {"file_name": video.name, "bytes": video.size},
        "requests": [
            {"method": "POST", "path": path, "data": {"upload_phase": "start", "file_size": video.size}},
            {
                "method": "POST",
                "path": path,
                "data": {"upload_phase": "transfer", "upload_session_id": "<from start>", "start_offset": "<from Meta>"},
                "files": {"video_file_chunk": "<the bytes from start_offset to end_offset that Meta names>"},
                "repeats": "until Meta answers with start_offset equal to end_offset",
            },
            {"method": "POST", "path": path, "data": {"upload_phase": "finish", "upload_session_id": "<from start>", **meta_fields}},
        ],
    }


async def _read_video_status(client: Any, video_id: str | None) -> dict[str, Any]:
    """Read processing status after an upload; a failed read never hides the video id."""
    if video_id is None:
        return {
            "processing_status": None,
            "status": None,
            "status_note": "Meta returned no video id; check list_ad_videos before uploading again.",
        }
    try:
        video = await client.get_object(video_id, fields=VIDEO_STATUS_FIELDS)
    except Exception as exc:
        return {
            "processing_status": None,
            "status": None,
            "status_note": (
                f"The upload succeeded (video {video_id}) but its status could not be read: {_failure_reason(exc)} "
                "Check list_ad_videos(video_ids=...) before using it."
            ),
        }
    processing_status = _video_processing_status(video)
    return {
        "processing_status": processing_status,
        "status": video.get("status"),
        "status_note": (
            "Video is ready to use." if processing_status == "ready"
            else "Meta is still processing or reported a problem; check list_ad_videos(video_ids=...) before using it."
        ),
    }


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
    """Upload a video to the ad account's video library from a public http(s) URL (video_url) or a local file in the upload folder (file_path, up to 4 GB), and return the video id and Meta's processing status. Files above 25 MB go through Meta's chunked upload; if it stops, the error names the upload session and how many bytes Meta confirmed. Meta processes the video after upload; check list_ad_videos(video_ids=...) until processing_status is ready before using it in a creative. dry_run returns what would be sent and sends nothing."""
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
    if url:
        refuse_url_credentials(url, "video_url")
        if not url.startswith(("http://", "https://")):
            raise ValidationError("video_url must be a public http:// or https:// URL.")
    meta_fields = {
        key: value
        for key, value in (("name", blank_to_none(name)), ("title", title), ("description", blank_to_none(description)))
        if value is not None
    }
    resolved_account = normalize_account_id(account)
    target = {"account_id": resolved_account}
    if url:
        return await _upload_video_request(
            resolved_account, target, data={"file_url": url, **meta_fields}, files=None,
            shown={"file_url": url, **meta_fields}, upload={"mode": "url"}, dry_run=dry_run,
        )
    # Only from the operator's upload folder: a path from the model must not be able
    # to send some other file on this machine to Meta.
    video = _open_upload_video(path_text)
    try:
        if video.size <= MAX_DIRECT_VIDEO_BYTES:
            source = {"file_name": video.name, "bytes": video.size}
            content = b"" if dry_run else _read_video_bytes(video, 0, video.size)
            return await _upload_video_request(
                resolved_account, target, data=dict(meta_fields), files={"source": (video.name, content)},
                shown={"source": source, **meta_fields}, upload={"mode": "direct", **source}, dry_run=dry_run,
            )
        if dry_run:
            return {
                "ok": True,
                "action": "upload_ad_video",
                "dry_run": True,
                "sent": False,
                "target": target,
                "would_send": _chunked_plan(resolved_account, video, meta_fields),
            }
        client = get_graph_api_client()
        outcome = await _chunked_video_upload(client, resolved_account, video, meta_fields)
    finally:
        os.close(video.descriptor)
    response = creation_response(action="upload_ad_video", target=target, result={"finish": outcome["finish"]})
    response.update({"video_id": outcome["video_id"], "upload": outcome["upload"]})
    response.update(await _read_video_status(client, outcome["video_id"]))
    return response


async def _upload_video_request(
    account: str,
    target: dict[str, Any],
    *,
    data: dict[str, Any],
    files: dict[str, Any] | None,
    shown: dict[str, Any],
    upload: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """One-request upload (URL or a small file), then a status read."""
    if dry_run:
        return {
            "ok": True,
            "action": "upload_ad_video",
            "dry_run": True,
            "sent": False,
            "target": target,
            "would_send": {"method": "POST", "path": f"{account}/advideos", "data": shown},
        }
    client = get_graph_api_client()
    uploaded = await client.create_edge_object(account, "advideos", data=data, files=files)
    video_id = str(uploaded.get("id") or uploaded.get("video_id") or "") or None
    response = creation_response(action="upload_ad_video", target=target, result=uploaded)
    response.update({"video_id": video_id, "upload": upload})
    response.update(await _read_video_status(client, video_id))
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


# One nested page of labels, read with the creative. AdCreative has no readable
# adlabels edge to page through, so a creative with more labels than this cannot be
# read completely, and a removal (which sends the full replacement set) is refused.
ADLABEL_READ_LIMIT = 100
ADLABEL_READ_FIELD = f"adlabels.limit({ADLABEL_READ_LIMIT}){{id,name}}"


def _existing_labels(previous: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The labels read and whether that read was the whole set (no further page)."""
    raw = previous.get("adlabels")
    complete = True
    if isinstance(raw, dict):
        paging = raw.get("paging")
        if isinstance(paging, dict) and paging.get("next"):
            complete = False
        raw = raw.get("data")
    if raw is not None and not isinstance(raw, list):
        return [], False
    return [label for label in raw or [] if isinstance(label, dict)], complete


async def _update_creative_labels(
    client: Any,
    creative_id: str,
    payload: dict[str, Any],
    labels: list[dict[str, str]],
    operation: str,
) -> dict[str, Any]:
    """Apply a label set, add or remove after reading the creative's current labels."""
    previous = await client.get_object(creative_id, fields=[*CREATIVE_FIELDS, ADLABEL_READ_FIELD])
    previous_labels, read_complete = _existing_labels(previous)
    shown_previous = [{key: str(label[key]) for key in ("id", "name") if label.get(key)} for label in previous_labels]
    expected: list[dict[str, Any]] | None
    if operation == "remove":
        if not read_complete:
            raise ValidationError(
                f"Creative {creative_id} has more labels than one read returns ({ADLABEL_READ_LIMIT}), and removing a "
                "label sends the full remaining set, so labels that were not read would be lost. Nothing was changed. "
                "Use adlabels_operation=set with the complete list you want."
            )
        missing = [label for label in labels if not any(_label_matches(existing, label) for existing in previous_labels)]
        if missing:
            raise ValidationError(f"These labels are not on creative {creative_id}, so nothing was changed: {missing}.")
        remaining = [label for label in previous_labels if not any(_label_matches(label, wanted) for wanted in labels)]
        expected = [{"id": str(label["id"])} if label.get("id") else {"name": str(label.get("name"))} for label in remaining]
        payload["adlabels"] = expected
    elif operation == "add":
        expected = [*shown_previous, *labels] if read_complete else None
    else:
        expected = labels
    if payload:
        await client.update_object(creative_id, data=payload)
    if operation == "add":
        try:
            await client.create_edge_object(creative_id, "adlabels", data={"adlabels": labels})
        except Exception as exc:
            if not payload:
                raise
            changed = ", ".join(sorted(payload))
            note = f"Creative {creative_id} was already updated ({changed} changed) but the labels were not added:"
            if _outcome_unknown(exc):
                note = f"Creative {creative_id} was already updated ({changed} changed); the labels may or may not have been added:"
            wrapped = _with_context(exc, note)
            if wrapped is exc:
                raise
            raise wrapped from exc
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
    response["adlabels_read_complete"] = read_complete
    if not read_complete:
        response["adlabels_note"] = (
            f"Only the first {ADLABEL_READ_LIMIT} labels could be read, so previous.adlabels is partial"
            + ("; the add only added labels, and the full resulting set is unknown." if operation == "add" else ".")
        )
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


# One-step boost: objective and destination -> ad set optimization goal and what the ad
# set promotes. Engagement (post and video), awareness, and sales and leads with a pixel
# come from the objective mapping table on Meta's campaign reference page. Profile visits
# (INSTAGRAM_PROFILE with VISIT_INSTAGRAM_PROFILE) and website traffic (WEBSITE with
# LINK_CLICKS) use values listed on Meta's ad set create reference; the table does not
# pair them, so they rest on that reference plus Meta's own boost tool. The first
# destination listed for each objective is its default.
BOOST_OBJECTIVES: dict[str, dict[str | None, dict[str, Any]]] = {
    "OUTCOME_ENGAGEMENT": {
        "ON_POST": {"optimization_goal": "POST_ENGAGEMENT"},
        "ON_VIDEO": {"optimization_goal": "THRUPLAY"},
    },
    "OUTCOME_AWARENESS": {
        None: {"optimization_goal": "REACH", "promoted_page": True},
    },
    "OUTCOME_TRAFFIC": {
        "INSTAGRAM_PROFILE": {"optimization_goal": "VISIT_INSTAGRAM_PROFILE"},
        "WEBSITE": {"optimization_goal": "LINK_CLICKS", "needs_link": True},
    },
    "OUTCOME_SALES": {
        "WEBSITE": {"optimization_goal": "OFFSITE_CONVERSIONS", "needs_link": True, "needs_pixel": True},
    },
    "OUTCOME_LEADS": {
        "WEBSITE": {"optimization_goal": "OFFSITE_CONVERSIONS", "needs_link": True, "needs_pixel": True},
    },
}
BOOST_DEFAULT_OBJECTIVE = "OUTCOME_ENGAGEMENT"
PARTNERSHIP_CODE_SHOWN = "<partnership_ad_code>"
BOOST_DEFAULT_DURATION_DAYS = 6
BOOST_MAX_DURATION_DAYS = 365
BOOST_BILLING_EVENT = "IMPRESSIONS"
BOOST_BID_STRATEGY = "LOWEST_COST_WITHOUT_CAP"
BOOST_PARTNERSHIP_AD_FORMATS = (1, 2, 3)
_DELETE_TOOL = {"campaign": "delete_campaign", "ad set": "delete_adset", "creative": "delete_creative", "ad": "delete_ad"}


def _boost_goal(
    objective: str | None,
    destination_type: str | None,
    promoted_object: dict[str, Any] | None,
    call_to_action: dict[str, Any] | None,
) -> tuple[str, str | None, dict[str, Any]]:
    """Resolve objective and destination to Meta's ad set settings, or refuse before any request."""
    resolved_objective = (blank_to_none(objective) or BOOST_DEFAULT_OBJECTIVE).upper()
    if resolved_objective not in BOOST_OBJECTIVES:
        raise ValidationError(
            f"objective must be one of {', '.join(BOOST_OBJECTIVES)} in one-step mode; other objectives need their own "
            "destination and goal, so create the campaign and ad set first and pass adset_id."
        )
    destinations = BOOST_OBJECTIVES[resolved_objective]
    requested = blank_to_none(destination_type)
    if requested is None:
        destination = next(iter(destinations))
    else:
        destination = requested.upper()
        if None in destinations:
            raise ValidationError(f"{resolved_objective} takes no destination_type in one-step mode; leave it out.")
        if destination not in destinations:
            raise ValidationError(
                f"destination_type for {resolved_objective} must be one of {', '.join(str(key) for key in destinations)} "
                "in one-step mode."
            )
    mapping = destinations[destination]
    if mapping.get("needs_pixel"):
        if not isinstance(promoted_object, dict) or not promoted_object.get("pixel_id") or not blank_to_none(str(promoted_object.get("custom_event_type") or "")):
            raise ValidationError(
                f"{resolved_objective} needs promoted_object with pixel_id and custom_event_type, for example "
                "{\"pixel_id\": \"123\", \"custom_event_type\": \"PURCHASE\"}."
            )
        if not str(promoted_object["pixel_id"]).strip().isdigit():
            raise ValidationError("promoted_object.pixel_id must be a numeric pixel id.")
        _refuse_url_logins(promoted_object, "promoted_object")
    elif promoted_object is not None:
        raise ValidationError("promoted_object applies to OUTCOME_SALES and OUTCOME_LEADS in one-step mode.")
    if mapping.get("needs_link"):
        value = call_to_action.get("value") if isinstance(call_to_action, dict) else None
        link = value.get("link") if isinstance(value, dict) else None
        if not isinstance(link, str) or not link.strip().startswith(("http://", "https://")):
            raise ValidationError(
                f"{resolved_objective} with destination {destination} sends people to a website, so call_to_action "
                "needs a website link, for example {\"type\": \"LEARN_MORE\", \"value\": {\"link\": \"https://...\"}}."
            )
    return resolved_objective, destination, mapping


def _boost_creative_spec(
    *,
    page_id: str | None,
    instagram_account_id: str,
    media_id: str | None,
    ad_code: str | None,
    call_to_action: dict[str, Any] | None,
    url_tags: str | None,
    sponsor_page_id: str | None,
    sponsor_instagram_account_id: str | None,
    partnership_ad_format: int | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The creative for the post, and any Partnership Ad defaults applied.

    With a creator's Partnership Ad code the post is named by the code (branded_content
    instagram_boost_post_access_token) and the brand is named as sponsor on Facebook and
    Instagram, as in Meta's "Boost Existing Instagram Media as Partnership Ads" guide.
    """
    spec: dict[str, Any] = {"object_id": page_id}
    defaults: dict[str, str] = {}
    if ad_code is None:
        spec["instagram_user_id"] = instagram_account_id
        spec["source_instagram_media_id"] = media_id
    if call_to_action is not None:
        spec["call_to_action"] = call_to_action
    if blank_to_none(url_tags):
        spec["url_tags"] = blank_to_none(url_tags)
    sponsor_page = blank_to_none(sponsor_page_id)
    sponsor_instagram = blank_to_none(sponsor_instagram_account_id)
    if ad_code is not None:
        if sponsor_page is None:
            sponsor_page = page_id
            defaults["sponsor_page_id"] = f"{page_id} (page_id)"
        if sponsor_instagram is None:
            sponsor_instagram = instagram_account_id
            defaults["sponsor_instagram_account_id"] = f"{instagram_account_id} (instagram_account_id)"
        branded: dict[str, Any] = {"instagram_boost_post_access_token": ad_code}
        if partnership_ad_format is not None:
            branded["ad_format"] = partnership_ad_format
        spec["branded_content"] = branded
    if sponsor_page:
        spec["facebook_branded_content"] = {"sponsor_page_id": sponsor_page}
    if sponsor_instagram:
        spec["instagram_branded_content"] = {"sponsor_id": sponsor_instagram}
    if ad_code is None and partnership_ad_format is not None:
        spec["branded_content"] = {"ad_format": partnership_ad_format}
    return spec, defaults


def _without_ad_code(spec: dict[str, Any]) -> dict[str, Any]:
    """A creative spec for display: the creator's Partnership Ad code is never shown."""
    branded = spec.get("branded_content")
    if not isinstance(branded, dict) or "instagram_boost_post_access_token" not in branded:
        return spec
    return {**spec, "branded_content": {**branded, "instagram_boost_post_access_token": PARTNERSHIP_CODE_SHOWN}}


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
    destination_type: str | None,
    promoted_object: dict[str, Any] | None,
    call_to_action: dict[str, Any] | None,
    partnership_defaults: dict[str, str],
    plan_only: bool,
) -> dict[str, Any]:
    """Create campaign, ad set, creative and ad, all paused, or return the plan."""
    if daily_budget is None:
        raise ValidationError("daily_budget (account currency, major units) is required when adset_id is omitted; this tool does not pick a budget for you.")
    validate_positive_amount(daily_budget, field_name="daily_budget")
    if not isinstance(targeting, dict) or not targeting:
        raise ValidationError("targeting is required when adset_id is omitted, for example {\"geo_locations\": {\"countries\": [\"US\"]}}.")
    resolved_objective, destination, mapping = _boost_goal(objective, destination_type, promoted_object, call_to_action)
    days = BOOST_DEFAULT_DURATION_DAYS if duration_days is None else duration_days
    if not isinstance(days, int) or not 1 <= days <= BOOST_MAX_DURATION_DAYS:
        raise ValidationError(f"duration_days must be a whole number from 1 to {BOOST_MAX_DURATION_DAYS}.")
    if page_id is None:
        raise ValidationError("page_id is required when adset_id is omitted: the creative needs the Facebook Page linked to the Instagram account.")
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
    if destination:
        adset_data["destination_type"] = destination
    if mapping.get("promoted_page"):
        adset_data["promoted_object"] = {"page_id": page_id}
    elif mapping.get("needs_pixel"):
        adset_data["promoted_object"] = promoted_object
    creative_data = {"name": f"{ad_name} creative", **creative_spec}
    ad_data = {"name": ad_name, "adset_id": "<ad set id>", "creative": {"creative_id": "<creative id>"}, "status": "PAUSED"}
    defaults = {
        "objective": resolved_objective if objective is None else None,
        "duration_days": days if duration_days is None else None,
        "optimization_goal": mapping["optimization_goal"],
        "billing_event": BOOST_BILLING_EVENT,
        "bid_strategy": BOOST_BID_STRATEGY,
        "special_ad_categories": [],
        "is_adset_budget_sharing_enabled": False,
        "start_time": "when Meta creates the ad set (no start_time sent)",
        "end_time": end_time,
        "status": "PAUSED for all four objects",
        "names": f"derived from '{ad_name}'",
        **partnership_defaults,
    }
    defaults = {key: value for key, value in defaults.items() if value is not None}
    if blank_to_none(destination_type) is None:
        defaults["destination_type"] = destination
    plan = {
        "currency": currency,
        "daily_budget": {"major_units": daily_budget, "sent_minor_units": budget_minor},
        "requests": [
            {"step": "campaign", "method": "POST", "path": f"{account_id}/campaigns", "data": campaign_data},
            {"step": "ad set", "method": "POST", "path": f"{account_id}/adsets", "data": adset_data},
            {"step": "creative", "method": "POST", "path": f"{account_id}/adcreatives", "data": _without_ad_code(creative_data)},
            {"step": "ad", "method": "POST", "path": f"{account_id}/ads", "data": ad_data},
        ],
        "defaults_applied": defaults,
    }
    target = {"account_id": account_id, "instagram_media_id": media_id, "instagram_account_id": instagram_account_id, "page_id": page_id}
    if media_id is None:
        target["partnership_ad_code"] = "<given, not shown>"
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
    instagram_media_id: str | None = None,
    instagram_account_id: str | None = None,
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
    destination_type: str | None = None,
    promoted_object: dict[str, Any] | None = None,
    partnership_ad_code: str | None = None,
) -> dict[str, Any]:
    """Boost an existing Instagram post, reel or story. With adset_id: creates a creative from it and a PAUSED ad in that ad set (page_id defaults to the ad set's promoted_object page; validate_only checks the ad with Meta). Without adset_id (one-step): creates a campaign, ad set, creative and ad, all PAUSED, from daily_budget (major units, never defaulted), targeting, duration_days (default 6) and objective: OUTCOME_ENGAGEMENT (default; destination_type ON_POST or ON_VIDEO), OUTCOME_AWARENESS, OUTCOME_TRAFFIC (INSTAGRAM_PROFILE profile visits by default, or WEBSITE), OUTCOME_SALES or OUTCOME_LEADS (WEBSITE, with promoted_object pixel_id and custom_event_type). Website destinations need call_to_action with value.link. Every default is stated; plan_only returns the plan and creates nothing. If a later step fails the error names every object already created. Partnership Ad: sponsor_page_id and/or sponsor_instagram_account_id, optional partnership_ad_format 1, 2 or 3; for a creator's post, partnership_ad_code (the code the creator issued) instead of instagram_media_id. Check list_instagram_media boost_eligible first."""
    ids = {
        "account_id": blank_to_none(account_id),
        "instagram_account_id": blank_to_none(instagram_account_id),
    }
    for field_name, value in ids.items():
        if value is None:
            raise ValidationError(f"{field_name} is required.")
    media_id = blank_to_none(instagram_media_id)
    ad_code = blank_to_none(partnership_ad_code)
    if (media_id is None) == (ad_code is None):
        raise ValidationError(
            "Give exactly one of instagram_media_id or partnership_ad_code: the media id for a post this account can "
            "boost, or the Partnership Ad code a creator issued for their post."
        )
    if ad_code is not None and (len(ad_code) > 512 or any(character.isspace() for character in ad_code)):
        raise ValidationError("partnership_ad_code must be the code exactly as the creator issued it, without spaces.")
    ids["adset_id"] = blank_to_none(adset_id)
    one_step = ids["adset_id"] is None
    if one_step and validate_only:
        raise ValidationError("validate_only needs an existing adset_id; in one-step mode use plan_only to see what would be created.")
    if not one_step:
        extra = [
            label for label, value in (
                ("daily_budget", daily_budget), ("duration_days", duration_days), ("targeting", targeting),
                ("objective", objective), ("destination_type", blank_to_none(destination_type)),
                ("promoted_object", promoted_object),
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
        _refuse_url_logins(call_to_action, "call_to_action")
    if promoted_object is not None and not isinstance(promoted_object, dict):
        raise ValidationError("promoted_object must be an object, for example {\"pixel_id\": \"123\", \"custom_event_type\": \"PURCHASE\"}.")
    if partnership_ad_format is not None:
        if partnership_ad_format not in BOOST_PARTNERSHIP_AD_FORMATS:
            raise ValidationError("partnership_ad_format must be 1 (both identities), 2 (first identity only) or 3 (automatic).")
        if not (blank_to_none(sponsor_page_id) or blank_to_none(sponsor_instagram_account_id) or ad_code):
            raise ValidationError("partnership_ad_format needs sponsor_page_id, sponsor_instagram_account_id or partnership_ad_code.")
    if targeting is not None:
        _ensure_v25_creative_payload(targeting)
    resolved_account_id = normalize_account_id(ids["account_id"])
    ad_name = blank_to_none(name) or (f"Instagram post {media_id}" if media_id else "Partnership ad")
    resolved_page_id = blank_to_none(page_id)

    def creative_for(page: str | None) -> tuple[dict[str, Any], dict[str, str]]:
        return _boost_creative_spec(
            page_id=page,
            instagram_account_id=ids["instagram_account_id"],
            media_id=media_id,
            ad_code=ad_code,
            call_to_action=call_to_action,
            url_tags=url_tags,
            sponsor_page_id=sponsor_page_id,
            sponsor_instagram_account_id=sponsor_instagram_account_id,
            partnership_ad_format=partnership_ad_format,
        )

    if one_step:
        creative_spec, partnership_defaults = creative_for(resolved_page_id)
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
            destination_type=destination_type,
            promoted_object=promoted_object,
            call_to_action=call_to_action,
            partnership_defaults=partnership_defaults,
            plan_only=plan_only,
        )
    client = get_graph_api_client()
    if resolved_page_id is None:
        adset = await client.get_object(ids["adset_id"], fields=["id", "promoted_object"])
        adset_promoted = adset.get("promoted_object")
        if isinstance(adset_promoted, dict):
            resolved_page_id = blank_to_none(str(adset_promoted.get("page_id") or ""))
        if resolved_page_id is None:
            raise ValidationError(
                "page_id is required: Meta's Instagram post creative needs the Facebook Page id, "
                "and the ad set's promoted_object has no page_id."
            )
    creative_spec, partnership_defaults = creative_for(resolved_page_id)
    target = {
        "account_id": resolved_account_id,
        "adset_id": ids["adset_id"],
        "instagram_media_id": media_id,
        "instagram_account_id": ids["instagram_account_id"],
        "page_id": resolved_page_id,
    }
    if ad_code is not None:
        target["partnership_ad_code"] = "<given, not shown>"
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
        if partnership_defaults:
            response["partnership_defaults_applied"] = partnership_defaults
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
    except Exception as exc:
        if _outcome_unknown(exc):
            note = (
                f"Creative {creative_id} was created; the ad may have been created too, because Meta's answer was lost. "
                f"Check the ads in ad set {ids['adset_id']} (list_ads) before retrying, and delete the creative with "
                "delete_creative only if no ad uses it."
            )
        else:
            note = (
                f"Creative {creative_id} was created but the ad was not. "
                "Delete the creative with delete_creative or retry the ad with create_ad."
            )
        wrapped = _with_context(exc, f"{note} Reason:")
        if wrapped is exc:
            raise
        raise wrapped from exc
    response = creation_response(
        action="boost_instagram_post",
        target=target,
        result={"creative": creative, "ad": ad},
    )
    response.update({"creative_id": creative_id, "ad_id": str(ad.get("id") or "") or None, "status": "PAUSED"})
    if partnership_defaults:
        response["partnership_defaults_applied"] = partnership_defaults
    return response
