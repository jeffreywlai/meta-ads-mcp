"""Audience management tools."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import secrets
import unicodedata
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastmcp.tools import Tool
from pydantic import Field, WithJsonSchema

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, RateLimitError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.schemas import mutation_response
from meta_ads_mcp.tool_types import FieldList, StringList, coerce_csv_string_list

AUDIENCE_SUMMARY_FIELDS = [
    "id",
    "name",
    "subtype",
    "approximate_count_lower_bound",
    "approximate_count_upper_bound",
    "time_updated",
    "operation_status",
]

AUDIENCE_FIELDS = [
    *AUDIENCE_SUMMARY_FIELDS,
    "description",
    "customer_file_source",
    "retention_days",
    "lookalike_spec",
]


def _resolve_lookalike_spec(
    *,
    country: str | None,
    countries: list[str] | None,
    ratio: float | None,
    starting_ratio: float | None,
    lookalike_type: str | None,
) -> dict[str, Any]:
    """Build a lookalike_spec payload."""
    country = blank_to_none(country)
    if country and countries:
        raise ValidationError("Provide country or countries, not both.")
    location_values = coerce_csv_string_list(countries) or ([country] if country else None)
    if not location_values:
        raise ValidationError("country or countries is required for create_lookalike_audience.")
    if any(not isinstance(value, str) or not value.strip() for value in location_values):
        raise ValidationError("countries must contain nonblank country codes.")
    location_values = list(dict.fromkeys(value.strip().upper() for value in location_values))
    resolved_type = blank_to_none(lookalike_type) or (
        "custom_ratio" if ratio is not None else "similarity"
    )
    if ratio is not None:
        if not math.isfinite(ratio) or not 0 < ratio <= 1:
            raise ValidationError("ratio must be a finite fraction greater than 0 and at most 1.")
    elif starting_ratio is not None:
        raise ValidationError("ratio is required when starting_ratio is supplied.")
    if starting_ratio is not None and (
        not math.isfinite(starting_ratio) or not 0 <= starting_ratio < ratio
    ):
        raise ValidationError("starting_ratio must be finite, nonnegative, and less than ratio.")
    spec: dict[str, Any] = {"type": resolved_type}
    if len(location_values) == 1:
        spec["country"] = location_values[0]
    else:
        spec["location_spec"] = {"geo_locations": {"countries": location_values}}
    if ratio is not None:
        spec["ratio"] = ratio
    if starting_ratio is not None:
        spec["starting_ratio"] = starting_ratio
    return spec


@mcp_server.tool()
async def list_audiences(
    account_id: str,
    subtype: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List one page of audience ids, sizes, or subtype metadata. Follow summary.next_after with after until summary.complete is true."""
    if limit < 1:
        raise ValidationError("limit must be positive.")
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit}
    if subtype:
        params["subtype"] = subtype
    if after:
        params["after"] = after
    payload = await client.list_objects(
        normalize_account_id(account_id),
        "customaudiences",
        fields=fields or AUDIENCE_SUMMARY_FIELDS,
        params=params,
    )
    normalized = normalize_collection(payload)
    has_more = bool(normalized["paging"].get("next"))
    normalized["summary"].update({
        "complete": not has_more,
        "truncated": has_more,
        "next_after": normalized["paging"].get("after") if has_more else None,
    })
    return normalized


