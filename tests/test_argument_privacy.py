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

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as MCPValidationError

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
_CREDENTIAL_HELPERS = frozenset({"_mask_secrets", "_redacted", "refuse_url_credentials"})


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
        direct = {
            name: {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
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


def test_h14_tools_that_mask_or_refuse_their_own_credentials_are_private():
    found = _tools_using_credential_helpers()
    assert {"update_catalog", "create_ad_creative_from_inputs"} <= set(found), found
    missing = {name: sorted(helpers) for name, helpers in found.items() if name not in PRIVATE_ARGUMENT_TOOLS}
    assert not missing, (
        "These tools mask, redact or refuse credentials in their own input, so their argument "
        f"errors must not quote values; add them to PRIVATE_ARGUMENT_TOOLS: {missing}"
    )
