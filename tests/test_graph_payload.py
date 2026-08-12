"""Safe Graph mutation payload composition tests."""

from __future__ import annotations

import pytest

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_payload import add_validate_only, merge_graph_payload


def test_extra_graph_fields_extend_but_do_not_override_typed_fields() -> None:
    assert merge_graph_payload(
        {"name": "Typed", "status": "PAUSED"},
        {"name": "Typed", "custom": 1},
    ) == {"name": "Typed", "status": "PAUSED", "custom": 1}

    with pytest.raises(ValidationError, match="cannot override typed fields"):
        merge_graph_payload({"name": "Typed"}, {"name": "Override"})


def test_extra_graph_fields_cannot_inject_transport_credentials() -> None:
    with pytest.raises(ValidationError, match="transport-managed"):
        merge_graph_payload({}, {"access_token": "secret"})


def test_validate_only_is_a_typed_execution_option() -> None:
    payload: dict[str, object] = {"name": "Campaign"}
    add_validate_only(payload, validate_only=True)
    assert payload["execution_options"] == ["validate_only"]
