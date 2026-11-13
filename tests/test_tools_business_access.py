"""Business, people, partner and product catalog access tool tests."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.errors import MetaApiError, ValidationError
from meta_ads_mcp.tools import business_access

PERSON_NAME = "Jordan Example"
PERSON_EMAIL = "jordan@example.com"
CATALOG = "456"
BUSINESS = "123"
PERSON = "789"
CALLER = "555"


def _graph_error() -> MetaApiError:
    return MetaApiError(message="(#200) Permissions error", status_code=403, code=200)


class FakeClient:
    """Fake Graph client recording every call made by the business access tools."""

    def __init__(
        self,
        *,
        objects: dict[str, Any] | None = None,
        edges: dict[tuple[str, str], Any] | None = None,
        errors: dict[Any, Exception] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.objects = objects or {}
        self.edges = edges or {}
        self.errors = errors or {}

    def _maybe_raise(self, key: Any) -> None:
        if key in self.errors:
            raise self.errors[key]

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append({"method": "get_object", "object_id": object_id, "fields": fields})
        self._maybe_raise(object_id)
        return dict(self.objects.get(object_id, {"id": object_id}))

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.calls.append({
            "method": "list_objects", "parent_id": parent_id, "edge": edge, "fields": fields, "params": params,
        })
        self._maybe_raise((parent_id, edge))
        value = self.edges.get((parent_id, edge), {"data": []})
        if callable(value):
            return value(params or {})
        return value

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.calls.append({"method": "create_edge_object", "parent_id": parent_id, "edge": edge, "data": data})
        self._maybe_raise(("POST", parent_id, edge))
        return {"success": True}

    async def delete_edge_object(self, parent_id: str, edge: str, *, data):
        self.calls.append({"method": "delete_edge_object", "parent_id": parent_id, "edge": edge, "data": data})
        self._maybe_raise(("DELETE", parent_id, edge))
        return {"success": True}

    async def update_object(self, object_id: str, *, data):
        self.calls.append({"method": "update_object", "object_id": object_id, "data": data})
        return {"success": True}

    async def delete_object(self, object_id: str):
        self.calls.append({"method": "delete_object", "object_id": object_id})
        return {"success": True}

    def writes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] in {"create_edge_object", "delete_edge_object", "update_object", "delete_object"}]

    def reads_of(self, edge: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("edge") == edge]


def _use(monkeypatch, client: FakeClient) -> FakeClient:
    monkeypatch.setattr(business_access, "get_graph_api_client", lambda: client)
    return client


def _no_client(monkeypatch) -> None:
    monkeypatch.setattr(
        business_access, "get_graph_api_client",
        lambda: pytest.fail("No client should be created for invalid input"),
    )


def run(coro):
    return asyncio.run(coro)


def _page(rows, after=None, more=False):
    payload: dict[str, Any] = {"data": rows}
    if after or more:
        payload["paging"] = {"cursors": {"after": after}}
        if more:
            payload["paging"]["next"] = "https://graph.facebook.com/next"
    return payload


class AssignedUsers:
    """A catalog's assigned_users edge whose rows change after a write."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __call__(self, params):
        return {"data": [dict(row) for row in self.rows]}


def _access_client(previous_tasks: list[str] | None, *, after_tasks: list[str] | None = "same", caller_ids=(CALLER,)):
    """Client for set_catalog_access: the person's row before, and after the write."""
    before = [] if previous_tasks is None else [
        {"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": previous_tasks},
    ]
    edge = AssignedUsers(before)
    client = FakeClient(
        objects={"me": {"id": "100"}},
        edges={
            (CATALOG, "assigned_users"): edge,
            ("me", "business_users"): {"data": [{"id": cid, "business": {"id": BUSINESS}} for cid in caller_ids]},
        },
    )
    original_create, original_delete = client.create_edge_object, client.delete_edge_object

    async def create(parent_id, edge_name, *, data, files=None):
        result = await original_create(parent_id, edge_name, data=data, files=files)
        tasks = data["tasks"] if after_tasks == "same" else after_tasks
        edge.rows = [{"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": tasks}]
        return result

    async def delete(parent_id, edge_name, *, data):
        result = await original_delete(parent_id, edge_name, data=data)
        edge.rows = [] if after_tasks == "same" else [
            {"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": after_tasks},
        ]
        return result

    client.create_edge_object = create
    client.delete_edge_object = delete
    return client


