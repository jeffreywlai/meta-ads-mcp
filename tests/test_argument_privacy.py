"""Argument values never reach FastMCP's validation log, nor a private tool's argument errors.

FastMCP validates declared argument types before a tool runs. On failure it logs
a WARNING carrying the supplied values (for a missing argument, every argument of
the call) and returns an error quoting them. A tool's own checks cannot prevent
that, so these tests drive the real server, directly and through the call_tool
search proxy.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import socket
from pathlib import Path

import anyio
import mcp.types as mcp_types
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as MCPValidationError
from mcp.shared.message import SessionMessage

import meta_ads_mcp.server  # noqa: F401  (registers every tool)
from meta_ads_mcp import argument_privacy
from meta_ads_mcp.argument_privacy import (
    FASTMCP_SERVER_LOGGER,
    PRIVATE_ARGUMENT_TOOLS,
    ValidationLogPrivacyFilter,
    safe_validation_errors,
    schema_parameter_names,
)
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server

SECRET = "synthetic.private.person@example.invalid"


@pytest.fixture(autouse=True)
def offline_writable(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network is prohibited in argument-privacy tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()


def _tools() -> dict:
    return {
        tool.name: tool
        for tool in mcp_server.local_provider._components.values()
        if hasattr(tool, "parameters") and hasattr(tool, "fn")
    }


def _call(name: str, arguments: dict, routed: bool):
    if routed:
        name, arguments = "call_tool", {"name": name, "arguments": arguments}
    return asyncio.run(mcp_server.call_tool(name, arguments))


def _rendered_chain(error: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts += [str(current), repr(current)]
        pending += [current.__cause__, current.__context__]
    return "\n".join(parts)


def _assert_not_logged(caplog, value: str) -> None:
    assert value not in caplog.text
    for record in caplog.records:
        assert value not in record.getMessage()
        assert value not in repr(record.args)
        if record.exc_info:
            assert value not in _rendered_chain(record.exc_info[1])


def _invalid_argument_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if str(record.msg).startswith("Invalid arguments for tool")]


# --- Reviewer reproductions (round two, G2, G3 and G5) ---------------------------


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
@pytest.mark.parametrize("tool", ["add_audience_users", "remove_audience_users"])
def test_g2_customer_value_in_unknown_argument_is_never_echoed(tool, routed, caplog):
    caplog.set_level(logging.DEBUG)
    raw = "synthetic.private.customer@example.invalid"
    with pytest.raises(ToolError) as info:
        _call(tool, {"audience_id": "123", "schema": ["EMAIL"], "rows": [[raw]], raw: "misplaced-column"}, routed)
    assert raw not in _rendered_chain(info.value)
    assert "1 unknown parameter" in str(info.value)
    _assert_not_logged(caplog, raw)


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
def test_g3_business_search_validation_never_echoes_person(routed, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises((ToolError, MCPValidationError)) as info:
        _call("list_business_users", {"search": SECRET}, routed)
    assert SECRET not in _rendered_chain(info.value)
    assert "business_id" in str(info.value)
    assert _invalid_argument_records(caplog), "the framework's warning should still be logged"
    _assert_not_logged(caplog, SECRET)


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
@pytest.mark.parametrize(
    ("tool", "args", "secret"),
    [
        (
            "create_product_feed",
            {"name": "Feed", "schedule": {"url": "https://example.invalid/f.csv", "interval": "DAILY", "password": "synthetic-feed-password"}},
            "synthetic-feed-password",
        ),
        (
            "create_product_feed_upload",
            {"url": "https://example.invalid/f.csv", "username": "feed", "password": "synthetic-feed-password"},
            "synthetic-feed-password",
        ),
        (
            "create_ad_creative_from_inputs",
            {"account_id": "10", "page_id": "20", "sponsor_page_id": "30", "partnership_ad_code": "synthetic-partnership-code"},
            "synthetic-partnership-code",
        ),
        ("upload_ad_video", {"file_path": "/synthetic/private/customer-video.mp4"}, "/synthetic/private/customer-video.mp4"),
    ],
)
def test_g5_missing_arguments_never_log_credentials(tool, args, secret, routed, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises((ToolError, MCPValidationError)) as info:
        _call(tool, args, routed)
    assert secret not in _rendered_chain(info.value)
    assert _invalid_argument_records(caplog)
    _assert_not_logged(caplog, secret)


# --- Every tool: the framework's warning keeps names, never values ----------------


def _json_types(schema: dict) -> set[str] | None:
    """The JSON types a parameter schema accepts, or None when it accepts anything."""
    if "type" in schema:
        kinds = schema["type"]
        return set(kinds if isinstance(kinds, list) else [kinds])
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        found: set[str] = set()
        for option in options:
            kinds = _json_types(option)
            if kinds is None:
                return None
            found |= kinds
        return found
    if "enum" in schema or "const" in schema or "$ref" in schema:
        return {"string", "object"} if "$ref" in schema else {"string"}
    return None


def _value_the_schema_refuses(schema: dict):
    accepted = _json_types(schema)
    if accepted is None:
        return None
    for kind, value in (("object", {"k": SECRET}), ("string", SECRET), ("array", [SECRET])):
        if kind not in accepted:
            return value
    return None


def _failing_arguments(tool) -> dict | None:
    """Arguments that fail the tool's declared types and carry SECRET, or None."""
    properties = tool.parameters.get("properties", {})
    required = list(tool.parameters.get("required", []))
    arguments = {}
    for name, schema in properties.items():
        value = _value_the_schema_refuses(schema)
        arguments[name] = SECRET if value is None else value
    if required:
        # A missing required argument makes Pydantic report the whole mapping as input.
        arguments.pop(required[0])
        return arguments if arguments else {}
    if any(_value_the_schema_refuses(schema) is not None for schema in properties.values()):
        return arguments
    return None


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
def test_every_tool_validation_warning_never_contains_argument_values(routed, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    exercised: list[str] = []
    not_exercisable: list[str] = []
    for name, tool in sorted(_tools().items()):
        arguments = _failing_arguments(tool)
        if arguments is None:
            not_exercisable.append(name)
            continue
        caplog.clear()
        with pytest.raises((ToolError, MCPValidationError)):
            _call(name, arguments, routed)
        records = [record for record in _invalid_argument_records(caplog) if record.args and record.args[0] == name]
        if not records:
            # The tool's own permissive types let the call through to its own checks.
            not_exercisable.append(name)
            continue
        exercised.append(name)
        _assert_not_logged(caplog, SECRET)
    assert len(exercised) >= 150, (len(exercised), not_exercisable)
    # Tools that declare every parameter as "anything" and validate in their own code.
    # The rest take no parameters, or (the customer-list tools) declare every
    # parameter as "anything" and validate in their own code.
    assert set(not_exercisable) <= {"add_audience_users", "remove_audience_users"} | {
        name for name, tool in _tools().items() if not tool.parameters.get("properties")
    }, not_exercisable


def test_the_warning_keeps_parameter_names_types_and_messages(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises((ToolError, MCPValidationError)):
        _call("list_adsets", {"account_id": "act_1", "limit": SECRET}, routed=False)
    (record,) = [record for record in _invalid_argument_records(caplog) if record.args[0] == "list_adsets"]
    (error,) = record.args[1]
    assert error["loc"] == ("limit",)
    assert error["type"] == "int_parsing"
    assert error["msg"].startswith("Input should be a valid integer")
    assert set(error) == {"type", "loc", "msg"}
    assert SECRET not in caplog.text


def test_unknown_names_and_input_derived_keys_are_not_logged():
    errors = [
        {"type": "unexpected_keyword_argument", "loc": (SECRET,), "msg": "Unexpected keyword argument", "input": SECRET},
        {"type": "int_parsing", "loc": ("limits", SECRET), "msg": "Input should be a valid integer", "input": SECRET},
        {"type": "value_error", "loc": ("limit",), "msg": f"Value error, {SECRET} is wrong", "input": SECRET, "ctx": {"error": SECRET}},
        {"type": "union_tag_invalid", "loc": ("spec", 0), "msg": f"Input tag '{SECRET}' found", "input": {}},
        {"type": "my_custom_error", "loc": ("limit",), "msg": f"custom {SECRET}", "input": SECRET},
    ]
    safe = safe_validation_errors(errors, {"limit", "limits", "spec"})
    assert SECRET not in repr(safe)
    assert [error["loc"] for error in safe] == [("<unknown>",), ("limits", "<key>"), ("limit",), ("spec", 0), ("limit",)]


@pytest.mark.parametrize(
    "args",
    [
        ("Invalid arguments for tool %r: %s", "x", f"1 validation error input_value='{SECRET}'"),
        ("Invalid arguments for tool %r: %s", "x", [{"type": "missing_argument", "loc": ("a",), "msg": "Missing", "input": {"b": SECRET}}]),
        ("Invalid arguments for tool %s %s %s", "x", SECRET, "y"),
    ],
)
def test_the_filter_withholds_every_shape_of_the_warning(args):
    record = logging.LogRecord(FASTMCP_SERVER_LOGGER, logging.WARNING, __file__, 1, args[0], args[1:], None)
    assert ValidationLogPrivacyFilter(lambda: {"a"}).filter(record) is True
    assert SECRET not in record.getMessage()
    assert "Invalid arguments for tool" in record.getMessage()


def test_the_filter_survives_a_broken_catalog_lookup():
    def broken():
        raise RuntimeError("catalog unavailable")

    record = logging.LogRecord(
        FASTMCP_SERVER_LOGGER, logging.WARNING, __file__, 1, "Invalid arguments for tool %r: %s",
        ("x", [{"type": "int_parsing", "loc": ("limit",), "msg": "Input should be a valid integer", "input": SECRET}]), None,
    )
    ValidationLogPrivacyFilter(broken).filter(record)
    assert SECRET not in record.getMessage()
    assert record.args[1][0]["loc"] == ("<key>",)


def test_the_filter_sits_on_the_logger_fastmcp_uses_for_the_warning():
    """If FastMCP moves or rewords the warning, this fails instead of the filter silently missing it."""
    import fastmcp
    import fastmcp.server.server as fastmcp_server

    assert fastmcp_server.logger.name == FASTMCP_SERVER_LOGGER
    assert any(
        isinstance(item, ValidationLogPrivacyFilter)
        for item in logging.getLogger(FASTMCP_SERVER_LOGGER).filters
    )
    package = Path(inspect.getfile(fastmcp)).parent
    mentions = {
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if re.search(r"Invalid arguments", path.read_text(encoding="utf-8"))
    }
    assert mentions == {"server/server.py"}, mentions
    source = (package / "server" / "server.py").read_text(encoding="utf-8")
    calls = re.findall(r"logger\.\w+\(\s*\"Invalid arguments[^\"]*\"", source)
    assert calls and all('"Invalid arguments for tool %r: %s"' in call for call in calls), calls


def test_the_catalog_lookup_returns_real_parameter_names():
    names = argument_privacy._catalog_parameter_names(mcp_server)
    assert {"account_id", "business_id", "file_path", "limit"} <= names


# --- Private tools: errors name parameters only, both call paths ------------------


def _private_cases(tool) -> list[tuple[str, dict]]:
    properties = tool.parameters.get("properties", {})
    required = list(tool.parameters.get("required", []))
    cases: list[tuple[str, dict]] = []
    if required:
        others = {name: SECRET for name in properties if name != required[0]}
        cases.append(("missing", others or {}))
    cases.append(("unknown", {**{name: "1" for name in required}, SECRET: SECRET, f"{SECRET}-2": "x"}))
    for name, schema in properties.items():
        value = _value_the_schema_refuses(schema)
        if value is not None:
            cases.append(("wrong_type", {**{key: "1" for key in required}, name: value}))
            break
    return cases


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
@pytest.mark.parametrize("tool_name", sorted(PRIVATE_ARGUMENT_TOOLS))
def test_private_tool_argument_errors_name_parameters_only(tool_name, routed, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    tool = _tools()[tool_name]
    for case, arguments in _private_cases(tool):
        caplog.clear()
        try:
            _call(tool_name, arguments, routed)
        except (ToolError, MCPValidationError) as error:
            rendered = _rendered_chain(error)
        else:
            pytest.fail(f"{case} arguments were accepted")
        assert SECRET not in rendered, (case, rendered)
        _assert_not_logged(caplog, SECRET)
        if case == "unknown":
            assert "2 unknown parameters" in rendered, rendered
            assert "Accepted parameters:" in rendered
        if case == "missing" and tool_name not in {"add_audience_users", "remove_audience_users"}:
            assert tool.parameters["required"][0] in rendered, rendered
            assert "Missing required argument" in rendered, rendered


def test_a_private_tool_wrong_type_error_says_which_parameter(caplog):
    with pytest.raises(ToolError) as info:
        _call("list_business_users", {"business_id": "1", "search": SECRET, "limit": SECRET}, routed=False)
    message = str(info.value)
    assert message.startswith("Invalid arguments for list_business_users: limit: Input should be a valid integer")
    assert "get_capabilities(tool_name='list_business_users')" in message
    assert SECRET not in _rendered_chain(info.value)


def test_other_tools_keep_naming_unknown_parameters_exactly():
    with pytest.raises(ToolError) as info:
        _call("list_adsets", {"account_id": "act_1", "not_a_parameter": True}, routed=False)
    accepted = ", ".join(_tools()["list_adsets"].parameters["properties"])
    assert str(info.value) == (
        "Unknown parameters for list_adsets: not_a_parameter. "
        f"Accepted parameters: {accepted}. "
        "Inspect types and defaults with get_capabilities(tool_name='list_adsets')."
    )


def test_other_tools_keep_the_framework_validation_error():
    with pytest.raises(MCPValidationError) as info:
        _call("list_adsets", {"account_id": "act_1", "limit": "not-a-number"}, routed=False)
    assert "not-a-number" in str(info.value)


# --- Guard: a tool with a secret- or person-shaped parameter must be listed --------

_SENSITIVE_NAME = re.compile(
    r"pass(word)?|passwd|secret|token|credential|username|e?mail|phone"
    r"|^rows$|file_path|partnership_ad_code|^search$|^code$|(^|_)url$",
    re.IGNORECASE,
)


def test_every_tool_with_a_sensitive_parameter_is_in_the_private_list():
    flagged = {
        name: sorted(parameter for parameter in schema_parameter_names(tool.parameters) if _SENSITIVE_NAME.search(parameter))
        for name, tool in _tools().items()
    }
    missing = {name: parameters for name, parameters in flagged.items() if parameters and name not in PRIVATE_ARGUMENT_TOOLS}
    assert not missing, (
        "Add these tools to PRIVATE_ARGUMENT_TOOLS in meta_ads_mcp/argument_privacy.py, "
        f"or rename the parameter if it holds nothing private: {missing}"
    )


def test_the_private_list_names_only_registered_tools():
    assert PRIVATE_ARGUMENT_TOOLS <= set(_tools()), sorted(PRIVATE_ARGUMENT_TOOLS - set(_tools()))


# --- Round-three review H14: credentials nested inside free-form arguments ---------


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
def test_h14_catalog_settings_tool_is_private_before_body_validation(routed, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "R3TOKEN7"
    arguments = {
        "catalog_id": {"misplaced_token": secret},
        "params": {"partner_integration": {"external_access_token": secret}},
    }
    with pytest.raises((ToolError, MCPValidationError)) as info:
        _call("update_catalog", arguments, routed)
    assert secret not in _rendered_chain(info.value)
    assert "catalog_id" in str(info.value)
    _assert_not_logged(caplog, secret)


# Tools whose credentials arrive inside free-form or nested arguments, where the
# parameter-name guard above cannot see them. Reviewed by hand: when a tool
# starts accepting a credential inside params or a nested object, add it here.
CREDENTIALS_INSIDE_NESTED_ARGUMENTS = {
    # params.partner_integration.external_access_token (catalogs.py masks it in current).
    "update_catalog": "params.partner_integration.external_access_token",
    # params is merged into the create request, which takes the same catalog settings.
    "create_catalog": "params.partner_integration.external_access_token",
    # schedule and update_schedule carry a feed host's username and password.
    "create_product_feed": "schedule.password, update_schedule.password",
    "update_product_feed": "schedule.password, update_schedule.password",
    # Product and image URLs can carry a login, inside params or batch items.
    "create_catalog_product": "params.url, params.image_url",
    "update_catalog_product": "params.url, params.image_url",
    "batch_update_catalog_products": "items[].url, items[].image_url",
    # Card and media URLs can carry a login.
    "create_ad_creative_from_inputs": "cards[].link, multi_media[].url",
    # Round-four review J3: tools that take Meta's raw creative fields, where a
    # creator's Partnership Ad code sits in branded_content.instagram_boost_post_access_token.
    "create_ad": "creative_spec.branded_content, creative.branded_content, params",
    "create_ad_creative": "params.branded_content, object_story_spec, asset_feed_spec",
    "preview_ad": "creative.branded_content",
    "update_creative": "params.branded_content, object_story_spec, asset_feed_spec",
}


@pytest.mark.parametrize("tool_name", sorted(CREDENTIALS_INSIDE_NESTED_ARGUMENTS))
def test_h14_tools_with_credentials_inside_nested_arguments_are_private(tool_name):
    assert tool_name in _tools(), f"{tool_name} is no longer registered; update the reviewed list"
    assert tool_name in PRIVATE_ARGUMENT_TOOLS, (
        f"{tool_name} carries {CREDENTIALS_INSIDE_NESTED_ARGUMENTS[tool_name]} and must be in "
        "PRIVATE_ARGUMENT_TOOLS in meta_ads_mcp/argument_privacy.py"
    )


# Helpers a tool calls when its own input can hold a credential: masking what it
# sent, redacting a request preview, or refusing a URL that carries a login.
_CREDENTIAL_HELPERS = frozenset({"_mask_secrets", "_redacted", "refuse_url_credentials", "_refuse_url_logins", "_refuse_url_logins_in"})


def _tools_using_credential_helpers() -> dict[str, set[str]]:
    """Registered tools whose code reaches a credential helper, following module-level calls."""
    import ast

    import meta_ads_mcp.tools as tools_package

    registered = set(_tools())
    found: dict[str, set[str]] = {}
    for path in sorted(Path(tools_package.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # Only the body counts. A decorator that masks what Meta returned (applied to
        # every tool in a module) says nothing about what the tool's own input can hold.
        direct = {
            name: {
                node.id
                for statement in function.body
                for node in ast.walk(statement)
                if isinstance(node, ast.Name)
            }
            for name, function in functions.items()
        }

        def reached(name: str, seen: set[str], direct=direct, functions=functions) -> set[str]:
            helpers = direct[name] & _CREDENTIAL_HELPERS
            for called in direct[name] & set(functions):
                if called not in seen:
                    seen.add(called)
                    helpers |= reached(called, seen)
            return helpers

        for name in set(functions) & registered:
            helpers = reached(name, {name})
            if helpers:
                found[name] = helpers
    return found


def _call_proxy(arguments: dict):
    return asyncio.run(mcp_server.call_tool("call_tool", arguments))


# --- Round-three review H19: the call_tool envelope of a private call -------------


@pytest.mark.parametrize("malformed", ["unknown_outer_key", "wrong_name_type"])
def test_h19_proxy_envelope_protects_private_target_before_dispatch(malformed, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "user@r3.invalid"
    arguments = {"name": "add_audience_users", "arguments": {"audience_id": "20", "schema": ["EMAIL"], "rows": [[secret]]}}
    if malformed == "unknown_outer_key":
        arguments[secret] = "misplaced-column"
    else:
        arguments["tool_name"] = arguments["name"]
        arguments["name"] = {"misplaced_customer": secret}
    with pytest.raises((ToolError, MCPValidationError)) as error:
        _call_proxy(arguments)
    assert secret not in _rendered_chain(error.value), str(error.value)
    _assert_not_logged(caplog, secret)


_ENVELOPE_TARGETS = [
    pytest.param({"name": "add_audience_users"}, id="name"),
    pytest.param({"tool_name": "remove_audience_users"}, id="alias"),
    pytest.param({"name": " update_catalog "}, id="padded_name"),
    # Names from Meta's official server that resolve to a private tool here.
    pytest.param({"name": "ads_catalog_update_catalog"}, id="official_name"),
    pytest.param({"tool_name": "ads_permission_set_asset_permissions"}, id="official_alias"),
    # An official name served by both customer-list tools: the target is not one tool.
    pytest.param({"name": "ads_update_custom_audience_users"}, id="official_several_private"),
    # One recognisable name is private: the other cannot make the call look ordinary.
    pytest.param({"name": "list_adsets", "tool_name": "add_audience_users"}, id="mixed_names"),
    # The target cannot be determined.
    pytest.param({}, id="no_name"),
    pytest.param({"name": None, "tool_name": None}, id="null_names"),
    pytest.param({"name": "   "}, id="blank_name"),
    pytest.param({"name": "no_such_tool_here"}, id="unknown_name"),
    pytest.param({"name": "call_tool"}, id="proxy_itself"),
]


@pytest.mark.parametrize("target", _ENVELOPE_TARGETS)
def test_unknown_envelope_keys_are_counted_when_the_target_is_private_or_unclear(target, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ToolError) as error:
        _call_proxy({**target, "arguments": {}, SECRET: "x", f"{SECRET}-2": "y"})
    rendered = _rendered_chain(error.value)
    assert SECRET not in rendered
    assert "Unknown parameters for call_tool: 2 unknown parameters" in str(error.value)
    assert "Accepted parameters: name, arguments, tool_name." in str(error.value)
    _assert_not_logged(caplog, SECRET)


@pytest.mark.parametrize(
    ("envelope", "parameter"),
    [
        pytest.param({"name": {"k": SECRET}, "tool_name": "add_audience_users"}, "name", id="name_object"),
        pytest.param({"name": [SECRET], "tool_name": "update_catalog"}, "name", id="name_list"),
        pytest.param({"name": "add_audience_users", "tool_name": {"k": SECRET}}, "tool_name", id="alias_object"),
        pytest.param({"name": {"k": SECRET}}, "name", id="name_only_object"),
        pytest.param({"tool_name": [SECRET]}, "tool_name", id="alias_only_list"),
    ],
)
def test_wrong_type_envelope_values_are_not_quoted_when_the_target_is_private_or_unclear(envelope, parameter, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises((ToolError, MCPValidationError)) as error:
        _call_proxy({**envelope, "arguments": {}})
    message = str(error.value)
    assert SECRET not in _rendered_chain(error.value)
    assert message.startswith(f"Invalid arguments for call_tool: {parameter}: Input should be a valid string")
    _assert_not_logged(caplog, SECRET)


def test_an_ordinary_target_keeps_naming_unknown_envelope_keys_exactly():
    with pytest.raises(ToolError) as info:
        _call_proxy({"name": "list_adsets", "arguments": {"account_id": "act_1"}, "bogus": 1})
    assert str(info.value) == (
        "Unknown parameters for call_tool: bogus. Accepted parameters: name, arguments, tool_name. "
        "Inspect types and defaults with get_capabilities(tool_name='call_tool')."
    )
    with pytest.raises(ToolError) as info:
        _call_proxy({"tool_name": "list_adsets", "bogus": 1})
    assert str(info.value).startswith("Unknown parameters for call_tool: bogus. ")


def test_a_wrong_type_target_beside_an_ordinary_one_is_treated_as_unclear():
    # Only name and tool_name have declared types, so a framework error on the
    # envelope always leaves one target unreadable: the value is not quoted.
    with pytest.raises((ToolError, MCPValidationError)) as info:
        _call_proxy({"name": "list_adsets", "tool_name": 5})
    assert str(info.value).startswith("Invalid arguments for call_tool: tool_name: Input should be a valid string")
    assert "input_value" not in str(info.value)


def test_a_private_target_reached_through_a_valid_envelope_still_runs_its_own_checks():
    # The envelope is fine; the nested arguments go to the private tool's own handling.
    with pytest.raises(ToolError) as info:
        _call_proxy({"name": "add_audience_users", "arguments": {"audience_id": "1", "schema": ["EMAIL"], "rows": [["a@b.invalid"]], SECRET: 1}})
    assert "Unknown parameters for add_audience_users: 1 unknown parameter" in str(info.value)
    assert SECRET not in _rendered_chain(info.value)


def test_h14_tools_that_mask_or_refuse_their_own_credentials_are_private():
    found = _tools_using_credential_helpers()
    assert {"update_catalog", "create_ad_creative_from_inputs"} <= set(found), found
    missing = {name: sorted(helpers) for name, helpers in found.items() if name not in PRIVATE_ARGUMENT_TOOLS}
    assert not missing, (
        "These tools mask, redact or refuse credentials in their own input, so their argument "
        f"errors must not quote values; add them to PRIVATE_ARGUMENT_TOOLS: {missing}"
    )


# --- Round-four review J3: raw creative fields can hold a Partnership Ad code ------

# Parameters that take Meta's raw ad creative fields (or a whole creative). Any of
# them can carry branded_content.instagram_boost_post_access_token, a creator's
# Partnership Ad code, nested where the parameter-name guard cannot see it.
RAW_CREATIVE_PARAMETERS = frozenset({"creative", "creative_spec", "object_story_spec", "asset_feed_spec"})
RAW_CREATIVE_TOOLS = ["create_ad", "create_ad_creative", "preview_ad", "update_creative"]


def _partnership_code() -> str:
    # Built at run time so the text appears only where the code under test puts it.
    return "".join(("R4-private-", "partnership-code"))


def _misplaced_code_arguments(tool: str, secret: str) -> dict:
    spec = {"branded_content": {"instagram_boost_post_access_token": secret}}
    misplaced = {"misplaced_code": secret}
    if tool == "create_ad":
        return {"account_id": misplaced, "name": "A", "adset_id": "20", "creative_spec": spec}
    if tool == "create_ad_creative":
        return {"account_id": misplaced, "name": "C", "params": spec}
    if tool == "preview_ad":
        return {"creative": spec, "account_id": misplaced}
    return {"creative_id": misplaced, "params": spec}


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
@pytest.mark.parametrize("tool", RAW_CREATIVE_TOOLS)
def test_nested_creative_code_tools_are_private_before_validation(tool, routed, caplog):
    caplog.set_level(logging.DEBUG)
    secret = _partnership_code()
    with pytest.raises((ToolError, MCPValidationError)) as error:
        _call(tool, _misplaced_code_arguments(tool, secret), routed)
    assert secret not in _rendered_chain(error.value)
    assert str(error.value).startswith(f"Invalid arguments for {tool}: ")
    _assert_not_logged(caplog, secret)


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
@pytest.mark.parametrize("tool", RAW_CREATIVE_TOOLS)
def test_a_code_misplaced_as_a_parameter_name_of_a_raw_creative_tool_is_counted(tool, routed, caplog):
    caplog.set_level(logging.DEBUG)
    secret = _partnership_code()
    with pytest.raises(ToolError) as error:
        _call(tool, {secret: "x"}, routed)
    assert secret not in _rendered_chain(error.value)
    assert "1 unknown parameter" in str(error.value)
    _assert_not_logged(caplog, secret)


def test_every_tool_that_takes_raw_creative_fields_is_private():
    """Independent of the name guard: a new tool taking a raw creative is caught here."""
    taking = {
        name: sorted(RAW_CREATIVE_PARAMETERS & set(tool.parameters.get("properties", {})))
        for name, tool in _tools().items()
    }
    taking = {name: parameters for name, parameters in taking.items() if parameters}
    assert set(RAW_CREATIVE_TOOLS) <= set(taking), taking
    missing = {name: parameters for name, parameters in taking.items() if name not in PRIVATE_ARGUMENT_TOOLS}
    assert not missing, (
        "These tools take raw creative fields, which can hold a Partnership Ad code; "
        f"add them to PRIVATE_ARGUMENT_TOOLS and CREDENTIALS_INSIDE_NESTED_ARGUMENTS: {missing}"
    )
    unreviewed = set(taking) - set(CREDENTIALS_INSIDE_NESTED_ARGUMENTS)
    assert not unreviewed, sorted(unreviewed)


# --- Round-four review J9: a target that is no tool is never repeated -------------

_MISPLACED_PERSON = "".join(("misplaced.person", "@proxy.invalid"))


def _assert_unknown_tool_refusal(error: BaseException, secret: str) -> None:
    from meta_ads_mcp.config import get_settings
    from meta_ads_mcp.error_middleware import READ_ONLY_UNKNOWN_TOOL_MESSAGE, UNKNOWN_TOOL_MESSAGE

    expected = READ_ONLY_UNKNOWN_TOOL_MESSAGE if get_settings().read_only else UNKNOWN_TOOL_MESSAGE
    assert str(error) == expected
    assert "search_tools" in str(error)
    # No chained exception carries the target either.
    assert error.__cause__ is None and error.__context__ is None
    assert secret not in _rendered_chain(error)


@pytest.mark.parametrize("target_key", ["name", "tool_name"])
def test_unknown_proxy_target_keeps_misplaced_personal_data_private(target_key, caplog):
    caplog.set_level(logging.DEBUG)
    secret = _MISPLACED_PERSON
    with pytest.raises(ToolError) as error:
        _call_proxy({target_key: secret, "arguments": {"audience_id": "20", "schema": ["EMAIL"], "rows": [[secret]]}})
    _assert_unknown_tool_refusal(error.value, secret)
    _assert_not_logged(caplog, secret)


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({"name": f"  {_MISPLACED_PERSON}  ", "arguments": {}}, id="padded"),
        pytest.param({"name": _MISPLACED_PERSON}, id="no_arguments"),
        pytest.param({"name": _MISPLACED_PERSON, "arguments": '{"audience_id": "20"}'}, id="string_arguments"),
        pytest.param({"name": "add_audience_users", "tool_name": _MISPLACED_PERSON, "arguments": {}}, id="known_name_unknown_alias"),
        pytest.param({"name": _MISPLACED_PERSON, "tool_name": "list_adsets", "arguments": {}}, id="unknown_name_known_alias"),
        pytest.param({"name": _MISPLACED_PERSON, "tool_name": _MISPLACED_PERSON, "arguments": {}}, id="both_unknown"),
        pytest.param({"name": f"ads_{_MISPLACED_PERSON}", "arguments": {}}, id="official_looking"),
    ],
)
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "read_only"])
def test_every_shape_of_an_unknown_proxy_target_is_refused_before_dispatch(envelope, locked, caplog, monkeypatch):
    monkeypatch.setenv("META_READ_ONLY", "true" if locked else "false")
    reload_settings()
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ToolError) as error:
        _call_proxy(envelope)
    _assert_unknown_tool_refusal(error.value, _MISPLACED_PERSON)
    _assert_not_logged(caplog, _MISPLACED_PERSON)


def test_an_unknown_direct_tool_name_is_not_repeated(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ToolError) as error:
        asyncio.run(mcp_server.call_tool(_MISPLACED_PERSON, {"rows": [[_MISPLACED_PERSON]]}))
    _assert_unknown_tool_refusal(error.value, _MISPLACED_PERSON)
    _assert_not_logged(caplog, _MISPLACED_PERSON)


def test_a_registered_proxy_target_behaves_as_before():
    # The nested call's own framework error, exactly as a direct call gets it.
    with pytest.raises(MCPValidationError) as routed:
        _call_proxy({"name": "list_adsets", "arguments": {"account_id": "act_1", "limit": "not-a-number"}})
    assert "not-a-number" in str(routed.value)
    with pytest.raises(MCPValidationError):
        _call_proxy({"tool_name": " list_adsets ", "arguments": {"account_id": "act_1", "limit": "not-a-number"}})
    # The proxy's own refusals keep their messages.
    with pytest.raises(ToolError, match="synthetic search tool"):
        _call_proxy({"name": "search_tools", "arguments": {"query": "x"}})
    with pytest.raises(ToolError, match="get_auction_ranking_report"):
        _call_proxy({"name": "ads_insights_industry_benchmark", "arguments": {}})
    with pytest.raises(ToolError, match="Provide name or tool_name"):
        _call_proxy({"name": "   ", "arguments": {}})


def _protocol_call(name: str, arguments: dict):
    """A tools/call as an MCP client sends it, through FastMCP's request handler."""
    return asyncio.run(mcp_server._call_tool_mcp(name, arguments))


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param(_MISPLACED_PERSON, {"rows": [[_MISPLACED_PERSON]]}, id="unknown_direct_name"),
        pytest.param("call_tool", {"name": _MISPLACED_PERSON, "arguments": {}}, id="unknown_proxy_target"),
        pytest.param("add_audience_users", {"audience_id": "1", "schema": ["EMAIL"], "rows": [[_MISPLACED_PERSON]]}, id="customer_rows"),
    ],
)
def test_the_protocol_debug_log_never_repeats_values_or_a_name_that_is_no_tool(name, arguments, caplog):
    from meta_ads_mcp.argument_privacy import FASTMCP_HANDLER_LOGGER

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=FASTMCP_HANDLER_LOGGER)
    with pytest.raises(Exception) as error:
        _protocol_call(name, arguments)
    assert _MISPLACED_PERSON not in _rendered_chain(error.value)
    handler_records = [record for record in caplog.records if "Handler called: call_tool" in record.getMessage()]
    assert handler_records, "FastMCP's DEBUG record of the call should still be logged"
    if name != _MISPLACED_PERSON:
        assert name in handler_records[0].getMessage()
    _assert_not_logged(caplog, _MISPLACED_PERSON)


