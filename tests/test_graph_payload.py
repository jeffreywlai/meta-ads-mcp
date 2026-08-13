"""Safe Graph mutation payload composition tests."""

from __future__ import annotations

import pytest

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_payload import OMIT, add_validate_only, merge_graph_payload


def test_extra_graph_fields_extend_but_do_not_override_typed_fields() -> None:
    assert merge_graph_payload(
        {"name": "Typed", "status": "PAUSED"},
        {"custom": 1},
    ) == {"name": "Typed", "status": "PAUSED", "custom": 1}

    with pytest.raises(ValidationError, match="cannot override typed fields"):
        merge_graph_payload(
            {"name": "Typed"},
            {"name": "Override"},
        )

    with pytest.raises(ValidationError, match="cannot override typed fields"):
        merge_graph_payload({"name": "Typed"}, {"name": "Typed"})


def test_extra_graph_fields_cannot_supply_omitted_tool_managed_fields() -> None:
    with pytest.raises(ValidationError, match="daily_budget"):
        merge_graph_payload(
            {
                "name": "Campaign",
                "daily_budget": OMIT,
            },
            {"daily_budget": 25},
        )

    assert merge_graph_payload(
        {"name": "Campaign", "daily_budget": OMIT},
        {"custom_extension": 1},
    ) == {"name": "Campaign", "custom_extension": 1}


def test_extra_graph_fields_cannot_inject_transport_credentials() -> None:
    with pytest.raises(ValidationError, match="transport-managed"):
        merge_graph_payload({}, {"access_token": "secret"})

    with pytest.raises(ValidationError, match="transport-managed"):
        merge_graph_payload({}, {"execution_options": ["validate_only"]})


def test_validate_only_is_a_typed_execution_option() -> None:
    payload: dict[str, object] = {"name": "Campaign"}
    add_validate_only(payload, validate_only=True)
    assert payload["execution_options"] == ["validate_only"]

    add_validate_only(payload, validate_only=False)
    assert payload["execution_options"] is OMIT
    assert merge_graph_payload(payload, {}) == {"name": "Campaign"}
