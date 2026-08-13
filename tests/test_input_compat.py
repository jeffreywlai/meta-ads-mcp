"""Shared compatibility-boundary tests."""

from __future__ import annotations

import pytest

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.input_compat import (
    canonical_tool_name,
    normalize_tool_arguments,
    resolve_identifier_alias,
    resolve_tool_name,
)


def test_tool_name_aliases_resolve_without_duplicating_catalog_names() -> None:
    assert canonical_tool_name("list_ad_sets") == "list_adsets"
    assert canonical_tool_name("get_ad_set") == "get_adset"
    assert resolve_tool_name(None, "delete_ad_set") == "delete_adset"
    assert resolve_tool_name("list_adsets", "list_ad_sets") == "list_adsets"


def test_proxy_name_inputs_reject_conflicts_and_blanks() -> None:
    with pytest.raises(ValidationError, match="must match"):
        resolve_tool_name("list_ads", "list_adsets")
    with pytest.raises(ValidationError, match="Provide name or tool_name"):
        resolve_tool_name(" ", None)


def test_tool_arguments_accept_objects_and_stringified_objects() -> None:
    assert normalize_tool_arguments({"limit": 2}) == {"limit": 2}
    assert normalize_tool_arguments('{"limit":2}') == {"limit": 2}
    assert normalize_tool_arguments(None) == {}


@pytest.mark.parametrize("arguments", ["[1,2]", '"value"', "not-json"])
def test_tool_arguments_reject_non_object_json(arguments: str) -> None:
    with pytest.raises(ValidationError, match="object"):
        normalize_tool_arguments(arguments)


def test_identifier_aliases_are_conflict_aware() -> None:
    assert resolve_identifier_alias(
        "act_1",
        "act_1",
        primary_name="account_id",
        alias_name="object_id",
    ) == "act_1"
    with pytest.raises(ValidationError, match="must match"):
        resolve_identifier_alias(
            "act_1",
            "act_2",
            primary_name="account_id",
            alias_name="object_id",
        )