def test_the_handler_filter_sits_on_the_logger_fastmcp_uses_for_the_call_record():
    """If FastMCP moves or rewords the record, this fails instead of the filter silently missing it."""
    import fastmcp.server.mixins.mcp_operations as operations

    from meta_ads_mcp.argument_privacy import FASTMCP_HANDLER_LOGGER, HandlerLogPrivacyFilter

    assert operations.logger.name == FASTMCP_HANDLER_LOGGER
    assert any(isinstance(item, HandlerLogPrivacyFilter) for item in logging.getLogger(FASTMCP_HANDLER_LOGGER).filters)
    source = Path(operations.__file__).read_text(encoding="utf-8")
    assert 'Handler called: call_tool %s with %s", key, arguments' in source


def test_a_registered_write_target_in_read_only_mode_keeps_the_read_only_refusal(monkeypatch):
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    with pytest.raises(ToolError, match="META_READ_ONLY is enabled; 'create_ad' is not permitted"):
        _call_proxy({"name": "create_ad", "arguments": {}})


# --- Round-five review K3: the MCP transport logs a malformed message first --------
#
# The mcp library checks each JSON-RPC message against the MCP request and
# notification types before FastMCP's middleware runs, and logs the ones that
# fail with the whole message. These tests drive the real low-level server over
# in-memory streams, as a transport does.

