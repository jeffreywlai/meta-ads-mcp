"""Meta product catalog, product and product set tools.

"Catalog" here always means a Meta product catalog (the Commerce Manager object
that feeds Advantage+ catalog ads), never this server's own list of tools.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, TypeAlias
from urllib.parse import urlparse

from pydantic import BeforeValidator, Field

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import (
    MetaApiError,
    NotFoundError,
    RateLimitError,
    UnsupportedFeatureError,
    ValidationError,
)
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.normalize import blank_to_none, normalize_collection, to_int
from meta_ads_mcp.schemas import analysis_response, creation_response, mutation_response
from meta_ads_mcp.tool_types import FieldList, GraphId, normalize_field_list
# The feed tools' masking: a URL password in a result or in a raised error's text
# is replaced, keeping user, host and path, and a password or token the caller supplied
# is removed; the URL-login check on inputs is shared too. catalog_feeds imports nothing
# from here.
from meta_ads_mcp.tools.catalog_feeds import _count_keys, _guard_secrets, _masked_json_text, _redact
from meta_ads_mcp.tools.catalog_feeds import _refuse_url_logins as _refuse_url_logins_shared

# Fields documented on the Product Catalog reference page.
CATALOG_FIELDS = ["id", "name", "vertical", "product_count", "feed_count", "is_catalog_segment"]
CATALOG_UPDATE_READ_FIELDS = [
    "id", "name", "vertical", "da_display_settings", "default_image_url", "fallback_image_url",
]
# Catalog update parameters whose prior value can be read back, beyond the
# fixed projection above: update parameter -> readable field. Each is read
# only when it is being changed, and Meta refusing one of these reads makes
# its previous value unavailable instead of blocking the update.
#   store_catalog_settings: readable on ProductCatalog and accepted by its
#     api_update in SDK 26.0.2 (Meta's reference lists it as an update
#     parameter only).
#   checkout_url, checkout_country: Meta's Product Catalog reference documents
#     the readable checkout_urls field, one entry per sell-to country, matching
#     these update parameters.
CATALOG_EXTRA_READABLE = {
    "store_catalog_settings": "store_catalog_settings",
    "checkout_url": "checkout_urls",
    "checkout_country": "checkout_urls",
}
# Keys inside a catalog setting that hold credentials (partner_integration's
# external_access_token); they are sent but never shown back.
_SECRET_SETTING_KEYS = frozenset({"external_access_token", "access_token", "password"})
# Where a caller's input keys are field names, so that a key naming a password or token
# holds one: the tool's named parameters, the Graph params passed through to Meta, and
# params.partner_integration (external_access_token), as an object or as its JSON text.
# Every other mapping (a product's custom_data, a product set filter, a batch item's
# fields) is not searched by key name; a password inside a URL is found anywhere.
_CATALOG_CREDENTIAL_PATHS = frozenset({(), ("params",), ("params", "partner_integration")})
# Catalog settings Meta documents as objects that can hold a credential; passed as JSON
# text, the credential is masked inside the text when shown, as in the object form.
_CATALOG_CREDENTIAL_OBJECT_KEYS = frozenset({"partner_integration"})
# Keys whose value is a data map whose own keys are the advertiser's values, never field
# names: a product's custom data and variant attributes, and a product set filter (keyed by
# whatever product fields and values the advertiser filters on). A key inside one is never
# masked as a credential, so the values shown are the values sent.
_CATALOG_DATA_MAP_KEYS = frozenset({"custom_data", "additional_variant_attributes", "filter"})
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
# Product item fields Meta's reference documents both as readable fields and as
# update parameters. An update that changes one of these through params reads
# it first, so previous shows its real prior value.
PRODUCT_READABLE_UPDATE_FIELDS = frozenset({
    "additional_image_urls", "additional_variant_attributes", "age_group", "availability",
    "brand", "category", "category_specific_fields", "color", "condition", "currency",
    "custom_data", "custom_label_0", "custom_label_1", "custom_label_2", "custom_label_3",
    "custom_label_4", "description", "expiration_date", "fb_product_category", "gender", "gtin",
    "image_url", "importer_address", "importer_name", "inventory", "live_special_price",
    "manufacturer_info", "manufacturer_part_number", "marked_for_product_launch", "material",
    "mobile_link", "name", "ordering_index", "origin_country", "pattern", "price",
    "product_type", "retailer_id", "sale_price", "sale_price_end_date", "sale_price_start_date",
    "short_description", "size", "start_date", "url", "visibility", "wa_compliance_category",
})
# Product item fields readable on ProductItem and accepted by its api_update in
# SDK 26.0.2 that Meta's reference does not list as readable. They are read
# before a change, and Meta refusing the read makes only their previous value
# unavailable.
PRODUCT_SDK_READABLE_UPDATE_FIELDS = frozenset({
    "custom_number_0", "custom_number_1", "custom_number_2", "custom_number_3", "custom_number_4",
    "quantity_to_sell_on_facebook", "rich_text_description",
})
_UNREADABLE_PREVIOUS = "Meta does not document this field as readable, so its prior value could not be read."
_REFUSED_PREVIOUS = (
    "Meta refused to read {field} before the update, so its prior value could not be read; the update "
    "was still sent."
)
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
# list_dpa_eligible_catalogs only counts must-fix groups and their affected items.
DPA_DIAGNOSTIC_FIELDS = ["type", "severity", "number_of_affected_items"]
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
# Supporting edges (product sets, event sources, diagnostics) are read to the
# end within this many pages each; anything left unread is reported as partial.
_SIGNAL_PAGE_SIZE = 25
_SIGNAL_MAX_PAGES = 4
_HEALTH_DIAGNOSTIC_PAGE_SIZE = 100
_HEALTH_MAX_PAGES = 5
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


def _paged(payload: dict[str, Any], *, limit: int | None = None, after: str | None = None) -> dict[str, Any]:
    """Shape one Graph page with the completeness fields list_audiences uses.

    Meta's products edge returns a next link even when every row has already been
    returned (seen live: 5 of 5 rows, limit 50, and the page after it is empty). So
    a next link is not believed when this is the first page, it is shorter than the
    limit, and it already holds as many rows as Meta's own total_count.
    """
    normalized = normalize_collection(payload)
    has_more = bool(normalized["paging"].get("next"))
    stated_total = to_int((payload.get("summary") or {}).get("total_count"))
    rows = len(normalized["items"])
    if (
        has_more and limit is not None and blank_to_none(after) is None
        and stated_total is not None and rows < limit and rows >= stated_total
    ):
        has_more = False
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
        except json.JSONDecodeError:
            # The decoder's error holds the whole text; it is not chained to this one.
            value = None
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


def _reject_bool_price(value: Any) -> Any:
    """Refuse true/false before Pydantic's lax float coercion turns them into 1.0 or 0.0."""
    if isinstance(value, bool):
        raise ValueError("a price must be a number, not true or false")
    return value


# Every money input on the catalog tools uses this type, so the MCP boundary
# rejects booleans before numeric coercion; _catalog_price repeats the check
# for direct Python calls.
CatalogPrice: TypeAlias = Annotated[
    float,
    BeforeValidator(_reject_bool_price),
    Field(description="Price in major units (12.5 means 12.50); sent to Meta as amount x 100."),
]


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