# list_businesses ----------------------------------------------------------------------


def test_list_businesses_reads_me_businesses_one_page(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        ("me", "businesses"): _page([{"id": 1, "name": "Acme", "permitted_roles": ["ADMIN"], "extra": "x"}], "c2", more=True),
    }))
    result = run(business_access.list_businesses(limit=10, after="c1"))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"]) == ("me", "businesses")
    assert call["params"] == {"limit": 10, "after": "c1"}
    assert call["fields"] == ["id", "name", "verification_status", "permitted_roles"]
    assert result["items"] == [{"id": "1", "name": "Acme", "permitted_roles": ["ADMIN"]}]
    assert result["summary"] == {"count": 1, "complete": False, "truncated": True, "next_after": "c2"}


def test_list_businesses_name_search_scans_pages_and_reports_coverage(monkeypatch) -> None:
    pages = {
        None: _page([{"id": "1", "name": "Acme Shoes"}, {"id": "2", "name": "Other"}], "p2", more=True),
        "p2": _page([{"id": "3", "name": "ACME Hats"}]),
    }
    client = _use(monkeypatch, FakeClient(edges={("me", "businesses"): lambda p: pages[p.get("after")]}))
    result = run(business_access.list_businesses(name_contains="acme"))
    assert [item["id"] for item in result["items"]] == ["1", "3"]
    assert result["summary"]["scanned"] == 3
    assert result["summary"]["requests"] == 2
    assert result["summary"]["complete"] is True
    assert [c["params"]["limit"] for c in client.calls] == [100, 100]


def test_list_businesses_search_stops_on_repeated_cursor_and_says_so(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={
        ("me", "businesses"): lambda p: _page([{"id": "1", "name": "Acme"}], "same", more=True),
    }))
    result = run(business_access.list_businesses(name_contains="acme"))
    assert result["summary"]["complete"] is False
    assert "same page cursor" in result["summary"]["incomplete_reason"]


def test_list_businesses_search_without_cursor_is_incomplete(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={
        ("me", "businesses"): {"data": [{"id": "1", "name": "Acme"}], "paging": {"next": "https://x"}},
    }))
    result = run(business_access.list_businesses(name_contains="acme"))
    assert result["summary"]["complete"] is False
    assert "no cursor" in result["summary"]["incomplete_reason"]


def test_list_businesses_search_caps_requests(monkeypatch) -> None:
    counter = iter(range(1000))
    client = _use(monkeypatch, FakeClient(edges={
        ("me", "businesses"): lambda p: _page([{"id": "1", "name": "x"}], f"c{next(counter)}", more=True),
    }))
    result = run(business_access.list_businesses(name_contains="zzz"))
    assert len(client.calls) == business_access.MAX_SCAN_PAGES
    assert result["summary"]["complete"] is False
    assert result["summary"]["next_after"]


@pytest.mark.parametrize("limit", [0, 101])
def test_list_businesses_rejects_bad_limit_before_any_call(monkeypatch, limit) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(business_access.list_businesses(limit=limit))


# list_business_users ------------------------------------------------------------------


def test_business_user_listing_returns_only_identifying_fields(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        (BUSINESS, "business_users"): _page([{
            "id": PERSON, "name": PERSON_NAME, "email": PERSON_EMAIL, "role": "EMPLOYEE",
            "first_name": "Jordan", "last_name": "Example", "title": "Buyer",
            "two_fac_status": "enabled", "pending_email": "old@example.com",
        }]),
    }))
    result = run(business_access.list_business_users(business_id=BUSINESS))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"]) == (BUSINESS, "business_users")
    assert call["fields"] == ["id", "name", "email", "role"]
    assert result["items"] == [{"id": PERSON, "name": PERSON_NAME, "email": PERSON_EMAIL, "role": "EMPLOYEE"}]
    assert result["scope"] == {"business_id": BUSINESS, "user_type": "EMPLOYEE"}


