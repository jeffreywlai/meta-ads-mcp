"""Meta product catalog, product and product set tools.

"Catalog" here always means a Meta product catalog (the Commerce Manager object
that feeds Advantage+ catalog ads), never this server's own list of tools.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import (
    MetaApiError,
    NotFoundError,
    UnsupportedFeatureError,
    ValidationError,
)
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.normalize import blank_to_none, normalize_collection, to_int
from meta_ads_mcp.schemas import analysis_response, creation_response, mutation_response
from meta_ads_mcp.tool_types import FieldList, normalize_field_list

# Fields documented on the Product Catalog reference page.
CATALOG_FIELDS = ["id", "name", "vertical", "product_count", "feed_count", "is_catalog_segment"]
CATALOG_UPDATE_READ_FIELDS = [
    "id", "name", "vertical", "da_display_settings", "default_image_url", "fallback_image_url",
]
# Allowed `vertical` values on POST /{business_id}/owned_product_catalogs.
CATALOG_VERTICALS = (
    "adoptable_pets", "apps_and_software", "articles_and_publications", "commerce",
    "destinations", "flights", "generic", "home_listings", "hotels",
    "local_service_businesses", "media_titles", "offer_items", "services",
    "offline_commerce", "transactable_items", "vehicles",
)
CATALOG_OWNERSHIP_EDGES = {"owned": "owned_product_catalogs", "client": "client_product_catalogs"}

PRODUCT_FIELDS = [
    "id", "retailer_id", "name", "availability", "price", "currency", "sale_price",
    "url", "image_url", "brand", "visibility", "review_status",
]
PRODUCT_DETAIL_FIELDS = [
    *PRODUCT_FIELDS, "description", "condition", "product_type", "category",
    "retailer_product_group_id", "additional_image_urls", "custom_label_0",
    "custom_label_1", "custom_label_2", "custom_label_3", "custom_label_4",
]
FILTER_SAMPLE_FIELDS = ["id", "retailer_id", "name", "price", "availability"]
PRODUCT_AVAILABILITY = (
    "in stock", "out of stock", "preorder", "available for order", "discontinued",
    "pending", "mark_as_sold", "mark_as_expired",
)
PRODUCT_CONDITIONS = (
    "new", "refurbished", "used", "used_like_new", "used_good", "used_fair", "cpo", "open_box_new",
)
PRODUCT_VISIBILITY = ("staging", "published")

PRODUCT_SET_FIELDS = ["id", "name", "filter", "product_count", "retailer_id"]
DATA_SOURCE_FIELDS = ["id", "name", "ingestion_source_type", "upload_type", "app_id"]
DATA_SOURCE_TYPES = ("ALL", "PRIMARY", "SUPPLEMENTARY")
DIAGNOSTIC_FIELDS = [
    "type", "severity", "title", "subtitle", "affected_channels", "affected_entity",
    "affected_features", "error_code", "number_of_affected_items",
    "number_of_affected_entities", "diagnostics",
]
DIAGNOSTIC_SEVERITIES = ("MUST_FIX", "OPPORTUNITY")
# Group keys kept in get_catalog_diagnostics' default compact view.
DIAGNOSTIC_SUMMARY_KEYS = (
    "type", "severity", "title", "subtitle", "affected_entity", "error_code",
    "number_of_affected_items", "number_of_affected_entities",
)
# Nested entries can each run to several KB (a live group of 5 entries was 29 KB),
# so the default view keeps one entry per group for the first few groups, with
# only its scalar values and long text cut short.
DIAGNOSTIC_DETAIL_SAMPLE_SIZE = 1
DIAGNOSTIC_DETAIL_SAMPLE_GROUPS = 5
DIAGNOSTIC_DETAIL_TEXT_LIMIT = 160
EVENT_SOURCE_FIELDS = ["id", "name", "source_type"]

# Operators named in Meta's product set filter documentation. starts_with is
# documented as available only for the category field.
FILTER_OPERATORS = (
    "eq", "neq", "contains", "not_contains", "i_contains", "is_any", "is_not_any",
    "lt", "lte", "gt", "gte", "starts_with",
)
_LIST_OPERATORS = {"is_any", "is_not_any"}
_NUMERIC_OPERATORS = {"lt", "lte", "gt", "gte"}
_STRING_OPERATORS = {"contains", "not_contains", "i_contains", "starts_with"}

_DPA_MAX_CATALOGS = 100
_DPA_PAGE_SIZE = 50
# Request cap for the catalog listing, independent of how many rows each page holds.
_DPA_MAX_LIST_PAGES = 4
_SIGNAL_ERRORS = (MetaApiError, UnsupportedFeatureError, NotFoundError)
_EMPTY_SET_MESSAGE = (
    "Meta documents that ads tied to an empty product set will not deliver."
)


def _require(value: str | None, name: str) -> str:
    """Return a stripped required id or raise before any network call."""
    normalized = blank_to_none(value) if isinstance(value, str) else None
    if normalized is None:
        raise ValidationError(f"{name} is required.")
    return normalized


def _one_scope(**scopes: str | None) -> tuple[str, str]:
    """Return the single supplied scope, rejecting none or several."""
    supplied = {
        name: blank_to_none(value) for name, value in scopes.items()
        if isinstance(value, str) and blank_to_none(value)
    }
    if len(supplied) != 1:
        names = list(scopes)
        listed = ", ".join(names[:-1]) + (", or " if len(names) > 2 else " or ") + names[-1]
        raise ValidationError(f"Provide exactly one of {listed}.")
    return next(iter(supplied.items()))


def _check_limit(limit: int) -> None:
    if limit < 1:
        raise ValidationError("limit must be positive.")


def _page_params(limit: int, after: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if cursor := blank_to_none(after):
        params["after"] = cursor
    return params


def _paged(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape one Graph page with the completeness fields list_audiences uses."""
    normalized = normalize_collection(payload)
    has_more = bool(normalized["paging"].get("next"))
    normalized["summary"].update({
        "complete": not has_more,
        "truncated": has_more,
        "next_after": normalized["paging"].get("after") if has_more else None,
    })
    total = (payload.get("summary") or {}).get("total_count")
    if total is not None:
        normalized["summary"]["total_count"] = to_int(total)
    return normalized


