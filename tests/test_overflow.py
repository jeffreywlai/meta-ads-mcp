"""Oversized MCP response artifact storage tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import stat
import time

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult
import mcp.types as mt
from mcp.types import TextContent
import pytest

from meta_ads_mcp.errors import NotFoundError, ValidationError
from meta_ads_mcp import overflow
from meta_ads_mcp.overflow import (
    ArchivedResponseLimitingMiddleware,
    OverflowArtifactStore,
)


def _result(marker: str = "marker") -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=f"content-{marker}")],
        structured_content={"payload": f"structured-{marker}"},
        meta={"source": f"meta-{marker}"},
    )


def _read_complete(store: OverflowArtifactStore, export_id: str) -> dict[str, object]:
    chunks: list[str] = []
    offset = 0
    while True:
        chunk = store.read(export_id, offset=offset, max_bytes=31)
        chunks.append(chunk["data"])
        if chunk["complete"]:
            break
        offset = chunk["next_offset"]
    return json.loads("".join(chunks))


def test_artifact_preserves_complete_tool_result_envelope(tmp_path: Path) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, artifact_bytes = store.create(_result(), tool_name="example_tool")

    artifact = _read_complete(store, export_id)
    assert artifact["schema_version"] == 1
    assert artifact["tool_name"] == "example_tool"
    assert artifact["tool_result"]["content"][0]["text"] == "content-marker"
    assert artifact["tool_result"]["structured_content"] == {
        "payload": "structured-marker"
    }
    assert artifact["tool_result"]["meta"] == {"source": "meta-marker"}
    assert artifact_bytes == store._artifact_path(export_id).stat().st_size


def test_artifact_permissions_are_private_even_with_open_umask(tmp_path: Path) -> None:
    directory = tmp_path / "exports"
    store = OverflowArtifactStore(directory)
    previous_umask = os.umask(0)
    try:
        export_id, _ = store.create(_result(), tool_name="example_tool")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store._artifact_path(export_id).stat().st_mode) == 0o600
    assert stat.S_IMODE(
        (directory / store._manifest_name(export_id)).stat().st_mode
    ) == 0o600
    assert stat.S_IMODE((directory / ".overflow.lock").stat().st_mode) == 0o600


def test_cleanup_enforces_ttl_and_file_quota(tmp_path: Path) -> None:
    store = OverflowArtifactStore(
        tmp_path,
        ttl_seconds=10**12,
        max_files=2,
        max_total_bytes=1_000_000,
    )
    first_id, _ = store.create(_result("first"), tool_name="example_tool")
    first_path = store._artifact_path(first_id)
    os.utime(first_path, (1, 1))
    second_id, _ = store.create(_result("second"), tool_name="example_tool")
    third_id, _ = store.create(_result("third"), tool_name="example_tool")

    assert not first_path.exists()
    assert store._artifact_path(second_id).exists()
    assert store._artifact_path(third_id).exists()

    expiring_store = OverflowArtifactStore(
        tmp_path / "expiring",
        ttl_seconds=1,
    )
    expired_id, _ = expiring_store.create(_result("expired"), tool_name="example_tool")
    expired_path = expiring_store._artifact_path(expired_id)
    old = time.time() - 5
    os.utime(expired_path, (old, old))
    assert expiring_store.cleanup()["removed_files"] == 1
    assert not expired_path.exists()


def test_store_rejects_oversized_artifact_and_invalid_reads(tmp_path: Path) -> None:
    tiny_store = OverflowArtifactStore(tmp_path / "tiny", max_total_bytes=10)
    with pytest.raises(ValueError, match="META_EXPORT_MAX_BYTES"):
        tiny_store.create(_result(), tool_name="example_tool")

    store = OverflowArtifactStore(tmp_path / "normal")
    export_id, _ = store.create(_result(), tool_name="example_tool")
    with pytest.raises(ValidationError, match="export_id"):
        store.read("../escape")
    with pytest.raises(ValidationError, match="max_bytes"):
        store.read(export_id, max_bytes=8_001)
    with pytest.raises(ValidationError, match="offset"):
        store.read(export_id, offset=10**9)

    assert store.delete(export_id)["deleted"] is True
    with pytest.raises(NotFoundError):
        store.read(export_id)
    assert not (store.export_directory / store._manifest_name(export_id)).exists()


def test_read_and_delete_reject_replaced_export_directory(tmp_path: Path) -> None:
    export_directory = tmp_path / "exports"
    store = OverflowArtifactStore(export_directory)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    original_directory = tmp_path / "original"
    export_directory.rename(original_directory)

    attacker_directory = tmp_path / "attacker"
    attacker_directory.mkdir()
    attacker_file = attacker_directory / store._artifact_name(export_id)
    attacker_file.write_text("ATTACKER_CONTROLLED")
    export_directory.symlink_to(attacker_directory, target_is_directory=True)

    with pytest.raises(OSError, match="must not be a symbolic link"):
        store.read(export_id)
    with pytest.raises(OSError, match="must not be a symbolic link"):
        store.delete(export_id)
    assert attacker_file.read_text() == "ATTACKER_CONTROLLED"


def test_post_write_failure_removes_partial_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    original_fchmod = os.fchmod
    calls = 0

    def fail_artifact_chmod(descriptor: int, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected chmod failure")
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", fail_artifact_chmod)
    with pytest.raises(OSError, match="injected chmod failure"):
        store.create(_result(), tool_name="example_tool")

    assert not list(tmp_path.glob("*.json"))


def test_store_fails_closed_without_secure_platform_primitives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    monkeypatch.setattr(overflow, "DIRECTORY_FD_SUPPORTED", False)

    with pytest.raises(OSError, match="Secure overflow artifact storage"):
        store.create(_result(), tool_name="example_tool")


def test_read_rejects_truncated_and_same_size_corrupt_artifacts(
    tmp_path: Path,
) -> None:
    truncated_store = OverflowArtifactStore(tmp_path / "truncated")
    truncated_id, _ = truncated_store.create(_result(), tool_name="example_tool")
    truncated_path = truncated_store._artifact_path(truncated_id)
    truncated_path.write_bytes(truncated_path.read_bytes()[:-1])

    with pytest.raises(ValidationError, match="integrity"):
        truncated_store.read(truncated_id)

    corrupt_store = OverflowArtifactStore(tmp_path / "corrupt")
    corrupt_id, _ = corrupt_store.create(_result(), tool_name="example_tool")
    corrupt_path = corrupt_store._artifact_path(corrupt_id)
    corrupt_bytes = bytearray(corrupt_path.read_bytes())
    corrupt_bytes[-2] = ord(" ")
    corrupt_path.write_bytes(corrupt_bytes)

    with pytest.raises(ValidationError, match="integrity"):
        corrupt_store.read(corrupt_id, max_bytes=8_000)


def test_read_rejects_fifo_replacement_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on this platform")
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    artifact_path = store._artifact_path(export_id)
    artifact_path.unlink()
    os.mkfifo(artifact_path)

    started_at = time.monotonic()
    with pytest.raises(NotFoundError):
        store.read(export_id)
    assert time.monotonic() - started_at < 1


def test_unlock_failure_does_not_lose_successful_artifact_or_leak_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if overflow.fcntl is None:
        pytest.skip("fcntl is unavailable on this platform")
    store = OverflowArtifactStore(tmp_path)
    original_flock = overflow.fcntl.flock

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == overflow.fcntl.LOCK_UN:
            raise OSError("injected unlock failure")
        original_flock(descriptor, operation)

    monkeypatch.setattr(overflow.fcntl, "flock", fail_unlock)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    monkeypatch.setattr(overflow.fcntl, "flock", original_flock)

    assert _read_complete(store, export_id)["tool_name"] == "example_tool"


def test_lock_close_failure_still_closes_directory_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    original_close = os.close
    closed_descriptors: list[int] = []

    def fail_first_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        original_close(descriptor)
        if len(closed_descriptors) == 1:
            raise OSError("injected close failure")

    monkeypatch.setattr(os, "close", fail_first_close)
    with pytest.raises(OSError, match="injected close failure"):
        store.cleanup()

    assert len(closed_descriptors) == 2


def test_cleanup_does_not_delete_unrelated_legacy_shaped_json(
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path, ttl_seconds=1)
    unrelated_path = tmp_path / "report-20260730.json"
    unrelated_hash_path = tmp_path / f"{'a' * 32}.json"
    tmp_path.mkdir(exist_ok=True)
    unrelated_path.write_text('{"user":"not an overflow artifact"}')
    unrelated_hash_path.write_text('{"user":"also not an overflow artifact"}')
    old = time.time() - 5
    os.utime(unrelated_path, (old, old))
    os.utime(unrelated_hash_path, (old, old))

    assert store.cleanup()["removed_files"] == 0
    assert unrelated_path.exists()
    assert unrelated_hash_path.exists()


def test_cleanup_removes_orphan_reserved_manifest(tmp_path: Path) -> None:
    store = OverflowArtifactStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    orphan_manifest = tmp_path / store._manifest_name("a" * 32)
    orphan_manifest.write_text('{"schema_version":1}')

    result = store.cleanup()

    assert result["removed_files"] == 1
    assert not orphan_manifest.exists()


def test_orphan_manifest_directory_does_not_wedge_store(tmp_path: Path) -> None:
    store = OverflowArtifactStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    orphan_manifest = tmp_path / store._manifest_name("a" * 32)
    orphan_manifest.mkdir()

    store.cleanup()
    export_id, _ = store.create(_result(), tool_name="example_tool")

    assert orphan_manifest.is_dir()
    assert store._artifact_path(export_id).exists()


def test_paired_manifest_directory_fails_integrity_without_fd_leak(
    tmp_path: Path,
) -> None:
    fd_directory = Path("/dev/fd")
    if not fd_directory.exists():
        pytest.skip("open-fd accounting is unavailable on this platform")
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    manifest_path = tmp_path / store._manifest_name(export_id)
    manifest_path.unlink()
    manifest_path.mkdir()
    before = len(list(fd_directory.iterdir()))

    for _ in range(20):
        with pytest.raises(ValidationError, match="integrity"):
            store.read(export_id)

    assert len(list(fd_directory.iterdir())) == before


def test_manifest_directory_does_not_make_delete_or_cleanup_report_failure(
    tmp_path: Path,
) -> None:
    delete_store = OverflowArtifactStore(tmp_path / "delete")
    delete_id, _ = delete_store.create(_result(), tool_name="example_tool")
    delete_manifest = (
        delete_store.export_directory / delete_store._manifest_name(delete_id)
    )
    delete_manifest.unlink()
    delete_manifest.mkdir()

    assert delete_store.delete(delete_id)["deleted"] is True
    assert not delete_store._artifact_path(delete_id).exists()
    assert delete_manifest.is_dir()

    cleanup_store = OverflowArtifactStore(
        tmp_path / "cleanup",
        ttl_seconds=1,
    )
    cleanup_id, _ = cleanup_store.create(_result(), tool_name="example_tool")
    cleanup_artifact = cleanup_store._artifact_path(cleanup_id)
    cleanup_manifest = (
        cleanup_store.export_directory
        / cleanup_store._manifest_name(cleanup_id)
    )
    cleanup_manifest.unlink()
    cleanup_manifest.mkdir()
    old = time.time() - 5
    os.utime(cleanup_artifact, (old, old))

    assert cleanup_store.cleanup()["removed_files"] == 1
    assert not cleanup_artifact.exists()
    assert cleanup_manifest.is_dir()


@pytest.mark.parametrize("fstat_call_to_fail", [3, 4])
def test_read_fstat_failure_does_not_leak_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fstat_call_to_fail: int,
) -> None:
    fd_directory = Path("/dev/fd")
    if not fd_directory.exists():
        pytest.skip("open-fd accounting is unavailable on this platform")
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    original_fstat = os.fstat
    call_index = 0

    def fail_selected_fstat(descriptor: int):
        nonlocal call_index
        call_index += 1
        if call_index == fstat_call_to_fail:
            raise OSError("injected fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", fail_selected_fstat)
    before = len(list(fd_directory.iterdir()))
    for _ in range(20):
        call_index = 0
        with pytest.raises(OSError, match="injected fstat failure"):
            store.read(export_id)

    assert len(list(fd_directory.iterdir())) == before


def test_read_refreshes_ttl_for_active_chunk_retrieval(tmp_path: Path) -> None:
    store = OverflowArtifactStore(tmp_path, ttl_seconds=1)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    artifact_path = store._artifact_path(export_id)
    almost_expired = time.time() - 0.5
    os.utime(artifact_path, (almost_expired, almost_expired))

    store.read(export_id, max_bytes=1)
    refreshed_at = artifact_path.stat().st_mtime

    assert refreshed_at > almost_expired
    assert store.cleanup(now=refreshed_at + 0.5)["removed_files"] == 0
    assert artifact_path.exists()


def test_cleanup_counts_corrupt_pairs_for_retention(tmp_path: Path) -> None:
    store = OverflowArtifactStore(
        tmp_path,
        ttl_seconds=1,
        max_files=1,
        max_total_bytes=1_000_000,
    )
    export_id, _ = store.create(_result(), tool_name="example_tool")
    artifact_path = store._artifact_path(export_id)
    artifact_path.write_bytes(artifact_path.read_bytes()[:-1])
    store.max_total_bytes = 1
    old = time.time() - 5
    os.utime(artifact_path, (old, old))

    result = store.cleanup()

    assert result["removed_files"] == 1
    assert not artifact_path.exists()
    assert not (tmp_path / store._manifest_name(export_id)).exists()


def test_directory_replacement_race_does_not_chmod_symlink_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    export_directory = tmp_path / "exports"
    victim_directory = tmp_path / "victim"
    victim_directory.mkdir(mode=0o755)
    victim_directory.chmod(0o755)
    store = OverflowArtifactStore(export_directory)
    original_ensure = store._ensure_directory

    def replace_after_ensure() -> None:
        original_ensure()
        export_directory.rename(tmp_path / "original")
        export_directory.symlink_to(victim_directory, target_is_directory=True)

    monkeypatch.setattr(store, "_ensure_directory", replace_after_ensure)

    with pytest.raises(OSError):
        store.cleanup()
    assert stat.S_IMODE(victim_directory.stat().st_mode) == 0o755


def test_lock_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on this platform")
    store = OverflowArtifactStore(tmp_path)
    store.cleanup()
    lock_path = tmp_path / overflow.LOCK_FILE_NAME
    lock_path.unlink()
    os.mkfifo(lock_path)

    started_at = time.monotonic()
    with pytest.raises(OSError, match="regular file"):
        store.cleanup()
    assert time.monotonic() - started_at < 1


def test_oversized_exception_uses_serialized_boundary_and_remains_retrievable(
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    middleware = ArchivedResponseLimitingMiddleware(
        max_size=64_000,
        truncation_suffix="fallback",
        artifact_store=store,
    )
    context = MiddlewareContext(
        message=mt.CallToolRequestParams(name="failing_tool", arguments={})
    )
    full_error = "x" * 63_920

    async def fail(_context):
        raise ValueError(full_error)

    with pytest.raises(ToolError, match="export_id") as exc_info:
        asyncio.run(middleware.on_call_tool(context, fail))

    match = re.search(r"export_id '([^']+)'", str(exc_info.value))
    assert match is not None
    artifact = _read_complete(store, match.group(1))
    assert artifact["tool_result"]["content"][0]["text"] == full_error
    assert artifact["tool_result"]["meta"]["overflow_exception"]["type"] == "ValueError"
