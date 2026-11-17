"""Ids that Meta defines as numbers must be digits (meta_ads_mcp/id_formats.py).

Every id-carrying parameter of every tool is either checked or listed as left
alone with a reason. A malformed id is refused before any request is built, with
a message that names the parameter and never repeats the value. Every valid form,
and every id class left alone, is still accepted.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import Tool
from jsonschema.validators import validator_for

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.id_formats import (
    LEFT_ALONE,
    LIST_IDS,
    NESTED_IDS,
    SINGLE_IDS,
    TOOL_OVERRIDES,
    check_id_arguments,
    id_format,
)

TOOLS = {
    tool.name: tool
    for tool in mcp_server.local_provider._components.values()
    if isinstance(getattr(tool, "parameters", None), dict)
}
# A parameter whose name says it carries an id.
ID_NAME = re.compile(r"(_id|_ids|_oids|_id_blocks)$|^(uid|hashes|after)$")

CHECKED = sorted(
    (tool, parameter)
    for tool, definition in TOOLS.items()
    for parameter in definition.parameters.get("properties", {})
    if id_format(tool, parameter) is not None
)
CHECKED_SINGLE = [(tool, parameter) for tool, parameter in CHECKED if not id_format(tool, parameter)[1]]
CHECKED_LISTS = [(tool, parameter) for tool, parameter in CHECKED if id_format(tool, parameter)[1]]

# Text a caller might put in an id field by mistake. It must never come back in a message.
MISPLACED = "Spring sale for jane.doe@example.com"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every HTTP request any tool tries to make; none is sent."""
    requests: list[str] = []

    async def refuse(self, method, url, *args, **kwargs):
        requests.append(f"{method} {url}")
        raise AssertionError("no request may be sent in this test")

    async def refuse_send(self, request, *args, **kwargs):
        requests.append(f"{request.method} {request.url}")
        raise AssertionError("no request may be sent in this test")

    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_send)
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    return requests


def _call(name: str, arguments: dict) -> object:
    return asyncio.run(mcp_server.call_tool(name, arguments))


def test_every_id_parameter_is_checked_or_left_alone_with_a_reason() -> None:
    unclassified = sorted(
        f"{tool}.{parameter}"
        for tool, definition in TOOLS.items()
        for parameter in definition.parameters.get("properties", {})
        if ID_NAME.search(parameter) and id_format(tool, parameter) is None and parameter not in LEFT_ALONE
    )
    assert not unclassified, f"Classify these id parameters in meta_ads_mcp/id_formats.py: {unclassified}"


def test_no_parameter_is_both_checked_and_left_alone() -> None:
    assert not (set(SINGLE_IDS) | set(LIST_IDS)) & set(LEFT_ALONE)
    assert not set(SINGLE_IDS) & set(LIST_IDS)
    for tool, parameter in TOOL_OVERRIDES:
        assert parameter in TOOLS[tool].parameters["properties"]


def test_every_listed_name_is_a_real_parameter() -> None:
    names = {parameter for definition in TOOLS.values() for parameter in definition.parameters.get("properties", {})}
    assert not (set(SINGLE_IDS) | set(LIST_IDS) | set(LEFT_ALONE)) - names


def test_the_check_covers_the_classes_the_design_names() -> None:
    for parameter in (
        "campaign_id", "adset_id", "ad_id", "creative_id", "audience_id", "pixel_id", "dataset_id",
        "business_id", "catalog_id", "product_set_id", "feed_id", "page_id", "instagram_account_id",
        "instagram_media_id", "study_id", "video_id", "system_user_id", "app_id",
    ):
        assert SINGLE_IDS[parameter].pattern == r"^[0-9]+$", parameter
    assert SINGLE_IDS["account_id"].pattern == r"^(act_)?[0-9]+$"
    assert len(CHECKED) > 150


@pytest.mark.parametrize(("tool", "parameter"), CHECKED_SINGLE)
def test_a_malformed_id_is_refused_before_any_request(tool: str, parameter: str, sent: list[str]) -> None:
    with pytest.raises(ToolError) as info:
        _call(tool, {parameter: MISPLACED})
    message = str(info.value)
    assert message.startswith(f"{parameter} must be ")
    assert "digits" in message
    assert "Spring" not in message and "jane" not in message
    assert sent == []


