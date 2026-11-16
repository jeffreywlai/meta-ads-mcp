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

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.id_formats import (
    LEFT_ALONE,
    LIST_IDS,
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

    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
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


@pytest.mark.parametrize(("tool", "parameter"), CHECKED)
def test_the_input_schema_carries_the_pattern(tool: str, parameter: str) -> None:
    expected, is_list = id_format(tool, parameter)
    schema = TOOLS[tool].parameters["properties"][parameter]
    branches = schema.get("anyOf", [schema])
    strings = [branch for branch in branches if branch.get("type") == "string"]
    arrays = [branch for branch in branches if branch.get("type") == "array"]
    assert strings or arrays
    assert all("pattern" in branch for branch in strings)
    for branch in arrays:
        assert branch["items"].get("pattern") == expected.pattern
    if not is_list:
        assert all(branch["pattern"] == expected.pattern for branch in strings)
        assert "integer" not in json.dumps(schema) and "number" not in json.dumps(schema)
    for branch in strings:
        assert re.fullmatch(branch["pattern"], "1234567890")
        assert not re.fullmatch(branch["pattern"], "Spring sale")
    assert schema.get("description")


def test_a_comma_separated_list_pattern_matches_the_check() -> None:
    schema = TOOLS["detect_auction_overlap"].parameters["properties"]["campaign_ids"]
    text = next(branch for branch in schema["anyOf"] if branch.get("type") == "string")["pattern"]
    for good in ("1234567890", "1, 2", "1,2,3"):
        assert re.fullmatch(text, good)
    for bad in ("1, two", "act_1", "one"):
        assert not re.fullmatch(text, bad)


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