@pytest.mark.parametrize(("user_type", "edge", "fields"), [
    ("SYSTEM_USER", "system_users", ["id", "name", "role"]),
    ("AGENCY", "agencies", ["id", "name"]),
])
def test_business_user_listing_other_rosters(monkeypatch, user_type, edge, fields) -> None:
    client = _use(monkeypatch, FakeClient(edges={(BUSINESS, edge): _page([{"id": "9", "name": "Robot", "role": "ADMIN", "email": "x@y"}])}))
    result = run(business_access.list_business_users(business_id=BUSINESS, user_type=user_type))
    assert client.calls[0]["edge"] == edge
    assert client.calls[0]["fields"] == fields
    assert set(result["items"][0]) <= set(fields)


def test_business_user_search_matches_name_or_email_but_system_users_name_only(monkeypatch) -> None:
    rows = [
        {"id": "1", "name": "Pat", "email": "jordan@example.com", "role": "ADMIN"},
        {"id": "2", "name": "Jordan B", "email": "b@example.com", "role": "EMPLOYEE"},
        {"id": "3", "name": "Sam", "email": "sam@example.com", "role": "EMPLOYEE"},
    ]
    _use(monkeypatch, FakeClient(edges={(BUSINESS, "business_users"): _page(rows), (BUSINESS, "system_users"): _page(rows)}))
    people = run(business_access.list_business_users(business_id=BUSINESS, search="JORDAN"))
    assert [i["id"] for i in people["items"]] == ["1", "2"]
    robots = run(business_access.list_business_users(business_id=BUSINESS, user_type="SYSTEM_USER", search="jordan"))
    assert [i["id"] for i in robots["items"]] == ["2"]
    exact = run(business_access.list_business_users(business_id=BUSINESS, search="3"))
    assert [i["id"] for i in exact["items"]] == ["3"]


@pytest.mark.parametrize("kwargs", [
    {"business_id": "biz_1"},
    {"business_id": " "},
    {"business_id": BUSINESS, "user_type": "OWNER"},
    {"business_id": BUSINESS, "limit": 0},
])
def test_business_user_listing_validates_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(business_access.list_business_users(**kwargs))


# list_catalog_assigned_users ----------------------------------------------------------


def test_catalog_assigned_users_lists_people_with_tasks(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        (CATALOG, "assigned_users"): _page([{"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": ["MANAGE", "ADVERTISE"]}]),
    }))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, limit=5))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"]) == (CATALOG, "assigned_users")
    assert call["params"] == {"limit": 5, "business": BUSINESS}
    assert call["fields"] == ["id", "name", "user_type", "tasks"]
    assert result["items"][0]["tasks"] == ["ADVERTISE", "MANAGE"]
    assert result["summary"]["complete"] is True
    assert set(result["task_meanings"]) == {"MANAGE", "ADVERTISE", "MANAGE_AR", "AA_ANALYZE"}


def test_catalog_assigned_users_defaults_business_to_catalog_owner(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(objects={CATALOG: {"id": CATALOG, "business": {"id": "321"}}}))
    run(business_access.list_catalog_assigned_users(catalog_id=CATALOG))
    assert client.calls[0] == {"method": "get_object", "object_id": CATALOG, "fields": ["id", "business"]}
    assert client.calls[1]["params"]["business"] == "321"


def test_catalog_assigned_users_without_owner_asks_for_business(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(objects={CATALOG: {"id": CATALOG}}))
    with pytest.raises(ValidationError, match="pass business_id"):
        run(business_access.list_catalog_assigned_users(catalog_id=CATALOG))
    assert len(client.calls) == 1


def test_catalog_assigned_users_one_person_check(monkeypatch) -> None:
    pages = {
        None: _page([{"id": "1", "name": "A", "tasks": ["ADVERTISE"]}], "p2", more=True),
        "p2": _page([{"id": PERSON, "name": PERSON_NAME, "tasks": ["MANAGE"]}], "p3", more=True),
    }
    client = _use(monkeypatch, FakeClient(edges={(CATALOG, "assigned_users"): lambda p: pages[p.get("after")]}))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON))
    assert result["access_status"] == "HAS_DIRECT_ACCESS"
    assert result["item"]["tasks"] == ["MANAGE"]
    assert "MANAGE" in result["item"]["can"]
    assert len(client.calls) == 2