_TRANSPORT_PERSON = "".join(("private.person", "@transport.invalid"))


def _jsonrpc_request(params: dict, method: str = "tools/call", request_id: int = 1):
    return SessionMessage(mcp_types.JSONRPCMessage(
        root=mcp_types.JSONRPCRequest(jsonrpc="2.0", id=request_id, method=method, params=params)
    ))


def _jsonrpc_notification(method: str, params: dict):
    return SessionMessage(mcp_types.JSONRPCMessage(
        root=mcp_types.JSONRPCNotification(jsonrpc="2.0", method=method, params=params)
    ))


def _serve(messages: list, replies: int = 1) -> list:
    """Send messages to the real low-level MCP server and return its first replies."""
    lowlevel = mcp_server._mcp_server

    async def exercise():
        to_server, server_in = anyio.create_memory_object_stream(10)
        server_out, from_server = anyio.create_memory_object_stream(10)
        received = []

        async def serve():
            await lowlevel.run(server_in, server_out, lowlevel.create_initialization_options(), stateless=True)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(serve)
            for message in messages:
                await to_server.send(message)
            with anyio.fail_after(5):
                for _ in range(replies):
                    received.append(await from_server.receive())
            tasks.cancel_scope.cancel()
        return received

    return anyio.run(exercise)