@mcp_server.tool()
async def get_audience(
    audience_id: str,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """Get, fetch, show, or inspect one custom or lookalike audience by id with selectable detail fields."""
    audience_id = blank_to_none(audience_id)
    if audience_id is None:
        raise ValidationError("audience_id is required.")
    audience = await get_graph_api_client().get_object(
        audience_id,
        fields=fields or AUDIENCE_FIELDS,
    )
    return {"item": audience, "summary": {"count": 1}}


@mcp_server.tool()
async def create_custom_audience(
    account_id: str,
    name: str,
    subtype: str = "CUSTOM",
    description: str | None = None,
    customer_file_source: str | None = None,
    retention_days: int | None = None,
    rule: dict[str, Any] | None = None,
    prefill: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use this when the user wants to create a first-party or rule-based custom audience."""
    payload: dict[str, Any] = {
        "name": name,
        "subtype": subtype,
        "prefill": prefill,
        "description": description or OMIT,
        "customer_file_source": customer_file_source or OMIT,
        "retention_days": OMIT if retention_days is None else retention_days,
        "rule": rule or OMIT,
    }
    payload = merge_graph_payload(payload, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(
        normalize_account_id(account_id),
        "customaudiences",
        data=payload,
    )
    return {
        "ok": True,
        "action": "create_custom_audience",
        "target": {"account_id": normalize_account_id(account_id)},
        "created": created,
    }


@mcp_server.tool()
async def create_lookalike_audience(
    account_id: str,
    name: str,
    origin_audience_id: str,
    country: str | None = None,
    countries: StringList | None = None,
    ratio: float | None = None,
    starting_ratio: float | None = None,
    lookalike_type: Annotated[
        str | None,
        Field(description="Defaults to custom_ratio when ratio is supplied, otherwise similarity. Explicit types are preserved and Meta validates their compatibility."),
    ] = None,
    description: str | None = None,
    params: Annotated[
        dict[str, Any] | None,
        Field(description="Additional Graph parameters. Cannot supply lookalike_spec or override tool-managed named fields; use country/countries, ratio, starting_ratio, and lookalike_type."),
    ] = None,
) -> dict[str, Any]:
    """Create a lookalike from a seed with tool-managed lookalike_spec. Supply country or countries; ratio is a fraction (0.01 means 1%). params cannot carry lookalike_spec."""
    payload: dict[str, Any] = {
        "name": name,
        "subtype": "LOOKALIKE",
        "origin_audience_id": origin_audience_id,
        "lookalike_spec": _resolve_lookalike_spec(
            country=country,
            countries=countries,
            ratio=ratio,
            starting_ratio=starting_ratio,
            lookalike_type=lookalike_type,
        ),
        "description": description or OMIT,
    }
    payload = merge_graph_payload(payload, params)
    client = get_graph_api_client()
    created = await client.create_edge_object(
        normalize_account_id(account_id),
        "customaudiences",
        data=payload,
    )
    return {
        "ok": True,
        "action": "create_lookalike_audience",
        "target": {"account_id": normalize_account_id(account_id), "origin_audience_id": origin_audience_id},
        "created": created,
    }


def _audience_rule(rule: Any) -> dict[str, Any]:
    """Accept a website audience rule as an object or a JSON-encoded object string."""
    value = rule
    if isinstance(rule, str):
        try:
            value = json.loads(rule)
        except ValueError:
            raise ValidationError(
                "rule must be a JSON object, or a string holding one, in Meta's website audience rule "
                "format (the same format create_custom_audience takes)."
            ) from None
    if not isinstance(value, dict) or not value:
        raise ValidationError(
            "rule must be a nonempty JSON object in Meta's website audience rule format "
            "(for example {\"inclusions\": {...}})."
        )
    return value


@mcp_server.tool()
async def update_custom_audience(
    audience_id: str,
    name: str | None = None,
    description: str | None = None,
    retention_days: int | None = None,
    customer_file_source: str | None = None,
    params: dict[str, Any] | None = None,
    rule: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Use this when the user already has an audience id and needs to change audience metadata.

    rule replaces a website (or other rule-based) audience's rule: a JSON object or JSON string in the
    format create_custom_audience takes; for website audiences, retention is retention_seconds inside it.
    The prior rule is read first and returned in previous.
    """
    payload: dict[str, Any] = {
        "name": OMIT,
        "description": OMIT,
        "retention_days": OMIT,
        "customer_file_source": OMIT,
    }
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if retention_days is not None:
        payload["retention_days"] = retention_days
    if customer_file_source is not None:
        payload["customer_file_source"] = customer_file_source
    if rule is not None:
        # Added only when named, so a call that passes rule through params (as callers did
        # before rule was named) is unchanged, while giving it both ways is rejected.
        payload["rule"] = _audience_rule(rule)
    payload = merge_graph_payload(payload, params)
    if not payload:
        raise ValidationError("At least one field must be provided for update_custom_audience.")
    client = get_graph_api_client()
    read_fields = AUDIENCE_FIELDS if rule is None else [*AUDIENCE_FIELDS, "rule"]
    previous = await client.get_object(audience_id, fields=read_fields)
    await client.update_object(audience_id, data=payload)
    prior = {
        "name": previous.get("name"),
        "description": previous.get("description"),
        "retention_days": previous.get("retention_days"),
        "customer_file_source": previous.get("customer_file_source"),
    }
    if rule is not None:
        prior["rule"] = previous.get("rule")
    return mutation_response(
        action="update_custom_audience",
        target={"audience_id": audience_id},
        previous=prior,
        current=payload,
    )


@mcp_server.tool()
async def delete_audience(audience_id: str) -> dict[str, Any]:
    """Use this only when the user explicitly wants to remove a custom audience."""
    client = get_graph_api_client()
    result = await client.delete_object(audience_id)
    return {
        "ok": True,
        "action": "delete_audience",
        "target": {"audience_id": audience_id},
        "result": result,
    }


# Customer-list uploads. Values are normalized and SHA-256 hashed here, following
# Meta's customer list formatting rules, so no raw value is sent to Meta. No value
# or hash is ever placed in a response, an error message, or a log line.

AUDIENCE_USERS_BATCH_SIZE = 10_000
AUDIENCE_USERS_INLINE_ROW_LIMIT = 1_000
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# Each normalizer below follows Meta's customer-list normalization table
# ("Hashing and normalization for multi-key", Marketing API custom audiences guide,
# https://developers.facebook.com/documentation/ads-commerce/marketing-api/audiences/guides/custom-audiences#hash)
# and returns "" when the normalized value does not have the published format.
# An empty result is counted as invalid and sent blank; it is never hashed.
# ST and ZIP also take the row's normalized COUNTRY, when the schema has one,
# because their published format differs for the US and the UK.

# ISO 3166-1 alpha-2 officially assigned codes (COUNTRY must be one of these).
_ISO_COUNTRY_CODES = frozenset(
    """
    ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm bn bo bq br bs bt bv bw by bz
    ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr
    ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp
    ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq mr ms mt
    mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw
    sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug
    um us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw
    """.split()
)

# US state and territory names mapped to their 2-letter ANSI codes, keyed by the
# lowercase a-z form produced by _ascii_letters.
_US_STATE_CODES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga",
    "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
    "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt",
    "nebraska": "ne", "nevada": "nv", "newhampshire": "nh", "newjersey": "nj", "newmexico": "nm",
    "newyork": "ny", "northcarolina": "nc", "northdakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhodeisland": "ri", "southcarolina": "sc",
    "southdakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "westvirginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "districtofcolumbia": "dc", "americansamoa": "as", "guam": "gu", "northernmarianaislands": "mp",
    "puertorico": "pr", "usvirginislands": "vi", "virginislands": "vi",
    "unitedstatesminoroutlyingislands": "um",
}
_US_STATE_ABBREVIATIONS = frozenset(_US_STATE_CODES.values())
_UK_POSTCODE = re.compile(r"([a-z]{1,2}[0-9][a-z0-9]?)([0-9])([a-z]{2})?")


def _has_non_ascii_digit(value: str) -> bool:
    return any(character.isdigit() and not "0" <= character <= "9" for character in value)


def _letters(value: str) -> str:
    """FN and LN: lowercase letters only, no punctuation; non-Roman letters stay in UTF-8."""
    return "".join(character for character in unicodedata.normalize("NFC", value).lower() if character.isalpha())


def _ascii_letters(value: str) -> str:
    """CT and non-US ST: a-z only. Accents are removed; any other non-Roman letter makes the value invalid."""
    decomposed = unicodedata.normalize("NFKD", value.lower())
    letters = [character for character in decomposed if character.isalpha()]
    if any(not "a" <= character <= "z" for character in letters):
        return ""
    return "".join(letters)


def _normalize_email(value: str) -> str:
    candidate = value.strip().lower()
    return candidate if _EMAIL_SHAPE.fullmatch(candidate) else ""


def _normalize_phone(value: str) -> str:
    """Remove symbols, letters and leading zeros; a phone number has 7 to 15 digits (E.164 allows 15)."""
    if _has_non_ascii_digit(value):
        return ""
    digits = re.sub(r"[^0-9]", "", value).lstrip("0")
    return digits if 7 <= len(digits) <= 15 else ""


def _normalize_gender(value: str) -> str:
    return {"m": "m", "male": "m", "f": "f", "female": "f"}.get(value.strip().lower(), "")


def _normalize_number(value: str, *, low: int, high: int, width: int) -> str:
    digits = value.strip()
    # ASCII digits only: str.isdigit() also accepts characters such as "²" that int() rejects.
    if not re.fullmatch(r"[0-9]{1,4}", digits) or not low <= int(digits) <= high:
        return ""
    return f"{int(digits):0{width}d}"


def _normalize_birth_year(value: str) -> str:
    digits = value.strip()
    if len(digits) != 4:
        return ""
    return _normalize_number(digits, low=1900, high=date.today().year, width=4)


def _normalize_first_initial(value: str) -> str:
    return _letters(value)[:1]


def _normalize_state(value: str, country: str | None = None) -> str:
    """US: the 2-letter ANSI code (full names are converted). Elsewhere: lowercase a-z, no punctuation or spaces."""
    letters = _ascii_letters(value)
    code = _US_STATE_CODES.get(letters, letters if letters in _US_STATE_ABBREVIATIONS else "")
    if country == "us" or (country is None and code):
        return code
    return letters


def _normalize_zip(value: str, country: str | None = None) -> str:
    """Lowercase without spaces; US: first 5 digits; UK: Area/District/Sector."""
    if _has_non_ascii_digit(value):
        return ""
    compact = "".join(value.lower().split())
    us_zip = re.fullmatch(r"([0-9]{5})(-?[0-9]{4})?", compact)
    if country == "us" or (country is None and us_zip):
        return us_zip.group(1) if us_zip else ""
    if country == "gb":
        uk = _UK_POSTCODE.fullmatch(compact)
        return f"{uk.group(1)}{uk.group(2)}" if uk else ""
    return compact if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", compact) and 2 <= len(compact) <= 10 else ""


def _normalize_country(value: str) -> str:
    candidate = value.strip().lower()
    return candidate if candidate in _ISO_COUNTRY_CODES else ""


# Meta customer-list keys and their published normalization rules. Only keys
# Meta requires to be hashed are supported, so every sent value is a SHA-256 digest.
_AUDIENCE_KEY_NORMALIZERS = {
    "EMAIL": _normalize_email,
    "PHONE": _normalize_phone,
    "GEN": _normalize_gender,
    "DOBY": _normalize_birth_year,
    "DOBM": lambda value: _normalize_number(value, low=1, high=12, width=2),
    "DOBD": lambda value: _normalize_number(value, low=1, high=31, width=2),
    "FN": _letters,
    "LN": _letters,
    "FI": _normalize_first_initial,
    "CT": _ascii_letters,
    "ST": _normalize_state,
    "ZIP": _normalize_zip,
    "COUNTRY": _normalize_country,
}
# Keys whose published format depends on the row's COUNTRY.
_COUNTRY_AWARE_KEYS = frozenset({"ST", "ZIP"})


def _audience_schema(schema: Any) -> list[str]:
    keys = coerce_csv_string_list(schema)
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key.strip() for key in keys):
        raise ValidationError("schema must list one or more Meta customer-list keys, such as EMAIL or PHONE.")
    normalized = [key.strip().upper() for key in keys]
    # Positions, not the keys themselves: a misplaced customer value can arrive here.
    unsupported = [str(position) for position, key in enumerate(normalized, start=1) if key not in _AUDIENCE_KEY_NORMALIZERS]
    if unsupported:
        raise ValidationError(
            f"schema position {', '.join(unsupported)} is not a supported key (the value is not shown). "
            f"Supported keys: {', '.join(_AUDIENCE_KEY_NORMALIZERS)}."
        )
    if len(set(normalized)) != len(normalized):
        raise ValidationError("schema lists a key more than once.")
    return normalized


