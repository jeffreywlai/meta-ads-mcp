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


def _configuration_contracts() -> dict[str, Contract]:
    return {
        name: _contract(name, effect="write")
        for name in (
            "update_campaign", "update_campaign_budget", "update_campaign_bid_strategy",
            "set_campaign_status", "update_adset_budget", "update_adset_bid_amount",
            "update_adset_bid_strategy", "set_adset_status",
        )
    }


def test_compact_daily_apply_query_exposes_each_requested_write() -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide("update ad set budget bid amount status campaign budget bid strategy")

    assert (decision.preferred_tool, *decision.additional_preferred_tools) == (
        "update_adset_budget", "update_adset_bid_amount", "set_adset_status",
        "update_campaign_budget", "update_adset_bid_strategy", "update_campaign_bid_strategy",
    )
    assert "update_campaign" not in decision.compatible_tools
    assert "set_campaign_status" not in decision.compatible_tools


def test_daily_apply_shorthand_is_recognized_with_explicit_write_context() -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide(
        "bid amount cost cap; pause ad set status; campaign daily budget; bid strategy roas floor"
    )

    assert decision.compatible_tools == {
        "update_adset_bid_amount", "set_adset_status", "update_campaign_budget",
        "update_adset_bid_strategy",
    }
    assert router.decide("campaign daily budget; bid strategy roas floor").suppress_mutations


@pytest.mark.parametrize("query", ["update campaign daily budget", "update campaign daily budget CBO", "set campaign lifetime budget"])
def test_budget_intent_selects_dedicated_campaign_budget_tool(query: str) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    assert router.decide(query).compatible_tools == {"update_campaign_budget"}


@pytest.mark.parametrize("query", [
    "do not update campaign daily budget and update adset bid amount",
    "don't update campaign daily budget, update adset bid strategy",
    "never set adset budget bid amount status campaign budget bid strategy",
    "compare campaign budget and bid strategy",
])
def test_negated_and_read_queries_do_not_expose_compound_writes(query: str) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide(query)

    assert decision.suppress_mutations
    assert not decision.compatible_tools


def test_configuration_qualifiers_do_not_turn_avoided_facets_into_changes() -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide("update adset budget without changing status or bid strategy")

    assert decision.compatible_tools == {"update_adset_budget"}


@pytest.mark.parametrize("query", [
    "update campaign budget; bid amount must not change",
    "update campaign budget; bid amount should not change",
    "update campaign budget and name must not change",
    "update campaign budget, objective should not change",
    "update campaign budget without changing name and objective",
    "update campaign budget; bid amount does not change",
    "update campaign budget; bid amount did not change",
    "update campaign budget; bid amount should remain unchanged",
    "update campaign budget; bid amount is unchanged",
    "update campaign budget; bid amount must never change",
    "update campaign budget; bid amount and status must not change",
    "update campaign budget; campaign name and objective must not change",
    "update campaign budget; campaign status and objective should not change",
    "update campaign budget; campaign name and objective should stay unchanged",
    "update campaign budget; bid amount unchanged",
    "update campaign budget; campaign name and objective unchanged",
    "update campaign budget; campaign name needs to stay unchanged",
    "update campaign budget; bid amount, must not change",
    "update campaign budget; bid amount: must not change",
    "update campaign budget; bid amount should remain unchanged and campaign status",
    "update campaign budget to 100 without changing name and objective",
])
def test_negated_continuations_preserve_only_the_requested_configuration(query: str) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    assert router.decide(query).compatible_tools == {"update_campaign_budget"}


@pytest.mark.parametrize("query", [
    "update campaign budget must not change",
    "set adset status should not change",
    "do not update campaign budget and name",
    "compare campaign budget and name",
    "show campaign status and objective",
])
def test_negated_and_read_configuration_fields_never_enable_mutations(query: str) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide(query)

    assert not decision.compatible_tools
    assert decision.suppress_mutations


