"""Application error types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_SENSITIVE_ERROR_KEY_PARTS = {
    "access_token",
    "authorization",
    "password",
    "secret",
    "token",
}


def _sanitize_error_data(value: Any) -> Any:
    """Recursively remove credential-like keys from Graph diagnostic data."""
    if isinstance(value, dict):
        return {
            str(key): _sanitize_error_data(candidate)
            for key, candidate in value.items()
            if not any(
                sensitive in str(key).lower()
                for sensitive in _SENSITIVE_ERROR_KEY_PARTS
            )
        }
    if isinstance(value, list):
        return [_sanitize_error_data(item) for item in value]
    return value


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


@dataclass(slots=True)
class RateLimitError(MetaAdsError):
    """Raised for throttling with machine-readable retry guidance."""

    message: str
    retry_after_seconds: float | None = None
    code: int | None = None
    subcode: int | None = None
    usage: dict[str, Any] | None = None
    operation: str | None = None

    def __str__(self) -> str:
        return self.message

    def to_public_dict(self) -> dict[str, Any]:
        """Return safe fields suitable for an MCP error response."""
        payload: dict[str, Any] = {
            "type": "rate_limit",
            "message": self.message,
            "retryable": True,
        }
        for key, value in (
            ("retry_after_seconds", self.retry_after_seconds),
            ("code", self.code),
            ("subcode", self.subcode),
            ("usage", self.usage),
            ("operation", self.operation),
        ):
            if value is not None:
                payload[key] = value
        return payload


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
    user_title: str | None = None
    user_message: str | None = None
    is_transient: bool | None = None
    trace_id: str | None = None
    error_data: dict[str, Any] | None = None
    operation: str | None = None
    retry_after_seconds: float | None = None
    mutation_outcome_unknown: bool = False

    def __str__(self) -> str:
        """Return the primary Meta error message for user-facing tool failures."""
        return self.message

    def to_public_dict(self) -> dict[str, Any]:
        """Return an allowlisted, actionable Graph error payload."""
        payload: dict[str, Any] = {
            "type": "meta_api",
            "message": self.message,
            "retryable": bool(self.is_transient) and not self.mutation_outcome_unknown,
            "mutation_outcome_unknown": self.mutation_outcome_unknown,
        }
        for key, value in (
            ("status_code", self.status_code),
            ("code", self.code),
            ("subcode", self.subcode),
            ("user_title", self.user_title),
            ("user_message", self.user_message),
            ("trace_id", self.trace_id),
            (
                "error_data",
                _sanitize_error_data(self.error_data)
                if self.error_data is not None
                else None,
            ),
            ("operation", self.operation),
            ("retry_after_seconds", self.retry_after_seconds),
        ):
            if value is not None:
                payload[key] = value
        if self.mutation_outcome_unknown:
            payload["next_step"] = (
                "Verify the target object's current state before retrying this mutation."
            )
        return payload

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        status_code: int | None = None,
    ) -> MetaApiError:
        """Build an error from a Graph API payload."""
        raw_error = payload.get("error", payload)
        error = (
            raw_error
            if isinstance(raw_error, dict)
            else {"message": str(raw_error)}
        )
        return cls(
            message=str(error.get("message", "Unknown Meta API error")),
            status_code=status_code,
            code=error.get("code"),
            subcode=error.get("error_subcode"),
            details=payload,
            user_title=error.get("error_user_title"),
            user_message=error.get("error_user_msg"),
            is_transient=error.get("is_transient"),
            trace_id=error.get("fbtrace_id"),
            error_data=error.get("error_data") if isinstance(error.get("error_data"), dict) else None,
        )
