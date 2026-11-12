"""Oversized MCP response artifact storage tests."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
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
from meta_ads_mcp.diagnostics import derive_core_metrics
from meta_ads_mcp.normalize import normalize_insights_row
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
    assert artifact["schema_version"] == 2
    assert artifact["tool_name"] == "example_tool"
    assert artifact["tool_result"]["content"][0]["text"] == "content-marker"
    assert artifact["tool_result"]["structured_content"] == {
        "payload": "structured-marker"
    }
    assert artifact["tool_result"]["meta"] == {"source": "meta-marker"}
    assert artifact_bytes == store._artifact_path(export_id).stat().st_size


def test_artifact_stores_redundant_json_once_compactly(tmp_path: Path) -> None:
    payload = {"items": [{"name": "ad", "actions": [{"action_type": "purchase", "value": "2"}]} for _ in range(500)]}
    result = ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
                        structured_content=payload, meta={"source": "test"})
    store = OverflowArtifactStore(tmp_path)
    export_id, size = store.create(result, tool_name="get_insights")
    artifact = _read_complete(store, export_id)

    assert artifact["tool_result"]["content"] == []
    assert artifact["tool_result"]["structured_content"] == payload
    assert artifact["tool_result"]["meta"] == {"source": "test"}
    assert size < 1.1 * len(json.dumps(payload, separators=(",", ":")).encode())
    assert result.content[0].text == json.dumps(payload, indent=2)


def test_s4_shaped_22_row_action_artifact_has_one_compact_copy(tmp_path: Path) -> None:
    # Reproduce the redacted report's shape, not its unavailable original values.
    action_types = ["purchase", *[f"offsite_conversion.fb_pixel_custom.action_{index}" for index in range(16)]]
    rows = []
    for index in range(22):
        row = normalize_insights_row({
            "ad_id": "ad_s4", "ad_name": "A3_story_creative",
            "date_start": "2026-03-17", "date_stop": "2026-07-28",
            "publisher_platform": "instagram", "platform_position": "story",
            "spend": "100", "impressions": "10000", "clicks": "80", "ctr": "0.8", "cpm": "10",
            "actions": [{"action_type": action, "value": str(index + 2), "7d_click": str(index + 1)}
                        for action in action_types],
            "action_values": [{"action_type": action, "value": str((index + 2) * 100), "7d_click": str((index + 1) * 100)}
                              for action in action_types],
        })
        row["metrics"] = derive_core_metrics(row)
        rows.append(row)
    payload = {"items": rows, "summary": {"count": 22, "complete": True}}
    compact_payload = json.dumps(payload, separators=(",", ":")).encode()
    assert len(compact_payload) > 64_000  # Exercise the actual overflow-sized workload.
    result = ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        structured_content=payload,
    )
    store = OverflowArtifactStore(tmp_path)
    export_id, artifact_bytes = store.create(result, tool_name="get_insights")
    artifact_text = store._artifact_path(export_id).read_text()
    artifact = json.loads(artifact_text)

    assert artifact["tool_result"]["content"] == []
    assert artifact["tool_result"]["structured_content"] == payload
    assert artifact_text.count('"ad_name":') == 22
    assert "\n" not in artifact_text
    assert artifact_bytes <= 1.1 * len(compact_payload)
    assert result.content[0].text == json.dumps(payload, indent=2)


def test_overflow_notice_exposes_size_rows_and_retention() -> None:
    notice = ArchivedResponseLimitingMiddleware._retrieval_message(
        max_size=64000, export_id="opaque", artifact_bytes=88169, row_count=22,
        ttl_seconds=86400, response_bytes=90500, item_pages=True,
    )
    assert "88,169 archived bytes" in notice
    assert "22 rows" in notice
    assert "retention 86,400 seconds" in notice
    assert "90,500 MCP result bytes" in notice
    assert '"item_offset": 0' in notice
    assert "next_item_offset" in notice
    assert "next_offset" in notice


@pytest.mark.parametrize(
    ("text", "structured"),
    [('not JSON', {"value": 1}), ('{"value": 1}', {"value": True}), ('{"value": 2}', {"value": 1})],
)
def test_archive_keeps_distinct_text(tmp_path, text, structured) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(ToolResult(content=[TextContent(type="text", text=text)],
                                          structured_content=structured), tool_name="test")
    assert _read_complete(store, export_id)["tool_result"]["content"][0]["text"] == text


def test_archive_keeps_annotations_on_duplicate_text(tmp_path) -> None:
    store = OverflowArtifactStore(tmp_path)
    result = ToolResult(content=[TextContent(type="text", text='{"value":1}', annotations=mt.Annotations(priority=0.5))],
                        structured_content={"value": 1})
    export_id, _ = store.create(result, tool_name="test")
    assert _read_complete(store, export_id)["tool_result"]["content"][0]["annotations"]["priority"] == 0.5


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
    os.utime(tmp_path / store._manifest_name(first_id), (1, 1))
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
    os.utime(
        expiring_store.export_directory
        / expiring_store._manifest_name(expired_id),
        (old, old),
    )
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
        store.read(export_id, max_bytes=44_001)
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


def test_read_rejects_corruption_before_returning_a_nonfinal_chunk(
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    result = ToolResult(
        content=[TextContent(type="text", text="A" * 200_000)],
        structured_content={"payload": "B" * 200_000},
    )
    export_id, _ = store.create(result, tool_name="large_tool")
    artifact_path = store._artifact_path(export_id)
    artifact_bytes = bytearray(artifact_path.read_bytes())
    artifact_bytes[artifact_bytes.index(ord("A"))] = ord("Z")
    artifact_path.write_bytes(artifact_bytes)

    with pytest.raises(ValidationError, match="integrity"):
        store.read(export_id, offset=0, max_bytes=1_000)


def test_chunk_reads_reuse_one_stable_full_artifact_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    result = ToolResult(
        content=[TextContent(type="text", text="A" * 200_000)],
        structured_content={"payload": "B" * 200_000},
    )
    export_id, _ = store.create(result, tool_name="large_tool")
    original_sha256 = overflow.hashlib.sha256
    hash_calls = 0

    def counting_sha256(*args, **kwargs):
        nonlocal hash_calls
        hash_calls += 1
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(overflow.hashlib, "sha256", counting_sha256)
    offset = 0
    while True:
        chunk = store.read(export_id, offset=offset, max_bytes=44_000)
        if chunk["complete"]:
            break
        offset = chunk["next_offset"]

    assert hash_calls == 1


def test_verified_identity_cache_is_bounded_and_eviction_rehashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path, max_files=70)
    export_ids = [
        store.create(_result(str(index)), tool_name="example_tool")[0]
        for index in range(65)
    ]
    original_sha256 = overflow.hashlib.sha256
    hash_calls = 0

    def counting_sha256(*args, **kwargs):
        nonlocal hash_calls
        hash_calls += 1
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(overflow.hashlib, "sha256", counting_sha256)
    for export_id in export_ids:
        store.read(export_id, max_bytes=1)

    assert hash_calls == 65
    assert len(store._verified_artifacts) == 64
    assert export_ids[0] not in store._verified_artifacts

    store.read(export_ids[0], max_bytes=1)

    assert hash_calls == 66
    assert len(store._verified_artifacts) == 64


def test_cleanup_prunes_cache_entries_deleted_by_another_store(
    tmp_path: Path,
) -> None:
    reader = OverflowArtifactStore(tmp_path)
    writer = OverflowArtifactStore(tmp_path)
    maximum_cache_entries = 0

    for index in range(80):
        export_id, _ = writer.create(
            _result(str(index)),
            tool_name="example_tool",
        )
        reader.read(export_id, max_bytes=1)
        writer.delete(export_id)
        maximum_cache_entries = max(
            maximum_cache_entries,
            len(reader._verified_artifacts),
        )

    reader.cleanup()

    assert maximum_cache_entries <= 1
    assert not reader._verified_artifacts


def test_read_errors_evict_cached_identity(
    tmp_path: Path,
) -> None:
    reader = OverflowArtifactStore(tmp_path)
    writer = OverflowArtifactStore(tmp_path)
    deleted_id, _ = writer.create(_result("deleted"), tool_name="example_tool")
    reader.read(deleted_id, max_bytes=1)
    writer.delete(deleted_id)

    with pytest.raises(NotFoundError):
        reader.read(deleted_id, max_bytes=1)
    assert deleted_id not in reader._verified_artifacts

    corrupt_id, _ = writer.create(_result("corrupt"), tool_name="example_tool")
    reader.read(corrupt_id, max_bytes=1)
    manifest_path = tmp_path / reader._manifest_name(corrupt_id)
    manifest_path.write_text("not-json")

    with pytest.raises(ValidationError, match="integrity"):
        reader.read(corrupt_id, max_bytes=1)
    assert corrupt_id not in reader._verified_artifacts


def test_concurrent_reads_keep_verification_cache_coherent(
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path, max_files=20)
    export_ids = [
        store.create(_result(str(index)), tool_name="example_tool")[0]
        for index in range(20)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        chunks = list(
            executor.map(
                lambda export_id: store.read(export_id, max_bytes=1),
                export_ids * 2,
            )
        )

    assert len(chunks) == 40
    assert len(store._verified_artifacts) <= store.max_files


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
    manifest_path = tmp_path / store._manifest_name(export_id)
    almost_expired = time.time() - 0.5
    os.utime(artifact_path, (almost_expired, almost_expired))
    os.utime(manifest_path, (almost_expired, almost_expired))
    original_artifact_mtime = artifact_path.stat().st_mtime_ns

    store.read(export_id, max_bytes=1)
    refreshed_at = manifest_path.stat().st_mtime

    assert refreshed_at > almost_expired
    assert artifact_path.stat().st_mtime_ns == original_artifact_mtime
    assert store.cleanup(now=refreshed_at + 0.5)["removed_files"] == 0
    assert artifact_path.exists()


def test_lease_refresh_never_trusts_concurrent_artifact_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    result = ToolResult(
        content=[TextContent(type="text", text="A" * 200_000)],
    )
    export_id, _ = store.create(result, tool_name="large_tool")
    artifact_path = store._artifact_path(export_id)
    original_utime = os.utime
    corruption_offset: int | None = None

    def corrupt_before_lease_refresh(path_or_fd, *args, **kwargs) -> None:
        nonlocal corruption_offset
        artifact_bytes = bytearray(artifact_path.read_bytes())
        corruption_offset = artifact_bytes.index(ord("A"))
        artifact_bytes[corruption_offset] = ord("Z")
        artifact_path.write_bytes(artifact_bytes)
        original_utime(path_or_fd, *args, **kwargs)
        monkeypatch.setattr(overflow.os, "utime", original_utime)

    monkeypatch.setattr(overflow.os, "utime", corrupt_before_lease_refresh)

    store.read(export_id, offset=0, max_bytes=100)

    assert corruption_offset is not None
    with pytest.raises(ValidationError, match="integrity"):
        store.read(export_id, offset=corruption_offset, max_bytes=1)


def test_cleanup_uses_manifest_lease_without_mutating_artifact(
    tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path, ttl_seconds=10)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    artifact_path = store._artifact_path(export_id)
    manifest_path = tmp_path / store._manifest_name(export_id)
    old = time.time() - 20
    active = time.time()
    os.utime(artifact_path, (old, old))
    os.utime(manifest_path, (active, active))

    result = store.cleanup(now=active + 5)

    assert result["removed_files"] == 0
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
    assert artifact["tool_result"]["is_error"] is True


def test_byte_offsets_must_be_issued_and_survive_store_restart(tmp_path: Path) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(content=[TextContent(type="text", text="x" * 150_000)]),
        tool_name="large_tool",
    )
    first = store.read(export_id)
    second = store.read(export_id, offset=first["next_offset"], max_bytes=16_803)
    assert second["next_offset"] == 60_803

    restarted = OverflowArtifactStore(tmp_path)
    with pytest.raises(ValidationError, match="issued next_offset"):
        restarted.read(export_id, offset=62_000)
    resumed = restarted.read(export_id, offset=60_803)
    assert resumed["offset"] == 60_803
    assert store.read(export_id, offset=first["next_offset"])["data"]


def test_recent_offsets_are_bounded_and_expired_cursors_fail_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(overflow, "MAX_RECENT_ARTIFACT_OFFSETS", 3)
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    offset = 0
    for _ in range(5):
        offset = store.read(export_id, offset=offset, max_bytes=1)["next_offset"]
    manifest = json.loads((tmp_path / store._manifest_name(export_id)).read_text())
    assert manifest["byte_offsets"] == [3, 4, 5]
    with pytest.raises(ValidationError, match="history has expired"):
        store.read(export_id, offset=1)
    assert store.read(export_id, offset=0, max_bytes=1)["next_offset"] == 1


@pytest.mark.parametrize("shape", ["items", "rows", "async"])
def test_parsed_pages_preserve_every_item_and_collection_context(
    tmp_path: Path, shape: str,
) -> None:
    items = [{"id": str(index), "detail": "x" * 800} for index in range(200)]
    if shape == "async":
        payload = {
            "job": {"state": "Job Completed", "ready": True},
            "rows": {"items": items, "paging": {"next": "cursor"}, "summary": {"count": 200}},
        }
        expected_path = ["rows", "items"]
    else:
        payload = {shape: items, "summary": {"count": 200, "complete": True}}
        expected_path = [shape]
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                   structured_content=payload, meta={"source": "retained"}),
        tool_name="get_async_insights_report" if shape == "async" else "list_audiences",
    )
    retrieved = []
    item_offset = 0
    calls = 0
    while True:
        page = store.read(export_id, item_offset=item_offset, item_limit=50)
        assert len(overflow._serialized_mcp_result(overflow.compact_overflow_chunk_result(page))) <= 63_000
        assert page["item_path"] == expected_path
        assert page["context_included"] is True
        assert page["context"]["tool_result"]["meta"] == {"source": "retained"}
        context = page["context"]["tool_result"]["structured_content"]
        if shape == "async":
            assert context["job"]["ready"] is True
            assert context["rows"]["paging"] == {"next": "cursor"}
            assert context["rows"]["items"] == []
        else:
            assert context["summary"]["complete"] is True
            assert context[shape] == []
        retrieved.extend(page["items"])
        calls += 1
        if page["complete"]:
            break
        item_offset = page["next_item_offset"]
        store = OverflowArtifactStore(tmp_path)  # Continuations survive a restart.
    assert retrieved == items
    assert calls < 10
    with pytest.raises(ValidationError, match="issued next_item_offset"):
        store.read(export_id, item_offset=1)


def test_many_item_pages_parse_100000_records_only_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(structured_content={
            "items": [{"id": str(index), "detail": "x" * 32} for index in range(100_000)],
            "summary": {"count": 100_000, "complete": True},
        }),
        tool_name="get_insights",
    )
    original_load = overflow.json.load
    parse_calls = 0

    def counting_load(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(overflow.json, "load", counting_load)
    item_offset = 0
    pages = 0
    while True:
        page = store.read(export_id, item_offset=item_offset, item_limit=500)
        assert [item["id"] for item in page["items"]] == [
            str(index) for index in range(item_offset, page["next_item_offset"])
        ]
        assert len(overflow._serialized_mcp_result(overflow.compact_overflow_chunk_result(page))) <= 63_000
        pages += 1
        if page["complete"]:
            break
        assert page["next_item_offset"] > item_offset
        item_offset = page["next_item_offset"]

    assert page["next_item_offset"] == 100_000
    assert pages >= 200
    assert parse_calls == 1
    assert 0 < store._parsed_artifact_bytes <= overflow.MAX_PARSED_ARTIFACT_CACHE_BYTES


def test_item_cache_revalidates_and_reparses_after_restart_or_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(structured_content={"items": [{"id": str(index)} for index in range(3)]}),
        tool_name="list_ads",
    )
    original_load = overflow.json.load
    original_sha256 = overflow.hashlib.sha256
    parse_calls = hash_calls = 0

    def counting_load(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_load(*args, **kwargs)

    def counting_sha256(*args, **kwargs):
        nonlocal hash_calls
        hash_calls += 1
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(overflow.json, "load", counting_load)
    monkeypatch.setattr(overflow.hashlib, "sha256", counting_sha256)
    first = store.read(export_id, item_offset=0, item_limit=1)
    restarted = OverflowArtifactStore(tmp_path)
    second = restarted.read(export_id, item_offset=first["next_item_offset"], item_limit=1)
    assert second["items"] == [{"id": "1"}]
    assert parse_calls == hash_calls == 2

    artifact_path = store._artifact_path(export_id)
    original_stat = artifact_path.stat()
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(artifact_path.read_bytes())
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replacement.replace(artifact_path)
    third = restarted.read(export_id, item_offset=second["next_item_offset"], item_limit=1)

    assert third["items"] == [{"id": "2"}]
    assert third["complete"] is True
    assert parse_calls == hash_calls == 3


@pytest.mark.parametrize("tamper", ["in_place", "replace", "manifest", "during_parse"])
def test_item_cache_never_hides_artifact_tampering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(structured_content={"items": [{"id": "original"}]}),
        tool_name="list_ads",
    )
    artifact_path = store._artifact_path(export_id)
    original_stat = artifact_path.stat()
    corrupted_bytes = artifact_path.read_bytes().replace(b'"original"', b'"tampered"')

    if tamper == "during_parse":
        original_load = overflow.json.load

        def tamper_after_load(*args, **kwargs):
            artifact = original_load(*args, **kwargs)
            artifact_path.write_bytes(corrupted_bytes)
            return artifact

        monkeypatch.setattr(overflow.json, "load", tamper_after_load)
    else:
        store.read(export_id, item_offset=0)
        assert export_id in store._parsed_artifacts
        if tamper == "manifest":
            manifest_path = tmp_path / store._manifest_name(export_id)
            manifest = json.loads(manifest_path.read_text())
            manifest["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest))
        elif tamper == "replace":
            replacement = tmp_path / "replacement.json"
            replacement.write_bytes(corrupted_bytes)
            os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            replacement.replace(artifact_path)
        else:
            artifact_path.write_bytes(corrupted_bytes)
            os.utime(artifact_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    with pytest.raises(ValidationError, match="integrity"):
        store.read(export_id, item_offset=0)
    assert export_id not in store._verified_artifacts
    assert export_id not in store._parsed_artifacts
    assert store._parsed_artifact_bytes == 0


def test_item_cache_keeps_returned_mutable_data_private(tmp_path: Path) -> None:
    store = OverflowArtifactStore(tmp_path)
    payload = {
        "items": [{"id": "one", "details": {"tags": ["original"]}}],
        "summary": {"notes": ["original"]},
    }
    export_id, _ = store.create(
        ToolResult(structured_content=payload, meta={"notes": ["original"]}),
        tool_name="list_ads",
    )
    first = store.read(export_id, item_offset=0)
    first["items"][0]["details"]["tags"].append("caller mutation")
    first["context"]["tool_result"]["structured_content"]["summary"]["notes"].clear()
    first["context"]["tool_result"]["meta"]["notes"].clear()
    second = store.read(export_id, item_offset=0)

    assert second["items"] == payload["items"]
    assert second["context"]["tool_result"]["structured_content"]["summary"] == payload["summary"]
    assert second["context"]["tool_result"]["meta"] == {"notes": ["original"]}


def test_item_cache_count_limit_evicts_least_recently_used_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(overflow, "MAX_PARSED_ARTIFACT_CACHE_ENTRIES", 2)
    store = OverflowArtifactStore(tmp_path)
    export_ids = [
        store.create(ToolResult(structured_content={"items": [{"id": str(index)}]}),
                     tool_name="list_ads")[0]
        for index in range(3)
    ]
    original_load = overflow.json.load
    parse_calls = 0

    def counting_load(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(overflow.json, "load", counting_load)
    for export_id in (export_ids[0], export_ids[1], export_ids[0], export_ids[2]):
        store.read(export_id, item_offset=0)
    assert parse_calls == 3
    assert list(store._parsed_artifacts) == [export_ids[0], export_ids[2]]
    store.read(export_ids[0], item_offset=0)
    assert parse_calls == 3
    assert store.read(export_ids[1], item_offset=0)["items"] == [{"id": "1"}]
    assert parse_calls == 4
    assert len(store._parsed_artifacts) == 2


def test_concurrent_item_reads_share_one_private_cached_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(structured_content={"items": [{"id": str(index)} for index in range(100)]}),
        tool_name="list_ads",
    )
    original_load = overflow.json.load
    parse_calls = 0

    def counting_load(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(overflow.json, "load", counting_load)

    def read_and_mutate(_index):
        page = store.read(export_id, item_offset=0, item_limit=1)
        page["items"][0]["id"] = "caller mutation"
        return page

    with ThreadPoolExecutor(max_workers=8) as executor:
        pages = list(executor.map(read_and_mutate, range(40)))

    assert len(pages) == 40
    assert parse_calls == 1
    assert store.read(export_id, item_offset=0, item_limit=1)["items"] == [{"id": "0"}]
    assert len(store._parsed_artifacts) == 1
    assert 0 < store._parsed_artifact_bytes <= overflow.MAX_PARSED_ARTIFACT_CACHE_BYTES


def test_item_cache_byte_limit_and_oversized_bypass_remain_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(overflow, "MAX_PARSED_ARTIFACT_CACHE_BYTES", 65_536)
    store = OverflowArtifactStore(tmp_path)
    export_ids = [
        store.create(ToolResult(structured_content={
            "items": [{"id": str(index), "detail": str(number) * 32} for index in range(count)],
        }), tool_name="list_ads")[0]
        for number, count in ((1, 100), (2, 100), (3, 1_000))
    ]
    original_load = overflow.json.load
    parse_calls = 0

    def counting_load(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(overflow.json, "load", counting_load)
    store.read(export_ids[0], item_offset=0)
    first_bytes = store._parsed_artifact_bytes
    assert 32_768 < first_bytes <= 65_536
    store.read(export_ids[1], item_offset=0)
    assert list(store._parsed_artifacts) == [export_ids[1]]
    assert 0 < store._parsed_artifact_bytes <= 65_536
    for _ in range(2):
        assert store.read(export_ids[2], item_offset=0)["items"][0]["detail"] == "3" * 32
    assert export_ids[2] not in store._parsed_artifacts
    assert list(store._parsed_artifacts) == [export_ids[1]]
    assert parse_calls == 4
    assert store.read(export_ids[1], item_offset=0)["complete"] is True
    assert parse_calls == 4


@pytest.mark.parametrize("removal", ["delete", "external_delete", "expire", "quota", "manifest"])
def test_item_cache_releases_removed_or_invalid_artifact(
    tmp_path: Path, removal: str,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(structured_content={"items": [{"id": "one"}]}), tool_name="list_ads",
    )
    store.read(export_id, item_offset=0)
    assert store._parsed_artifact_bytes > 0
    if removal == "delete":
        store.delete(export_id)
    elif removal == "external_delete":
        OverflowArtifactStore(tmp_path).delete(export_id)
        store.cleanup()
    elif removal == "expire":
        store.cleanup(now=time.time() + store.ttl_seconds + 1)
    elif removal == "quota":
        OverflowArtifactStore(tmp_path, max_files=1).create(_result(), tool_name="example_tool")
        store.cleanup()
    else:
        (tmp_path / store._manifest_name(export_id)).write_text("not-json")
        with pytest.raises(ValidationError, match="integrity"):
            store.read(export_id, item_offset=0)
    assert export_id not in store._parsed_artifacts
    assert store._parsed_artifact_bytes == 0


def test_large_context_does_not_block_small_item_pages(tmp_path: Path) -> None:
    payload = {"items": [{"id": "one"}], "context": "x" * 100_000}
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(ToolResult(structured_content=payload), tool_name="list_ads")
    page = store.read(export_id, item_offset=0)
    assert page["items"] == [{"id": "one"}]
    assert page["complete"] is True
    assert page["context_included"] is False
    assert "byte mode" in page["context_note"]
    assert _read_complete(store, export_id)["tool_result"]["structured_content"] == payload


def test_single_oversized_item_remains_recoverable_without_skipping(tmp_path: Path) -> None:
    payload = {"items": [{"id": "huge", "value": '\\"🙂' * 20_000}, {"id": "later"}]}
    store = OverflowArtifactStore(tmp_path)
    export_id, _ = store.create(
        ToolResult(content=[TextContent(type="text", text=json.dumps(payload))], structured_content=payload),
        tool_name="list_ads",
    )
    page = store.read(export_id, item_offset=0)
    assert page["items"] == []
    assert page["oversized_item"] == 0
    assert page["next_item_offset"] == 0
    assert page["complete"] is False
    assert "offset=0" in page["next_step"]
    chunks = []
    offset = 0
    while True:
        chunk = store.read(export_id, offset=offset, max_result_bytes=63_000)
        assert len(overflow._serialized_mcp_result(overflow.compact_overflow_chunk_result(chunk))) <= 63_000
        chunks.append(chunk["data"])
        if chunk["complete"]:
            break
        offset = chunk["next_offset"]
    assert json.loads("".join(chunks))["tool_result"]["structured_content"] == payload


@pytest.mark.parametrize("archive_fails", [False, True])
@pytest.mark.parametrize("is_error", [False, True])
def test_overflow_preserves_error_status_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_fails: bool, is_error: bool,
) -> None:
    store = OverflowArtifactStore(tmp_path)
    middleware = ArchivedResponseLimitingMiddleware(
        max_size=64_000, truncation_suffix="fallback", artifact_store=store,
    )
    original = ToolResult(structured_content={"payload": "x" * 100_000},
                          meta={"mutation_outcome_unknown": True}, is_error=is_error)

    async def call_next(_context):
        return original

    if archive_fails:
        def fail_create(*_args, **_kwargs):
            raise OSError("unavailable")
        monkeypatch.setattr(store, "create", fail_create)
    result = asyncio.run(middleware.on_call_tool(
        MiddlewareContext(message=mt.CallToolRequestParams(name="update_adset", arguments={})),
        call_next,
    ))
    assert result.is_error is is_error
    assert result.meta["mutation_outcome_unknown"] is True
    assert result.structured_content["overflow"] == result.meta["overflow"]
    assert len(overflow._serialized_mcp_result(result)) <= 64_000
    if not archive_fails:
        archived = _read_complete(store, result.meta["overflow"]["export_id"])
        assert archived["tool_result"]["is_error"] is is_error
        assert archived["tool_result"]["meta"]["mutation_outcome_unknown"] is True
