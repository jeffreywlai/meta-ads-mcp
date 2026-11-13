"""Businesses, their people and partners, and who can reach a product catalog.

Personal data: rows about people carry only what identifies a person and their
role (id, name, business email where Meta returns it, role or catalog tasks).
Nothing in this module logs, and error messages name ids only, never a name or
an email address.
"""

from __future__ import annotations

from typing import Any, Literal

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaAdsError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.tool_types import GraphId, StringList

BUSINESS_FIELDS = ["id", "name", "verification_status", "permitted_roles"]
BUSINESS_PERSON_FIELDS = ["id", "name", "email", "role"]
SYSTEM_USER_FIELDS = ["id", "name", "role"]
PARTNER_FIELDS = ["id", "name"]
ASSIGNED_USER_FIELDS = ["id", "name", "user_type", "tasks"]
ASSIGNED_CATALOG_FIELDS = ["id", "name", "access_type"]
OWNED_CATALOG_FIELDS = ["id", "name"]

# Catalog task names from Meta's ProductCatalog assigned_users reference.
CATALOG_TASKS = ("MANAGE", "ADVERTISE", "MANAGE_AR", "AA_ANALYZE")
# Meta's reference lists these names without defining their scope; these are
# this server's plain summaries, labelled as such in every response.
TASK_MEANINGS = {
    "MANAGE": "manage the catalog (Business Manager's full catalog control, including editing products and settings)",
    "ADVERTISE": "use the catalog to advertise",
    "MANAGE_AR": "manage augmented-reality effects for the catalog",
    "AA_ANALYZE": "see advanced analytics for the catalog",
}
TASK_MEANINGS_NOTE = (
    "Meta's reference lists these task names without defining what each allows; "
    "the descriptions are this server's summary."
)
UNTOUCHED_ACCESS_NOTE = (
    "Only direct catalog assignments change. Access through a business role, a business "
    "asset group or a partner business share is not affected and is not shown here."
)

USER_TYPES = ("EMPLOYEE", "SYSTEM_USER", "AGENCY")
CATALOG_RECIPIENT_TYPES = ("PERSON", "AGENCY")

MAX_LIMIT = 100
SCAN_PAGE_SIZE = 100
MAX_SCAN_PAGES = 10
MAX_CATALOGS_SCANNED = 50


def _numeric_id(value: Any, name: str) -> str:
    """Return a digits-only id or raise before any network call."""
    normalized = blank_to_none(value if isinstance(value, str) else None)
    if normalized is None or not normalized.isdigit():
        raise ValidationError(f"{name} must be a numeric Meta id passed as a string.")
    return normalized


def _optional_numeric_id(value: Any, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _numeric_id(value, name)


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_LIMIT}.")


def _choice(value: Any, name: str, allowed: tuple[str, ...]) -> str:
    normalized = str(value or "").strip().upper()
    if normalized not in allowed:
        raise ValidationError(f"{name} must be one of: {', '.join(allowed)}.")
    return normalized


