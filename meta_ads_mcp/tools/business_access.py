"""Businesses, their people and partners, and who can reach a product catalog.

Personal data: rows about people carry only what identifies a person and their
role (id, name, business email where Meta returns it, role or catalog tasks).
Nothing in this module logs, and error messages name ids only, never a name or
an email address.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaAdsError, MetaApiError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import blank_to_none, collection_fault, read_collection_page
from meta_ads_mcp.pagination import extract_paging
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

EMPTY_BUSINESSES_NOTE = (
    "Meta returned no businesses for this token. That is not an error: a system-user token may "
    "list none here even when its system user belongs to a business. Read an ad account's owning "
    'business with get_ad_account(fields=["business"]).'
)

USER_TYPES = ("EMPLOYEE", "SYSTEM_USER", "AGENCY")
# Meta's own user_type values on a catalog's assigned_users rows, mapped to the kinds
# above. Meta's reference and the SDK (AssignedUser.user_type is a plain string) list no
# allowed values, so only a value Meta has actually returned is mapped: business_user,
# seen in a live read on 2026-11-13. Matching is case-insensitive, so Meta's system_user
# also matches this server's own SYSTEM_USER.
META_USER_TYPE_KINDS = {"BUSINESS_USER": "EMPLOYEE"}
_USER_TYPE_ACCEPTED = (
    "EMPLOYEE, SYSTEM_USER, AGENCY, or BUSINESS_USER (Meta's user_type for a person, "
    "meaning EMPLOYEE); case does not matter"
)
_PERSON_KINDS = ("EMPLOYEE", "SYSTEM_USER")

MAX_LIMIT = 100
SCAN_PAGE_SIZE = 100
MAX_SCAN_PAGES = 10
MAX_CATALOGS_SCANNED = 50


def _numeric_id(value: Any, name: str) -> str:
    """Return a digits-only id or raise before any network call."""
    normalized = blank_to_none(value if isinstance(value, str) else None)
    if normalized is None or not (normalized.isascii() and normalized.isdigit()):
        raise ValidationError(f"{name} must be a numeric Meta id passed as a string.")
    return normalized


def _optional_numeric_id(value: Any, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _numeric_id(value, name)


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_LIMIT}.")


def _known_kind(value: Any) -> str | None:
    """Map this server's kinds and Meta's returned user_type values to one kind."""
    normalized = str(value or "").strip().upper()
    if normalized in USER_TYPES:
        return normalized
    return META_USER_TYPE_KINDS.get(normalized)


def _user_kind(value: Any) -> str:
    """Return EMPLOYEE, SYSTEM_USER or AGENCY, or raise listing what is accepted.

    The message never repeats the value given: callers sometimes put a name or
    an email address in the wrong argument.
    """
    kind = _known_kind(value)
    if kind is None:
        raise ValidationError(f"user_type is not recognised. Accepted values: {_USER_TYPE_ACCEPTED}.")
    return kind


def _catalog_recipient_kind(value: Any) -> str:
    """Return PERSON or AGENCY for the catalog access tools, accepting the other tools' kinds."""
    if str(value or "").strip().upper() == "PERSON":
        return "PERSON"
    kind = _known_kind(value)
    if kind is None:
        raise ValidationError(f"user_type is not recognised. Accepted values: PERSON, {_USER_TYPE_ACCEPTED}.")
    return "AGENCY" if kind == "AGENCY" else "PERSON"


def _row_kind(row: dict[str, Any]) -> str | None:
    """The person kind of an assigned_users row, in the vocabulary the other tools accept."""
    kind = _known_kind(row.get("user_type"))
    return kind if kind in _PERSON_KINDS else None


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


PAGE_UNREADABLE_REASON = (
    "Meta returned a page without a readable data list, so the rows on it, and whether more pages "
    "follow, are unknown."
)


def _non_object_rows_reason(count: int) -> str:
    return (
        f"Meta returned {count} row(s) that are not objects, so they could not be read and could be "
        "any person, partner or business."
    )