def _ping(request_id: int = 99):
    """A well-formed request: its reply shows the messages sent before it were handled."""
    return _jsonrpc_request({}, method="ping", request_id=request_id)


def _transport_records(caplog, prefix: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage().startswith(prefix)]


def _capture_transport_logs(caplog) -> None:
    from meta_ads_mcp.argument_privacy import MCP_LOWLEVEL_LOGGER

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=MCP_LOWLEVEL_LOGGER)


@pytest.mark.parametrize("unknown_name", [False, True])
def test_malformed_mcp_arguments_are_private_before_tool_dispatch(caplog, unknown_name):
    secret = _TRANSPORT_PERSON
    name = secret if unknown_name else "add_audience_users"
    _capture_transport_logs(caplog)
    (reply,) = _serve([_jsonrpc_request({"name": name, "arguments": [secret]})])
    assert reply.message.root.error.message == "Invalid request parameters"
    assert secret not in caplog.text
    _assert_not_logged(caplog, secret)
    # An operator still sees that a malformed call arrived, its method, where and why.
    (warning,) = _transport_records(caplog, "Failed to validate request: ")
    assert warning.levelno == logging.WARNING
    assert warning.getMessage().startswith("Failed to validate request: method 'tools/call'; params.arguments: dict_type")
    (debug,) = _transport_records(caplog, "Message that failed validation: ")
    assert debug.levelno == logging.DEBUG
    assert "method 'tools/call'; params.arguments: dict_type" in debug.getMessage()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(
            _jsonrpc_request({"name": 5, "arguments": {"rows": [[_TRANSPORT_PERSON]]}}),
            "Failed to validate request: method 'tools/call'; params.name: string_type",
            id="wrong_type_name_beside_customer_rows",
        ),
        pytest.param(
            _jsonrpc_request({"name": "add_audience_users", _TRANSPORT_PERSON: "x", "arguments": [1]}),
            "Failed to validate request: method 'tools/call'; params.arguments: dict_type",
            id="customer_value_as_an_envelope_key",
        ),
        pytest.param(
            _jsonrpc_request({"uri": _TRANSPORT_PERSON, "arguments": [_TRANSPORT_PERSON]}, method=_TRANSPORT_PERSON),
            "Failed to validate request: the method is no MCP request method and is not repeated",
            id="customer_value_as_the_method",
        ),
        pytest.param(
            _jsonrpc_request({"uri": [_TRANSPORT_PERSON]}, method="resources/read"),
            "Failed to validate request: method 'resources/read'; params.uri: url_type",
            id="another_request_type",
        ),
        pytest.param(
            _jsonrpc_notification("notifications/cancelled", {"requestId": [_TRANSPORT_PERSON]}),
            "Failed to validate notification: method 'notifications/cancelled'; params.requestId.int: int_type",
            id="wrong_type_notification",
        ),
        pytest.param(
            _jsonrpc_notification(_TRANSPORT_PERSON, {"requestId": [_TRANSPORT_PERSON]}),
            "Failed to validate notification: the method is no MCP notification method and is not repeated",
            id="customer_value_as_a_notification_method",
        ),
    ],
)
def test_every_malformed_mcp_message_keeps_method_location_and_type_only(message, expected, caplog):
    _capture_transport_logs(caplog)
    _serve([message, _ping()])
    _assert_not_logged(caplog, _TRANSPORT_PERSON)
    records = _transport_records(caplog, expected)
    assert records, [record.getMessage() for record in caplog.records]
    assert records[0].levelno == logging.WARNING