def test_catalog_assigned_users_one_person_absent_from_complete_list(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={(CATALOG, "assigned_users"): _page([{"id": "1", "tasks": ["ADVERTISE"]}])}))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON))
    assert result["access_status"] == "NO_DIRECT_ACCESS"


def test_catalog_assigned_users_one_person_unknown_when_read_incomplete(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={(CATALOG, "assigned_users"): {"data": [], "paging": {"next": "https://x"}}}))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON))
    assert result["access_status"] == "UNKNOWN"
    assert result["summary"]["complete"] is False
    assert "incomplete_reason" in result["summary"]


def test_catalog_partner_listing_reads_catalog_agencies(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): _page([{"id": 77, "name": "Agency"}])}))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, user_type="AGENCY"))
    assert (client.calls[0]["edge"], client.calls[0]["fields"]) == ("agencies", ["id", "name"])
    assert result["items"] == [{"id": "77", "name": "Agency"}]


def test_catalog_partner_listing_rejects_business_id(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_type="AGENCY"))


# list_user_assigned_catalogs ----------------------------------------------------------


@pytest.mark.parametrize("user_type", ["EMPLOYEE", "SYSTEM_USER"])
def test_user_assigned_catalogs_reads_assigned_product_catalogs(monkeypatch, user_type) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        (PERSON, "assigned_product_catalogs"): _page([{"id": CATALOG, "name": "Shoes", "access_type": "OWNER"}]),
    }))
    result = run(business_access.list_user_assigned_catalogs(user_id=PERSON, user_type=user_type))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"]) == (PERSON, "assigned_product_catalogs")
    assert call["fields"] == ["id", "name", "access_type"]
    assert result["items"] == [{"id": CATALOG, "name": "Shoes", "access_type": "OWNER"}]


def test_user_assigned_catalogs_for_partner_scans_owned_catalogs(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        (BUSINESS, "owned_product_catalogs"): _page([{"id": "1", "name": "A"}, {"id": "2", "name": "B"}, {"id": "3", "name": "C"}]),
        ("1", "agencies"): _page([{"id": "77"}]),
        ("2", "agencies"): _page([{"id": "88"}]),
    }, errors={("3", "agencies"): _graph_error()}))
    result = run(business_access.list_user_assigned_catalogs(user_id="77", user_type="AGENCY", business_id=BUSINESS))
    assert result["items"] == [{"id": "1", "name": "A", "access_type": "AGENCY"}]
    assert client.calls[0]["params"]["limit"] == business_access.MAX_CATALOGS_SCANNED
    assert result["summary"]["catalogs_checked"] == 2
    assert result["summary"]["complete"] is False
    assert "3" in result["summary"]["incomplete_reason"]


def test_user_assigned_catalogs_partner_scan_reports_more_catalogs(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={
        (BUSINESS, "owned_product_catalogs"): _page([{"id": "1", "name": "A"}], "next1", more=True),
    }))
    result = run(business_access.list_user_assigned_catalogs(user_id="77", user_type="AGENCY", business_id=BUSINESS))
    assert result["summary"]["complete"] is False
    assert result["summary"]["next_after"] == "next1"


def test_user_assigned_catalogs_partner_requires_business(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="business_id"):
        run(business_access.list_user_assigned_catalogs(user_id="77", user_type="AGENCY"))


# preview_catalog_access_change and set_catalog_access --------------------------------


def test_set_catalog_access_reads_current_access_first_and_returns_before_and_after(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["ADVERTISE"]))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE", "MANAGE"],
    ))
    methods = [c["method"] for c in client.calls]
    assert methods[0] == "list_objects" and client.calls[0]["edge"] == "assigned_users"
    assert methods.index("create_edge_object") > 0
    write = client.writes()[0]
    assert (write["parent_id"], write["edge"]) == (CATALOG, "assigned_users")
    assert write["data"] == {"user": PERSON, "business": BUSINESS, "tasks": ["ADVERTISE", "MANAGE"]}
    assert result["previous"]["tasks"] == ["ADVERTISE"]
    assert result["current"]["tasks"] == ["ADVERTISE", "MANAGE"]
    assert result["change"] == "increase"
    assert result["verified"] is True
    assert result["ok"] is True
    assert methods[-1] == "list_objects"  # read back after the write


