"""Focused tests for the coordinator-facing FastMCP contract catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from meta_ads_mcp.tool_contracts import build_tool_contracts


@dataclass
class FakeTool:
    name: str
    parameters: object
    annotations: object = None
    tags: object = field(default_factory=set)


CAMPAIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "campaign_id": {"type": "string", "pattern": r"^\d+$"},
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["campaign_id"],
    "additionalProperties": False,
}


def test_builds_compiled_full_and_partial_validators_from_live_schema() -> None:
    contract = build_tool_contracts(
        [FakeTool("get_campaign", CAMPAIGN_SCHEMA)]
    )["get_campaign"]

    assert contract.name == "get_campaign"
    assert contract.schema is CAMPAIGN_SCHEMA
    assert contract.effect == "read"
    assert contract.validator is not None
    assert contract.partial_validator is not None
    assert contract.validator.is_valid({"campaign_id": "123", "limit": 5})
    assert not contract.validator.is_valid({"campaign_id": 123})
    assert not contract.validator.is_valid({"campaign_id": "abc"})
    assert not contract.validator.is_valid({})
    assert contract.partial_validator.is_valid({})
    assert contract.partial_validator.is_valid({"limit": 5})
    assert not contract.partial_validator.is_valid({"limit": 0})
    assert not contract.partial_validator.is_valid({"unknown": True})


@pytest.mark.parametrize(
    ("annotations", "tags", "name", "expected"),
    [
        ({"readOnlyHint": True}, {"write"}, "update_campaign", "read"),
        ({"readOnlyHint": False}, {"read"}, "get_campaign", "write"),
        ({"destructiveHint": True, "readOnlyHint": True}, set(), "get_campaign", "write"),
        (None, {"mutation"}, "get_campaign", "write"),
        (None, {"read-only"}, "update_campaign", "read"),
        (None, set(), "create_campaign", "write"),
        (None, set(), "exchange_code_for_token", "write"),
        (None, set(), "generate_system_user_token", "write"),
        (None, set(), "refresh_to_long_lived_token", "write"),
        (None, set(), "preview_ad", "read"),
        (None, set(), "setup_ab_test", "write"),
        (None, set(), "list_campaigns", "read"),
    ],
)
def test_effect_prefers_annotations_then_tags_then_name_fallback(
    annotations: object,
    tags: object,
    name: str,
    expected: str,
) -> None:
    contract = build_tool_contracts(
        [FakeTool(name, {"type": "object"}, annotations, tags)]
    )[name]

    assert contract.effect == expected


def test_skips_objects_without_a_live_name_and_parameters_schema() -> None:
    contracts = build_tool_contracts(
        [
            FakeTool("", {"type": "object"}),
            FakeTool("missing_schema", None),
            FakeTool("valid", {"type": "object"}),
        ]
    )

    assert set(contracts) == {"valid"}


def test_malformed_schema_has_no_validator_and_cannot_be_treated_as_valid() -> None:
    contract = build_tool_contracts(
        [FakeTool("broken", {"type": "definitely-not-a-json-type"})]
    )["broken"]

    assert contract.validator is None
    assert contract.partial_validator is None


def test_live_catalog_exposes_read_and_write_contracts_with_exact_validation() -> None:
    from meta_ads_mcp.coordinator import mcp_server

    components = mcp_server.local_provider._components.values()
    tools = [component for component in components if getattr(component, "name", None)]
    contracts = build_tool_contracts(tools)

    get_creative = contracts["get_creative"]
    create_campaign = contracts["create_campaign"]
    generate_token = contracts["generate_system_user_token"]
    assert get_creative.effect == "read"
    assert create_campaign.effect == "write"
    assert generate_token.effect == "write"
    assert get_creative.validator.is_valid({"creative_id": "123"})
    assert not get_creative.validator.is_valid({"creative_id": 123})
    assert not get_creative.validator.is_valid({})
    assert create_campaign.validator.is_valid(
        {"account_id": "act_123", "name": "Launch", "objective": "OUTCOME_TRAFFIC"}
    )
    assert not create_campaign.validator.is_valid(
        {"account_id": "act_123", "name": "Launch"}
    )


def test_live_catalog_classifies_every_current_mutation() -> None:
    from meta_ads_mcp.coordinator import mcp_server

    components = mcp_server.local_provider._components.values()
    tools = [component for component in components if getattr(component, "name", None)]
    contracts = build_tool_contracts(tools)

    assert {
        name for name, contract in contracts.items() if contract.effect == "write"
    } == {
        "create_ad",
        "create_ad_creative",
        "create_ad_set",
        "create_async_insights_report",
        "create_campaign",
        "create_custom_audience",
        "create_lookalike_audience",
        "delete_audience",
        "delete_campaign",
        "delete_creative",
        "delete_overflow_artifact",
        "exchange_code_for_token",
        "generate_system_user_token",
        "refresh_to_long_lived_token",
        "set_ad_status",
        "set_adset_status",
        "set_campaign_status",
        "setup_ab_test",
        "update_adset_bid_amount",
        "update_adset_bid_strategy",
        "update_adset_budget",
        "update_campaign",
        "update_campaign_bid_strategy",
        "update_campaign_budget",
        "update_creative",
        "update_custom_audience",
        "upload_creative_asset",
    }
