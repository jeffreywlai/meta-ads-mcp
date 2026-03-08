"""Test fixtures."""

from __future__ import annotations

import pytest

from meta_ads_mcp.config import reload_settings


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset cached settings and provide a default token."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "test-token")
    reload_settings()
    yield
    reload_settings()