async def _read_previous(
    client, object_id: str, base_fields: list[str], extra_fields: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """Read an object before an update; return (values, extra fields Meta refused to read).

    extra_fields are read only because a change touches them. If Meta rejects
    the request as an invalid parameter (code 100, as for a field it will not
    read), it is repeated once with base_fields alone and every extra field is
    reported as refused. Any other failure, or a failed repeat, is raised, so
    nothing is written without a previous value.
    """
    extra = [name for name in extra_fields if name not in base_fields]
    try:
        return await client.get_object(object_id, fields=[*base_fields, *extra]), []
    except MetaApiError as error:
        if not extra or error.code != 100:
            raise
    return await client.get_object(object_id, fields=list(base_fields)), extra


def _mask_secrets(value: Any, *, in_data: bool = False) -> Any:
    """Return value with credential keys replaced, for showing what was sent.

    A key is taken to name a credential only where keys are field names: not inside a
    data map (_CATALOG_DATA_MAP_KEYS), whose keys are the advertiser's own values. A
    partner_integration given as JSON text is shown as that text with its credentials
    replaced; the text sent to Meta is unchanged.
    """
    if isinstance(value, dict):
        shown: dict[Any, Any] = {}
        for key, item in value.items():
            if not in_data and str(key).lower() in _SECRET_SETTING_KEYS and item is not None:
                shown[key] = "[redacted]"
            elif not in_data and key in _CATALOG_CREDENTIAL_OBJECT_KEYS and isinstance(item, str):
                shown[key] = _masked_json_text(item, _mask_secrets)
            else:
                shown[key] = _mask_secrets(item, in_data=in_data or key in _CATALOG_DATA_MAP_KEYS)
        return shown
    if isinstance(value, list):
        return [_mask_secrets(item, in_data=in_data) for item in value]
    return value


def _refuse_url_logins(value: Any, name: str) -> None:
    """Refuse a caller value holding a URL with a username or password, at any depth.

    Every string inside value is checked, whether it is the URL itself or text with
    a URL in it (a description, a filter value), and so is every mapping key, so a
    login cannot reach Meta, where it would be stored and shown back. The message
    names the location by parameter, index and key position (a key by name only when
    it is a field name this module defines), never the URL or any other key.
    """
    _refuse_url_logins_shared(value, name, _known_keys())


def _refuse_url_logins_in(**values: Any) -> None:
    for name, value in values.items():
        _refuse_url_logins(value, name)


def _shown(value: Any) -> Any:
    """Copy a value for a response: URL passwords and credential keys are masked."""
    return _mask_secrets(_redact(value, _CATALOG_DATA_MAP_KEYS))


def _masked(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Run a tool so that neither its result nor an error it raises shows a password or token.

    Every tool in this module goes through this, so a value read back from Meta
    (a stored product or checkout URL, a diagnostic, an error message) cannot
    show a URL password, whichever field or key it arrives in, and a password or
    token the caller supplied (such as params.partner_integration.external_access_token)
    is removed wherever Meta repeats it, in the result or in an error and every error
    behind it.
    """
    return _guard_secrets(fn, _shown, _CATALOG_CREDENTIAL_PATHS)


def _row_identity(row: Any) -> str | None:
    """Return a row's Meta id, or None when the row carries none."""
    if not isinstance(row, dict):
        return None
    value = row.get("id")
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    text = str(value).strip()
    return text or None


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
    no cursor, a cursor already requested, or a page already read stops the scan
    with a stated reason, and that page's rows are not added twice. Pages are
    compared by identity, never by the values a narrow field selection
    returns: rows carrying an id are kept once per id, and a page whose rows
    were all read before is a repeat. Rows without an id cannot be told apart,
    so a page of them identical to an earlier one stops the read as
    incomplete, saying it is ambiguous, rather than being counted twice or
    called a repeat. max_pages caps requests regardless of how many rows came
    back, so a run of empty pages cannot loop. With stop_at_total, the first
    summary total_count Meta returns ends the read. summary_total is the first
    summary total_count Meta returned on any page, or None.
    """
    rows: list[Any] = []
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    seen_anonymous_pages: set[str] = set()
    cursor: str | None = None
    pages = 0
    summary_total: int | None = None
    while True:
        page_params = dict(params)
        if cursor is not None:
            page_params["after"] = cursor
            seen_cursors.add(cursor)
        payload = await client.list_objects(parent_id, edge, fields=fields, params=page_params)
        pages += 1
        data = payload.get("data") or []
        if data:
            identities = [_row_identity(row) for row in data]
            if all(identity is not None for identity in identities):
                if all(identity in seen_ids for identity in identities):
                    return {"rows": rows, "complete": False, "total_count": None, "pages": pages,
                            "stop_reason": "Meta returned a page whose rows had all already been read.",
                            "summary_total": summary_total}
                for identity, row in zip(identities, data):
                    if identity not in seen_ids:
                        seen_ids.add(identity)
                        rows.append(row)
            else:
                fingerprint = json.dumps(data, sort_keys=True, default=str)
                if fingerprint in seen_anonymous_pages:
                    return {"rows": rows, "complete": False, "total_count": None, "pages": pages,
                            "stop_reason": ("Meta returned a page identical to one already read; its rows carry "
                                            "no id, so whether it repeats that page or holds further identical "
                                            "rows cannot be told, and the read stopped."),
                            "summary_total": summary_total}
                seen_anonymous_pages.add(fingerprint)
                rows.extend(data)
        total = (payload.get("summary") or {}).get("total_count")
        if summary_total is None:
            summary_total = to_int(total)
        if stop_at_total and to_int(total) is not None:
            return {"rows": rows, "complete": True, "total_count": to_int(total), "pages": pages,
                    "stop_reason": None, "summary_total": summary_total}
        paging = payload.get("paging") or {}
        if not paging.get("next"):
            return {"rows": rows, "complete": True, "total_count": None, "pages": pages, "stop_reason": None,
                    "summary_total": summary_total}
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
        return {"rows": rows, "complete": False, "total_count": None, "pages": pages, "stop_reason": reason,
                "summary_total": summary_total}


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
@_masked
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


_DPA_URL_SAMPLE_SIZE = 5


def _host(url: Any) -> str | None:
    """Return a URL's lowercased host without a leading www., or None."""
    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        host = (urlparse(text).hostname or "").lower()
    except ValueError:
        return None
    if not host or "." not in host or " " in host:
        return None
    return host[4:] if host.startswith("www.") else host


def _same_site(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host)


async def _ad_domain_match(client, catalog_id: str, domain: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Compare a small sample of product URLs with the ad's domain (one request)."""
    try:
        payload = await client.list_objects(catalog_id, "products", fields=["url"],
                                            params={"limit": _DPA_URL_SAMPLE_SIZE})
    except _SIGNAL_ERRORS as error:
        return {"domain": domain, "sampled_urls": 0, "matching_urls": 0, "match": "unknown"}, \
            _error_entry("products (url sample)", error)
    hosts = [host for row in payload.get("data") or [] if isinstance(row, dict)
             if (host := _host(row.get("url"))) is not None]
    matching = sum(_same_site(host, domain) for host in hosts)
    if not hosts:
        match = "unknown"
    elif matching == len(hosts):
        match = "yes"
    elif matching:
        match = "partial"
    else:
        match = "no"
    return {"domain": domain, "sampled_urls": len(hosts), "matching_urls": matching, "match": match}, None


async def _catalog_dpa_signals(
    client,
    catalog: dict[str, Any],
    *,
    ad_domain: str | None = None,
    pixel_id: str | None = None,
) -> dict[str, Any]:
    """Collect the documented per-catalog signals used to rank catalog-ad readiness.

    A catalog Meta reports with no products is blocked whatever else is true, so
    its supporting edges are not read.
    """
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
    partial: list[dict[str, Any]] = []
    context: dict[str, Any] = {}
    if signals["product_count"] == 0:
        if ad_domain is not None:
            context["ad_domain"] = {"domain": ad_domain, "sampled_urls": 0, "matching_urls": 0, "match": "unknown"}
        if pixel_id is not None:
            context["pixel"] = {"pixel_id": pixel_id, "connected": None}
        return {"signals": signals, "unavailable": unavailable, "partial": partial, "context": context,
                "skipped": "The catalog has no products, so its product sets, event sources and diagnostics were not read."}

    def note_partial(signal: str, read: dict[str, Any], rows_read: int) -> None:
        partial.append({
            "signal": signal,
            "reason": read["stop_reason"],
            "rows_read": rows_read,
            "count_is_lower_bound": rows_read > 0,
        })

    try:
        sets = await _collect_edge(client, catalog_id, "product_sets", fields=["id"],
                                   params={"limit": _SIGNAL_PAGE_SIZE, "summary": "total_count"},
                                   max_pages=_SIGNAL_MAX_PAGES, stop_at_total=True)
        if sets["total_count"] is not None:
            signals["product_set_count"] = sets["total_count"]
        elif sets["complete"]:
            signals["product_set_count"] = len(sets["rows"])
        else:
            note_partial("product_sets", sets, len(sets["rows"]))
            signals["product_set_count"] = len(sets["rows"]) or None
    except _SIGNAL_ERRORS as error:
        unavailable.append(_error_entry("product_sets", error))
    try:
        sources = await _collect_edge(client, catalog_id, "external_event_sources", fields=["id"],
                                      params={"limit": _SIGNAL_PAGE_SIZE}, max_pages=_SIGNAL_MAX_PAGES)
        if sources["complete"]:
            signals["event_source_count"] = len(sources["rows"])
        else:
            note_partial("external_event_sources", sources, len(sources["rows"]))
            signals["event_source_count"] = len(sources["rows"]) or None
        if pixel_id is not None:
            source_ids = {str(row.get("id")) for row in sources["rows"] if isinstance(row, dict)}
            connected = True if pixel_id in source_ids else (False if sources["complete"] else None)
            context["pixel"] = {"pixel_id": pixel_id, "connected": connected}
    except _SIGNAL_ERRORS as error:
        unavailable.append(_error_entry("external_event_sources", error))
        if pixel_id is not None:
            context["pixel"] = {"pixel_id": pixel_id, "connected": None}
    try:
        diagnostics = await _collect_edge(
            client, catalog_id, "diagnostics", fields=DPA_DIAGNOSTIC_FIELDS,
            params={"affected_channels": ["da"], "severities": ["MUST_FIX"], "limit": _SIGNAL_PAGE_SIZE},
            max_pages=_SIGNAL_MAX_PAGES,
        )
        groups = [group for group in diagnostics["rows"] if isinstance(group, dict)]
        affected = sum(to_int(group.get("number_of_affected_items")) or 0 for group in groups)
        if diagnostics["complete"] or groups:
            signals["da_must_fix_groups"] = len(groups)
            signals["da_must_fix_affected_items"] = affected
        if not diagnostics["complete"]:
            note_partial("diagnostics", diagnostics, len(groups))
    except _SIGNAL_ERRORS as error:
        unavailable.append(_error_entry("diagnostics", error))
    context_errors: list[dict[str, Any]] = []
    if ad_domain is not None:
        context["ad_domain"], error_entry = await _ad_domain_match(client, catalog_id, ad_domain)
        if error_entry is not None:
            context_errors.append(error_entry)
    return {"signals": signals, "unavailable": unavailable, "partial": partial, "context": context,
            "context_errors": context_errors}


_DPA_REQUIRED_SIGNALS = (
    ("product_set_count", "product_sets"),
    ("event_source_count", "external_event_sources"),
    ("da_must_fix_groups", "diagnostics"),
)


# ad_format values Meta's own catalog tool accepts. A_PLUS_CA is Advantage
# catalog ads (the default rules here). PE is product extensions, whose
# documented eligibility (developers.facebook.com/docs/marketing-api/
# advantage-catalog-ads/product-extensions) asks for a catalog and a product
# set (creative_sourcing_spec.associated_product_set_id) and names no event
# source, so a connected event source is reported but not required.
DPA_AD_FORMATS = ("A_PLUS_CA", "PE")


def _assess_catalog(
    signals: dict[str, Any],
    unavailable: list[dict[str, Any]],
    partial: list[dict[str, Any]] | None = None,
    *,
    ad_format: str = "A_PLUS_CA",
) -> tuple[str, list[str]]:
    """Apply the stated readiness rules to one catalog's signals.

    A signal that is unknown (not read, or only partly read without a deciding
    row) can never let a catalog count as ready. With ad_format PE an event
    source is not required, so its absence or unreadability is reported
    without deciding readiness.
    """
    partial = partial or []
    needs_event_source = ad_format != "PE"
    reasons: list[str] = []
    product_count = signals["product_count"]
    if product_count == 0:
        return "blocked", ["Catalog has no products, so there is nothing for catalog ads to show."]
    if product_count is None:
        reasons.append("Meta did not return product_count, so product availability is unknown.")
    else:
        reasons.append(f"Catalog has {product_count} products.")
    problems = False
    if signals["event_source_count"] == 0 and not needs_event_source:
        reasons.append(
            "No pixel, app or other event source is connected; Meta's product extensions documentation does "
            "not list one as a requirement."
        )
    elif signals["event_source_count"] == 0:
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
    for entry in partial:
        reasons.append(
            f"{entry['signal']} was only partly read ({entry['reason']})"
            + ("; its count is a lower bound." if entry["count_is_lower_bound"] else "; nothing was found before the read stopped.")
        )
    deciding_unavailable = [
        entry for entry in unavailable
        if needs_event_source or entry["signal"] != "external_event_sources"
    ]
    if unavailable:
        reasons.append(
            "Could not read: " + ", ".join(entry["signal"] for entry in unavailable) + "."
        )
    if deciding_unavailable:
        return "unknown", reasons
    unknown = [edge for key, edge in _DPA_REQUIRED_SIGNALS if signals.get(key) is None
               and (needs_event_source or key != "event_source_count")]
    if unknown:
        reasons.append("Not established: " + ", ".join(unknown) + ", so the catalog cannot be called ready.")
        return "unknown", reasons
    if product_count is None:
        return "unknown", reasons
    return ("needs_attention" if problems else "ready"), reasons


_ASSESSMENT_RANK = {"ready": 0, "needs_attention": 1, "unknown": 2, "blocked": 3}


_ELIGIBLE_GROUP = {"ready": 0, "needs_attention": 0, "unknown": 1, "blocked": 2}


def _context_reasons(context: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    domain = context.get("ad_domain")
    if domain is not None:
        if domain["match"] == "unknown":
            reasons.append(f"Could not compare product URLs with {domain['domain']} (no product URL was read).")
        else:
            reasons.append(
                f"{domain['matching_urls']} of {domain['sampled_urls']} sampled product URL(s) are on "
                f"{domain['domain']}."
            )
    pixel = context.get("pixel")
    if pixel is not None:
        if pixel["connected"] is True:
            reasons.append(f"Connected to pixel {pixel['pixel_id']}.")
        elif pixel["connected"] is False:
            reasons.append(f"Not connected to pixel {pixel['pixel_id']}.")
        else:
            reasons.append(f"Whether pixel {pixel['pixel_id']} is connected could not be established.")
    page = context.get("page")
    if page is not None:
        if page["linked"] is True:
            reasons.append(f"Listed among Page {page['page_id']}'s product catalogs.")
        elif page["linked"] is False:
            reasons.append(f"Not listed among Page {page['page_id']}'s product catalogs.")
        else:
            reasons.append(f"Whether Page {page['page_id']} lists this catalog could not be established.")
    return reasons


def _context_score(context: dict[str, Any]) -> tuple[int, int]:
    """Return (checks matched, checks supplied) for the optional ad context."""
    matched = supplied = 0
    if (domain := context.get("ad_domain")) is not None:
        supplied += 1
        matched += domain["match"] in {"yes", "partial"}
    if (pixel := context.get("pixel")) is not None:
        supplied += 1
        matched += pixel["connected"] is True
    if (page := context.get("page")) is not None:
        supplied += 1
        matched += page["linked"] is True
    return matched, supplied


async def _page_catalog_ids(client, page_id: str) -> dict[str, Any]:
    """Read the catalogs a Page lists (GET /{page_id}/product_catalogs), within the page cap.

    Meta documents this edge as the product catalogs owned by the Page, with no
    parameters. ids is None when the edge could not be read at all.
    """
    try:
        read = await _collect_edge(client, page_id, "product_catalogs", fields=["id"], params={},
                                   max_pages=_SIGNAL_MAX_PAGES)
    except _SIGNAL_ERRORS as error:
        return {"ids": None, "complete": False, "error": _error_entry("page product_catalogs", error)}
    ids = {identity for row in read["rows"] if (identity := _row_identity(row)) is not None}
    return {"ids": ids, "complete": read["complete"], "stop_reason": read["stop_reason"], "error": None}


@mcp_server.tool()
@_masked
async def list_dpa_eligible_catalogs(
    business_id: str | None = None,
    account_id: str | None = None,
    ad_url: str | None = None,
    pixel_id: GraphId | None = None,
    page_id: GraphId | None = None,
    ad_format: str | None = None,
) -> dict[str, Any]:
    """Rank a business's product catalogs (or an ad account's owning business's) by readiness for Advantage catalog (dynamic) ads, with the reasons and a recommended catalog when exactly one is ready. Optional ad_url, pixel_id and page_id put first the eligible catalogs whose sampled product URLs are on the ad's domain, that are connected to that pixel, or that the Page lists as its catalogs, and say why. ad_format PE (product extensions) applies that format's documented needs, a catalog with a product set, instead of A_PLUS_CA, the default. Derived locally from documented catalog data; Meta publishes no eligibility endpoint."""
    scope_name, scope_value = _one_scope(business_id=business_id, account_id=account_id)
    ad_domain = None
    _refuse_url_logins(ad_url, "ad_url")
    if ad_url is not None:
        ad_domain = _host(ad_url)
        if ad_domain is None:
            raise ValidationError("ad_url must be a web address such as https://shop.example.com/product.")
    pixel_id = blank_to_none(pixel_id) if isinstance(pixel_id, str) else None
    page_id = blank_to_none(page_id) if isinstance(page_id, str) else None
    normalized_format = "A_PLUS_CA"
    if ad_format is not None:
        normalized_format = ad_format.strip().upper() if isinstance(ad_format, str) else ""
        if normalized_format not in DPA_AD_FORMATS:
            raise ValidationError(
                "ad_format must be A_PLUS_CA (Advantage catalog ads) or PE (product extensions)."
            )
    has_context = ad_domain is not None or pixel_id is not None or page_id is not None
    client = get_graph_api_client()
    scope = await _business_scope(
        client,
        scope_value if scope_name == "business_id" else None,
        scope_value if scope_name == "account_id" else None,
    )
    page_read = await _page_catalog_ids(client, page_id) if page_id is not None else None
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
        collected = await _catalog_dpa_signals(client, catalog, ad_domain=ad_domain, pixel_id=pixel_id)
        if page_read is not None:
            catalog_key = str(catalog.get("id"))
            if page_read["ids"] is None:
                linked = None
            elif catalog_key in page_read["ids"]:
                linked = True
            else:
                linked = False if page_read["complete"] else None
            collected["context"]["page"] = {"page_id": page_id, "linked": linked}
        assessment, reasons = _assess_catalog(
            collected["signals"], collected["unavailable"], collected["partial"], ad_format=normalized_format,
        )
        for entry in collected["unavailable"]:
            missing_signals.append(f"{catalog.get('id')}: {entry['signal']} could not be read ({entry['error']}).")
        for entry in collected["partial"]:
            missing_signals.append(
                f"{catalog.get('id')}: {entry['signal']} was only partly read "
                f"({entry['rows_read']} row(s) read). {entry['reason']}"
            )
        for entry in collected.get("context_errors", []):
            missing_signals.append(f"{catalog.get('id')}: {entry['signal']} could not be read ({entry['error']}).")
        finding = {
            "catalog_id": str(catalog.get("id")),
            "name": catalog.get("name"),
            "vertical": catalog.get("vertical"),
            "assessment": assessment,
            "reasons": reasons,
            "signals": collected["signals"],
            "unavailable_signals": collected["unavailable"],
            "partial_signals": collected["partial"],
        }
        if collected.get("skipped"):
            finding["signals_skipped"] = collected["skipped"]
        if has_context:
            finding["context_match"] = collected["context"]
            reasons.extend(_context_reasons(collected["context"]))
        findings.append(finding)
    findings.sort(key=lambda item: (
        _ELIGIBLE_GROUP[item["assessment"]],
        -_context_score(item.get("context_match") or {})[0],
        _ASSESSMENT_RANK[item["assessment"]],
        item["signals"]["da_must_fix_affected_items"] or 0,
        -(item["signals"]["product_count"] or 0),
    ))
    for rank, finding in enumerate(findings, start=1):
        finding["rank"] = rank
    ready = [finding for finding in findings if finding["assessment"] == "ready"]
    supplied = ", ".join(
        name for name, value in (("ad_url", ad_domain), ("pixel_id", pixel_id), ("page_id", page_id)) if value
    )
    if page_read is not None:
        if page_read["error"] is not None:
            missing_signals.append(
                f"Page {page_id}'s product catalogs could not be read ({page_read['error']['error']}), so no "
                "catalog could be matched to the Page."
            )
        elif not page_read["complete"]:
            missing_signals.append(
                f"Page {page_id}'s product catalogs were only partly read. {page_read['stop_reason']}"
            )
    if has_context:
        matched_ready = [
            finding for finding in ready
            if (score := _context_score(finding["context_match"]))[0] == score[1]
        ]
        if len(matched_ready) == 1:
            recommended = matched_ready[0]["catalog_id"]
            reason = f"It is the only catalog that passed every readiness rule and matched {supplied}."
        elif matched_ready:
            recommended = None
            reason = (f"{len(matched_ready)} ready catalogs matched {supplied}; choose by which products the "
                      "campaign should promote.")
        else:
            recommended = None
            reason = (f"No ready catalog matched {supplied}; check that the ad's site, pixel and Page belong "
                      "to one of these catalogs.")
    elif len(ready) == 1:
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
        stop = listing["stop_reason"] or f"Stopped at the {_DPA_MAX_CATALOGS}-catalog cap."
        recommended = None
        reason = (f"The catalog list is incomplete, so no catalog can be called the only choice. {stop}")
        missing_signals.append(
            f"The catalog list is incomplete: only {len(catalogs)} catalog(s) were evaluated. {stop}"
        )
    signals_used = [
        "product_catalog.product_count",
        "product_catalog/product_sets",
        "product_catalog/external_event_sources",
        "product_catalog/diagnostics",
    ]
    rules = {
        "blocked": "product_count is 0. Supporting edges are not read for such a catalog.",
        "ready": ("Has products, at least one product set, at least one connected event source, "
                  "and no must-fix dynamic-ads diagnostics."),
        "needs_attention": "Has products but fails one of the ready rules.",
        "unknown": ("A signal could not be read, was only partly read without a deciding result, "
                    "or Meta omitted product_count."),
        "eligible": "ready or needs_attention.",
    }
    ordering = "assessment, then fewest items affected by must-fix diagnostics, then most products."
    if ad_domain is not None:
        signals_used.append(f"product_catalog/products (url of up to {_DPA_URL_SAMPLE_SIZE} products)")
        rules["ad_url"] = (f"Matches when at least one of up to {_DPA_URL_SAMPLE_SIZE} sampled product URLs is on "
                           "the ad's domain or a subdomain of it; a small sample, not every product.")
    if pixel_id is not None:
        rules["pixel_id"] = "Matches when the pixel is among the catalog's connected event sources."
    extra_basis: dict[str, Any] = {}
    if page_id is not None:
        signals_used.append("page/product_catalogs (catalogs the Page lists)")
        rules["page_id"] = ("Matches when the catalog is among the product catalogs Meta lists for the Page "
                            "(GET /{page_id}/product_catalogs, documented as the catalogs the Page owns).")
        if page_read is not None and page_read["ids"]:
            evaluated = {str(catalog.get("id")) for catalog in catalogs}
            outside = sorted(page_read["ids"] - evaluated)
            if outside:
                extra_basis["page_catalogs_not_evaluated"] = outside[:20]
                extra_basis["page_catalogs_not_evaluated_note"] = (
                    "The Page lists these catalogs, but they are not among this business's owned catalogs that "
                    "were evaluated."
                )
    if normalized_format == "PE":
        rules["ready"] = ("Has products, at least one product set, and no must-fix dynamic-ads diagnostics. "
                          "Meta's product extensions documentation asks for a catalog and a product set "
                          "(associated_product_set_id) and names no event source, so event sources are reported "
                          "but not required. The must-fix rule is kept as a precaution; Meta does not say "
                          "whether dynamic-ads diagnostics apply to product extensions.")
        rules["ad_format"] = ("PE also needs a SALES or TRAFFIC campaign and a single image or video ad, which "
                              "this catalog check cannot see.")
    if has_context:
        ordering = ("eligible catalogs first, then the most context checks matched (ad_url, pixel_id, page_id), "
                    "then assessment, fewest items affected by must-fix diagnostics, and most products.")
        rules["recommended"] = "The only ready catalog that matched every context check supplied."
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
                "ad_format": normalized_format,
                "signals_used": signals_used,
                "rules": rules,
                "ordering": ordering,
                **extra_basis,
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
@_masked
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
    """Read a supporting edge to the end within the page cap.

    Returns (rows, complete); rows is None when the edge could not be read at all.
    A partly read edge is reported in missing, so it cannot support a claim that
    no blockers exist.
    """
    try:
        read = await _collect_edge(client, parent_id, edge, fields=fields, params=params,
                                   max_pages=_HEALTH_MAX_PAGES)
    except _SIGNAL_ERRORS as error:
        missing.append(f"{label} could not be read ({error}).")
        return None, False
    used.append(label)
    rows = [row for row in read["rows"] if isinstance(row, dict)]
    if not read["complete"]:
        missing.append(
            f"{label} was only partly read ({len(rows)} row(s) read; counts are lower bounds). {read['stop_reason']}"
        )
    return rows, read["complete"]


@mcp_server.tool()
@_masked
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

    diagnostics, _ = await _health_edge(client, catalog_id, "diagnostics", list(DIAGNOSTIC_SUMMARY_KEYS),
                                        {"affected_channels": ["da"], "limit": _HEALTH_DIAGNOSTIC_PAGE_SIZE},
                                        used, missing, "product_catalog/diagnostics")
    sources, sources_complete = await _health_edge(
        client, catalog_id, "external_event_sources", EVENT_SOURCE_FIELDS,
        {"limit": _SIGNAL_PAGE_SIZE}, used, missing, "product_catalog/external_event_sources",
    )

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
    if sources is not None and not sources and sources_complete:
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
            "event_source_count": None if sources is None or (not sources and not sources_complete) else len(sources),
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
                    "inconclusive": "No problem was found but at least one signal could not be read or was only partly read.",
                    "no_blockers_found": "Every signal was read and none showed a problem.",
                },
                "note": "Derived here from documented Meta signals; Meta has no single catalog-ads health endpoint.",
            },
        },
    )