def _page_params(limit: int, after: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if cursor := blank_to_none(after):
        params["after"] = cursor
    return params


def _project(row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Keep only the listed fields; ids stay strings."""
    item: dict[str, Any] = {}
    for field in fields:
        if field not in row:
            continue
        value = row[field]
        item[field] = str(value) if field == "id" and value is not None else value
    return item


def _page_response(payload: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Shape one Graph page the way list_audiences does, keeping only listed fields."""
    normalized = normalize_collection(payload)
    normalized["items"] = [_project(row, fields) for row in normalized["items"] if isinstance(row, dict)]
    has_more = bool(normalized["paging"].get("next"))
    after = normalized["paging"].get("after")
    normalized["summary"] = {
        "count": len(normalized["items"]),
        "complete": not has_more,
        "truncated": has_more,
        "next_after": after if has_more else None,
    }
    if has_more and not after:
        normalized["summary"]["incomplete_reason"] = (
            "Meta reported another page but gave no cursor to read it."
        )
    return normalized


async def _scan(
    client: Any,
    parent_id: str,
    edge: str,
    *,
    fields: list[str],
    params: dict[str, Any] | None = None,
    after: str | None = None,
    max_pages: int = MAX_SCAN_PAGES,
    page_size: int = SCAN_PAGE_SIZE,
    stop_when: Any = None,
) -> dict[str, Any]:
    """Read an edge page by page, capped by pages, and say whether the end was reached.

    The end is reached only when a page has no next link. A next link without a
    cursor, a repeated cursor, or the page cap stops the read as incomplete.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = blank_to_none(after)
    requests = 0
    reason: str | None = None
    next_after: str | None = None
    while True:
        if requests >= max_pages:
            reason = f"stopped after {max_pages} requests; more pages exist."
            next_after = cursor
            break
        query: dict[str, Any] = {**(params or {}), "limit": page_size}
        if cursor:
            query["after"] = cursor
        payload = await client.list_objects(parent_id, edge, fields=fields, params=query)
        requests += 1
        page = normalize_collection(payload)
        page_rows = [row for row in page["items"] if isinstance(row, dict)]
        rows.extend(page_rows)
        if not page["paging"].get("next"):
            return {"rows": rows, "complete": True, "requests": requests, "reason": None, "next_after": None}
        new_cursor = page["paging"].get("after")
        if not new_cursor:
            reason = "Meta reported another page but gave no cursor to read it."
            break
        if new_cursor in seen or new_cursor == cursor:
            reason = "Meta returned the same page cursor twice, so the read stopped."
            break
        seen.add(new_cursor)
        cursor = new_cursor
        if stop_when is not None and stop_when(page_rows):
            reason = "stopped once the requested row was found; later pages were not read."
            next_after = cursor
            break
    return {"rows": rows, "complete": False, "requests": requests, "reason": reason, "next_after": next_after}


def _matches(row: dict[str, Any], search: str, text_fields: tuple[str, ...]) -> bool:
    if search.isdigit():
        return str(row.get("id", "")) == search
    needle = search.casefold()
    return any(needle in str(row.get(field) or "").casefold() for field in text_fields)


def _search_response(
    scan: dict[str, Any],
    fields: list[str],
    search: str,
    text_fields: tuple[str, ...],
    limit: int,
) -> dict[str, Any]:
    matches = [_project(row, fields) for row in scan["rows"] if _matches(row, search, text_fields)]
    items = matches[:limit]
    summary: dict[str, Any] = {
        "count": len(items),
        "matched": len(matches),
        "scanned": len(scan["rows"]),
        "requests": scan["requests"],
        "complete": scan["complete"] and len(matches) <= limit,
        "search": (
            "exact id match" if search.isdigit()
            else f"case-insensitive substring of {' or '.join(text_fields)}, matched by this server"
        ),
        "next_after": scan["next_after"],
    }
    reasons = []
    if not scan["complete"]:
        reasons.append(f"Not every row was read: {scan['reason']}")
    if len(matches) > limit:
        reasons.append(f"{len(matches)} rows matched; only the first {limit} are returned. Narrow the search.")
    if reasons:
        summary["incomplete_reason"] = " ".join(reasons)
    return {"items": items, "paging": {"before": None, "after": scan["next_after"], "next": None}, "summary": summary}


@mcp_server.tool()
async def list_businesses(
    name_contains: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List the Meta businesses (Business Manager portfolios) the token's user can access, with the user's roles on each. name_contains filters by name here, because Meta has no name filter; the response says how many businesses were scanned and whether that was all of them."""
    _validate_limit(limit)
    client = get_graph_api_client()
    search = blank_to_none(name_contains)
    if search is None:
        payload = await client.list_objects("me", "businesses", fields=BUSINESS_FIELDS, params=_page_params(limit, after))
        return _page_response(payload, BUSINESS_FIELDS)
    scan = await _scan(client, "me", "businesses", fields=BUSINESS_FIELDS, after=after)
    return _search_response(scan, BUSINESS_FIELDS, search, ("name",), limit)


@mcp_server.tool()
async def list_business_users(
    business_id: GraphId,
    user_type: Literal["EMPLOYEE", "SYSTEM_USER", "AGENCY"] = "EMPLOYEE",
    search: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List a business's people (EMPLOYEE), system users (SYSTEM_USER) or partner businesses (AGENCY, Business Manager's Partners) with their role. Each person's id is the business-scoped id the catalog access tools take. Rows carry only id, name, business email where Meta returns it, and role. search matches an exact id, or a name (or a person's email) substring."""
    business_id = _numeric_id(business_id, "business_id")
    user_type = _choice(user_type, "user_type", USER_TYPES)
    _validate_limit(limit)
    if user_type == "EMPLOYEE":
        edge, fields, text_fields = "business_users", BUSINESS_PERSON_FIELDS, ("name", "email")
    elif user_type == "SYSTEM_USER":
        edge, fields, text_fields = "system_users", SYSTEM_USER_FIELDS, ("name",)
    else:
        edge, fields, text_fields = "agencies", PARTNER_FIELDS, ("name",)
    client = get_graph_api_client()
    query = blank_to_none(search)
    if query is None:
        response = _page_response(
            await client.list_objects(business_id, edge, fields=fields, params=_page_params(limit, after)), fields,
        )
    else:
        response = _search_response(await _scan(client, business_id, edge, fields=fields, after=after), fields, query, text_fields, limit)
    response["scope"] = {"business_id": business_id, "user_type": user_type}
    response["notes"] = [
        "Meta may show a person who is not a business admin only themselves and the admins, so a short list can mean limited visibility.",
        "This lists who is on the business, not who can use one catalog; use list_catalog_assigned_users for that.",
    ]
    return response


async def _catalog_business_id(client: Any, catalog_id: str) -> str:
    """Return the catalog's owning business id, read from the catalog."""
    catalog = await client.get_object(catalog_id, fields=["id", "business"])
    business = catalog.get("business") if isinstance(catalog, dict) else None
    business_id = business.get("id") if isinstance(business, dict) else None
    if not business_id:
        raise ValidationError(
            f"Meta did not return an owning business for catalog {catalog_id}; pass business_id."
        )
    return str(business_id)


def _row_tasks(row: dict[str, Any] | None) -> list[str]:
    if not row:
        return []
    tasks = row.get("tasks")
    if not isinstance(tasks, list):
        return []
    return sorted({str(task).upper() for task in tasks})


def _task_meanings(tasks: list[str]) -> dict[str, str]:
    return {task: TASK_MEANINGS[task] for task in tasks if task in TASK_MEANINGS}


async def _find_assignment(
    client: Any, catalog_id: str, business_id: str, user_id: str,
) -> dict[str, Any]:
    """Read the catalog's direct assignments until one user's row is found."""
    scan = await _scan(
        client, catalog_id, "assigned_users", fields=ASSIGNED_USER_FIELDS,
        params={"business": business_id},
        stop_when=lambda rows: any(str(row.get("id")) == user_id for row in rows),
    )
    row = next((row for row in scan["rows"] if str(row.get("id")) == user_id), None)
    known = row is not None or scan["complete"]
    return {
        "row": _project(row, ASSIGNED_USER_FIELDS) if row else None,
        "known": known,
        "scanned": len(scan["rows"]),
        "requests": scan["requests"],
        "reason": None if known else scan["reason"],
    }


@mcp_server.tool()
async def list_catalog_assigned_users(
    catalog_id: GraphId,
    business_id: GraphId | None = None,
    user_type: Literal["PERSON", "AGENCY"] = "PERSON",
    user_id: GraphId | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List who has direct access to a Meta product catalog and with which tasks (MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE), or with user_type=AGENCY the partner businesses it is shared with. Pass user_id to check one person. business_id defaults to the catalog's owning business. Access through a business role or business asset group is not listed."""
    catalog_id = _numeric_id(catalog_id, "catalog_id")
    business_id = _optional_numeric_id(business_id, "business_id")
    user_id = _optional_numeric_id(user_id, "user_id")
    user_type = _choice(user_type, "user_type", CATALOG_RECIPIENT_TYPES)
    _validate_limit(limit)
    if user_type == "AGENCY" and business_id is not None:
        raise ValidationError("business_id is not used with user_type=AGENCY; the partner list belongs to the catalog.")
    client = get_graph_api_client()
    if user_type == "AGENCY":
        if user_id is None:
            response = _page_response(
                await client.list_objects(catalog_id, "agencies", fields=PARTNER_FIELDS, params=_page_params(limit, after)),
                PARTNER_FIELDS,
            )
        else:
            scan = await _scan(client, catalog_id, "agencies", fields=PARTNER_FIELDS, after=after)
            response = _search_response(scan, PARTNER_FIELDS, user_id, ("name",), limit)
        response["scope"] = {"catalog_id": catalog_id, "user_type": "AGENCY"}
        response["notes"] = ["Meta's catalog agencies edge returns the partner businesses only, not the tasks shared with each."]
        return response

    if business_id is None:
        business_id = await _catalog_business_id(client, catalog_id)
    scope = {"catalog_id": catalog_id, "business_id": business_id, "user_type": "PERSON"}
    if user_id is not None:
        found = await _find_assignment(client, catalog_id, business_id, user_id)
        row = found["row"]
        tasks = _row_tasks(row)
        if row:
            status = "HAS_DIRECT_ACCESS" if tasks else "ASSIGNED_WITHOUT_TASKS"
        elif found["known"]:
            status = "NO_DIRECT_ACCESS"
        else:
            status = "UNKNOWN"
        result: dict[str, Any] = {
            "scope": {**scope, "user_id": user_id},
            "item": {**(row or {"id": user_id}), "tasks": tasks, "can": _task_meanings(tasks)},
            "access_status": status,
            "summary": {"scanned": found["scanned"], "requests": found["requests"], "complete": found["known"]},
            "notes": [UNTOUCHED_ACCESS_NOTE, TASK_MEANINGS_NOTE],
        }
        if status == "UNKNOWN":
            result["summary"]["incomplete_reason"] = f"The person was not among the rows read: {found['reason']}"
        return result
    params = {**_page_params(limit, after), "business": business_id}
    payload = await client.list_objects(catalog_id, "assigned_users", fields=ASSIGNED_USER_FIELDS, params=params)
    response = _page_response(payload, ASSIGNED_USER_FIELDS)
    for item in response["items"]:
        item["tasks"] = _row_tasks(item)
    response["scope"] = scope
    response["task_meanings"] = dict(TASK_MEANINGS)
    response["notes"] = [UNTOUCHED_ACCESS_NOTE, TASK_MEANINGS_NOTE]
    return response


@mcp_server.tool()
async def list_user_assigned_catalogs(
    user_id: GraphId,
    user_type: Literal["EMPLOYEE", "SYSTEM_USER", "AGENCY"] = "EMPLOYEE",
    business_id: GraphId | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List the Meta product catalogs assigned to one person or system user (business-scoped id from list_business_users), or with user_type=AGENCY the catalogs your business (business_id, required) shares with that partner business. The partner view scans your owned catalogs one by one and reports how many it checked."""
    user_id = _numeric_id(user_id, "user_id")
    user_type = _choice(user_type, "user_type", USER_TYPES)
    business_id = _optional_numeric_id(business_id, "business_id")
    _validate_limit(limit)
    if user_type == "AGENCY" and business_id is None:
        raise ValidationError("business_id (your business, which owns the catalogs) is required with user_type=AGENCY.")
    client = get_graph_api_client()
    if user_type != "AGENCY":
        payload = await client.list_objects(
            user_id, "assigned_product_catalogs", fields=ASSIGNED_CATALOG_FIELDS, params=_page_params(limit, after),
        )
        response = _page_response(payload, ASSIGNED_CATALOG_FIELDS)
        response["scope"] = {"user_id": user_id, "user_type": user_type}
        response["notes"] = [
            "Lists direct and business asset group assignments Meta returns for this business-scoped user.",
            "Use list_catalog_assigned_users with user_id for the tasks this person holds on one catalog.",
        ]
        return response

    owned = await _scan(
        client, business_id, "owned_product_catalogs", fields=OWNED_CATALOG_FIELDS,
        after=after, max_pages=1, page_size=MAX_CATALOGS_SCANNED,
    )
    catalogs = owned["rows"]
    gaps: list[str] = []
    if not owned["complete"]:
        gaps.append(
            f"Only {len(catalogs)} owned catalogs were checked ({owned['reason']}). "
            + ("Continue with after=summary.next_after." if owned["next_after"] else "")
        )
    items: list[dict[str, Any]] = []
    unreadable: list[str] = []
    requests = owned["requests"]
    for catalog in catalogs:
        catalog_id = str(catalog.get("id"))
        try:
            partners = await _scan(client, catalog_id, "agencies", fields=PARTNER_FIELDS, max_pages=3)
        except MetaAdsError:
            unreadable.append(catalog_id)
            continue
        requests += partners["requests"]
        if any(str(row.get("id")) == user_id for row in partners["rows"]):
            items.append({**_project(catalog, OWNED_CATALOG_FIELDS), "access_type": "AGENCY"})
        elif not partners["complete"]:
            gaps.append(f"Catalog {catalog_id}: partner list not fully read ({partners['reason']}).")
    if unreadable:
        gaps.append(f"Partner lists could not be read for catalogs: {', '.join(unreadable)}.")
    complete = not gaps
    summary: dict[str, Any] = {
        "count": len(items[:limit]),
        "matched": len(items),
        "catalogs_checked": len(catalogs) - len(unreadable),
        "requests": requests,
        "complete": complete and len(items) <= limit,
        "next_after": owned["next_after"],
    }
    if gaps:
        summary["incomplete_reason"] = " ".join(gaps)
    return {
        "items": items[:limit],
        "paging": {"before": None, "after": None, "next": None},
        "summary": summary,
        "scope": {"user_id": user_id, "user_type": "AGENCY", "business_id": business_id},
        "notes": [
            "Only catalogs owned by business_id were checked; Meta's catalog agencies edge does not return the tasks shared.",
        ],
    }


def _normalize_tasks(tasks: Any) -> list[str]:
    if tasks is None:
        raise ValidationError(
            f"tasks is required: the complete set the person should hold, from {', '.join(CATALOG_TASKS)}; "
            "pass an empty list to remove their direct access."
        )
    if isinstance(tasks, str):
        tasks = [part for part in tasks.split(",")] if tasks.strip() else []
    if not isinstance(tasks, list):
        raise ValidationError("tasks must be a list of task names.")
    normalized: set[str] = set()
    for task in tasks:
        name = str(task).strip().upper()
        if name not in CATALOG_TASKS:
            raise ValidationError(
                f"Unknown catalog task {str(task).strip()!r}. Accepted values: {', '.join(CATALOG_TASKS)}."
            )
        normalized.add(name)
    return sorted(normalized)


async def _caller_ids(client: Any) -> set[str] | None:
    """Ids that belong to the token's own user, or None when they cannot be read."""
    try:
        me = await client.get_object("me", fields=["id"])
        ids = {str(me["id"])} if isinstance(me, dict) and me.get("id") else set()
        scan = await _scan(client, "me", "business_users", fields=["id", "business"], max_pages=3)
    except MetaAdsError:
        return None
    if not ids or not scan["complete"]:
        return None
    ids.update(str(row["id"]) for row in scan["rows"] if row.get("id"))
    return ids


def _access_inputs(catalog_id: Any, user_id: Any, tasks: Any, business_id: Any) -> dict[str, Any]:
    """Validate the access-change inputs before any network call."""
    return {
        "catalog_id": _numeric_id(catalog_id, "catalog_id"),
        "user_id": _numeric_id(user_id, "user_id"),
        "business_id": _optional_numeric_id(business_id, "business_id"),
        "proposed": _normalize_tasks(tasks),
    }


async def _plan_access_change(
    client: Any,
    *,
    catalog_id: str,
    user_id: str,
    proposed: list[str],
    business_id: str | None,
    allow_self_removal: bool,
) -> dict[str, Any]:
    """Read current access and compute the change; shared by the preview and the write."""
    if business_id is None:
        business_id = await _catalog_business_id(client, catalog_id)
    found = await _find_assignment(client, catalog_id, business_id, user_id)
    row = found["row"]
    previous = _row_tasks(row)
    lost = [task for task in previous if task not in proposed]
    gained = [task for task in proposed if task not in previous]
    if not found["known"]:
        change = "unknown"
    elif not proposed:
        change = "remove" if row is not None else "nothing_to_remove"
    elif row is not None and previous == proposed:
        change = "no_change"
    elif row is None:
        change = "grant"
    elif not lost:
        change = "increase"
    elif not gained:
        change = "reduce"
    else:
        change = "change"

    target = {"catalog_id": catalog_id, "business_id": business_id, "user_id": user_id}
    if change == "remove":
        request = {"method": "DELETE", "path": f"{catalog_id}/assigned_users", "data": {"user": user_id, "business": business_id}}
    elif change in {"grant", "increase", "reduce", "change"}:
        request = {
            "method": "POST", "path": f"{catalog_id}/assigned_users",
            "data": {"user": user_id, "business": business_id, "tasks": proposed},
        }
    else:
        request = None

    warnings: list[str] = []
    refusal: str | None = None
    risky = change == "remove" or "MANAGE" in lost
    self_check: dict[str, Any] = {"checked": False}
    if risky:
        caller = await _caller_ids(client)
        is_self = None if caller is None else user_id in caller
        self_check = {"checked": True, "is_caller": is_self}
        if is_self is True and not allow_self_removal:
            refusal = (
                f"User {user_id} is the token's own user. This change would remove its own catalog "
                "access or its MANAGE task and could lock this server out of the catalog. "
                "Pass allow_self_removal=true only if that is intended."
            )
        elif is_self is None and not allow_self_removal:
            refusal = (
                "The token's own user ids could not be read, so this server cannot confirm that "
                f"user {user_id} is not the caller. Pass allow_self_removal=true to proceed after "
                "confirming the recipient is not you."
            )
        elif is_self is True:
            warnings.append("This removes or reduces the token's own access; this server may lose the ability to manage the catalog.")
    if change == "unknown":
        refusal = (
            f"The current access of user {user_id} could not be read completely ({found['reason']}); "
            "nothing can be changed safely."
        )
    if change == "nothing_to_remove":
        refusal = f"User {user_id} has no direct access to catalog {catalog_id}; there is nothing to remove."
    if lost:
        what = "; ".join(f"{task}: {TASK_MEANINGS[task]}" for task in lost)
        warnings.insert(0, f"After this change user {user_id} can no longer: {what}.")
    if change == "remove":
        warnings.insert(1, f"User {user_id} loses all direct access to catalog {catalog_id}.")
    if "MANAGE" in gained:
        warnings.append(f"MANAGE gives user {user_id} full control of catalog {catalog_id}.")
    if change in {"increase", "reduce", "change"}:
        warnings.append(
            "Meta documents granting and removing, not editing in place; the new task set is sent as a "
            "grant and the result is read back to confirm Meta replaced the old set."
        )

    return {
        "target": target,
        "recipient": row or {"id": user_id},
        "previous": {"tasks": previous, "has_direct_access": row is not None, "can": _task_meanings(previous)},
        "proposed": {"tasks": proposed, "has_direct_access": bool(proposed), "can": _task_meanings(proposed)},
        "change": change,
        "can_no_longer": _task_meanings(lost),
        "gains": _task_meanings(gained),
        "warnings": warnings,
        "request": request,
        "refusal": refusal,
        "self_check": self_check,
        "current_access_read": {
            "complete": found["known"], "rows_scanned": found["scanned"], "requests": found["requests"],
        },
        "notes": [UNTOUCHED_ACCESS_NOTE, TASK_MEANINGS_NOTE],
    }


@mcp_server.tool(tags={"read-only"})
async def preview_catalog_access_change(
    catalog_id: GraphId,
    user_id: GraphId,
    tasks: StringList,
    business_id: GraphId | None = None,
    allow_self_removal: bool = False,
) -> dict[str, Any]:
    """Read-only. Show a person's or system user's current tasks on a Meta product catalog, the proposed set, what they would lose or gain, warnings, and the exact request set_catalog_access would send. Sends no change. tasks is the complete new set from MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE; an empty list means remove direct access."""
    inputs = _access_inputs(catalog_id, user_id, tasks, business_id)
    client = get_graph_api_client()
    plan = await _plan_access_change(client, **inputs, allow_self_removal=allow_self_removal)
    return {
        "read_only": True,
        "sent": False,
        **{key: plan[key] for key in ("target", "recipient", "previous", "proposed", "change", "can_no_longer", "gains", "warnings")},
        "would_send": plan["request"],
        "would_be_refused": plan["refusal"] is not None,
        "refusal_reason": plan["refusal"],
        "self_check": plan["self_check"],
        "current_access_read": plan["current_access_read"],
        "notes": plan["notes"],
    }


@mcp_server.tool()
async def set_catalog_access(
    catalog_id: GraphId,
    user_id: GraphId,
    tasks: StringList,
    business_id: GraphId | None = None,
    allow_self_removal: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Use only on an explicit request to grant, change or remove a person's or system user's direct access to a Meta product catalog. tasks is the complete new set (MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE); an empty list removes direct access. Reads current access first, refuses to remove the token's own access unless allow_self_removal=true, and returns before and after. Partner businesses are not supported."""
    inputs = _access_inputs(catalog_id, user_id, tasks, business_id)
    client = get_graph_api_client()
    plan = await _plan_access_change(client, **inputs, allow_self_removal=allow_self_removal)
    if plan["refusal"] is not None:
        raise ValidationError(plan["refusal"] + " Nothing was changed.")
    target = plan["target"]
    base = {
        "ok": True,
        "action": "set_catalog_access",
        "target": target,
        "previous": plan["previous"],
        "change": plan["change"],
        "can_no_longer": plan["can_no_longer"],
        "gains": plan["gains"],
        "warnings": plan["warnings"],
        "notes": plan["notes"],
    }
    if plan["change"] == "no_change":
        return {**base, "current": plan["previous"], "sent": False, "message": "The person already holds exactly these tasks; nothing was sent."}
    request = plan["request"]
    if dry_run:
        return {**base, "dry_run": True, "sent": False, "would_send": request, "current": plan["proposed"]}
    if request["method"] == "DELETE":
        await client.delete_edge_object(target["catalog_id"], "assigned_users", data=dict(request["data"]))
    else:
        await client.create_edge_object(target["catalog_id"], "assigned_users", data=dict(request["data"]))
    response = {**base, "sent": True, "request_sent": request}
    try:
        after = await _find_assignment(client, target["catalog_id"], target["business_id"], target["user_id"])
    except MetaAdsError:
        after = None
    if after is None or not after["known"]:
        response["current"] = plan["proposed"]
        response["verified"] = False
        response["warnings"] = [*plan["warnings"], "Meta accepted the request, but the access could not be read back to confirm it."]
        return response
    after_tasks = _row_tasks(after["row"])
    response["current"] = {"tasks": after_tasks, "has_direct_access": after["row"] is not None, "can": _task_meanings(after_tasks)}
    response["verified"] = after_tasks == plan["proposed"]["tasks"]
    if not response["verified"]:
        response["ok"] = False
        response["warnings"] = [
            *plan["warnings"],
            f"Meta reports tasks {after_tasks or 'none'} after the change, not the requested "
            f"{plan['proposed']['tasks'] or 'none'}; the change did not fully take effect.",
        ]
    return response
