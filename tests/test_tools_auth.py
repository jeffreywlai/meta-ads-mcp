"""Auth helper tool tests."""

from __future__ import annotations

import asyncio
import pytest

from meta_ads_mcp.tools import auth_tools


class FakeAuthClient:
    """Fake auth client."""

    async def oauth_access_token(self, params):
        return {"access_token": "token_123", "params": params}

    async def debug_token(self, *, input_token: str, debug_access_token: str | None = None):
        return {
            "data": {
                "is_valid": True,
                "app_id": "123",
                "type": "USER",
                "scopes": ["ads_management"],
                "input_token": input_token,
                "debug_access_token": debug_access_token,
            }
        }

    async def generate_system_user_token(self, system_user_id: str, *, business_app: str, scope, access_token: str | None = None):
        return {
            "access_token": "system_token",
            "system_user_id": system_user_id,
            "business_app": business_app,
            "scope": scope,
            "access_token_used": access_token,
        }


def test_generate_auth_url_uses_env_defaults(monkeypatch) -> None:
    monkeypatch.setenv("META_APP_ID", "123")
    monkeypatch.setenv("META_REDIRECT_URI", "https://example.com/callback")
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    result = asyncio.run(auth_tools.generate_auth_url(scopes=["ads_management"], state="abc"))
    assert "client_id=123" in result["auth_url"]
    assert "state=abc" in result["auth_url"]


def test_validate_token_returns_boolean_status(monkeypatch) -> None:
    monkeypatch.setattr(auth_tools, "get_graph_api_client", lambda *args, **kwargs: FakeAuthClient())
    result = asyncio.run(auth_tools.validate_token(input_token="user_token", debug_access_token="debug_token"))
    assert result["is_valid"] is True
    assert result["raw"]["app_id"] == "123"


def test_generate_auth_url_requires_app_id_or_env(monkeypatch) -> None:
    monkeypatch.delenv("META_APP_ID", raising=False)
    from meta_ads_mcp.config import reload_settings

    reload_settings()
    with pytest.raises(auth_tools.ConfigError):
        asyncio.run(auth_tools.generate_auth_url(redirect_uri="https://example.com/callback"))