def _stdio_parse_error(line: str) -> Exception:
    """The exception the stdio transport hands the server for a line it cannot read."""
    try:
        mcp_types.JSONRPCMessage.model_validate_json(line)
    except Exception as error:  # noqa: BLE001 - whatever the transport would forward
        return error
    raise AssertionError("the line was expected to be unreadable")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["' + _TRANSPORT_PERSON + '"]}',
            "JSONRPCRequest.params: dict_type",
            id="params_as_a_list",
        ),
        pytest.param("tools/call " + _TRANSPORT_PERSON, "(whole message): json_invalid", id="not_json"),
    ],
)
def test_an_unreadable_transport_line_is_logged_without_its_contents(line, expected, caplog):
    _capture_transport_logs(caplog)
    _serve([_stdio_parse_error(line)])
    _assert_not_logged(caplog, _TRANSPORT_PERSON)
    (record,) = _transport_records(caplog, "Received exception from stream: ")
    assert record.levelno == logging.ERROR
    assert expected in record.getMessage(), record.getMessage()


def test_the_low_level_server_does_not_log_a_tool_name_that_is_no_tool(caplog):
    _capture_transport_logs(caplog)
    _serve([
        _jsonrpc_request({"name": _TRANSPORT_PERSON, "arguments": {"rows": [[_TRANSPORT_PERSON]]}}, request_id=1),
        _jsonrpc_request({"name": "list_adsets", "arguments": {}}, request_id=2),
    ], replies=2)
    _assert_not_logged(caplog, _TRANSPORT_PERSON)
    misses = [record.getMessage() for record in _transport_records(caplog, "Tool cache miss for ")]
    assert "Tool cache miss for <name that is no tool, withheld>, refreshing cache" in misses, misses
    assert "Tool cache miss for list_adsets, refreshing cache" in misses, misses