def _page_response(payload: Any, fields: list[str]) -> dict[str, Any]:
    """Shape one Graph page the way list_audiences does, keeping only listed fields.

    The one place a single listing page in this module is called complete or not: complete
    only when collection_fault finds nothing wrong with the page and Meta reported no next
    page. A page without a readable data list returns no items and no cursor; rows that are
    not objects cannot be shown, so they are counted. Either way incomplete_reason says why.
    """
    fault = collection_fault(payload)
    rows, bad_rows, page_unreadable = read_collection_page(payload)
    if page_unreadable:
        return {
            "items": [],
            "paging": {"before": None, "after": None, "next": None},
            "summary": {
                "count": 0, "complete": False, "truncated": False, "next_after": None,
                "page_unreadable": True, "incomplete_reason": fault,
            },
        }
    paging = extract_paging(payload)
    items = [_project(row, fields) for row in rows]
    has_more = bool(paging.get("next"))
    after = paging.get("after")
    summary: dict[str, Any] = {
        "count": len(items),
        "complete": not has_more and not bad_rows,
        "truncated": has_more,
        "next_after": after if has_more else None,
    }
    reasons = []
    if has_more and not after:
        reasons.append("Meta reported another page but gave no cursor to read it.")
    if fault:
        summary["unreadable_rows"] = bad_rows
        reasons.append(fault)
    if reasons:
        summary["incomplete_reason"] = " ".join(reasons)
    return {"items": items, "paging": paging, "summary": summary}


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
) -> dict[str, Any]:
    """Read an edge page by page, capped by pages, and say whether the end was reached.

    The end is reached only when a readable page has no next link. A page without a
    readable data list stops the read as incomplete: its rows, and whether more pages
    follow, are unknown. So do a next link without a cursor, a repeated cursor, and the
    page cap. Rows that are not objects are counted in unreadable_rows and also make the
    read incomplete, because any one of them could be the row a caller is looking for.

    A page whose next cursor repeats an earlier one is still a page Meta answered to a
    cursor not read before, so its rows are kept; the read stops there as incomplete and
    the repeated cursor is never followed.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = blank_to_none(after)
    requests = 0
    unreadable = 0
    reason: str | None = None
    next_after: str | None = None
    page_unreadable = False
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
        page_rows, bad_rows, page_unreadable = read_collection_page(payload)
        if page_unreadable:
            reason = PAGE_UNREADABLE_REASON
            # The same page can be asked for again.
            next_after = cursor
            break
        unreadable += bad_rows
        rows.extend(page_rows)
        paging = extract_paging(payload)
        if not paging.get("next"):
            break
        new_cursor = paging.get("after")
        if not new_cursor:
            reason = "Meta reported another page but gave no cursor to read it."
            break
        if new_cursor in seen or new_cursor == cursor:
            reason = "Meta returned the same page cursor twice, so the read stopped."
            break
        seen.add(new_cursor)
        cursor = new_cursor
    reasons = [text for text in (reason, _non_object_rows_reason(unreadable) if unreadable else None) if text]
    return {
        "rows": rows,
        "complete": not reasons,
        "requests": requests,
        "reason": " ".join(reasons) if reasons else None,
        "next_after": next_after,
        "unreadable_rows": unreadable,
        "page_unreadable": page_unreadable,
    }


def _row_identity(value: Any) -> str | None:
    """The one canonical identity of an id Meta returned, or None when it cannot be read.

    Every id this server matches against is a plain numeric Graph id, so a row's id
    counts only when it is a non-negative whole number, or a string of digits once
    surrounding spaces are removed. Anything else (a word, a negative number, a
    boolean, a decimal, an object) cannot be compared with the recipient or the
    caller, so the row is unidentified. Used for matching the recipient, for
    counting unidentified rows, and for the caller's own ids alike.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped.isascii() and stripped.isdigit() else None
    return None


def _is_row(row: dict[str, Any], wanted_id: str) -> bool:
    """True when the row's canonical identity is wanted_id."""
    return _row_identity(row.get("id")) == wanted_id


def _unidentified_rows(scan: dict[str, Any]) -> int:
    """Rows of a scan whose own id cannot be read: not an object, no id, or an id that is not a numeric id."""
    return scan.get("unreadable_rows", 0) + sum(1 for row in scan["rows"] if _row_identity(row.get("id")) is None)


def _matches(row: dict[str, Any], search: str, text_fields: tuple[str, ...]) -> bool | None:
    """True or False for a search, or None when the row lacks what the search needs.

    An exact id search needs a readable numeric id. A text search needs the row's first text
    field (a name, which every business, person and partner has); later fields, such as a
    person's business email, are optional in Meta's answer and can only add a match.
    """
    if search.isdigit():
        identity = _row_identity(row.get("id"))
        return None if identity is None else identity == search
    needle = search.casefold()
    values = [row.get(field) for field in text_fields]
    if any(isinstance(value, str) and needle in value.casefold() for value in values):
        return True
    return None if not isinstance(values[0], str) or not values[0].strip() else False


def _search_response(
    scan: dict[str, Any],
    fields: list[str],
    search: str,
    text_fields: tuple[str, ...],
    limit: int,
) -> dict[str, Any]:
    verdicts = [(row, _matches(row, search, text_fields)) for row in scan["rows"]]
    matches = [_project(row, fields) for row, verdict in verdicts if verdict]
    items = matches[:limit]
    # A row that could not be judged could be the one searched for. Rows that are not
    # objects already make the scan incomplete. An object row without what the search needs
    # (a readable id, or a name) is counted too, except that an exact id search that found
    # its row is settled, because ids are unique.
    unsearchable = sum(1 for _, verdict in verdicts if verdict is None)
    if search.isdigit() and matches:
        unsearchable = 0
    summary: dict[str, Any] = {
        "count": len(items),
        "matched": len(matches),
        "scanned": len(scan["rows"]),
        "requests": scan["requests"],
        "complete": scan["complete"] and len(matches) <= limit and not unsearchable,
        "search": (
            "exact id match" if search.isdigit()
            else f"case-insensitive substring of {' or '.join(text_fields)}, matched by this server"
        ),
        "next_after": scan["next_after"],
    }
    reasons = []
    if not scan["complete"]:
        reasons.append(f"Not every row was read: {scan['reason']}")
    if unsearchable:
        needs = "a readable numeric id" if search.isdigit() else f"a readable {text_fields[0]}"
        reasons.append(f"Meta returned {unsearchable} row(s) without {needs}, so whether they match is unknown.")
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
        response = _page_response(payload, BUSINESS_FIELDS)
        returned = len(response["items"])
    else:
        scan = await _scan(client, "me", "businesses", fields=BUSINESS_FIELDS, after=after)
        response = _search_response(scan, BUSINESS_FIELDS, search, ("name",), limit)
        returned = len(scan["rows"])
        if scan["page_unreadable"] or scan["unreadable_rows"]:
            returned = None
    # Only a readable answer with no rows means Meta returned no businesses.
    if returned == 0 and not response["summary"].get("page_unreadable") and not response["summary"].get("unreadable_rows"):
        response["notes"] = [EMPTY_BUSINESSES_NOTE]
    return response


