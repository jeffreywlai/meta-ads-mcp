"""Bounded contract tests for deterministic tool-search routing."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from jsonschema import Draft202012Validator

from meta_ads_mcp.intent_routing import (
    MAX_QUERY_CHARS,
    StructuredIntentRouter,
    filter_compatible_names,
)


@dataclass(frozen=True)
class Contract:
    name: str
    schema: dict[str, object]
    effect: str

    @property
    def validator(self) -> Draft202012Validator:
        return Draft202012Validator(self.schema)

    @property
    def partial_validator(self) -> Draft202012Validator:
        return self.validator


def _contract(
    name: str,
    *,
    effect: str = "read",
    required: tuple[str, ...] = (),
    properties: dict[str, object] | None = None,
) -> Contract:
    return Contract(
        name=name,
        effect=effect,
        schema={
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    )


CONTRACTS = {
    "get_campaign": _contract(
        "get_campaign",
        required=("campaign_id",),
        properties={
            "campaign_id": {"type": "string"},
            "include_summary": {"type": "boolean"},
        },
    ),
    "list_campaigns": _contract("list_campaigns"),
    "get_creative": _contract(
        "get_creative",
        required=("creative_id",),
        properties={"creative_id": {"type": "string"}},
    ),
    "list_creatives": _contract("list_creatives"),
    "delete_campaign": _contract(
        "delete_campaign",
        effect="write",
        required=("campaign_id",),
        properties={"campaign_id": {"type": "string"}},
    ),
    "delete_creative": _contract(
        "delete_creative",
        effect="write",
        required=("creative_id",),
        properties={"creative_id": {"type": "string"}},
    ),
    "create_campaign": _contract(
        "create_campaign",
        effect="write",
        required=("account_id",),
        properties={"account_id": {"type": "string"}},
    ),
    "set_campaign_status": _contract(
        "set_campaign_status",
        effect="write",
        required=("campaign_id", "status"),
        properties={
            "campaign_id": {"type": "string"},
            "status": {"enum": ["ACTIVE", "PAUSED"]},
        },
    ),
}
ROUTER = StructuredIntentRouter(tool_contracts=CONTRACTS)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_every_live_canonical_name_routes_exactly(name: str) -> None:
    decision = ROUTER.decide(f"find tool {name}")
    assert decision.preferred_tool == name
    assert decision.compatible_tools == {name}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("get campaign 123", "get_campaign"),
        ("show the creative 123", "get_creative"),
        ("list campaigns", "list_campaigns"),
        ("show creatives", "list_creatives"),
        ("delete creative 123", "delete_creative"),
        ("create campaign", "create_campaign"),
        ("pause campaign 123", "set_campaign_status"),
        ("set campaign status to paused", "set_campaign_status"),
    ],
)
def test_bounded_direct_english_commands(query: str, expected: str) -> None:
    decision = ROUTER.decide(query)
    assert decision.preferred_tool == expected
    assert decision.compatible_tools == {expected}


def test_direct_negation_never_exposes_the_write() -> None:
    decision = ROUTER.decide("do not delete campaign 123")
    assert decision.preferred_tool is None
    assert "delete_campaign" in decision.excluded_tools
    assert filter_compatible_names(CONTRACTS, decision) == [
        "get_campaign",
        "list_campaigns",
        "get_creative",
        "list_creatives",
    ]


def test_unknown_prose_falls_back_to_reads_only() -> None:
    decision = ROUTER.decide("campaign performance from last quarter")
    assert decision.preferred_tool is None
    assert decision.suppress_mutations is True
    assert not {
        name
        for name in decision.compatible_tools
        if CONTRACTS[name].effect == "write"
    }
    assert decision.compatible_tools == {
        "get_campaign",
        "list_campaigns",
        "get_creative",
        "list_creatives",
    }


def test_semicolon_and_newline_preserve_direct_operation_order() -> None:
    decision = ROUTER.decide(
        "delete creative 123; get campaign 456\nshow creatives"
    )
    assert decision.preferred_tool == "delete_creative"
    assert decision.additional_preferred_tools == (
        "get_campaign",
        "list_creatives",
    )
    assert decision.compatible_tools == {
        "delete_creative",
        "get_campaign",
        "list_creatives",
    }


def test_valid_exact_call_uses_the_live_schema() -> None:
    delete = ROUTER.decide('delete_campaign(campaign_id="123")')
    boolean = ROUTER.decide(
        'get_campaign(campaign_id="ABC", include_summary=True)'
    )

    assert delete.preferred_tool == "delete_campaign"
    assert delete.compatible_tools == {"delete_campaign"}
    assert boolean.preferred_tool == "get_campaign"
    assert boolean.compatible_tools == {"get_campaign"}


@pytest.mark.parametrize(
    "query",
    [
        "delete_campaign()",
        "delete_campaign(campaign_id=123)",
        "delete_campaign(campaign_id=123",
        'delete_campaign(campaign_id="123", extra=True)',
        'delete_campaign("123")',
        'delete_campaign(campaign_id=get_secret())',
    ],
)
def test_invalid_exact_calls_fail_closed(query: str) -> None:
    decision = ROUTER.decide(query)
    assert decision.preferred_tool is None
    assert decision.compatible_tools == frozenset()
    assert decision.frame.invalid_exact_call is True


def test_bare_canonical_name_does_not_require_arguments() -> None:
    decision = ROUTER.decide("use delete_campaign")
    assert decision.preferred_tool == "delete_campaign"


def test_exact_calls_are_literal_only() -> None:
    decision = ROUTER.decide(
        'get_campaign(campaign_id="123" + suffix)'
    )
    assert decision.compatible_tools == frozenset()


def test_exact_calls_reject_non_json_python_literals() -> None:
    contract = _contract(
        "update_campaign",
        effect="write",
        required=("daily_budget",),
        properties={"daily_budget": {"type": "number"}},
    )
    router = StructuredIntentRouter(
        tool_contracts={"update_campaign": contract}  # type: ignore[arg-type]
    )

    decision = router.decide("update_campaign(daily_budget=1j)")

    assert decision.preferred_tool is None
    assert decision.frame.invalid_exact_call is True


def test_semicolon_inside_literal_cannot_bypass_exact_call_schema() -> None:
    contract = _contract(
        "create_campaign",
        effect="write",
        required=("account_id", "name", "objective"),
        properties={
            "account_id": {"type": "string"},
            "name": {"type": "string"},
            "objective": {"type": "string"},
        },
    )
    router = StructuredIntentRouter(
        tool_contracts={"create_campaign": contract}  # type: ignore[arg-type]
    )

    decision = router.decide(
        'create_campaign(account_id=123, name="Summer; Sale", '
        'objective="OUTCOME_TRAFFIC")'
    )

    assert decision.preferred_tool is None
    assert decision.frame.invalid_exact_call is True


def test_length_guard_is_read_safe() -> None:
    decision = ROUTER.decide("delete_campaign " * MAX_QUERY_CHARS)
    assert decision.preferred_tool is None
    assert decision.suppress_mutations is True
    assert not {
        name
        for name in decision.compatible_tools
        if CONTRACTS[name].effect == "write"
    }


def test_catalog_is_taken_from_live_contracts() -> None:
    router = StructuredIntentRouter(
        tool_contracts={"future_reader": _contract("future_reader")}  # type: ignore[arg-type]
    )
    decision = router.decide("find tool future_reader")
    assert decision.preferred_tool == "future_reader"
    assert decision.compatible_tools == {"future_reader"}


def test_entity_alias_cannot_inject_a_tool_missing_from_live_catalog() -> None:
    router = StructuredIntentRouter(
        tool_contracts={"future_reader": _contract("future_reader")}  # type: ignore[arg-type]
    )

    decision = router.decide("get campaign 123")

    assert decision.preferred_tool is None
    assert decision.compatible_tools == {"future_reader"}


def test_live_effect_metadata_controls_future_mutation_safety() -> None:
    router = StructuredIntentRouter(
        tool_contracts={
            "future_writer": _contract(
                "future_writer",
                effect="write",
            )
        }  # type: ignore[arg-type]
    )

    fallback = router.decide("unsupported prose")
    explicit = router.decide("find tool future_writer")

    assert fallback.compatible_tools == frozenset()
    assert fallback.suppress_mutations is True
    assert explicit.compatible_tools == {"future_writer"}
    assert explicit.suppress_mutations is False


def test_canonical_name_inside_unsupported_prose_still_routes() -> None:
    decision = ROUTER.decide("find tool get_campaign for this task")
    assert decision.preferred_tool == "get_campaign"


def test_canonical_surface_prefers_ad_set_over_shorter_ad_prefix() -> None:
    contracts = {
        "delete_ad": _contract("delete_ad", effect="write"),
        "delete_adset": _contract("delete_adset", effect="write"),
    }
    router = StructuredIntentRouter(tool_contracts=contracts)  # type: ignore[arg-type]

    decision = router.decide("delete an ad set")

    assert decision.preferred_tool == "delete_adset"
    assert decision.compatible_tools == {"delete_adset"}


def test_unmatched_explicit_write_verb_exposes_only_same_verb_candidates() -> None:
    contracts = {
        "create_ad_set": _contract("create_ad_set", effect="write"),
        "create_campaign": _contract("create_campaign", effect="write"),
        "delete_campaign": _contract("delete_campaign", effect="write"),
        "get_adset": _contract("get_adset"),
    }
    router = StructuredIntentRouter(tool_contracts=contracts)  # type: ignore[arg-type]

    decision = router.decide("create target ROAS ad set with bid constraints")

    assert decision.preferred_tool is None
    assert decision.compatible_tools == {"create_ad_set", "create_campaign"}
    assert "delete_campaign" not in decision.compatible_tools
    assert "get_adset" not in decision.compatible_tools
    assert decision.suppress_mutations is False


def test_write_verb_scope_can_include_explicitly_read_safe_name_exception() -> None:
    contracts = {
        "create_async_insights_report": _contract("create_async_insights_report"),
        "create_campaign": _contract("create_campaign", effect="write"),
        "get_entity_insights": _contract("get_entity_insights"),
    }
    router = StructuredIntentRouter(tool_contracts=contracts)  # type: ignore[arg-type]

    decision = router.decide("create report summary")

    assert decision.compatible_tools == {
        "create_async_insights_report",
        "create_campaign",
    }
    assert "get_entity_insights" not in decision.compatible_tools


def test_create_creative_alias_routes_to_ad_creative_tool() -> None:
    contracts = {
        "create_ad": _contract("create_ad", effect="write"),
        "create_ad_creative": _contract("create_ad_creative", effect="write"),
    }
    router = StructuredIntentRouter(tool_contracts=contracts)  # type: ignore[arg-type]

    decision = router.decide("create creative")

    assert decision.preferred_tool == "create_ad_creative"
    assert decision.compatible_tools == {"create_ad_creative"}