def _session_record(message: str, args: tuple = (), exc_info=None, level: int = logging.WARNING) -> logging.LogRecord:
    import mcp.shared.session as session

    return logging.LogRecord("root", level, session.__file__, 1, message, args, exc_info)


def _raised_with(secret: str):
    try:
        try:
            raise KeyError(secret)
        except KeyError as inner:
            raise RuntimeError(f"while reading {secret}") from inner
    except RuntimeError:
        import sys

        return sys.exc_info()


def _formatted(record: logging.LogRecord) -> str:
    return logging.Formatter("%(levelname)s %(name)s %(message)s").format(record)


def test_an_exception_on_the_transport_keeps_its_type_and_frames_but_not_its_message():
    from meta_ads_mcp.argument_privacy import SessionLogPrivacyFilter

    secret = _TRANSPORT_PERSON
    record = _session_record(f"Unhandled exception in receive loop: {secret}", exc_info=_raised_with(secret), level=logging.ERROR)
    assert SessionLogPrivacyFilter().filter(record) is True
    rendered = _formatted(record)
    assert secret not in rendered
    assert record.levelno == logging.ERROR
    assert rendered.startswith("ERROR root Unhandled exception in receive loop: RuntimeError")
    assert "builtins.KeyError: (message withheld" in rendered
    assert "builtins.RuntimeError: (message withheld" in rendered
    assert "_raised_with" in rendered, "the frames should still show where it failed"