@mcp_server.tool()
async def list_business_users(
    business_id: GraphId,
    user_type: str = "EMPLOYEE",
    search: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List a business's people (EMPLOYEE), system users (SYSTEM_USER) or partner businesses (AGENCY, Business Manager's Partners) with their role. Each person's id is the business-scoped id the catalog access tools take. Rows carry only id, name, business email where Meta returns it, and role. search matches an exact id, or a name (or a person's email) substring. user_type also accepts Meta's business_user, as list_catalog_assigned_users returns it."""
    business_id = _numeric_id(business_id, "business_id")
    user_type = _user_kind(user_type)
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
        "The list holds only the people Meta lets this token see, so a short list can mean limited visibility rather than a small business.",
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
            "Meta did not return an owning business for this catalog; pass business_id."
        )
    return str(business_id)


def _tasks_state(row: dict[str, Any] | None, field: str = "tasks") -> tuple[list[str] | None, str | None]:
    """Return (tasks, None) when a row's tasks can be trusted, or (None, reason) when not.

    No row means no direct access: an empty set. A row whose task list is absent,
    is not a list, or holds a value this server does not recognise is not "no
    tasks"; the person's current access is unknown, and every caller treats it as
    an incomplete read. The reason never quotes Meta's values.
    """
    if not row:
        return [], None
    if field not in row or row.get(field) is None:
        return None, f"Meta returned the row without its {field} list"
    tasks = row.get(field)
    if not isinstance(tasks, list):
        return None, f"Meta returned {field} in a shape this server cannot read"
    names: set[str] = set()
    unknown = 0
    for task in tasks:
        name = task.strip().upper() if isinstance(task, str) else None
        if name not in CATALOG_TASKS:
            unknown += 1
            continue
        names.add(name)
    if unknown:
        return None, f"Meta returned {unknown} {field} value(s) this server does not recognise"
    return sorted(names), None


def _task_meanings(tasks: list[str]) -> dict[str, str]:
    return {task: TASK_MEANINGS[task] for task in tasks if task in TASK_MEANINGS}


async def _find_assignment(
    client: Any, catalog_id: str, business_id: str, user_id: str,
) -> dict[str, Any]:
    """Read all of the catalog's direct assignments and look up one user's access."""
    scan = await _scan(
        client, catalog_id, "assigned_users", fields=ASSIGNED_USER_FIELDS, params={"business": business_id},
    )
    return _lookup_identity(scan, user_id, ASSIGNED_USER_FIELDS, "tasks")


def _lookup_identity(scan: dict[str, Any], wanted_id: str, fields: list[str], task_field: str) -> dict[str, Any]:
    """One recipient's current tasks from a scan, and whether they are known.

    The one place a person's or partner's access is looked up by id: the assignment
    lookup, the partner lookup, the one-person and one-partner checks, the read-back
    after a write, and so every preview, write and self-access guard built on them.

    Every row read for the id counts; none is preferred because it came first. The
    answer is known only when:
    - rows for the id were found, each with readable tasks, all holding the same task
      set (compared as a set, so an identical duplicate is fine), and every page was
      read, so no later row could disagree; or
    - no row for the id was found, every page was read, and every row had a usable id.
    Two rows for the id with different tasks, a row whose tasks cannot be read, pages
    left unread, or rows without a usable id make the answer unknown, with the reason.
    """
    matching = [row for row in scan["rows"] if _is_row(row, wanted_id)]
    base = {"scanned": len(scan["rows"]), "requests": scan["requests"], "matching_rows": len(matching)}
    if not matching:
        unidentified = _unidentified_rows(scan)
        known = scan["complete"] and not unidentified
        if known:
            reason = None
        elif scan["complete"]:
            reason = (
                f"Meta returned {unidentified} row(s) without a readable id, so this server cannot tell whether "
                "the recipient is among them"
            )
        else:
            reason = scan["reason"]
        return {**base, "row": None, "tasks": [] if known else None, "known": known, "reason": reason}
    states = [_tasks_state(row, task_field) for row in matching]
    problems = [problem for _, problem in states if problem is not None]
    task_sets = {tuple(tasks) for tasks, problem in states if problem is None}
    copies = f" (one of {len(matching)} rows Meta returned for this recipient)" if len(matching) > 1 else ""
    if problems:
        reason = f"{problems[0]}{copies}, so the current tasks are unknown"
    elif len(task_sets) > 1:
        reason = (
            f"Meta returned {len(matching)} rows for this recipient with different {task_field}, so the current "
            "tasks are unknown"
        )
    elif not scan["complete"]:
        reason = (
            f"Not every row was read ({scan['reason']}), so another row for this recipient could hold different "
            f"{task_field}; the current tasks are unknown"
        )
    else:
        tasks = list(next(iter(task_sets)))
        return {**base, "row": _project(matching[0], fields), "tasks": tasks, "known": True, "reason": None}
    return {**base, "row": _project(matching[0], fields), "tasks": None, "known": False, "reason": reason}


