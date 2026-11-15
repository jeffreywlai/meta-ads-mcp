"""Test fixtures."""

from __future__ import annotations

import pytest

from meta_ads_mcp.config import reload_settings


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Reset cached settings, provide a default token, and allow uploads from tmp_path only."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("META_UPLOAD_DIR", str(tmp_path))
    reload_settings()
    yield
    reload_settings()

