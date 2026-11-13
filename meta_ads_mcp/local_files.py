"""Read a local file for upload, only from the folder the operator named.

Tools that upload a local file send its contents to Meta. The model calling
those tools also reads text written by other people (ad comments, Page reviews,
competitor ads), and such text can carry instructions. A file path supplied by
the model must therefore never reach anything outside one folder the operator
chose, set with the META_UPLOAD_DIR environment variable. Reading is off until
that folder is configured.

Error messages here never quote a path or a folder: a mistaken value can itself
be private, and the upload folder's location is not the caller's business.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.errors import ValidationError


@dataclass(frozen=True, slots=True)
class LocalUpload:
    """A local file read for upload."""

    name: str
    content: bytes
    content_type: str


def _upload_folder() -> Path:
    configured = get_settings().upload_directory
    if not configured or not configured.strip():
        raise ValidationError(
            "Reading local files is off. Set the META_UPLOAD_DIR environment variable to the "
            "one folder this server may read uploads from, put the file there, and try again."
        )
    try:
        folder = Path(configured.strip()).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValidationError(
            "The folder named by META_UPLOAD_DIR does not exist or cannot be read."
        ) from None
    if not folder.is_dir():
        raise ValidationError("META_UPLOAD_DIR must name a folder.")
    return folder


def read_upload_file(
    file_path: object,
    *,
    allowed_types: Mapping[str, str],
    max_bytes: int,
    parameter: str = "file_path",
    too_large_hint: str = "",
) -> LocalUpload:
    """Return a file's name, bytes and content type, or refuse without quoting the path.

    The path may be absolute or relative to the upload folder. Links are followed
    first and the real location is what gets checked, so a link inside the folder
    cannot point the read somewhere else. The file type is judged from the real
    file's name. The read uses the same open file that was checked.
    """
    if not isinstance(file_path, str) or not file_path.strip() or "\x00" in file_path:
        raise ValidationError(f"{parameter} must be a non-blank local file path.")
    too_large = (
        f"{parameter} is larger than this server's {max_bytes}-byte limit for this upload."
        + (f" {too_large_hint}" if too_large_hint else "")
    )
    folder = _upload_folder()
    candidate = Path(file_path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = folder / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValidationError(f"{parameter} does not point to a file that can be read.") from None
    if not resolved.is_relative_to(folder):
        raise ValidationError(
            f"{parameter} must be inside the upload folder set by META_UPLOAD_DIR."
        )
    content_type = allowed_types.get(resolved.suffix.lower()) if resolved.stem else None
    if content_type is None:
        raise ValidationError(f"{parameter} must end in one of: {', '.join(allowed_types)}.")
    descriptor = None
    try:
        # O_NONBLOCK so opening a named pipe cannot hang; O_NOFOLLOW so the final
        # name cannot have been swapped for a link since it was resolved.
        descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValidationError(f"{parameter} does not point to a file that can be read.")
        if details.st_size == 0:
            raise ValidationError(f"{parameter} points to an empty file.")
        if details.st_size > max_bytes:
            raise ValidationError(too_large)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            content = handle.read(max_bytes + 1)
    except OSError:
        raise ValidationError(f"{parameter} does not point to a file that can be read.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(content) > max_bytes:
        raise ValidationError(too_large)
    return LocalUpload(name=resolved.name, content=content, content_type=content_type)
