"""Bounded server-side storage for oversized MCP tool responses."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

import mcp.types as mt
import pydantic_core
from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from meta_ads_mcp.errors import NotFoundError, ValidationError


DEFAULT_ARTIFACT_TTL_SECONDS = 86_400
DEFAULT_ARTIFACT_MAX_FILES = 100
DEFAULT_ARTIFACT_MAX_BYTES = 1_000_000_000
DEFAULT_ARTIFACT_CHUNK_BYTES = 44_000
MAX_ARTIFACT_CHUNK_BYTES = 44_000
MAX_VERIFIED_ARTIFACT_CACHE_ENTRIES = 64
EXPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")
ARTIFACT_PREFIX = "overflow-"
ARTIFACT_FILE_PATTERN = re.compile(
    rf"^{ARTIFACT_PREFIX}([A-Za-z0-9_-]{{32}})\.json$"
)
MANIFEST_SUFFIX = ".meta"
MANIFEST_FILE_PATTERN = re.compile(
    rf"^{ARTIFACT_PREFIX}([A-Za-z0-9_-]{{32}})"
    rf"{re.escape(MANIFEST_SUFFIX)}$"
)
TEMP_MANIFEST_FILE_PATTERN = re.compile(
    rf"^{ARTIFACT_PREFIX}[A-Za-z0-9_-]{{32}}{re.escape(MANIFEST_SUFFIX)}\.[0-9a-f]{{16}}\.tmp$"
)
MAX_MANIFEST_BYTES = 65_536
MAX_RECENT_ARTIFACT_OFFSETS = 2_048
DEFAULT_ARTIFACT_ITEM_LIMIT = 100
MAX_ARTIFACT_ITEM_LIMIT = 500
DEFAULT_ARTIFACT_RESULT_BYTES = 63_000
LOCK_FILE_NAME = ".overflow.lock"
DIRECTORY_FD_SUPPORTED = all(
    function in os.supports_dir_fd
    for function in (os.open, os.stat, os.unlink)
)

ArtifactIdentity = tuple[int, int, int, int, int]
_PROXY_CALL_ACTIVE: ContextVar[bool] = ContextVar("overflow_proxy_call", default=False)


def _has_redundant_json_text(result: ToolResult) -> bool:
    """Recognize only unannotated text that exactly duplicates structured JSON."""
    if result.structured_content is None or len(result.content) != 1:
        return False
    content = result.content[0].model_dump(mode="json", by_alias=True)
    if content.get("type") != "text" or any(
        value is not None
        for key, value in content.items()
        if key not in {"type", "text"}
    ):
        return False
    try:
        decoded = json.loads(content["text"])
    except (TypeError, ValueError):
        return False
    # Canonical JSON keeps booleans distinct from numbers (True != 1).
    return json.dumps(decoded, sort_keys=True) == json.dumps(
        result.structured_content, sort_keys=True
    )


def _serialized_mcp_result(result: ToolResult) -> bytes:
    """Measure the MCP result envelope, including content, metadata and error status."""
    wire_result = mt.CallToolResult(
        content=result.content,
        structuredContent=result.structured_content,
        _meta=result.meta,
        isError=result.is_error,
    )
    return pydantic_core.to_json(
        wire_result, by_alias=True, exclude_none=True, fallback=str
    )


def _collection(payload: Any) -> tuple[list[str], list[Any]] | None:
    """Find standard collections, including async reports' rows.items envelope."""
    if not isinstance(payload, dict):
        return None
    for key in ("items", "rows"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [key], candidate
        if isinstance(candidate, dict):
            for nested_key in ("items", "rows"):
                nested = candidate.get(nested_key)
                if isinstance(nested, list):
                    return [key, nested_key], nested
    return None


def compact_overflow_chunk_result(
    chunk: dict[str, Any],
) -> ToolResult:
    """Return structured data plus the MCP compatibility JSON text copy."""
    compatibility_text = pydantic_core.to_json(
        chunk,
        fallback=str,
    ).decode()
    return ToolResult(
        content=[TextContent(type="text", text=compatibility_text)],
        structured_content=chunk,
    )


@contextmanager
def _owned_fdopen(descriptor: int, mode: str) -> Iterator[Any]:
    """Transfer an fd to a file object without leaking when fdopen fails."""
    try:
        file_object = os.fdopen(descriptor, mode)
    except Exception:
        os.close(descriptor)
        raise
    with file_object:
        yield file_object


class OverflowArtifactStore:
    """Persist complete tool results behind opaque, remotely retrievable ids."""

    def __init__(
        self,
        export_directory: str | Path | None = None,
        *,
        ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
        max_files: int = DEFAULT_ARTIFACT_MAX_FILES,
        max_total_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES,
    ) -> None:
        if ttl_seconds < 1 or max_files < 1 or max_total_bytes < 1:
            raise ValueError("Overflow artifact retention settings must be positive.")
        self.export_directory = (
            Path(export_directory).expanduser()
            if export_directory
            else Path(tempfile.gettempdir()) / "meta-ads-mcp-exports"
        )
        self.ttl_seconds = ttl_seconds
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self._thread_lock = threading.RLock()
        self._verified_artifacts: OrderedDict[
            str,
            tuple[ArtifactIdentity, str],
        ] = OrderedDict()

    def _ensure_directory(self) -> None:
        """Create the artifact directory without following a final-component link."""
        if self.export_directory.is_symlink():
            raise OSError("META_EXPORT_DIR must not be a symbolic link.")
        self.export_directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _validate_export_id(export_id: str) -> str:
        """Reject paths and malformed artifact identifiers."""
        if not isinstance(export_id, str):
            raise ValidationError("export_id is invalid.")
        normalized = export_id.strip()
        if not EXPORT_ID_PATTERN.fullmatch(normalized):
            raise ValidationError("export_id is invalid.")
        return normalized

    def _artifact_path(self, export_id: str) -> Path:
        """Resolve a validated opaque id inside the configured directory."""
        return self.export_directory / self._artifact_name(
            self._validate_export_id(export_id)
        )

    @staticmethod
    def _artifact_name(export_id: str) -> str:
        """Return the artifact filename for an already validated id."""
        return f"{ARTIFACT_PREFIX}{export_id}.json"

    @staticmethod
    def _manifest_name(export_id: str) -> str:
        """Return the integrity-manifest filename for an opaque artifact id."""
        return f"{ARTIFACT_PREFIX}{export_id}{MANIFEST_SUFFIX}"

    @contextmanager
    def _locked_directory(self) -> Iterator[int]:
        """Lock the artifact directory across threads and cooperating processes."""
        if (
            not DIRECTORY_FD_SUPPORTED
            or fcntl is None
            or not hasattr(os, "O_NOFOLLOW")
        ):
            raise OSError(
                "Secure overflow artifact storage is unavailable on this platform."
            )
        with self._thread_lock:
            self._ensure_directory()
            directory_fd: int | None = None
            lock_fd: int | None = None
            try:
                directory_flags = (
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
                )
                directory_fd = os.open(self.export_directory, directory_flags)
                directory_stat = os.fstat(directory_fd)
                if not stat.S_ISDIR(directory_stat.st_mode):
                    raise OSError("META_EXPORT_DIR must be a directory.")
                os.fchmod(directory_fd, 0o700)
                lock_fd = os.open(
                    LOCK_FILE_NAME,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_NOFOLLOW
                    | getattr(os, "O_NONBLOCK", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise OSError("Overflow artifact lock must be a regular file.")
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield directory_fd
            finally:
                try:
                    if lock_fd is not None:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                        finally:
                            os.close(lock_fd)
                finally:
                    if directory_fd is not None:
                        os.close(directory_fd)

    def _stat_name(self, name: str, directory_fd: int) -> os.stat_result:
        """Stat a directory-relative name without following symlinks."""
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

    def _unlink_name(self, name: str, directory_fd: int) -> None:
        """Unlink a directory-relative artifact name."""
        os.unlink(name, dir_fd=directory_fd)

    def _open_name(
        self,
        name: str,
        flags: int,
        directory_fd: int,
        mode: int = 0o600,
    ) -> int:
        """Open a directory-relative name without following its final symlink."""
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=directory_fd)

    def _artifact_records(
        self,
        directory_fd: int,
    ) -> list[tuple[float, int, str]]:
        """Return regular artifacts using their manifest as an access lease."""
        entries = os.listdir(directory_fd)
        artifacts: list[tuple[float, int, str]] = []
        for name in entries:
            artifact_match = ARTIFACT_FILE_PATTERN.fullmatch(name)
            if artifact_match is None:
                continue
            try:
                file_stat = self._stat_name(name, directory_fd)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(file_stat.st_mode):
                lease_modified_at = file_stat.st_mtime
                try:
                    manifest_stat = self._stat_name(
                        self._manifest_name(artifact_match.group(1)),
                        directory_fd,
                    )
                except FileNotFoundError:
                    manifest_stat = None
                if (
                    manifest_stat is not None
                    and stat.S_ISREG(manifest_stat.st_mode)
                ):
                    lease_modified_at = max(
                        lease_modified_at,
                        manifest_stat.st_mtime,
                    )
                artifacts.append(
                    (lease_modified_at, file_stat.st_size, name)
                )
        return artifacts

    def _unlink_artifact_files(self, name: str, directory_fd: int) -> None:
        """Remove an artifact and its integrity manifest when present."""
        names = [name]
        if artifact_match := ARTIFACT_FILE_PATTERN.fullmatch(name):
            self._verified_artifacts.pop(artifact_match.group(1), None)
            names.append(self._manifest_name(artifact_match.group(1)))
        for candidate in names:
            try:
                candidate_stat = self._stat_name(candidate, directory_fd)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(candidate_stat.st_mode):
                continue
            try:
                self._unlink_name(candidate, directory_fd)
            except FileNotFoundError:
                pass

    def _prune_verified_artifacts(self, directory_fd: int) -> None:
        """Forget cached identities whose artifact pair is no longer active."""
        for export_id in tuple(self._verified_artifacts):
            try:
                artifact_stat = self._stat_name(
                    self._artifact_name(export_id),
                    directory_fd,
                )
                manifest_stat = self._stat_name(
                    self._manifest_name(export_id),
                    directory_fd,
                )
            except FileNotFoundError:
                self._verified_artifacts.pop(export_id, None)
                continue
            if not (
                stat.S_ISREG(artifact_stat.st_mode)
                and stat.S_ISREG(manifest_stat.st_mode)
            ):
                self._verified_artifacts.pop(export_id, None)

    def _remember_verified_artifact(
        self,
        *,
        export_id: str,
        identity: ArtifactIdentity,
        expected_digest: str,
    ) -> None:
        """Cache a verified identity within a retention-sized LRU bound."""
        self._verified_artifacts[export_id] = (identity, expected_digest)
        self._verified_artifacts.move_to_end(export_id)
        cache_limit = min(
            self.max_files,
            MAX_VERIFIED_ARTIFACT_CACHE_ENTRIES,
        )
        while len(self._verified_artifacts) > cache_limit:
            self._verified_artifacts.popitem(last=False)

    def _cleanup_orphans_unlocked(
        self,
        directory_fd: int,
    ) -> tuple[int, int]:
        """Remove reserved-name manifests without a regular paired artifact."""
        removed_files = 0
        removed_bytes = 0
        entries = os.listdir(directory_fd)
        for name in entries:
            if TEMP_MANIFEST_FILE_PATTERN.fullmatch(name):
                try:
                    temporary_stat = self._stat_name(name, directory_fd)
                    if stat.S_ISREG(temporary_stat.st_mode):
                        self._unlink_name(name, directory_fd)
                except FileNotFoundError:
                    pass
                continue
            manifest_match = MANIFEST_FILE_PATTERN.fullmatch(name)
            if manifest_match is None:
                continue
            artifact_name = self._artifact_name(manifest_match.group(1))
            try:
                artifact_stat = self._stat_name(artifact_name, directory_fd)
            except FileNotFoundError:
                artifact_stat = None
            if artifact_stat is not None and stat.S_ISREG(artifact_stat.st_mode):
                continue
            try:
                manifest_stat = self._stat_name(name, directory_fd)
                self._unlink_name(name, directory_fd)
            except FileNotFoundError:
                continue
            except OSError:
                manifest_stat = None
            if manifest_stat is not None:
                removed_files += 1
                removed_bytes += (
                    manifest_stat.st_size
                    if stat.S_ISREG(manifest_stat.st_mode)
                    else 0
                )
            if artifact_stat is not None:
                try:
                    self._unlink_name(artifact_name, directory_fd)
                except OSError:
                    pass
        return removed_files, removed_bytes

    def _cleanup_unlocked(
        self,
        *,
        directory_fd: int,
        now: float | None = None,
        protect_name: str | None = None,
    ) -> dict[str, int]:
        """Apply retention limits while the directory lock is held."""
        current_time = time.time() if now is None else now
        removed_files, removed_bytes = self._cleanup_orphans_unlocked(
            directory_fd
        )
        for modified_at, file_size, name in self._artifact_records(directory_fd):
            if (
                name != protect_name
                and current_time - modified_at > self.ttl_seconds
            ):
                self._unlink_artifact_files(name, directory_fd)
                removed_files += 1
                removed_bytes += file_size

        remaining = self._artifact_records(directory_fd)
        remaining.sort(
            key=lambda artifact: (artifact[2] == protect_name, artifact[0]),
            reverse=True,
        )

        kept_files = 0
        kept_bytes = 0
        for _mtime, file_size, name in remaining:
            keep = name == protect_name or (
                kept_files < self.max_files
                and kept_bytes + file_size <= self.max_total_bytes
            )
            if keep:
                kept_files += 1
                kept_bytes += file_size
                continue
            self._unlink_artifact_files(name, directory_fd)
            removed_files += 1
            removed_bytes += file_size

        self._prune_verified_artifacts(directory_fd)
        return {"removed_files": removed_files, "removed_bytes": removed_bytes}

    def cleanup(self, *, now: float | None = None) -> dict[str, int]:
        """Delete expired artifacts, then enforce count and total-byte quotas."""
        with self._locked_directory() as directory_fd:
            return self._cleanup_unlocked(
                directory_fd=directory_fd,
                now=now,
            )

    def create(self, result: ToolResult, *, tool_name: str) -> tuple[str, int]:
        """Store compact JSON, omitting only redundant unannotated compatibility text."""
        created_at = datetime.now(timezone.utc).isoformat()
        archived_result = result.model_dump(mode="json", by_alias=True)
        if _has_redundant_json_text(result):
            archived_result["content"] = []
        artifact_payload = {
            "schema_version": 2,
            "tool_name": tool_name,
            "created_at": created_at,
            "tool_result": archived_result,
        }
        artifact_bytes = json.dumps(
            artifact_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        manifest_bytes = json.dumps(
            {
                "schema_version": 1,
                "size": len(artifact_bytes),
                "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(artifact_bytes) > self.max_total_bytes:
            raise ValueError("The overflow artifact exceeds META_EXPORT_MAX_BYTES.")
        with self._locked_directory() as directory_fd:
            self._cleanup_unlocked(directory_fd=directory_fd)
            while True:
                export_id = secrets.token_urlsafe(24)
                artifact_name = self._artifact_name(export_id)
                try:
                    descriptor = self._open_name(
                        artifact_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        directory_fd,
                    )
                except FileExistsError:
                    continue
                break

            manifest_name = self._manifest_name(export_id)
            try:
                with _owned_fdopen(descriptor, "wb") as artifact_file:
                    os.fchmod(artifact_file.fileno(), 0o600)
                    artifact_file.write(artifact_bytes)
                manifest_descriptor = self._open_name(
                    manifest_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    directory_fd,
                )
                with _owned_fdopen(manifest_descriptor, "wb") as manifest_file:
                    os.fchmod(manifest_file.fileno(), 0o600)
                    manifest_file.write(manifest_bytes)
                self._cleanup_unlocked(
                    directory_fd=directory_fd,
                    protect_name=artifact_name,
                )
            except Exception:
                self._unlink_artifact_files(artifact_name, directory_fd)
                raise
            return export_id, len(artifact_bytes)

    def _read_manifest(
        self,
        *,
        export_id: str,
        directory_fd: int,
    ) -> dict[str, Any]:
        """Read and validate a bounded integrity manifest without following links."""
        try:
            descriptor = self._open_name(
                self._manifest_name(export_id),
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
                directory_fd,
            )
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.EISDIR, errno.ENXIO}:
                raise ValidationError(
                    "Overflow artifact failed integrity validation."
                ) from exc
            raise
        try:
            manifest_stat = os.fstat(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        if not stat.S_ISREG(manifest_stat.st_mode):
            os.close(descriptor)
            raise ValidationError(
                "Overflow artifact failed integrity validation."
            )
        with _owned_fdopen(descriptor, "rb") as manifest_file:
            manifest_bytes = manifest_file.read(MAX_MANIFEST_BYTES + 1)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValidationError("Overflow artifact failed integrity validation.")
        try:
            manifest = json.loads(manifest_bytes)
            expected_size = manifest["size"]
            expected_digest = manifest["sha256"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(
                "Overflow artifact failed integrity validation."
            ) from exc
        if (
            manifest.get("schema_version") != 1
            or type(expected_size) is not int
            or expected_size < 0
            or not isinstance(expected_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        ):
            raise ValidationError("Overflow artifact failed integrity validation.")
        for key in ("byte_offsets", "item_offsets"):
            offsets = manifest.get(key, [])
            if (
                not isinstance(offsets, list)
                or len(offsets) > MAX_RECENT_ARTIFACT_OFFSETS
                or any(type(value) is not int or value < 0 for value in offsets)
            ):
                raise ValidationError("Overflow artifact failed integrity validation.")
        return manifest

    def _record_offset(
        self,
        *,
        export_id: str,
        directory_fd: int,
        manifest: dict[str, Any],
        key: str,
        offset: int,
    ) -> None:
        """Atomically persist recent continuations under the shared directory lock."""
        offsets = manifest.get(key, [])
        if offset in offsets or offset == 0:
            return
        manifest[key] = [*offsets, offset][-MAX_RECENT_ARTIFACT_OFFSETS:]
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("ascii")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValidationError("Overflow artifact continuation metadata is too large.")
        manifest_name = self._manifest_name(export_id)
        temporary_name = f"{manifest_name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = self._open_name(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                directory_fd,
            )
            with _owned_fdopen(descriptor, "wb") as manifest_file:
                os.fchmod(manifest_file.fileno(), 0o600)
                manifest_file.write(manifest_bytes)
                manifest_file.flush()
                os.fsync(manifest_file.fileno())
            os.replace(
                temporary_name,
                manifest_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        finally:
            try:
                self._unlink_name(temporary_name, directory_fd)
            except FileNotFoundError:
                pass

    def _refresh_manifest_lease(
        self,
        *,
        export_id: str,
        directory_fd: int,
    ) -> None:
        """Refresh retention without mutating the verified artifact identity."""
        try:
            descriptor = self._open_name(
                self._manifest_name(export_id),
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
                directory_fd,
            )
        except OSError as exc:
            if exc.errno in {
                errno.ENOENT,
                errno.ELOOP,
                errno.EISDIR,
                errno.ENXIO,
            }:
                raise ValidationError(
                    "Overflow artifact failed integrity validation."
                ) from exc
            raise
        try:
            manifest_stat = os.fstat(descriptor)
            if not stat.S_ISREG(manifest_stat.st_mode):
                raise ValidationError(
                    "Overflow artifact failed integrity validation."
                )
            os.utime(descriptor, None)
        finally:
            os.close(descriptor)

    @staticmethod
    def _artifact_identity(file_stat: os.stat_result) -> ArtifactIdentity:
        """Return metadata that changes whenever artifact bytes are replaced."""
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )

    def _verify_open_artifact(
        self,
        *,
        export_id: str,
        artifact_file: Any,
        file_stat: os.stat_result,
        expected_digest: str,
    ) -> ArtifactIdentity:
        """Verify one stable file version once before returning any chunk."""
        identity = self._artifact_identity(file_stat)
        if self._verified_artifacts.get(export_id) == (
            identity,
            expected_digest,
        ):
            self._verified_artifacts.move_to_end(export_id)
            return identity
        artifact_file.seek(0)
        digest = hashlib.sha256()
        while digest_chunk := artifact_file.read(64 * 1024):
            digest.update(digest_chunk)
        verified_stat = os.fstat(artifact_file.fileno())
        verified_identity = self._artifact_identity(verified_stat)
        if (
            verified_identity != identity
            or not secrets.compare_digest(
                digest.hexdigest(),
                expected_digest,
            )
        ):
            self._verified_artifacts.pop(export_id, None)
            raise ValidationError(
                "Overflow artifact failed integrity validation."
            )
        self._remember_verified_artifact(
            export_id=export_id,
            identity=verified_identity,
            expected_digest=expected_digest,
        )
        return verified_identity

    @staticmethod
    def _item_page(
        artifact: dict[str, Any],
        *,
        export_id: str,
        collection: tuple[list[str], list[Any]],
        item_offset: int,
        item_limit: int,
        max_result_bytes: int,
    ) -> dict[str, Any]:
        """Return a usable, byte-bounded item page with its original context."""
        item_path, items = collection
        if item_offset > len(items):
            raise ValidationError("item_offset exceeds the collection size.")
        context = dict(artifact)
        context["tool_result"] = dict(artifact["tool_result"])
        original = artifact["tool_result"]["structured_content"]
        copied = dict(original)
        context["tool_result"]["structured_content"] = copied
        for key in item_path[:-1]:
            original = original[key]
            copied[key] = dict(original)
            copied = copied[key]
        copied[item_path[-1]] = []
        page: dict[str, Any] = {
            "export_id": export_id,
            "format": "application/json",
            "mode": "items",
            "item_path": item_path,
            "item_offset": item_offset,
            "next_item_offset": item_offset,
            "total_items": len(items),
            "complete": item_offset == len(items),
            "items": [],
            "context_included": True,
            "context": context,
        }

        def fits() -> bool:
            return len(_serialized_mcp_result(compact_overflow_chunk_result(page))) <= max_result_bytes

        def omit_context() -> None:
            del page["context"]
            page["context_included"] = False
            page["context_note"] = "Read byte mode with offset=0 for the full response context."

        if not fits():
            omit_context()
        count = min(item_limit, len(items) - item_offset)
        if count and page["context_included"]:
            page["items"] = items[item_offset:item_offset + 1]
            page["next_item_offset"] = item_offset + 1
            page["complete"] = item_offset + 1 == len(items)
            if not fits():
                omit_context()
        lower, upper = 0, count
        while lower < upper:
            trial = (lower + upper + 1) // 2
            page["items"] = items[item_offset:item_offset + trial]
            page["next_item_offset"] = item_offset + trial
            page["complete"] = item_offset + trial == len(items)
            if fits():
                lower = trial
            else:
                upper = trial - 1
        page["items"] = items[item_offset:item_offset + lower]
        page["next_item_offset"] = item_offset + lower
        page["complete"] = item_offset + lower == len(items)
        if lower == 0 and item_offset < len(items):
            page["oversized_item"] = item_offset
            page["next_step"] = (
                "This item exceeds the item-page byte limit. Read the complete original "
                "JSON in byte mode with offset=0, following next_offset. No item was skipped."
            )
        if not fits():
            raise ValidationError("The requested result byte limit is too small for an item page.")
        return page

    def read(
        self,
        export_id: str,
        *,
        offset: int = 0,
        max_bytes: int = DEFAULT_ARTIFACT_CHUNK_BYTES,
        item_offset: int | None = None,
        item_limit: int = DEFAULT_ARTIFACT_ITEM_LIMIT,
        prefer_items: bool = False,
        max_result_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Read verified byte chunks or parsed item pages at issued continuations."""
        if type(offset) is not int or offset < 0:
            raise ValidationError("offset must be at least 0.")
        if type(max_bytes) is not int or max_bytes < 1 or max_bytes > MAX_ARTIFACT_CHUNK_BYTES:
            raise ValidationError(
                f"max_bytes must be between 1 and {MAX_ARTIFACT_CHUNK_BYTES}."
            )
        if item_offset is not None and (type(item_offset) is not int or item_offset < 0):
            raise ValidationError("item_offset must be at least 0.")
        if type(item_limit) is not int or not 1 <= item_limit <= MAX_ARTIFACT_ITEM_LIMIT:
            raise ValidationError(f"item_limit must be between 1 and {MAX_ARTIFACT_ITEM_LIMIT}.")
        if item_offset is not None and offset != 0:
            raise ValidationError("Use offset for bytes or item_offset for items, not both.")
        normalized_export_id = self._validate_export_id(export_id)
        artifact_name = self._artifact_name(normalized_export_id)
        with self._locked_directory() as directory_fd:
            self._cleanup_unlocked(directory_fd=directory_fd)
            try:
                path_stat = self._stat_name(artifact_name, directory_fd)
            except FileNotFoundError as exc:
                self._verified_artifacts.pop(normalized_export_id, None)
                raise NotFoundError(
                    "Overflow artifact was not found or has expired."
                ) from exc
            if not stat.S_ISREG(path_stat.st_mode):
                self._verified_artifacts.pop(normalized_export_id, None)
                raise NotFoundError(
                    "Overflow artifact was not found or has expired."
                )
            try:
                manifest = self._read_manifest(
                    export_id=normalized_export_id,
                    directory_fd=directory_fd,
                )
                expected_size, expected_digest = manifest["size"], manifest["sha256"]
            except Exception:
                self._verified_artifacts.pop(normalized_export_id, None)
                raise
            try:
                descriptor = self._open_name(
                    artifact_name,
                    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
                    directory_fd,
                )
            except OSError as exc:
                if exc.errno in {
                    errno.ENOENT,
                    errno.ELOOP,
                    errno.EISDIR,
                    errno.ENXIO,
                }:
                    self._verified_artifacts.pop(
                        normalized_export_id,
                        None,
                    )
                    raise NotFoundError(
                        "Overflow artifact was not found or has expired."
                    ) from exc
                self._verified_artifacts.pop(normalized_export_id, None)
                raise
            try:
                file_stat = os.fstat(descriptor)
            except Exception:
                os.close(descriptor)
                self._verified_artifacts.pop(normalized_export_id, None)
                raise
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(descriptor)
                self._verified_artifacts.pop(normalized_export_id, None)
                raise NotFoundError(
                    "Overflow artifact was not found or has expired."
                )
            with _owned_fdopen(descriptor, "rb") as artifact_file:
                total_bytes = file_stat.st_size
                if total_bytes != expected_size:
                    self._verified_artifacts.pop(normalized_export_id, None)
                    raise ValidationError(
                        "Overflow artifact failed integrity validation."
                    )
                if offset > total_bytes:
                    raise ValidationError("offset exceeds the artifact size.")
                verified_identity = self._verify_open_artifact(
                    export_id=normalized_export_id,
                    artifact_file=artifact_file,
                    file_stat=file_stat,
                    expected_digest=expected_digest,
                )
                collection = None
                artifact = None
                if item_offset is not None or prefer_items:
                    artifact_file.seek(0)
                    try:
                        artifact = json.load(artifact_file)
                        collection = _collection(artifact["tool_result"].get("structured_content"))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValidationError("Overflow artifact failed integrity validation.") from exc
                if collection is not None:
                    item_offset = 0 if item_offset is None else item_offset
                    if item_offset != 0 and item_offset not in manifest.get("item_offsets", []):
                        raise ValidationError(
                            "item_offset must be 0 or a recently issued next_item_offset; "
                            "restart at 0 if its continuation history has expired."
                        )
                    chunk = self._item_page(
                        artifact,
                        export_id=normalized_export_id,
                        collection=collection,
                        item_offset=item_offset,
                        item_limit=item_limit,
                        max_result_bytes=max_result_bytes or DEFAULT_ARTIFACT_RESULT_BYTES,
                    )
                    continuation_key, next_offset = "item_offsets", chunk["next_item_offset"]
                else:
                    if item_offset is not None:
                        raise ValidationError("This artifact has no items/rows collection; use byte mode with offset=0.")
                    if offset != 0 and offset not in manifest.get("byte_offsets", []):
                        raise ValidationError(
                            "offset must be 0 or a recently issued next_offset; "
                            "restart at 0 if its continuation history has expired."
                        )
                    artifact_file.seek(offset)
                    chunk_bytes = artifact_file.read(max_bytes)
                    try:
                        chunk_data = chunk_bytes.decode("ascii")
                    except UnicodeDecodeError as exc:
                        self._verified_artifacts.pop(normalized_export_id, None)
                        raise ValidationError("Overflow artifact failed integrity validation.") from exc
                    chunk = {
                        "export_id": normalized_export_id,
                        "format": "application/json",
                        "encoding": "utf-8",
                        "offset": offset,
                        "next_offset": offset + len(chunk_bytes),
                        "total_bytes": total_bytes,
                        "complete": offset + len(chunk_bytes) >= total_bytes,
                        "data": chunk_data,
                    }
                    if max_result_bytes is not None:
                        lower, upper = 0, len(chunk_data)
                        while lower < upper:
                            trial = (lower + upper + 1) // 2
                            chunk["data"] = chunk_data[:trial]
                            chunk["next_offset"] = offset + trial
                            chunk["complete"] = offset + trial >= total_bytes
                            if len(_serialized_mcp_result(compact_overflow_chunk_result(chunk))) <= max_result_bytes:
                                lower = trial
                            else:
                                upper = trial - 1
                        chunk["data"] = chunk_data[:lower]
                        chunk["next_offset"] = offset + lower
                        chunk["complete"] = offset + lower >= total_bytes
                        if lower == 0 and offset < total_bytes:
                            raise ValidationError("The requested result byte limit is too small for a byte chunk.")
                    continuation_key, next_offset = "byte_offsets", chunk["next_offset"]
                if self._artifact_identity(
                    os.fstat(artifact_file.fileno())
                ) != verified_identity:
                    self._verified_artifacts.pop(normalized_export_id, None)
                    raise ValidationError(
                        "Overflow artifact failed integrity validation."
                    )
                try:
                    self._record_offset(
                        export_id=normalized_export_id,
                        directory_fd=directory_fd,
                        manifest=manifest,
                        key=continuation_key,
                        offset=next_offset,
                    )
                    self._refresh_manifest_lease(
                        export_id=normalized_export_id,
                        directory_fd=directory_fd,
                    )
                except Exception:
                    self._verified_artifacts.pop(normalized_export_id, None)
                    raise
        return chunk

    def delete(self, export_id: str) -> dict[str, Any]:
        """Delete one overflow artifact by opaque id."""
        normalized_export_id = self._validate_export_id(export_id)
        artifact_name = self._artifact_name(normalized_export_id)
        with self._locked_directory() as directory_fd:
            try:
                file_stat = self._stat_name(artifact_name, directory_fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise NotFoundError(
                        "Overflow artifact was not found or has expired."
                    )
                self._unlink_artifact_files(artifact_name, directory_fd)
            except FileNotFoundError as exc:
                raise NotFoundError(
                    "Overflow artifact was not found or has expired."
                ) from exc
        return {"ok": True, "export_id": normalized_export_id, "deleted": True}


class ArchivedResponseLimitingMiddleware(ResponseLimitingMiddleware):
    """Archive oversized results and return a remotely retrievable export id."""

    def __init__(
        self,
        *,
        max_size: int,
        truncation_suffix: str,
        artifact_store: OverflowArtifactStore,
    ) -> None:
        super().__init__(
            max_size=max_size,
            truncation_suffix=truncation_suffix,
        )
        self.artifact_store = artifact_store

    def _overflow_notice(
        self,
        *,
        message: str,
        overflow: dict[str, Any],
        original: ToolResult,
        include_structured: bool,
    ) -> ToolResult:
        """Keep direct dict tools' required object output and the operation's status."""
        notice = ToolResult(
            content=[TextContent(type="text", text=message)],
            structured_content={"overflow": overflow, "message": message}
            if include_structured else None,
            meta={"overflow": overflow},
            is_error=original.is_error,
        )
        if original.meta is None:
            return notice
        candidate = notice.model_copy(update={
            "meta": {**original.meta, **(notice.meta or {})},
        })
        return candidate if len(_serialized_mcp_result(candidate)) <= self.max_size else notice

    @staticmethod
    def _retrieval_message(
        *,
        max_size: int,
        export_id: str,
        response_kind: str = "response",
        artifact_bytes: int | None = None,
        ttl_seconds: int | None = None,
        row_count: int | None = None,
        response_bytes: int | None = None,
        item_pages: bool = False,
    ) -> str:
        """Return compact remote-retrieval guidance for an archived payload."""
        details = []
        if artifact_bytes is not None:
            details.append(f"{artifact_bytes:,} archived bytes")
        if row_count is not None:
            details.append(f"{row_count:,} rows")
        if ttl_seconds is not None:
            details.append(f"retention {ttl_seconds:,} seconds")
        if response_bytes is not None:
            details.append(f"{response_bytes:,} MCP result bytes")
        size_hint = f" ({'; '.join(details)})" if details else ""
        arguments = (
            f'{{"export_id": "{export_id}", "item_offset": 0}}'
            if item_pages
            else f'{{"export_id": "{export_id}", "offset": 0}}'
        )
        continuation = "next_item_offset" if item_pages else "next_offset"
        return (
            f"[{response_kind.capitalize()} exceeded the {max_size:,}-byte "
            f"MCP result limit. The complete JSON is available as export_id "
            f"'{export_id}'{size_hint}. Use call_tool with name='read_overflow_artifact' "
            f"and arguments={arguments} repeatedly using {continuation}. "
            "Use offset=0 and next_offset for complete original JSON byte chunks. When done, use call_tool with "
            "name='delete_overflow_artifact' and that export_id.]"
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Bound the final MCP envelope, measuring proxy results only once."""
        if self.tools is not None and context.message.name not in self.tools:
            return await call_next(context)
        if _PROXY_CALL_ACTIVE.get():
            # The schema-free outer proxy owns its complete transport boundary.
            return await call_next(context)
        is_proxy = context.message.name == "call_tool"
        archive_tool_name = context.message.name
        if is_proxy:
            proxy_arguments = context.message.arguments or {}
            requested_name = proxy_arguments.get("name") or proxy_arguments.get("tool_name")
            if isinstance(requested_name, str) and requested_name.strip():
                archive_tool_name = requested_name.strip()
        proxy_token = _PROXY_CALL_ACTIVE.set(True) if is_proxy else None
        try:
            try:
                result = await call_next(context)
            finally:
                if proxy_token is not None:
                    _PROXY_CALL_ACTIVE.reset(proxy_token)
        except Exception as exc:
            error_text = str(exc)
            error_bytes = len(error_text.encode("utf-8", errors="replace"))
            error_result = ToolResult(
                content=[TextContent(type="text", text=error_text)],
                meta={
                    "overflow_exception": {
                        "type": type(exc).__name__,
                        "message_bytes": error_bytes,
                    }
                },
                is_error=True,
            )
            serialized_error = _serialized_mcp_result(error_result)
            if len(serialized_error) <= self.max_size:
                raise
            try:
                export_id, artifact_size = self.artifact_store.create(
                    error_result,
                    tool_name=archive_tool_name,
                )
            except (OSError, TypeError, ValueError):
                raise ToolError(self.truncation_suffix.strip()) from None
            raise ToolError(
                self._retrieval_message(
                    max_size=self.max_size,
                    export_id=export_id,
                    response_kind="error",
                    artifact_bytes=artifact_size,
                    ttl_seconds=self.artifact_store.ttl_seconds,
                    response_bytes=len(serialized_error),
                )
            ) from None

        if _has_redundant_json_text(result):
            result = result.model_copy(update={
                "content": compact_overflow_chunk_result(result.structured_content).content,
            })
        serialized = _serialized_mcp_result(result)
        if len(serialized) <= self.max_size:
            return result
        if is_proxy and _has_redundant_json_text(result):
            # call_tool has no output schema. Keep one compact JSON text copy for
            # clients that consume only content; direct typed tools retain theirs.
            compact = ToolResult(
                content=[TextContent(type="text", text=pydantic_core.to_json(
                    result.structured_content, fallback=str
                ).decode())],
                meta=result.meta,
                is_error=result.is_error,
            )
            serialized = _serialized_mcp_result(compact)
            if len(serialized) <= self.max_size:
                return compact

        try:
            export_id, artifact_size = self.artifact_store.create(
                result,
                tool_name=archive_tool_name,
            )
        except (OSError, TypeError, ValueError):
            return self._overflow_notice(
                message=self.truncation_suffix.strip(),
                overflow={
                    "archived": False,
                    "original_response_bytes": len(serialized),
                    "max_inline_bytes": self.max_size,
                },
                original=result,
                include_structured=not is_proxy and result.structured_content is not None,
            )

        structured = result.structured_content or {}
        collection = _collection(structured)
        message = self._retrieval_message(
            max_size=self.max_size,
            export_id=export_id,
            artifact_bytes=artifact_size,
            ttl_seconds=self.artifact_store.ttl_seconds,
            row_count=len(collection[1]) if collection is not None else None,
            response_bytes=len(serialized),
            item_pages=collection is not None,
        )
        return self._overflow_notice(
            message=message,
            overflow={
                "archived": True,
                "export_id": export_id,
                "artifact_bytes": artifact_size,
                "original_response_bytes": len(serialized),
                "max_inline_bytes": self.max_size,
            },
            original=result,
            include_structured=not is_proxy and result.structured_content is not None,
        )
