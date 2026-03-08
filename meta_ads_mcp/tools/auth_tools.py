"""Auth and token helper tools."""

from __future__ import annotations

from urllib.parse import urlencode

from meta_ads_mcp.auth import build_app_access_token, resolve_access_token, resolve_app_credentials
from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ConfigError
from meta_ads_mcp.graph_api import get_graph_api_client


def _resolve_redirect_uri(redirect_uri: str | None) -> str:
    """Resolve redirect_uri from argument or config."""
    resolved = redirect_uri or get_settings().redirect_uri
    if not resolved:
        raise ConfigError("redirect_uri is required unless META_REDIRECT_URI is set.")
    return resolved


@mcp_server.tool()
async def generate_auth_url(
    redirect_uri: str | None = None,
    scopes: list[str] | None = None,
    state: str | None = None,
    app_id: str | None = None,
    response_type: str = "code",
) -> dict[str, str]:
    """Use this only when the user wants to start an interactive Meta OAuth browser flow."""
    resolved_app_id = app_id or get_settings().app_id
    if not resolved_app_id:
        raise ConfigError("app_id is required unless META_APP_ID is set.")
    resolved_redirect_uri = _resolve_redirect_uri(redirect_uri)
    query = {
        "client_id": resolved_app_id,
        "redirect_uri": resolved_redirect_uri,
        "response_type": response_type,
    }
    if scopes:
        query["scope"] = ",".join(scopes)
    if state:
        query["state"] = state
    return {
        "auth_url": f"https://www.facebook.com/{get_settings().api_version}/dialog/oauth?{urlencode(query)}",
        "redirect_uri": resolved_redirect_uri,
        "response_type": response_type,
    }


@mcp_server.tool()
async def exchange_code_for_token(
    code: str,
    redirect_uri: str | None = None,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> dict[str, object]:
    """Use this after Meta redirects back with a code and the user needs to exchange it for a token."""
    resolved_app_id, resolved_app_secret = resolve_app_credentials(
        app_id=app_id,
        app_secret=app_secret,
    )
    client = get_graph_api_client()
    return await client.oauth_access_token(
        {
            "client_id": resolved_app_id,
            "client_secret": resolved_app_secret,
            "redirect_uri": _resolve_redirect_uri(redirect_uri),
            "code": code,
        }
    )


@mcp_server.tool()
async def refresh_to_long_lived_token(
    access_token: str | None = None,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> dict[str, object]:
    """Use this when the user already has a short-lived Meta token and wants a longer-lived replacement."""
    resolved_app_id, resolved_app_secret = resolve_app_credentials(
        app_id=app_id,
        app_secret=app_secret,
    )
    client = get_graph_api_client()
    return await client.oauth_access_token(
        {
            "grant_type": "fb_exchange_token",
            "client_id": resolved_app_id,
            "client_secret": resolved_app_secret,
            "fb_exchange_token": resolve_access_token(access_token),
        }
    )


@mcp_server.tool()
async def generate_system_user_token(
    system_user_id: str,
    scope: list[str],
    business_app: str | None = None,
    access_token: str | None = None,
    app_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user explicitly needs a business system-user token rather than a user token."""
    resolved_business_app = business_app or app_id or get_settings().app_id
    if not resolved_business_app:
        raise ConfigError("business_app or app_id is required for generate_system_user_token.")
    client = get_graph_api_client(access_token_override=access_token)
    return await client.generate_system_user_token(
        system_user_id,
        business_app=resolved_business_app,
        scope=scope,
        access_token=access_token or resolve_access_token(),
    )


@mcp_server.tool()
async def get_token_info(
    input_token: str | None = None,
    debug_access_token: str | None = None,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> dict[str, object]:
    """Use this when the user wants raw token metadata such as scopes, expiry, or app binding."""
    client = get_graph_api_client()
    effective_debug_access_token = debug_access_token
    if effective_debug_access_token is None and (app_id or app_secret or get_settings().app_id):
        effective_debug_access_token = build_app_access_token(app_id=app_id, app_secret=app_secret)
    payload = await client.debug_token(
        input_token=resolve_access_token(input_token),
        debug_access_token=effective_debug_access_token,
    )
    return payload.get("data", payload)


@mcp_server.tool()
async def validate_token(
    input_token: str | None = None,
    debug_access_token: str | None = None,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> dict[str, object]:
    """Use this for a compact token validity check before trying account reads or writes."""
    info = await get_token_info(
        input_token=input_token,
        debug_access_token=debug_access_token,
        app_id=app_id,
        app_secret=app_secret,
    )
    return {
        "is_valid": bool(info.get("is_valid")),
        "app_id": info.get("app_id"),
        "type": info.get("type"),
        "expires_at": info.get("expires_at"),
        "scopes": info.get("scopes"),
        "granular_scopes": info.get("granular_scopes"),
        "raw": info,
    }
