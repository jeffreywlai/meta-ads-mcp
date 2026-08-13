"""Version-aware compatibility checks for Meta Graph API contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.errors import ValidationError


_API_VERSION_PATTERN = re.compile(r"^v?(\d+)(?:\.(\d+))?$", re.IGNORECASE)

V26_REMOVED_INSIGHTS_FIELDS = frozenset(
    {
        "marketing_messages_website_add_to_cart",
        "marketing_messages_website_initiate_checkout",
        "marketing_messages_website_purchase",
        "marketing_messages_website_purchase_values",
    }
)


def _parse_api_version(api_version: str) -> tuple[int, int] | None:
    """Parse a Graph API version such as ``v26.0`` for comparisons."""
    match = _API_VERSION_PATTERN.fullmatch(api_version.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def is_api_version_at_least(
    required: tuple[int, int],
    *,
    api_version: str | None = None,
) -> bool:
    """Return whether the configured, parseable Graph API version meets a floor."""
    parsed = _parse_api_version(api_version or get_settings().api_version)
    return parsed is not None and parsed >= required


def _normalized_string_values(value: Any) -> set[str]:
    """Return lowercase string values without changing the caller's payload."""
    if isinstance(value, str):
        return {value.strip().lower()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {
            item.strip().lower()
            for item in value
            if isinstance(item, str)
        }
    return set()


def validate_targeting_placements(
    targeting: Mapping[str, Any],
    *,
    api_version: str | None = None,
) -> None:
    """Reject placements removed or silently discarded by Graph API v26+."""
    if not is_api_version_at_least((26, 0), api_version=api_version):
        return

    unsupported: list[str] = []
    instagram_positions = _normalized_string_values(
        targeting.get("instagram_positions")
    )
    if "explore" in instagram_positions:
        unsupported.append(
            "instagram_positions=explore (Instagram Explore Feed was removed)"
        )

    messenger_positions = _normalized_string_values(
        targeting.get("messenger_positions")
    )
    if "story" in messenger_positions:
        unsupported.append(
            "messenger_positions=story (Messenger Stories would be silently removed)"
        )

    if unsupported:
        raise ValidationError(
            "Targeting includes placements unsupported by Meta Graph API v26.0+: "
            f"{'; '.join(unsupported)}. Remove those placements or use another placement."
        )


def validate_insights_fields(
    fields: Iterable[str],
    *,
    api_version: str | None = None,
) -> None:
    """Reject Insights metrics removed from Graph API v26 before an API call."""
    if not is_api_version_at_least((26, 0), api_version=api_version):
        return

    removed = sorted(
        {
            field.strip()
            for field in fields
            if field.strip() in V26_REMOVED_INSIGHTS_FIELDS
        }
    )
    if removed:
        raise ValidationError(
            "Meta Graph API v26.0+ removed these Insights fields: "
            f"{', '.join(removed)}. Remove them from fields before retrying."
        )