def _read_customer_csv(file_path: str, schema: list[str]) -> list[list[Any]]:
    """Read only the schema columns from a local CSV, matched to the header by name."""
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValidationError("file_path must point to a readable file.")
    lines_read = 0
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            # strict rejects malformed quoting (such as an unclosed quote) instead of
            # silently merging the following lines into one different record.
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)
            if not header:
                raise ValidationError("file_path has no header row.")
            columns = [name.strip().upper() for name in header]
            if len(set(columns)) != len(columns):
                raise ValidationError("file_path header repeats a column name.")
            missing = [key for key in schema if key not in columns]
            if missing:
                raise ValidationError(
                    f"file_path header has no column named {', '.join(missing)}; header columns must be named after the schema keys."
                )
            positions = [columns.index(key) for key in schema]
            rows: list[list[Any]] = []
            lines_read = reader.line_num
            for record in reader:
                lines_read = reader.line_num
                if not any(cell.strip() for cell in record):
                    continue
                rows.append([record[index] if index < len(record) else "" for index in positions])
            return rows
    except UnicodeDecodeError:
        raise ValidationError("file_path must be a UTF-8 encoded CSV file.") from None
    except csv.Error:
        # Report only where the bad record starts; the parser's own message and the
        # record's content are withheld because they can contain customer values.
        raise ValidationError(
            f"file_path is not valid CSV: the record starting on line {lines_read + 1} is malformed "
            "(for example an unclosed or misplaced quote). Nothing was uploaded."
        ) from None


