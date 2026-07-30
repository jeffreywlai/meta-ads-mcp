"""Oversized MCP response artifact storage tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
import pytest

from meta_ads_mcp.errors import NotFoundError, ValidationError
from meta_ads_mcp import overflow
from meta_ads_mcp.overflow import OverflowArtifactStore


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


def test_read_and_delete_reject_replaced_export_directory(tmp_path: Path) -> None:
    export_directory = tmp_path / "exports"
    store = OverflowArtifactStore(export_directory)
    export_id, _ = store.create(_result(), tool_name="example_tool")
    original_directory = tmp_path / "original"
    export_directory.rename(original_directory)

    attacker_directory = tmp_path / "attacker"
    attacker_directory.mkdir()
    attacker_file = attacker_directory / f"{export_id}.json"
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