@pytest.mark.parametrize(("tool", "parameter"), CHECKED_LISTS)
def test_a_malformed_id_in_a_list_is_refused_before_any_request(tool: str, parameter: str, sent: list[str]) -> None:
    for value in (["1234567890", MISPLACED], f"1234567890, {MISPLACED}"):
        with pytest.raises(ToolError) as info:
            _call(tool, {parameter: value})
        message = str(info.value)
        assert message.startswith(f"{parameter}[1] must be ")
        assert "Spring" not in message and "jane" not in message
    assert sent == []


def test_a_routed_call_is_refused_the_same_way(sent: list[str]) -> None:
    with pytest.raises(ToolError) as info:
        _call("call_tool", {"name": "get_campaign", "arguments": {"campaign_id": MISPLACED}})
    assert "campaign_id must be a campaign id: digits only" in str(info.value)
    assert "Spring" not in str(info.value)
    with pytest.raises(ToolError) as info:
        _call("call_tool", {"name": "get_ad_set", "arguments": json.dumps({"adset_id": "my ad set"})})
    assert "adset_id must be an ad set id" in str(info.value)
    assert sent == []


@pytest.mark.parametrize(
    "value",
    ["act_1234567890", "1234567890", "1234567890123456"],
)
def test_account_id_takes_digits_or_act_digits_only(value: str) -> None:
    check_id_arguments("get_ad_account", {"account_id": value})
    for wrong in ("act_", "act_12a", "ACT_123", "<account_id>", "123/ads", "my account", "12.5", "-123"):
        with pytest.raises(ValidationError, match="account_id must be an ad account id"):
            check_id_arguments("get_ad_account", {"account_id": wrong})


@pytest.mark.parametrize(("tool", "parameter"), CHECKED_SINGLE)
def test_every_valid_form_of_a_checked_id_is_still_accepted(tool: str, parameter: str) -> None:
    expected, _ = id_format(tool, parameter)
    valid = ["1234567890", "120200000000000001", " 1234567890 ", "", "   ", None]
    if "act_" in expected.pattern:
        valid += ["act_1234567890", " act_1234567890"]
    if "me" in expected.pattern:
        valid.append("me")
    for value in valid:
        check_id_arguments(tool, {parameter: value})


@pytest.mark.parametrize(("tool", "parameter"), CHECKED_LISTS)
def test_every_valid_form_of_a_checked_id_list_is_still_accepted(tool: str, parameter: str) -> None:
    for value in (["1234567890", "120200000000000001"], "1234567890,120200000000000001", "1234567890, 1", [], "", None, ["1", ""]):
        check_id_arguments(tool, {parameter: value})


def test_a_value_of_another_type_is_left_to_the_framework_as_before(sent: list[str]) -> None:
    """A JSON number was refused as 'not a valid string' before this check, and still is."""
    with pytest.raises(ToolError) as info:
        _call("get_campaign", {"campaign_id": 1234567890})
    assert "Input should be a valid string" in str(info.value)
    with pytest.raises(ToolError) as info:
        _call("list_campaigns", {"account_id": 1234567890})
    assert "Input should be a valid string" in str(info.value)
    assert sent == []


REALISTIC_LEFT_ALONE = {
    "image_hash": "8a3f0c6d2b1e4f5a9c7d6e5f4a3b2c1d",
    "hashes": ["8a3f0c6d2b1e4f5a9c7d6e5f4a3b2c1d", "0f1e2d3c4b5a69788796a5b4c3d2e1f0"],
    "comment_id": "1234567890_9876543210",
    "object_story_id": "1234567890_9876543210",
    "export_id": "insights-20261116-abc123",
    "after": "QVFIUmFfZAkJ3MGRTaGxyRzJwbmc4ZAWFuTE1",
    "error_after": "QVFIUmFfZAkJ3MGRTaGxyRzJwbmc4ZAWFuTE1",
    "targeting_after": "QVFIUmFfZAkJ3MGRTaGxyRzJwbmc4ZAWFuTE1",
    "report_run_id": "1234567890123456",
    "retailer_id": "SKU-blue shirt 42",
    "product_id": "1234567890",
    "upload_id": "1234567890",
    "rule_id": "1234567890",
    "interest_ids": ["6003139266461", "6003107902433"],
    "cell_ids": ["1234567890"],
    "owner_id": "act_1234567890",
    "extra_oids": ["1234567890"],
    "uid": "1234567890",
    "time_based_ad_rotation_id_blocks": [["1234567890"]],
}


