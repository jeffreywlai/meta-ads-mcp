"""Keep argument values out of validation logs, and out of errors for private tools.

When a call's arguments fail the declared types, FastMCP logs a WARNING built
from Pydantic's error list, and each entry's ``input`` holds the supplied value;
for a missing required argument it holds every argument of the call. The error
returned to the caller quotes values the same way. That happens before a tool's
own checks run, so the tool cannot prevent it.

Two measures, both applied before any tool code runs:

* ``ValidationLogPrivacyFilter`` rewrites that FastMCP log record for every
  tool, keeping parameter names, error types and Pydantic's own messages and
  dropping every value. It sits on the logger that emits the record, so a new
  tool is covered without registering anything.
* ``PRIVATE_ARGUMENT_TOOLS`` names the tools whose arguments can carry personal
  data or secrets. For those, ``ToolParameterHelpMiddleware`` returns validation
  errors that name parameters only and counts unknown parameters instead of
  quoting them (a misplaced customer value can arrive as an argument name).

``HandlerLogPrivacyFilter`` does the same for FastMCP's DEBUG record of each
MCP tool call, which otherwise carries the tool name and every argument value.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any, get_args

from pydantic_core.core_schema import ErrorType

# The single list of tools whose arguments can carry personal data or secrets.
# tests/test_argument_privacy.py fails when a registered tool has a parameter
# whose name suggests either and is missing here.
PRIVATE_ARGUMENT_TOOLS: frozenset[str] = frozenset(
    {
        # Customer-list rows and customer CSV files.
        "add_audience_users",
        "remove_audience_users",
        # Feed schedules and uploads can carry a username and password, and a
        # feed URL can carry a login.
        "create_product_feed",
        "update_product_feed",
        "create_product_feed_upload",
        # A Partnership Ad authorization code, local file paths, media URLs.
        "create_ad_creative_from_inputs",
        "boost_instagram_post",
        "upload_ad_video",
        "upload_creative_asset",
        # People searched for and the access granted to them.
        "list_business_users",
        "list_catalog_partners",
        "set_catalog_access",
        "preview_catalog_access_change",
        # Product and landing-page URLs can carry a login, in params or batch items.
        "create_catalog_product",
        "update_catalog_product",
        "batch_update_catalog_products",
        "list_dpa_eligible_catalogs",
        # Product set filters and names are checked for URLs that carry a login, so
        # the value being checked must not be quoted back by a framework error.
        "create_product_set",
        "update_product_set",
        "list_catalog_products",
        "build_product_set_filter",
        # Reads a creative that can hold a Partnership Ad code and previews its copy.
        "update_ad_creative_enhancements",
        # Take Meta's raw creative fields (creative_spec, creative, params,
        # object_story_spec, asset_feed_spec), where a creator's Partnership Ad
        # code sits in branded_content.instagram_boost_post_access_token.
        "create_ad",
        "create_ad_creative",
        "preview_ad",
        "update_creative",
        # Catalog settings in params can carry an integration access token
        # (partner_integration.external_access_token).
        "create_catalog",
        "update_catalog",
        # App secrets, access tokens and OAuth codes.
        "exchange_code_for_token",
        "generate_system_user_token",
        "get_token_info",
        "refresh_to_long_lived_token",
        "validate_token",
    }
)

# FastMCP's logger for tool calls (fastmcp/server/server.py, get_logger(__name__)).
FASTMCP_SERVER_LOGGER = "fastmcp.server.server"
_INVALID_ARGUMENTS_PREFIX = "Invalid arguments for tool"
_WITHHELD = "details withheld so argument values stay out of logs"

_BUILTIN_ERROR_TYPES = frozenset(get_args(ErrorType))
# Built-in error types whose message embeds text derived from the input: a
# validator's own exception text, a union tag read from the input, or an
# exception raised while reading the input.
_VALUE_BEARING_ERROR_TYPES = frozenset(
    {
        "value_error",
        "assertion_error",
        "union_tag_invalid",
        "get_attribute_error",
        "mapping_type",
        "iteration_error",
        "no_such_attribute",
    }
)
# Error types whose location ends in a name the caller made up.
_UNKNOWN_NAME_ERROR_TYPES = frozenset({"unexpected_keyword_argument", "extra_forbidden"})
_UNKNOWN_SEGMENT = "<unknown>"
_VALUE_SEGMENT = "<key>"


def schema_parameter_names(schema: Any) -> set[str]:
    """Return every property name declared anywhere in a JSON schema."""
    names: set[str] = set()
    pending = [schema]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            properties = current.get("properties")
            if isinstance(properties, Mapping):
                names.update(str(name) for name in properties)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return names


def _safe_message(error: Mapping[str, Any]) -> str:
    error_type = str(error.get("type", ""))
    if error_type in _VALUE_BEARING_ERROR_TYPES or error_type not in _BUILTIN_ERROR_TYPES:
        return "Value is not valid"
    message = error.get("msg")
    return str(message) if isinstance(message, str) else "Value is not valid"


def _safe_location(error: Mapping[str, Any], known_names: Iterable[str]) -> tuple[Any, ...]:
    """Keep positions and declared parameter names; replace anything taken from the input."""
    known = set(known_names)
    location = error.get("loc")
    segments = list(location) if isinstance(location, (list, tuple)) else []
    unknown_last = str(error.get("type", "")) in _UNKNOWN_NAME_ERROR_TYPES
    safe: list[Any] = []
    for index, segment in enumerate(segments):
        if unknown_last and index == len(segments) - 1:
            safe.append(_UNKNOWN_SEGMENT)
        elif isinstance(segment, int) and not isinstance(segment, bool):
            safe.append(segment)
        elif isinstance(segment, str) and segment in known:
            safe.append(segment)
        else:
            safe.append(_VALUE_SEGMENT)
    return tuple(safe)


def safe_validation_errors(
    errors: Iterable[Any],
    known_names: Iterable[str],
) -> list[dict[str, Any]]:
    """Reduce Pydantic error entries to type, location and message, without values."""
    known = set(known_names)
    safe: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, Mapping):
            safe.append({"type": "unknown", "loc": (), "msg": "Value is not valid"})
            continue
        safe.append(
            {
                "type": str(error.get("type", "unknown")),
                "loc": _safe_location(error, known),
                "msg": _safe_message(error),
            }
        )
    return safe


def _is_error_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, Mapping) for item in value)


class ValidationLogPrivacyFilter(logging.Filter):
    """Strip argument values from FastMCP's ``Invalid arguments for tool`` WARNING."""

    def __init__(self, known_names: Callable[[], Iterable[str]]) -> None:
        super().__init__()
        self._known_names = known_names

    def _names(self) -> set[str]:
        try:
            return set(self._known_names())
        except Exception:
            # Without the catalog every name is treated as possibly a value.
            return set()

    def filter(self, record: logging.LogRecord) -> bool:
        if not str(record.msg).startswith(_INVALID_ARGUMENTS_PREFIX):
            return True
        args = record.args
        if isinstance(args, tuple) and len(args) == 2:
            name, detail = args
            if _is_error_list(detail):
                detail = safe_validation_errors(detail, self._names())
            else:
                detail = _WITHHELD
            record.args = (name, detail)
        else:
            # A message formatted some other way: keep only the fixed prefix.
            record.msg = f"{_INVALID_ARGUMENTS_PREFIX} ({_WITHHELD})"
            record.args = ()
        return True