def test_set_catalog_access_grant_to_person_without_access(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(None))
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks="advertise"))
    assert result["change"] == "grant"
    assert result["previous"] == {"tasks": [], "has_direct_access": False, "can": {}}
    assert client.writes()[0]["data"]["tasks"] == ["ADVERTISE"]


def test_set_catalog_access_removal_states_what_is_lost_and_deletes(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["MANAGE", "ADVERTISE"]))
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=[]))
    write = client.writes()[0]
    assert write["method"] == "delete_edge_object"
    assert write["data"] == {"user": PERSON, "business": BUSINESS}
    assert result["change"] == "remove"
    assert set(result["can_no_longer"]) == {"MANAGE", "ADVERTISE"}
    assert "can no longer" in result["warnings"][0]
    assert "loses all direct access" in result["warnings"][1]
    assert result["current"] == {"tasks": [], "has_direct_access": False, "can": {}}
    # The caller identity was checked because this removes access.
    assert any(c.get("object_id") == "me" for c in client.calls)


def test_set_catalog_access_reduction_states_what_is_lost(monkeypatch) -> None:
    _use(monkeypatch, _access_client(["MANAGE", "ADVERTISE"]))
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"]))
    assert result["change"] == "reduce"
    assert list(result["can_no_longer"]) == ["MANAGE"]
    assert result["warnings"][0].startswith(f"After this change user {PERSON} can no longer: MANAGE")


def test_set_catalog_access_reports_when_meta_did_not_replace_the_set(monkeypatch) -> None:
    _use(monkeypatch, _access_client(["MANAGE", "ADVERTISE"], after_tasks=["MANAGE", "ADVERTISE"]))
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"]))
    assert result["verified"] is False
    assert result["ok"] is False
    assert "did not fully take effect" in result["warnings"][-1]


def test_set_catalog_access_unconfirmed_after_read(monkeypatch) -> None:
    client = _access_client(["ADVERTISE"])
    calls = {"n": 0}
    edge = client.edges[(CATALOG, "assigned_users")]

    def flaky(params):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _graph_error()
        return edge(params)

    client.edges[(CATALOG, "assigned_users")] = flaky
    _use(monkeypatch, client)
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE", "MANAGE"]))
    assert result["verified"] is False
    assert result["sent"] is True
    assert "could not be read back" in result["warnings"][-1]


def test_set_catalog_access_does_not_write_when_the_current_read_fails(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(errors={(CATALOG, "assigned_users"): _graph_error()}))
    with pytest.raises(MetaApiError):
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"]))
    assert client.writes() == []


def test_set_catalog_access_refuses_when_current_access_is_unknown(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={(CATALOG, "assigned_users"): {"data": [], "paging": {"next": "https://x"}}}))
    with pytest.raises(ValidationError, match="could not be read completely"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"]))
    assert client.writes() == []


def test_set_catalog_access_dry_run_sends_nothing(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["MANAGE"]))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"], dry_run=True,
    ))
    assert client.writes() == []
    assert result["sent"] is False and result["dry_run"] is True
    assert result["would_send"] == {
        "method": "POST", "path": f"{CATALOG}/assigned_users",
        "data": {"user": PERSON, "business": BUSINESS, "tasks": ["ADVERTISE"]},
    }
    assert result["previous"]["tasks"] == ["MANAGE"]


def test_set_catalog_access_no_change_sends_nothing(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["ADVERTISE"]))
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["advertise"]))
    assert result["change"] == "no_change"
    assert result["sent"] is False
    assert client.writes() == []


def test_set_catalog_access_refuses_to_remove_nothing(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(None))
    with pytest.raises(ValidationError, match="nothing to remove"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=[]))
    assert client.writes() == []


@pytest.mark.parametrize("tasks", [[], ["ADVERTISE"]])
def test_set_catalog_access_refuses_own_removal_or_manage_loss(monkeypatch, tasks) -> None:
    client = _use(monkeypatch, _access_client(["MANAGE", "ADVERTISE"], caller_ids=(PERSON,)))
    with pytest.raises(ValidationError, match="allow_self_removal") as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=tasks))
    assert client.writes() == []
    assert PERSON_NAME not in str(error.value)