def test_every_left_alone_class_has_a_realistic_example() -> None:
    assert set(REALISTIC_LEFT_ALONE) == set(LEFT_ALONE)


@pytest.mark.parametrize("parameter", sorted(LEFT_ALONE))
def test_an_id_class_left_alone_is_still_accepted(parameter: str) -> None:
    tools = [tool for tool, definition in TOOLS.items() if parameter in definition.parameters.get("properties", {})]
    assert tools, parameter
    for tool in tools:
        assert id_format(tool, parameter) is None
        check_id_arguments(tool, {parameter: REALISTIC_LEFT_ALONE[parameter]})
        check_id_arguments(tool, {parameter: "not digits at all"})


def test_get_account_pages_still_takes_me(sent: list[str]) -> None:
    check_id_arguments("get_account_pages", {"account_id": "me"})
    check_id_arguments("get_account_pages", {"account_id": "act_1234567890"})
    with pytest.raises(ValidationError, match="or 'me'"):
        check_id_arguments("get_account_pages", {"account_id": "my pages"})
    # "me" is still not an ad account anywhere else.
    with pytest.raises(ValidationError):
        check_id_arguments("list_campaigns", {"account_id": "me"})


def test_a_valid_id_reaches_the_tool(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
    from meta_ads_mcp.tools import discovery, social_feedback

    reads: list[str] = []

    class Reader:
        async def get_object(self, object_id, *, fields=None, params=None):
            reads.append(object_id)
            return {"id": object_id, "name": "Campaign"}

        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            reads.append(f"{parent_id}/{edge}")
            return {"data": []}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: Reader())
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: Reader())
    _call("get_campaign", {"campaign_id": "120200000000000001"})
    _call("list_campaigns", {"account_id": "act_1234567890"})
    _call("list_campaigns", {"account_id": "1234567890"})
    _call("get_account_pages", {})
    _call("get_account_pages", {"account_id": "me"})
    assert reads[:3] == ["120200000000000001", "act_1234567890/campaigns", "act_1234567890/campaigns"]
    assert reads[3:] == ["me/accounts", "me/accounts"]
    assert sent == []


def _schema_without_id_patterns(tool: str) -> dict:
    """The tool's input schema as its own signature makes it, before id patterns are added."""
    return Tool.from_function(TOOLS[tool].fn, name=tool).parameters


RAW_SCHEMAS = {tool: _schema_without_id_patterns(tool) for tool in {tool for tool, _ in CHECKED}}


def _string_patterns(schema: dict) -> list[str]:
    branches = schema.get("anyOf", [schema])
    return [branch["pattern"] for branch in branches if branch.get("type") == "string" and "pattern" in branch]


def _item_patterns(schema: dict) -> list[str]:
    branches = schema.get("anyOf", [schema])
    return [
        branch["items"]["pattern"]
        for branch in branches
        if branch.get("type") == "array" and isinstance(branch.get("items"), dict) and "pattern" in branch["items"]
    ]