def _hash_rows(
    rows: list[Any],
    schema: list[str],
    *,
    pre_hashed: bool,
) -> tuple[list[list[str]], int, int]:
    """Return rows to send, the invalid value count and the number of dropped rows."""
    prepared: list[list[str]] = []
    invalid = 0
    dropped = 0
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, (list, tuple)):
            raise ValidationError(f"Row {row_number} must be a list with one value per schema key.")
        if len(row) != len(schema):
            raise ValidationError(
                f"Row {row_number} must have exactly {len(schema)} values, one per schema key; it has {len(row)}."
            )
        sent: list[str] = []
        country: str | None = None
        if not pre_hashed and "COUNTRY" in schema:
            raw_country = row[schema.index("COUNTRY")]
            country = (_normalize_country(raw_country) or None) if isinstance(raw_country, str) else None
        for key, value in zip(schema, row):
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ValidationError(f"Row {row_number}, {key}: values must be strings.")
            if not value.strip():
                invalid += 1
                sent.append("")
                continue
            if pre_hashed:
                digest = value.strip().lower()
                if not _SHA256_HEX.fullmatch(digest):
                    raise ValidationError(
                        f"Row {row_number}, {key}: with pre_hashed=true every value must be a SHA-256 hex digest. "
                        "The value is not shown."
                    )
                if digest == _EMPTY_SHA256:
                    invalid += 1
                    sent.append("")
                    continue
            else:
                normalizer = _AUDIENCE_KEY_NORMALIZERS[key]
                normalized = normalizer(value, country) if key in _COUNTRY_AWARE_KEYS else normalizer(value)
                if not normalized:
                    invalid += 1
                    sent.append("")
                    continue
                digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            sent.append(digest)
        if any(sent):
            prepared.append(sent)
        else:
            dropped += 1
    return prepared, invalid, dropped