def test_set_catalog_access_self_removal_allowed_when_explicit(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["MANAGE"], caller_ids=(PERSON,)))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=[], allow_self_removal=True,
    ))
    assert client.writes()[0]["method"] == "delete_edge_object"
    assert any("token's own access" in warning for warning in result["warnings"])


def test_set_catalog_access_app_scoped_caller_id_counts_as_self(monkeypatch) -> None:
    client = _access_client(["MANAGE"], caller_ids=())
    client.objects["me"] = {"id": PERSON}
    _use(monkeypatch, client)
    with pytest.raises(ValidationError, match="token's own user"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=[]))


def test_set_catalog_access_refuses_risky_change_when_caller_cannot_be_identified(monkeypatch) -> None:
    client = _access_client(["MANAGE"])
    client.errors[("me", "business_users")] = _graph_error()
    _use(monkeypatch, client)
    with pytest.raises(ValidationError, match="cannot confirm"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=[]))
    assert client.writes() == []


def test_set_catalog_access_grant_does_not_need_caller_identity(monkeypatch) -> None:
    client = _access_client(None)
    client.errors["me"] = _graph_error()
    _use(monkeypatch, client)
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"]))
    assert result["sent"] is True
    assert not any(c.get("object_id") == "me" for c in client.calls)


@pytest.mark.parametrize("tasks", [["ADMIN"], ["advertise", "EDIT"], None, "MANAGE,  ,"])
def test_set_catalog_access_rejects_unknown_tasks_listing_accepted_values(monkeypatch, tasks) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=tasks))
    assert "MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE" in str(error.value)


@pytest.mark.parametrize("kwargs", [
    {"catalog_id": "cat_1", "user_id": PERSON},
    {"catalog_id": CATALOG, "user_id": "act_1"},
    {"catalog_id": CATALOG, "user_id": PERSON, "business_id": "biz"},
])
def test_set_catalog_access_rejects_non_numeric_ids(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(business_access.set_catalog_access(tasks=["ADVERTISE"], **kwargs))


def test_preview_shares_the_plan_with_set_and_sends_nothing(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["MANAGE", "ADVERTISE"]))
    preview = run(business_access.preview_catalog_access_change(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"],
    ))
    assert client.writes() == []
    assert preview["read_only"] is True and preview["sent"] is False
    dry = run(business_access.set_catalog_access(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"], dry_run=True,
    ))
    for key in ("previous", "change", "can_no_longer", "gains", "warnings"):
        assert preview[key] == dry[key]
    assert preview["would_send"] == dry["would_send"]
    assert preview["recipient"] == {"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": ["MANAGE", "ADVERTISE"]}


def test_preview_reports_a_refusal_instead_of_raising(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["MANAGE"], caller_ids=(PERSON,)))
    preview = run(business_access.preview_catalog_access_change(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=[],
    ))
    assert preview["would_be_refused"] is True
    assert "allow_self_removal" in preview["refusal_reason"]
    assert preview["self_check"] == {"checked": True, "is_caller": True}
    assert client.writes() == []


def test_preview_grant_of_manage_warns(monkeypatch) -> None:
    _use(monkeypatch, _access_client(None))
    preview = run(business_access.preview_catalog_access_change(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["MANAGE"]))
    assert preview["change"] == "grant"
    assert any("full control" in warning for warning in preview["warnings"])


def test_access_tools_never_put_names_or_emails_in_errors_or_logs(monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client = _use(monkeypatch, _access_client(["MANAGE"], caller_ids=(PERSON,)))
    messages: list[str] = []
    for tasks in ([], ["ADVERTISE"], ["BOGUS"]):
        try:
            run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=tasks))
        except ValidationError as error:
            messages.append(str(error))
    run(business_access.list_business_users(business_id=BUSINESS))
    text = " ".join(messages) + caplog.text
    assert messages
    assert PERSON_NAME not in text and PERSON_EMAIL not in text
    assert client.writes() == []
    source = inspect.getsource(business_access)
    assert "logger" not in source and "logging" not in source


# Read-only mode and registration -------------------------------------------------------


def _contracts():
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.tool_contracts import build_tool_contracts

    components = mcp_server.local_provider._components.values()
    return build_tool_contracts([c for c in components if getattr(c, "name", None)])