def _single(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": [item],
        "paging": {"before": None, "after": None, "next": None},
        "summary": {"count": 1, "complete": True, "truncated": False, "next_after": None},
    }


def _parse_filter(value: dict[str, Any] | str | None, *, name: str = "filter") -> dict[str, Any] | None:
    """Accept a filter object or its JSON text; Meta expects a JSON object."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{name} must be a JSON object, for example {{\"brand\": {{\"eq\": \"Acme\"}}}}.") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be a JSON object, for example {{\"brand\": {{\"eq\": \"Acme\"}}}}.")
    return value


def _catalog_price(value: Any, field_name: str) -> int:
    """Encode a major-unit price the way Meta's product API documents it.

    Meta: "Price of the item with 2 digits added for cents (ex: use "100" for 1
    or "599" for 5.99)". The same x100 rule is documented for all currencies.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be a finite number.")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a finite number.") from exc
    if not numeric.is_finite():
        raise ValidationError(f"{field_name} must be a finite number.")
    if numeric < 0:
        raise ValidationError(f"{field_name} must not be negative.")
    scaled = numeric * 100
    if scaled != scaled.to_integral_value():
        raise ValidationError(f"{field_name} accepts at most 2 decimal places (Meta sends prices as amount x 100).")
    return int(scaled)