@pytest.mark.parametrize(("query", "expected"), [
    ("update campaign budget and name", ("update_campaign_budget", "update_campaign")),
    ("update campaign status and objective", ("set_campaign_status", "update_campaign")),
    ("set campaign lifetime budget, name", ("update_campaign_budget", "update_campaign")),
    ("update campaign name and budget", ("update_campaign", "update_campaign_budget")),
    ("update adset budget and campaign name", ("update_adset_budget", "update_campaign")),
    ("update adset budget and bid amount must not change", ("update_adset_budget",)),
    ("update campaign budget to 100 and name", ("update_campaign_budget", "update_campaign")),
    ("update campaign budget to 100, name", ("update_campaign_budget", "update_campaign")),
    ("update campaign budget to 100, and name", ("update_campaign_budget", "update_campaign")),
    ("update campaign status to paused and objective", ("set_campaign_status", "update_campaign")),
    ("update campaign status to paused, objective", ("set_campaign_status", "update_campaign")),
    ('update campaign name to "Winter", budget', ("update_campaign", "update_campaign_budget")),
    ('update campaign name to "Budget and Status" and objective', ("update_campaign",)),
    ("update campaign budget; bid amount shouldn't change; update campaign name", (
        "update_campaign_budget", "update_campaign",
    )),
])
def test_field_continuations_keep_scope_and_select_each_needed_updater(query, expected) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide(query)

    assert (decision.preferred_tool, *decision.additional_preferred_tools) == expected
    assert decision.compatible_tools == set(expected)


@pytest.mark.parametrize("entity", ["adset", "ad set"])
@pytest.mark.parametrize("separator", [" and ", ", ", ", and "])
@pytest.mark.parametrize("prefix_separator", ["; ", ", "])
def test_coordinated_entity_subjects_share_their_preservation_predicate(
    entity: str, separator: str, prefix_separator: str,
) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]
    query = (
        f"update campaign budget{prefix_separator}{entity} bid amount"
        f"{separator}{entity} status must not change"
    )

    assert router.decide(query).compatible_tools == {"update_campaign_budget"}


@pytest.mark.parametrize(("query", "expected"), [
    ("update campaign budget, name and objective must not change", ("update_campaign_budget",)),
    ("update campaign budget and campaign name and campaign objective must not change", ("update_campaign_budget",)),
    ("update campaign name; campaign objective and campaign status must not change", ("update_campaign",)),
    ('update campaign budget and name to "Winter", objective and status must not change', (
        "update_campaign_budget", "update_campaign",
    )),
    ("update campaign budget, name and objective must not change and update adset bid amount", (
        "update_campaign_budget", "update_adset_bid_amount",
    )),
    ("update campaign budget, name and objective must not change, update adset bid amount", (
        "update_campaign_budget", "update_adset_bid_amount",
    )),
    ("update campaign budget; bid amount to 2 and adset status must not change", (
        "update_campaign_budget", "update_adset_bid_amount",
    )),
    ("update campaign budget; adset bid amount to 2, adset status must not change", (
        "update_campaign_budget", "update_adset_bid_amount",
    )),
    ("update campaign budget; bid amount to 2 must not change", ("update_campaign_budget",)),
    ("update campaign budget; bid amount and adset status to paused must not change", ("update_campaign_budget",)),
    ("pause ad set status, campaign budget", ("set_adset_status", "update_campaign_budget")),
    ("update campaign status to paused and campaign budget", ("set_campaign_status", "update_campaign_budget")),
    ("update campaign budget and adset budget and bid strategy", (
        "update_campaign_budget", "update_adset_budget", "update_adset_bid_strategy",
    )),
    ("update adset budget and campaign budget bid strategy", (
        "update_adset_budget", "update_campaign_budget", "update_campaign_bid_strategy",
    )),
])
def test_preservation_subjects_do_not_remove_separate_requested_changes(query, expected) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide(query)

    assert (decision.preferred_tool, *decision.additional_preferred_tools) == expected
    assert decision.compatible_tools == set(expected)