@mcp_server.tool()
@_masked
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
@_masked
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
    _refuse_url_logins_in(name=name, params=params)
    payload = merge_graph_payload({"name": name, "vertical": vertical}, params)
    result = await get_graph_api_client().create_edge_object(
        business_id, "owned_product_catalogs", data=payload,
    )
    return creation_response(action="create_catalog", target={"business_id": business_id}, result=result)


@mcp_server.tool()
@_masked
async def update_catalog(
    catalog_id: str,
    name: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a product catalog's name or other documented settings (params), returning previous and current values. A changed setting Meta cannot read back is named in previous_unavailable."""
    catalog_id = _require(catalog_id, "catalog_id")
    _refuse_url_logins_in(name=name, params=params)
    payload = merge_graph_payload({"name": name if name is not None else OMIT}, params)
    if not payload:
        raise ValidationError("At least one field must be provided for update_catalog.")
    if "name" in payload and not blank_to_none(str(payload["name"])):
        raise ValidationError("name must not be blank.")
    extra_reads = list(dict.fromkeys(
        CATALOG_EXTRA_READABLE[key] for key in payload if key in CATALOG_EXTRA_READABLE
    ))
    client = get_graph_api_client()
    previous, refused = await _read_previous(client, catalog_id, CATALOG_UPDATE_READ_FIELDS, extra_reads)
    await client.update_object(catalog_id, data=payload)
    previous_values = {key: previous.get(key) for key in CATALOG_UPDATE_READ_FIELDS if key != "id"}
    for field_name in extra_reads:
        if field_name not in refused:
            previous_values[field_name] = previous.get(field_name)
    response = mutation_response(
        action="update_catalog",
        target={"catalog_id": catalog_id},
        previous=previous_values,
        current=_mask_secrets(payload),
    )
    unavailable: dict[str, str] = {}
    for key in sorted(payload):
        if key in CATALOG_UPDATE_READ_FIELDS:
            continue
        read_as = CATALOG_EXTRA_READABLE.get(key)
        if read_as is None:
            unavailable[key] = _UNREADABLE_PREVIOUS
        elif read_as in refused:
            unavailable[key] = _REFUSED_PREVIOUS.format(field=read_as)
    if unavailable:
        response["previous_unavailable"] = unavailable
    return response


PRODUCT_LIST_VIEWS = ("items", "product_groups")
# ProductGroup fields from the SDK at 26.0.2 (the edge is documented on the catalog).
PRODUCT_GROUP_FIELDS = ["id", "retailer_id", "variants"]
PRODUCT_ERROR_PRIORITIES = ("HIGH", "MEDIUM", "LOW")


def _error_type(value: str | None) -> str | None:
    """Normalize a catalog diagnostic error type; Meta's enum values are upper snake case."""
    if value is None:
        return None
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized):
        raise ValidationError(
            "error_type must be one of Meta's catalog diagnostic error types, for example IMAGE_RESOLUTION_LOW."
        )
    return normalized