def _catalog_parameter_names(server: Any) -> set[str]:
    """Every parameter name declared by any tool registered on the server."""
    names: set[str] = set()
    for component in list(server.local_provider._components.values()):
        parameters = getattr(component, "parameters", None)
        if isinstance(parameters, Mapping):
            names |= schema_parameter_names(parameters)
    return names


_INSTALLED: dict[int, ValidationLogPrivacyFilter] = {}

# FastMCP's MCP request handler logs every tools/call at DEBUG with the tool name
# and every argument value (fastmcp/server/mixins/mcp_operations.py).
FASTMCP_HANDLER_LOGGER = "fastmcp.server.mixins.mcp_operations"
_HANDLER_CALL_MARKER = "Handler called: call_tool"
_UNKNOWN_TOOL = "<name that is no tool, withheld>"


class HandlerLogPrivacyFilter(logging.Filter):
    """Keep argument values, and a tool name that is no tool, out of FastMCP's DEBUG call log.

    The tool name stays when it is a registered tool: a name that matches no tool
    may be a customer value put in the wrong place. Arguments are counted.
    """

    def __init__(self, tool_names: Callable[[], Iterable[str]]) -> None:
        super().__init__()
        self._tool_names = tool_names

    def _known(self, name: Any) -> bool:
        try:
            return isinstance(name, str) and name in set(self._tool_names())
        except Exception:
            return False

    def filter(self, record: logging.LogRecord) -> bool:
        message = str(record.msg)
        if _HANDLER_CALL_MARKER not in message:
            return True
        args = record.args
        if isinstance(args, tuple) and len(args) == 2:
            name, arguments = args
            count = len(arguments) if isinstance(arguments, Mapping) else 0
            record.args = (
                name if self._known(name) else _UNKNOWN_TOOL,
                f"{count} argument{'s' if count != 1 else ''} ({_WITHHELD})",
            )
        else:
            record.msg = f"{message.split(_HANDLER_CALL_MARKER, 1)[0]}{_HANDLER_CALL_MARKER} ({_WITHHELD})"
            record.args = ()
        return True


