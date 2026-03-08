"""Authentication helpers."""

from __future__ import annotations

from .config import Settings, get_settings
from .errors import AuthError, ConfigError


def resolve_access_token(
    override: str | None = None,
    *,
    settings: Settings | None = None,
) -> str:
    """Resolve the Meta access token."""
    if override:
        return override
    resolved = (settings or get_settings()).access_token
    if not resolved:
        raise AuthError(
            "META_ACCESS_TOKEN is required. Set it in the environment before "
            "starting the server."
        )
    return resolved


def build_auth_headers(
    override: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, str]:
    """Build Graph API auth headers."""
    return {"Authorization": f"Bearer {resolve_access_token(override, settings=settings)}"}


def resolve_app_credentials(
    *,
    app_id: str | None = None,
    app_secret: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """Resolve app credentials from explicit args or config."""
    resolved_settings = settings or get_settings()
    resolved_app_id = app_id or resolved_settings.app_id
    resolved_app_secret = app_secret or resolved_settings.app_secret
    if not resolved_app_id or not resolved_app_secret:
        raise ConfigError(
            "META_APP_ID and META_APP_SECRET are required for this operation "
            "unless explicit app credentials are provided."
        )
    return resolved_app_id, resolved_app_secret


def build_app_access_token(
    *,
    app_id: str | None = None,
    app_secret: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Build the app access token used by debug_token and related flows."""
    resolved_app_id, resolved_app_secret = resolve_app_credentials(
        app_id=app_id,
        app_secret=app_secret,
        settings=settings,
    )
    return f"{resolved_app_id}|{resolved_app_secret}"