@pytest.mark.parametrize(("tool", "parameter"), CHECKED)
def test_the_input_schema_carries_the_pattern(tool: str, parameter: str) -> None:
    expected, is_list = id_format(tool, parameter)
    schema = TOOLS[tool].parameters["properties"][parameter]
    raw = RAW_SCHEMAS[tool]["properties"][parameter]
    branches = schema.get("anyOf", [schema])
    strings = [branch for branch in branches if branch.get("type") == "string"]
    arrays = [branch for branch in branches if branch.get("type") == "array"]
    assert strings or arrays
    assert all("pattern" in branch for branch in strings)
    # A pattern the tool's own type already had is kept; otherwise the one this check adds.
    for pattern, before in zip(_item_patterns(schema), _item_patterns(raw) or [None] * len(arrays)):
        assert pattern == (before or expected.schema_pattern())
    if not is_list:
        for pattern, before in zip(_string_patterns(schema), _string_patterns(raw) or [None] * len(strings)):
            assert pattern == (before or expected.schema_pattern())
        assert "integer" not in json.dumps(schema) and "number" not in json.dumps(schema)
    for branch in strings:
        assert re.fullmatch(branch["pattern"], "1234567890")
        assert not re.fullmatch(branch["pattern"], "Spring sale")
    assert schema.get("description")


def test_a_comma_separated_list_pattern_matches_the_check() -> None:
    schema = TOOLS["detect_auction_overlap"].parameters["properties"]["campaign_ids"]
    text = next(branch for branch in schema["anyOf"] if branch.get("type") == "string")["pattern"]
    for good in ("1234567890", "1, 2", "1,2,3", "", " ", "1,,2", " 1 , 2 ,"):
        assert re.fullmatch(text, good)
    for bad in ("1, two", "act_1", "one", "1 2"):
        assert not re.fullmatch(text, bad)


# Building blocks for every value tried against each checked parameter. The spaces
# include ones str.strip() removes beyond the ASCII space (a tab, a newline, a
# no-break space, an em space, a file separator).
SPACES = ("", " ", "\t", "\n", " ", " ", "\x1c")
WRONG_TOKENS = ("Spring sale", "abc", "act_", "12 34", "1a", "<id>", "1/2", "١٢", "１２", "ACT_1")


def _tokens(expected) -> tuple[str, ...]:
    tokens = ["1234567890", "7"]
    if "act_" in expected.pattern:
        tokens.append("act_1234567890")
    if "me" in expected.pattern:
        tokens.append("me")
    return (*tokens, *WRONG_TOKENS)


def _single_values(expected) -> list[str]:
    return [left + token + right for token in ("", *_tokens(expected)) for left in SPACES for right in SPACES]


def _list_values(expected) -> list[object]:
    pieces = ["", " ", "\t"] + [
        left + token + right for token in _tokens(expected) for left, right in (("", ""), (" ", " "), (" ", ""))
    ]
    lists: list[object] = [[]] + [[piece] for piece in pieces] + [[a, b] for a in pieces[:8] for b in pieces]
    texts = [",", ",,", " , "] + pieces + [f"{a},{b}" for a in pieces for b in pieces[:8]]
    texts += [f"{a},{b},{c}" for a in pieces[:6] for b in pieces[:6] for c in pieces[:6]]
    return lists + texts


_VALIDATORS: dict[str, object] = {}


def _validates(schema: dict, defs: dict, value: object) -> bool:
    key = json.dumps([schema, defs], sort_keys=True)
    if key not in _VALIDATORS:
        whole = {**schema, "$defs": defs} if defs else schema
        _VALIDATORS[key] = validator_for(whole)(whole)
    return _VALIDATORS[key].is_valid(value)


def _accepted_by_the_check(tool: str, parameter: str, value: object) -> bool:
    try:
        check_id_arguments(tool, {parameter: value})
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize(("tool", "parameter"), CHECKED)
def test_the_schema_accepts_exactly_what_the_check_accepts(tool: str, parameter: str) -> None:
    """A value the id check lets through must not fail the advertised schema, or tool search drops the call.

    Only the patterns this check adds are judged: a value the tool's own type already
    refused (a strict id type that predates the check) is left out.
    """
    expected, is_list = id_format(tool, parameter)
    final = TOOLS[tool].parameters
    raw = RAW_SCHEMAS[tool]
    final_property, raw_property = final["properties"][parameter], raw["properties"][parameter]
    values = _list_values(expected) if is_list else _single_values(expected)
    wrongly_refused, wrongly_accepted = [], []
    for value in values:
        if not _validates(raw_property, raw.get("$defs", {}), value):
            continue
        accepted = _accepted_by_the_check(tool, parameter, value)
        valid = _validates(final_property, final.get("$defs", {}), value)
        if accepted and not valid:
            wrongly_refused.append(value)
        if valid and not accepted:
            wrongly_accepted.append(value)
    assert not wrongly_refused, f"{tool}.{parameter}: the schema refuses values the check accepts: {wrongly_refused[:5]}"
    assert not wrongly_accepted, f"{tool}.{parameter}: the schema accepts values the check refuses: {wrongly_accepted[:5]}"