def _safe_int(value: Any) -> int | None:
    return value if _is_count(value) else None


def _sanitized_upload_error(
    exc: Exception,
    *,
    action: str,
    batch_seq: int,
    total_batches: int,
    rows_accepted: int,
    session_id: int,
) -> Exception:
    """Rebuild an upload failure from structured facts only.

    Meta's free-text fields (message, user title and message, error data, trace
    and operation) can quote any uploaded value, raw, normalized or hashed, from
    this batch or an earlier one, and no text scrubbing can be shown to catch
    them all. They are withheld; only Meta's numeric code and subcode, the HTTP
    status, whether a retry is safe, and the upload progress are kept.
    """
    accepted = batch_seq - 1
    progress = (
        f"{action} failed on batch {batch_seq} of {total_batches} in session {session_id}; "
        f"{accepted} earlier batch{'es' if accepted != 1 else ''} with {rows_accepted} "
        f"row{'s' if rows_accepted != 1 else ''} {'were' if accepted != 1 else 'was'} accepted by Meta. "
        "Meta closes an unfinished session automatically after 90 minutes."
    )
    withheld = "Meta's error text is withheld because it can quote uploaded customer values."
    if isinstance(exc, RateLimitError):
        retry_after = exc.retry_after_seconds
        return RateLimitError(
            f"{progress} Meta rate limit. {withheld}",
            retry_after_seconds=float(retry_after)
            if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
            else None,
            code=_safe_int(exc.code),
            subcode=_safe_int(exc.subcode),
        )
    if isinstance(exc, MetaApiError):
        retry_after = exc.retry_after_seconds
        return MetaApiError(
            message=f"{progress} {withheld}",
            status_code=_safe_int(exc.status_code),
            code=_safe_int(exc.code),
            subcode=_safe_int(exc.subcode),
            is_transient=exc.is_transient if isinstance(exc.is_transient, bool) else None,
            retry_after_seconds=float(retry_after)
            if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
            else None,
            mutation_outcome_unknown=exc.mutation_outcome_unknown is True,
        )
    return MetaApiError(
        message=f"{progress} The request failed ({type(exc).__name__}); details are withheld because they can contain uploaded values.",
        mutation_outcome_unknown=True,
    )


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _session_counters(batch_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize Meta's per-response counters without adding them up.

    Meta's add-members guide defines num_received as the total received in the
    session so far, so the latest reported value is the session total. It calls
    num_invalid_entries approximate and does not say whether it covers the request
    or the session, so it is reported as a total only when there was one batch.
    """
    reported = [batch for batch in batch_results if batch["num_received"] is not None]
    counters: dict[str, Any] = {
        "num_received": reported[-1]["num_received"] if reported else None,
        "num_received_as_of_batch": reported[-1]["batch_seq"] if reported else None,
        "num_invalid_entries": batch_results[0]["num_invalid_entries"] if len(batch_results) == 1 else None,
    }
    if len(batch_results) > 1:
        counters["counter_note"] = (
            "num_received is Meta's session-to-date count from the latest batch that reported it. "
            "Meta does not say whether num_invalid_entries covers one request or the session, so no "
            "total is given; see batches for each reported value."
        )
    return counters


def _upload_flag(value: Any) -> bool:
    """Read pre_hashed the way MCP clients send booleans, without quoting a bad value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValidationError("pre_hashed must be true or false (the value is not shown).")


async def _send_audience_users(
    *,
    action: str,
    audience_id: Any,
    schema: Any,
    file_path: Any,
    rows: Any,
    pre_hashed: Any,
) -> dict[str, Any]:
    # Every argument is checked here rather than by the MCP framework (see the
    # note above _AudienceIdArgument). Messages describe position and shape only.
    if audience_id is not None and not isinstance(audience_id, str):
        raise ValidationError("audience_id must be a string (the value is not shown).")
    audience_id = blank_to_none(audience_id)
    if audience_id is None:
        raise ValidationError("audience_id is required.")
    if file_path is not None and not isinstance(file_path, str):
        raise ValidationError("file_path must be a string path to a local CSV file (the value is not shown).")
    file_path = blank_to_none(file_path)
    pre_hashed = _upload_flag(pre_hashed)
    if (file_path is None) == (rows is None):
        raise ValidationError("Provide exactly one of file_path or rows.")
    keys = _audience_schema(schema)
    if rows is not None:
        if not isinstance(rows, list):
            raise ValidationError(
                "rows must be a list of rows, each a list with one value per schema key (the value is not shown)."
            )
        if len(rows) > AUDIENCE_USERS_INLINE_ROW_LIMIT:
            raise ValidationError(
                f"Inline rows are limited to {AUDIENCE_USERS_INLINE_ROW_LIMIT}; pass a local CSV with file_path instead."
            )
        source_rows: list[Any] = rows
    else:
        source_rows = _read_customer_csv(file_path, keys)
    prepared, invalid, dropped = _hash_rows(source_rows, keys, pre_hashed=pre_hashed)
    if not prepared:
        raise ValidationError(
            f"No valid values to send: all {len(source_rows)} rows were blank or failed normalization."
        )
    batches = [
        prepared[start:start + AUDIENCE_USERS_BATCH_SIZE]
        for start in range(0, len(prepared), AUDIENCE_USERS_BATCH_SIZE)
    ]
    session_id = secrets.randbelow(2**62) + 1
    client = get_graph_api_client()
    send = client.create_edge_object if action == "add_audience_users" else client.delete_edge_object
    batch_results: list[dict[str, Any]] = []
    rows_accepted = 0
    for batch_seq, batch in enumerate(batches, start=1):
        data = {
            "payload": {"schema": keys, "data": batch},
            "session": {
                "session_id": session_id,
                "batch_seq": batch_seq,
                "last_batch_flag": batch_seq == len(batches),
                "estimated_num_total": len(prepared),
            },
        }
        failure: Exception | None = None
        try:
            result = await send(audience_id, "users", data=data)
        except Exception as exc:
            failure = _sanitized_upload_error(
                exc,
                action=action,
                batch_seq=batch_seq,
                total_batches=len(batches),
                rows_accepted=rows_accepted,
                session_id=session_id,
            )
        if failure is not None:
            # Raised outside the except block so the original error, which can
            # echo uploaded values, is not chained as __context__.
            raise failure
        counts = {
            key: result.get(key) if _is_count(result.get(key)) else None
            for key in ("num_received", "num_invalid_entries")
        }
        batch_results.append({"batch_seq": batch_seq, "rows_sent": len(batch), **counts})
        rows_accepted += len(batch)
    return {
        "ok": True,
        "action": action,
        "target": {"audience_id": audience_id},
        "source": "file_path" if file_path is not None else "rows",
        "schema": keys,
        "hashing": "pre_hashed_by_caller" if pre_hashed else "sha256_by_server",
        "rows_read": len(source_rows),
        "rows_sent": len(prepared),
        "rows_dropped": dropped,
        "invalid_values": invalid,
        "session": {
            "session_id": str(session_id),
            "batches": len(batches),
            "last_batch_flag_sent": True,
        },
        "meta": _session_counters(batch_results),
        "batches": batch_results,
        "privacy_note": "No row values or hashes are returned; the sample invalid entries Meta sends back are discarded.",
    }


# FastMCP validates declared parameter types before a tool runs, and on failure
# Pydantic quotes the rejected input, or every argument when one is missing, in
# the raised error and in a WARNING log line. These parameters can carry
# customer values, so the framework is given types it always accepts and
# _send_audience_users validates them with structural messages instead.
# WithJsonSchema keeps the advertised input schema. Field(default=None) lets a
# missing argument reach the tool while Python callers must still pass it;
# _register_customer_upload_tool restores those two in the schema's required list.
_AudienceIdArgument = Annotated[Any, Field(default=None), WithJsonSchema({"type": "string"})]
_SchemaArgument = Annotated[
    Any,
    Field(default=None),
    WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}),
]
_FilePathArgument = Annotated[Any, WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]})]
_RowsArgument = Annotated[
    Any,
    WithJsonSchema(
        {
            "anyOf": [
                {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                    "maxItems": AUDIENCE_USERS_INLINE_ROW_LIMIT,
                },
                {"type": "null"},
            ],
            "description": "One list per person with one string value per schema key, in schema order.",
        }
    ),
]
_PreHashedArgument = Annotated[Any, WithJsonSchema({"type": "boolean"})]
_REQUIRED_UPLOAD_ARGUMENTS = ["audience_id", "schema"]