@mcp_server.tool()
async def list_catalog_assigned_users(
    catalog_id: GraphId,
    business_id: GraphId | None = None,
    user_type: str = "PERSON",
    user_id: GraphId | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List who has direct access to a Meta product catalog and with which tasks (MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE), or with user_type=AGENCY the partner businesses it is shared with. Pass user_id to check one person. business_id defaults to the catalog's owning business. Access through a business role or business asset group is not listed. Each person row keeps Meta's user_type and adds user_kind (EMPLOYEE or SYSTEM_USER), the value the other access tools take."""
    catalog_id = _numeric_id(catalog_id, "catalog_id")
    business_id = _optional_numeric_id(business_id, "business_id")
    user_id = _optional_numeric_id(user_id, "user_id")
    user_type = _catalog_recipient_kind(user_type)
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
        response["notes"] = [
            "This view reads the partner businesses only, not the tasks shared with each; "
            "list_catalog_partners also reads the tasks.",
        ]
        return response

    if business_id is None:
        business_id = await _catalog_business_id(client, catalog_id)
    scope = {"catalog_id": catalog_id, "business_id": business_id, "user_type": "PERSON"}
    if user_id is not None:
        found = await _find_assignment(client, catalog_id, business_id, user_id)
        row = found["row"]
        tasks = found["tasks"]
        if not found["known"]:
            status = "UNKNOWN"
        elif row:
            status = "HAS_DIRECT_ACCESS" if tasks else "ASSIGNED_WITHOUT_TASKS"
        else:
            status = "NO_DIRECT_ACCESS"
        result: dict[str, Any] = {
            "scope": {**scope, "user_id": user_id},
            "item": {
                **(row or {"id": user_id}), "user_kind": _row_kind(row or {}),
                "tasks": tasks, "can": _task_meanings(tasks or []),
            },
            "access_status": status,
            "summary": {"scanned": found["scanned"], "requests": found["requests"], "complete": found["known"]},
            "notes": [UNTOUCHED_ACCESS_NOTE, TASK_MEANINGS_NOTE],
        }
        if status == "UNKNOWN":
            result["summary"]["incomplete_reason"] = (
                f"The person's row was found, but {found['reason']}." if row
                else f"The person was not among the rows read: {found['reason']}"
            )
        return result
    params = {**_page_params(limit, after), "business": business_id}
    payload = await client.list_objects(catalog_id, "assigned_users", fields=ASSIGNED_USER_FIELDS, params=params)
    response = _page_response(payload, ASSIGNED_USER_FIELDS)
    unreadable = 0
    for item in response["items"]:
        tasks, problem = _tasks_state(item)
        item["tasks"] = tasks
        if problem is not None:
            unreadable += 1
            item["tasks_unreadable"] = f"{problem}; this person's current tasks are unknown."
        item["user_kind"] = _row_kind(item)
    if unreadable:
        response["summary"]["rows_with_unreadable_tasks"] = unreadable
    response["scope"] = scope
    response["task_meanings"] = dict(TASK_MEANINGS)
    response["notes"] = [UNTOUCHED_ACCESS_NOTE, TASK_MEANINGS_NOTE]
    return response


@mcp_server.tool()
async def list_user_assigned_catalogs(
    user_id: GraphId,
    user_type: str = "EMPLOYEE",
    business_id: GraphId | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List the Meta product catalogs assigned to one person or system user (business-scoped id from list_business_users), or with user_type=AGENCY the catalogs your business (business_id, required) shares with that partner business. The partner view scans your owned catalogs one by one and reports how many it checked. user_type takes EMPLOYEE, SYSTEM_USER or AGENCY, or the user_type or user_kind a catalog listing returned."""
    user_id = _numeric_id(user_id, "user_id")
    user_type = _user_kind(user_type)
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
    # Rows that are not objects are already in owned["reason"]; these are objects without a usable id.
    unidentified_catalogs = _unidentified_rows(owned) - owned["unreadable_rows"]
    if unidentified_catalogs:
        gaps.append(f"{unidentified_catalogs} owned catalog row(s) had no readable id and could not be checked.")
    skipped = 0
    for catalog in catalogs:
        usable = _row_identity(catalog.get("id"))
        if usable is None:
            skipped += 1
            continue
        catalog_id = usable
        try:
            partners = await _scan(client, catalog_id, "agencies", fields=PARTNER_FIELDS, max_pages=3)
        except MetaAdsError:
            unreadable.append(catalog_id)
            continue
        requests += partners["requests"]
        if any(_is_row(row, user_id) for row in partners["rows"]):
            items.append({**_project(catalog, OWNED_CATALOG_FIELDS), "access_type": "AGENCY"})
        elif not partners["complete"]:
            gaps.append(f"Catalog {catalog_id}: partner list not fully read ({partners['reason']}).")
        elif unidentified := _unidentified_rows(partners):
            gaps.append(
                f"Catalog {catalog_id}: {unidentified} partner row(s) had no readable id, so whether it is shared "
                "with this partner is unknown."
            )
    if unreadable:
        gaps.append(f"Partner lists could not be read for catalogs: {', '.join(unreadable)}.")
    complete = not gaps
    summary: dict[str, Any] = {
        "count": len(items[:limit]),
        "matched": len(items),
        "catalogs_checked": len(catalogs) - len(unreadable) - skipped,
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
            "Only catalogs owned by business_id were checked, and only whether each is shared with the partner; "
            "list_catalog_partners reads the tasks shared on one catalog.",
        ],
    }


