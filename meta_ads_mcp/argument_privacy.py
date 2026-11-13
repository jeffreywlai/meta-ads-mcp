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

Before any of that, the ``mcp`` library checks each JSON-RPC message against
the MCP request and notification types and logs the ones that fail, with the
whole message and every value in it. ``SessionLogPrivacyFilter`` and
``LowLevelServerLogPrivacyFilter`` rewrite those records to keep the method,
where the fault is and the error type (see "MCP transport records" below).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sys
import traceback
from collections.abc import Callable, Iterable, Mapping
from typing import Any, get_args

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
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


# --- MCP transport records --------------------------------------------------------
#
# The mcp library validates every incoming JSON-RPC message against the MCP
# request and notification types before FastMCP's middleware runs. When a
# message fails (a tools/call whose arguments is a list, a misspelt method, a
# wrong-typed notification), mcp/shared/session.py logs on the root logger, with
# the module-level logging.warning and logging.debug, the Pydantic error (each
# entry quotes the input) and the whole message. When the transport cannot even
# read a JSON-RPC message (stdio hands the parse error to the server), the
# low-level server logs the error text at ERROR on its own logger. Either can
# hold a customer's email, a password or a name that is no tool.
#
# The session records are logged directly on the root logger, so a filter on the
# root logger sees them before any handler, including handlers configured later;
# it does not see records from other loggers, which propagate to root's handlers
# without passing root's own filters. Within root, only records whose source file
# is the mcp session module are changed, so other libraries that log on root
# directly are untouched. Records are rewritten, never dropped: an operator still
# sees that a malformed message arrived, its method when that is an MCP method,
# where the fault is and the error type.

MCP_LOWLEVEL_LOGGER = "mcp.server.lowlevel.server"
_MCP_SESSION_SOURCE = os.path.normcase(os.path.join("mcp", "shared", "session.py"))
_TRANSPORT_WITHHELD = "values withheld so request contents stay out of logs"
_MAX_FAULTS = 8
# Location segments that are type tags Pydantic adds inside a union, not input.
_TYPE_TAGS = frozenset({"bool", "dict", "float", "int", "list", "none", "str"})
_NO_INPUT = object()
# Pydantic's first line, which may follow the record's own prefix.
_VALIDATION_HEADER = re.compile(r"\b\d+ validation errors? for \S+$")
_VALIDATION_TYPE = re.compile(r"\[type=(?P<type>[a-z_]+)")


@functools.cache
def _mcp_schema_names() -> frozenset[str]:
    """Every model and field name (and alias) the MCP types declare."""
    names: set[str] = set(_TYPE_TAGS)
    try:
        import mcp.types as mcp_types

        for value in vars(mcp_types).values():
            if isinstance(value, type) and issubclass(value, BaseModel):
                names.add(value.__name__)
                for field_name, field in value.model_fields.items():
                    names.add(field_name)
                    if field.alias:
                        names.add(field.alias)
    except Exception:
        # Without the schema every segment is treated as possibly a value.
        pass
    return frozenset(names)


@functools.cache
def _mcp_methods(kind: str) -> Mapping[str, str]:
    """MCP method name -> the request or notification type that has it."""
    methods: dict[str, str] = {}
    try:
        import mcp.types as mcp_types

        union = {"request": mcp_types.ClientRequest, "notification": mcp_types.ClientNotification}[kind]
        for member in get_args(union.model_fields["root"].annotation):
            field = getattr(member, "model_fields", {}).get("method")
            for value in get_args(field.annotation) if field is not None else ():
                if isinstance(value, str):
                    methods[value] = member.__name__
    except Exception:
        # Without the list no method is recognised, so none is logged.
        pass
    return methods


def _transport_location(location: Iterable[Any]) -> str:
    names = _mcp_schema_names()
    rendered = [
        str(segment)
        if (isinstance(segment, int) and not isinstance(segment, bool)) or segment in names
        else _VALUE_SEGMENT
        for segment in location
    ]
    return ".".join(rendered) or "(whole message)"


def _transport_error_type(error_type: Any) -> str:
    return error_type if error_type in _BUILTIN_ERROR_TYPES else "custom_error"


def _errors_from_exception(error: BaseException | None) -> list[tuple[tuple[Any, ...], str, Any]] | None:
    if not isinstance(error, PydanticValidationError):
        return None
    try:
        return [
            (tuple(item.get("loc", ())), str(item.get("type", "")), item.get("input", _NO_INPUT))
            for item in error.errors(include_url=False)
        ]
    except Exception:
        return None


