"""Bounded server-side storage for oversized MCP tool responses."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
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
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from meta_ads_mcp.errors import NotFoundError, ValidationError


DEFAULT_ARTIFACT_TTL_SECONDS = 86_400
DEFAULT_ARTIFACT_MAX_FILES = 100
DEFAULT_ARTIFACT_MAX_BYTES = 1_000_000_000
DEFAULT_ARTIFACT_CHUNK_BYTES = 8_000
MAX_ARTIFACT_CHUNK_BYTES = 8_000
EXPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")
LOCK_FILE_NAME = ".overflow.lock"
DIRECTORY_FD_SUPPORTED = all(
    function in os.supports_dir_fd
    for function in (os.open, os.stat, os.unlink)
)


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

    def _ensure_directory(self) -> None:
        """Create or tighten the artifact directory to owner-only access."""
        if self.export_directory.is_symlink():
            raise OSError("META_EXPORT_DIR must not be a symbolic link.")
        self.export_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.export_directory.is_dir():
            raise OSError("META_EXPORT_DIR must be a directory.")
        self.export_directory.chmod(0o700)

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
        return self.export_directory / f"{self._validate_export_id(export_id)}.json"

    @staticmethod
    def _artifact_name(export_id: str) -> str:
        """Return the artifact filename for an already validated id."""
        return f"{export_id}.json"

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
                lock_fd = os.open(
                    LOCK_FILE_NAME,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield directory_fd
            finally:
                if lock_fd is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
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
        """Return recognized regular artifacts without following symlinks."""
        entries = os.listdir(directory_fd)
        artifacts: list[tuple[float, int, str]] = []
        for name in entries:
            if not name.endswith(".json") or not EXPORT_ID_PATTERN.fullmatch(name[:-5]):
                continue
            try:
                file_stat = self._stat_name(name, directory_fd)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(file_stat.st_mode):
                artifacts.append((file_stat.st_mtime, file_stat.st_size, name))
        return artifacts

    def _cleanup_unlocked(
        self,
        *,
        directory_fd: int,
        now: float | None = None,
        protect_name: str | None = None,
    ) -> dict[str, int]:
        """Apply retention limits while the directory lock is held."""
        current_time = time.time() if now is None else now
        removed_files = 0
        removed_bytes = 0
        for modified_at, file_size, name in self._artifact_records(directory_fd):
            if (
                name != protect_name
                and current_time - modified_at > self.ttl_seconds
            ):
                try:
                    self._unlink_name(name, directory_fd)
                except FileNotFoundError:
                    continue
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
            try:
                self._unlink_name(name, directory_fd)
            except FileNotFoundError:
                continue
            removed_files += 1
            removed_bytes += file_size

        return {"removed_files": removed_files, "removed_bytes": removed_bytes}

    def cleanup(self, *, now: float | None = None) -> dict[str, int]:
        """Delete expired artifacts, then enforce count and total-byte quotas."""
        with self._locked_directory() as directory_fd:
            return self._cleanup_unlocked(
                directory_fd=directory_fd,
                now=now,
            )

    def create(self, result: ToolResult, *, tool_name: str) -> tuple[str, int]:
        """Write a versioned, complete ToolResult envelope and return its opaque id."""
        created_at = datetime.now(timezone.utc).isoformat()
        artifact_payload = {
            "schema_version": 1,
            "tool_name": tool_name,
            "created_at": created_at,
            "tool_result": result.model_dump(mode="json", by_alias=True),
        }
        artifact_bytes = json.dumps(
            artifact_payload,
            ensure_ascii=True,
            indent=2,
            default=str,
        ).encode("utf-8")
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

            try:
                with os.fdopen(descriptor, "wb") as artifact_file:
                    os.fchmod(artifact_file.fileno(), 0o600)
                    artifact_file.write(artifact_bytes)
                self._cleanup_unlocked(
                    directory_fd=directory_fd,
                    protect_name=artifact_name,
                )
            except Exception:
                try:
                    self._unlink_name(artifact_name, directory_fd)
                except FileNotFoundError:
                    pass
                raise
            return export_id, len(artifact_bytes)

    def read(
        self,
        export_id: str,
        *,
        offset: int = 0,
        max_bytes: int = DEFAULT_ARTIFACT_CHUNK_BYTES,
    ) -> dict[str, Any]:
        """Read one ASCII-safe JSON chunk by opaque id."""
        if offset < 0:
            raise ValidationError("offset must be at least 0.")
        if max_bytes < 1 or max_bytes > MAX_ARTIFACT_CHUNK_BYTES:
            raise ValidationError(
                f"max_bytes must be between 1 and {MAX_ARTIFACT_CHUNK_BYTES}."
            )
        normalized_export_id = self._validate_export_id(export_id)
        artifact_name = self._artifact_name(normalized_export_id)
        with self._locked_directory() as directory_fd:
            self._cleanup_unlocked(directory_fd=directory_fd)
            try:
                descriptor = self._open_name(
                    artifact_name,
                    os.O_RDONLY,
                    directory_fd,
                )
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ELOOP, errno.EISDIR}:
                    raise NotFoundError(
                        "Overflow artifact was not found or has expired."
                    ) from exc
                raise
            with os.fdopen(descriptor, "rb") as artifact_file:
                file_stat = os.fstat(artifact_file.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise NotFoundError(
                        "Overflow artifact was not found or has expired."
                    )
                total_bytes = file_stat.st_size
                if offset > total_bytes:
                    raise ValidationError("offset exceeds the artifact size.")
                artifact_file.seek(offset)
                chunk_bytes = artifact_file.read(max_bytes)
        next_offset = offset + len(chunk_bytes)
        return {
            "export_id": normalized_export_id,
            "format": "application/json",
            "encoding": "utf-8",
            "offset": offset,
            "next_offset": next_offset,
            "total_bytes": total_bytes,
            "complete": next_offset >= total_bytes,
            "data": chunk_bytes.decode("ascii"),
        }

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
                self._unlink_name(artifact_name, directory_fd)
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

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Archive responses whose complete serialized MCP result exceeds the cap."""
        result = await call_next(context)
        if self.tools is not None and context.message.name not in self.tools:
            return result

        serialized = pydantic_core.to_json(result, fallback=str)
        if len(serialized) <= self.max_size:
            return result

        try:
            export_id, artifact_size = self.artifact_store.create(
                result,
                tool_name=context.message.name,
            )
        except (OSError, TypeError, ValueError):
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=self.truncation_suffix.strip(),
                    )
                ],
                meta={
                    "overflow": {
                        "archived": False,
                        "original_response_bytes": len(serialized),
                        "max_inline_bytes": self.max_size,
                    }
                },
            )

        message = (
            f"[Response exceeded the {self.max_size:,}-byte inline limit. "
            f"The complete JSON response is available as export_id '{export_id}'. "
            "Use call_tool with name='read_overflow_artifact' and arguments="
            f'{{"export_id": "{export_id}", "offset": 0}} repeatedly using '
            "next_offset. When done, use call_tool with "
            "name='delete_overflow_artifact' and that export_id.]"
        )
        return ToolResult(
            content=[TextContent(type="text", text=message)],
            meta={
                "overflow": {
                    "archived": True,
                    "export_id": export_id,
                    "artifact_bytes": artifact_size,
                    "original_response_bytes": len(serialized),
                    "max_inline_bytes": self.max_size,
                }
            },
        )