@pytest.mark.parametrize(
    ("message", "args", "expected"),
    [
        ("Progress callback raised an exception: %s", (ValueError(_TRANSPORT_PERSON),), "Progress callback raised an exception: ValueError"),
        (f"Response ID {_TRANSPORT_PERSON!r} cannot be normalized to match pending requests", (), "Response ID cannot be normalized"),
        (f"A message a newer mcp release logs: {_TRANSPORT_PERSON}", (), "MCP session record not recognised"),
        (f"Failed to validate request: {_TRANSPORT_PERSON}", (), "Failed to validate request: details withheld"),
    ],
)
def test_every_session_record_shape_is_withheld(message, args, expected):
    from meta_ads_mcp.argument_privacy import SessionLogPrivacyFilter

    record = _session_record(message, args)
    assert SessionLogPrivacyFilter().filter(record) is True
    assert _TRANSPORT_PERSON not in _formatted(record)
    assert record.getMessage().startswith(expected), record.getMessage()


def test_the_session_filter_leaves_other_records_on_the_root_logger_alone():
    from meta_ads_mcp.argument_privacy import SessionLogPrivacyFilter

    unchanged = _session_record("Read stream closed by client", level=logging.DEBUG)
    other = logging.LogRecord("root", logging.WARNING, "/elsewhere/library/module.py", 1, "value %s", ("kept",), None)
    for record, text in ((unchanged, "Read stream closed by client"), (other, "value kept")):
        assert SessionLogPrivacyFilter().filter(record) is True
        assert record.getMessage() == text


