"""Application error types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class MetaAdsError(RuntimeError):
    """Base application error."""


class ConfigError(MetaAdsError):
    """Raised when required config is missing."""


class AuthError(MetaAdsError):
    """Raised for auth failures."""


class ValidationError(MetaAdsError):
    """Raised for invalid user input."""


class NotFoundError(MetaAdsError):
    """Raised for missing entities."""


class RateLimitError(MetaAdsError):
    """Raised for throttling."""


class AsyncJobError(MetaAdsError):
    """Raised for async insights/report issues."""


class UnsupportedFeatureError(MetaAdsError):
    """Raised when a Meta surface is unavailable or unsupported."""


@dataclass(slots=True)
class MetaApiError(MetaAdsError):
    """Structured Graph API error."""

    message: str
    status_code: int | None = None
    code: int | None = None
    subcode: int | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        """Return the primary Meta error message for user-facing tool failures."""
        return self.message

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        status_code: int | None = None,
    ) -> "MetaApiError":
        """Build an error from a Graph API payload."""
        error = payload.get("error", payload)
        return cls(
            message=error.get("message", "Unknown Meta API error"),
            status_code=status_code,
            code=error.get("code"),
            subcode=error.get("error_subcode"),
            details=payload,
        )
