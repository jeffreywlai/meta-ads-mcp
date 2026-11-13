"""Product catalog feeds, feed uploads, feed rules, and catalog event sources.

These tools work on Meta product catalogs (commerce catalogs), not on this
server's own tool catalog.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import WithJsonSchema

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.diagnostics import build_finding
from meta_ads_mcp.errors import MetaApiError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.local_files import read_upload_file
from meta_ads_mcp.normalize import blank_to_none, normalize_collection, to_int
from meta_ads_mcp.schemas import analysis_response, creation_response, mutation_response
from meta_ads_mcp.secret_scrub import (  # noqa: F401 - the length thresholds stay importable from here
    ERROR,
    MIN_SECRET_SUBSTRING_LENGTH,
    MIN_SECRET_TOKEN_LENGTH,
    RESULT,
    distinct_key,
    rewrite_exception,
    scrub_exception,
    scrub_value,
)
from meta_ads_mcp.tool_types import GraphId, StringList, coerce_csv_string_list
from meta_ads_mcp.url_secrets import refuse_url_credentials

UPLOAD_FIELDS = [
    "id",
    "start_time",
    "end_time",
    "input_method",
    "filename",
    "url",
    "error_count",
    "warning_count",
    "num_detected_items",
    "num_persisted_items",
    "num_invalid_items",
    "num_deleted_items",
]
UPLOAD_ERROR_FIELDS = [
    "id",
    "summary",
    "description",
    "severity",
    "error_type",
    "affected_surfaces",
    "total_count",
]
# One sample per error from ProductFeedUploadError's documented samples edge, read by field
# expansion. ProductFeedUploadErrorSample has id, row_number and retailer_id (SDK 26.0.2
# productfeeduploaderrorsample.py; the sample response in Meta's Feed API guide shows the same
# three). Meta's public API gives the row and the item's retailer id, not the invalid value.
UPLOAD_ERROR_SAMPLE_FIELD = "samples.limit(1){id,row_number,retailer_id}"
UPLOAD_ERROR_PAGE_SIZE = 100
# This server's own caps on one upload's error read: rows, and so requests (rows / page size).
MAX_UPLOAD_ERRORS = 1000
# A sample's retailer id is the advertiser's own product id; Meta caps it at 100 characters,
# and anything longer is cut here so one sample cannot fill the response.
MAX_SAMPLE_CHARS = 100
# ProductFeed fields read by list_product_feeds on top of FEED_FIELDS and FEED_LIST_FIELDS.
# Their readability rests on the SDK alone (ProductFeed.Field in SDK 26.0.2 productfeed.py lists
# supplementary_feeds among the readable fields; Meta's reference does not list it), so a read
# Meta refuses is repeated without them and the response names them. They are kept out of
# FEED_FIELDS so the pre-read of a delete or an update never depends on them.
FEED_OPTIONAL_FIELDS = ["supplementary_feeds"]
FEED_FIELDS = [
    "id",
    "name",
    "file_name",
    "country",
    "default_currency",
    "deletion_enabled",
    "delimiter",
    "encoding",
    "quoted_fields_mode",
    "ingestion_source_type",
    "item_sub_type",
    "override_type",
    "primary_feeds",
    "migrated_from_feed_id",
    "product_count",
    "created_time",
    "schedule",
    "update_schedule",
    "latest_upload{" + ",".join(UPLOAD_FIELDS) + "}",
]
FEED_LIST_FIELDS = [
    "id",
    "name",
    "file_name",
    "ingestion_source_type",
    "product_count",
    "schedule",
    "update_schedule",
    "latest_upload{id,start_time,end_time,error_count,warning_count}",
]
FEED_UPDATE_READ_FIELDS = [
    "id",
    "name",
    "default_currency",
    "deletion_enabled",
    "delimiter",
    "encoding",
    "quoted_fields_mode",
    "schedule",
    "update_schedule",
    "migrated_from_feed_id",
]
# Fields this module already reads from a ProductFeed; a params change to one of
# them is read first so previous shows its real prior value.
FEED_READABLE_FIELDS = frozenset(field.split("{", 1)[0] for field in FEED_FIELDS)
RULE_FIELDS = ["id", "attribute", "rule_type", "params"]
EVENT_SOURCE_FIELDS = ["id", "name", "source_type"]
EVENT_STAT_FIELDS = [
    "date_start",
    "date_stop",
    "event",
    "event_source",
    "total_matched_content_ids",
    "total_unmatched_content_ids",
    "unique_matched_content_ids",
    "unique_unmatched_content_ids",
    "total_content_ids_matched_other_catalogs",
    "unique_content_ids_matched_other_catalogs",
]
DA_CHECK_FIELDS = ["key", "title", "description", "result", "user_message", "action_uri"]
CATALOG_SCAN_FIELDS = ["id", "name"]
PIXEL_CANDIDATE_FIELDS = ["id", "name", "last_fired_time", "is_unavailable"]

SCHEDULE_INTERVALS = ("HOURLY", "DAILY", "WEEKLY", "MONTHLY")
SCHEDULE_DAYS = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")
SCHEDULE_KEYS = (
    "url",
    "interval",
    "interval_count",
    "hour",
    "minute",
    "day_of_week",
    "day_of_month",
    "timezone",
    "username",
    "password",
)
SCHEDULE_URL_SCHEMES = ("http", "https", "ftp")
# Names a message about a nested params key may show; any other key is given by position.
_FEED_KNOWN_KEYS = frozenset({*FEED_READABLE_FIELDS, *FEED_OPTIONAL_FIELDS, *SCHEDULE_KEYS})
# ProductFeed options, with the values Meta's reference documents for
# POST /{product_catalog_id}/product_feeds and POST /{product_feed_id}. The value sent
# is the SDK 26.0.2 enum value (facebook_business/adobjects/productfeed.py).
FEED_TYPES = (
    "ACTIVITY", "APP_AND_SOFTWARE", "ARTICLE_AND_PUBLICATION", "AUTOMOTIVE_MODEL", "COLLECTION",
    "DESTINATION", "FLIGHT", "HOME_LISTING", "HOTEL", "HOTEL_ROOM", "LOCAL_INVENTORY", "MEDIA_TITLE",
    "OFFER", "PRODUCT_RATINGS_AND_REVIEWS", "PRODUCTS", "SERVICE", "TRANSACTABLE_ITEMS",
    "VEHICLE_OFFER", "VEHICLES",
)
FEED_DELIMITERS = ("AUTODETECT", "BAR", "COMMA", "TAB", "TILDE", "SEMICOLON")
# The characters themselves, as Meta's own server takes them.
_DELIMITER_CHARACTERS = {"|": "BAR", ",": "COMMA", "\t": "TAB", "~": "TILDE", ";": "SEMICOLON"}
FEED_ENCODINGS = ("AUTODETECT", "LATIN1", "UTF8", "UTF16LE", "UTF16BE", "UTF32LE", "UTF32BE")
# Common spellings: UTF-8 for UTF8 (hyphens are dropped) and ISO-8859-1 for LATIN1.
_ENCODING_SPELLINGS = {"ISO88591": "LATIN1"}
FEED_QUOTED_FIELDS_MODES = ("AUTODETECT", "ON", "OFF")
FEED_OVERRIDE_TYPES = (
    "LANGUAGE", "COUNTRY", "VERSION", "CATALOG_SEGMENT_CUSTOMIZE_DEFAULT", "LANGUAGE_AND_COUNTRY",
    "BATCH_API_LANGUAGE_OR_COUNTRY", "SMART_PIXEL_LANGUAGE_OR_COUNTRY", "LOCAL",
)
# The SDK sends these in lower case; Meta's reference shows them in upper case.
FEED_INGESTION_SOURCE_TYPES = ("primary_feed", "supplementary_feed")
FEED_RULE_TYPES = (
    "mapping_rule",
    "value_mapping_rule",
    "letter_case_rule",
    "fallback_rule",
    "regex_replace_rule",
)
LETTER_CASE_TYPES = ("to_upper", "to_lower", "capitalize_all", "capitalize_first")
VALUE_MAPPING_MAX_PAIRS = 10
VALUE_MAPPING_MAX_LENGTH = 20
DA_CHECK_SOURCE_TYPES = ("PIXEL", "APP")
DA_CHECK_EVALUATED_RESULTS = ("passed", "failed")

MAX_CATALOGS_SCANNED = 50
CATALOG_SCAN_PAGE_SIZE = 25
MAX_EDGE_PAGES = 10
MAX_PIXEL_CANDIDATES_CHECKED = 10
DEFAULT_MATCH_RATE_THRESHOLD = 0.8
# list_event_source_catalogs(include_match_rates=true): at most this many connected
# catalogs get one more read (their event_stats), each capped at this many pages.
MAX_MATCH_RATE_CATALOGS = 10
MATCH_RATE_STATS_PAGES = 5
# ProductEventStat.event values Meta documents.
CATALOG_EVENT_NAMES = (
    "ViewContent", "AddToCart", "Purchase", "InitiateCheckout", "Search", "Lead", "AddToWishlist", "Subscribe",
)
# Feed file formats Meta documents for direct uploads (CSV, TSV, XML, and zip, gzip
# or bz2 compressed files), by file extension, with the multipart content type sent.
FEED_FILE_TYPES = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xml": "application/xml",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".bz2": "application/x-bzip2",
}
# This server's own limit on a local feed file read into memory; Meta publishes none.
MAX_FEED_FILE_BYTES = 100 * 1024 * 1024
# Where the login part of a URL ends at the latest: white space, or the start of the path,
# query or fragment (see _url_logins).
_LOGIN_STOP = re.compile(r"[\s/?#]")
_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_SCHEME_FIRST = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
# Parameters that can carry a secret or pasted data. The framework accepts any value for
# them and the tool validates it, because the framework's own type errors quote the input.
_OptionalText = Annotated[Any, WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]})]
_OptionalSchedule = Annotated[Any, WithJsonSchema({"anyOf": [{"type": "object"}, {"type": "null"}]})]
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_DERIVED_METHOD = (
    "Derived by this server from Meta's documented catalog data; Meta publishes no "
    "single health or recommendation endpoint for catalog event sources."
)
_MATCH_RATE_NOTE = (
    "match_rate = total_matched_content_ids / (total_matched_content_ids + "
    "total_unmatched_content_ids) from the catalog's event_stats edge. This ratio and "
    "the threshold are computed here; Meta does not publish a match-rate threshold."
)
_ACROSS_CATALOGS_NOTE = (
    "For the content ids a source sent, Meta's event_stats counts how many matched this catalog "
    "(total_matched_content_ids) and how many matched another catalog connected to the source "
    "(total_content_ids_matched_other_catalogs). Meta does not say whether an id that matched both is "
    "counted in both, so the share matching any connected catalog is given as a range: at least the "
    "larger of the two shares, at most their sum (capped at 1). Computed here; Meta publishes no "
    "all-catalog rate. Counts are not de-duplicated."
)


def _require_id(value: str | None, name: str) -> str:
    """Return a stripped id or raise before any network call."""
    normalized = blank_to_none(value)
    if normalized is None:
        raise ValidationError(f"{name} is required.")
    return normalized


def _count_keys(count: int, noun: str) -> str:
    """'1 unsupported key', '3 unsupported keys': how a message counts keys it does not repeat."""
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _validate_limit(limit: int) -> None:
    if limit < 1:
        raise ValidationError("limit must be positive.")


def _page_params(limit: int, after: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    cursor = blank_to_none(after)
    if cursor:
        params["after"] = cursor
    return params


def _paged_collection(payload: dict[str, Any], after: str | None = None) -> dict[str, Any]:
    """Shape one Graph page the way list_audiences does.

    When Meta hands back the cursor that was just read, following it would return this
    page again, so no next cursor is offered and the stop reason says why.
    """
    normalized = normalize_collection(payload)
    has_more = bool(normalized["paging"].get("next"))
    next_after = normalized["paging"].get("after") if has_more else None
    requested = blank_to_none(after)
    repeated = has_more and next_after is not None and next_after == requested
    normalized["summary"].update({
        "complete": not has_more,
        "truncated": has_more,
        "next_after": None if repeated else next_after,
    })
    if repeated:
        normalized["summary"]["stop_reason"] = (
            "Meta returned the cursor that was just read, so following it would repeat this page; "
            "the rest of the list cannot be read with this cursor."
        )
    return normalized


async def _collect_edge(
    client: Any,
    parent_id: str,
    edge: str,
    *,
    fields: list[str],
    page_size: int,
    max_items: int | None = None,
    max_pages: int = MAX_EDGE_PAGES,
    extra_params: dict[str, Any] | None = None,
    stop_reasons: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read an edge across pages up to a cap; report whether the end was reached.

    The end is reached only when Meta's page has no next link. When the read stops
    early, a plain-language reason is appended to stop_reasons if a list is given.
    A page identical to one already read is not added again, and a cursor already
    read is not followed: either stops the read as incomplete, so a page Meta keeps
    returning is never counted twice.
    """
    items: list[dict[str, Any]] = []
    after: str | None = None
    cursors_read: set[str | None] = {None}
    pages_read: set[str] = set()

    def stopped(reason: str) -> None:
        if stop_reasons is not None:
            stop_reasons.append(f"{edge} of {parent_id}: {reason}")

    for _ in range(max_pages):
        params: dict[str, Any] = {**(extra_params or {}), "limit": page_size}
        if after:
            params["after"] = after
        payload = await client.list_objects(parent_id, edge, fields=fields, params=params)
        page = normalize_collection(payload)
        if page["items"]:
            fingerprint = json.dumps(page["items"], sort_keys=True, default=str)
            if fingerprint in pages_read:
                stopped(
                    "Meta returned the same page again; its rows were not counted a second time "
                    "and the read stopped, so later pages were not read."
                )
                return items, False
            pages_read.add(fingerprint)
        items.extend(page["items"])
        has_next = bool(page["paging"].get("next"))
        if max_items is not None and len(items) >= max_items:
            more = len(items) > max_items or has_next
            if more:
                stopped(f"stopped at the {max_items}-item cap; more items exist.")
            return items[:max_items], not more
        if not has_next:
            return items, True
        after = page["paging"].get("after")
        if not after:
            stopped("Meta reported another page but gave no cursor to read it.")
            return items, False
        if after in cursors_read:
            stopped(
                "Meta returned a cursor that was already read; the read stopped so no page is "
                "counted twice, and later pages were not read."
            )
            return items, False
        cursors_read.add(after)
    stopped(f"stopped after {max_pages} pages; more pages exist.")
    return items, False


