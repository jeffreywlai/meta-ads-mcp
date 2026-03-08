"""Runtime configuration for the Meta Ads MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during bootstrap
    load_dotenv = None


if load_dotenv:
    load_dotenv()


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved runtime settings."""

    access_token: str | None
    api_version: str
    default_account_id: str | None
    app_id: str | None
    app_secret: str | None
    log_level: str
    host: str
    port: int
    request_timeout: float
    max_retries: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached runtime settings."""
    return Settings(
        access_token=os.getenv("META_ACCESS_TOKEN"),
        api_version=os.getenv("META_API_VERSION", "v25.0"),
        default_account_id=os.getenv("META_DEFAULT_ACCOUNT_ID"),
        app_id=os.getenv("META_APP_ID"),
        app_secret=os.getenv("META_APP_SECRET"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        host=os.getenv("FASTMCP_HOST", "127.0.0.1"),
        port=int(os.getenv("FASTMCP_PORT", "8000")),
        request_timeout=float(os.getenv("META_REQUEST_TIMEOUT", "30")),
        max_retries=int(os.getenv("META_MAX_RETRIES", "2")),
    )


def reload_settings() -> Settings:
    """Clear and rebuild cached settings for tests."""
    get_settings.cache_clear()
    return get_settings()