def _currency(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if not re.fullmatch(r"[A-Z]{3}", normalized):
        raise ValidationError("currency must be a 3-letter ISO 4217 code such as USD.")
    return normalized


def _choice(value: str | None, allowed: tuple[str, ...], name: str) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise ValidationError(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _product_fields(
    *,
    name: str | None,
    price: float | None,
    currency: str | None,
    sale_price: float | None,
    availability: str | None,
    condition: str | None,
    visibility: str | None,
    url: str | None,
    image_url: str | None,
    description: str | None,
    brand: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (payload sent to Meta, caller-facing values) for product edits."""
    payload: dict[str, Any] = {}
    shown: dict[str, Any] = {}
    for key, value in (("name", name), ("url", url), ("image_url", image_url),
                       ("description", description), ("brand", brand)):
        if value is not None:
            payload[key] = shown[key] = value
    if price is not None:
        payload["price"] = _catalog_price(price, "price")
        shown["price"] = price
    if currency is not None:
        payload["currency"] = shown["currency"] = _currency(currency)
    if sale_price is not None:
        payload["sale_price"] = _catalog_price(sale_price, "sale_price")
        shown["sale_price"] = sale_price
    for key, value, allowed in (
        ("availability", availability, PRODUCT_AVAILABILITY),
        ("condition", condition, PRODUCT_CONDITIONS),
        ("visibility", visibility, PRODUCT_VISIBILITY),
    ):
        if _choice(value, allowed, key) is not None:
            payload[key] = shown[key] = value
    return payload, shown


def _error_entry(signal: str, error: Exception) -> dict[str, Any]:
    entry: dict[str, Any] = {"signal": signal, "error": str(error)}
    if isinstance(error, MetaApiError):
        entry["code"] = error.code
    return entry


async def _read_edge(client, parent_id: str, edge: str, *, fields, params) -> dict[str, Any]:
    return await client.list_objects(parent_id, edge, fields=fields, params=params)


async def _collect_edge(
    client,
    parent_id: str,
    edge: str,
    *,
    fields: list[str],
    params: dict[str, Any],
    max_pages: int,
    max_items: int | None = None,
    stop_at_total: bool = False,
) -> dict[str, Any]:
    """Read an edge page by page until Meta reports no next page, or a cap or paging fault stops it.

    complete is true only when Meta's paging has no next link. A next link with
    no cursor, a cursor already requested, or a page identical to one already
    read stops the scan with a stated reason, and that page's rows are not added
    twice. max_pages caps requests regardless of how many rows came back, so a
    run of empty pages cannot loop. With stop_at_total, the first summary
    total_count Meta returns ends the read.
    """
    rows: list[Any] = []
    seen_cursors: set[str] = set()
    seen_pages: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        page_params = dict(params)
        if cursor is not None:
            page_params["after"] = cursor
            seen_cursors.add(cursor)
        payload = await client.list_objects(parent_id, edge, fields=fields, params=page_params)
        pages += 1
        data = payload.get("data") or []
        if data:
            fingerprint = json.dumps(data, sort_keys=True, default=str)
            if fingerprint in seen_pages:
                return {"rows": rows, "complete": False, "total_count": None, "pages": pages,
                        "stop_reason": "Meta returned a page that had already been read."}
            seen_pages.add(fingerprint)
            rows.extend(data)
        total = (payload.get("summary") or {}).get("total_count")
        if stop_at_total and to_int(total) is not None:
            return {"rows": rows, "complete": True, "total_count": to_int(total), "pages": pages,
                    "stop_reason": None}
        paging = payload.get("paging") or {}
        if not paging.get("next"):
            return {"rows": rows, "complete": True, "total_count": None, "pages": pages, "stop_reason": None}
        cursor = blank_to_none((paging.get("cursors") or {}).get("after"))
        if cursor is None:
            reason = "Meta reported another page but returned no cursor to read it."
        elif cursor in seen_cursors:
            reason = "Meta returned a page cursor that had already been read."
        elif max_items is not None and len(rows) >= max_items:
            reason = f"Stopped at the {max_items}-row cap."
        elif pages >= max_pages:
            reason = f"Stopped after {pages} pages."
        else:
            continue
        return {"rows": rows, "complete": False, "total_count": None, "pages": pages, "stop_reason": reason}


async def _business_scope(client, business_id: str | None, account_id: str | None) -> dict[str, Any]:
    """Resolve a business scope directly or through an ad account's owning business."""
    if business_id:
        return {"business_id": business_id}
    normalized_account = normalize_account_id(account_id or "")
    account = await client.get_object(normalized_account, fields=["business"])
    business = account.get("business") or {}
    owner_id = str(business.get("id") or "").strip() if isinstance(business, dict) else ""
    if not owner_id:
        raise ValidationError(
            f"Ad account {normalized_account} is not owned by a business, so it has no business product "
            "catalogs to list. Pass business_id for the business that owns the catalog."
        )
    return {
        "account_id": normalized_account,
        "business_id": owner_id,
    }


_ACCOUNT_SCOPE_NOTE = (
    "Ad accounts have no documented catalog edge; these are catalogs of the business that owns the ad account."
)


@mcp_server.tool()
async def list_catalogs(
    business_id: str | None = None,
    account_id: str | None = None,
    catalog_id: str | None = None,
    ownership: str = "owned",
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List product catalogs owned by a business (ownership=client for client catalogs it can use), or by an ad account's owning business, or read one catalog with product and feed counts. Needs catalog_management."""
    scope_name, scope_value = _one_scope(
        business_id=business_id, account_id=account_id, catalog_id=catalog_id,
    )
    _check_limit(limit)
    edge = CATALOG_OWNERSHIP_EDGES.get(ownership)
    if edge is None:
        raise ValidationError("ownership must be owned or client.")
    requested = normalize_field_list(fields) or CATALOG_FIELDS
    client = get_graph_api_client()
    if scope_name == "catalog_id":
        catalog = await client.get_object(scope_value, fields=requested)
        result = _single(catalog)
        result["summary"]["scope"] = {"catalog_id": scope_value}
        return result
    scope = await _business_scope(
        client,
        scope_value if scope_name == "business_id" else None,
        scope_value if scope_name == "account_id" else None,
    )
    payload = await client.list_objects(
        scope["business_id"], edge, fields=requested, params=_page_params(limit, after),
    )
    result = _paged(payload)
    result["summary"]["scope"] = {**scope, "ownership": ownership}
    if "account_id" in scope:
        result["summary"]["scope"]["note"] = _ACCOUNT_SCOPE_NOTE
    return result


async def _catalog_dpa_signals(client, catalog: dict[str, Any]) -> dict[str, Any]:
    """Collect the documented per-catalog signals used to rank catalog-ad readiness."""
    catalog_id = str(catalog.get("id"))
    signals: dict[str, Any] = {
        "product_count": to_int(catalog.get("product_count")),
        "is_catalog_segment": catalog.get("is_catalog_segment"),
        "product_set_count": None,
        "event_source_count": None,
        "da_must_fix_groups": None,
        "da_must_fix_affected_items": None,
    }
    unavailable: list[dict[str, Any]] = []
    try:
        sets = await _read_edge(client, catalog_id, "product_sets", fields=["id"],
                                params={"limit": 1, "summary": "total_count"})
        total = (sets.get("summary") or {}).get("total_count")
        signals["product_set_count"] = to_int(total) if total is not None else (
            len(sets.get("data") or []) or None
        )
    except _SIGNAL_ERRORS as error:
        unavailable.append(_error_entry("product_sets", error))
    try:
        sources = await _read_edge(client, catalog_id, "external_event_sources",
                                   fields=EVENT_SOURCE_FIELDS, params={"limit": 25})
        signals["event_source_count"] = len(sources.get("data") or [])
    except _SIGNAL_ERRORS as error:
        unavailable.append(_error_entry("external_event_sources", error))
    try:
        diagnostics = await _read_edge(client, catalog_id, "diagnostics", fields=DIAGNOSTIC_FIELDS,
                                       params={"affected_channels": ["da"], "severities": ["MUST_FIX"], "limit": 25})
        groups = diagnostics.get("data") or []
        signals["da_must_fix_groups"] = len(groups)
        signals["da_must_fix_affected_items"] = sum(
            to_int(group.get("number_of_affected_items")) or 0 for group in groups
        )
    except _SIGNAL_ERRORS as error:
        unavailable.append(_error_entry("diagnostics", error))
    return {"signals": signals, "unavailable": unavailable}


def _assess_catalog(signals: dict[str, Any], unavailable: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Apply the stated readiness rules to one catalog's signals."""
    reasons: list[str] = []
    product_count = signals["product_count"]
    if product_count == 0:
        return "blocked", ["Catalog has no products, so there is nothing for catalog ads to show."]
    if product_count is None:
        reasons.append("Meta did not return product_count, so product availability is unknown.")
    else:
        reasons.append(f"Catalog has {product_count} products.")
    problems = False
    if signals["event_source_count"] == 0:
        problems = True
        reasons.append(
            "No pixel, app or other event source is connected; catalog ads cannot match product events "
            "(ViewContent, AddToCart, Purchase) to catalog items without one."
        )
    elif signals["event_source_count"]:
        reasons.append(f"{signals['event_source_count']} event source(s) connected.")
    if signals["da_must_fix_groups"]:
        problems = True
        reasons.append(
            f"{signals['da_must_fix_groups']} must-fix diagnostic group(s) for dynamic ads affecting "
            f"{signals['da_must_fix_affected_items']} item(s)."
        )
    elif signals["da_must_fix_groups"] == 0:
        reasons.append("No must-fix diagnostics for dynamic ads.")
    if signals["product_set_count"] == 0:
        problems = True
        reasons.append("Catalog has no product sets; catalog ads are run from a product set.")
    elif signals["product_set_count"]:
        reasons.append(f"{signals['product_set_count']} product set(s).")
    if unavailable:
        reasons.append(
            "Could not read: " + ", ".join(entry["signal"] for entry in unavailable) + "."
        )
        return "unknown", reasons
    if product_count is None:
        return "unknown", reasons
    return ("needs_attention" if problems else "ready"), reasons


_ASSESSMENT_RANK = {"ready": 0, "needs_attention": 1, "unknown": 2, "blocked": 3}


@mcp_server.tool()
async def list_dpa_eligible_catalogs(
    business_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Rank a business's product catalogs (or an ad account's owning business's) by readiness for Advantage catalog (dynamic) ads, with the reasons and a recommended catalog when exactly one is ready. Derived locally from documented catalog data; Meta publishes no eligibility endpoint."""
    scope_name, scope_value = _one_scope(business_id=business_id, account_id=account_id)
    client = get_graph_api_client()
    scope = await _business_scope(
        client,
        scope_value if scope_name == "business_id" else None,
        scope_value if scope_name == "account_id" else None,
    )
    listing = await _collect_edge(
        client, scope["business_id"], "owned_product_catalogs",
        fields=CATALOG_FIELDS, params={"limit": _DPA_PAGE_SIZE},
        max_pages=_DPA_MAX_LIST_PAGES, max_items=_DPA_MAX_CATALOGS,
    )
    catalogs = [row for row in listing["rows"] if isinstance(row, dict)]
    complete = listing["complete"] and len(catalogs) <= _DPA_MAX_CATALOGS
    catalogs = catalogs[:_DPA_MAX_CATALOGS]

    findings: list[dict[str, Any]] = []
    missing_signals: list[str] = []
    for catalog in catalogs:
        collected = await _catalog_dpa_signals(client, catalog)
        assessment, reasons = _assess_catalog(collected["signals"], collected["unavailable"])
        for entry in collected["unavailable"]:
            missing_signals.append(f"{catalog.get('id')}: {entry['signal']} could not be read ({entry['error']}).")
        findings.append({
            "catalog_id": str(catalog.get("id")),
            "name": catalog.get("name"),
            "vertical": catalog.get("vertical"),
            "assessment": assessment,
            "reasons": reasons,
            "signals": collected["signals"],
            "unavailable_signals": collected["unavailable"],
        })
    findings.sort(key=lambda item: (
        _ASSESSMENT_RANK[item["assessment"]],
        item["signals"]["da_must_fix_affected_items"] or 0,
        -(item["signals"]["product_count"] or 0),
    ))
    for rank, finding in enumerate(findings, start=1):
        finding["rank"] = rank
    ready = [finding for finding in findings if finding["assessment"] == "ready"]
    if len(ready) == 1:
        recommended = ready[0]["catalog_id"]
        reason = "It is the only catalog that passed every readiness rule."
    elif ready:
        recommended = None
        reason = (f"{len(ready)} catalogs passed every readiness rule; choose by which products the "
                  "campaign should promote.")
    else:
        recommended = None
        reason = "No catalog passed every readiness rule; see each catalog's reasons."
    if not complete:
        reason = listing["stop_reason"] or f"Stopped at the {_DPA_MAX_CATALOGS}-catalog cap."
        missing_signals.append(
            f"The catalog list is incomplete: only {len(catalogs)} catalog(s) were evaluated. {reason}"
        )
    return analysis_response(
        scope={**scope, "ownership": "owned"},
        metrics={
            "catalogs_evaluated": len(findings),
            "eligible_count": sum(f["assessment"] in {"ready", "needs_attention"} for f in findings),
            "ready_count": len(ready),
            "complete": complete,
        },
        findings=findings,
        missing_signals=missing_signals,
        extra={
            "recommended_catalog_id": recommended,
            "recommendation_reason": reason,
            "basis": {
                "note": ("This ranking is derived here from documented catalog data; Meta publishes no "
                         "catalog-ad eligibility endpoint."),
                "signals_used": [
                    "product_catalog.product_count",
                    "product_catalog/product_sets",
                    "product_catalog/external_event_sources",
                    "product_catalog/diagnostics",
                ],
                "rules": {
                    "blocked": "product_count is 0.",
                    "ready": ("Has products, at least one product set, at least one connected event source, "
                              "and no must-fix dynamic-ads diagnostics."),
                    "needs_attention": "Has products but fails one of the ready rules.",
                    "unknown": "A signal could not be read or Meta omitted product_count.",
                    "eligible": "ready or needs_attention.",
                },
                "ordering": "assessment, then fewest items affected by must-fix diagnostics, then most products.",
            },
        },
    )


def _diagnostic_sort_key(group: dict[str, Any]) -> tuple[int, int, int]:
    affected = to_int(group.get("number_of_affected_items"))
    entities = to_int(group.get("number_of_affected_entities")) or 0
    severity_rank = 0 if group.get("severity") == "MUST_FIX" else 1
    return (affected is None, -(affected or 0), severity_rank if affected is None else -entities)


def _short_text(value: str) -> str:
    if len(value) <= DIAGNOSTIC_DETAIL_TEXT_LIMIT:
        return value
    return value[:DIAGNOSTIC_DETAIL_TEXT_LIMIT] + "..."


def _detail_sample_entry(entry: Any) -> dict[str, Any] | None:
    """Keep a nested entry's scalar values, cutting long text; lists and objects are left out."""
    if not isinstance(entry, dict):
        return None
    sample: dict[str, Any] = {}
    for key, value in entry.items():
        if isinstance(value, str):
            sample[key] = _short_text(value)
        elif value is None or isinstance(value, (bool, int, float)):
            sample[key] = value
    return sample


def _compact_diagnostic_group(group: dict[str, Any], *, with_sample: bool) -> dict[str, Any]:
    compact = {key: group.get(key) for key in DIAGNOSTIC_SUMMARY_KEYS}
    for key in ("title", "subtitle"):
        if isinstance(compact[key], str):
            compact[key] = _short_text(compact[key])
    nested = group.get("diagnostics")
    nested = nested if isinstance(nested, list) else []
    compact["detail_count"] = len(nested)
    sample = []
    if with_sample:
        for entry in nested[:DIAGNOSTIC_DETAIL_SAMPLE_SIZE]:
            if (trimmed := _detail_sample_entry(entry)) is not None:
                sample.append(trimmed)
    compact["detail_sample"] = sample
    return compact


@mcp_server.tool()
async def get_catalog_diagnostics(
    catalog_id: str,
    severity: str | None = None,
    limit: int = 100,
    after: str | None = None,
    include_details: bool = False,
) -> dict[str, Any]:
    """Product catalog diagnostics (Meta severities MUST_FIX and OPPORTUNITY) ordered by how many products each affects. Ordering applies within the returned page. Each group is a compact summary with a short sample of Meta's nested detail; include_details returns the full nested data, which can be very large."""
    catalog_id = _require(catalog_id, "catalog_id")
    _check_limit(limit)
    params = _page_params(limit, after)
    if severity is not None:
        normalized = severity.strip().upper()
        if normalized not in DIAGNOSTIC_SEVERITIES:
            raise ValidationError(f"severity must be one of: {', '.join(DIAGNOSTIC_SEVERITIES)}.")
        params["severities"] = [normalized]
    payload = await get_graph_api_client().list_objects(
        catalog_id, "diagnostics", fields=DIAGNOSTIC_FIELDS, params=params,
    )
    result = _paged(payload)
    groups = sorted(result["items"], key=_diagnostic_sort_key)
    result["summary"].update({
        "ordered_by": "number_of_affected_items",
        "order_scope": "page",
        "by_severity": dict(Counter(item.get("severity") or "UNKNOWN" for item in groups)),
        "details_included": include_details,
    })
    if include_details:
        result["items"] = groups
        return result
    result["items"] = [
        _compact_diagnostic_group(group, with_sample=index < DIAGNOSTIC_DETAIL_SAMPLE_GROUPS)
        for index, group in enumerate(groups)
    ]
    detail_total = sum(item["detail_count"] for item in result["items"])
    sampled = sum(len(item["detail_sample"]) for item in result["items"])
    result["summary"]["detail_entries_total"] = detail_total
    result["summary"]["detail_note"] = (
        f"Nested detail was left out: {sampled} of {detail_total} nested diagnostic entries are shown as a "
        f"short sample (at most {DIAGNOSTIC_DETAIL_SAMPLE_SIZE} per group for the first "
        f"{DIAGNOSTIC_DETAIL_SAMPLE_GROUPS} groups, scalar values only, text cut at "
        f"{DIAGNOSTIC_DETAIL_TEXT_LIMIT} characters). Call again with include_details=true for the full "
        "nested data."
    )
    return result


async def _health_edge(client, parent_id, edge, fields, params, used, missing, label):
    try:
        payload = await _read_edge(client, parent_id, edge, fields=fields, params=params)
    except _SIGNAL_ERRORS as error:
        missing.append(f"{label} could not be read ({error}).")
        return None
    used.append(label)
    return payload.get("data") or []


@mcp_server.tool()
async def get_catalog_dynamic_ads_health(
    catalog_id: str,
    product_set_id: str | None = None,
) -> dict[str, Any]:
    """Whether a product catalog, or one product set in it, can serve catalog (dynamic) ads, and what blocks it. Combines the catalog's dynamic-ads diagnostics, product counts and connected event sources; lists the signals used."""
    catalog_id = _require(catalog_id, "catalog_id")
    product_set_id = blank_to_none(product_set_id)
    client = get_graph_api_client()
    catalog = await client.get_object(catalog_id, fields=CATALOG_FIELDS)
    used = ["product_catalog.product_count"]
    missing: list[str] = []
    product_set: dict[str, Any] | None = None
    if product_set_id:
        product_set = await client.get_object(product_set_id, fields=[*PRODUCT_SET_FIELDS, "product_catalog"])
        owner = (product_set.get("product_catalog") or {}).get("id")
        if owner and str(owner) != catalog_id:
            raise ValidationError(f"Product set {product_set_id} belongs to catalog {owner}, not {catalog_id}.")
        used.append("product_set.product_count")

    diagnostics = await _health_edge(client, catalog_id, "diagnostics", list(DIAGNOSTIC_SUMMARY_KEYS),
                                     {"affected_channels": ["da"], "limit": 100}, used, missing,
                                     "product_catalog/diagnostics")
    sources = await _health_edge(client, catalog_id, "external_event_sources", EVENT_SOURCE_FIELDS,
                                 {"limit": 25}, used, missing, "product_catalog/external_event_sources")

    blocking: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    catalog_products = to_int(catalog.get("product_count"))
    if catalog_products == 0:
        blocking.append({"severity": "blocking", "source": "product_catalog.product_count",
                         "message": "The catalog has no products, so there is nothing for catalog ads to show."})
    elif catalog_products is None:
        missing.append("Meta did not return the catalog's product_count.")
    set_products = to_int(product_set.get("product_count")) if product_set is not None else None
    if product_set is not None:
        if set_products == 0:
            blocking.append({"severity": "blocking", "source": "product_set.product_count",
                             "message": f"Product set {product_set_id} contains no products. {_EMPTY_SET_MESSAGE}"})
        elif set_products is None:
            missing.append("Meta did not return the product set's product_count.")
    must_fix = sorted(
        (group for group in diagnostics or [] if group.get("severity") == "MUST_FIX"),
        key=_diagnostic_sort_key,
    )
    for group in must_fix:
        issues.append({
            "severity": "issue",
            "source": "product_catalog/diagnostics",
            "type": group.get("type"),
            "title": group.get("title"),
            "message": group.get("subtitle") or group.get("title"),
            "affected_items": to_int(group.get("number_of_affected_items")),
        })
    if sources is not None and not sources:
        issues.append({
            "severity": "issue",
            "source": "product_catalog/external_event_sources",
            "message": ("No pixel, app or other event source is connected to the catalog, so product events "
                        "(ViewContent, AddToCart, Purchase) cannot be matched to catalog items."),
        })

    if blocking:
        status = "blocked"
    elif missing and not issues:
        status = "inconclusive"
    elif issues:
        status = "at_risk"
    else:
        status = "no_blockers_found"
    opportunity_groups = sum(1 for group in diagnostics or [] if group.get("severity") == "OPPORTUNITY")
    return analysis_response(
        scope={"catalog_id": catalog_id, "product_set_id": product_set_id},
        metrics={
            "catalog_product_count": catalog_products,
            "product_set_product_count": set_products,
            "event_source_count": None if sources is None else len(sources),
            "must_fix_groups": len(must_fix),
            "opportunity_groups": opportunity_groups,
        },
        findings=[*blocking, *issues],
        evidence=[
            {"source": "product_catalog", "item": catalog},
            *([{"source": "product_set", "item": product_set}] if product_set is not None else []),
            *([{"source": "product_catalog/external_event_sources", "items": sources}] if sources else []),
        ],
        missing_signals=missing,
        extra={
            "status": status,
            "basis": {
                "signals_used": used,
                "rules": {
                    "blocked": "The catalog or the product set has no products.",
                    "at_risk": "A must-fix dynamic-ads diagnostic exists, or no event source is connected.",
                    "inconclusive": "No problem was found but at least one signal could not be read.",
                    "no_blockers_found": "Every signal was read and none showed a problem.",
                },
                "note": "Derived here from documented Meta signals; Meta has no single catalog-ads health endpoint.",
            },
        },
    )


@mcp_server.tool()
async def list_catalog_data_sources(
    catalog_id: str,
    ingestion_source_type: str = "ALL",
    limit: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """List the feeds, uploads and API sources that supply a product catalog, with counts by upload type. ingestion_source_type is ALL, PRIMARY or SUPPLEMENTARY."""
    catalog_id = _require(catalog_id, "catalog_id")
    _check_limit(limit)
    source_type = (ingestion_source_type or "").strip().upper()
    if source_type not in DATA_SOURCE_TYPES:
        raise ValidationError(f"ingestion_source_type must be one of: {', '.join(DATA_SOURCE_TYPES)}.")
    params = _page_params(limit, after)
    params["ingestion_source_type"] = source_type
    payload = await get_graph_api_client().list_objects(
        catalog_id, "data_sources", fields=DATA_SOURCE_FIELDS, params=params,
    )
    result = _paged(payload)
    result["summary"]["by_upload_type"] = dict(
        Counter(item.get("upload_type") or "UNKNOWN" for item in result["items"])
    )
    return result


@mcp_server.tool()
async def create_catalog(
    business_id: str,
    name: str,
    vertical: str = "commerce",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a product catalog owned by a business. Load products afterwards with the feed or product tools."""
    business_id = _require(business_id, "business_id")
    name = _require(name, "name")
    _choice(vertical, CATALOG_VERTICALS, "vertical")
    payload = merge_graph_payload({"name": name, "vertical": vertical}, params)
    result = await get_graph_api_client().create_edge_object(
        business_id, "owned_product_catalogs", data=payload,
    )
    return creation_response(action="create_catalog", target={"business_id": business_id}, result=result)


@mcp_server.tool()
async def update_catalog(
    catalog_id: str,
    name: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a product catalog's name or other documented settings (params), returning previous and current values."""
    catalog_id = _require(catalog_id, "catalog_id")
    payload = merge_graph_payload({"name": name if name is not None else OMIT}, params)
    if not payload:
        raise ValidationError("At least one field must be provided for update_catalog.")
    if "name" in payload and not blank_to_none(str(payload["name"])):
        raise ValidationError("name must not be blank.")
    client = get_graph_api_client()
    previous = await client.get_object(catalog_id, fields=CATALOG_UPDATE_READ_FIELDS)
    await client.update_object(catalog_id, data=payload)
    return mutation_response(
        action="update_catalog",
        target={"catalog_id": catalog_id},
        previous={key: previous.get(key) for key in CATALOG_UPDATE_READ_FIELDS if key != "id"},
        current=payload,
    )


@mcp_server.tool()
async def list_catalog_products(
    catalog_id: str | None = None,
    product_set_id: str | None = None,
    product_id: str | None = None,
    filter: dict[str, Any] | str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List products in a product catalog or product set, optionally narrowed by a Meta filter object, or read one product. Prices are returned as Meta formats them."""
    scope_name, scope_value = _one_scope(
        catalog_id=catalog_id, product_set_id=product_set_id, product_id=product_id,
    )
    _check_limit(limit)
    parsed_filter = _parse_filter(filter)
    if scope_name == "product_id" and parsed_filter is not None:
        raise ValidationError("filter applies to catalog_id or product_set_id, not product_id.")
    requested = normalize_field_list(fields) or PRODUCT_FIELDS
    client = get_graph_api_client()
    if scope_name == "product_id":
        result = _single(await client.get_object(scope_value, fields=requested))
        result["summary"]["scope"] = {"product_id": scope_value}
        return result
    params = _page_params(limit, after)
    params["summary"] = "total_count"
    if parsed_filter is not None:
        params["filter"] = parsed_filter
    payload = await client.list_objects(scope_value, "products", fields=requested, params=params)
    result = _paged(payload)
    result["summary"]["scope"] = {scope_name: scope_value}
    return result


@mcp_server.tool()
async def create_catalog_product(
    catalog_id: str,
    retailer_id: str,
    name: str,
    price: float,
    currency: str,
    url: str,
    image_url: str,
    availability: str = "in stock",
    description: str | None = None,
    brand: str | None = None,
    condition: str | None = None,
    sale_price: float | None = None,
    visibility: str = "staging",
    allow_upsert: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one product in a product catalog. price and sale_price are major units (12.5 means 12.50). Created hidden (visibility=staging) until updated to published; fails if retailer_id exists unless allow_upsert."""
    catalog_id = _require(catalog_id, "catalog_id")
    typed: dict[str, Any] = {
        "retailer_id": _require(retailer_id, "retailer_id"),
        "name": _require(name, "name"),
    }
    product, _ = _product_fields(
        name=None, price=price, currency=currency, sale_price=sale_price,
        availability=availability, condition=condition, visibility=visibility,
        url=_require(url, "url"), image_url=_require(image_url, "image_url"),
        description=description, brand=brand,
    )
    typed.update({
        "price": product["price"],
        "currency": product["currency"],
        "url": product["url"],
        "image_url": product["image_url"],
        "availability": product["availability"],
        "visibility": product["visibility"],
        "allow_upsert": allow_upsert,
        "sale_price": product.get("sale_price", OMIT),
        "condition": product.get("condition", OMIT),
        "description": product.get("description", OMIT),
        "brand": product.get("brand", OMIT),
    })
    payload = merge_graph_payload(typed, params)
    result = await get_graph_api_client().create_edge_object(catalog_id, "products", data=payload)
    return creation_response(
        action="create_catalog_product",
        target={"catalog_id": catalog_id, "retailer_id": typed["retailer_id"]},
        result=result,
    )


@mcp_server.tool()
async def update_catalog_product(
    product_id: str,
    name: str | None = None,
    price: float | None = None,
    currency: str | None = None,
    sale_price: float | None = None,
    availability: str | None = None,
    condition: str | None = None,
    visibility: str | None = None,
    url: str | None = None,
    image_url: str | None = None,
    description: str | None = None,
    brand: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a catalog product's fields, returning previous and current values. price and sale_price are major units; previous prices are shown as Meta formats them."""
    product_id = _require(product_id, "product_id")
    payload, shown = _product_fields(
        name=name, price=price, currency=currency, sale_price=sale_price,
        availability=availability, condition=condition, visibility=visibility,
        url=url, image_url=image_url, description=description, brand=brand,
    )
    typed = {key: payload.get(key, OMIT) for key in (
        "name", "price", "currency", "sale_price", "availability", "condition",
        "visibility", "url", "image_url", "description", "brand",
    )}
    payload = merge_graph_payload(typed, params)
    if not payload:
        raise ValidationError("At least one field must be provided for update_catalog_product.")
    if params:
        shown.update(params)
    client = get_graph_api_client()
    previous = await client.get_object(product_id, fields=PRODUCT_DETAIL_FIELDS)
    await client.update_object(product_id, data=payload)
    response = mutation_response(
        action="update_catalog_product",
        target={"product_id": product_id},
        previous={key: previous.get(key) for key in payload},
        current=shown,
    )
    response["sent"] = payload
    return response


@mcp_server.tool()
async def delete_catalog_product(product_id: str) -> dict[str, Any]:
    """Delete one product from its product catalog. Use only when the user explicitly asks; the product is read first and returned as previous."""
    product_id = _require(product_id, "product_id")
    client = get_graph_api_client()
    previous = await client.get_object(product_id, fields=PRODUCT_DETAIL_FIELDS)
    result = await client.delete_object(product_id)
    response = mutation_response(
        action="delete_catalog_product",
        target={"product_id": product_id},
        previous=previous,
        current={"deleted": True},
    )
    response["result"] = result
    return response


def _filter_condition(index: int, condition: Any) -> dict[str, Any]:
    """Validate one structured condition and return its Meta rule."""
    label = f"conditions[{index}]"
    if not isinstance(condition, dict):
        raise ValidationError(f"{label} must be an object with field, operator and value.")
    field_name = condition.get("field")
    if not isinstance(field_name, str) or not field_name.strip():
        raise ValidationError(f"{label}.field must be a nonblank product field name such as brand.")
    operator = str(condition.get("operator") or "").strip().lower()
    if operator not in FILTER_OPERATORS:
        raise ValidationError(
            f"{label}.operator {condition.get('operator')!r} is not accepted. "
            f"Accepted operators: {', '.join(FILTER_OPERATORS)}."
        )
    if "value" not in condition:
        raise ValidationError(f"{label}.value is required.")
    value = condition["value"]
    if operator in _LIST_OPERATORS:
        if not isinstance(value, list) or not value:
            raise ValidationError(f"{label}.value must be a nonempty list for {operator}.")
    elif operator in _NUMERIC_OPERATORS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{label}.value must be a number for {operator}.")
    elif operator in _STRING_OPERATORS:
        if not isinstance(value, str):
            raise ValidationError(f"{label}.value must be a string for {operator}.")
        if operator == "starts_with" and field_name.strip() != "category":
            raise ValidationError(f"{label}: Meta documents starts_with only for the category field.")
    elif isinstance(value, (list, dict)) or value is None:
        raise ValidationError(f"{label}.value must be a single string, number or boolean for {operator}.")
    return {field_name.strip(): {operator: value}}


@mcp_server.tool(tags={"read-only"})
async def build_product_set_filter(
    catalog_id: str,
    conditions: list[dict[str, Any]],
    match: str = "all",
) -> dict[str, Any]:
    """Read-only: turn conditions [{field, operator, value}] into a Meta product set filter, then count matching products in the catalog and return up to five samples. Creates nothing. price_amount is price x 100."""
    catalog_id = _require(catalog_id, "catalog_id")
    if not isinstance(conditions, list) or not conditions:
        raise ValidationError("conditions must be a nonempty list of {field, operator, value} objects.")
    normalized_match = (match or "").strip().lower()
    if normalized_match not in {"all", "any"}:
        raise ValidationError("match must be all or any.")
    rules = [_filter_condition(index, condition) for index, condition in enumerate(conditions)]
    product_filter = rules[0] if len(rules) == 1 else {
        ("and" if normalized_match == "all" else "or"): rules
    }
    payload = await get_graph_api_client().list_objects(
        catalog_id, "products", fields=FILTER_SAMPLE_FIELDS,
        params={"filter": product_filter, "summary": "total_count", "limit": 5},
    )
    total = (payload.get("summary") or {}).get("total_count")
    match_count = to_int(total) if total is not None else None
    warnings: list[str] = []
    missing: list[str] = []
    if match_count == 0:
        warnings.append(f"No products match this filter. {_EMPTY_SET_MESSAGE}")
    if match_count is None:
        missing.append("Meta did not return summary.total_count, so the match count is unknown.")
    return {
        "catalog_id": catalog_id,
        "match": normalized_match,
        "filter": product_filter,
        "filter_json": json.dumps(product_filter, separators=(",", ":")),
        "match_count": match_count,
        "sample": (payload.get("data") or [])[:5],
        "warnings": warnings,
        "missing_signals": missing,
        "next_step": "Pass filter to create_product_set or update_product_set to save it.",
    }


@mcp_server.tool()
async def list_product_sets(
    catalog_id: str | None = None,
    product_set_id: str | None = None,
    product_id: str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
) -> dict[str, Any]:
    """List product sets in a product catalog, read one product set, or list the sets that contain a product, with each set's filter and product count."""
    scope_name, scope_value = _one_scope(
        catalog_id=catalog_id, product_set_id=product_set_id, product_id=product_id,
    )
    _check_limit(limit)
    requested = normalize_field_list(fields) or PRODUCT_SET_FIELDS
    client = get_graph_api_client()
    if scope_name == "product_set_id":
        result = _single(await client.get_object(scope_value, fields=requested))
        result["summary"]["scope"] = {"product_set_id": scope_value}
        return result
    params = _page_params(limit, after)
    if scope_name == "catalog_id":
        params["summary"] = "total_count"
    payload = await client.list_objects(scope_value, "product_sets", fields=requested, params=params)
    result = _paged(payload)
    result["summary"]["scope"] = {scope_name: scope_value}
    return result


@mcp_server.tool()
async def create_product_set(
    catalog_id: str,
    name: str,
    filter: dict[str, Any] | str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a product set in a product catalog from a Meta filter object ({} means every product). Build and test the filter first with build_product_set_filter."""
    catalog_id = _require(catalog_id, "catalog_id")
    name = _require(name, "name")
    parsed_filter = _parse_filter(filter)
    payload = merge_graph_payload({"name": name, "filter": parsed_filter}, params)
    result = await get_graph_api_client().create_edge_object(catalog_id, "product_sets", data=payload)
    return creation_response(action="create_product_set", target={"catalog_id": catalog_id}, result=result)


@mcp_server.tool()
async def update_product_set(
    product_set_id: str,
    name: str | None = None,
    filter: dict[str, Any] | str | None = None,
    retailer_id: str | None = None,
) -> dict[str, Any]:
    """Edit a product set's name, filter or retailer_id, returning previous and current values including the previous product count."""
    product_set_id = _require(product_set_id, "product_set_id")
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = _require(name, "name")
    parsed_filter = _parse_filter(filter)
    if parsed_filter is not None:
        payload["filter"] = parsed_filter
    if retailer_id is not None:
        payload["retailer_id"] = retailer_id
    if not payload:
        raise ValidationError("At least one field must be provided for update_product_set.")
    client = get_graph_api_client()
    previous = await client.get_object(product_set_id, fields=PRODUCT_SET_FIELDS)
    await client.update_object(product_set_id, data=payload)
    previous_values = {key: previous.get(key) for key in payload}
    previous_values["product_count"] = previous.get("product_count")
    return mutation_response(
        action="update_product_set",
        target={"product_set_id": product_set_id},
        previous=previous_values,
        current=payload,
    )


@mcp_server.tool()
async def delete_product_set(product_set_id: str) -> dict[str, Any]:
    """Delete a product set; its products stay in the catalog. Use only on explicit request. Meta refuses while an active ad uses the set."""
    product_set_id = _require(product_set_id, "product_set_id")
    client = get_graph_api_client()
    previous = await client.get_object(product_set_id, fields=PRODUCT_SET_FIELDS)
    result = await client.delete_object(product_set_id)
    response = mutation_response(
        action="delete_product_set",
        target={"product_set_id": product_set_id},
        previous=previous,
        current={"deleted": True},
    )
    response["result"] = result
    return response