@pytest.mark.parametrize(("query", "expected"), [
    ("show campaign budget and campaign status; update adset budget", (
        "get_campaign", "update_adset_budget",
    )),
    ("update campaign budget; show adset bid amount, adset status", (
        "update_campaign_budget", "get_adset",
    )),
    ("show campaign budget and ad set status; update adset budget", (
        "get_campaign", "get_adset", "update_adset_budget",
    )),
])
def test_coordinated_read_subjects_keep_their_read_verb(query, expected) -> None:
    contracts = {
        **_configuration_contracts(),
        "get_campaign": _contract("get_campaign"),
        "get_adset": _contract("get_adset"),
    }
    router = StructuredIntentRouter(tool_contracts=contracts)  # type: ignore[arg-type]

    decision = router.decide(query)

    assert (decision.preferred_tool, *decision.additional_preferred_tools) == expected
    assert decision.compatible_tools == set(expected)


@pytest.mark.parametrize("value", ["LOWEST_COST_WITHOUT_CAP", '"CUSTOM"'])
def test_bid_strategy_literal_values_allow_a_following_requested_field(value: str) -> None:
    router = StructuredIntentRouter(tool_contracts=_configuration_contracts())  # type: ignore[arg-type]

    decision = router.decide(f'update campaign budget to 100 and bid strategy to {value} and name "Winter"')

    assert (decision.preferred_tool, *decision.additional_preferred_tools) == (
        "update_campaign_budget", "update_campaign_bid_strategy", "update_campaign",
    )


def test_quoted_configuration_values_cannot_select_canonical_tools() -> None:
    contracts = {**_configuration_contracts(), "get_campaign": _contract("get_campaign")}
    router = StructuredIntentRouter(tool_contracts=contracts)  # type: ignore[arg-type]

    decision = router.decide('update campaign name to "get_campaign" and budget to 100')

    assert decision.compatible_tools == {"update_campaign", "update_campaign_budget"}
    assert router.decide('find tool "update_campaign"').compatible_tools == {"update_campaign"}


def test_mixed_creative_workflow_keeps_all_explicit_operations() -> None:
    contracts = {
        "list_ads": _contract("list_ads"),
        "get_ad": _contract("get_ad"),
        "get_creative": _contract("get_creative"),
        "update_ad_creative": _contract("update_ad_creative", effect="write"),
        "upload_creative_asset": _contract("upload_creative_asset", effect="write"),
    }
    router = StructuredIntentRouter(tool_contracts=contracts)  # type: ignore[arg-type]

    decision = router.decide(
        "list ads filter by name, get ad creative details, update ad creative, upload image"
    )

    assert (decision.preferred_tool, *decision.additional_preferred_tools) == (
        "list_ads", "get_creative", "update_ad_creative", "upload_creative_asset",
    )
    assert "get_ad" not in decision.compatible_tools


def test_compound_exact_calls_preserve_literal_delimiters_and_validation() -> None:
    decision = ROUTER.decide(
        'get_campaign(campaign_id="123, and 456"); '
        'delete_creative(creative_id="789")'
    )

    assert decision.compatible_tools == {"get_campaign", "delete_creative"}
    invalid = ROUTER.decide('delete_campaign(campaign_id=123), list_campaigns')
    assert "delete_campaign" not in invalid.compatible_tools
    assert invalid.preferred_tool == "list_campaigns"
    assert invalid.frame.invalid_exact_call
    literal_negation = ROUTER.decide(
        'get_campaign(campaign_id="without") and delete_creative(creative_id="789")'
    )
    assert literal_negation.compatible_tools == {"get_campaign", "delete_creative"}


@pytest.mark.parametrize("query", [
    "summarize async report spend by product_id",
    "aggregate spend by month for report_run_id 123",
])
def test_saved_report_aggregation_routes_to_read_tool(query: str) -> None:
    name = "summarize_async_insights_report"
    router = StructuredIntentRouter(tool_contracts={name: _contract(name)})  # type: ignore[arg-type]

    assert router.decide(query).preferred_tool == name