def _object_id(value: Any) -> str | None:
    """Read an id from a nested Graph object or a bare id string."""
    if isinstance(value, dict):
        candidate = value.get("id")
        return str(candidate) if candidate not in (None, "") else None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None


def _validate_http_url(
    value: Any,
    *,
    name: str,
    schemes: tuple[str, ...],
    credentials_hint: str = "pass them in the username and password fields",
) -> str:
    """Accept only absolute URLs with an allowed scheme and a host and no credentials.

    The value is never quoted back: a mistaken value can be a feed record or a secret.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be an absolute {'/'.join(schemes)} URL.")
    url = value.strip()
    try:
        parts = urlsplit(url)
        has_credentials = parts.username is not None or parts.password is not None
    except ValueError:
        raise ValidationError(f"{name} must be an absolute URL using {', '.join(schemes)}.") from None
    if parts.scheme.lower() not in schemes or not parts.netloc or any(c.isspace() for c in url):
        raise ValidationError(f"{name} must be an absolute URL using {', '.join(schemes)}.")
    if has_credentials:
        raise ValidationError(
            f"{name} must not contain a username or password, because the URL is stored and shown "
            f"in responses; {credentials_hint}."
        )
    return url


def _mask_text(text: str) -> str:
    """Replace every password embedded in a URL inside the text, keeping user, host and path."""
    return _mask_url_passwords(text)


@dataclasses.dataclass(frozen=True, slots=True)
class _UrlLogin:
    """The login part of one scheme://user:password@ in a text: [start, end) ends after the @."""

    start: int
    end: int
    scheme: str
    login: str


def _url_logins(text: str) -> Iterator[_UrlLogin]:
    """Every scheme://login@ in the text, left to right, in one pass.

    The scheme is a letter followed by letters, digits, '+', '.' or '-', directly before
    '://'. The login runs to the last @ before white space or the path, query or fragment,
    which is where a URL parser ends it: a user name may itself contain @ (an email
    address), and a password may contain @ or :, so the split into user and password is
    made at the first colon afterwards (see _mask_url_passwords). A scan, not a regular
    expression, so a long run of letters cannot make it slow: each character is looked at
    a fixed number of times.
    """
    if "@" not in text or "://" not in text:
        return
    allowed_from = 0
    search = 0
    while (separator := text.find("://", search)) >= 0:
        search = separator + 1
        start = separator
        while start > allowed_from and text[start - 1] in _SCHEME_CHARS:
            start -= 1
        while start < separator and text[start] not in _SCHEME_FIRST:
            start += 1
        if start == separator:
            continue
        login_from = separator + 3
        stop = _LOGIN_STOP.search(text, login_from)
        at = text.rfind("@", login_from, stop.start() if stop else len(text))
        if at < 0:
            continue
        yield _UrlLogin(start, at + 1, text[start:login_from], text[login_from:at])
        allowed_from = search = at + 1


def _mask_url_passwords(text: str) -> str:
    """Mask the password of every URL login in the text, splitting it as a URL parser would."""
    parts: list[str] = []
    kept_from = 0
    for login in _url_logins(text):
        user, colon, _password = login.login.partition(":")
        if colon:
            parts.extend((text[kept_from:login.start], f"{login.scheme}{user}:[redacted]@"))
            kept_from = login.end
    if not parts:
        return text
    parts.append(text[kept_from:])
    return "".join(parts)


def _json_object(value: Any) -> dict[str, Any] | None:
    """The object a string holds as JSON text, or None when it holds none.

    Meta's reference documents some parameters as JSON objects (a catalog's
    partner_integration, a feed's schedule), and the Graph client sends an object as its
    JSON text, so a caller may pass either form. The text form is read only to find and
    mask the credentials it carries; it is always sent as it was given.
    """
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _masked_json_text(value: str, mask: Callable[[Any], Any]) -> str:
    """JSON text of an object with mask applied to it, or the text itself when nothing changed."""
    parsed = _json_object(value)
    if parsed is None:
        return value
    masked = mask(parsed)
    return value if masked == parsed else json.dumps(masked)


# Keys whose value is a data map: its own keys are the advertiser's data (a feed rule's
# params, where value_mapping_rule maps one product value to another), not field names.
# A key inside one is never taken to name a password, so a mapping such as
# {"password": "Kennwort"} is shown and sent as written. URL passwords are still masked.
_FEED_DATA_MAP_KEYS = frozenset({"params"})
# Feed parameters Meta documents as objects that can hold a password (ProductFeedSchedule's
# username and password). Passed as JSON text, their password is masked inside the text.
_FEED_SCHEDULE_KEYS = frozenset({"schedule", "update_schedule"})


def _redact(value: Any, data_map_keys: frozenset[str] = _FEED_DATA_MAP_KEYS, *, in_data: bool = False) -> Any:
    """Copy a response for display: any password key, and any password inside a URL, is masked.

    Every tool in this module returns through this function (see _secrets_masked), so a
    new field that carries a URL, such as an upload's url, cannot be missed. Keys are
    masked as well as values, since Meta can return a map keyed by a URL. A key is taken
    to name a password only where keys are field names: not inside a data map
    (data_map_keys), whose keys are product values. A schedule given as JSON text is
    masked inside the text, as the object form is.
    """
    if isinstance(value, dict):
        shown: dict[Any, Any] = {}
        for key, item in value.items():
            if not in_data and str(key).lower() == "password" and item is not None:
                masked: Any = "[redacted]"
            elif not in_data and key in _FEED_SCHEDULE_KEYS and isinstance(item, str):
                masked = _mask_text(_masked_json_text(item, lambda parsed: _redact(parsed, data_map_keys)))
            else:
                masked = _redact(item, data_map_keys, in_data=in_data or key in data_map_keys)
            shown[distinct_key(shown, _mask_text(key) if isinstance(key, str) else key)] = masked
        return shown
    if isinstance(value, list):
        return [_redact(item, data_map_keys, in_data=in_data) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, data_map_keys, in_data=in_data) for item in value)
    if isinstance(value, str):
        return _mask_text(value)
    return value


def _mask_error(exc: BaseException) -> None:
    """Mask URL passwords in a raised error and in every error behind it, in place.

    The Graph client raises a rate-limit or unsupported-feature error from the original
    Meta error, so the original stays reachable through __cause__ and __context__ and a
    traceback log prints it. Every error reachable that way, and every error inside an
    exception group, is masked (secret_scrub.rewrite_exception).
    """
    rewrite_exception(exc, _redact)


# Secrets a caller supplies (a feed or upload password, a catalog integration token) are
# removed from any text a tool hands back by meta_ads_mcp.secret_scrub, in every form an
# upstream text can repeat them and by the length rules described there: from an error with
# its ERROR policy, from a successful result with its RESULT policy. Masking by key name (a
# password key, a URL password) applies whatever the length.
# An input key holding any of these (in any letter case) holds a secret; matches the
# credential key parts errors.py strips from Meta's diagnostic data.
_SECRET_KEY_PARTS = ("password", "secret", "token", "authorization", "credential", "api_key")
# Where an input's keys are field names, so that a key naming a password or token holds
# one: the arguments themselves (the tool's named parameters), a feed schedule
# (username, password), and the Graph params passed through to Meta with the schedules they
# can carry. Every other mapping in an input is data (a feed rule's value mapping, a
# product's custom_data, a filter), whose keys are the advertiser's own values: a key there
# never makes its value a secret. A password inside a URL is found anywhere. A value at one
# of these paths given as the JSON text of an object is read as that object.
_FEED_CREDENTIAL_PATHS = frozenset({
    (), ("schedule",), ("update_schedule",),
    ("params",), ("params", "schedule"), ("params", "update_schedule"),
})
# For a tool whose params is itself a data map (a feed rule's params).
_ARGUMENTS_ONLY = frozenset({()})


def _caller_secrets(
    arguments: dict[str, Any], credential_paths: frozenset[tuple[str, ...]] = _FEED_CREDENTIAL_PATHS,
) -> list[str]:
    """Every secret the caller supplied in this call, trimmed, longest first.

    A secret is a string under a key that names a password, token, secret, authorization,
    credential or api key, where keys are field names (credential_paths: the arguments
    themselves and the documented places a credential sits), or the password of a URL
    login in any string or key at any depth. A value at one of credential_paths given as
    the JSON text of an object (Meta's documented object, as the Graph client would send
    it) is searched as that object.
    """
    found: set[str] = set()

    def add_url_passwords(text: str) -> None:
        for login in _url_logins(text):
            _user, colon, password = login.login.partition(":")
            if colon and password.strip():
                found.add(password.strip())

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    def visit(value: Any, path: tuple[str, ...] | None) -> None:
        if isinstance(value, str):
            add_url_passwords(value)
        elif isinstance(value, dict):
            fields = path is not None and path in credential_paths
            for key, item in value.items():
                if isinstance(key, str):
                    add_url_passwords(key)
                    if fields and any(part in key.lower() for part in _SECRET_KEY_PARTS):
                        add(item)
                child = (*path, key) if fields and isinstance(key, str) else None
                parsed = _json_object(item) if child in credential_paths else None
                if parsed is not None:
                    visit(item, None)
                    visit(parsed, child)
                else:
                    visit(item, child)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, None)

    visit(arguments, ())
    return sorted(found, key=lambda text: (-len(text), text))