def _errors_from_text(text: str) -> list[tuple[tuple[Any, ...], str, Any]] | None:
    """Locations and error types from Pydantic's rendering of a ValidationError.

    Used only when the exception object is not at hand. Values are never read:
    a location keeps only schema names and positions, and a type only Pydantic's
    own error types.
    """
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if _VALIDATION_HEADER.search(line)), None)
    if start is None:
        return None
    errors: list[tuple[tuple[Any, ...], str, Any]] = []
    location: tuple[Any, ...] = ()
    for line in lines[start + 1 :]:
        if line.startswith("    "):
            continue
        if line.startswith("  "):
            found = _VALIDATION_TYPE.search(line)
            errors.append((location, found["type"] if found else "unknown", _NO_INPUT))
            location = ()
            continue
        location = tuple(int(part) if part.isdigit() else part for part in line.strip().split("."))
    return errors


def _fault_list(faults: list[str]) -> str:
    unique = list(dict.fromkeys(faults))
    shown = "; ".join(unique[:_MAX_FAULTS])
    if len(unique) > _MAX_FAULTS:
        shown += f"; and {len(unique) - _MAX_FAULTS} more"
    return shown


def describe_message_validation(
    errors: list[tuple[tuple[Any, ...], str, Any]] | None,
    kind: str | None,
) -> str:
    """Say which method failed and where, without any value from the message.

    The method is named only when it is an MCP method: a client that sends a
    customer value as the method must not have it logged. For a union of MCP
    types (every request type, every notification type) only the errors of the
    type the method selects are listed; the others say only that their method
    differs.
    """
    if errors is None:
        return f"details withheld ({_TRANSPORT_WITHHELD})"
    methods = _mcp_methods(kind) if kind else {}
    supplied: Any = _NO_INPUT
    for location, error_type, given in errors:
        if error_type == "literal_error" and len(location) == 2 and location[1] == "method":
            supplied = given
            break
    if supplied is not _NO_INPUT:
        if not isinstance(supplied, str):
            return f"the method is not text, so no MCP {kind} matches ({_TRANSPORT_WITHHELD})"
        member = methods.get(supplied)
        if member is None:
            return f"the method is no MCP {kind} method and is not repeated ({_TRANSPORT_WITHHELD})"
        faults = [
            f"{_transport_location(location[1:])}: {_transport_error_type(error_type)}"
            for location, error_type, _given in errors
            if location[:1] == (member,)
        ]
        return f"method {supplied!r}; {_fault_list(faults) or 'no field named'} ({_TRANSPORT_WITHHELD})"
    faults = [
        f"{_transport_location(location)}: {_transport_error_type(error_type)}"
        for location, error_type, _given in errors
    ]
    return f"{_fault_list(faults) or 'no field named'} ({_TRANSPORT_WITHHELD})"


def value_free_exception_text(exc_info: Any) -> str | None:
    """A traceback with each exception's type and frames but not its message.

    Frames show source lines of code, never runtime values; the message of an
    exception raised while handling a message can quote that message.
    """
    if not isinstance(exc_info, tuple) or len(exc_info) != 3 or exc_info[1] is None:
        return None
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc_info[1]
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    parts: list[str] = []
    for index, error in enumerate(reversed(chain)):
        if index:
            parts.append("\nWhile handling the above exception, another exception occurred:\n\n")
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(error.__traceback__))
        parts.append(f"{type(error).__module__}.{type(error).__qualname__}: (message withheld; {_TRANSPORT_WITHHELD})\n")
    return "".join(parts).rstrip("\n")


def _strip_exception(record: logging.LogRecord) -> None:
    """Replace a record's exception with a traceback that leaves out every message."""
    if record.exc_info:
        record.exc_text = value_free_exception_text(record.exc_info)
        record.exc_info = None


def _rewrite(record: logging.LogRecord, message: str) -> None:
    record.msg = message
    record.args = ()


def _active_exception() -> BaseException | None:
    # The session logs these records inside its except block, so the exception
    # being handled is the one whose text the record quotes.
    return sys.exc_info()[1]