@pytest.mark.parametrize(
    "query",
    [
        'list_campaigns(account_id="")',
        'list_campaigns(account_id=" ")',
        'list_campaigns(account_id=" 1234567890 ")',
        'get_creative(creative_id=" 2222222222 ")',
        'get_ad_image(ad_ids=[" 2222222222 "])',
        'get_ad_image(ad_ids=" 2222222222 , 3333333333 ")',
    ],
)
def test_tool_search_keeps_a_call_with_a_blank_or_padded_id(query: str, sent: list[str]) -> None:
    result = _call("search_tools", {"query": query})
    text = "\n".join(block.text for block in result.content)
    assert f"- `{query.split('(', 1)[0]}`" in text
    assert sent == []


@pytest.mark.parametrize("parameter", sorted(LEFT_ALONE))
def test_the_schema_of_a_class_left_alone_gains_no_pattern(parameter: str) -> None:
    for tool, definition in TOOLS.items():
        schema = definition.parameters.get("properties", {}).get(parameter)
        if schema is None:
            continue
        rendered = json.dumps(schema)
        if parameter in {"rule_id", "time_based_ad_rotation_id_blocks"} and "pattern" in rendered:
            continue  # these types already asked for digits before this check
        assert '"pattern"' not in rendered, f"{tool}.{parameter}"


# Ids a tool reads from inside an object or list argument get the same check, named by path.
_STUDY = {"account_id": "1234567890", "start_time": "2026-12-01T00:00:00+00:00", "end_time": "2026-12-08T00:00:00+00:00"}
_PIXEL_OBJECTIVE = [{"name": "Buy", "adspixels": [{"id": "1234567890", "event_names": ["Purchase"]}]}]
NESTED_CASES = [
    ("create_ad", {"account_id": "1234567890", "name": "Ad", "adset_id": "2222222222", "creative": {"id": MISPLACED}},
     "creative.id"),
    ("create_ad", {"account_id": "1234567890", "name": "Ad", "adset_id": "2222222222",
                   "creative": {"creative_id": MISPLACED}}, "creative.creative_id"),
    ("create_ab_test", {**_STUDY, "name": "Test", "cells": [
        {"name": "A", "treatment_percentage": 50, "campaign_ids": ["1234567890"]},
        {"name": "B", "treatment_percentage": 50, "campaign_ids": f"2222222222, {MISPLACED}"},
    ]}, "cells[1].campaign_ids[1]"),
    ("create_ab_test", {**_STUDY, "name": "Test", "cells": [
        {"name": "A", "treatment_percentage": 50, "adset_ids": [MISPLACED]},
        {"name": "B", "treatment_percentage": 50, "adset_ids": ["2222222222"]},
    ]}, "cells[0].adset_ids[0]"),
    ("create_ab_test", {**_STUDY, "name": "Test", "cells": [
        {"name": "A", "treatment_percentage": 50, "ad_ids": ["1234567890"]},
        {"name": "B", "treatment_percentage": 50, "ad_ids": [MISPLACED]},
    ]}, "cells[1].ad_ids[0]"),
    ("create_lift_study", {**_STUDY, "name": "Lift", "objectives": _PIXEL_OBJECTIVE, "cells": [
        {"name": "A", "treatment_percentage": 50, "control_percentage": 50, "campaign_ids": [MISPLACED]},
    ]}, "cells[0].campaign_ids[0]"),
    ("create_lift_study", {**_STUDY, "name": "Lift", "objectives": _PIXEL_OBJECTIVE, "cells": [
        {"name": "A", "treatment_percentage": 50, "control_percentage": 50, "ad_account_ids": [MISPLACED]},
    ]}, "cells[0].ad_account_ids[0]"),
    ("create_lift_study", {**_STUDY, "name": "Lift", "cells": [
        {"name": "A", "treatment_percentage": 50, "control_percentage": 50},
    ], "objectives": [{"name": "Buy", "adspixels": [{"id": MISPLACED, "event_names": ["Purchase"]}]}]},
     "objectives[0].adspixels[0].id"),
    ("boost_instagram_post", {
        "account_id": "1234567890", "instagram_media_id": "1234567890", "objective": "OUTCOME_SALES",
        "promoted_object": {"pixel_id": MISPLACED, "custom_event_type": "PURCHASE"},
    }, "promoted_object.pixel_id"),
    ("boost_instagram_post", {
        "account_id": "1234567890", "instagram_media_id": "1234567890", "objective": "OUTCOME_APP_PROMOTION",
        "promoted_object": {"application_id": MISPLACED, "object_store_url": "https://example.com/app"},
    }, "promoted_object.application_id"),
]


