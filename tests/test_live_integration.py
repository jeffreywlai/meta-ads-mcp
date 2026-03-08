"""Env-gated live integration tests for real Meta account scenarios."""

from __future__ import annotations

import asyncio
import os

import pytest

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.tools import diagnostics, discovery, insights, recommendations


pytestmark = pytest.mark.skipif(
    os.getenv("META_RUN_LIVE_TESTS") != "1",
    reason="Set META_RUN_LIVE_TESTS=1 and the required META_LIVE_* env vars to run live Meta tests.",
)


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is not configured for live integration tests.")
    return value


def _use_token(monkeypatch: pytest.MonkeyPatch, env_name: str) -> None:
    monkeypatch.setenv("META_ACCESS_TOKEN", _env(env_name))
    reload_settings()


def test_live_read_only_account_with_no_spend_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_NO_SPEND_ACCOUNT_ID")
    result = asyncio.run(diagnostics.get_account_optimization_snapshot(account_id=account_id))
    assert result["scope"]["object_id"] == account_id


def test_live_active_spend_account_insights(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    result = asyncio.run(insights.get_entity_insights(level="account", object_id=account_id, date_preset="last_7d"))
    assert result["summary"]["count"] >= 0


def test_live_ads_read_token_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    from meta_ads_mcp.tools import auth_tools

    info = asyncio.run(auth_tools.validate_token())
    assert "ads_read" in (info.get("scopes") or [])


def test_live_ads_management_token_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    from meta_ads_mcp.tools import auth_tools

    info = asyncio.run(auth_tools.validate_token())
    assert "ads_management" in (info.get("scopes") or [])


def test_live_account_pages_and_instagram_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    pages = asyncio.run(discovery.get_account_pages(account_id=account_id))
    instagram_accounts = asyncio.run(discovery.list_instagram_accounts(account_id=account_id))
    assert pages["summary"]["count"] >= 0
    assert instagram_accounts["summary"]["count"] >= 0


def test_live_recommendations_unsupported_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_UNSUPPORTED_RECOMMENDATIONS_ACCOUNT_ID")
    result = asyncio.run(recommendations.get_recommendations(account_id=account_id))
    assert "supported" in result
