"""Creative and experiment tools."""

from __future__ import annotations

import dataclasses
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypeAlias
from urllib.parse import quote, quote_plus

from pydantic import BeforeValidator

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, RateLimitError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.local_files import LocalUpload, open_upload_file, read_upload_file
from meta_ads_mcp.money import resolve_account_currency, to_minor_units, validate_positive_amount
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import (
    AccountId,
    FieldList,
    GraphId,
    StrictCurrencyAmount,
    StringList,
    coerce_csv_string_list,
)
from meta_ads_mcp.url_secrets import refuse_url_credentials


def _refuse_true_false(value: Any) -> Any:
    """MCP clients may send true or false where a number belongs; never read them as 1 or 0."""
    if isinstance(value, bool):
        raise ValueError("must be a whole number, not true or false")
    return value


# A whole number that refuses true and false (which the lax int type reads as 1 and 0).
_WholeNumber: TypeAlias = Annotated[int, BeforeValidator(_refuse_true_false)]
# A time as ISO 8601 text with a UTC offset, or Unix seconds as a number or digits.
_TimeInput: TypeAlias = Annotated[str | int, BeforeValidator(_refuse_true_false)]

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


_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _key_label(key: Any) -> str:
    """A nested key as it may appear in a message: a field-like name, or a placeholder for anything else."""
    return key if isinstance(key, str) and _SAFE_KEY.match(key) else "<key>"


def _error_attribute_names(exc: BaseException) -> list[str]:
    """The data attributes an error carries: dataclass fields, slots and instance attributes."""
    names: list[str] = [field.name for field in dataclasses.fields(exc)] if dataclasses.is_dataclass(exc) else []
    for cls in type(exc).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        names.extend([slots] if isinstance(slots, str) else list(slots))
    names.extend(getattr(exc, "__dict__", {}))
    return [name for name in dict.fromkeys(names) if not name.startswith("__")]


def _scrub_value(value: Any, secrets: list[str], shown: str) -> Any:
    """value with every secret replaced, inside strings, dict keys and values, lists and tuples."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, shown)
        return value
    if isinstance(value, dict):
        return {_scrub_value(key, secrets, shown): _scrub_value(item, secrets, shown) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(item, secrets, shown) for item in value)
    return value


def _scrub_error(exc: BaseException, secrets: list[str], shown: str) -> None:
    """Remove secret text from a raised error and every error behind it, in place.

    A traceback log prints the whole __cause__ and __context__ chain, and the MCP error
    shows the top error's public fields, so every error reachable that way (and inside an
    exception group) is cleaned: its fields, args and notes. Each error is visited once,
    so a cycle ends.
    """
    secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    if not secrets:
        return
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for name in _error_attribute_names(current):
            try:
                value = getattr(current, name)
            except AttributeError:
                continue
            if value is None or isinstance(value, BaseException):
                continue
            cleaned = _scrub_value(value, secrets, shown)
            if cleaned != value:
                try:
                    setattr(current, name, cleaned)
                except (AttributeError, TypeError):
                    pass
        if current.args:
            current.args = tuple(_scrub_value(arg, secrets, shown) for arg in current.args)
        notes = getattr(current, "__notes__", None)
        if isinstance(notes, list):
            current.__notes__ = [_scrub_value(note, secrets, shown) for note in notes]
        pending.extend([current.__cause__, current.__context__])
        pending.extend(getattr(current, "exceptions", None) or ())


def _secret_forms(secret: str | None) -> list[str]:
    """A secret as given, trimmed, and URL-encoded, the ways an error could repeat it."""
    if not isinstance(secret, str) or not secret.strip():
        return []
    forms = {secret, secret.strip()}
    forms |= {quote(form, safe="") for form in list(forms)} | {quote_plus(form) for form in list(forms)}
    return [form for form in forms if form]


def _refuse_url_logins(value: Any, name: str) -> None:
    """Refuse a login embedded in any URL inside a nested input, before any request is built.

    The message names the location by field-like keys only, never by a key that may be pasted data.
    """
    if isinstance(value, str):
        if "://" in value or "@" in value:
            refuse_url_credentials(value, name)
    elif isinstance(value, dict):
        for key, item in value.items():
            _refuse_url_logins(item, f"{name}.{_key_label(key)}")
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


# Meta's ad image reference (act_{id}/adimages) names neither file types nor a size
# limit and points to the Ads Guide, whose image placements list "JPG or PNG" with a
# maximum file size of 30MB (facebook.com/business/ads-guide/image/facebook-feed).
IMAGE_FILE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024


def _read_image_file(file_path: str) -> LocalUpload:
    """Read an image from the operator's upload folder; errors never quote the path."""
    return read_upload_file(
        file_path,
        allowed_types=IMAGE_FILE_TYPES,
        max_bytes=MAX_IMAGE_BYTES,
        too_large_hint="Meta's Ads Guide gives 30 MB as the largest image file for ads.",
    )