@pytest.mark.parametrize(("tool", "arguments", "label"), NESTED_CASES)
@pytest.mark.parametrize("routed", [False, True])
def test_an_id_inside_an_object_argument_is_refused_before_any_request(
    tool: str, arguments: dict, label: str, routed: bool, sent: list[str]
) -> None:
    name, call_arguments = ("call_tool", {"name": tool, "arguments": arguments}) if routed else (tool, arguments)
    with pytest.raises(ToolError) as info:
        _call(name, call_arguments)
    message = str(info.value)
    assert message.startswith(f"{label} must be ")
    assert "digits" in message
    assert "Spring" not in message and "jane" not in message
    assert sent == []


# One example argument per nested id leaf, built around the value placed at that leaf.
_NESTED_LEAVES = [
    ("create_ad", "creative", lambda v: {"id": v}, "creative.id"),
    ("create_ad", "creative", lambda v: {"creative_id": v}, "creative.creative_id"),
    ("create_ab_test", "cells", lambda v: [{"campaign_ids": [v]}], "cells[0].campaign_ids[0]"),
    ("create_ab_test", "cells", lambda v: [{"adset_ids": [v]}], "cells[0].adset_ids[0]"),
    ("create_ab_test", "cells", lambda v: [{"ad_ids": [v]}], "cells[0].ad_ids[0]"),
    ("create_lift_study", "cells", lambda v: [{"campaign_ids": [v]}], "cells[0].campaign_ids[0]"),
    ("create_lift_study", "cells", lambda v: [{"ad_account_ids": [v]}], "cells[0].ad_account_ids[0]"),
    *[
        ("create_lift_study", "objectives", lambda v, s=source: [{s: [{"id": v}]}], f"objectives[0].{source}[0].id")
        for source in ("adspixels", "applications", "offsite_datasets", "offline_conversion_data_sets",
                       "product_sets", "product_catalogs")
    ],
    ("boost_instagram_post", "promoted_object", lambda v: {"pixel_id": v}, "promoted_object.pixel_id"),
    ("boost_instagram_post", "promoted_object", lambda v: {"application_id": v}, "promoted_object.application_id"),
]


def test_every_nested_leaf_has_an_example() -> None:
    leaves = {(tool, parameter, len(paths)) for (tool, parameter), paths in NESTED_IDS.items()}
    counted = {(tool, parameter, sum(1 for t, p, _, _ in _NESTED_LEAVES if (t, p) == (tool, parameter)))
               for tool, parameter, _ in leaves}
    assert leaves == counted