@mcp_server.tool()
@_masked
async def list_catalog_products(
    catalog_id: str | None = None,
    product_set_id: str | None = None,
    product_id: str | None = None,
    filter: dict[str, Any] | str | None = None,
    limit: int = 50,
    after: str | None = None,
    fields: FieldList | None = None,
    error_type: str | None = None,
    error_priority: str | None = None,
    view: str = "items",
) -> dict[str, Any]:
    """List products in a product catalog or product set, optionally narrowed by a Meta filter object, or read one product. error_type (a diagnostic type such as IMAGE_RESOLUTION_LOW) or error_priority (HIGH, MEDIUM, LOW) lists only products hit by catalog diagnostics; Meta rejects some types, which does not mean no products are affected. view=product_groups lists a catalog's product groups (one row per variant group) instead of items. Prices are returned as Meta formats them."""
    scope_name, scope_value = _one_scope(
        catalog_id=catalog_id, product_set_id=product_set_id, product_id=product_id,
    )
    _check_limit(limit)
    parsed_filter = _parse_filter(filter)
    _refuse_url_logins(parsed_filter, "filter")
    normalized_error_type = _error_type(error_type)
    normalized_priority = None
    if error_priority is not None:
        normalized_priority = error_priority.strip().upper() if isinstance(error_priority, str) else ""
        if normalized_priority not in PRODUCT_ERROR_PRIORITIES:
            raise ValidationError(f"error_priority must be one of: {', '.join(PRODUCT_ERROR_PRIORITIES)}.")
    if view not in PRODUCT_LIST_VIEWS:
        raise ValidationError(f"view must be one of: {', '.join(PRODUCT_LIST_VIEWS)}.")
    diagnostic = normalized_error_type is not None or normalized_priority is not None
    if scope_name == "product_id" and parsed_filter is not None:
        raise ValidationError("filter applies to catalog_id or product_set_id, not product_id.")
    if scope_name == "product_id" and diagnostic:
        raise ValidationError(
            "error_type and error_priority apply to catalog_id or product_set_id; for one product, "
            "request the errors field instead."
        )
    if view == "product_groups":
        if scope_name != "catalog_id":
            raise ValidationError("view=product_groups needs catalog_id; Meta documents product groups on catalogs only.")
        if parsed_filter is not None or diagnostic:
            raise ValidationError(
                "view=product_groups takes no filter, error_type or error_priority; Meta documents no "
                "parameters on the product_groups edge."
            )
    client = get_graph_api_client()
    if scope_name == "product_id":
        requested = normalize_field_list(fields) or PRODUCT_FIELDS
        result = _single(await client.get_object(scope_value, fields=requested))
        result["summary"]["scope"] = {"product_id": scope_value}
        return result
    params = _page_params(limit, after)
    params["summary"] = "total_count"
    if view == "product_groups":
        requested = normalize_field_list(fields) or PRODUCT_GROUP_FIELDS
        payload = await client.list_objects(scope_value, "product_groups", fields=requested, params=params)
        result = _paged(payload, limit=limit, after=after)
        result["summary"]["scope"] = {scope_name: scope_value, "view": "product_groups"}
        return result
    requested = normalize_field_list(fields) or PRODUCT_FIELDS
    if parsed_filter is not None:
        params["filter"] = parsed_filter
    if normalized_error_type is not None:
        params["error_type"] = normalized_error_type
    if normalized_priority is not None:
        params["error_priority"] = normalized_priority
    payload = await client.list_objects(scope_value, "products", fields=requested, params=params)
    result = _paged(payload, limit=limit, after=after)
    scope: dict[str, Any] = {scope_name: scope_value}
    if normalized_error_type is not None:
        scope["error_type"] = normalized_error_type
    if normalized_priority is not None:
        scope["error_priority"] = normalized_priority
    result["summary"]["scope"] = scope
    return result