@mcp_server.tool()
async def upload_creative_asset(
    account_id: str,
    file_path: str | None = None,
    image_url: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Use this when the user needs an image asset uploaded before creating or updating a creative. Give exactly one of image_url or file_path. file_path is read only from the folder named by the META_UPLOAD_DIR environment variable (a path inside it, or a name relative to it); local files are refused while that variable is unset. Files must be JPG or PNG (.jpg, .jpeg or .png, the types Meta's Ads Guide lists for image ads) and at most 30 MB."""
    if bool(file_path) == bool(image_url):
        raise ValidationError("Provide exactly one of file_path or image_url.")
    image_file = _read_image_file(file_path) if file_path else None
    client = get_graph_api_client()
    if image_file is not None:
        result = await client.upload_ad_image(
            normalize_account_id(account_id),
            file_name=image_file.name,
            file_content=image_file.content,
            name=name,
        )
    else:
        result = await client.upload_ad_image(
            normalize_account_id(account_id),
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


@dataclass(slots=True)
class _OpenVideo:
    """A local video opened for upload: the open descriptor is what gets read."""

    descriptor: int
    name: str
    size: int
    modified_ns: int


FILE_SHOWN = "<file>"
_FILE_CHANGED = (
    "The local file changed while it was being uploaded (its size or modification time is not what it was "
    "when it was opened), so the bytes sent may not be the whole file."
)


def _video_file_change(video: _OpenVideo) -> str | None:
    """Why the opened file no longer matches what was opened, or None when it is unchanged."""
    try:
        details = os.fstat(video.descriptor)
    except OSError:
        return "The local file could not be checked for changes while it was being uploaded."
    if details.st_size != video.size or details.st_mtime_ns != video.modified_ns:
        return _FILE_CHANGED
    return None


def _open_upload_video(file_path: str, parameter: str = "file_path") -> _OpenVideo:
    """Open a video inside the upload folder without reading it all into memory.

    local_files.open_upload_file does every check (folder from META_UPLOAD_DIR, links
    resolved and checked, type from the real name, no link followed on the way to the
    file, size read from the open file). The caller closes the descriptor.
    """
    opened = open_upload_file(
        file_path,
        allowed_types=VIDEO_FILE_TYPES,
        max_bytes=MAX_CHUNKED_VIDEO_BYTES,
        parameter=parameter,
        too_large_message=(
            f"{parameter} is larger than this server's {MAX_CHUNKED_VIDEO_BYTES}-byte limit for "
            "video uploads (Meta's limit for ad videos is 4 GB)."
        ),
    )
    try:
        details = os.fstat(opened.descriptor)
    except OSError:
        os.close(opened.descriptor)
        raise ValidationError(f"{parameter} could not be read.") from None
    if details.st_size != opened.size:
        os.close(opened.descriptor)
        raise ValidationError(f"{parameter} changed size while it was being opened; try again once it is complete.")
    return _OpenVideo(descriptor=opened.descriptor, name=opened.name, size=opened.size, modified_ns=details.st_mtime_ns)


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
    """Run start, transfer and finish on the advideos edge; on failure name the session and what was sent.

    The upload is complete only when Meta's offsets reach the file size. Each answer must
    continue exactly where the chunk just sent ended: no gap (bytes never sent) and no
    step backwards. The opened file is checked for changes before each transfer and before
    the finish. Messages never name the local file.
    """
    try:
        started = await client.post_ad_video_upload_phase(
            account, data={"upload_phase": "start", "file_size": video.size},
        )
    except Exception as exc:
        prefix = "Starting the chunked upload failed, so nothing was transferred."
        if _outcome_unknown(exc):
            prefix = (
                "Starting the chunked upload may or may not have opened an upload session; "
                "no video bytes were sent."
            )
        wrapped = _with_context(exc, f"{prefix} Reason:")
        if wrapped is exc:
            raise
        raise wrapped from exc
    session_id = str(started.get("upload_session_id") or "") if isinstance(started, dict) else ""
    video_id = str(started.get("video_id") or "") if isinstance(started, dict) else ""
    if not session_id:
        raise MetaApiError(message="Meta did not return an upload session for the chunked upload; nothing was transferred.")
    progress = {"phase": "transfer", "confirmed": 0, "requests": 0}

    def stopped_message(reason: str) -> str:
        message = (
            f"The chunked upload stopped during the {progress['phase']} step: upload session {session_id}, "
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
        sent_end: int | None = None
        while True:
            if start is None or end is None or not 0 <= start <= end <= video.size:
                raise _ChunkedUploadStopped(
                    f"Meta asked for bytes {started.get('start_offset') if start is None else start} to "
                    f"{started.get('end_offset') if end is None else end}, outside the {video.size}-byte file."
                )
            expected = 0 if sent_end is None else sent_end
            if start < expected:
                raise _ChunkedUploadStopped(
                    f"Meta's next chunk (from byte {start}) did not move forward to byte {expected}, "
                    "where the bytes already sent end."
                )
            if start > expected:
                raise _ChunkedUploadStopped(
                    f"Meta's next chunk starts at byte {start}, but only bytes 0 to {expected} were sent in this "
                    f"session, so bytes {expected} to {start} would never be sent."
                )
            progress["confirmed"] = start
            if start == end:
                if end != video.size:
                    raise _ChunkedUploadStopped(
                        f"Meta ended the transfer at byte {end} of the {video.size}-byte file, so the rest was never sent."
                    )
                break
            if end - start > MAX_VIDEO_CHUNK_BYTES:
                raise _ChunkedUploadStopped(
                    f"Meta asked for a {end - start}-byte chunk, larger than this server reads at once ({MAX_VIDEO_CHUNK_BYTES} bytes)."
                )
            if progress["requests"] >= MAX_CHUNKED_TRANSFER_REQUESTS:
                raise _ChunkedUploadStopped(
                    f"The upload reached this server's {MAX_CHUNKED_TRANSFER_REQUESTS}-request limit for one video."
                )
            changed = _video_file_change(video)
            if changed:
                raise _ChunkedUploadStopped(changed)
            chunk = _read_video_bytes(video, start, end - start)
            progress["requests"] += 1
            answer = await client.post_ad_video_upload_phase(
                account,
                data={"upload_phase": "transfer", "upload_session_id": session_id, "start_offset": start},
                files={"video_file_chunk": (video.name, chunk)},
            )
            sent_end = end
            started = answer if isinstance(answer, dict) else {}
            start = _offset(started.get("start_offset"))
            end = _offset(started.get("end_offset"))
        progress["phase"] = "finish"
        changed = _video_file_change(video)
        if changed:
            raise _ChunkedUploadStopped(changed)
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
                "repeats": (
                    "while each answer continues exactly where the chunk sent ended, until start_offset and "
                    "end_offset both equal the file size"
                ),
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
        return await _upload_video_file(resolved_account, target, video, meta_fields, dry_run=dry_run)
    except Exception as exc:
        # A file name can itself be private: no error or log line names the file, even
        # when Meta's own error text repeats it.
        _scrub_error(exc, [*_secret_forms(video.name), *_secret_forms(path_text)], FILE_SHOWN)
        raise
    finally:
        os.close(video.descriptor)


async def _upload_video_file(
    account: str,
    target: dict[str, Any],
    video: _OpenVideo,
    meta_fields: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Upload an opened local video in one request or in chunks; the caller closes it."""
    if video.size <= MAX_DIRECT_VIDEO_BYTES:
        source = {"file_name": video.name, "bytes": video.size}
        content = b""
        if not dry_run:
            content = _read_video_bytes(video, 0, video.size)
            if _video_file_change(video):
                raise ValidationError(
                    "file_path changed while it was being read (its size or modification time differs from when it "
                    "was opened), so nothing was sent. Upload it again once it is complete."
                )
        return await _upload_video_request(
            account, target, data=dict(meta_fields), files={"source": (video.name, content)},
            shown={"source": source, **meta_fields}, upload={"mode": "direct", **source}, dry_run=dry_run,
        )
    if dry_run:
        return {
            "ok": True,
            "action": "upload_ad_video",
            "dry_run": True,
            "sent": False,
            "target": target,
            "would_send": _chunked_plan(account, video, meta_fields),
        }
    client = get_graph_api_client()
    outcome = await _chunked_video_upload(client, account, video, meta_fields)
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
        raise ValidationError(f"No image with the given image_hash was found in {resolved_account}; nothing was deleted.")
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
            raise ValidationError(
                f"{len(missing)} of the adlabels to remove {'is' if len(missing) == 1 else 'are'} not on creative "
                f"{creative_id}, so nothing was changed. Read the creative's labels with get_creative first."
            )
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


# One-step boost. Each objective lists the destinations it takes in one step; for each
# destination, the optimization goals Meta documents for it (the first is the default),
# what the ad set promotes, and what the post's call to action must be. Sources:
# - Campaign reference, Objective Mapping table (ad-campaign-group#odax-mapping):
#   awareness; engagement on a post or video; Messenger and WhatsApp; PHONE_CALL; instant
#   forms (ON_AD); sales and leads with a pixel; app promotion with application_id and
#   object_store_url.
# - Click to Instagram and click to WhatsApp guides (ad-creative/messaging-ads):
#   INSTAGRAM_DIRECT and WHATSAPP with CONVERSATIONS, billing IMPRESSIONS, promoted_object
#   page_id, and the call to action each shows for an existing Instagram post.
# - Use Posts as Instagram Ads guide: MESSAGE_PAGE with app_destination MESSENGER on a post.
# - Call ads guide: PHONE_CALL with QUALITY_CALL, billing IMPRESSIONS, CALL_NOW with a tel: link.
# - Ad set reference: INSTAGRAM_PROFILE with VISIT_INSTAGRAM_PROFILE, and WEBSITE.
# Combinations those pages do not show (for example OUTCOME_SALES with a messaging
# destination, where the guides and the mapping table disagree on the pixel) are not offered.
def _route(goals: tuple[str, ...], *, promoted: str | None = None, cta: str | None = None) -> dict[str, Any]:
    return {"goals": goals, "promoted": promoted, "cta": cta}


_ENGAGEMENT_MESSAGING_GOALS = ("CONVERSATIONS", "LINK_CLICKS")
_TRAFFIC_MESSAGING_GOALS = ("CONVERSATIONS", "LINK_CLICKS", "REACH", "IMPRESSIONS")
BOOST_OBJECTIVES: dict[str, dict[str | None, dict[str, Any]]] = {
    "OUTCOME_ENGAGEMENT": {
        "ON_POST": _route(("POST_ENGAGEMENT", "REACH")),
        "ON_VIDEO": _route(("THRUPLAY", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS")),
        "INSTAGRAM_DIRECT": _route(_ENGAGEMENT_MESSAGING_GOALS, promoted="page", cta="message"),
        "WHATSAPP": _route(_ENGAGEMENT_MESSAGING_GOALS, promoted="page", cta="message"),
        "MESSENGER": _route(_ENGAGEMENT_MESSAGING_GOALS, promoted="page", cta="message"),
    },
    "OUTCOME_AWARENESS": {
        None: _route(("REACH", "IMPRESSIONS", "AD_RECALL_LIFT", "THRUPLAY", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"), promoted="page"),
    },
    "OUTCOME_TRAFFIC": {
        "INSTAGRAM_PROFILE": _route(("VISIT_INSTAGRAM_PROFILE",)),
        "WEBSITE": _route(("LINK_CLICKS", "LANDING_PAGE_VIEWS", "IMPRESSIONS"), cta="website"),
        "INSTAGRAM_DIRECT": _route(_TRAFFIC_MESSAGING_GOALS, promoted="page", cta="message"),
        "WHATSAPP": _route(_TRAFFIC_MESSAGING_GOALS, promoted="page", cta="message"),
        "MESSENGER": _route(_TRAFFIC_MESSAGING_GOALS, promoted="page", cta="message"),
        "PHONE_CALL": _route(("QUALITY_CALL", "LINK_CLICKS"), cta="call"),
    },
    "OUTCOME_SALES": {
        "WEBSITE": _route(("OFFSITE_CONVERSIONS",), promoted="pixel", cta="website"),
        "PHONE_CALL": _route(("QUALITY_CALL",), promoted="page", cta="call"),
    },
    "OUTCOME_LEADS": {
        "WEBSITE": _route(("OFFSITE_CONVERSIONS",), promoted="pixel", cta="website"),
        "ON_AD": _route(("LEAD_GENERATION", "QUALITY_LEAD"), promoted="page", cta="lead_form"),
        "WHATSAPP": _route(("CONVERSATIONS",), promoted="page", cta="message"),
        "PHONE_CALL": _route(("QUALITY_CALL",), promoted="page", cta="call"),
    },
    "OUTCOME_APP_PROMOTION": {
        None: _route(("APP_INSTALLS", "LINK_CLICKS", "OFFSITE_CONVERSIONS"), promoted="app", cta="app"),
    },
}
# The call to action each messaging guide shows for an existing Instagram post.
BOOST_MESSAGE_CALLS_TO_ACTION: dict[str, dict[str, Any]] = {
    "INSTAGRAM_DIRECT": {"type": "INSTAGRAM_MESSAGE", "value": {"link": "https://www.instagram.com"}},
    "WHATSAPP": {"type": "WHATSAPP_MESSAGE", "value": {"link": "https://api.whatsapp.com/send", "app_destination": "WHATSAPP"}},
    "MESSENGER": {"type": "MESSAGE_PAGE", "value": {"app_destination": "MESSENGER"}},
}
# Call-to-action types tied to one destination; elsewhere they are refused.
_DESTINATION_CALL_TO_ACTIONS = {"INSTAGRAM_MESSAGE": "INSTAGRAM_DIRECT", "WHATSAPP_MESSAGE": "WHATSAPP", "MESSAGE_PAGE": "MESSENGER", "CALL_NOW": "PHONE_CALL"}
# Lead ads guide: the call-to-action types an instant form accepts.
BOOST_LEAD_FORM_CALL_TO_ACTIONS = ("APPLY_NOW", "DOWNLOAD", "GET_QUOTE", "LEARN_MORE", "SIGN_UP", "SUBSCRIBE")
_TEL_LINK = re.compile(r"^tel:\+[0-9]{6,15}$")
BOOST_DEFAULT_OBJECTIVE = "OUTCOME_ENGAGEMENT"
PARTNERSHIP_CODE_SHOWN = "<partnership_ad_code>"
BOOST_DEFAULT_DURATION_DAYS = 6
BOOST_MAX_DURATION_DAYS = 365
BOOST_MIN_RUN = timedelta(hours=24)
BOOST_BILLING_EVENT = "IMPRESSIONS"
BOOST_BID_STRATEGY = "LOWEST_COST_WITHOUT_CAP"
# Campaign and ad set references: bid_amount is required with these two and means nothing without them.
BOOST_BID_STRATEGIES = ("LOWEST_COST_WITHOUT_CAP", "LOWEST_COST_WITH_BID_CAP", "COST_CAP")
_CAPPED_BID_STRATEGIES = {"LOWEST_COST_WITH_BID_CAP", "COST_CAP"}
# Campaign reference special_ad_categories values.
BOOST_SPECIAL_AD_CATEGORIES = (
    "NONE", "EMPLOYMENT", "HOUSING", "CREDIT", "ISSUES_ELECTIONS_POLITICS", "ONLINE_GAMBLING_AND_GAMING",
    "FINANCIAL_PRODUCTS_SERVICES",
)
BOOST_PARTNERSHIP_AD_FORMATS = (1, 2, 3)
BOOST_PAGE_LOOKUP_MAX_REQUESTS = 10
BOOST_PAGE_LOOKUP_PAGE_SIZE = 100
_MESSAGING_DESTINATIONS = frozenset(BOOST_MESSAGE_CALLS_TO_ACTION)
_DELETE_TOOL = {"campaign": "delete_campaign", "ad set": "delete_adset", "creative": "delete_creative", "ad": "delete_ad"}
_BUDGET_PARAMETERS = ("daily_budget", "lifetime_budget", "campaign_daily_budget", "campaign_lifetime_budget")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _upper(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text.")
    return blank_to_none(value.strip().upper())


def _boost_billing_events(goal: str, destination: str | None) -> tuple[str, ...]:
    """Billing events for a goal, from Meta's optimization goal and billing event table.

    The messaging and call ads guides require IMPRESSIONS for their destinations.
    """
    allowed = [BOOST_BILLING_EVENT]
    if destination in _MESSAGING_DESTINATIONS or destination == "PHONE_CALL":
        return tuple(allowed)
    if goal == "LINK_CLICKS":
        allowed.append("LINK_CLICKS")
    if goal == "THRUPLAY":
        allowed.append("THRUPLAY")
    return tuple(allowed)


def _boost_route(objective: str | None, destination_type: str | None) -> tuple[str, str | None, dict[str, Any]]:
    """Resolve objective and destination, or refuse before any request."""
    resolved_objective = _upper(objective, "objective") or BOOST_DEFAULT_OBJECTIVE
    if resolved_objective not in BOOST_OBJECTIVES:
        raise ValidationError(
            f"objective must be one of {', '.join(BOOST_OBJECTIVES)} in one-step mode; for anything else create the "
            "campaign and ad set first and pass adset_id."
        )
    destinations = BOOST_OBJECTIVES[resolved_objective]
    requested = _upper(destination_type, "destination_type")
    if requested is None:
        destination = next(iter(destinations))
    else:
        destination = requested
        if None in destinations:
            raise ValidationError(f"{resolved_objective} takes no destination_type in one-step mode; leave it out.")
        if destination not in destinations:
            raise ValidationError(
                f"destination_type for {resolved_objective} must be one of {', '.join(str(key) for key in destinations)} "
                "in one-step mode."
            )
    return resolved_objective, destination, destinations[destination]


def _cta_type(call_to_action: dict[str, Any] | None) -> str | None:
    return str(call_to_action.get("type")).strip().upper() if isinstance(call_to_action, dict) else None


def _cta_value(call_to_action: dict[str, Any] | None) -> dict[str, Any]:
    value = call_to_action.get("value") if isinstance(call_to_action, dict) else None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("call_to_action.value must be an object.")
    return value


def _boost_call_to_action(
    kind: str | None,
    objective: str,
    destination: str | None,
    call_to_action: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool]:
    """The call to action sent with the post, and whether this tool built it."""
    cta_type = _cta_type(call_to_action)
    value = _cta_value(call_to_action)
    tied = _DESTINATION_CALL_TO_ACTIONS.get(cta_type or "")
    if tied is not None and tied != destination:
        raise ValidationError(f"call_to_action type {cta_type} needs destination_type {tied}.")
    if kind == "message":
        expected = BOOST_MESSAGE_CALLS_TO_ACTION[destination]
        if call_to_action is None:
            return deepcopy(expected), True
        if value and value != expected["value"]:
            raise ValidationError(
                f"For {destination} this tool sends the call to action Meta's guide shows for an Instagram post; "
                "leave call_to_action.value out."
            )
        return deepcopy(expected), False
    if kind == "website":
        link = value.get("link")
        if not isinstance(link, str) or not link.strip().startswith(("http://", "https://")):
            raise ValidationError(
                f"{objective} with destination {destination} sends people to a website, so call_to_action "
                "needs a website link, for example {\"type\": \"LEARN_MORE\", \"value\": {\"link\": \"https://...\"}}."
            )
        return call_to_action, False
    if kind == "lead_form":
        form_id = value.get("lead_gen_form_id")
        if cta_type not in BOOST_LEAD_FORM_CALL_TO_ACTIONS or not isinstance(form_id, (str, int)) or isinstance(form_id, bool) or not str(form_id).strip().isdigit():
            raise ValidationError(
                "destination_type ON_AD (instant form) needs call_to_action with a type from "
                f"{', '.join(BOOST_LEAD_FORM_CALL_TO_ACTIONS)} and value.lead_gen_form_id, the numeric form id, "
                "for example {\"type\": \"SIGN_UP\", \"value\": {\"lead_gen_form_id\": \"123\"}}."
            )
        if set(value) - {"lead_gen_form_id"}:
            raise ValidationError("For an instant form, call_to_action.value takes only lead_gen_form_id.")
        return {"type": cta_type, "value": {"lead_gen_form_id": str(form_id).strip()}}, False
    if kind == "call":
        link = value.get("link")
        if cta_type != "CALL_NOW" or not isinstance(link, str) or not _TEL_LINK.match(link.strip()) or set(value) - {"link"}:
            raise ValidationError(
                "destination_type PHONE_CALL needs call_to_action {\"type\": \"CALL_NOW\", \"value\": {\"link\": "
                "\"tel:\" followed by the phone number with its country code, for example tel:+15551234567}}."
            )
        return {"type": "CALL_NOW", "value": {"link": link.strip()}}, False
    if kind == "app":
        link = value.get("link")
        if call_to_action is None or not isinstance(link, str) or not link.strip().startswith(("http://", "https://")):
            raise ValidationError(
                "OUTCOME_APP_PROMOTION needs call_to_action with the app's store link, for example "
                "{\"type\": \"INSTALL_MOBILE_APP\", \"value\": {\"link\": \"https://play.google.com/store/apps/details?id=...\"}}."
            )
        return call_to_action, False
    return call_to_action, False


def _boost_promoted_object(
    kind: str | None,
    objective: str,
    destination: str | None,
    goal: str,
    promoted_object: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Check promoted_object for the route; page-based routes get page_id added once the Page is known."""
    if kind == "pixel":
        if not isinstance(promoted_object, dict) or not promoted_object.get("pixel_id") or not blank_to_none(str(promoted_object.get("custom_event_type") or "")):
            raise ValidationError(
                f"{objective} needs promoted_object with pixel_id and custom_event_type, for example "
                "{\"pixel_id\": \"123\", \"custom_event_type\": \"PURCHASE\"}."
            )
        if not str(promoted_object["pixel_id"]).strip().isdigit():
            raise ValidationError("promoted_object.pixel_id must be a numeric pixel id.")
        _refuse_url_logins(promoted_object, "promoted_object")
        return promoted_object
    if kind == "app":
        application_id = promoted_object.get("application_id") if isinstance(promoted_object, dict) else None
        store_url = promoted_object.get("object_store_url") if isinstance(promoted_object, dict) else None
        if application_id is None or not str(application_id).strip().isdigit() or isinstance(application_id, bool):
            raise ValidationError(
                "OUTCOME_APP_PROMOTION needs promoted_object with application_id (numeric) and object_store_url, for "
                "example {\"application_id\": \"123\", \"object_store_url\": \"https://play.google.com/store/apps/details?id=...\"}."
            )
        if not isinstance(store_url, str) or not store_url.strip().startswith(("http://", "https://")):
            raise ValidationError("promoted_object.object_store_url must be the app's http(s) store address.")
        if goal == "OFFSITE_CONVERSIONS" and not blank_to_none(str(promoted_object.get("custom_event_type") or "")):
            raise ValidationError(
                "optimization_goal OFFSITE_CONVERSIONS for an app needs promoted_object.custom_event_type, the app event "
                "to optimize for (Meta's ad set reference)."
            )
        _refuse_url_logins(promoted_object, "promoted_object")
        return promoted_object
    if kind == "page":
        if promoted_object is None:
            return {}
        if destination == "WHATSAPP" and isinstance(promoted_object, dict) and set(promoted_object) <= {"whatsapp_phone_number"}:
            number = promoted_object.get("whatsapp_phone_number")
            if number is not None and not (isinstance(number, str) and re.fullmatch(r"\+?[0-9]{6,15}", number.strip())):
                raise ValidationError("promoted_object.whatsapp_phone_number must be a phone number with its country code.")
            return {key: value.strip() for key, value in promoted_object.items() if isinstance(value, str)}
        allowed = " (only whatsapp_phone_number may be added)" if destination == "WHATSAPP" else ""
        raise ValidationError(
            f"promoted_object for {destination or objective} is the Page, set from page_id{allowed}; leave it out."
        )
    if promoted_object is not None:
        raise ValidationError(
            f"promoted_object does not apply to {objective}"
            + (f" with destination {destination}" if destination else "")
            + " in one-step mode."
        )
    return None


def _boost_time(value: Any, field_name: str) -> tuple[Any, datetime]:
    """A one-step time (ISO 8601 with a UTC offset, or Unix seconds): what is sent, and when it is."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f"{field_name} must be an ISO 8601 time with a UTC offset (2026-12-01T00:00:00-05:00) or Unix seconds.")
    if isinstance(value, int) or value.strip().isdigit():
        seconds = int(value if isinstance(value, int) else value.strip())
        if seconds <= 0:
            raise ValidationError(f"{field_name} must be a positive Unix time.")
        return seconds, datetime.fromtimestamp(seconds, UTC)
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"{field_name} must be an ISO 8601 time with a UTC offset (2026-12-01T00:00:00-05:00) or Unix seconds."
        ) from None
    if parsed.tzinfo is None:
        raise ValidationError(
            f"{field_name} needs a UTC offset or Z (for example 2026-12-01T00:00:00-05:00) so the ad set does not "
            "run at the wrong hour."
        )
    return text, parsed


@dataclass(slots=True)
class _BoostSettings:
    """Everything a one-step boost sends, checked before any request."""

    objective: str
    destination: str | None
    route: dict[str, Any]
    goal: str
    billing_event: str
    bid_strategy: str
    bid_amount: float | None
    budget_parameter: str
    budget: float
    promoted_object: dict[str, Any] | None
    call_to_action: dict[str, Any] | None
    call_to_action_built: bool
    special_ad_categories: list[str]
    buying_type: str | None
    start_sent: Any
    end_sent: Any
    end_computed: bool
    days: int | None
    targeting: dict[str, Any]
    given: set[str]


def _boost_settings(
    *,
    objective: str | None,
    destination_type: str | None,
    optimization_goal: str | None,
    billing_event: str | None,
    bid_strategy: str | None,
    bid_amount: float | None,
    budgets: dict[str, float | None],
    promoted_object: dict[str, Any] | None,
    call_to_action: dict[str, Any] | None,
    special_ad_categories: Any,
    buying_type: str | None,
    start_time: Any,
    end_time: Any,
    duration_days: int | None,
    targeting: dict[str, Any] | None,
) -> _BoostSettings:
    """Check every one-step input against Meta's documented combinations; nothing is sent."""
    given_budgets = {name: value for name, value in budgets.items() if value is not None}
    if not given_budgets:
        raise ValidationError(
            "daily_budget (account currency, major units) is required when adset_id is omitted, or instead one of "
            "lifetime_budget, campaign_daily_budget or campaign_lifetime_budget; this tool does not pick a budget for you."
        )
    if len(given_budgets) > 1:
        raise ValidationError(
            f"Give one budget, not {', '.join(given_budgets)}: an ad set daily or lifetime budget, or a campaign daily "
            "or lifetime budget."
        )
    budget_parameter, budget = next(iter(given_budgets.items()))
    validate_positive_amount(budget, field_name=budget_parameter)
    if not isinstance(targeting, dict) or not targeting:
        raise ValidationError("targeting is required when adset_id is omitted, for example {\"geo_locations\": {\"countries\": [\"US\"]}}.")
    resolved_objective, destination, route = _boost_route(objective, destination_type)
    goal = _upper(optimization_goal, "optimization_goal") or route["goals"][0]
    if goal not in route["goals"]:
        place = f"{resolved_objective} with destination {destination}" if destination else resolved_objective
        raise ValidationError(f"optimization_goal for {place} must be one of {', '.join(route['goals'])} in one-step mode.")
    billing = _upper(billing_event, "billing_event") or BOOST_BILLING_EVENT
    allowed_billing = _boost_billing_events(goal, destination)
    if billing not in allowed_billing:
        raise ValidationError(f"billing_event for optimization_goal {goal} must be {' or '.join(allowed_billing)}.")
    strategy = _upper(bid_strategy, "bid_strategy") or BOOST_BID_STRATEGY
    if strategy not in BOOST_BID_STRATEGIES:
        raise ValidationError(
            f"bid_strategy must be one of {', '.join(BOOST_BID_STRATEGIES)} in one-step mode; a minimum return on ad "
            "spend needs bid constraints, so use create_ad_set for it."
        )
    if strategy in _CAPPED_BID_STRATEGIES and bid_amount is None:
        raise ValidationError(f"bid_strategy {strategy} needs bid_amount (account currency, major units).")
    if strategy not in _CAPPED_BID_STRATEGIES and bid_amount is not None:
        raise ValidationError("bid_amount needs bid_strategy LOWEST_COST_WITH_BID_CAP or COST_CAP.")
    if bid_amount is not None:
        validate_positive_amount(bid_amount, field_name="bid_amount")
    if promoted_object is not None and not isinstance(promoted_object, dict):
        raise ValidationError("promoted_object must be an object, for example {\"pixel_id\": \"123\", \"custom_event_type\": \"PURCHASE\"}.")
    promoted = _boost_promoted_object(route["promoted"], resolved_objective, destination, goal, promoted_object)
    cta, built = _boost_call_to_action(route["cta"], resolved_objective, destination, call_to_action)
    categories: list[str] = []
    if special_ad_categories is not None:
        values = coerce_csv_string_list(special_ad_categories)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValidationError(f"special_ad_categories must list values from {', '.join(BOOST_SPECIAL_AD_CATEGORIES)}.")
        categories = list(dict.fromkeys(value.strip().upper() for value in values if value.strip()))
        unknown = [value for value in categories if value not in BOOST_SPECIAL_AD_CATEGORIES]
        if unknown:
            raise ValidationError(
                f"special_ad_categories has {len(unknown)} unknown value(s); use {', '.join(BOOST_SPECIAL_AD_CATEGORIES)}."
            )
        if "NONE" in categories and len(categories) > 1:
            raise ValidationError("special_ad_categories NONE cannot be combined with another category.")
    resolved_buying_type = _upper(buying_type, "buying_type")
    if resolved_buying_type == "RESERVED":
        raise ValidationError(
            "buying_type RESERVED (reach and frequency) needs a reserved prediction whose budget and schedule the ad set "
            "must match, which one-step mode cannot plan; create the campaign and ad set first and pass adset_id."
        )
    if resolved_buying_type not in (None, "AUCTION"):
        raise ValidationError("buying_type must be AUCTION in one-step mode.")
    now = _utc_now()
    start_sent, start_at = (None, now) if start_time is None else _boost_time(start_time, "start_time")
    if start_time is not None and start_at < now - timedelta(minutes=1):
        raise ValidationError("start_time is in the past; omit it to start when Meta creates the ad set.")
    if end_time is not None and duration_days is not None:
        raise ValidationError("Give end_time or duration_days, not both.")
    days: int | None = None
    if end_time is not None:
        end_sent, end_at = _boost_time(end_time, "end_time")
    else:
        days = BOOST_DEFAULT_DURATION_DAYS if duration_days is None else duration_days
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= BOOST_MAX_DURATION_DAYS:
            raise ValidationError(f"duration_days must be a whole number from 1 to {BOOST_MAX_DURATION_DAYS}.")
        end_at = (start_at + timedelta(days=days)).replace(microsecond=0)
        end_sent = end_at.isoformat()
    if end_at - start_at < BOOST_MIN_RUN:
        raise ValidationError("end_time must be at least 24 hours after the start (start_time, or now when it is omitted).")
    if end_at - start_at > timedelta(days=BOOST_MAX_DURATION_DAYS):
        raise ValidationError(f"The ad set can run for at most {BOOST_MAX_DURATION_DAYS} days.")
    given = {
        name for name, value in (
            ("objective", objective), ("destination_type", blank_to_none(destination_type) if isinstance(destination_type, str) else destination_type),
            ("optimization_goal", optimization_goal), ("billing_event", billing_event), ("bid_strategy", bid_strategy),
            ("special_ad_categories", special_ad_categories), ("buying_type", buying_type), ("start_time", start_time),
            ("duration_days", duration_days),
        ) if value is not None
    }
    return _BoostSettings(
        objective=resolved_objective, destination=destination, route=route, goal=goal, billing_event=billing,
        bid_strategy=strategy, bid_amount=bid_amount, budget_parameter=budget_parameter, budget=budget,
        promoted_object=promoted, call_to_action=cta, call_to_action_built=built, special_ad_categories=categories,
        buying_type=resolved_buying_type, start_sent=start_sent, end_sent=end_sent, end_computed=end_time is None,
        days=days, targeting=targeting, given=given,
    )


async def _page_for_instagram_account(client: Any, instagram_account_id: str) -> tuple[str, dict[str, Any]]:
    """Find the one Page whose instagram_business_account is this Instagram account.

    Reads the token user's Pages (me/accounts, the Page field instagram_business_account),
    page by page with a request cap. Only a complete read with exactly one match is used.
    """
    matches: list[str] = []
    cursors: set[str] = set()
    params: dict[str, Any] = {"limit": BOOST_PAGE_LOOKUP_PAGE_SIZE}
    pages_read = 0
    requests = 0
    complete = False
    stop_reason = f"the read stopped at this server's {BOOST_PAGE_LOOKUP_MAX_REQUESTS}-request limit"
    while requests < BOOST_PAGE_LOOKUP_MAX_REQUESTS:
        payload = await client.list_objects("me", "accounts", fields=["id", "instagram_business_account"], params=dict(params))
        requests += 1
        rows = payload.get("data") if isinstance(payload, dict) else None
        for page in rows if isinstance(rows, list) else []:
            if not isinstance(page, dict):
                continue
            pages_read += 1
            linked = page.get("instagram_business_account")
            linked_id = linked.get("id") if isinstance(linked, dict) else None
            if linked_id is not None and str(linked_id) == instagram_account_id and page.get("id"):
                matches.append(str(page["id"]))
        paging = payload.get("paging") if isinstance(payload, dict) else None
        if not isinstance(paging, dict) or not paging.get("next"):
            complete = True
            break
        cursor = (paging.get("cursors") or {}).get("after") if isinstance(paging.get("cursors"), dict) else None
        if not cursor:
            stop_reason = "Meta said more Pages exist but gave no cursor to read them"
            break
        if cursor in cursors:
            stop_reason = "Meta repeated a page cursor"
            break
        cursors.add(cursor)
        params["after"] = cursor
    matches = list(dict.fromkeys(matches))
    lookup = {"source": "me/accounts instagram_business_account", "pages_read": pages_read, "requests": requests, "complete": complete}
    if complete and len(matches) == 1:
        return matches[0], lookup
    if len(matches) > 1:
        reason = f"{len(matches)} Pages list this Instagram account"
    elif not complete:
        reason = f"the list of your Pages could not be read completely ({stop_reason})"
    else:
        reason = f"none of the {pages_read} Pages this token manages lists this Instagram account as its instagram_business_account"
    raise ValidationError(
        f"page_id is required: it could not be found from instagram_account_id because {reason}. Nothing was created; "
        "pass page_id, the Facebook Page linked to the Instagram account."
    )


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
    media_id: str | None,
    instagram_account_id: str,
    page_id: str | None,
    ad_name: str,
    creative_for: Any,
    settings: _BoostSettings,
    plan_only: bool,
) -> dict[str, Any]:
    """Create campaign, ad set, creative and ad, all paused, or return the plan."""
    client = client_factory()
    currency = await resolve_account_currency(client, account_id)
    budget_minor = to_minor_units(settings.budget, currency, field_name=settings.budget_parameter)
    bid_minor = (
        to_minor_units(settings.bid_amount, currency, field_name="bid_amount") if settings.bid_amount is not None else None
    )
    page_lookup: dict[str, Any] | None = None
    if page_id is None:
        try:
            page_id, page_lookup = await _page_for_instagram_account(client, instagram_account_id)
        except (MetaApiError, RateLimitError) as exc:
            exc.message = (
                f"Finding page_id from instagram_account_id failed, so nothing was created; pass page_id. Reason: {exc.message}"
            )
            raise
    creative_spec, partnership_defaults = creative_for(page_id, settings.call_to_action)
    campaign_level = settings.budget_parameter.startswith("campaign_")
    budget_field = "daily_budget" if settings.budget_parameter.endswith("daily_budget") else "lifetime_budget"
    campaign_data: dict[str, Any] = {
        "name": f"{ad_name} campaign",
        "objective": settings.objective,
        "status": "PAUSED",
        "special_ad_categories": settings.special_ad_categories,
    }
    if settings.buying_type is not None:
        campaign_data["buying_type"] = settings.buying_type
    if campaign_level:
        campaign_data[budget_field] = budget_minor
        campaign_data["bid_strategy"] = settings.bid_strategy
    else:
        campaign_data["is_adset_budget_sharing_enabled"] = False
    adset_data: dict[str, Any] = {"name": f"{ad_name} ad set", "campaign_id": "<campaign id>"}
    if not campaign_level:
        adset_data[budget_field] = budget_minor
    adset_data.update({
        "billing_event": settings.billing_event,
        "optimization_goal": settings.goal,
    })
    if not campaign_level:
        adset_data["bid_strategy"] = settings.bid_strategy
    if bid_minor is not None:
        adset_data["bid_amount"] = bid_minor
    adset_data["targeting"] = settings.targeting
    if settings.start_sent is not None:
        adset_data["start_time"] = settings.start_sent
    adset_data["end_time"] = settings.end_sent
    adset_data["status"] = "PAUSED"
    if settings.destination:
        adset_data["destination_type"] = settings.destination
    if settings.route["promoted"] == "page":
        adset_data["promoted_object"] = {"page_id": page_id, **(settings.promoted_object or {})}
    elif settings.promoted_object is not None:
        adset_data["promoted_object"] = settings.promoted_object
    creative_data = {"name": f"{ad_name} creative", **creative_spec}
    ad_data = {"name": ad_name, "adset_id": "<ad set id>", "creative": {"creative_id": "<creative id>"}, "status": "PAUSED"}
    given = settings.given
    defaults: dict[str, Any] = {
        "objective": settings.objective if "objective" not in given else None,
        "destination_type": settings.destination if "destination_type" not in given and settings.destination else None,
        "optimization_goal": settings.goal if "optimization_goal" not in given else None,
        "billing_event": settings.billing_event if "billing_event" not in given else None,
        "bid_strategy": (
            (f"{settings.bid_strategy} (on the campaign)" if campaign_level else settings.bid_strategy)
            if "bid_strategy" not in given else None
        ),
        "special_ad_categories": [] if "special_ad_categories" not in given else None,
        "buying_type": "AUCTION (Meta's default; not sent)" if "buying_type" not in given else None,
        "is_adset_budget_sharing_enabled": False if not campaign_level else None,
        "start_time": "when Meta creates the ad set (no start_time sent)" if settings.start_sent is None else None,
        "duration_days": settings.days if settings.end_computed and "duration_days" not in given else None,
        "end_time": settings.end_sent if settings.end_computed else None,
        "call_to_action": settings.call_to_action if settings.call_to_action_built else None,
        "page_id": f"{page_id} (the Page whose instagram_business_account is instagram_account_id)" if page_lookup else None,
        "status": "PAUSED for all four objects",
        "names": f"derived from '{ad_name}'",
        **partnership_defaults,
    }
    defaults = {key: value for key, value in defaults.items() if value is not None}
    budget_shown = {
        "level": "campaign" if campaign_level else "ad set",
        "type": "daily" if budget_field == "daily_budget" else "lifetime",
        "parameter": settings.budget_parameter,
        "major_units": settings.budget,
        "sent_minor_units": budget_minor,
    }
    plan: dict[str, Any] = {
        "currency": currency,
        "budget": budget_shown,
        "requests": [
            {"step": "campaign", "method": "POST", "path": f"{account_id}/campaigns", "data": campaign_data},
            {"step": "ad set", "method": "POST", "path": f"{account_id}/adsets", "data": adset_data},
            {"step": "creative", "method": "POST", "path": f"{account_id}/adcreatives", "data": _without_ad_code(creative_data)},
            {"step": "ad", "method": "POST", "path": f"{account_id}/ads", "data": ad_data},
        ],
        "defaults_applied": defaults,
    }
    if settings.budget_parameter == "daily_budget":
        plan["daily_budget"] = {"major_units": settings.budget, "sent_minor_units": budget_minor}
    if bid_minor is not None:
        plan["bid_amount"] = {"major_units": settings.bid_amount, "sent_minor_units": bid_minor}
    if page_lookup is not None:
        plan["page_lookup"] = page_lookup
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
        "budget": budget_shown,
        "currency": currency,
        "note": "All four objects are paused. Activate the campaign, ad set and ad with set_*_status when ready.",
    })
    if settings.budget_parameter == "daily_budget":
        response["daily_budget"] = plan["daily_budget"]
    if bid_minor is not None:
        response["bid_amount"] = plan["bid_amount"]
    if page_lookup is not None:
        response["page_lookup"] = page_lookup
    return response


_ONE_STEP_ONLY = (
    "daily_budget", "lifetime_budget", "campaign_daily_budget", "campaign_lifetime_budget", "duration_days",
    "targeting", "objective", "destination_type", "promoted_object", "optimization_goal", "billing_event",
    "bid_strategy", "bid_amount", "start_time", "end_time", "special_ad_categories", "buying_type",
)


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
    daily_budget: StrictCurrencyAmount | None = None,
    duration_days: _WholeNumber | None = None,
    targeting: dict[str, Any] | None = None,
    objective: str | None = None,
    plan_only: bool = False,
    url_tags: str | None = None,
    sponsor_page_id: GraphId | None = None,
    sponsor_instagram_account_id: GraphId | None = None,
    partnership_ad_format: _WholeNumber | None = None,
    destination_type: str | None = None,
    promoted_object: dict[str, Any] | None = None,
    partnership_ad_code: str | None = None,
    optimization_goal: str | None = None,
    billing_event: str | None = None,
    bid_strategy: str | None = None,
    bid_amount: StrictCurrencyAmount | None = None,
    lifetime_budget: StrictCurrencyAmount | None = None,
    campaign_daily_budget: StrictCurrencyAmount | None = None,
    campaign_lifetime_budget: StrictCurrencyAmount | None = None,
    start_time: _TimeInput | None = None,
    end_time: _TimeInput | None = None,
    special_ad_categories: StringList | None = None,
    buying_type: str | None = None,
) -> dict[str, Any]:
    """Boost an existing Instagram post, reel or story. With adset_id: creates a creative from it and a PAUSED ad in that ad set (page_id defaults to the ad set's promoted_object page; validate_only checks the ad with Meta). Without adset_id (one-step): creates a campaign, ad set, creative and ad, all PAUSED, with every default stated; plan_only returns the exact requests and creates nothing; if a later step fails the error names every object already created. One-step needs targeting and exactly one budget, never defaulted, in major units: daily_budget or lifetime_budget on the ad set, or campaign_daily_budget or campaign_lifetime_budget (bid_strategy then goes on the campaign). The run is duration_days (default 6) or end_time, from start_time or creation; times are ISO 8601 with a UTC offset or Unix seconds. objective and destination_type: OUTCOME_ENGAGEMENT (default; ON_POST, ON_VIDEO, INSTAGRAM_DIRECT, WHATSAPP, MESSENGER), OUTCOME_AWARENESS, OUTCOME_TRAFFIC (INSTAGRAM_PROFILE default, WEBSITE, the three messaging destinations, PHONE_CALL), OUTCOME_SALES (WEBSITE with promoted_object pixel_id and custom_event_type, or PHONE_CALL), OUTCOME_LEADS (WEBSITE with a pixel, ON_AD instant form, WHATSAPP, PHONE_CALL), OUTCOME_APP_PROMOTION (promoted_object application_id and object_store_url). Messaging destinations get their message button automatically; WEBSITE and app promotion need call_to_action value.link; ON_AD needs call_to_action value.lead_gen_form_id; PHONE_CALL needs CALL_NOW with a tel: link. optimization_goal, billing_event, bid_strategy (LOWEST_COST_WITHOUT_CAP default, LOWEST_COST_WITH_BID_CAP or COST_CAP with bid_amount), special_ad_categories and buying_type (AUCTION) are checked against Meta's documented combinations. Without page_id, the Page is found from the Instagram account when exactly one of your Pages lists it. Partnership Ad: sponsor_page_id and/or sponsor_instagram_account_id, optional partnership_ad_format 1, 2 or 3; for a creator's post, partnership_ad_code instead of instagram_media_id. Check list_instagram_media boost_eligible first."""
    try:
        return await _boost_instagram_post(
            account_id=account_id,
            instagram_media_id=instagram_media_id,
            instagram_account_id=instagram_account_id,
            adset_id=adset_id,
            name=name,
            call_to_action=call_to_action,
            validate_only=validate_only,
            page_id=page_id,
            duration_days=duration_days,
            targeting=targeting,
            objective=objective,
            plan_only=plan_only,
            url_tags=url_tags,
            sponsor_page_id=sponsor_page_id,
            sponsor_instagram_account_id=sponsor_instagram_account_id,
            partnership_ad_format=partnership_ad_format,
            destination_type=destination_type,
            promoted_object=promoted_object,
            partnership_ad_code=partnership_ad_code,
            optimization_goal=optimization_goal,
            billing_event=billing_event,
            bid_strategy=bid_strategy,
            bid_amount=bid_amount,
            budgets={
                "daily_budget": daily_budget,
                "lifetime_budget": lifetime_budget,
                "campaign_daily_budget": campaign_daily_budget,
                "campaign_lifetime_budget": campaign_lifetime_budget,
            },
            start_time=start_time,
            end_time=end_time,
            special_ad_categories=special_ad_categories,
            buying_type=buying_type,
        )
    except Exception as exc:
        # Meta's error text may repeat the creator's code; it never reaches the caller or a log.
        _scrub_error(exc, _secret_forms(partnership_ad_code), PARTNERSHIP_CODE_SHOWN)
        raise


async def _boost_instagram_post(
    *,
    account_id: str,
    instagram_media_id: str | None,
    instagram_account_id: str | None,
    adset_id: str | None,
    name: str | None,
    call_to_action: dict[str, Any] | None,
    validate_only: bool,
    page_id: str | None,
    duration_days: int | None,
    targeting: dict[str, Any] | None,
    objective: str | None,
    plan_only: bool,
    url_tags: str | None,
    sponsor_page_id: str | None,
    sponsor_instagram_account_id: str | None,
    partnership_ad_format: int | None,
    destination_type: str | None,
    promoted_object: dict[str, Any] | None,
    partnership_ad_code: str | None,
    optimization_goal: str | None,
    billing_event: str | None,
    bid_strategy: str | None,
    bid_amount: float | None,
    budgets: dict[str, float | None],
    start_time: Any,
    end_time: Any,
    special_ad_categories: Any,
    buying_type: str | None,
) -> dict[str, Any]:
    """The boost itself; boost_instagram_post wraps it so no error shows the Partnership Ad code."""
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
        values = {
            "duration_days": duration_days, "targeting": targeting, "objective": objective,
            "destination_type": blank_to_none(destination_type) if isinstance(destination_type, str) else destination_type,
            "promoted_object": promoted_object, "optimization_goal": optimization_goal, "billing_event": billing_event,
            "bid_strategy": bid_strategy, "bid_amount": bid_amount, "start_time": start_time, "end_time": end_time,
            "special_ad_categories": special_ad_categories, "buying_type": buying_type, **budgets,
        }
        extra = [label for label in _ONE_STEP_ONLY if values.get(label) is not None]
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

    def creative_for(page: str | None, cta: dict[str, Any] | None = call_to_action) -> tuple[dict[str, Any], dict[str, str]]:
        return _boost_creative_spec(
            page_id=page,
            instagram_account_id=ids["instagram_account_id"],
            media_id=media_id,
            ad_code=ad_code,
            call_to_action=cta,
            url_tags=url_tags,
            sponsor_page_id=sponsor_page_id,
            sponsor_instagram_account_id=sponsor_instagram_account_id,
            partnership_ad_format=partnership_ad_format,
        )

    if one_step:
        settings = _boost_settings(
            objective=objective,
            destination_type=destination_type,
            optimization_goal=optimization_goal,
            billing_event=billing_event,
            bid_strategy=bid_strategy,
            bid_amount=bid_amount,
            budgets=budgets,
            promoted_object=promoted_object,
            call_to_action=call_to_action,
            special_ad_categories=special_ad_categories,
            buying_type=buying_type,
            start_time=start_time,
            end_time=end_time,
            duration_days=duration_days,
            targeting=targeting,
        )
        return await _boost_one_step(
            get_graph_api_client,
            account_id=resolved_account_id,
            media_id=media_id,
            instagram_account_id=ids["instagram_account_id"],
            page_id=resolved_page_id,
            ad_name=ad_name,
            creative_for=creative_for,
            settings=settings,
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