def _normalize_tasks(tasks: Any) -> list[str]:
    """Return the requested task set, or raise without repeating what was given."""
    accepted = ", ".join(CATALOG_TASKS)
    if tasks is None:
        raise ValidationError(
            f"tasks is required: the complete set the recipient should hold, from {accepted}; "
            "pass an empty list to remove their direct access."
        )
    if isinstance(tasks, str):
        tasks = [part for part in tasks.split(",")] if tasks.strip() else []
    if not isinstance(tasks, list):
        raise ValidationError(f"tasks must be a list of task names from {accepted}.")
    normalized: set[str] = set()
    unknown = 0
    for task in tasks:
        name = task.strip().upper() if isinstance(task, str) else None
        if name not in CATALOG_TASKS:
            unknown += 1
            continue
        normalized.add(name)
    if unknown:
        raise ValidationError(
            f"tasks has {unknown} entr{'y' if unknown == 1 else 'ies'} that are not catalog tasks. "
            f"Accepted values: {accepted}."
        )
    return sorted(normalized)


async def _caller_ids(client: Any) -> set[str] | None:
    """Ids that belong to the token's own user, or None when they cannot all be read.

    One row without a usable id makes the whole set unknown: that row could be
    the recipient, so a partial set must not clear a removal.
    """
    try:
        me = await client.get_object("me", fields=["id"])
        own_id = _row_identity(me.get("id")) if isinstance(me, dict) else None
        scan = await _scan(client, "me", "business_users", fields=["id", "business"], max_pages=3)
    except MetaAdsError:
        return None
    if own_id is None or not scan["complete"] or scan.get("unreadable_rows"):
        return None
    row_ids = [_row_identity(row.get("id")) for row in scan["rows"]]
    if any(row_id is None for row_id in row_ids):
        return None
    return {own_id, *row_ids}  # type: ignore[arg-type]


def _row_business_id(row: dict[str, Any]) -> str | None:
    business = row.get("business")
    return _row_identity(business.get("id")) if isinstance(business, dict) else None


async def _caller_business_ids(client: Any) -> set[str] | None:
    """Businesses the token's own user belongs to, or None when that cannot be read.

    An empty answer is treated as unreadable: a system-user token can see no
    business rows even though it belongs to one. So is any row whose business
    id cannot be read, since that business could be the recipient.
    """
    try:
        scan = await _scan(client, "me", "business_users", fields=["id", "business"], max_pages=3)
    except MetaAdsError:
        return None
    if not scan["complete"] or scan.get("unreadable_rows"):
        return None
    ids = [_row_business_id(row) for row in scan["rows"]]
    if not ids or any(business_id is None for business_id in ids):
        return None
    return set(ids)  # type: ignore[arg-type]


def _access_inputs(catalog_id: Any, user_id: Any, tasks: Any, business_id: Any, user_type: Any) -> dict[str, Any]:
    """Validate the access-change inputs before any network call."""
    inputs = {
        "kind": _catalog_recipient_kind(user_type),
        "catalog_id": _numeric_id(catalog_id, "catalog_id"),
        "user_id": _numeric_id(user_id, "user_id"),
        "business_id": _optional_numeric_id(business_id, "business_id"),
        "proposed": _normalize_tasks(tasks),
    }
    if inputs["kind"] == "AGENCY" and inputs["business_id"] is not None:
        raise ValidationError(
            "business_id is not used with user_type=AGENCY: user_id is the partner business and the share belongs to the catalog."
        )
    return inputs


def _classify(found: dict[str, Any], proposed: list[str]) -> tuple[str, list[str], list[str]]:
    """Name the change from a known current task set, and what is lost and gained."""
    row, previous = found["row"], found["tasks"]
    if not found["known"]:
        return "unknown", [], []
    lost = [task for task in previous if task not in proposed]
    gained = [task for task in proposed if task not in previous]
    if not proposed:
        return ("remove" if row is not None else "nothing_to_remove"), lost, gained
    if row is None:
        return "grant", lost, gained
    if previous == proposed:
        return "no_change", lost, gained
    if not lost:
        return "increase", lost, gained
    if not gained:
        return "reduce", lost, gained
    return "change", lost, gained


def _previous_state(found: dict[str, Any]) -> dict[str, Any]:
    if not found["known"]:
        return {"tasks": None, "has_direct_access": True if found["row"] is not None else None, "can": {}}
    tasks = found["tasks"] or []
    return {"tasks": tasks, "has_direct_access": found["row"] is not None, "can": _task_meanings(tasks)}


def _current_access_read(found: dict[str, Any]) -> dict[str, Any]:
    read = {"complete": found["known"], "rows_scanned": found["scanned"], "requests": found["requests"]}
    if not found["known"]:
        read["incomplete_reason"] = found["reason"]
    return read