@pytest.mark.parametrize(("tool", "parameter", "build", "label"), _NESTED_LEAVES, ids=[leaf[3] + "@" + leaf[0] for leaf in _NESTED_LEAVES])
def test_a_nested_id_of_another_type_is_refused_and_an_integer_is_judged_by_its_digits(
    tool: str, parameter: str, build, label: str
) -> None:
    for wrong in (True, False, 1.5, 2.0, {"misplaced": "Spring sale"}, ["1234567890"], -5):
        with pytest.raises(ValidationError) as info:
            check_id_arguments(tool, {parameter: build(wrong)})
        assert str(info.value).startswith(f"{label} must be ")
        assert "Spring" not in str(info.value) and "misplaced" not in str(info.value)
    for fine in (1234567890, 0, "1234567890", " 1234567890 ", "", None):
        check_id_arguments(tool, {parameter: build(fine)})


def test_a_boolean_lift_source_id_is_refused_before_any_request(sent: list[str]) -> None:
    arguments = {**_STUDY, "name": "Lift", "cells": [{"name": "A", "treatment_percentage": 50, "control_percentage": 50}],
                 "objectives": [{"name": "Buy", "adspixels": [{"id": True, "event_names": ["Purchase"]}]}]}
    for name, call_arguments in (("create_lift_study", arguments),
                                 ("call_tool", {"name": "create_lift_study", "arguments": arguments})):
        with pytest.raises(ToolError) as info:
            _call(name, call_arguments)
        assert str(info.value).startswith("objectives[0].adspixels[0].id must be ")
    assert sent == []


def test_every_form_of_a_nested_id_is_still_accepted() -> None:
    for creative in ({"id": "1234567890"}, {"creative_id": " 1234567890 "}, {"id": ""}, {"creative_id": None},
                     {"id": "1234567890", "creative_id": "1234567890"}, {}, {"other": "not an id"}, "not an object"):
        check_id_arguments("create_ad", {"creative": creative})
    for cells in ([{"campaign_ids": "1, 2"}, {"adset_ids": ["3", " 4 "]}, {"ad_ids": []}], "not a list", [None, "x"]):
        check_id_arguments("create_ab_test", {"cells": cells})
    check_id_arguments("create_lift_study", {
        "cells": [{"campaign_ids": ["1"], "ad_account_ids": ["act_1", "2"]}],
        "objectives": [{"adspixels": [{"id": "1"}], "applications": [{"id": "2"}], "offsite_datasets": [{"id": "3"}],
                        "offline_conversion_data_sets": [{"id": "4"}], "product_sets": [{"id": "5"}],
                        "product_catalogs": [{"id": "6"}], "customconversions": [{"id": "left alone"}]}],
    })
    check_id_arguments("boost_instagram_post", {"promoted_object": {"pixel_id": 1234567890, "page_id": "left alone"}})
    # The same key in an argument no nested rule names is not judged.
    check_id_arguments("preview_ad", {"creative": {"id": "not checked here"}})


def test_each_nested_id_rule_names_a_real_parameter() -> None:
    for tool, parameter in NESTED_IDS:
        assert parameter in TOOLS[tool].parameters["properties"], f"{tool}.{parameter}"


# A parameter that a tool uses as a second name for an id it also takes under the checked name.
SECOND_NAMES = {
    ("generate_system_user_token", "business_app"): "app_id",
    ("get_recommendations", "object_id"): "account_id",
}


@pytest.mark.parametrize(("tool", "parameter"), sorted(SECOND_NAMES))
def test_a_second_name_for_an_id_gets_the_same_check(tool: str, parameter: str) -> None:
    second, first = id_format(tool, parameter), id_format(tool, SECOND_NAMES[(tool, parameter)])
    assert second is not None, f"{tool}.{parameter} is not checked"
    assert second[0].pattern == first[0].pattern


@pytest.mark.parametrize("routed", [False, True])
def test_business_app_is_refused_like_app_id(routed: bool, sent: list[str]) -> None:
    arguments = {"system_user_id": "2222222222", "scope": ["ads_read"], "business_app": MISPLACED}
    name, call_arguments = (
        ("call_tool", {"name": "generate_system_user_token", "arguments": arguments})
        if routed else ("generate_system_user_token", arguments)
    )
    with pytest.raises(ToolError) as info:
        _call(name, call_arguments)
    assert str(info.value).startswith("business_app must be an app id: digits only")
    assert "Spring" not in str(info.value)
    assert sent == []