def _call_arguments(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """The call's arguments by parameter name, however they were passed."""
    try:
        return dict(inspect.signature(fn).bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return dict(kwargs)


def _guard_secrets(
    fn: Callable[..., Awaitable[Any]],
    show: Callable[[Any], Any],
    credential_paths: frozenset[tuple[str, ...]] = _FEED_CREDENTIAL_PATHS,
) -> Callable[..., Awaitable[Any]]:
    """Run a tool so that neither its result nor an error it raises shows a password or token.

    show masks the result (password keys and URL passwords); an error is masked the same
    way. Then every secret the caller supplied in this call (found where credential_paths
    says keys are field names, and in any URL) is removed from both, by secret_scrub's
    length rules, so a secret Meta repeats as text cannot reach the response or a traceback log.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            _mask_error(exc)
            scrub_exception(exc, _caller_secrets(_call_arguments(fn, args, kwargs), credential_paths), policy=ERROR)
            raise
        secrets = _caller_secrets(_call_arguments(fn, args, kwargs), credential_paths)
        return scrub_value(show(result), secrets, policy=RESULT)

    return wrapper


def _secrets_masked(
    fn: Callable[..., Awaitable[Any]] | None = None,
    *,
    credential_paths: frozenset[tuple[str, ...]] = _FEED_CREDENTIAL_PATHS,
) -> Any:
    """Run a tool so that neither its result nor an error it raises shows a password.

    Used bare, or with credential_paths for a tool whose params is a data map.
    """
    if fn is None:
        return lambda tool: _guard_secrets(tool, _redact, credential_paths)
    return _guard_secrets(fn, _redact, credential_paths)


def _key_label(key: Any, position: int, known_keys: frozenset[str]) -> str:
    """A key as a message may show it: its name when it is one this server defines, else its position."""
    return f".{key}" if isinstance(key, str) and key in known_keys else f"[key #{position}]"


def _refuse_url_logins(value: Any, name: str, known_keys: frozenset[str] = frozenset()) -> None:
    """Refuse a caller value holding a URL with a username or password, at any depth.

    Every string inside value is checked, whether it is the URL itself or text with
    a URL in it (a description, a filter value), and so is every mapping key, so a
    login cannot reach Meta, where it would be stored and shown back. The message
    names the location by parameter, list index and key position, and names a key
    only when it is one of known_keys (names this server defines); it never quotes
    the URL or any other key.
    """
    if isinstance(value, dict):
        for position, (key, item) in enumerate(value.items(), start=1):
            label = f"{name}{_key_label(key, position, known_keys)}"
            if isinstance(key, str):
                _refuse_url_login_text(key, f"the name of key #{position} in {name}")
            _refuse_url_logins(item, label, known_keys)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_url_logins(item, f"{name}[{index}]", known_keys)
    elif isinstance(value, str):
        _refuse_url_login_text(value, name)


def _refuse_url_login_text(text: str, name: str) -> None:
    refuse_url_credentials(text, name)
    if (login := next(_url_logins(text), None)) is not None:
        refuse_url_credentials(text[login.start:].split()[0], name)


def _bounded_int(value: Any, *, name: str, low: int, high: int | None) -> None:
    """Validate an integer-like schedule value without changing its form."""
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer.")
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    elif isinstance(value, int):
        number = value
    else:
        raise ValidationError(f"{name} must be an integer.")
    if number < low or (high is not None and number > high):
        bound = f"{low} to {high}" if high is not None else f"at least {low}"
        raise ValidationError(f"{name} must be {bound}.")


def _normalize_schedule(value: Any, *, name: str) -> dict[str, Any]:
    """Validate a feed fetch schedule against Meta's ProductFeedSchedule fields."""
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object with url and interval.")
    unknown = [key for key in value if key not in SCHEDULE_KEYS]
    if unknown:
        # Counted, never named: a misplaced key can be pasted data or a secret.
        raise ValidationError(
            f"{name} has {_count_keys(len(unknown), 'unsupported key')} (not repeated here). "
            f"Accepted keys: {', '.join(SCHEDULE_KEYS)}."
        )
    normalized = dict(value)
    normalized["url"] = _validate_http_url(
        value.get("url"), name=f"{name}.url", schemes=SCHEDULE_URL_SCHEMES,
        credentials_hint=f"put them in {name}.username and {name}.password",
    )
    interval = value.get("interval")
    interval_value = interval.strip().upper() if isinstance(interval, str) else None
    if interval_value not in SCHEDULE_INTERVALS:
        raise ValidationError(
            f"{name}.interval must be one of {', '.join(SCHEDULE_INTERVALS)}."
        )
    normalized["interval"] = interval_value
    if "hour" in value:
        _bounded_int(value["hour"], name=f"{name}.hour", low=0, high=23)
    if "minute" in value:
        _bounded_int(value["minute"], name=f"{name}.minute", low=0, high=59)
    if "day_of_month" in value:
        _bounded_int(value["day_of_month"], name=f"{name}.day_of_month", low=1, high=31)
    if "interval_count" in value:
        _bounded_int(value["interval_count"], name=f"{name}.interval_count", low=1, high=None)
    if "day_of_week" in value:
        day = value["day_of_week"]
        day_value = day.strip().upper() if isinstance(day, str) else None
        if day_value not in SCHEDULE_DAYS:
            raise ValidationError(f"{name}.day_of_week must be one of {', '.join(SCHEDULE_DAYS)}.")
        normalized["day_of_week"] = day_value
    for key in ("timezone", "username", "password"):
        if key in value and not isinstance(value[key], str):
            raise ValidationError(f"{name}.{key} must be a string.")
    return normalized


def _feed_choice(
    value: Any,
    *,
    name: str,
    allowed: tuple[str, ...],
    spellings: dict[str, str] | None = None,
    strip_chars: str = "",
) -> Any:
    """Return the documented value for a feed option, OMIT when not given.

    The input is never quoted back in the error.
    """
    if value is None:
        return OMIT
    if isinstance(value, str):
        if spellings and value in spellings:
            return spellings[value]
        key = value.strip().upper()
        for char in strip_chars:
            key = key.replace(char, "")
        key = (spellings or {}).get(key, key)
        for choice in allowed:
            if choice.upper() == key:
                return choice
    raise ValidationError(f"{name} must be one of {', '.join(allowed)}.")


def _feed_options(
    *,
    delimiter: Any,
    encoding: Any,
    quoted_fields_mode: Any,
    deletion_enabled: bool | None,
) -> dict[str, Any]:
    """The parsing options both feed writes accept, validated and in Meta's form."""
    return {
        "delimiter": _feed_choice(
            delimiter, name="delimiter", allowed=FEED_DELIMITERS, spellings=_DELIMITER_CHARACTERS,
        ),
        "encoding": _feed_choice(
            encoding, name="encoding", allowed=FEED_ENCODINGS, spellings=_ENCODING_SPELLINGS, strip_chars="-_",
        ),
        "quoted_fields_mode": _feed_choice(
            quoted_fields_mode, name="quoted_fields_mode", allowed=FEED_QUOTED_FIELDS_MODES,
        ),
        "deletion_enabled": OMIT if deletion_enabled is None else deletion_enabled,
    }


def _supplementary_options(
    *,
    ingestion_source_type: Any,
    primary_feed_ids: list[str] | None,
    override_type: Any,
    override_value: str | None,
) -> dict[str, Any]:
    """Validate the inputs of a supplementary feed, which overrides fields of primary feeds."""
    source_type = _feed_choice(
        ingestion_source_type, name="ingestion_source_type", allowed=FEED_INGESTION_SOURCE_TYPES,
    )
    override = _feed_choice(override_type, name="override_type", allowed=FEED_OVERRIDE_TYPES)
    value = blank_to_none(override_value)
    supplementary = source_type == "supplementary_feed"
    if isinstance(primary_feed_ids, str):
        primary_feed_ids = coerce_csv_string_list(primary_feed_ids)
    if primary_feed_ids is not None:
        if not supplementary:
            raise ValidationError("primary_feed_ids applies only with ingestion_source_type=supplementary_feed.")
        if not primary_feed_ids or not all(
            isinstance(item, str) and item.strip().isdigit() for item in primary_feed_ids
        ):
            raise ValidationError("primary_feed_ids must be one or more numeric product feed ids.")
    if override is not OMIT and not supplementary:
        raise ValidationError("override_type applies only with ingestion_source_type=supplementary_feed.")
    if value is not None and override is OMIT:
        raise ValidationError("override_value needs override_type, the kind of value it is.")
    return {
        "ingestion_source_type": source_type,
        "primary_feed_ids": OMIT if primary_feed_ids is None else [item.strip() for item in primary_feed_ids],
        "override_type": override,
        "override_value": OMIT if value is None else value,
    }


def _upload_status(upload: dict[str, Any]) -> str:
    """Meta has no upload status field; an end_time means the upload finished."""
    return "finished" if upload.get("end_time") else "in_progress"


def _sort_upload_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order upload errors by rows affected, fatal before warning on ties."""
    def key(error: dict[str, Any]) -> tuple[bool, int, int]:
        count = to_int(error.get("total_count"))
        severity = str(error.get("severity") or "").lower()
        return (count is None, -(count or 0), 0 if severity == "fatal" else 1)

    return sorted(errors, key=key)


def _short_sample(error: dict[str, Any]) -> dict[str, Any]:
    """Replace Meta's nested samples page with one short sample (or None when Meta gave none)."""
    shaped = {key: value for key, value in error.items() if key != "samples"}
    samples = error.get("samples")
    rows = samples.get("data") if isinstance(samples, dict) else samples
    first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if first is None:
        shaped["sample"] = None
        return shaped
    sample = {key: first[key] for key in ("id", "row_number", "retailer_id") if key in first}
    retailer_id = sample.get("retailer_id")
    if isinstance(retailer_id, str) and len(retailer_id) > MAX_SAMPLE_CHARS:
        sample["retailer_id"] = retailer_id[:MAX_SAMPLE_CHARS]
        sample["retailer_id_truncated"] = True
    shaped["sample"] = sample
    return shaped


async def _read_upload_errors(
    client: Any,
    upload_id: str,
    *,
    max_errors: int,
    after: str | None,
    include_samples: bool,
) -> dict[str, Any]:
    """Read one upload's errors across pages, up to max_errors rows.

    Requests are capped by max_errors / UPLOAD_ERROR_PAGE_SIZE. The list is complete only when
    Meta's last page has no next link. A next page with no cursor, a cursor already read, or a
    page identical to one already read stops the read as incomplete, with the reason. When
    samples were asked for and Meta refuses a page with them (error code 100), on any page,
    that page and the rest are read without samples; the errors already read keep theirs,
    and the response says after how many errors samples stopped.
    """
    fields = [*UPLOAD_ERROR_FIELDS, UPLOAD_ERROR_SAMPLE_FIELD] if include_samples else list(UPLOAD_ERROR_FIELDS)
    samples_unavailable: str | None = None
    # How many errors had been read, with samples, when Meta first refused them.
    sampled_before_refusal: int | None = None
    errors: list[dict[str, Any]] = []
    cursor = after
    cursors_read: set[str | None] = {cursor}
    pages_read: set[str] = set()
    complete = False
    next_after: str | None = None
    stop_reason: str | None = None
    # One request per page; cursors_read holds one entry per request made so far.
    max_requests = -(-max_errors // UPLOAD_ERROR_PAGE_SIZE)
    while True:
        params: dict[str, Any] = {"limit": min(UPLOAD_ERROR_PAGE_SIZE, max_errors - len(errors))}
        if cursor:
            params["after"] = cursor
        try:
            payload = await client.list_objects(upload_id, "errors", fields=fields, params=params)
        except MetaApiError as error:
            if not (include_samples and samples_unavailable is None and error.code == 100):
                raise
            fields = list(UPLOAD_ERROR_FIELDS)
            sampled_before_refusal = len(errors)
            if errors:
                samples_unavailable = (
                    f"Meta refused the samples part of the error read (error code 100) after "
                    f"{len(errors)} errors had been read with samples, so that page and the rest were "
                    "read again without samples; errors read without one carry sample_unavailable."
                )
            else:
                samples_unavailable = (
                    "Meta refused the samples part of the error read (error code 100), so the errors "
                    "were read again without samples and could not be read with one."
                )
            payload = await client.list_objects(upload_id, "errors", fields=fields, params=params)
        page = normalize_collection(payload)
        if page["items"]:
            fingerprint = json.dumps(page["items"], sort_keys=True, default=str)
            if fingerprint in pages_read:
                stop_reason = (
                    "Meta returned the same page of errors again; it was not counted twice and the "
                    "read stopped, so later errors were not read."
                )
                break
            pages_read.add(fingerprint)
        if include_samples and samples_unavailable is None:
            errors.extend(_short_sample(row) if isinstance(row, dict) else row for row in page["items"])
        elif sampled_before_refusal:
            # Some errors in this response carry a sample, so say which ones cannot.
            errors.extend({**row, "sample_unavailable": True} if isinstance(row, dict) else row
                          for row in page["items"])
        else:
            errors.extend(page["items"])
        if not page["paging"].get("next"):
            complete = True
            break
        cursor = page["paging"].get("after")
        if not cursor:
            stop_reason = "Meta reported more errors but gave no cursor to read them."
            break
        if cursor in cursors_read:
            stop_reason = (
                "Meta returned a cursor that was already read; the read stopped so no error is "
                "counted twice, and later errors were not read."
            )
            break
        if len(errors) >= max_errors or len(cursors_read) >= max_requests:
            next_after = cursor
            limit_reached = (
                f"error_limit ({max_errors} errors)" if len(errors) >= max_errors
                else f"the cap of {max_requests} requests for error_limit {max_errors}"
            )
            stop_reason = (
                f"Stopped at {limit_reached}; more errors exist. Call again with upload_id and "
                "error_after set to errors_next_after to read on."
            )
            break
        cursors_read.add(cursor)
    result: dict[str, Any] = {
        "errors": _sort_upload_errors(errors),
        "errors_complete": complete,
        "errors_next_after": next_after,
        "errors_from_start": after is None,
    }
    if stop_reason:
        result["errors_stop_reason"] = stop_reason
    if include_samples:
        if samples_unavailable:
            result["samples_unavailable"] = samples_unavailable
            result["samples_unavailable_after_errors"] = sampled_before_refusal
        else:
            result["samples_note"] = (
                "Each error carries one sample: the row number and the item's retailer id in the feed "
                "file. Meta's public API gives no copy of the invalid value itself."
            )
    return result


async def _upload_with_errors(
    client: Any,
    upload: dict[str, Any],
    *,
    max_errors: int = UPLOAD_ERROR_PAGE_SIZE,
    after: str | None = None,
    include_samples: bool = False,
    requested_id: str | None = None,
) -> dict[str, Any]:
    """Attach the upload's errors (by rows affected among those read) and a derived status.

    The errors are read by the id Meta returned, or by requested_id (the upload the caller
    asked for) when Meta's row has none. Explicit zero error and warning counts mean there
    are no errors to read. An upload with neither an id nor those zero counts gets
    errors_complete=false with the reason, never an empty list called complete.
    """
    upload_id = _object_id(upload.get("id")) or requested_id
    item = {**upload, "status": _upload_status(upload)}
    error_count = to_int(upload.get("error_count"))
    warning_count = to_int(upload.get("warning_count"))
    if error_count == 0 and warning_count == 0:
        item.update({"errors": [], "errors_complete": True})
        return item
    if upload_id is None:
        item.update({
            "errors": [],
            "errors_complete": False,
            "errors_stop_reason": (
                "Meta returned this upload without an id, so its errors could not be read. Meta did not "
                "report zero errors and zero warnings for it, so it may have errors."
            ),
        })
        return item
    item.update(await _read_upload_errors(
        client, upload_id, max_errors=max_errors, after=after, include_samples=include_samples,
    ))
    return item


def _check_rule_params_shape(params: Any) -> None:
    """The checks every rule type shares: a non-empty object of string keys and values."""
    if not isinstance(params, dict) or not params:
        raise ValidationError("params must be a non-empty object of string keys and string values.")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in params.items()):
        raise ValidationError("params keys and values must all be strings.")


def _validate_rule_params(rule_type: str, params: Any) -> dict[str, str]:
    """Check rule inputs against the shapes Meta documents for each rule type."""
    _check_rule_params_shape(params)
    if rule_type == "mapping_rule":
        if not params.get("map_from", "").strip():
            raise ValidationError("mapping_rule params need map_from, the column name in your feed file.")
    elif rule_type == "letter_case_rule":
        if params.get("type") not in LETTER_CASE_TYPES:
            raise ValidationError(f"letter_case_rule params.type must be one of {', '.join(LETTER_CASE_TYPES)}.")
    elif rule_type == "regex_replace_rule":
        regex = params.get("regex")
        if not regex:
            raise ValidationError("regex_replace_rule params need regex.")
        try:
            re.compile(regex)
        except re.error as exc:
            # Python's own message can quote part of the pattern (a group name), so only the
            # position is given, and the original error is not chained.
            where = f" (the problem is at character {exc.pos})" if isinstance(exc.pos, int) else ""
            failure = ValidationError(
                f"regex_replace_rule params.regex is not a valid regular expression{where}."
            )
        else:
            failure = None
        if failure is not None:
            raise failure
    elif rule_type == "fallback_rule":
        if not params.get("user_default_value", "").strip():
            raise ValidationError("fallback_rule params need user_default_value.")
    elif rule_type == "value_mapping_rule":
        if len(params) > VALUE_MAPPING_MAX_PAIRS:
            raise ValidationError(f"value_mapping_rule allows at most {VALUE_MAPPING_MAX_PAIRS} mappings.")
        if any(len(text) > VALUE_MAPPING_MAX_LENGTH for pair in params.items() for text in pair):
            raise ValidationError(f"value_mapping_rule strings can be at most {VALUE_MAPPING_MAX_LENGTH} characters.")
    return dict(params)


async def _connected_sources(
    client: Any, catalog_id: str, stop_reasons: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    return await _collect_edge(
        client, catalog_id, "external_event_sources",
        fields=EVENT_SOURCE_FIELDS, page_size=100, max_pages=5, stop_reasons=stop_reasons,
    )


def _match_rate(matched: int | None, unmatched: int | None) -> float | None:
    """Matched share of content ids; None unless both counters are known."""
    if matched is None or unmatched is None:
        return None
    total = matched + unmatched
    return round(matched / total, 4) if total else None


def _add_counts(totals: list[int], matched: int | None, unmatched: int | None) -> None:
    """Add one row's counters; [matched, unmatched, rows missing matched, rows missing unmatched]."""
    if matched is None:
        totals[2] += 1
    else:
        totals[0] += matched
    if unmatched is None:
        totals[3] += 1
    else:
        totals[1] += unmatched


def _counts_view(totals: list[int]) -> dict[str, Any]:
    """A total is unknown (None) when any row behind it lacked that counter."""
    matched = None if totals[2] else totals[0]
    unmatched = None if totals[3] else totals[1]
    return {
        "matched_content_ids": matched,
        "unmatched_content_ids": unmatched,
        "match_rate": _match_rate(matched, unmatched),
    }


def _known_sum(values: Any) -> int | None:
    """Sum counters; None when any of them is unknown."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _summarize_event_stats(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], int]:
    """Group event_stats rows per source: daily match history and per-event totals.

    A counter Meta omitted is unknown, not zero: any total it feeds is None, and
    match rates are computed only when both counters are known.
    """
    per_source: dict[str, dict[str, Any]] = {}
    unattributed = 0
    for row in rows:
        source_id = _object_id(row.get("event_source"))
        if source_id is None:
            unattributed += 1
            continue
        matched = to_int(row.get("total_matched_content_ids"))
        unmatched = to_int(row.get("total_unmatched_content_ids"))
        bucket = per_source.setdefault(
            source_id,
            {"days": {}, "events": {}, "window": [0, 0, 0, 0], "rows_missing_counters": 0, "other": [0, 0]},
        )
        if matched is None or unmatched is None:
            bucket["rows_missing_counters"] += 1
        other = to_int(row.get("total_content_ids_matched_other_catalogs"))
        if other is None:
            bucket["other"][1] += 1
        else:
            bucket["other"][0] += other
        day = str(row.get("date_start") or row.get("date_stop") or "unknown")
        _add_counts(bucket["days"].setdefault(day, [0, 0, 0, 0]), matched, unmatched)
        event = str(row.get("event") or "unknown")
        _add_counts(bucket["events"].setdefault(event, [0, 0, 0, 0]), matched, unmatched)
        _add_counts(bucket["window"], matched, unmatched)
    summaries: dict[str, dict[str, Any]] = {}
    for source_id, bucket in per_source.items():
        window = _counts_view(bucket["window"])
        summaries[source_id] = {
            "matched_content_ids": window["matched_content_ids"],
            "unmatched_content_ids": window["unmatched_content_ids"],
            "window_match_rate": window["match_rate"],
            "rows_missing_counters": bucket["rows_missing_counters"],
            "content_ids_matched_other_catalogs": None if bucket["other"][1] else bucket["other"][0],
            "match_rate_history": [
                {"date": day, **_counts_view(totals)}
                for day, totals in sorted(bucket["days"].items())
            ],
            "by_event": {
                event: _counts_view(totals)
                for event, totals in sorted(bucket["events"].items())
            },
        }
    return summaries, unattributed


def _across_catalogs(stats: dict[str, Any] | None, stats_complete: bool) -> dict[str, Any]:
    """This catalog's rate for a source beside the share that matched its other catalogs.

    Computed only from a finished event_stats read with both counters known.
    """
    view: dict[str, Any] = {
        "this_catalog_match_rate": None,
        "content_ids_matched_other_catalogs": None,
        "other_catalogs_match_share": None,
        "all_catalogs_match_rate_range": None,
        "note": None,
    }
    if stats is None:
        view["note"] = "No event_stats rows for this source were read, so no rates are available."
        return view
    if not stats_complete:
        view["note"] = "event_stats was only partly read, so the rates were not computed."
        return view
    matched, unmatched = stats["matched_content_ids"], stats["unmatched_content_ids"]
    other = stats["content_ids_matched_other_catalogs"]
    this_rate = _match_rate(matched, unmatched)
    view["this_catalog_match_rate"] = this_rate
    view["content_ids_matched_other_catalogs"] = other
    if this_rate is None:
        view["note"] = "Meta omitted a matched or unmatched counter, or reported no content ids, so no rate."
        return view
    if other is None:
        view["note"] = "Meta omitted the other-catalogs counter on some rows, so the all-catalog range is unknown."
        return view
    other_share = round(other / (matched + unmatched), 4)
    view["other_catalogs_match_share"] = other_share
    view["all_catalogs_match_rate_range"] = {
        "low": max(this_rate, other_share),
        "high": round(min(1.0, this_rate + other_share), 4),
    }
    return view


def _validate_threshold(match_rate_threshold: float) -> None:
    if not 0 < match_rate_threshold <= 1:
        raise ValidationError("match_rate_threshold must be greater than 0 and at most 1.")


def _sorted_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda finding: _SEVERITY_ORDER.get(finding["severity"], 3))


async def _catalog_signal_inputs(client: Any, catalog_id: str) -> dict[str, Any]:
    """Read connected sources and 28-day event stats for one product catalog.

    Each incomplete read adds a plain-language entry to scan_gaps, for missing_signals.
    """
    source_reasons: list[str] = []
    sources, sources_complete = await _connected_sources(client, catalog_id, source_reasons)
    stat_reasons: list[str] = []
    stat_rows, stats_complete = await _collect_edge(
        client, catalog_id, "event_stats", fields=EVENT_STAT_FIELDS, page_size=500,
        stop_reasons=stat_reasons,
    )
    stats, unattributed = _summarize_event_stats(stat_rows)
    scan_gaps: list[str] = []
    if not sources_complete:
        scan_gaps.append(
            "The connected-source list is partial: "
            f"{' '.join(source_reasons) or 'external_event_sources had more pages than were read.'}"
        )
    if not stats_complete:
        scan_gaps.append(
            f"event_stats was only partly read ({len(stat_rows)} rows), so 28-day counts and match "
            f"rates are partial: {' '.join(stat_reasons) or 'more pages exist.'}"
        )
    return {
        "sources": sources,
        "sources_complete": sources_complete,
        "stat_rows": len(stat_rows),
        "stats_complete": stats_complete,
        "stats": stats,
        "unattributed_rows": unattributed,
        "scan_gaps": scan_gaps,
    }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@mcp_server.tool()
@_secrets_masked
async def list_product_feeds(
    catalog_id: str | None = None,
    feed_id: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List data feeds on a Meta product catalog, or pass feed_id for one feed's settings, fetch schedules, and latest upload. Give exactly one of catalog_id or feed_id."""
    catalog_id = blank_to_none(catalog_id)
    feed_id = blank_to_none(feed_id)
    if (catalog_id is None) == (feed_id is None):
        raise ValidationError("Provide exactly one of catalog_id or feed_id.")
    _validate_limit(limit)
    client = get_graph_api_client()
    if feed_id is not None:
        feed, unavailable = await _read_with_optional_fields(
            lambda fields: client.get_object(feed_id, fields=fields), FEED_FIELDS,
        )
        result: dict[str, Any] = {"item": feed, "summary": {"count": 1}}
    else:
        payload, unavailable = await _read_with_optional_fields(
            lambda fields: client.list_objects(
                catalog_id, "product_feeds", fields=fields, params=_page_params(limit, after),
            ),
            FEED_LIST_FIELDS,
        )
        result = _paged_collection(payload, after)
    if unavailable:
        result["summary"]["fields_unavailable"] = unavailable
    return result


async def _read_with_optional_fields(
    read: Callable[[list[str]], Awaitable[Any]], fields: list[str],
) -> tuple[Any, dict[str, str]]:
    """Read with FEED_OPTIONAL_FIELDS added; if Meta refuses the read as an invalid field
    (error code 100), read again without them and name them as unavailable.

    Any other error, or a refusal that persists without them, is raised.
    """
    try:
        return await read([*fields, *FEED_OPTIONAL_FIELDS]), {}
    except MetaApiError as error:
        if error.code != 100:
            raise
    payload = await read(list(fields))
    reason = (
        "Meta refused this field when it was requested (error code 100), so the feed was read "
        "without it. Meta's reference does not list it; its readability rests on the SDK."
    )
    return payload, {field: reason for field in FEED_OPTIONAL_FIELDS}


@mcp_server.tool()
@_secrets_masked
async def create_product_feed(
    catalog_id: str,
    name: str,
    schedule: _OptionalSchedule = None,
    update_schedule: _OptionalSchedule = None,
    file_name: str | None = None,
    country: str | None = None,
    default_currency: str | None = None,
    params: dict[str, Any] | None = None,
    feed_type: str | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
    quoted_fields_mode: str | None = None,
    deletion_enabled: bool | None = None,
    ingestion_source_type: str | None = None,
    primary_feed_ids: StringList | None = None,
    override_type: str | None = None,
    override_value: str | None = None,
) -> dict[str, Any]:
    """Register a data feed on a product catalog. schedule {url, interval HOURLY|DAILY|WEEKLY|MONTHLY, hour, ...} fetches the file on a timer and replaces items; update_schedule only adds or updates items. Omit both to upload manually. feed_type sets the item type (PRODUCTS by default; HOTEL, FLIGHT, DESTINATION, HOME_LISTING, VEHICLES, MEDIA_TITLE and others). Parsing: delimiter (COMMA, TAB, BAR, TILDE, SEMICOLON, AUTODETECT, or the character), encoding (UTF8, LATIN1, UTF16LE, ...), quoted_fields_mode (ON, OFF, AUTODETECT). deletion_enabled=true removes items missing from a later file and cannot be turned off afterwards. A supplementary feed (ingestion_source_type=supplementary_feed) names its primary_feed_ids and may set override_type (LANGUAGE, COUNTRY, ...) and override_value."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    feed_name = blank_to_none(name)
    if feed_name is None:
        raise ValidationError("name is required.")
    payload: dict[str, Any] = {
        "name": feed_name,
        "schedule": OMIT if schedule is None else _normalize_schedule(schedule, name="schedule"),
        "update_schedule": (
            OMIT if update_schedule is None
            else _normalize_schedule(update_schedule, name="update_schedule")
        ),
        "file_name": blank_to_none(file_name) or OMIT,
        "country": (blank_to_none(country) or "").upper() or OMIT,
        "default_currency": (blank_to_none(default_currency) or "").upper() or OMIT,
        "feed_type": _feed_choice(feed_type, name="feed_type", allowed=FEED_TYPES),
        **_feed_options(
            delimiter=delimiter, encoding=encoding, quoted_fields_mode=quoted_fields_mode,
            deletion_enabled=deletion_enabled,
        ),
        **_supplementary_options(
            ingestion_source_type=ingestion_source_type, primary_feed_ids=primary_feed_ids,
            override_type=override_type, override_value=override_value,
        ),
    }
    _refuse_url_logins(params, "params", _FEED_KNOWN_KEYS)
    data = merge_graph_payload(payload, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(catalog_id, "product_feeds", data=data)
    return creation_response(
        action="create_product_feed",
        target={"catalog_id": catalog_id},
        # Meta may return the schedule it was sent; _secrets_masked masks any password.
        result=created,
    )


@mcp_server.tool()
@_secrets_masked
async def update_product_feed(
    feed_id: str,
    name: str | None = None,
    schedule: _OptionalSchedule = None,
    update_schedule: _OptionalSchedule = None,
    default_currency: str | None = None,
    params: dict[str, Any] | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
    quoted_fields_mode: str | None = None,
    deletion_enabled: bool | None = None,
) -> dict[str, Any]:
    """Edit a product catalog data feed's name, default currency, parsing options (delimiter, encoding, quoted_fields_mode, with the values create_product_feed takes), deletion_enabled, or fetch schedules, returning before and after. schedule replaces the timed full fetch, update_schedule the timed add-or-update fetch. A schedule cannot be removed or paused here: Meta's public API documents no way to clear one, so to stop timed fetches the only documented route is delete_product_feed, which also removes the feed. deletion_enabled cannot be turned off once on."""
    feed_id = _require_id(feed_id, "feed_id")
    payload: dict[str, Any] = {
        "name": OMIT,
        "schedule": OMIT if schedule is None else _normalize_schedule(schedule, name="schedule"),
        "update_schedule": (
            OMIT if update_schedule is None
            else _normalize_schedule(update_schedule, name="update_schedule")
        ),
        "default_currency": (blank_to_none(default_currency) or "").upper() or OMIT,
        **_feed_options(
            delimiter=delimiter, encoding=encoding, quoted_fields_mode=quoted_fields_mode,
            deletion_enabled=deletion_enabled,
        ),
    }
    if name is not None:
        if blank_to_none(name) is None:
            raise ValidationError("name cannot be blank.")
        payload["name"] = name.strip()
    _refuse_url_logins(params, "params", _FEED_KNOWN_KEYS)
    data = merge_graph_payload(payload, params)
    if not data:
        raise ValidationError("At least one field must be provided for update_product_feed.")
    extra_reads = [key for key in data if key in FEED_READABLE_FIELDS and key not in FEED_UPDATE_READ_FIELDS]
    client = get_graph_api_client()
    previous = await client.get_object(feed_id, fields=[*FEED_UPDATE_READ_FIELDS, *extra_reads])
    if data.get("deletion_enabled") is False and previous.get("deletion_enabled") is True:
        # Meta's reference: "Once enabled, we do not allow this field to be disabled."
        raise ValidationError(
            "deletion_enabled is on for this feed and cannot be turned off once enabled; nothing was changed."
        )
    await client.update_object(feed_id, data=data)
    unavailable: dict[str, str] = {}
    for key in data:
        if key not in FEED_READABLE_FIELDS:
            unavailable[key] = "not a field this server reads back from a product feed"
        elif key not in previous:
            unavailable[key] = "Meta returned no value for it; it may have been unset"
    for key in ("schedule", "update_schedule"):
        schedule_value = data.get(key) if isinstance(data.get(key), dict) else _json_object(data.get(key))
        if schedule_value is not None and "password" in schedule_value:
            unavailable[f"{key}.password"] = "schedule passwords are never shown in responses"
    response = mutation_response(
        action="update_product_feed",
        target={"feed_id": feed_id},
        previous={key: previous[key] for key in data if key in FEED_READABLE_FIELDS and key in previous},
        current=data,
    )
    response["previous_unavailable"] = unavailable
    return response


@mcp_server.tool()
@_secrets_masked
async def delete_product_feed(feed_id: str) -> dict[str, Any]:
    """Use only on an explicit request to delete a product catalog data feed and its schedule. Products the feed supplied are removed from the catalog afterwards unless another source also supplies them, and ads using them stop. Returns the feed's prior settings."""
    feed_id = _require_id(feed_id, "feed_id")
    client = get_graph_api_client()
    previous = await client.get_object(feed_id, fields=FEED_FIELDS)
    result = await client.delete_object(feed_id)
    return mutation_response(
        action="delete_product_feed",
        target={"feed_id": feed_id},
        previous=previous,
        current={"deleted": True, "result": result},
    )


def _read_feed_file(file_path: Any) -> tuple[str, bytes, str]:
    """Read a local feed file in the server, only from the operator's upload folder.

    Errors never quote the path or the contents.
    """
    upload = read_upload_file(
        file_path,
        allowed_types=FEED_FILE_TYPES,
        max_bytes=MAX_FEED_FILE_BYTES,
    )
    return upload.name, upload.content, upload.content_type


@mcp_server.tool()
@_secrets_masked
async def create_product_feed_upload(
    feed_id: str,
    url: _OptionalText = None,
    update_only: bool | None = None,
    file_path: _OptionalText = None,
    username: _OptionalText = None,
    password: _OptionalText = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Start a one-off upload for a product catalog data feed, from a url (only http and https addresses are accepted for a one-off upload; Meta documents no FTP address for it, though a feed schedule may use one; optional basic-auth username and password) or from a local file_path (CSV, TSV, XML, zip, gz, bz2) that this server reads and sends, so the file's rows never pass through the conversation. update_only=true only adds or updates items; otherwise items missing from the file may be deleted. The password is never returned. dry_run=true returns the request and sends nothing. Check progress with list_product_feed_uploads."""
    feed_id = _require_id(feed_id, "feed_id")
    url_text = url if isinstance(url, str) and url.strip() else None
    file_given = file_path is not None
    if (url_text is not None) == file_given:
        raise ValidationError(
            "Provide exactly one of url (an http or https address) or file_path (a local feed file)."
        )
    if (username is None) != (password is None):
        raise ValidationError("username and password must be given together.")
    credentials = username is not None
    if credentials and file_given:
        raise ValidationError("username and password apply only to a url upload, not to file_path.")
    if credentials and not (
        isinstance(username, str) and username.strip() and isinstance(password, str) and password
    ):
        raise ValidationError("username and password must be non-blank strings.")
    data: dict[str, Any] = {}
    files: dict[str, Any] | None = None
    source: dict[str, Any]
    if url_text is not None:
        data["url"] = _validate_http_url(
            url_text, name="url", schemes=("http", "https"),
            credentials_hint="pass them as the username and password parameters",
        )
        source = {"type": "url", "url": data["url"], "credentials_sent": credentials}
    else:
        file_name, content, content_type = _read_feed_file(file_path)
        files = {"file": (file_name, content, content_type)}
        source = {"type": "file", "file_name": file_name, "size_bytes": len(content), "content_type": content_type}
    if update_only is not None:
        data["update_only"] = update_only
    if credentials:
        data["username"] = username.strip()
        data["password"] = password
    target = {"feed_id": feed_id}
    if dry_run:
        would_send: dict[str, Any] = {
            "method": "POST",
            "path": f"{feed_id}/uploads",
            "data": {**data, "password": "[redacted]"} if credentials else dict(data),
        }
        if files is not None:
            would_send["files"] = {
                "file": {key: source[key] for key in ("file_name", "size_bytes", "content_type")},
            }
        return {
            "ok": True,
            "action": "create_product_feed_upload",
            "target": target,
            "dry_run": True,
            "sent": False,
            "would_send": would_send,
            "upload_source": source,
        }
    client = get_graph_api_client()
    if files is None:
        created = await client.create_edge_object(feed_id, "uploads", data=data)
    else:
        created = await client.create_edge_object(feed_id, "uploads", data=data, files=files)
    response = creation_response(
        action="create_product_feed_upload",
        target=target,
        # Never hand a password back, even if Meta echoes the request.
        result={key: value for key, value in created.items() if key != "password"}
        if isinstance(created, dict) else created,
    )
    response["dry_run"] = False
    response["upload_source"] = source
    return response


@mcp_server.tool()
@_secrets_masked
async def list_product_feed_uploads(
    feed_id: str,
    upload_id: str | None = None,
    limit: int = 10,
    after: str | None = None,
    include_samples: bool = False,
    error_limit: int | None = None,
    error_after: str | None = None,
) -> dict[str, Any]:
    """List uploads for a product catalog data feed, newest first, with item counts and errors ordered by rows affected; pass upload_id for one upload. status is finished when Meta reports an end_time. Each upload carries its first 100 errors; with upload_id, error_limit (up to 1000) reads more and error_after continues from errors_next_after. include_samples=true adds one sample per error: the row number and item retailer id in the feed file (Meta gives no copy of the invalid value)."""
    feed_id = _require_id(feed_id, "feed_id")
    upload_id = blank_to_none(upload_id)
    _validate_limit(limit)
    if not isinstance(include_samples, bool):
        raise ValidationError("include_samples must be true or false.")
    error_after = blank_to_none(error_after)
    if upload_id is None and (error_limit is not None or error_after is not None):
        raise ValidationError("error_limit and error_after apply only with upload_id, to one upload's errors.")
    if error_limit is not None and (
        isinstance(error_limit, bool) or not isinstance(error_limit, int)
        or not 1 <= error_limit <= MAX_UPLOAD_ERRORS
    ):
        raise ValidationError(f"error_limit must be an integer from 1 to {MAX_UPLOAD_ERRORS}.")
    client = get_graph_api_client()
    if upload_id is not None:
        upload = await client.get_object(upload_id, fields=UPLOAD_FIELDS)
        return {
            "item": await _upload_with_errors(
                client, upload, max_errors=error_limit or UPLOAD_ERROR_PAGE_SIZE, after=error_after,
                include_samples=include_samples, requested_id=upload_id,
            ),
            "feed_id": feed_id,
            "summary": {"count": 1},
        }
    payload = await client.list_objects(
        feed_id, "uploads", fields=UPLOAD_FIELDS, params=_page_params(limit, after),
    )
    collection = _paged_collection(payload, after)
    collection["items"] = [
        await _upload_with_errors(client, upload, include_samples=include_samples)
        for upload in collection["items"]
    ]
    return collection


@mcp_server.tool()
@_secrets_masked
async def list_feed_rules(
    feed_id: str,
    limit: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """List the attribute mapping and transformation rules applied to a product catalog data feed's uploads."""
    feed_id = _require_id(feed_id, "feed_id")
    _validate_limit(limit)
    client = get_graph_api_client()
    payload = await client.list_objects(
        feed_id, "rules", fields=RULE_FIELDS, params=_page_params(limit, after),
    )
    return _paged_collection(payload, after)


@mcp_server.tool()
# params is a data map here (value_mapping_rule keys are product values), not field names.
@_secrets_masked(credential_paths=_ARGUMENTS_ONLY)
async def create_feed_rule(
    feed_id: str,
    attribute: str,
    rule_type: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Add a rule to a product catalog data feed. rule_type and params: mapping_rule {map_from}, value_mapping_rule {old: new} (max 10), letter_case_rule {type}, regex_replace_rule {regex}, fallback_rule {user_default_value}. One rule per attribute and rule_type."""
    feed_id = _require_id(feed_id, "feed_id")
    attribute_name = _require_id(attribute, "attribute")
    normalized_type = rule_type.strip().lower() if isinstance(rule_type, str) else ""
    if normalized_type not in FEED_RULE_TYPES:
        raise ValidationError(f"rule_type must be one of {', '.join(FEED_RULE_TYPES)}.")
    rule_params = _validate_rule_params(normalized_type, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(
        feed_id,
        "rules",
        data={"attribute": attribute_name, "rule_type": normalized_type, "params": rule_params},
    )
    return creation_response(
        action="create_feed_rule",
        target={"feed_id": feed_id},
        result=created,
    )


@mcp_server.tool()
# params is a data map here (value_mapping_rule keys are product values), not field names.
@_secrets_masked(credential_paths=_ARGUMENTS_ONLY)
async def update_feed_rule(
    rule_id: GraphId,
    params: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Change the params of one product catalog feed rule, returning before and after. params replaces the rule's params in full, in the shape its rule_type takes (see create_feed_rule). A rule's attribute and rule_type cannot be changed: delete the rule and create a new one. dry_run=true reads the rule and returns the request without sending it."""
    rule_id = _require_id(rule_id, "rule_id")
    _check_rule_params_shape(params)
    client = get_graph_api_client()
    rule = await client.get_object(rule_id, fields=RULE_FIELDS)
    rule_type = str(rule.get("rule_type") or "").strip().lower()
    notes: list[str] = []
    if rule_type in FEED_RULE_TYPES:
        new_params = _validate_rule_params(rule_type, params)
    else:
        new_params = dict(params)
        notes.append(
            "Meta returned no recognised rule_type for this rule, so only the general params checks "
            "(string keys and values) ran; the checks for a specific rule type did not."
        )
    previous = {key: rule[key] for key in ("attribute", "rule_type", "params") if key in rule}
    current: dict[str, Any] = {
        **{key: previous[key] for key in ("attribute", "rule_type") if key in previous},
        "params": new_params,
    }
    request = {"method": "POST", "path": rule_id, "data": {"params": new_params}}
    unchanged = rule.get("params") == new_params
    if unchanged:
        current["unchanged"] = True
    response = mutation_response(
        action="update_feed_rule",
        target={"rule_id": rule_id},
        previous=previous,
        current=current,
    )
    response.update({"dry_run": dry_run, "missing_signals": notes})
    if unchanged:
        notes.append("The rule already has these params; nothing was sent.")
        response.update({"sent": False, "would_send": None})
        return response
    if dry_run:
        response.update({"sent": False, "would_send": request})
        return response
    response["result"] = await client.update_object(rule_id, data=request["data"])
    response["sent"] = True
    return response


# Meta's reference documents DELETE /{rule_id}, but the live API (v26.0, seen 2026-11-13)
# answers code 100, subcode 33, "Accesssing Deprecated Platform API Endpoint".
RULE_DELETE_RETIRED = (
    "Meta has retired deleting a feed rule through its API, so the rule was not deleted. "
    "Remove the rule in Commerce Manager, change what it does with update_feed_rule, or delete "
    "the whole feed with delete_product_feed, which removes its rules and the items it loaded."
)


def _is_retired_endpoint(error: MetaApiError) -> bool:
    return error.code == 100 and "deprecated platform api endpoint" in (error.message or "").lower()


@mcp_server.tool()
@_secrets_masked
async def delete_feed_rule(rule_id: str) -> dict[str, Any]:
    """Use only on an explicit request to delete one product catalog feed rule. The feed and its products are unaffected; later uploads are no longer transformed by the rule. Returns the rule as it was. Meta's live API has answered that this delete is retired; when it does, the rule is not deleted and the error says what to do."""
    rule_id = _require_id(rule_id, "rule_id")
    client = get_graph_api_client()
    previous = await client.get_object(rule_id, fields=RULE_FIELDS)
    try:
        result = await client.delete_object(rule_id)
    except MetaApiError as error:
        if _is_retired_endpoint(error):
            raise UnsupportedFeatureError(RULE_DELETE_RETIRED) from error
        raise
    return mutation_response(
        action="delete_feed_rule",
        target={"rule_id": rule_id},
        previous=previous,
        current={"deleted": True, "result": result},
    )


@mcp_server.tool()
@_secrets_masked
async def list_catalog_event_sources(
    catalog_id: str,
    limit: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """List the pixels, apps, and other event sources connected to a Meta product catalog."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    _validate_limit(limit)
    client = get_graph_api_client()
    payload = await client.list_objects(
        catalog_id, "external_event_sources",
        fields=EVENT_SOURCE_FIELDS, params=_page_params(limit, after),
    )
    return _paged_collection(payload, after)


def _canonical_event_name(event_name: str | None, include_match_rates: bool) -> str | None:
    text = blank_to_none(event_name)
    if text is None:
        return None
    if not include_match_rates:
        raise ValidationError("event_name only applies with include_match_rates=true.")
    for name in CATALOG_EVENT_NAMES:
        if name.lower() == text.lower():
            return name
    raise ValidationError(f"event_name must be one of {', '.join(CATALOG_EVENT_NAMES)}.")


async def _source_rate_in_catalog(
    client: Any, catalog_id: str, source_id: str, event_name: str | None,
) -> dict[str, Any]:
    """One source's 28-day match rate in one catalog, from that catalog's event_stats.

    A rate is given only when the read finished and both counters are known.
    """
    reasons: list[str] = []
    rows, complete = await _collect_edge(
        client, catalog_id, "event_stats", fields=EVENT_STAT_FIELDS, page_size=500,
        max_pages=MATCH_RATE_STATS_PAGES, stop_reasons=reasons,
    )
    own = [row for row in rows if _object_id(row.get("event_source")) == source_id]
    if event_name is not None:
        own = [row for row in own if row.get("event") == event_name]
    stats = _summarize_event_stats(own)[0].get(source_id)
    matched = stats["matched_content_ids"] if stats else None
    unmatched = stats["unmatched_content_ids"] if stats else None
    base = {
        "event_stat_rows": len(own),
        "events": sorted({str(row.get("event") or "unknown") for row in own}),
        "stats_complete": complete,
    }
    empty = {"match_rate": None, "matched_content_ids": None, "unmatched_content_ids": None}
    if not complete:
        return {
            "status": "partial_read", **empty, **base,
            "partial_counts_from_pages_read": {"matched_content_ids": matched, "unmatched_content_ids": unmatched},
            "note": f"event_stats was only partly read, so no rate is given: {' '.join(reasons)}",
        }
    if stats is None:
        return {
            "status": "no_events_in_window", **empty, **base,
            "note": "Meta reported no rows for this source"
            f"{' and event' if event_name else ''} in this catalog's 28-day event_stats window.",
        }
    rate = stats["window_match_rate"]
    if rate is None:
        status = "counters_missing" if matched is None or unmatched is None else "no_content_ids"
        return {
            "status": status, "match_rate": None, "matched_content_ids": matched,
            "unmatched_content_ids": unmatched, **base,
            "note": "Meta omitted a matched or unmatched counter on some rows, so the rate is unknown."
            if status == "counters_missing" else "The rows read counted no content ids, so there is no rate.",
        }
    return {
        "status": "computed", "match_rate": rate, "matched_content_ids": matched,
        "unmatched_content_ids": unmatched, **base,
    }


@mcp_server.tool()
@_secrets_masked
async def list_event_source_catalogs(
    event_source_id: str,
    business_id: str | None = None,
    include_match_rates: bool = False,
    event_name: str | None = None,
) -> dict[str, Any]:
    """Find the product catalogs one pixel or app is connected to by checking a business's owned catalogs. business_id defaults to the pixel's owner business; the summary says how many catalogs were checked and whether the scan was complete. include_match_rates=true also computes the source's 28-day match rate in each connected catalog (one more read per catalog, first 10 only), optionally for one event_name such as Purchase."""
    event_source_id = _require_id(event_source_id, "event_source_id")
    business_id = blank_to_none(business_id)
    event_name = _canonical_event_name(event_name, include_match_rates)
    client = get_graph_api_client()
    if business_id is None:
        source = await client.get_object(event_source_id, fields=["id", "owner_business"])
        business_id = _object_id(source.get("owner_business"))
        if business_id is None:
            raise ValidationError(
                "The event source has no readable owner_business; pass business_id to choose "
                "which business's catalogs to check."
            )
    stop_reasons: list[str] = []
    catalogs, catalogs_complete = await _collect_edge(
        client, business_id, "owned_product_catalogs",
        fields=CATALOG_SCAN_FIELDS, page_size=CATALOG_SCAN_PAGE_SIZE,
        max_items=MAX_CATALOGS_SCANNED, stop_reasons=stop_reasons,
    )
    matches: list[dict[str, Any]] = []
    sources_complete = True
    for catalog in catalogs:
        catalog_id = _object_id(catalog.get("id"))
        if catalog_id is None:
            continue
        sources, complete = await _connected_sources(client, catalog_id, stop_reasons)
        sources_complete = sources_complete and complete
        if any(_object_id(source.get("id")) == event_source_id for source in sources):
            matches.append(catalog)
    complete = catalogs_complete and sources_complete
    note = (
        "Every owned catalog of the business was checked."
        if complete
        else f"The scan is incomplete after checking {len(catalogs)} catalogs: "
        f"{' '.join(stop_reasons)} Catalogs or sources not read may also be connected."
    )
    summary: dict[str, Any] = {
        "count": len(matches),
        "event_source_id": event_source_id,
        "business_id": business_id,
        "catalogs_scanned": len(catalogs),
        "complete": complete,
        "stop_reasons": stop_reasons,
        "method": "derived: each catalog owned by the business is checked for this source "
        "on its external_event_sources edge; catalogs shared with the business by others "
        "are not checked.",
        "note": note,
    }
    if not include_match_rates:
        return {"items": matches, "summary": summary}
    items: list[dict[str, Any]] = []
    not_checked: list[str] = []
    for index, catalog in enumerate(matches):
        catalog_id = _object_id(catalog.get("id"))
        if index < MAX_MATCH_RATE_CATALOGS and catalog_id is not None:
            rate = await _source_rate_in_catalog(client, catalog_id, event_source_id, event_name)
        else:
            not_checked.append(catalog_id or "")
            rate = {
                "status": "not_checked",
                "match_rate": None,
                "note": f"Only the first {MAX_MATCH_RATE_CATALOGS} connected catalogs get a match-rate read.",
            }
        items.append({**catalog, "match_rate": rate})
    computed = [item for item in items if item["match_rate"]["status"] == "computed"]
    ranked = sorted(computed, key=lambda item: -item["match_rate"]["match_rate"])
    summary["match_rates"] = {
        "event_name": event_name,
        "catalogs_checked": len(items) - len(not_checked),
        "catalogs_with_rate": len(computed),
        "catalogs_not_checked": not_checked,
        "max_catalogs_checked": MAX_MATCH_RATE_CATALOGS,
        "max_event_stats_pages_per_catalog": MATCH_RATE_STATS_PAGES,
        "ranked_by_match_rate": [item["id"] for item in ranked],
        "ranking_covers_every_connected_catalog": complete and len(computed) == len(items),
        "method": "derived: for each connected catalog, this source's rows in the catalog's event_stats "
        "(last 28 days) are summed. A rate is given only when that read finished and Meta reported both "
        "counters; a counter Meta omitted is unknown, not zero.",
        "definition": _MATCH_RATE_NOTE,
        "caution": "Each catalog's rate counts content ids (not de-duplicated) against that catalog alone. "
        "A higher rate does not by itself mean a catalog is the right one for this source; "
        + ("all events are counted, so catalogs can differ in which events they received; pass event_name "
           "to compare one event." if event_name is None else f"only {event_name} events are counted."),
    }
    return {"items": items, "summary": summary}


@mcp_server.tool()
@_secrets_masked
async def get_catalog_event_source_health(
    catalog_id: str,
    match_rate_threshold: float = DEFAULT_MATCH_RATE_THRESHOLD,
    event_source_id: GraphId | None = None,
) -> dict[str, Any]:
    """Health of each pixel or app connected to a product catalog: daily match-rate history over Meta's 28-day event_stats window, per-event match rates, failed Dynamic Ads checks ranked as findings, and each source's share of content ids that matched its other catalogs beside this catalog's rate. event_source_id limits the report to one connected source."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    _validate_threshold(match_rate_threshold)
    scope_id = blank_to_none(event_source_id)
    client = get_graph_api_client()
    inputs = await _catalog_signal_inputs(client, catalog_id)
    findings: list[dict[str, Any]] = []
    missing: list[str] = []
    reports: list[dict[str, Any]] = []
    sources = inputs["sources"]
    if scope_id is not None:
        sources = [source for source in sources if _object_id(source.get("id")) == scope_id]
        if not sources and inputs["sources_complete"]:
            raise ValidationError(
                f"Event source {scope_id} is not connected to catalog {catalog_id}; "
                "list_catalog_event_sources shows the connected ones."
            )
        if not sources:
            sources = [{"id": scope_id}]
            missing.append(
                f"Source {scope_id} was not among the connected sources read (that list is partial), "
                "so its name and type are unknown."
            )
    for source in sources:
        source_id = _object_id(source.get("id"))
        if source_id is None:
            continue
        source_type = str(source.get("source_type") or "").upper()
        stats = inputs["stats"].get(source_id)
        report: dict[str, Any] = {
            "id": source_id,
            "name": source.get("name"),
            "source_type": source.get("source_type"),
            "history_available": stats is not None,
            # False when event_stats stopped early: the counts below then cover only the
            # pages read, are not 28-day totals, and no window rate is given from them.
            "counts_complete": inputs["stats_complete"],
            "match_rate_history": stats["match_rate_history"] if stats else [],
            "window_match_rate": stats["window_match_rate"] if stats and inputs["stats_complete"] else None,
            "matched_content_ids": stats["matched_content_ids"] if stats else None,
            "unmatched_content_ids": stats["unmatched_content_ids"] if stats else None,
            "by_event": stats["by_event"] if stats else {},
            "across_catalogs": _across_catalogs(stats, inputs["stats_complete"]),
            "checks": [],
            "failed_checks": [],
            "unevaluated_checks": [],
        }
        if stats is None and not inputs["stats_complete"]:
            missing.append(
                f"No event_stats rows for source {source_id} were among the pages read; event_stats "
                "has more pages, so whether it sent catalog events in the 28-day window is unknown."
            )
        elif stats is None:
            missing.append(
                f"No event_stats rows for source {source_id} in Meta's 28-day window, so no "
                "match-rate history is available for it."
            )
            findings.append(build_finding(
                "no_event_stats",
                f"Source {source_id} is connected but sent no catalog events Meta counted in the last 28 days.",
                severity="medium",
                confidence=0.7,
                affected_entities=[{"event_source_id": source_id}],
                next_actions=["Confirm the pixel or app fires ViewContent, AddToCart and Purchase with content_ids."],
            ))
        elif (
            inputs["stats_complete"]
            and stats["window_match_rate"] is not None
            and stats["window_match_rate"] < match_rate_threshold
        ):
            findings.append(build_finding(
                "low_match_rate",
                f"Only {stats['window_match_rate']:.0%} of content ids from source {source_id} matched "
                f"catalog items (threshold {match_rate_threshold:.0%}).",
                severity="medium",
                confidence=0.6,
                evidence=[{
                    "event_source_id": source_id,
                    "matched_content_ids": stats["matched_content_ids"],
                    "unmatched_content_ids": stats["unmatched_content_ids"],
                    "threshold": match_rate_threshold,
                }],
                affected_entities=[{"event_source_id": source_id}],
                next_actions=["Make the content_ids the source sends equal the catalog's retailer ids."],
            ))
        if stats is not None and stats["rows_missing_counters"]:
            missing.append(
                f"{stats['rows_missing_counters']} event_stats rows for source {source_id} lacked a "
                "matched or unmatched counter; totals and match rates those rows feed are unknown (null)."
            )
        if source_type in DA_CHECK_SOURCE_TYPES:
            checks_payload = await client.list_objects(source_id, "da_checks", fields=DA_CHECK_FIELDS)
            checks_page = normalize_collection(checks_payload)
            checks = checks_page["items"]
            failed = [check for check in checks if str(check.get("result") or "").lower() == "failed"]
            unevaluated = [
                check for check in checks
                if str(check.get("result") or "").lower() not in DA_CHECK_EVALUATED_RESULTS
            ]
            report["checks"] = checks
            report["failed_checks"] = failed
            report["unevaluated_checks"] = unevaluated
            for check in unevaluated:
                missing.append(
                    f"Meta's check {check.get('key')} for source {source_id} returned result "
                    f"{check.get('result')!r}, so it was neither passed nor failed and could not be evaluated."
                )
            if not checks:
                missing.append(f"Meta returned no Dynamic Ads checks for source {source_id}.")
            if checks_page["paging"].get("next"):
                missing.append(f"da_checks for source {source_id} had more pages than were read.")
            for check in failed:
                findings.append(build_finding(
                    "failed_dynamic_ads_check",
                    f"Source {source_id} failed Meta's check {check.get('key')}: {check.get('title') or ''}".strip(),
                    severity="high",
                    confidence=0.9,
                    evidence=[check],
                    affected_entities=[{"event_source_id": source_id}],
                    next_actions=[check["action_uri"]] if check.get("action_uri") else [],
                ))
        else:
            missing.append(
                f"Meta da_checks were not run for source {source_id}: source_type "
                f"{source.get('source_type')!r} is not one documented for da_checks (pixel or app)."
            )
        reports.append(report)
    if not inputs["sources"] and inputs["sources_complete"]:
        findings.append(build_finding(
            "no_connected_event_sources",
            "No pixel or app is connected to this catalog, so catalog ads get no event signal from it.",
            severity="high",
            confidence=0.9,
            next_actions=["Use get_catalog_event_source_recommendations, then connect_catalog_event_source."],
        ))
    elif not inputs["sources"]:
        missing.append(
            "No connected sources were among the pages read, but the list is partial; whether any "
            "source is connected could not be determined."
        )
    missing.extend(inputs["scan_gaps"])
    if not inputs["stats_complete"]:
        missing.append("Low-match-rate findings were not made because event_stats was only partly read.")
    if inputs["unattributed_rows"]:
        missing.append(f"{inputs['unattributed_rows']} event_stats rows had no event_source and were not attributed.")
    metrics: dict[str, Any] = {
        "connected_sources": len(reports),
        "sources_complete": inputs["sources_complete"],
        "event_stat_rows": inputs["stat_rows"],
        "stats_complete": inputs["stats_complete"],
        "match_rate_threshold": match_rate_threshold,
    }
    scope: dict[str, Any] = {"catalog_id": catalog_id}
    if scope_id is not None:
        scope["event_source_id"] = scope_id
        across = reports[0]["across_catalogs"]
        metrics["this_catalog_match_rate"] = across["this_catalog_match_rate"]
        metrics["all_catalogs_match_rate_range"] = across["all_catalogs_match_rate_range"]
    return analysis_response(
        scope=scope,
        metrics=metrics,
        findings=_sorted_findings(findings),
        missing_signals=missing,
        extra={
            "sources": reports,
            "method": _DERIVED_METHOD,
            "match_rate_definition": _MATCH_RATE_NOTE,
            "across_catalogs_definition": _ACROSS_CATALOGS_NOTE,
            "signals_used": [
                "GET {catalog_id}/external_event_sources (connected sources)",
                "GET {catalog_id}/event_stats (daily matched and unmatched content ids per event and source, "
                "and ids matched in the source's other catalogs, last 28 days)",
                "GET {event_source_id}/da_checks for pixel and app sources (Meta's Dynamic Ads checks: passed, failed, unavailable)",
            ],
        },
    )


@mcp_server.tool()
@_secrets_masked
async def get_catalog_event_source_recommendations(
    catalog_id: str,
    business_id: str | None = None,
    match_rate_threshold: float = DEFAULT_MATCH_RATE_THRESHOLD,
) -> dict[str, Any]:
    """Recommend pixels worth connecting to a product catalog when its event signal is weak or missing, with evidence. Candidates are the business's pixels not yet connected; Meta's Dynamic Ads check shows whether each already sends catalog events."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    business_id = blank_to_none(business_id)
    _validate_threshold(match_rate_threshold)
    client = get_graph_api_client()
    if business_id is None:
        catalog = await client.get_object(catalog_id, fields=["id", "business"])
        business_id = _object_id(catalog.get("business"))
        if business_id is None:
            raise ValidationError(
                "The catalog has no readable business; pass business_id to choose whose pixels to consider."
            )
    inputs = await _catalog_signal_inputs(client, catalog_id)
    connected_ids = {_object_id(source.get("id")) for source in inputs["sources"]}
    findings: list[dict[str, Any]] = []
    missing: list[str] = []
    matched = _known_sum(stat["matched_content_ids"] for stat in inputs["stats"].values())
    unmatched = _known_sum(stat["unmatched_content_ids"] for stat in inputs["stats"].values())
    overall_rate = _match_rate(matched, unmatched)
    rows_missing_counters = sum(stat["rows_missing_counters"] for stat in inputs["stats"].values())
    if rows_missing_counters:
        missing.append(
            f"{rows_missing_counters} event_stats rows lacked a matched or unmatched counter; "
            "the 28-day totals and match rate they feed are unknown (null)."
        )
    missing.extend(inputs["scan_gaps"])
    sources_complete = inputs["sources_complete"]
    stats_complete = inputs["stats_complete"]
    coverage_evidence: dict[str, Any] = {
        "connected_sources": len(inputs["sources"]),
        "sources_complete": sources_complete,
        "stats_complete": stats_complete,
        "matched_content_ids_28d": matched if stats_complete else None,
        "unmatched_content_ids_28d": unmatched if stats_complete else None,
        "match_rate_28d": overall_rate if stats_complete else None,
        "threshold": match_rate_threshold,
    }
    if not stats_complete:
        coverage_evidence["partial_counts_from_pages_read"] = {
            "event_stat_rows": inputs["stat_rows"],
            "matched_content_ids": matched,
            "unmatched_content_ids": unmatched,
        }
    if not inputs["sources"] and not sources_complete:
        missing.append(
            "No connected sources were among the pages read, but the list is partial; whether any "
            "source is connected could not be determined."
        )
    elif not inputs["sources"]:
        findings.append(build_finding(
            "no_connected_event_sources",
            "No pixel or app is connected to this catalog.",
            severity="high",
            confidence=0.9,
            evidence=[coverage_evidence],
        ))
    elif not stats_complete:
        missing.append(
            "Whether the connected sources' catalog signal coverage is weak could not be determined "
            "because event_stats was only partly read."
        )
    elif matched != 0 and overall_rate is None:
        missing.append(
            "Whether the connected sources' catalog signal coverage is weak could not be determined "
            "because Meta omitted matched or unmatched counters."
        )
    elif matched == 0 or (overall_rate is not None and overall_rate < match_rate_threshold):
        findings.append(build_finding(
            "weak_catalog_signal_coverage",
            "Connected sources produced few matched catalog events in Meta's 28-day window.",
            severity="medium",
            confidence=0.6,
            evidence=[coverage_evidence],
            next_actions=[
                "Connect a pixel that already sends catalog events, or fix content_ids on the connected sources.",
            ],
        ))

    pixels, pixels_complete = await _collect_edge(
        client, business_id, "adspixels", fields=PIXEL_CANDIDATE_FIELDS, page_size=100, max_pages=5,
    )
    if not pixels_complete:
        missing.append("The business has more pixels than were read; some candidates were not considered.")
    candidates = [
        pixel for pixel in pixels
        if _object_id(pixel.get("id")) not in connected_ids and not pixel.get("is_unavailable")
    ]
    excluded = [
        {"event_source_id": _object_id(pixel.get("id")), "reason": "Meta marks this pixel unavailable."}
        for pixel in pixels
        if _object_id(pixel.get("id")) not in connected_ids and pixel.get("is_unavailable")
    ]
    if not pixels:
        missing.append(f"No pixels were returned for business {business_id}, so there are no candidates to recommend.")
    if candidates and not sources_complete:
        missing.append(
            "Candidate pixels were not among the connected sources read, but that list is partial, "
            "so some candidates may already be connected (connection_status unknown)."
        )
    connection_status = "not_connected" if sources_complete else "unknown"
    recommendations: list[dict[str, Any]] = []
    for index, pixel in enumerate(candidates):
        pixel_id = _object_id(pixel.get("id"))
        evidence: list[dict[str, Any]] = [{"last_fired_time": pixel.get("last_fired_time")}]
        strength = "unchecked"
        reason = (
            "Pixel belongs to the business and is not connected to this catalog."
            if sources_complete
            else "Pixel belongs to the business and was not among the connected sources read."
        )
        if index < MAX_PIXEL_CANDIDATES_CHECKED:
            checks_payload = await client.list_objects(pixel_id, "da_checks", fields=DA_CHECK_FIELDS)
            checks_page = normalize_collection(checks_payload)
            checks = checks_page["items"]
            checks_paged = bool(checks_page["paging"].get("next"))
            if checks_paged:
                missing.append(f"da_checks for pixel {pixel_id} had more pages than were read.")
            dpa_check = next((c for c in checks if c.get("key") == "pixel_missing_dpa_event"), None)
            result = str((dpa_check or {}).get("result") or "").lower()
            if dpa_check is not None:
                evidence.append(dpa_check)
            if result == "passed":
                strength = "strong"
                reason = "Meta's check pixel_missing_dpa_event passed: the pixel already sends catalog events."
            elif result == "failed":
                strength = "weak"
                reason = "Meta's check pixel_missing_dpa_event failed: the pixel does not send all catalog events yet."
            elif dpa_check is None and checks_paged:
                strength = "unknown"
                reason = (
                    "Meta's check pixel_missing_dpa_event was not on the first page of checks read, "
                    "and more pages exist, so the result is unknown."
                )
            else:
                strength = "unknown"
                reason = "Meta returned no usable pixel_missing_dpa_event result for this pixel."
        recommendations.append({
            "event_source_id": pixel_id,
            "name": pixel.get("name"),
            "source_type": "PIXEL",
            "connection_status": connection_status,
            "strength": strength,
            "reason": reason,
            "evidence": evidence,
            "next_step": {
                "tool": "connect_catalog_event_source",
                "arguments": {"catalog_id": catalog_id, "event_source_id": pixel_id},
            },
        })
    if len(candidates) > MAX_PIXEL_CANDIDATES_CHECKED:
        missing.append(
            f"Only the first {MAX_PIXEL_CANDIDATES_CHECKED} unconnected pixels were checked with da_checks; "
            "the rest are listed as unchecked."
        )
    missing.append("Apps are not considered as candidates: Meta documents no business edge listing apps for this purpose.")
    strength_order = {"strong": 0, "unknown": 1, "unchecked": 2, "weak": 3}

    def recommendation_key(item: dict[str, Any]) -> tuple[int, float]:
        fired = _parse_time(item["evidence"][0].get("last_fired_time"))
        return (strength_order[item["strength"]], -(fired.timestamp() if fired else 0.0))

    recommendations.sort(key=recommendation_key)
    return analysis_response(
        scope={"catalog_id": catalog_id, "business_id": business_id},
        metrics=coverage_evidence,
        findings=_sorted_findings(findings),
        missing_signals=missing,
        extra={
            "recommendations": recommendations,
            "excluded": excluded,
            "method": _DERIVED_METHOD,
            "match_rate_definition": _MATCH_RATE_NOTE,
            "signals_used": [
                "GET {catalog_id}/external_event_sources (already connected sources)",
                "GET {catalog_id}/event_stats (matched and unmatched content ids, last 28 days)",
                "GET {business_id}/adspixels (candidate pixels with last_fired_time, is_unavailable)",
                "GET {pixel_id}/da_checks check pixel_missing_dpa_event (whether the pixel sends catalog events)",
            ],
        },
    )


async def _connection_state(
    client: Any, catalog_id: str,
) -> tuple[list[dict[str, Any]], list[str], bool, list[str]]:
    """Read the connected sources; when the read stops early, say so in plain words."""
    reasons: list[str] = []
    sources, complete = await _connected_sources(client, catalog_id, reasons)
    ids = [source_id for source in sources if (source_id := _object_id(source.get("id")))]
    gaps: list[str] = []
    if not complete:
        gaps.append(
            f"The connected-source list is partial ({len(ids)} read): "
            f"{' '.join(reasons) or 'external_event_sources had more pages than were read.'} "
            "Sources not read are missing from the before and after lists."
        )
    return sources, ids, complete, gaps


def _with_completeness(response: dict[str, Any], complete: bool, gaps: list[str]) -> dict[str, Any]:
    response.update({"previous_complete": complete, "missing_signals": gaps})
    return response


@mcp_server.tool()
@_secrets_masked
async def connect_catalog_event_source(
    catalog_id: str,
    event_source_id: str,
) -> dict[str, Any]:
    """Connect a pixel or app to a Meta product catalog so its catalog events can match items. Returns the connected sources before and after; an already connected source is left as is."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    event_source_id = _require_id(event_source_id, "event_source_id")
    client = get_graph_api_client()
    _, before, complete, gaps = await _connection_state(client, catalog_id)
    target = {"catalog_id": catalog_id, "event_source_id": event_source_id}
    if event_source_id in before:
        return _with_completeness(mutation_response(
            action="connect_catalog_event_source",
            target=target,
            previous={"connected_event_source_ids": before},
            current={"connected_event_source_ids": before, "already_connected": True},
        ), complete, gaps)
    if not complete:
        gaps.append(
            "The source given as event_source_id was not among the sources read, so it may already have "
            "been connected; the connect request was sent either way."
        )
    await client.create_edge_object(
        catalog_id, "external_event_sources", data={"external_event_sources": [event_source_id]},
    )
    return _with_completeness(mutation_response(
        action="connect_catalog_event_source",
        target=target,
        previous={"connected_event_source_ids": before},
        current={"connected_event_source_ids": [*before, event_source_id]},
    ), complete, gaps)


@mcp_server.tool()
@_secrets_masked
async def disconnect_catalog_event_source(
    catalog_id: str,
    event_source_id: str,
) -> dict[str, Any]:
    """Use only on an explicit request to disconnect a pixel or app from a Meta product catalog; catalog ads stop using its events. Returns the source and the connected list before and after."""
    catalog_id = _require_id(catalog_id, "catalog_id")
    event_source_id = _require_id(event_source_id, "event_source_id")
    client = get_graph_api_client()
    sources, before, complete, gaps = await _connection_state(client, catalog_id)
    source = next((s for s in sources if _object_id(s.get("id")) == event_source_id), None)
    if source is None and complete:
        raise ValidationError(
            # The ids are not repeated: event_source_id is not checked as an id before this point.
            "The source given as event_source_id is not connected to this catalog; nothing was changed."
        )
    if source is None:
        # The list is capped, so absence proves nothing. Disconnecting a source that is not
        # connected removes nothing else, while refusing would block a request on large catalogs.
        gaps.append(
            "The source given as event_source_id was not among the sources read, so whether it was "
            "connected before this request is not confirmed; the disconnect request was sent."
        )
    await client.delete_edge_object(
        catalog_id, "external_event_sources", data={"external_event_sources": [event_source_id]},
    )
    return _with_completeness(mutation_response(
        action="disconnect_catalog_event_source",
        target={"catalog_id": catalog_id, "event_source_id": event_source_id},
        previous={"connected_event_source_ids": before, "event_source": source},
        current={"connected_event_source_ids": [s for s in before if s != event_source_id]},
    ), complete, gaps)