async def _plan_access_change(
    client: Any,
    *,
    kind: str,
    catalog_id: str,
    user_id: str,
    proposed: list[str],
    business_id: str | None,
    allow_self_removal: bool,
) -> dict[str, Any]:
    """Read current access and compute the change; shared by the preview and the write."""
    if kind == "AGENCY":
        return await _plan_partner_change(
            client, catalog_id=catalog_id, partner_id=user_id, proposed=proposed, allow_self_removal=allow_self_removal,
        )
    if business_id is None:
        business_id = await _catalog_business_id(client, catalog_id)
    found = await _find_assignment(client, catalog_id, business_id, user_id)
    row = found["row"]
    change, lost, gained = _classify(found, proposed)

    target = {"catalog_id": catalog_id, "business_id": business_id, "user_id": user_id}
    # Meta's ProductCatalog assigned_users reference and SDK 26.0.2 list only user and tasks for
    # POST, and only user for DELETE; business is a parameter of the read alone, so it is not sent.
    if change == "remove":
        request = {"method": "DELETE", "path": f"{catalog_id}/assigned_users", "data": {"user": user_id}}
    elif change in {"grant", "increase", "reduce", "change"}:
        request = {
            "method": "POST", "path": f"{catalog_id}/assigned_users",
            "data": {"user": user_id, "tasks": proposed},
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
                "This person is the token's own user. This change would remove its own catalog "
                "access or its MANAGE task and could lock this server out of the catalog. "
                "Pass allow_self_removal=true only if that is intended."
            )
        elif is_self is None and not allow_self_removal:
            refusal = (
                "The token's own user ids could not all be read, so this server cannot confirm that "
                "this person is not the caller. Pass allow_self_removal=true to proceed after "
                "confirming the recipient is not you."
            )
        elif is_self is True:
            warnings.append("This removes or reduces the token's own access; this server may lose the ability to manage the catalog.")
    if change == "unknown":
        refusal = (
            f"The person's current access could not be read completely ({found['reason']}); "
            "nothing can be changed safely."
        )
        warnings.append(
            "The person's current tasks are unknown, so this server cannot say what the change would remove."
        )
    if change == "nothing_to_remove":
        refusal = "This person has no direct access to the catalog; there is nothing to remove."
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

    async def read_back() -> dict[str, Any]:
        return await _find_assignment(client, catalog_id, business_id, user_id)

    return {
        "target": target,
        "recipient": row or {"id": user_id},
        "previous": _previous_state(found),
        "proposed": {"tasks": proposed, "has_direct_access": bool(proposed), "can": _task_meanings(proposed)},
        "change": change,
        "can_no_longer": _task_meanings(lost),
        "gains": _task_meanings(gained),
        "warnings": warnings,
        "request": request,
        "refusal": refusal,
        "self_check": self_check,
        "current_access_read": _current_access_read(found),
        "notes": [UNTOUCHED_ACCESS_NOTE, TASK_MEANINGS_NOTE],
        "edge": "assigned_users",
        "read_back": read_back,
        "extra": {},
    }


# Partner businesses: sharing a catalog with another business ----------------------------

PARTNER_TASK_FIELDS = ["id", "name", "permitted_tasks"]
PARTNER_VERIFIED_LIVE_REASON = (
    "Meta's reference lists the catalog agencies edge but documents neither its permitted_tasks field "
    "nor sharing a whole catalog through it, so the request shape comes from Meta's Python SDK and has "
    "not been run against the live API; a failure may be the request shape rather than your permissions."
)
PARTNER_ACCESS_NOTE = (
    "Only the partner business's direct share of this catalog is shown or changed. Which of the partner's "
    "own people can use the catalog is set inside the partner's business and is not visible here."
)
PARTNER_TASKS_REJECTED_REASON = (
    "Meta refused the permitted_tasks field on the catalog agencies edge, so partners were read without "
    "their tasks"
)


def _partner_unverified() -> dict[str, Any]:
    return {"verified_live": False, "verified_live_reason": PARTNER_VERIFIED_LIVE_REASON}


async def _read_partners(read: Any) -> tuple[dict[str, Any], bool]:
    """Run a partner read with permitted_tasks, falling back to id and name if Meta refuses the field.

    Returns the read and whether the tasks field was refused. Only Meta's
    invalid-parameter error (code 100) triggers the fallback; anything else is raised.
    """
    try:
        return await read(PARTNER_TASK_FIELDS), False
    except MetaApiError as error:
        if error.code != 100:
            raise _partner_error(error) from error
    try:
        return await read(PARTNER_FIELDS), True
    except MetaApiError as error:
        raise _partner_error(error) from error


PARTNER_ERROR_HINT = (
    "The partner request shape is not yet confirmed against Meta's live API (verified_live=false), "
    "so this error may come from the request shape rather than your permissions."
)


def _partner_error(error: MetaApiError) -> MetaApiError:
    """The same Meta error with a sentence saying the partner request is unconfirmed."""
    return dataclasses.replace(error, message=f"{error.message} {PARTNER_ERROR_HINT}")


def _partner_item(row: dict[str, Any], tasks_rejected: bool) -> dict[str, Any]:
    item = _project(row, PARTNER_TASK_FIELDS)
    tasks, problem = _tasks_state(row, "permitted_tasks")
    item["permitted_tasks"] = tasks
    item["can"] = _task_meanings(tasks or [])
    if problem is not None:
        reason = PARTNER_TASKS_REJECTED_REASON if tasks_rejected else problem
        item["tasks_unreadable"] = f"{reason}; the tasks shared with this partner are unknown."
    return item