def _register_customer_upload_tool(fn: Any) -> Any:
    """Register an upload tool as @mcp_server.tool() does, keeping audience_id and schema required in its schema."""
    tool = Tool.from_function(fn, task=False, run_in_thread=True)
    tool.parameters["required"] = list(_REQUIRED_UPLOAD_ARGUMENTS)
    for name in _REQUIRED_UPLOAD_ARGUMENTS:
        tool.parameters["properties"][name].pop("default", None)
    mcp_server.add_tool(tool)
    return fn


@_register_customer_upload_tool
async def add_audience_users(
    audience_id: _AudienceIdArgument,
    schema: _SchemaArgument,
    file_path: _FilePathArgument = None,
    rows: _RowsArgument = None,
    pre_hashed: _PreHashedArgument = False,
) -> dict[str, Any]:
    """Add people to a customer list audience. schema lists Meta keys (EMAIL, PHONE, FN, LN, FI, GEN, DOBY, DOBM, DOBD, CT, ST, ZIP, COUNTRY). Prefer file_path, a local CSV whose header names those columns, so values never pass through the model; inline rows (max 1,000) have already passed through the calling model and its provider. Values are normalized and SHA-256 hashed locally unless pre_hashed; blank or malformed values are dropped and counted. Returns counts only."""
    return await _send_audience_users(
        action="add_audience_users",
        audience_id=audience_id,
        schema=schema,
        file_path=file_path,
        rows=rows,
        pre_hashed=pre_hashed,
    )


@_register_customer_upload_tool
async def remove_audience_users(
    audience_id: _AudienceIdArgument,
    schema: _SchemaArgument,
    file_path: _FilePathArgument = None,
    rows: _RowsArgument = None,
    pre_hashed: _PreHashedArgument = False,
) -> dict[str, Any]:
    """Remove people from a customer list audience. Same inputs as add_audience_users: prefer file_path, a local CSV whose header names the schema columns, so values never pass through the model; inline rows (max 1,000) have already passed through the calling model and its provider. Values are normalized and SHA-256 hashed locally unless pre_hashed. Returns counts only."""
    return await _send_audience_users(
        action="remove_audience_users",
        audience_id=audience_id,
        schema=schema,
        file_path=file_path,
        rows=rows,
        pre_hashed=pre_hashed,
    )
