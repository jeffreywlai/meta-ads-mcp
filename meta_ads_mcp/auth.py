"""Authentication helpers."""

from __future__ import annotations

from .config import Settings, get_settings
from .errors import AuthError


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

