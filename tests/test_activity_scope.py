"""Shared change-history scope contract tests."""

from __future__ import annotations

import pytest

from meta_ads_mcp.activity_scope import validate_activity_scope_arguments


@pytest.mark.parametrize(
    "arguments",
    [
        {"object_id": "101"},
        {"level": "campaign", "account_id": "201"},
        {"level": "ad", "campaign_id": "101"},
        {"level": "campaign", "adset_id": "101"},
        {
            "level": "account",
            "object_id": "201",
            "campaign_id": "101",
        },
        {
            "level": "campaign",
            "object_id": "101",
            "campaign_id": "202",
        },
    ],
)
def test_invalid_activity_scope_dependencies(arguments: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        validate_activity_scope_arguments(
            level=arguments.get("level"),
            object_id=arguments.get("object_id"),
            account_id=arguments.get("account_id"),
            campaign_id=arguments.get("campaign_id"),
            adset_id=arguments.get("adset_id"),
            ad_id=arguments.get("ad_id"),
        )


@pytest.mark.parametrize("level", ["campaign", "adset", "ad"])
def test_matching_redundant_activity_scope_is_valid(level: str) -> None:
    result = validate_activity_scope_arguments(
        level=level,
        object_id="101",
        account_id=None,
        campaign_id="101" if level == "campaign" else None,
        adset_id="101" if level == "adset" else None,
        ad_id="101" if level == "ad" else None,
    )
    assert result.level == level
    assert result.object_id == "101"
    assert result.object_alias == (level, "101")


def test_account_scope_normalizes_account_prefix_for_agreement() -> None:
    result = validate_activity_scope_arguments(
        level="account",
        object_id="act_201",
        account_id="201",
        campaign_id=None,
        adset_id=None,
        ad_id=None,
    )
    assert result.level == "account"