@mcp_server.tool()
@_masked
async def create_catalog_product(
    catalog_id: str,
    retailer_id: str,
    name: str,
    price: CatalogPrice,
    currency: str,
    url: str,
    image_url: str,
    availability: str = "in stock",
    description: str | None = None,
    brand: str | None = None,
    condition: str | None = None,
    sale_price: CatalogPrice | None = None,
    visibility: str = "staging",
    allow_upsert: bool = False,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one product in a product catalog. price and sale_price are major units (12.5 means 12.50). Created hidden (visibility=staging) until updated to published; fails if retailer_id exists unless allow_upsert."""
    catalog_id = _require(catalog_id, "catalog_id")
    _refuse_url_logins_in(
        retailer_id=retailer_id, name=name, url=url, image_url=image_url, description=description,
        brand=brand, params=params,
    )
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
@_masked
async def update_catalog_product(
    product_id: str,
    name: str | None = None,
    price: CatalogPrice | None = None,
    currency: str | None = None,
    sale_price: CatalogPrice | None = None,
    availability: str | None = None,
    condition: str | None = None,
    visibility: str | None = None,
    url: str | None = None,
    image_url: str | None = None,
    description: str | None = None,
    brand: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a catalog product's fields, returning previous and current values. price and sale_price are major units; previous prices are shown as Meta formats them. A changed field Meta cannot read back is named in previous_unavailable."""
    product_id = _require(product_id, "product_id")
    _refuse_url_logins_in(
        name=name, url=url, image_url=image_url, description=description, brand=brand, params=params,
    )
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
    readable_fields = PRODUCT_READABLE_UPDATE_FIELDS | PRODUCT_SDK_READABLE_UPDATE_FIELDS
    readable = [key for key in payload if key in readable_fields]
    client = get_graph_api_client()
    previous, refused = await _read_previous(
        client, product_id, PRODUCT_DETAIL_FIELDS, [key for key in readable if key not in PRODUCT_DETAIL_FIELDS],
    )
    await client.update_object(product_id, data=payload)
    response = mutation_response(
        action="update_catalog_product",
        target={"product_id": product_id},
        # Each field here was requested in the pre-read; Meta omits fields that hold no value.
        previous={key: previous.get(key) for key in readable if key not in refused},
        current=shown,
    )
    unavailable = {key: _UNREADABLE_PREVIOUS for key in sorted(payload) if key not in readable_fields}
    unavailable.update({key: _REFUSED_PREVIOUS.format(field=key) for key in refused})
    if unavailable:
        response["previous_unavailable"] = unavailable
    response["sent"] = payload
    return response


@mcp_server.tool()
@_masked
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


# ------------------------------------------------------------ items batch
#
# POST /{catalog_id}/items_batch, documented at
# developers.facebook.com/docs/marketing-api/reference/product-catalog/items_batch
# and the "send product updates" guide. Products are keyed by `id` (the
# retailer id), prices are "<amount> <ISO currency>", and Meta processes the
# batch in the background: completion is read from
# GET /{catalog_id}/check_batch_request_status?handle=..., where the only
# documented status value is "finished".

BATCH_MAX_ITEMS = 50
BATCH_METHODS = {"create": "CREATE", "update": "UPDATE", "delete": "DELETE"}
_BATCH_POLL_INTERVAL = 2
_BATCH_MAX_WAIT = 60
# Status requests one call may make across all of a batch's handles.
_BATCH_MAX_STATUS_CHECKS = 60
_BATCH_READ_PAGE_SIZE = 100
_BATCH_READ_MAX_PAGES = 2
_BATCH_ITEM_KEYS = frozenset({
    "method", "retailer_id", "name", "description", "url", "image_url", "brand", "availability",
    "condition", "price", "sale_price", "currency", "fields",
})
# Named item key -> PRODUCT_ITEM field in the items_batch request.
_BATCH_NAMED_FIELDS = {
    "name": "title", "description": "description", "url": "link", "image_url": "image",
    "brand": "brand", "availability": "availability", "condition": "condition",
    "price": "price", "sale_price": "sale_price",
}
# Request fields a caller must set through the item's named key, not through fields.
_FIELDS_WITH_NAMED_KEY = {
    **{batch: key for key, batch in _BATCH_NAMED_FIELDS.items()},
    "id": "retailer_id", "image_link": "image_url", "currency": "currency",
}
# The guide's CREATE requirement for PRODUCT_ITEM: availability, brand, condition,
# description, id, image, link, price, title.
_BATCH_CREATE_REQUIRED = ("name", "description", "url", "image_url", "brand", "availability",
                          "condition", "price", "currency")
# Further PRODUCT_ITEM fields listed in the "send product updates" guide, passed
# through `fields` unchanged. Fields with a named key above are not repeated here.
BATCH_EXTRA_FIELDS = frozenset({
    "rich_text_description", "mobile_link", "additional_image_link", "video",
    "quantity_to_sell_on_facebook", "sale_price_effective_date", "color", "size", "material",
    "pattern", "gender", "age_group", "mpn", "gtin", "item_group_id", "additional_variant_attribute",
    "ordering_index", "origin_country", "expiration_date", "fb_product_category",
    "google_product_category", "product_type", "custom_label_0", "custom_label_1",
    "custom_label_2", "custom_label_3", "custom_label_4", "custom_number_0", "custom_number_1",
    "custom_number_2", "custom_number_3", "custom_number_4", "internal_label", "shipping",
    "shipping_weight", "applink", "disabled_capabilities", "importer_address", "importer_name",
})
# Batch request field -> readable product item field, for showing previous values.
_BATCH_READ_FIELD = {
    "title": "name", "description": "description", "link": "url", "image": "image_url",
    "brand": "brand", "availability": "availability", "condition": "condition", "price": "price",
    "sale_price": "sale_price", "mobile_link": "mobile_link", "additional_image_link": "additional_image_urls",
    "color": "color", "size": "size", "material": "material", "pattern": "pattern", "gender": "gender",
    "age_group": "age_group", "gtin": "gtin", "mpn": "manufacturer_part_number",
    "item_group_id": "retailer_product_group_id", "ordering_index": "ordering_index",
    "origin_country": "origin_country", "expiration_date": "expiration_date",
    "fb_product_category": "fb_product_category", "product_type": "product_type",
    "custom_label_0": "custom_label_0", "custom_label_1": "custom_label_1",
    "custom_label_2": "custom_label_2", "custom_label_3": "custom_label_3",
    "custom_label_4": "custom_label_4", "importer_address": "importer_address",
    "importer_name": "importer_name",
}
# Batch fields whose readable product item field rests on SDK 26.0.2 alone
# (PRODUCT_SDK_READABLE_UPDATE_FIELDS); Meta refusing to read them leaves only
# their previous value unavailable.
_BATCH_SDK_READ_FIELD = {name: name for name in PRODUCT_SDK_READABLE_UPDATE_FIELDS}
_BATCH_READ_FIELD.update(_BATCH_SDK_READ_FIELD)
_BATCH_BASE_READ_FIELDS = [
    "id", "retailer_id", "name", "description", "price", "currency", "sale_price", "availability",
    "condition", "url", "image_url", "brand", "visibility", "review_status",
]
_FEED_FIELD = "product_feed"


@functools.cache
def _known_keys() -> frozenset[str]:
    """Field names this module defines; only these may be named in a message about a nested key.

    Any other key a caller sends is described by its position, since a misplaced value
    (an email address, a URL with a login) can arrive as a key.
    """
    return frozenset({
        *CATALOG_FIELDS, *CATALOG_UPDATE_READ_FIELDS, *CATALOG_EXTRA_READABLE, *_SECRET_SETTING_KEYS,
        "partner_integration", *PRODUCT_DETAIL_FIELDS, *PRODUCT_READABLE_UPDATE_FIELDS,
        *PRODUCT_SDK_READABLE_UPDATE_FIELDS, *PRODUCT_SET_FIELDS, *FILTER_OPERATORS, "and", "or",
        "field", "operator", "value", *_BATCH_ITEM_KEYS, *_BATCH_NAMED_FIELDS.values(), *BATCH_EXTRA_FIELDS,
    })


_BATCH_RETRY_WHY = (
    "This tool reads the catalog before every write, so on a resend creates of SKUs that already exist "
    "and deletes of SKUs that are already gone are skipped, and updates send the same values again. "
    "Meta keys catalog products by retailer id (its batch documentation calls it a unique content ID), "
    "so the catalog holds one product per retailer id and a resend cannot add a duplicate. A resend "
    "does overwrite any change someone else or a feed made to the same fields in between."
)


async def _pause(seconds: float) -> None:
    """Wait between batch status checks; replaced in tests."""
    await asyncio.sleep(seconds)


def _batch_price(value: Any, currency: str, label: str) -> str:
    """Encode a major-unit price as items_batch documents it: '9.99 USD', '14 GBP'."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValidationError(f"{label} must be a finite number in major units.")
    minor = _catalog_price(value, label)
    whole, cents = divmod(minor, 100)
    amount = str(whole) if cents == 0 else f"{whole}.{cents:02d}"
    return f"{amount} {currency}"


def _batch_entry(index: int, item: Any) -> dict[str, Any]:
    """Validate one caller item and build its items_batch request and caller-facing view."""
    label = f"items[{index}]"
    if not isinstance(item, dict):
        raise ValidationError(f"{label} must be an object with method and retailer_id.")
    # Every value, including image, video, additional image and app links nested
    # in fields, is checked before the catalog is read.
    _refuse_url_logins(item, label)
    # Unknown keys are counted, never named: a misplaced value can arrive as a key.
    unknown = [key for key in item if key not in _BATCH_ITEM_KEYS]
    if unknown:
        raise ValidationError(
            f"{label} has {_count_keys(len(unknown), 'unsupported key')} (not repeated here). Named keys: "
            f"{', '.join(sorted(_BATCH_ITEM_KEYS))}; other documented product fields go in fields."
        )
    method = str(item.get("method") or "").strip().lower()
    if method not in BATCH_METHODS:
        raise ValidationError(f"{label}.method must be create, update or delete.")
    retailer_id = item.get("retailer_id")
    if not isinstance(retailer_id, str) or not retailer_id.strip():
        raise ValidationError(f"{label}.retailer_id must be a nonblank string (the product's SKU).")
    retailer_id = retailer_id.strip()
    values = {key: value for key, value in item.items() if key not in {"method", "retailer_id"} and value is not None}
    extra = values.pop("fields", None) or {}
    if not isinstance(extra, dict):
        raise ValidationError(f"{label}.fields must be an object of documented product fields.")
    if method == "delete":
        if values or extra:
            raise ValidationError(f"{label}: a delete takes only retailer_id.")
        return {"method": method, "retailer_id": retailer_id,
                "request": {"method": "DELETE", "data": {"id": retailer_id}}, "changes": {}}
    # A key is named only when it is one of this module's own field names; any other key
    # is counted, since a misplaced value can arrive as a key.
    named_here = [key for key in extra if key in _FIELDS_WITH_NAMED_KEY]
    if named_here:
        listed = ", ".join(f"{key} (use {_FIELDS_WITH_NAMED_KEY[key]})" for key in named_here)
        raise ValidationError(f"{label}.fields: use the item's named key instead for {listed}.")
    unknown_fields = [key for key in extra if key not in BATCH_EXTRA_FIELDS]
    if unknown_fields:
        raise ValidationError(
            f"{label}.fields has {_count_keys(len(unknown_fields), 'key')} that Meta's batch documentation does "
            f"not list as a PRODUCT_ITEM field (not repeated here). Accepted fields: "
            f"{', '.join(sorted(BATCH_EXTRA_FIELDS))}."
        )
    if method == "create":
        missing = [key for key in _BATCH_CREATE_REQUIRED if values.get(key) in (None, "")]
        if missing:
            raise ValidationError(
                f"{label}: a create is missing {', '.join(missing)} (Meta requires these for a new product)."
            )
    if not values and not extra:
        raise ValidationError(f"{label}: an update needs at least one field to change.")
    data: dict[str, Any] = {"id": retailer_id}
    changes: dict[str, Any] = {}
    currency = _currency(values["currency"]) if "currency" in values else None
    if currency is not None and not any(key in values for key in ("price", "sale_price")):
        raise ValidationError(f"{label}.currency is sent only with price or sale_price.")
    for key, batch_field in _BATCH_NAMED_FIELDS.items():
        if key not in values:
            continue
        value = values[key]
        if key in {"price", "sale_price"}:
            if currency is None:
                raise ValidationError(f"{label}.{key} needs currency in the same item.")
            data[batch_field] = _batch_price(value, currency, f"{label}.{key}")
        elif key == "image_url":
            data[batch_field] = [{"url": _require(value, f"{label}.image_url")}]
        else:
            if key == "availability":
                _choice(value, PRODUCT_AVAILABILITY, f"{label}.availability")
            elif key == "condition":
                _choice(value, PRODUCT_CONDITIONS, f"{label}.condition")
            elif not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{label}.{key} must be a nonblank string.")
            data[batch_field] = value
        changes[key] = value
    if currency is not None:
        changes["currency"] = currency
    for key, value in extra.items():
        data[key] = value
        changes[key] = value
    return {"method": method, "retailer_id": retailer_id,
            "request": {"method": BATCH_METHODS[method], "data": data}, "changes": changes}


def _batch_read_fields(entries: list[dict[str, Any]], *, with_feed: bool, with_sdk_only: bool = True) -> list[str]:
    fields = list(_BATCH_BASE_READ_FIELDS)
    for entry in entries:
        for key in entry["request"]["data"]:
            read = _BATCH_READ_FIELD.get(key)
            if not with_sdk_only and key in _BATCH_SDK_READ_FIELD:
                continue
            if read and read not in fields:
                fields.append(read)
    if with_feed:
        fields.append(_FEED_FIELD)
    return fields


async def _read_batch_products(client, catalog_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Read the current products for every retailer id in one filtered request.

    If Meta rejects the read as an invalid parameter, it is repeated once without
    product_feed and without fields whose readability rests on the SDK alone;
    the feed check is reported as unavailable and those fields are returned in
    refused_fields, so their previous values are reported as unreadable.
    """
    ids = [entry["retailer_id"] for entry in entries]
    params = {"filter": {"retailer_id": {"is_any": ids}}, "limit": _BATCH_READ_PAGE_SIZE}
    feed_check = {"status": "read", "method": "product_feed field on each product"}
    first_fields = _batch_read_fields(entries, with_feed=True)
    refused: list[str] = []
    try:
        read = await _collect_edge(client, catalog_id, "products", fields=first_fields,
                                   params=params, max_pages=_BATCH_READ_MAX_PAGES)
    except MetaApiError as error:
        if error.code != 100:
            raise
        fallback = _batch_read_fields(entries, with_feed=False, with_sdk_only=False)
        refused = [name for name in first_fields if name not in fallback and name != _FEED_FIELD]
        read = await _collect_edge(client, catalog_id, "products", fields=fallback,
                                   params=params, max_pages=_BATCH_READ_MAX_PAGES)
        feed_check = {"status": "unavailable", "method": "product_feed field on each product",
                      "error": str(error)}
    wanted = set(ids)
    found = {
        str(row.get("retailer_id")): row for row in read["rows"]
        if isinstance(row, dict) and str(row.get("retailer_id")) in wanted
    }
    return {"found": found, "complete": read["complete"], "stop_reason": read["stop_reason"],
            "feed_check": feed_check, "refused_fields": refused}


def _previous_for(
    entry: dict[str, Any], product: dict[str, Any], refused: frozenset[str] | set[str] = frozenset(),
) -> tuple[dict[str, Any], list[str]]:
    """Return (previous values keyed as the caller wrote them, keys that cannot be read back)."""
    if entry["method"] == "delete":
        return {key: value for key, value in product.items() if key != _FEED_FIELD}, []
    previous: dict[str, Any] = {}
    unavailable: list[str] = []
    named = {batch: key for key, batch in _BATCH_NAMED_FIELDS.items()}
    for batch_field in entry["request"]["data"]:
        if batch_field == "id":
            continue
        key = named.get(batch_field, batch_field)
        read = _BATCH_READ_FIELD.get(batch_field)
        if read is None or read in refused:
            unavailable.append(key)
            continue
        previous[key] = product.get(read)
        if batch_field in {"price", "sale_price"}:
            previous["currency"] = product.get("currency")
    return previous, unavailable


def _feed_warning(entry: dict[str, Any], product: dict[str, Any]) -> str | None:
    feed = product.get(_FEED_FIELD)
    feed_id = feed.get("id") if isinstance(feed, dict) else feed
    if not feed_id:
        return None
    if entry["method"] == "delete":
        return (f"This product is linked to product feed {feed_id}. The feed's next fetch or upload may add it "
                "back unless it is removed from the feed too.")
    return (f"This product is linked to product feed {feed_id}. The feed's next fetch or upload may overwrite "
            "this change unless the feed is changed too.")


_NO_MESSAGE_ERROR = "Meta reported an error for this item without a message."
_NO_MESSAGE_WARNING = "Meta reported a warning for this item without a message."


def _entry_list(entries: Any) -> list[Any]:
    """Return Meta's error or warning entries as a list; an empty value means none.

    A null inside a list is left out here; _null_entries counts it, because Meta listed
    something there that cannot be read.
    """
    if entries is None or entries == "" or entries == [] or entries == {}:
        return []
    if isinstance(entries, list):
        return [entry for entry in entries if entry is not None]
    return [entries]


def _null_entries(entries: Any) -> int:
    """How many entries of a listed value are null: listed by Meta, but unreadable."""
    return sum(1 for entry in entries if entry is None) if isinstance(entries, list) else 0


def _entry_text(entry: Any, generic: str) -> str:
    """Return one entry's message, or a generic one when Meta gave none.

    An entry with no message still counts: it is replaced by generic text, with
    its code when it has one, and never dropped.
    """
    message = entry.get("message") if isinstance(entry, dict) else entry
    if isinstance(message, str) and message.strip():
        return message
    if message is not None and not isinstance(message, (str, dict, list)):
        return str(message)
    code = entry.get("code") if isinstance(entry, dict) else None
    if code is not None and not isinstance(code, (dict, list, bool)):
        return f"{generic[:-1]} (code {code})."
    return generic


def _messages(entries: Any, generic: str = _NO_MESSAGE_ERROR) -> list[str]:
    """One text per entry Meta listed, so the number of texts is the number of entries."""
    return [_entry_text(entry, generic) for entry in _entry_list(entries)]


def _by_id(entries: Any) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for entry in _entry_list(entries):
        if isinstance(entry, dict) and entry.get("id") is not None:
            grouped.setdefault(str(entry["id"]), []).append(_entry_text(entry, _NO_MESSAGE_WARNING))
    return grouped


# The Graph client's wrappers for an answer that was not a JSON object: a bare null, text or
# number ({"json_response": ...}) or a body that was not JSON ({"text_response": ...}).
_UNREADABLE_ANSWER_KEYS = ("json_response", "text_response")


def _answer_unreadable(payload: Any) -> bool:
    """Whether Meta's answer cannot be read as an object at all."""
    return not isinstance(payload, dict) or any(key in payload for key in _UNREADABLE_ANSWER_KEYS)


def _batch_status_row(payload: Any) -> dict[str, Any] | None:
    """The status row in Meta's answer to a status check, or None when it cannot be read.

    The documented answer is a data list whose first entry is the batch's status; an object
    without a data list is read as that entry itself. Anything else (not an object, one of
    the client's wrappers for a non-object answer, or a data list with no object in front)
    holds no status this tool can read.
    """
    if _answer_unreadable(payload):
        return None
    rows = payload.get("data")
    if rows is None:
        return payload
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def _is_finished(row: dict[str, Any] | None) -> bool:
    return bool(row) and str(row.get("status") or "").strip().lower() == "finished"


async def _check_batch(client, catalog_id: str, handle: str) -> dict[str, Any] | None:
    payload = await client.list_objects(
        catalog_id, "check_batch_request_status",
        params={"handle": handle, "load_ids_of_invalid_requests": True},
    )
    return _batch_status_row(payload)


# A batch error entry names its item by the request's id (the retailer id).
_identified = _row_identity


def _batch_attribution(row: dict[str, Any]) -> dict[str, Any]:
    """Read a finished batch status once, with one set of rules for every caller.

    failed_ids are the items Meta names as failed (ids_of_invalid_requests, or an
    error entry carrying the item's id). complete is true only when every error
    in the batch is known to belong to a named item, so an item not named can be
    called applied: Meta gave an explicit zero error count, or Meta listed the
    invalid requests by id, or every error counted is listed with an item id.
    An error entry that names no item, an invalid-request entry that cannot be read
    as an id (null, blank, an object), or a missing error count, leaves every item
    not named unknown; reason says which.
    """
    raw_invalid = row.get("ids_of_invalid_requests")
    raw_invalid = _entry_list(raw_invalid) + [None] * _null_entries(raw_invalid)
    invalid: set[str] = set()
    unreadable_invalid = 0
    for value in raw_invalid:
        # Meta documents these as the requests' ids; a single id given without a list is
        # read as one id, never character by character.
        text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        if text:
            invalid.add(text)
        else:
            unreadable_invalid += 1
    listed = row.get("errors") or []
    if not isinstance(listed, list):
        listed = [listed]
    named = [entry for entry in listed if _identified(entry) is not None]
    unnamed = len(listed) - len(named)
    error_total = to_int(row.get("errors_total_count"))
    failed_ids = invalid | {_identified(entry) for entry in named}
    reason: str | None = None
    if error_total is None:
        complete = False
        reason = ("Meta did not report how many errors the batch had"
                  + (", and listed errors" if listed else "")
                  + ", so items it did not name cannot be called applied.")
    elif unreadable_invalid:
        complete = False
        reason = (f"Meta listed {unreadable_invalid} invalid request(s) whose id could not be read, so any "
                  "item it did not name may be among them.")
    elif unnamed:
        complete = False
        reason = (f"Meta listed {unnamed} error(s) that name no item, so any item it did not name "
                  "may be among them.")
    elif error_total == 0:
        complete = True
    elif invalid or len(named) >= error_total:
        complete = True
    else:
        complete = False
        reason = (f"Meta reported {error_total} error(s) for the batch but named the item for only "
                  f"{len(named)}, so any item it did not name may be among them.")
    errors_by_id: dict[str, list[str]] = {}
    for entry in named:
        message = entry.get("message")
        errors_by_id.setdefault(_identified(entry), []).append(
            str(message) if message else "Meta reported an error for this item."
        )
    return {"failed_ids": failed_ids, "errors_by_id": errors_by_id, "complete": complete,
            "reason": reason, "errors_total_count": error_total, "unattributed_errors": unnamed,
            "unreadable_invalid_ids": unreadable_invalid}


_UNFINISHED_REASON = (
    "Meta accepted the batch; it had not reported it finished when this tool stopped checking."
)
_STATUS_UNREADABLE = (
    "Meta's answer to the status check could not be read as a batch status, so whether the batch "
    "has finished is not known; it was not checked again."
)


def _settle(
    item: dict[str, Any],
    rows: dict[str, Any] | list[dict[str, Any]],
    *,
    unfinished_reason: str | None = None,
    unattributed_submission_errors: int = 0,
    unreadable_submission_entries: int = 0,
) -> None:
    """Set a sent item's outcome from the status of every handle of its batch.

    rows are the finished status rows; unfinished_reason is given when at least
    one handle has not been reported finished. Meta does not say which handle
    holds which item, so an item is applied only when every handle is finished
    and every error in every handle is attributed to a named item. An item Meta
    named as failed in any handle is failed. An item flagged when the batch was
    submitted (validation_status), even by an error without a message or by an
    error entry that cannot be read, is never called applied; nor is any item
    when a submission error named no item, or when the submission answer held an
    entry that cannot be read and so cannot be tied to an item
    (unreadable_submission_entries).
    """
    rows = [rows] if isinstance(rows, dict) else list(rows)
    attributions = [_batch_attribution(row) for row in rows]
    retailer_id = item["retailer_id"]
    failed_errors: list[str] = []
    named_failed = False
    for row, attribution in zip(rows, attributions):
        item["warnings"] = [*item["warnings"], *_by_id(row.get("warnings")).get(retailer_id, [])]
        if retailer_id in attribution["failed_ids"]:
            named_failed = True
            failed_errors.extend(attribution["errors_by_id"].get(retailer_id, []))
    if named_failed:
        item["outcome"] = "failed"
        item["errors"] = [*item["errors"], *failed_errors] or ["Meta listed this item as invalid."]
    elif unfinished_reason is not None:
        item["outcome"] = "processing"
        item["reason"] = unfinished_reason
    elif item["errors"] or item.get("submission_error_count"):
        # Meta's answer to the submission already listed errors for this item
        # (validation_status), yet the finished batch does not name it as failed.
        # The two disagree, so the item is not called applied.
        item["outcome"] = "outcome_unknown"
        item["reason"] = (
            "Meta listed errors for this item when the batch was submitted, but the finished batch "
            "did not name it as failed. Read the product before resending."
        )
    elif item.get("submission_unreadable_error_count"):
        item["outcome"] = "outcome_unknown"
        item["reason"] = (
            f"Meta listed {item['submission_unreadable_error_count']} error entr"
            f"{'y' if item['submission_unreadable_error_count'] == 1 else 'ies'} for this item when the batch "
            "was submitted that could not be read, so whether it was applied is unknown. Read the product "
            "before resending."
        )
    elif unattributed_submission_errors:
        item["outcome"] = "outcome_unknown"
        item["reason"] = (
            f"Meta listed {unattributed_submission_errors} error(s) when the batch was submitted that name no "
            "item in it, so this item may be among them. Read the product before resending."
        )
    elif unreadable_submission_entries:
        item["outcome"] = "outcome_unknown"
        item["reason"] = _unreadable_submission_reason(unreadable_submission_entries)
    elif attributions and all(attribution["complete"] for attribution in attributions):
        item["outcome"] = "applied"
    else:
        item["outcome"] = "outcome_unknown"
        item["reason"] = next(
            (attribution["reason"] for attribution in attributions if not attribution["complete"]),
            "No finished batch status was read, so the outcome is unknown.",
        )


def _unreadable_submission_reason(unreadable: int) -> str:
    return (
        f"Meta's answer to the submission held {unreadable} entr{'y' if unreadable == 1 else 'ies'} that could "
        "not be read (an entry that is not an object, or a null error), so it cannot be tied to an item and this "
        "item may be among them. Read the product before resending."
    )


def _unreadable_handles_reason(unreadable: int, returned: int) -> str:
    return (
        f"Meta returned {returned} batch handle entr{'y' if returned == 1 else 'ies'} and {unreadable} of them "
        "could not be read as a handle, so the part of the batch they stand for could not be checked and no "
        "item can be called applied. Read the products before resending."
    )


def _retry_statement(outcomes: set[str], handles: list[str] | str | None) -> dict[str, Any]:
    if isinstance(handles, str):
        handles = [handles]
    handles = list(handles or [])
    if "processing" in outcomes:
        if len(handles) == 1:
            read = f"Call this tool again with catalog_id and handle={handles[0]!r} to read the outcome"
        else:
            listed = ", ".join(repr(value) for value in handles)
            read = (f"Meta returned several handles for this batch and these are not known to be finished: "
                    f"{listed}. Call this tool again with catalog_id and each of these as handle, one call "
                    "per handle, to read the outcome")
        return {
            "safe_now": False,
            "how": (f"Do not resend yet: Meta accepted the batch and is still processing it. {read}, then "
                    "resend only the items reported failed."),
            "why": _BATCH_RETRY_WHY,
        }
    if "outcome_unknown" in outcomes:
        return {
            "safe_now": True,
            "how": ("Wait a few minutes, then resend the same items with this tool; if the timeout repeats, "
                    "split them into batches of about half the size. A batch still being processed when "
                    "you resend is not visible to the re-read, so a create in it may then be reported "
                    "by Meta as an error for that item."),
            "why": _BATCH_RETRY_WHY,
        }
    if outcomes & {"failed", "not_applied"}:
        return {
            "safe_now": True,
            "how": "Fix the reported errors and resend only the items that failed or were not applied.",
            "why": _BATCH_RETRY_WHY,
        }
    return {"safe_now": True, "how": "Nothing needs resending.", "why": _BATCH_RETRY_WHY}


async def _batch_status_only(client, catalog_id: str, handle: str) -> dict[str, Any]:
    row = await _check_batch(client, catalog_id, handle)
    if row is None:
        return {
            "ok": False,
            "outcome": "status_unreadable",
            "action": "batch_update_catalog_products",
            "target": {"catalog_id": catalog_id},
            "batch": {"handle": handle, "status": None, "complete": False, "status_checks": 1,
                      "status_error": _STATUS_UNREADABLE},
            "note": ("Nothing is known about the batch's outcome from this answer. Call again with the same "
                     "handle to read it; do not resend the items until the batch is reported finished."),
        }
    finished = _is_finished(row)
    # A single object given without a list is one entry, not a list of its keys.
    errors = [entry for entry in _entry_list(row.get("errors")) if isinstance(entry, dict)]
    warnings = [entry for entry in _entry_list(row.get("warnings")) if isinstance(entry, dict)]
    attribution = _batch_attribution(row)
    failed = sorted(attribution["failed_ids"])
    if not finished:
        outcome = "processing"
    elif failed:
        outcome = "some_failed" if attribution["complete"] else "some_failed_others_unknown"
    else:
        outcome = "all_applied" if attribution["complete"] else "outcome_unknown"
    attribution_view: dict[str, Any] = {
        "complete": attribution["complete"] if finished else None,
        "unattributed_errors": attribution["unattributed_errors"],
    }
    if attribution["unreadable_invalid_ids"]:
        attribution_view["unreadable_invalid_ids"] = attribution["unreadable_invalid_ids"]
    if finished and attribution["reason"]:
        attribution_view["reason"] = attribution["reason"]
    return {
        "ok": outcome == "all_applied",
        "outcome": outcome,
        "error_attribution": attribution_view,
        "action": "batch_update_catalog_products",
        "target": {"catalog_id": catalog_id},
        "batch": {
            "handle": handle,
            "status": row.get("status"),
            "complete": finished,
            "status_checks": 1,
            "errors_total_count": to_int(row.get("errors_total_count")),
            "warnings_total_count": to_int(row.get("warnings_total_count")),
        },
        "failed_retailer_ids": failed[:100],
        "errors": errors[:50],
        "warnings": warnings[:50],
        "note": ("Meta documents only the status value 'finished'; any other value means the batch is not "
                 "known to be done."),
    }


async def _poll_handles(
    client, catalog_id: str, handles: list[str], wait_seconds: int,
) -> dict[str, dict[str, Any]]:
    """Check every batch handle until each is finished, its check fails, or the wait or request cap ends.

    Each round waits once, then checks every handle not yet finished; a handle
    whose check fails, or whose answer holds no readable status, is not checked again
    and keeps the reason as its error.
    """
    tracks = {handle: {"row": None, "checks": 0, "error": None} for handle in handles}
    requests = 0
    capped = False
    for _ in range(wait_seconds // _BATCH_POLL_INTERVAL):
        pending = [handle for handle, track in tracks.items()
                   if not _is_finished(track["row"]) and track["error"] is None]
        if not pending:
            break
        if requests >= _BATCH_MAX_STATUS_CHECKS:
            capped = True
            break
        await _pause(_BATCH_POLL_INTERVAL)
        for handle in pending:
            if requests >= _BATCH_MAX_STATUS_CHECKS:
                capped = True
                break
            requests += 1
            try:
                row = await _check_batch(client, catalog_id, handle)
            except (MetaApiError, RateLimitError, UnsupportedFeatureError, NotFoundError) as error:
                tracks[handle]["error"] = str(error)
                continue
            except Exception as error:
                # The batch has been submitted: an unexpected failure while reading its status
                # must not lose the handle and the items' outcomes. CancelledError is not an
                # Exception, so a cancelled call still stops.
                tracks[handle]["error"] = (
                    f"The status check failed in this server ({type(error).__name__}), so whether the batch "
                    "has finished is not known; it was not checked again."
                )
                continue
            tracks[handle]["checks"] += 1
            if row is None:
                tracks[handle]["error"] = _STATUS_UNREADABLE
                continue
            tracks[handle]["row"] = row
    if capped:
        for track in tracks.values():
            if not _is_finished(track["row"]) and track["error"] is None:
                track["capped"] = True
    return tracks


def _batch_view(handles: list[str], tracks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Describe every handle Meta returned, and the batch as a whole."""
    entries: list[dict[str, Any]] = []
    for handle in handles:
        track = tracks.get(handle) or {"row": None, "checks": 0, "error": None}
        row = track["row"] or {}
        entry: dict[str, Any] = {
            "handle": handle,
            "status": row.get("status"),
            "complete": _is_finished(track["row"]),
            "status_checks": track["checks"],
            "errors_total_count": to_int(row.get("errors_total_count")),
            "warnings_total_count": to_int(row.get("warnings_total_count")),
        }
        if track["error"] is not None:
            entry["status_error"] = track["error"]
        if track.get("capped"):
            entry["status_note"] = f"Not checked again: this call's cap of {_BATCH_MAX_STATUS_CHECKS} status requests was reached."
        entries.append(entry)

    def total(key: str) -> int | None:
        values = [entry[key] for entry in entries]
        return None if any(value is None for value in values) else sum(values)

    view: dict[str, Any] = {
        "handle": handles[0] if len(handles) == 1 else None,
        "status": entries[0]["status"] if len(entries) == 1 else None,
        "complete": all(entry["complete"] for entry in entries),
        "status_checks": sum(entry["status_checks"] for entry in entries),
        "errors_total_count": total("errors_total_count"),
        "warnings_total_count": total("warnings_total_count"),
        "handles": entries,
    }
    if len(entries) == 1 and "status_error" in entries[0]:
        view["status_error"] = entries[0]["status_error"]
    if len(entries) > 1:
        view["note"] = (
            f"Meta returned {len(entries)} handles for this batch. Each was checked; Meta does not say which "
            "items each holds, so an item is called applied only when every handle is finished with every "
            "error attributed to a named item."
        )
    return view


@mcp_server.tool()
@_masked
async def batch_update_catalog_products(
    catalog_id: GraphId,
    items: list[dict[str, Any]] | None = None,
    allow_upsert: bool = False,
    dry_run: bool = False,
    wait_seconds: int = 10,
    handle: str | None = None,
) -> dict[str, Any]:
    """Create, update or delete up to 50 catalog products by retailer_id (SKU) in one call. Item: method (create, update, delete), retailer_id, then name, description, url, image_url, brand, availability, condition, price, sale_price, currency (major units) or fields. Reads products first and skips creates of existing SKUs and deletes of missing ones, so resending is safe. Meta applies batches in the background: each item is reported applied, failed, processing or unknown, with a feed warning where relevant. handle alone reads an earlier batch. dry_run writes nothing. Created products are not held in staging."""
    if not isinstance(wait_seconds, int) or isinstance(wait_seconds, bool) or not 0 <= wait_seconds <= _BATCH_MAX_WAIT:
        raise ValidationError(f"wait_seconds must be a whole number from 0 to {_BATCH_MAX_WAIT}.")
    handle = blank_to_none(handle) if isinstance(handle, str) else None
    if (items is None) == (handle is None):
        raise ValidationError("Pass either items or handle: items to send a batch, handle to read an earlier one.")
    if handle is not None:
        return await _batch_status_only(get_graph_api_client(), catalog_id, handle)
    if not isinstance(items, list) or not 1 <= len(items) <= BATCH_MAX_ITEMS:
        raise ValidationError(f"items must hold 1 to {BATCH_MAX_ITEMS} products per call; split larger edits.")
    entries = [_batch_entry(index, item) for index, item in enumerate(items)]
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if entry["retailer_id"] in seen:
            # Positions, not the value: a retailer_id can be a misplaced personal value.
            raise ValidationError(
                f"items[{seen[entry['retailer_id']]}] and items[{index}] have the same retailer_id, so it "
                "appears more than once; send one item per SKU."
            )
        seen[entry["retailer_id"]] = index

    client = get_graph_api_client()
    current = await _read_batch_products(client, catalog_id, entries)
    if not current["complete"]:
        return {
            "ok": False,
            "action": "batch_update_catalog_products",
            "target": {"catalog_id": catalog_id},
            "sent": False,
            "reason": ("The current products could not be read completely, so nothing was written. "
                       f"{current['stop_reason']}"),
        }

    results: list[dict[str, Any]] = []
    to_send: list[dict[str, Any]] = []
    for entry in entries:
        product = current["found"].get(entry["retailer_id"])
        item: dict[str, Any] = {
            "retailer_id": entry["retailer_id"],
            "method": entry["method"],
            "outcome": None,
            "changes": entry["changes"],
            "errors": [],
            "warnings": [],
        }
        if product is not None:
            item["product_id"] = str(product.get("id")) if product.get("id") is not None else None
        if entry["method"] == "create" and product is not None:
            item["outcome"] = "skipped_already_exists"
            item["reason"] = "A product with this retailer_id is already in the catalog; use method update to change it."
        elif entry["method"] == "delete" and product is None:
            item["outcome"] = "skipped_not_found"
            item["reason"] = "No product with this retailer_id is in the catalog, so there is nothing to delete."
        elif entry["method"] == "update" and product is None and not allow_upsert:
            item["outcome"] = "skipped_not_found"
            item["reason"] = ("No product with this retailer_id is in the catalog; pass allow_upsert=true to "
                              "create it from this update.")
        else:
            if product is not None:
                previous, unavailable = _previous_for(entry, product, set(current["refused_fields"]))
                item["previous"] = previous
                if unavailable:
                    item["previous_unavailable"] = unavailable
                if warning := _feed_warning(entry, product):
                    item["feed_warning"] = warning
            to_send.append(item)
            item["request"] = entry["request"]
        results.append(item)

    request_data = {
        "item_type": "PRODUCT_ITEM",
        "allow_upsert": allow_upsert,
        "requests": [item["request"] for item in to_send],
    }
    response: dict[str, Any] = {
        "action": "batch_update_catalog_products",
        "target": {"catalog_id": catalog_id},
        "dry_run": dry_run,
        "items": results,
        "feed_check": {
            **current["feed_check"],
            "note": ("A feed_warning appears when Meta reports a product_feed on the product. Meta documents "
                     "that field without describing it, so its absence is not a guarantee that no feed, "
                     "partner integration or other source will overwrite the change."),
        },
    }
    notes: list[str] = []
    if current["refused_fields"]:
        notes.append("Meta refused the first read of the current products, so it was repeated without "
                     f"{', '.join(current['refused_fields'])}; their previous values are listed as unavailable.")
    if any(entry["method"] == "create" for entry in entries):
        notes.append("Meta's batch documentation lists no visibility field for products, so products created "
                     "here are not held in staging; use create_catalog_product for one that must start hidden.")
    handles: list[str] = []
    tracks: dict[str, dict[str, Any]] = {}
    unreadable_handles = 0

    if dry_run:
        for item in to_send:
            item["outcome"] = "would_send"
        response["request"] = {"endpoint": f"POST /{catalog_id}/items_batch", "data": request_data}
    elif to_send:
        try:
            posted = await client.create_edge_object(catalog_id, "items_batch", data=request_data)
        except (MetaApiError, RateLimitError) as error:
            unknown = isinstance(error, MetaApiError) and error.mutation_outcome_unknown
            for item in to_send:
                item["outcome"] = "outcome_unknown" if unknown else "not_applied"
                item["reason"] = ("The request failed without a response, so Meta may have applied it."
                                  if unknown else "Meta refused the whole request, so nothing was ingested.")
            response["error"] = error.to_public_dict()
        else:
            try:
                # The write has gone out. An answer that is not an object (including the Graph
                # client's json_response and text_response wrappers for a bare null, text, number
                # or non-JSON body) says nothing about whether Meta ingested it, so it is never
                # read as "no handle, nothing ingested".
                response_unreadable = _answer_unreadable(posted)
                if response_unreadable:
                    posted = {}
                sent_ids = {item["retailer_id"] for item in to_send}
                flagged: dict[str, list[dict[str, Any]]] = {}
                unattributed: list[str] = []
                # Evidence that cannot be read is counted, never skipped: a validation_status entry
                # that is not an object (null, text, a number), and a null error inside an entry
                # that names no item in this batch. Neither can be tied to an item.
                validation_status = posted.get("validation_status")
                unreadable_entries = _null_entries(validation_status)
                unreadable_unattributed_errors = 0
                for entry in _entry_list(validation_status):
                    if not isinstance(entry, dict):
                        unreadable_entries += 1
                        continue
                    raw_id = entry.get("retailer_id")
                    entry_id = str(raw_id).strip() if raw_id is not None else ""
                    if entry_id in sent_ids:
                        flagged.setdefault(entry_id, []).append(entry)
                    else:
                        unattributed.extend(_messages(entry.get("errors")))
                        unreadable_unattributed_errors += _null_entries(entry.get("errors"))
                for item in to_send:
                    statuses = flagged.get(item["retailer_id"], [])
                    item["errors"] = [text for status in statuses for text in _messages(status.get("errors"))]
                    item["warnings"] = [
                        text for status in statuses for text in _messages(status.get("warnings"), _NO_MESSAGE_WARNING)
                    ]
                    # The count is kept apart from the texts, so an error is never lost for want of a message.
                    if item["errors"]:
                        item["submission_error_count"] = len(item["errors"])
                    # A null error entry for this item cannot be read, but Meta listed it.
                    unreadable_errors = sum(_null_entries(status.get("errors")) for status in statuses)
                    if unreadable_errors:
                        item["submission_unreadable_error_count"] = unreadable_errors
                unreadable_submission = unreadable_entries + unreadable_unattributed_errors
                if unattributed or unreadable_submission:
                    submission: dict[str, Any] = {}
                    if unattributed:
                        submission.update({
                            "unattributed_errors": unattributed[:20],
                            "unattributed_error_count": len(unattributed),
                        })
                    if unreadable_entries:
                        submission["unreadable_entries"] = unreadable_entries
                    if unreadable_unattributed_errors:
                        submission["unreadable_error_entries"] = unreadable_unattributed_errors
                    submission["note"] = (
                        "Meta's answer to the submission listed "
                        + ("errors without naming an item in this batch" if unattributed else "")
                        + (" and " if unattributed and unreadable_submission else "")
                        + ("entries that could not be read" if unreadable_submission else "")
                        + ", so no item can be called applied."
                    )
                    response["submission"] = submission
                raw_handles = posted.get("handles")
                raw_handles = raw_handles if isinstance(raw_handles, list) else _entry_list(raw_handles)
                # Meta documents handles as strings. Any other entry (an object, null, true, a
                # list, a number, blank text) is a handle that cannot be checked; it is counted,
                # and while one exists no item can be called applied.
                handles = list(dict.fromkeys(
                    value.strip() for value in raw_handles if isinstance(value, str) and value.strip()
                ))
                unreadable_handles = sum(1 for value in raw_handles if not (isinstance(value, str) and value.strip()))
                if response_unreadable:
                    for item in to_send:
                        item["outcome"] = "outcome_unknown"
                        item["reason"] = ("Meta's answer to the submission could not be read, so whether it "
                                          "ingested this item is not known. Read the product before retrying.")
                    response["batch"] = {"complete": False, "handles": [], "response_unreadable": True}
                elif not handles and not unreadable_handles:
                    for item in to_send:
                        item["outcome"] = "not_applied"
                        item["reason"] = "Meta returned no batch handle, which it documents as meaning nothing was ingested."
                elif not handles:
                    for item in to_send:
                        item["outcome"] = "outcome_unknown"
                        item["reason"] = _unreadable_handles_reason(unreadable_handles, len(raw_handles))
                    response["batch"] = {"complete": False, "handles": [], "unreadable_handles": unreadable_handles}
                else:
                    tracks = await _poll_handles(client, catalog_id, handles, wait_seconds)
                    finished_rows = [track["row"] for track in tracks.values() if _is_finished(track["row"])]
                    unfinished = [handle for handle in handles if not _is_finished(tracks[handle]["row"])]
                    unfinished_reason = None
                    if unfinished:
                        unfinished_reason = _UNFINISHED_REASON
                        if any(tracks[handle]["error"] is not None for handle in unfinished):
                            unfinished_reason = (
                                "Meta accepted the batch, but its status could not be read (see the handle's "
                                "status_error), so whether it has finished is not known."
                            )
                        if len(handles) > 1:
                            unfinished_reason = (
                                f"Meta returned {len(handles)} handles for this batch and "
                                f"{len(unfinished)} had not been reported finished when this tool stopped "
                                "checking; Meta does not say which items each handle holds."
                            )
                    for item in to_send:
                        _settle(item, finished_rows, unfinished_reason=unfinished_reason,
                                unattributed_submission_errors=len(unattributed),
                                unreadable_submission_entries=unreadable_submission)
                        if unreadable_handles and item["outcome"] == "applied":
                            item["outcome"] = "outcome_unknown"
                            item["reason"] = _unreadable_handles_reason(unreadable_handles, len(raw_handles))
            except Exception as error:
                # Nothing but the tool's deliberate results may leave after the write has gone
                # out: a failure while reading Meta's answer keeps every item unresolved (or as
                # Meta already named it) with the handles known so far. CancelledError is not an
                # Exception, so a cancelled call still stops.
                answer_error = (
                    f"The request reached Meta, but this server failed while reading Meta's answer "
                    f"({type(error).__name__}), so whether each item was applied is not known. Read the "
                    "products before retrying."
                )
                for item in to_send:
                    if item["outcome"] in (None, "applied", "not_applied"):
                        item["outcome"] = "outcome_unknown"
                        item["reason"] = answer_error
                response["answer_error"] = answer_error
    for item in to_send:
        item.pop("request", None)
    if handles:
        response["batch"] = _batch_view(handles, tracks)
        if unreadable_handles:
            response["batch"]["unreadable_handles"] = unreadable_handles
            response["batch"]["complete"] = False
            response["batch"]["unreadable_note"] = _unreadable_handles_reason(unreadable_handles, len(raw_handles))
    outcomes = Counter(item["outcome"] for item in results)
    response["summary"] = {
        "submitted": len(results),
        "items_in_request": 0 if dry_run else len(to_send),
        "outcomes": dict(outcomes),
    }
    response["ok"] = dry_run or set(outcomes) <= {"applied", "skipped_already_exists", "skipped_not_found"}
    response["retry"] = _retry_statement(
        set(outcomes), [handle for handle in handles if not _is_finished(tracks.get(handle, {}).get("row"))],
    )
    if notes:
        response["notes"] = notes
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
        # The value given is not repeated: it can be a misplaced personal value.
        raise ValidationError(
            f"{label}.operator is not one Meta's product set filter accepts. "
            f"Accepted operators: {', '.join(FILTER_OPERATORS)}."
        )
    if "value" not in condition:
        raise ValidationError(f"{label}.value is required.")
    value = condition["value"]
    if operator in _LIST_OPERATORS:
        if not isinstance(value, list) or not value:
            raise ValidationError(f"{label}.value must be a nonempty list for {operator}.")
    elif operator in _NUMERIC_OPERATORS:
        if field_name.strip() == "price":
            # Meta's product set filter reference: price "must be specified with currency";
            # price_amount is "the price multiplied by 100, for all currencies".
            raise ValidationError(
                f"{label}: compare prices with field price_amount, the price x 100 in the product's currency "
                "(under 100 dollars is price_amount lt 10000), and add a currency condition when the catalog "
                "holds more than one currency. Meta requires price itself to be given with its currency."
            )
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
@_masked
async def build_product_set_filter(
    catalog_id: str,
    conditions: list[dict[str, Any]],
    match: str = "all",
) -> dict[str, Any]:
    """Read-only: turn conditions [{field, operator, value}] into a Meta product set filter, then count matching products in the catalog and return up to five samples. Creates nothing. Compare prices with price_amount, the price x 100 in the product's currency, plus a currency condition when the catalog holds several currencies."""
    catalog_id = _require(catalog_id, "catalog_id")
    if not isinstance(conditions, list) or not conditions:
        raise ValidationError("conditions must be a nonempty list of {field, operator, value} objects.")
    normalized_match = (match or "").strip().lower()
    if normalized_match not in {"all", "any"}:
        raise ValidationError("match must be all or any.")
    _refuse_url_logins(conditions, "conditions")
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


# Readable product item fields (SDK 26.0.2 ProductItem) whose values a product set
# filter is usually built on. Meta documents no endpoint that returns the values
# present in a catalog with counts, so they are counted from a bounded scan.
ATTRIBUTE_VALUE_FIELDS = (
    "brand", "category", "product_type", "custom_label_0", "custom_label_1", "custom_label_2",
    "custom_label_3", "custom_label_4", "availability", "condition", "color", "size", "material",
    "pattern", "gender", "age_group", "fb_product_category", "currency", "visibility",
    "review_status", "retailer_product_group_id",
)
_ATTRIBUTE_PAGE_SIZE = 100
_ATTRIBUTE_MAX_PRODUCTS = 5000
_ATTRIBUTE_MAX_TOP_N = 200
_ATTRIBUTE_MAX_VERIFY = 10


def _attribute_values(value: Any) -> list[str]:
    """Return the countable values of one product's attribute; blanks count as no value."""
    if isinstance(value, list):
        return [text for entry in value for text in _attribute_values(entry)]
    if value is None or isinstance(value, dict):
        return [] if value is None else [json.dumps(value, sort_keys=True)]
    text = str(value).strip()
    return [text] if text else []


@mcp_server.tool()
@_masked
async def list_catalog_attribute_values(
    catalog_id: GraphId,
    attribute: str,
    product_set_id: GraphId | None = None,
    max_products: int = 1000,
    top_n: int = 50,
    verify_top: int = 0,
) -> dict[str, Any]:
    """Read-only: the values that actually occur for one product attribute (brand, category, product_type, custom_label_0 to custom_label_4, availability, color, size and similar) in a product catalog or one of its product sets, with counts, so product set filters can use exact existing values. Meta has no endpoint for this, so it scans up to max_products products (whole pages of 100) and reports how many it examined and whether that was all; counts from a partial scan are lower bounds. verify_top (up to 10) reads Meta's exact match count for that many top values, one request each."""
    normalized = (attribute or "").strip().lower() if isinstance(attribute, str) else ""
    if normalized not in ATTRIBUTE_VALUE_FIELDS:
        raise ValidationError(f"attribute must be one of: {', '.join(ATTRIBUTE_VALUE_FIELDS)}.")
    if not 1 <= max_products <= _ATTRIBUTE_MAX_PRODUCTS:
        raise ValidationError(f"max_products must be from 1 to {_ATTRIBUTE_MAX_PRODUCTS}.")
    if not 1 <= top_n <= _ATTRIBUTE_MAX_TOP_N:
        raise ValidationError(f"top_n must be from 1 to {_ATTRIBUTE_MAX_TOP_N}.")
    if not 0 <= verify_top <= _ATTRIBUTE_MAX_VERIFY:
        raise ValidationError(f"verify_top must be from 0 to {_ATTRIBUTE_MAX_VERIFY}.")
    parent = product_set_id or catalog_id
    client = get_graph_api_client()
    # id is requested so pages are compared by product, not by attribute value:
    # two pages of different products can hold identical values.
    scan = await _collect_edge(
        client, parent, "products", fields=["id", normalized],
        params={"limit": min(_ATTRIBUTE_PAGE_SIZE, max_products), "summary": "total_count"},
        max_pages=-(-max_products // _ATTRIBUTE_PAGE_SIZE), max_items=max_products,
    )
    rows = [row for row in scan["rows"] if isinstance(row, dict)]
    counts: Counter[str] = Counter()
    without_value = 0
    for row in rows:
        found = _attribute_values(row.get(normalized))
        if not found:
            without_value += 1
        counts.update(set(found))
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    values: list[dict[str, Any]] = [{"value": value, "count": count} for value, count in ranked[:top_n]]
    complete = scan["complete"]
    in_scope = scan["summary_total"]
    notes: list[str] = ["Values are counted exactly as Meta returns them; filters match on these values."]
    if not complete:
        of_total = f" of {in_scope}" if in_scope is not None else ""
        notes.append(
            f"Examined {len(rows)}{of_total} products, so counts are lower bounds and values that occur only "
            "in unexamined products are missing. Raise max_products or use verify_top for exact counts."
        )
    verification_requests = 0
    for entry in values[:verify_top]:
        verification_requests += 1
        try:
            payload = await client.list_objects(
                parent, "products", fields=["id"],
                params={"filter": {normalized: {"eq": entry["value"]}}, "summary": "total_count", "limit": 1},
            )
        except _SIGNAL_ERRORS as error:
            entry["verify_error"] = str(error)
            continue
        exact = to_int((payload.get("summary") or {}).get("total_count"))
        if exact is None:
            entry["verify_error"] = "Meta did not return summary.total_count."
        else:
            entry["verified_count"] = exact
    scope: dict[str, Any] = {"catalog_id": catalog_id}
    if product_set_id:
        scope["product_set_id"] = product_set_id
    scope["attribute"] = normalized
    result: dict[str, Any] = {
        "scope": scope,
        "values": values,
        "distinct_values": len(counts),
        "values_shown": len(values),
        "products_without_value": without_value,
        "coverage": {
            "products_examined": len(rows),
            "products_in_scope": in_scope,
            "complete": complete,
            "stop_reason": scan["stop_reason"],
            "requests": scan["pages"],
            "counts_are": "exact" if complete else "lower_bounds",
        },
        "notes": notes,
        "next_step": "Use these exact values in build_product_set_filter conditions.",
    }
    if verify_top:
        result["verification_requests"] = verification_requests
    return result


@mcp_server.tool()
@_masked
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
    result = _paged(payload, limit=limit, after=after)
    result["summary"]["scope"] = {scope_name: scope_value}
    return result


@mcp_server.tool()
@_masked
async def create_product_set(
    catalog_id: str,
    name: str,
    filter: dict[str, Any] | str,
    params: dict[str, Any] | None = None,
    retailer_id: str | None = None,
) -> dict[str, Any]:
    """Create a product set in a product catalog from a Meta filter object ({} means every product). Build and test the filter first with build_product_set_filter. retailer_id is your own id for the set."""
    catalog_id = _require(catalog_id, "catalog_id")
    name = _require(name, "name")
    parsed_filter = _parse_filter(filter)
    typed: dict[str, Any] = {"name": name, "filter": parsed_filter}
    if retailer_id is not None:
        # Meta's product_sets edge reference: retailer_id, "External product set retailer id" (string).
        # params may still carry it for older callers; merge_graph_payload refuses both at once.
        typed["retailer_id"] = _require(retailer_id, "retailer_id")
    _refuse_url_logins_in(name=name, filter=parsed_filter, params=params, retailer_id=retailer_id)
    payload = merge_graph_payload(typed, params)
    result = await get_graph_api_client().create_edge_object(catalog_id, "product_sets", data=payload)
    return creation_response(action="create_product_set", target={"catalog_id": catalog_id}, result=result)


@mcp_server.tool()
@_masked
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
    _refuse_url_logins_in(**payload)
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
@_masked
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