@pytest.fixture
def read_only_mode(monkeypatch):
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    yield
    monkeypatch.delenv("META_READ_ONLY")
    reload_settings()


@pytest.mark.parametrize("routed", [False, True])
def test_preview_catalog_access_change_is_read_only_allowed(monkeypatch, read_only_mode, routed) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.read_only import read_only_tool_names

    client = _use(monkeypatch, _access_client(["MANAGE"]))
    assert "preview_catalog_access_change" in read_only_tool_names()
    assert _contracts()["preview_catalog_access_change"].effect == "read"
    arguments = {"catalog_id": CATALOG, "business_id": BUSINESS, "user_id": PERSON, "tasks": ["ADVERTISE"]}
    name = "preview_catalog_access_change"
    tool, payload = ("call_tool", {"name": name, "arguments": arguments}) if routed else (name, arguments)
    result = run(mcp_server.call_tool(tool, payload))
    assert result.structured_content["read_only"] is True
    assert client.writes() == []


@pytest.mark.parametrize("routed", [False, True])
def test_set_catalog_access_is_blocked_in_read_only(monkeypatch, read_only_mode, routed) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.read_only import read_only_tool_names

    _no_client(monkeypatch)
    assert "set_catalog_access" not in read_only_tool_names()
    assert _contracts()["set_catalog_access"].effect == "write"
    arguments = {"catalog_id": CATALOG, "business_id": BUSINESS, "user_id": PERSON, "tasks": [], "dry_run": True}
    name = "set_catalog_access"
    tool, payload = ("call_tool", {"name": name, "arguments": arguments}) if routed else (name, arguments)
    with pytest.raises(ToolError, match="META_READ_ONLY"):
        run(mcp_server.call_tool(tool, payload))


def test_access_tools_are_grouped_reads_in_access_and_the_write_in_writes() -> None:
    from meta_ads_mcp.tools.utility import TOOL_GROUPS

    reads = {
        "list_businesses", "list_business_users", "list_catalog_assigned_users",
        "list_user_assigned_catalogs", "preview_catalog_access_change",
    }
    assert set(TOOL_GROUPS["access"]) == reads
    assert "set_catalog_access" in TOOL_GROUPS["writes"]
    other_groups = [names for group, names in TOOL_GROUPS.items() if group != "writes"]
    assert all("set_catalog_access" not in names for names in other_groups)
    contracts = _contracts()
    assert all(contracts[name].effect == "read" for name in reads)


def test_ids_use_graph_id_at_the_mcp_boundary() -> None:
    from meta_ads_mcp.coordinator import mcp_server

    tool = run(mcp_server.get_tool("set_catalog_access"))
    props = tool.parameters["properties"]
    assert props["catalog_id"]["pattern"] == "^[0-9]+$"
    assert props["user_id"]["pattern"] == "^[0-9]+$"


def test_intent_guide_and_mutation_paths_cover_access_tools(monkeypatch) -> None:
    from meta_ads_mcp.tools import utility

    route = utility.INTENT_GUIDE["manage_business_people_and_catalog_access"]
    routed = set(route["recommended_order"]) | set(route["avoid_unless_needed"])
    assert set(utility.TOOL_GROUPS["access"]) | {"set_catalog_access"} <= routed
    monkeypatch.delenv("META_READ_ONLY", raising=False)
    reload_settings()
    paths = run(utility.list_mutation_tools())["common_paths"]
    assert paths["product_catalog_access"] == ["set_catalog_access"]


def test_docstrings_have_no_plus_character() -> None:
    for name in (
        "list_businesses", "list_business_users", "list_catalog_assigned_users",
        "list_user_assigned_catalogs", "preview_catalog_access_change", "set_catalog_access",
    ):
        assert "+" not in (inspect.getdoc(getattr(business_access, name)) or "")


# Vocabulary shared between the access tools, and empty business lists -------------------