class SessionLogPrivacyFilter(logging.Filter):
    """Keep message contents out of the records mcp/shared/session.py logs on the root logger.

    Installed on the root logger. Records from any other source file pass
    unchanged. Each record the session module is known to log is rewritten to
    say what happened without a value from the message; one it is not known to
    log (a newer mcp release) keeps only its level and source.
    """

    @staticmethod
    def _from_session(record: logging.LogRecord) -> bool:
        return os.path.normcase(str(record.pathname)).endswith(_MCP_SESSION_SOURCE)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._from_session(record):
            return True
        try:
            self._sanitize(record)
        except Exception:
            _rewrite(record, f"MCP session record ({_TRANSPORT_WITHHELD})")
            record.exc_info = None
            record.exc_text = None
        return True

    @staticmethod
    def _sanitize(record: logging.LogRecord) -> None:
        message = str(record.msg)
        active = _active_exception()
        if message.startswith("Failed to validate request: "):
            errors = _errors_from_exception(active)
            if errors is None:
                errors = _errors_from_text(message)
            _rewrite(record, f"Failed to validate request: {describe_message_validation(errors, 'request')}")
        elif message.startswith("Message that failed validation: "):
            errors = _errors_from_exception(active)
            _rewrite(record, f"Message that failed validation: {describe_message_validation(errors, 'request')}")
        elif message.startswith("Failed to validate notification: "):
            errors = _errors_from_exception(active)
            if errors is None:
                errors = _errors_from_text(message)
            _rewrite(record, f"Failed to validate notification: {describe_message_validation(errors, 'notification')}")
        elif message.startswith("Unhandled exception in receive loop: "):
            error = record.exc_info[1] if record.exc_info else active
            name = type(error).__name__ if error is not None else "exception"
            _rewrite(record, f"Unhandled exception in receive loop: {name} (message withheld; {_TRANSPORT_WITHHELD})")
        elif message.startswith("Progress callback raised an exception: "):
            error = next((item for item in (record.args or ()) if isinstance(item, BaseException)), None)
            name = type(error).__name__ if error is not None else "exception"
            _rewrite(record, f"Progress callback raised an exception: {name} (message withheld; {_TRANSPORT_WITHHELD})")
        elif message.startswith("Response ID ") and "cannot be normalized" in message:
            _rewrite(record, f"Response ID cannot be normalized to match pending requests (the ID is withheld; {_TRANSPORT_WITHHELD})")
        elif message == "Read stream closed by client":
            pass
        else:
            _rewrite(record, f"MCP session record not recognised ({_TRANSPORT_WITHHELD})")
        _strip_exception(record)


class LowLevelServerLogPrivacyFilter(logging.Filter):
    """Keep message contents and names that are no tool out of the low-level MCP server's records.

    Installed on the ``mcp.server.lowlevel.server`` logger. The records it logs
    with a value are rewritten; the rest (request type names, request ids) pass.
    """

    _UNKNOWN_TOOL_TEMPLATES = (
        "Tool cache miss for %s, refreshing cache",
        "Tool '%s' not listed, no validation will be performed",
    )

    def __init__(self, tool_names: Callable[[], Iterable[str]]) -> None:
        super().__init__()
        self._tool_names = tool_names

    def _known(self, name: Any) -> bool:
        try:
            return isinstance(name, str) and name in set(self._tool_names())
        except Exception:
            return False

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._sanitize(record)
        except Exception:
            _rewrite(record, f"MCP server record ({_TRANSPORT_WITHHELD})")
            record.exc_info = None
            record.exc_text = None
        return True

    @staticmethod
    def _message_kind(message: Any) -> str:
        request = getattr(message, "request", None)
        root = getattr(request, "root", None) if request is not None else getattr(message, "root", None)
        return type(root if root is not None else message).__name__

    def _sanitize(self, record: logging.LogRecord) -> None:
        message = str(record.msg)
        args = record.args if isinstance(record.args, tuple) else ()
        if message in self._UNKNOWN_TOOL_TEMPLATES:
            name = args[0] if args else None
            record.args = (name if self._known(name) else _UNKNOWN_TOOL,)
        elif message.startswith("Received exception from stream: "):
            errors = _errors_from_text(message)
            detail = describe_message_validation(errors, None) if errors is not None else (
                f"details withheld ({_TRANSPORT_WITHHELD})"
            )
            _rewrite(record, f"Received exception from stream: {detail}")
        elif message == "Received message: %s":
            record.args = (f"{self._message_kind(args[0]) if args else 'message'} (contents withheld)",)
        elif message == "Warning: %s: %s":
            record.args = (args[0] if args else "Warning", f"(message withheld; {_TRANSPORT_WITHHELD})")
        _strip_exception(record)


_TRANSPORT_TOOL_NAMES: list[Callable[[], Iterable[str]]] = []
_TRANSPORT_FILTERS: list[logging.Filter] = []


def _transport_tool_names() -> set[str]:
    names: set[str] = set()
    for source in list(_TRANSPORT_TOOL_NAMES):
        names |= set(source())
    return names


def _install_transport_log_privacy(server: Any) -> None:
    _TRANSPORT_TOOL_NAMES.append(lambda: _catalog_tool_names(server))
    if _TRANSPORT_FILTERS:
        return
    session_filter = SessionLogPrivacyFilter()
    logging.getLogger().addFilter(session_filter)
    lowlevel_filter = LowLevelServerLogPrivacyFilter(_transport_tool_names)
    logging.getLogger(MCP_LOWLEVEL_LOGGER).addFilter(lowlevel_filter)
    _TRANSPORT_FILTERS.extend((session_filter, lowlevel_filter))


def install_validation_log_privacy(server: Any) -> ValidationLogPrivacyFilter:
    """Attach the log filters for this server's tools once, and return the validation one."""
    if id(server) not in _HANDLER_INSTALLED:
        handler_filter = HandlerLogPrivacyFilter(lambda: _catalog_tool_names(server))
        logging.getLogger(FASTMCP_HANDLER_LOGGER).addFilter(handler_filter)
        _HANDLER_INSTALLED[id(server)] = handler_filter
        _install_transport_log_privacy(server)
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