@pytest.mark.parametrize(
    ("message", "args", "expected"),
    [
        ("Warning: %s: %s", ("UserWarning", f"serialized {_TRANSPORT_PERSON}"), "Warning: UserWarning: (message withheld"),
        ("Received message: %s", (_jsonrpc_request({"name": _TRANSPORT_PERSON}).message,), "Received message: JSONRPCRequest (contents withheld)"),
        ("Tool '%s' not listed, no validation will be performed", (_TRANSPORT_PERSON,), "Tool '<name that is no tool, withheld>' not listed"),
        ("Processing request of type %s", ("CallToolRequest",), "Processing request of type CallToolRequest"),
    ],
)
def test_every_low_level_server_record_shape_is_withheld(message, args, expected):
    from meta_ads_mcp.argument_privacy import MCP_LOWLEVEL_LOGGER, LowLevelServerLogPrivacyFilter

    record = logging.LogRecord(MCP_LOWLEVEL_LOGGER, logging.INFO, __file__, 1, message, args, _raised_with(_TRANSPORT_PERSON))
    assert LowLevelServerLogPrivacyFilter(lambda: {"list_adsets"}).filter(record) is True
    assert _TRANSPORT_PERSON not in _formatted(record)
    assert record.getMessage().startswith(expected), record.getMessage()


def test_the_transport_filters_sit_where_the_mcp_library_logs():
    """If the mcp library moves or rewords a record, this fails instead of the filter missing it."""
    import mcp.server.lowlevel.server as lowlevel_server
    import mcp.shared.session as session

    from meta_ads_mcp.argument_privacy import (
        MCP_LOWLEVEL_LOGGER,
        LowLevelServerLogPrivacyFilter,
        SessionLogPrivacyFilter,
    )

    assert any(isinstance(item, SessionLogPrivacyFilter) for item in logging.getLogger().filters)
    assert lowlevel_server.logger.name == MCP_LOWLEVEL_LOGGER
    assert any(isinstance(item, LowLevelServerLogPrivacyFilter) for item in logging.getLogger(MCP_LOWLEVEL_LOGGER).filters)

    # The session module logs only through the root logger, and only these records.
    source = Path(session.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\blogger\s*=|getLogger\(", source)
    messages = re.findall(r"logging\.(?:debug|info|warning|error|exception|critical)\(\s*f?\"([^\"]*)\"", source)
    known = (
        "Failed to validate request: ", "Message that failed validation: ", "Failed to validate notification: ",
        "Read stream closed by client", "Unhandled exception in receive loop: ", "Response ID ",
        "Progress callback raised an exception: ",
    )
    assert messages and all(message.startswith(known) for message in messages), messages

    # The low-level server's records that carry a value are the ones the filter rewrites.
    source = Path(lowlevel_server.__file__).read_text(encoding="utf-8")
    formatted = re.findall(r"logger\.\w+\(\s*f\"([^\"]*)\"", source)
    assert formatted == ["Received exception from stream: {message}"], formatted
    templates = set(re.findall(r"logger\.\w+\(\s*\"([^\"]*%[sr][^\"]*)\"", source))
    rewritten = {
        "Tool cache miss for %s, refreshing cache", "Tool '%s' not listed, no validation will be performed",
        "Received message: %s", "Warning: %s: %s",
    }
    # These carry the server's own name, a request type's name or a request id.
    harmless = {
        "Initializing server %r", "Processing request of type %s", "Dispatching request of type %s",
        "Request %s cancelled - duplicate response suppressed", "Response for %s dropped - transport closed",
        "Dispatching notification of type %s",
    }
    assert templates == rewritten | harmless, templates ^ (rewritten | harmless)