def test_catalog_assigned_users_adds_user_kind_beside_metas_user_type(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={
        (CATALOG, "assigned_users"): _page([
            {"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": ["ADVERTISE"]},
            {"id": "790", "name": "Robot", "user_type": "system_user", "tasks": ["MANAGE"]},
            {"id": "791", "name": "Odd", "user_type": "something_new", "tasks": []},
        ]),
    }))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS))
    items = {item["id"]: item for item in result["items"]}
    assert items[PERSON]["user_type"] == "business_user"
    assert items[PERSON]["user_kind"] == "EMPLOYEE"
    assert items["790"]["user_type"] == "system_user"
    assert items["790"]["user_kind"] == "SYSTEM_USER"
    assert items["791"]["user_type"] == "something_new"
    assert items["791"]["user_kind"] is None


def test_catalog_assigned_users_one_person_check_adds_user_kind(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={
        (CATALOG, "assigned_users"): _page([{"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": ["MANAGE"]}]),
    }))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON))
    assert result["item"]["user_type"] == "business_user"
    assert result["item"]["user_kind"] == "EMPLOYEE"


@pytest.mark.parametrize(("given", "edge"), [
    ("business_user", "business_users"),
    ("BUSINESS_USER", "business_users"),
    ("employee", "business_users"),
    ("system_user", "system_users"),
    ("agency", "agencies"),
])
def test_business_user_listing_accepts_metas_user_type_values(monkeypatch, given, edge) -> None:
    client = _use(monkeypatch, FakeClient())
    run(business_access.list_business_users(business_id=BUSINESS, user_type=given))
    assert client.calls[0]["edge"] == edge


@pytest.mark.parametrize("given", ["business_user", "system_user", "EMPLOYEE"])
def test_user_assigned_catalogs_accepts_the_user_type_list_catalog_assigned_users_returned(monkeypatch, given) -> None:
    client = _use(monkeypatch, FakeClient())
    result = run(business_access.list_user_assigned_catalogs(user_id=PERSON, user_type=given))
    assert client.calls[0]["edge"] == "assigned_product_catalogs"
    assert result["scope"]["user_type"] in {"EMPLOYEE", "SYSTEM_USER"}


@pytest.mark.parametrize("given", ["business_user", "SYSTEM_USER", "person"])
def test_catalog_assigned_users_accepts_person_kinds_as_user_type(monkeypatch, given) -> None:
    client = _use(monkeypatch, FakeClient())
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_type=given))
    assert client.calls[0]["edge"] == "assigned_users"
    assert result["scope"]["user_type"] == "PERSON"


@pytest.mark.parametrize("tool", ["list_business_users", "list_user_assigned_catalogs", "list_catalog_assigned_users"])
def test_unknown_user_type_lists_accepted_values_before_any_call(monkeypatch, tool) -> None:
    _no_client(monkeypatch)
    kwargs = {
        "list_business_users": {"business_id": BUSINESS},
        "list_user_assigned_catalogs": {"user_id": PERSON},
        "list_catalog_assigned_users": {"catalog_id": CATALOG, "business_id": BUSINESS},
    }[tool]
    with pytest.raises(ValidationError) as error:
        run(getattr(business_access, tool)(user_type="pending_user", **kwargs))
    message = str(error.value)
    assert "BUSINESS_USER" in message and "SYSTEM_USER" in message and "AGENCY" in message


def test_user_type_from_list_catalog_assigned_users_passes_mcp_validation(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    client = _use(monkeypatch, FakeClient())
    result = run(mcp_server.call_tool(
        "list_user_assigned_catalogs", {"user_id": PERSON, "user_type": "business_user"},
    ))
    assert result.structured_content["scope"]["user_type"] == "EMPLOYEE"
    assert client.calls[0]["edge"] == "assigned_product_catalogs"


def test_list_businesses_explains_an_empty_list_without_failing(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={("me", "businesses"): {"data": []}}))
    result = run(business_access.list_businesses())
    assert result["items"] == []
    assert result["summary"]["complete"] is True
    note = " ".join(result["notes"])
    assert "system-user token" in note
    assert 'get_ad_account(fields=["business"])' in note


def test_list_businesses_name_search_with_no_businesses_explains_too(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={("me", "businesses"): {"data": []}}))
    result = run(business_access.list_businesses(name_contains="acme"))
    assert 'get_ad_account(fields=["business"])' in " ".join(result["notes"])


def test_list_businesses_with_results_has_no_empty_list_note(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={("me", "businesses"): _page([{"id": "1", "name": "Acme"}])}))
    result = run(business_access.list_businesses())
    assert "notes" not in result
