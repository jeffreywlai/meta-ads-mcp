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


# Folders on the way to the file are opened only to look up the next name, so
# Linux's O_PATH or macOS's O_SEARCH is used where available: they need search
# permission on a folder, as an ordinary open by path does, not read permission.
# A platform without descriptor-relative opens or these flags (Windows) cannot
# make the guarantee, so reading is refused there rather than done unsafely.
_SAFE_OPEN_SUPPORTED = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_NONBLOCK")
)
_FOLDER_FLAGS = (
    (getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0) or os.O_RDONLY)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
# O_NONBLOCK so opening a named pipe cannot hang.
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _open_without_links(resolved: Path) -> int:
    """Open an already-resolved absolute path one name at a time, refusing every link.

    The path was resolved and checked by name, and something could replace any
    folder on it with a link before the open. Opening each name relative to the
    folder opened before it, with O_NOFOLLOW, means a link anywhere on the path
    (the upload folder and its parents included) makes the open fail instead of
    being followed. The resolved path itself contains no links, so a legitimate
    read never meets one. Raises OSError; every folder descriptor is closed.
    """
    if not _SAFE_OPEN_SUPPORTED:
        raise OSError("descriptor-relative opens are not available on this platform")
    anchor, *names = resolved.parts
    if not names:
        raise IsADirectoryError(resolved)
    current = os.open(anchor, _FOLDER_FLAGS)
    try:
        for name in names[:-1]:
            following = os.open(name, _FOLDER_FLAGS, dir_fd=current)
            previous, current = current, following
            os.close(previous)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise NotADirectoryError(name)
        return os.open(names[-1], _FILE_FLAGS, dir_fd=current)
    finally:
        os.close(current)


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
        descriptor = _open_without_links(resolved)
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