def _catalog_tool_names(server: Any) -> set[str]:
    return {
        str(getattr(component, "name", ""))
        for component in list(server.local_provider._components.values())
    } | {"call_tool", "search_tools"}


_HANDLER_INSTALLED: dict[int, HandlerLogPrivacyFilter] = {}


def install_validation_log_privacy(server: Any) -> ValidationLogPrivacyFilter:
    """Attach the log filters for this server's tools once, and return the validation one."""
    if id(server) not in _HANDLER_INSTALLED:
        handler_filter = HandlerLogPrivacyFilter(lambda: _catalog_tool_names(server))
        logging.getLogger(FASTMCP_HANDLER_LOGGER).addFilter(handler_filter)
        _HANDLER_INSTALLED[id(server)] = handler_filter
    existing = _INSTALLED.get(id(server))
    if existing is not None:
        return existing
    log_filter = ValidationLogPrivacyFilter(lambda: _catalog_parameter_names(server))
    logging.getLogger(FASTMCP_SERVER_LOGGER).addFilter(log_filter)
    _INSTALLED[id(server)] = log_filter
    return log_filter


def _describe_location(location: tuple[Any, ...]) -> str:
    rendered = ""
    for segment in location:
        if isinstance(segment, int):
            rendered += f"[{segment}]"
        else:
            rendered += f".{segment}" if rendered else str(segment)
    return rendered or "arguments"


def private_validation_message(
    tool_name: str,
    errors: Iterable[Any] | None,
    known_names: Iterable[str],
) -> str:
    """An argument error for a private tool that names parameters and never values."""
    unknown = 0
    problems: list[str] = []
    for error in safe_validation_errors(errors or [], known_names):
        if error["type"] in _UNKNOWN_NAME_ERROR_TYPES:
            unknown += 1
            continue
        problem = f"{_describe_location(error['loc'])}: {error['msg']}"
        if problem not in problems:
            problems.append(problem)
    if unknown:
        problems.append(unknown_parameter_count(unknown))
    if not problems:
        problems.append("the arguments do not match the tool's input schema")
    return (
        f"Invalid arguments for {tool_name}: {'; '.join(problems)}. "
        "Values are not repeated because this tool's arguments can hold personal data or secrets. "
        f"Inspect types and defaults with get_capabilities(tool_name='{tool_name}')."
    )


def unknown_parameter_count(count: int) -> str:
    return f"{count} unknown parameter{'s' if count != 1 else ''}"
