"""Version-aware Meta Graph API compatibility tests."""

from __future__ import annotations

import pytest

from meta_ads_mcp.api_compat import (
    is_api_version_at_least,
    validate_insights_fields,
    validate_targeting_placements,
)
from meta_ads_mcp.errors import ValidationError


@pytest.mark.parametrize(
    ("api_version", "expected"),
    [
        ("v25.0", False),
        ("26.0", True),
        ("v26.0", True),
        ("v27.0", True),
        ("latest", False),
    ],
)
def test_is_api_version_at_least_v26(api_version: str, expected: bool) -> None:
    assert is_api_version_at_least((26, 0), api_version=api_version) is expected


@pytest.mark.parametrize(
    ("targeting", "message"),
    [
        (
            {"instagram_positions": ["stream", "explore"]},
            "instagram_positions=explore",
        ),
        (
            {"messenger_positions": "story"},
            "messenger_positions=story",
        ),
    ],
)
def test_validate_targeting_placements_rejects_v26_removals(
    targeting: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_targeting_placements(targeting, api_version="v26.0")


def test_validate_targeting_placements_preserves_v25_and_supported_explore() -> None:
    removed_in_v26 = {
        "instagram_positions": ["explore"],
        "messenger_positions": ["story"],
    }
    validate_targeting_placements(removed_in_v26, api_version="v25.0")
    validate_targeting_placements(
        {"instagram_positions": ["explore_home"]},
        api_version="v26.0",
    )


def test_validate_targeting_placements_does_not_mutate_payload() -> None:
    targeting = {"instagram_positions": ["STREAM", "reels"]}
    validate_targeting_placements(targeting, api_version="v26.0")
    assert targeting == {"instagram_positions": ["STREAM", "reels"]}


@pytest.mark.parametrize(
    "field",
    [
        "marketing_messages_website_add_to_cart",
        "marketing_messages_website_initiate_checkout",
        "marketing_messages_website_purchase",
        "marketing_messages_website_purchase_values",
    ],
)
def test_validate_insights_fields_rejects_v26_removed_metrics(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        validate_insights_fields(["spend", field], api_version="v26.0")


def test_validate_insights_fields_preserves_v25_behavior() -> None:
    validate_insights_fields(
        ["marketing_messages_website_purchase"],
        api_version="v25.0",
    )