async def _find_partner(client: Any, catalog_id: str, partner_id: str) -> dict[str, Any]:
    """Read all of the catalog's partner shares and look up one partner's share."""

    async def read(fields: list[str]) -> dict[str, Any]:
        return await _scan(client, catalog_id, "agencies", fields=fields)

    scan, rejected = await _read_partners(read)
    found = _lookup_identity(scan, partner_id, PARTNER_TASK_FIELDS, "permitted_tasks")
    if rejected and found["row"] is not None:
        found["reason"] = f"{PARTNER_TASKS_REJECTED_REASON}, so the current tasks are unknown"
    found["tasks_field_rejected"] = rejected
    return found


async def _plan_partner_change(
    client: Any,
    *,
    catalog_id: str,
    partner_id: str,
    proposed: list[str],
    allow_self_removal: bool,
) -> dict[str, Any]:
    """Read the partner's current share and compute the change, for preview and write alike."""
    found = await _find_partner(client, catalog_id, partner_id)
    change, lost, gained = _classify(found, proposed)
    if change == "grant":
        change = "share"
    target = {"catalog_id": catalog_id, "user_id": partner_id, "user_type": "AGENCY"}
    path = f"{catalog_id}/agencies"
    if change == "remove":
        request = {"method": "DELETE", "path": path, "data": {"business": partner_id}}
    elif change in {"share", "increase"}:
        request = {"method": "POST", "path": path, "data": {"business": partner_id, "permitted_tasks": proposed}}
    else:
        request = None

    warnings: list[str] = []
    refusal: str | None = None
    self_check: dict[str, Any] = {"checked": False}
    if change == "remove":
        businesses = await _caller_business_ids(client)
        is_self = None if businesses is None else partner_id in businesses
        self_check = {"checked": True, "is_caller_business": is_self}
        if is_self is True and not allow_self_removal:
            refusal = (
                "The token's own user belongs to this partner business, so removing the share could lock "
                "this server out of the catalog. Pass allow_self_removal=true only if that is intended."
            )
        elif is_self is None and not allow_self_removal:
            refusal = (
                "The businesses the token's own user belongs to could not all be read, so this server cannot "
                "confirm that the caller does not use the catalog through this partner. Pass "
                "allow_self_removal=true to proceed after confirming that."
            )
        elif is_self is True:
            warnings.append("This removes the share the token's own business uses; this server may lose access to the catalog.")
    if change in {"reduce", "change"}:
        refusal = (
            "Taking tasks away from a partner is not supported: the only documented share request adds "
            "tasks, and Meta documents no way to remove one task from a share. Remove the whole share with "
            "tasks=[] and share again with the smaller set, or change it in Business Manager."
        )
    if change == "unknown":
        refusal = (
            f"The partner's current share could not be read completely ({found['reason']}); "
            "nothing can be changed safely."
        )
        warnings.append(
            "The partner's current tasks are unknown, so this server cannot say what the change would remove."
        )
    if change == "nothing_to_remove":
        refusal = "This partner business has no direct share of the catalog; there is nothing to remove."
    if lost:
        what = "; ".join(f"{task}: {TASK_MEANINGS[task]}" for task in lost)
        warnings.insert(0, f"After this change partner business {partner_id} can no longer: {what}.")
    if change == "remove":
        warnings.insert(
            1, f"Partner business {partner_id} loses its direct share of catalog {catalog_id}, and its people "
            "lose the access they had through it.",
        )
    if "MANAGE" in gained:
        warnings.append(f"MANAGE gives partner business {partner_id} full control of catalog {catalog_id}.")
    if change == "increase":
        warnings.append(
            "Meta documents sharing and removing, not editing a share; the full new set is sent as a share "
            "and the result is read back to confirm it."
        )

    async def read_back() -> dict[str, Any]:
        return await _find_partner(client, catalog_id, partner_id)

    current_read = _current_access_read(found)
    current_read["tasks_field_refused"] = found["tasks_field_rejected"]
    return {
        "target": target,
        "recipient": found["row"] or {"id": partner_id},
        "previous": _previous_state(found),
        "proposed": {"tasks": proposed, "has_direct_access": bool(proposed), "can": _task_meanings(proposed)},
        "change": change,
        "can_no_longer": _task_meanings(lost),
        "gains": _task_meanings(gained),
        "warnings": warnings,
        "request": request,
        "refusal": refusal,
        "self_check": self_check,
        "current_access_read": current_read,
        "notes": [PARTNER_ACCESS_NOTE, TASK_MEANINGS_NOTE],
        "edge": "agencies",
        "read_back": read_back,
        "extra": _partner_unverified(),
    }


@mcp_server.tool()
async def list_catalog_partners(
    catalog_id: GraphId,
    partner_id: GraphId | None = None,
    search: str | None = None,
    limit: int = 25,
    after: str | None = None,
) -> dict[str, Any]:
    """List the partner businesses a Meta product catalog is shared with and the tasks shared with each (permitted_tasks from MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE). Pass partner_id to check one partner, or search for a name substring or exact id. The request shape is not yet confirmed against Meta's live API: every response carries verified_live=false, so a failure may be the request shape rather than your permissions. Shows direct shares only, not what the partner's own people can do."""
    catalog_id = _numeric_id(catalog_id, "catalog_id")
    partner_id = _optional_numeric_id(partner_id, "partner_id")
    query = blank_to_none(search)
    _validate_limit(limit)
    if partner_id is not None and query is not None:
        raise ValidationError("Pass partner_id or search, not both.")
    client = get_graph_api_client()
    scope: dict[str, Any] = {"catalog_id": catalog_id}
    common = {
        **_partner_unverified(),
        "task_meanings": dict(TASK_MEANINGS),
        "notes": [PARTNER_ACCESS_NOTE, TASK_MEANINGS_NOTE],
    }
    if partner_id is not None:
        found = await _find_partner(client, catalog_id, partner_id)
        row = found["row"]
        if not found["known"]:
            status = "UNKNOWN"
        else:
            status = "SHARED" if row is not None else "NOT_SHARED"
        item = (
            _partner_item(row, found["tasks_field_rejected"]) if row is not None
            else {"id": partner_id, "permitted_tasks": [] if found["known"] else None, "can": {}}
        )
        summary: dict[str, Any] = {"scanned": found["scanned"], "requests": found["requests"], "complete": found["known"]}
        if not found["known"]:
            summary["incomplete_reason"] = (
                f"The partner's row was found, but {found['reason']}." if row is not None
                else f"The partner was not among the rows read: {found['reason']}"
            )
        return {
            "scope": {**scope, "partner_id": partner_id}, "item": item, "share_status": status,
            "summary": summary, **common,
        }
    if query is None:
        async def read_page(fields: list[str]) -> dict[str, Any]:
            return await client.list_objects(catalog_id, "agencies", fields=fields, params=_page_params(limit, after))

        payload, rejected = await _read_partners(read_page)
        response = _page_response(payload, PARTNER_TASK_FIELDS)
    else:
        async def read_all(fields: list[str]) -> dict[str, Any]:
            return await _scan(client, catalog_id, "agencies", fields=fields, after=after)

        scan, rejected = await _read_partners(read_all)
        response = _search_response(scan, PARTNER_TASK_FIELDS, query, ("name",), limit)
    # Items are already projected to id, name and permitted_tasks.
    response["items"] = [_partner_item(item, rejected) for item in response["items"]]
    unreadable = sum(1 for item in response["items"] if "tasks_unreadable" in item)
    if unreadable:
        response["summary"]["rows_with_unreadable_tasks"] = unreadable
    if rejected:
        response["summary"]["tasks_field_refused"] = True
    response["scope"] = scope
    response.update(common)
    return response


@mcp_server.tool(tags={"read-only"})
async def preview_catalog_access_change(
    catalog_id: GraphId,
    user_id: GraphId,
    tasks: StringList,
    business_id: GraphId | None = None,
    allow_self_removal: bool = False,
    user_type: str = "PERSON",
) -> dict[str, Any]:
    """Read-only. Show a person's or system user's current tasks on a Meta product catalog, the proposed set, what they would lose or gain, warnings, and the exact request set_catalog_access would send. Sends no change. tasks is the complete new set from MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE; an empty list means remove direct access. With user_type=AGENCY, user_id is a partner business and the preview covers sharing the catalog with it or removing the share; that request shape is not yet confirmed against Meta's live API (the response says verified_live=false)."""
    inputs = _access_inputs(catalog_id, user_id, tasks, business_id, user_type)
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
        **plan["extra"],
    }


@mcp_server.tool()
async def set_catalog_access(
    catalog_id: GraphId,
    user_id: GraphId,
    tasks: StringList,
    business_id: GraphId | None = None,
    allow_self_removal: bool = False,
    dry_run: bool = False,
    user_type: str = "PERSON",
) -> dict[str, Any]:
    """Use only on an explicit request to grant, change or remove a person's or system user's direct access to a Meta product catalog. tasks is the complete new set (MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE); an empty list removes direct access. Reads current access first, refuses when it cannot be read completely, refuses to remove the token's own access unless allow_self_removal=true, and returns before and after. With user_type=AGENCY, user_id is a partner business: a non-empty set shares the catalog with it or adds tasks, an empty list removes the share, and taking single tasks away is refused. The partner request shape is not yet confirmed against Meta's live API (the response says verified_live=false), so a failure may be the request shape rather than your permissions."""
    inputs = _access_inputs(catalog_id, user_id, tasks, business_id, user_type)
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
        **plan["extra"],
    }
    if plan["change"] == "no_change":
        return {**base, "current": plan["previous"], "sent": False, "message": "The recipient already holds exactly these tasks; nothing was sent."}
    request = plan["request"]
    if dry_run:
        return {**base, "dry_run": True, "sent": False, "would_send": request, "current": plan["proposed"]}
    try:
        if request["method"] == "DELETE":
            meta_response = await client.delete_edge_object(target["catalog_id"], plan["edge"], data=dict(request["data"]))
        else:
            meta_response = await client.create_edge_object(target["catalog_id"], plan["edge"], data=dict(request["data"]))
    except MetaApiError as error:
        if inputs["kind"] != "AGENCY":
            raise
        raise _partner_error(error) from error
    response = {**base, "sent": True, "request_sent": request}
    if inputs["kind"] == "AGENCY":
        response["meta_response"] = meta_response if isinstance(meta_response, dict) else None
    try:
        after = await plan["read_back"]()
    except MetaAdsError:
        after = None
    if after is None or not after["known"]:
        response["current"] = plan["proposed"]
        response["verified"] = False
        detail = f" ({after['reason']})" if after is not None and after.get("reason") else ""
        response["warnings"] = [
            *plan["warnings"],
            f"Meta accepted the request, but the access could not be read back to confirm it{detail}.",
        ]
        return response
    after_tasks = after["tasks"] or []
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
