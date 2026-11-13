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
        "list_user_assigned_catalogs", "preview_catalog_access_change", "list_catalog_partners",
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
        "list_catalog_partners",
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


# Round-three review fixes: unreadable current tasks (G4) and echoed input (G3) ----------


def test_g4_missing_current_tasks_prevents_access_write(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        ("10", "assigned_users"): {"data": [{"id": "20", "name": "Synthetic Person"}]},
    }))
    try:
        run(business_access.set_catalog_access("10", "20", ["ADVERTISE"], business_id="30"))
    except ValidationError:
        pass
    assert client.writes() == [], client.calls


def test_g3_access_task_validation_never_echoes_person() -> None:
    raw = "synthetic.private.person@example.invalid"
    with pytest.raises(ValidationError) as info:
        run(business_access.set_catalog_access("10", "20", [raw], business_id="30"))
    assert raw not in str(info.value), str(info.value)


UNREADABLE_TASK_SHAPES = [
    pytest.param({}, id="tasks-missing"),
    pytest.param({"tasks": None}, id="tasks-null"),
    pytest.param({"tasks": "MANAGE"}, id="tasks-a-string"),
    pytest.param({"tasks": {"MANAGE": True}}, id="tasks-an-object"),
    pytest.param({"tasks": ["MANAGE", "SUPER_ADMIN"]}, id="tasks-with-an-unknown-name"),
    pytest.param({"tasks": [7]}, id="tasks-with-a-number"),
    pytest.param({"tasks": [None]}, id="tasks-with-a-null"),
    pytest.param({"tasks": [PERSON_EMAIL]}, id="tasks-with-text-that-is-not-a-task"),
]


def _row_client(extra: dict[str, Any]) -> FakeClient:
    row = {"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", **extra}
    return FakeClient(
        objects={"me": {"id": "100"}},
        edges={
            (CATALOG, "assigned_users"): {"data": [row]},
            ("me", "business_users"): {"data": [{"id": CALLER, "business": {"id": BUSINESS}}]},
        },
    )


@pytest.mark.parametrize("extra", UNREADABLE_TASK_SHAPES)
@pytest.mark.parametrize("tasks", [["ADVERTISE"], ["MANAGE", "ADVERTISE"], []])
def test_unreadable_current_tasks_refuse_every_write(monkeypatch, extra, tasks) -> None:
    client = _use(monkeypatch, _row_client(extra))
    with pytest.raises(ValidationError, match="could not be read completely") as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=tasks))
    assert client.writes() == []
    message = str(error.value)
    assert PERSON_EMAIL not in message and "SUPER_ADMIN" not in message and PERSON_NAME not in message


@pytest.mark.parametrize("extra", UNREADABLE_TASK_SHAPES)
def test_unreadable_current_tasks_make_the_preview_refuse_the_same_way(monkeypatch, extra) -> None:
    client = _use(monkeypatch, _row_client(extra))
    preview = run(business_access.preview_catalog_access_change(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"],
    ))
    assert client.writes() == []
    assert preview["change"] == "unknown"
    assert preview["would_be_refused"] is True
    assert preview["would_send"] is None
    assert preview["previous"] == {"tasks": None, "has_direct_access": True, "can": {}}
    assert preview["current_access_read"]["complete"] is False
    assert "current tasks are unknown" in preview["current_access_read"]["incomplete_reason"]
    assert any("cannot say what the change would remove" in w for w in preview["warnings"])


def test_empty_task_list_on_a_found_row_is_readable(monkeypatch) -> None:
    client = _use(monkeypatch, _row_client({"tasks": []}))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"], dry_run=True,
    ))
    assert result["change"] == "increase"
    assert result["previous"] == {"tasks": [], "has_direct_access": True, "can": {}}
    assert client.writes() == []


def test_read_back_with_unreadable_tasks_is_not_verified(monkeypatch) -> None:
    client = _access_client(["ADVERTISE"])
    original = client.create_edge_object

    async def create(parent_id, edge_name, *, data, files=None):
        result = await original(parent_id, edge_name, data=data, files=files)
        client.edges[(CATALOG, "assigned_users")].rows = [{"id": PERSON, "name": PERSON_NAME, "tasks": ["MANAGE", "NEW_THING"]}]
        return result

    client.create_edge_object = create
    _use(monkeypatch, client)
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE", "MANAGE"],
    ))
    assert result["sent"] is True
    assert result["verified"] is False
    assert "could not be read back" in result["warnings"][-1]
    assert "does not recognise" in result["warnings"][-1]


def test_catalog_listing_marks_unreadable_tasks_instead_of_showing_none(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={
        (CATALOG, "assigned_users"): _page([
            {"id": PERSON, "name": PERSON_NAME, "user_type": "business_user", "tasks": ["advertise"]},
            {"id": "790", "name": "Robot", "user_type": "system_user"},
            {"id": "791", "name": "Odd", "user_type": "system_user", "tasks": ["MANAGE", "OWNER"]},
        ]),
    }))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS))
    items = {item["id"]: item for item in result["items"]}
    assert items[PERSON]["tasks"] == ["ADVERTISE"] and "tasks_unreadable" not in items[PERSON]
    assert items["790"]["tasks"] is None and "without its tasks list" in items["790"]["tasks_unreadable"]
    assert items["791"]["tasks"] is None and "OWNER" not in items["791"]["tasks_unreadable"]
    assert result["summary"]["rows_with_unreadable_tasks"] == 2


@pytest.mark.parametrize("extra", UNREADABLE_TASK_SHAPES)
def test_one_person_check_with_unreadable_tasks_is_unknown(monkeypatch, extra) -> None:
    _use(monkeypatch, _row_client(extra))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON))
    assert result["access_status"] == "UNKNOWN"
    assert result["item"]["tasks"] is None
    assert result["summary"]["complete"] is False
    assert result["summary"]["incomplete_reason"].startswith("The person's row was found")


SECRET = "synthetic.private.person@example.invalid"


@pytest.mark.parametrize(("tool", "kwargs"), [
    ("list_business_users", {"business_id": BUSINESS, "user_type": SECRET}),
    ("list_user_assigned_catalogs", {"user_id": PERSON, "user_type": SECRET}),
    ("list_catalog_assigned_users", {"catalog_id": CATALOG, "user_type": SECRET}),
    ("list_catalog_assigned_users", {"catalog_id": CATALOG, "user_id": SECRET}),
    ("list_business_users", {"business_id": SECRET}),
    ("list_catalog_partners", {"catalog_id": SECRET}),
    ("list_catalog_partners", {"catalog_id": CATALOG, "partner_id": SECRET}),
    ("preview_catalog_access_change", {"catalog_id": CATALOG, "user_id": PERSON, "tasks": ["MANAGE", SECRET]}),
    ("preview_catalog_access_change", {"catalog_id": CATALOG, "user_id": PERSON, "tasks": [], "user_type": SECRET}),
    ("set_catalog_access", {"catalog_id": CATALOG, "user_id": SECRET, "tasks": []}),
    ("set_catalog_access", {"catalog_id": CATALOG, "user_id": PERSON, "tasks": SECRET}),
    ("set_catalog_access", {"catalog_id": CATALOG, "user_id": PERSON, "tasks": [], "business_id": SECRET}),
])
def test_validation_messages_never_copy_caller_text(monkeypatch, tool, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as error:
        run(getattr(business_access, tool)(**kwargs))
    assert SECRET not in str(error.value)
    assert "synthetic" not in str(error.value).lower()


def test_unknown_task_message_counts_entries_and_lists_accepted_values(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PERSON, tasks=["MANAGE", SECRET, "x", 5]))
    message = str(error.value)
    assert message.startswith("tasks has 3 entries that are not catalog tasks.")
    assert "MANAGE, ADVERTISE, MANAGE_AR, AA_ANALYZE" in message


@pytest.mark.parametrize("tasks", [[], ["ADVERTISE"]])
def test_refusals_do_not_quote_ids_or_names(monkeypatch, tasks) -> None:
    _use(monkeypatch, _access_client(["MANAGE", "ADVERTISE"], caller_ids=(PERSON,)))
    with pytest.raises(ValidationError) as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=tasks))
    message = str(error.value)
    assert PERSON not in message and CATALOG not in message and PERSON_NAME not in message


def test_person_call_without_user_type_sends_the_same_request_as_before(monkeypatch) -> None:
    client = _use(monkeypatch, _access_client(["ADVERTISE"]))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE", "MANAGE"],
    ))
    assert [(w["method"], w["parent_id"], w["edge"], w["data"]) for w in client.writes()] == [
        ("create_edge_object", CATALOG, "assigned_users",
         {"user": PERSON, "business": BUSINESS, "tasks": ["ADVERTISE", "MANAGE"]}),
    ]
    assert result["request_sent"] == {
        "method": "POST", "path": f"{CATALOG}/assigned_users",
        "data": {"user": PERSON, "business": BUSINESS, "tasks": ["ADVERTISE", "MANAGE"]},
    }
    assert "verified_live" not in result and "meta_response" not in result
    assert not client.reads_of("agencies")


# Partner businesses: list_catalog_partners and user_type=AGENCY on the access pair ------

PARTNER = "880"
OTHER_BUSINESS = "990"


class Agencies:
    """A catalog's agencies edge whose rows change after a write."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __call__(self, params):
        return {"data": [dict(row) for row in self.rows]}


def _partner_client(
    previous_tasks: Any = None,
    *,
    caller_businesses=(OTHER_BUSINESS,),
    after: Any = "same",
    row_extra: dict[str, Any] | None = None,
):
    """Client with the catalog's partner shares before and after a write."""
    before: list[dict[str, Any]] = []
    if previous_tasks is not None or row_extra is not None:
        row: dict[str, Any] = {"id": PARTNER, "name": "Partner Agency"}
        if previous_tasks is not None:
            row["permitted_tasks"] = previous_tasks
        row.update(row_extra or {})
        before = [row]
    edge = Agencies(before)
    client = FakeClient(edges={
        (CATALOG, "agencies"): edge,
        ("me", "business_users"): {"data": [{"id": CALLER, "business": {"id": bid}} for bid in caller_businesses]},
    })
    original_create, original_delete = client.create_edge_object, client.delete_edge_object

    async def create(parent_id, edge_name, *, data, files=None):
        result = await original_create(parent_id, edge_name, data=data, files=files)
        tasks = data["permitted_tasks"] if after == "same" else after
        edge.rows = [{"id": PARTNER, "name": "Partner Agency", "permitted_tasks": tasks}]
        return result

    async def delete(parent_id, edge_name, *, data):
        result = await original_delete(parent_id, edge_name, data=data)
        edge.rows = [] if after == "same" else [{"id": PARTNER, "name": "Partner Agency", "permitted_tasks": after}]
        return result

    client.create_edge_object = create
    client.delete_edge_object = delete
    return client


def _field_rejection() -> MetaApiError:
    return MetaApiError(message="(#100) Tried accessing nonexisting field (permitted_tasks)", status_code=400, code=100)


def test_list_catalog_partners_reads_agencies_with_tasks_and_says_it_is_unverified(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        (CATALOG, "agencies"): _page([
            {"id": 77, "name": "Agency", "permitted_tasks": ["advertise", "MANAGE"], "verification_status": "x"},
        ], "c2", more=True),
    }))
    result = run(business_access.list_catalog_partners(catalog_id=CATALOG, limit=5, after="c1"))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"]) == (CATALOG, "agencies")
    assert call["fields"] == ["id", "name", "permitted_tasks"]
    assert call["params"] == {"limit": 5, "after": "c1"}
    assert result["items"] == [{
        "id": "77", "name": "Agency", "permitted_tasks": ["ADVERTISE", "MANAGE"],
        "can": {"ADVERTISE": business_access.TASK_MEANINGS["ADVERTISE"], "MANAGE": business_access.TASK_MEANINGS["MANAGE"]},
    }]
    assert result["summary"]["complete"] is False and result["summary"]["next_after"] == "c2"
    assert result["verified_live"] is False
    assert "not been run against the live API" in result["verified_live_reason"]
    assert result["scope"] == {"catalog_id": CATALOG}


def test_list_catalog_partners_falls_back_when_meta_refuses_the_tasks_field(monkeypatch) -> None:
    calls = {"n": 0}

    def agencies(params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _field_rejection()
        return _page([{"id": "77", "name": "Agency"}])

    client = _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): agencies}))
    result = run(business_access.list_catalog_partners(catalog_id=CATALOG))
    assert [c["fields"] for c in client.calls] == [["id", "name", "permitted_tasks"], ["id", "name"]]
    assert result["items"][0]["permitted_tasks"] is None
    assert "refused the permitted_tasks field" in result["items"][0]["tasks_unreadable"]
    assert result["summary"]["tasks_field_refused"] is True
    assert result["verified_live"] is False


def test_list_catalog_partners_raises_other_errors(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(errors={(CATALOG, "agencies"): _graph_error()}))
    with pytest.raises(MetaApiError) as error:
        run(business_access.list_catalog_partners(catalog_id=CATALOG))
    assert len(client.calls) == 1
    assert error.value.code == 200
    assert str(error.value).startswith("(#200) Permissions error")
    assert "not yet confirmed against Meta's live API" in str(error.value)


def test_a_failed_partner_write_says_the_request_shape_is_unconfirmed(monkeypatch) -> None:
    client = _partner_client(None)
    client.errors[("POST", CATALOG, "agencies")] = MetaApiError(message="(#100) Invalid parameter", status_code=400, code=100)
    _use(monkeypatch, client)
    with pytest.raises(MetaApiError) as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE"], user_type="AGENCY"))
    assert error.value.code == 100 and error.value.status_code == 400
    assert "may come from the request shape" in str(error.value)


def test_a_failed_person_write_keeps_metas_message_unchanged(monkeypatch) -> None:
    client = _access_client(None)
    client.errors[("POST", CATALOG, "assigned_users")] = _graph_error()
    _use(monkeypatch, client)
    with pytest.raises(MetaApiError) as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON, tasks=["ADVERTISE"]))
    assert str(error.value) == "(#200) Permissions error"


@pytest.mark.parametrize(("rows", "status", "tasks"), [
    ([{"id": PARTNER, "name": "P", "permitted_tasks": ["ADVERTISE"]}], "SHARED", ["ADVERTISE"]),
    ([{"id": "1", "name": "Other", "permitted_tasks": ["MANAGE"]}], "NOT_SHARED", []),
    ([{"id": PARTNER, "name": "P"}], "UNKNOWN", None),
])
def test_list_catalog_partners_checks_one_partner(monkeypatch, rows, status, tasks) -> None:
    _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): _page(rows)}))
    result = run(business_access.list_catalog_partners(catalog_id=CATALOG, partner_id=PARTNER))
    assert result["share_status"] == status
    assert result["item"]["permitted_tasks"] == tasks
    assert result["summary"]["complete"] is (status != "UNKNOWN")
    assert result["verified_live"] is False


def test_list_catalog_partners_one_partner_unknown_when_read_incomplete(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): {"data": [], "paging": {"next": "https://x"}}}))
    result = run(business_access.list_catalog_partners(catalog_id=CATALOG, partner_id=PARTNER))
    assert result["share_status"] == "UNKNOWN"
    assert result["item"]["permitted_tasks"] is None
    assert "not among the rows read" in result["summary"]["incomplete_reason"]


def test_list_catalog_partners_search_matches_names(monkeypatch) -> None:
    rows = [
        {"id": "1", "name": "North Agency", "permitted_tasks": ["ADVERTISE"]},
        {"id": "2", "name": "South Media", "permitted_tasks": ["MANAGE"]},
    ]
    _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): _page(rows)}))
    result = run(business_access.list_catalog_partners(catalog_id=CATALOG, search="agency"))
    assert [(i["id"], i["permitted_tasks"]) for i in result["items"]] == [("1", ["ADVERTISE"])]
    assert result["summary"]["complete"] is True


@pytest.mark.parametrize("kwargs", [
    {"catalog_id": "cat"},
    {"catalog_id": CATALOG, "partner_id": "p1"},
    {"catalog_id": CATALOG, "partner_id": PARTNER, "search": "x"},
    {"catalog_id": CATALOG, "limit": 0},
])
def test_list_catalog_partners_validates_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(business_access.list_catalog_partners(**kwargs))


def test_share_catalog_with_partner_reads_first_posts_and_verifies(monkeypatch) -> None:
    client = _use(monkeypatch, _partner_client(None))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE"], user_type="AGENCY",
    ))
    assert client.calls[0]["edge"] == "agencies" and client.calls[0]["method"] == "list_objects"
    assert [(w["method"], w["parent_id"], w["edge"], w["data"]) for w in client.writes()] == [
        ("create_edge_object", CATALOG, "agencies", {"business": PARTNER, "permitted_tasks": ["ADVERTISE"]}),
    ]
    assert result["change"] == "share"
    assert result["previous"] == {"tasks": [], "has_direct_access": False, "can": {}}
    assert result["current"]["tasks"] == ["ADVERTISE"]
    assert result["verified"] is True and result["ok"] is True
    assert result["verified_live"] is False and result["verified_live_reason"]
    assert result["meta_response"] == {"success": True}
    assert result["target"] == {"catalog_id": CATALOG, "user_id": PARTNER, "user_type": "AGENCY"}
    # A first share needs no caller check and never touches assigned_users.
    assert not client.reads_of("assigned_users") and not client.reads_of("business_users")


def test_partner_increase_sends_the_full_set(monkeypatch) -> None:
    client = _use(monkeypatch, _partner_client(["ADVERTISE"]))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE", "MANAGE"], user_type="agency",
    ))
    assert client.writes()[0]["data"] == {"business": PARTNER, "permitted_tasks": ["ADVERTISE", "MANAGE"]}
    assert result["change"] == "increase"
    assert result["previous"]["tasks"] == ["ADVERTISE"]
    assert any("full control" in w for w in result["warnings"])
    assert result["verified"] is True


def test_partner_increase_that_meta_did_not_apply_is_reported(monkeypatch) -> None:
    _use(monkeypatch, _partner_client(["ADVERTISE"], after=["ADVERTISE"]))
    result = run(business_access.set_catalog_access(
        catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE", "MANAGE"], user_type="AGENCY",
    ))
    assert result["verified"] is False and result["ok"] is False
    assert "did not fully take effect" in result["warnings"][-1]


def test_partner_removal_checks_caller_business_then_deletes(monkeypatch) -> None:
    client = _use(monkeypatch, _partner_client(["MANAGE", "ADVERTISE"]))
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=[], user_type="AGENCY"))
    assert [(w["method"], w["edge"], w["data"]) for w in client.writes()] == [
        ("delete_edge_object", "agencies", {"business": PARTNER}),
    ]
    assert client.reads_of("business_users")
    assert result["change"] == "remove"
    assert set(result["can_no_longer"]) == {"MANAGE", "ADVERTISE"}
    assert result["current"] == {"tasks": [], "has_direct_access": False, "can": {}}
    assert result["verified"] is True


def test_partner_removal_refused_when_caller_belongs_to_the_partner(monkeypatch) -> None:
    client = _use(monkeypatch, _partner_client(["ADVERTISE"], caller_businesses=(PARTNER,)))
    with pytest.raises(ValidationError, match="allow_self_removal") as error:
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=[], user_type="AGENCY"))
    assert client.writes() == []
    assert PARTNER not in str(error.value)
    allowed = run(business_access.set_catalog_access(
        catalog_id=CATALOG, user_id=PARTNER, tasks=[], user_type="AGENCY", allow_self_removal=True,
    ))
    assert allowed["sent"] is True
    assert any("token's own business" in w for w in allowed["warnings"])


@pytest.mark.parametrize("broken", ["error", "empty", "incomplete"])
def test_partner_removal_refused_when_caller_business_is_unreadable(monkeypatch, broken) -> None:
    client = _partner_client(["ADVERTISE"])
    if broken == "error":
        client.errors[("me", "business_users")] = _graph_error()
    elif broken == "empty":
        client.edges[("me", "business_users")] = {"data": []}
    else:
        client.edges[("me", "business_users")] = {"data": [], "paging": {"next": "https://x"}}
    _use(monkeypatch, client)
    with pytest.raises(ValidationError, match="cannot confirm"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=[], user_type="AGENCY"))
    assert client.writes() == []


@pytest.mark.parametrize(("tasks", "change"), [(["ADVERTISE"], "reduce"), (["AA_ANALYZE"], "change")])
def test_taking_tasks_from_a_partner_is_refused(monkeypatch, tasks, change) -> None:
    client = _use(monkeypatch, _partner_client(["MANAGE", "ADVERTISE"]))
    preview = run(business_access.preview_catalog_access_change(
        catalog_id=CATALOG, user_id=PARTNER, tasks=tasks, user_type="AGENCY",
    ))
    assert preview["change"] == change
    assert preview["would_be_refused"] is True and preview["would_send"] is None
    assert "Remove the whole share" in preview["refusal_reason"]
    with pytest.raises(ValidationError, match="not supported"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=tasks, user_type="AGENCY"))
    assert client.writes() == []


@pytest.mark.parametrize("row_extra", [
    {},
    {"permitted_tasks": None},
    {"permitted_tasks": "ADVERTISE"},
    {"permitted_tasks": ["ADVERTISE", "SOMETHING_NEW"]},
])
@pytest.mark.parametrize("tasks", [[], ["ADVERTISE", "MANAGE"]])
def test_partner_with_unreadable_tasks_refuses_every_write(monkeypatch, row_extra, tasks) -> None:
    client = _use(monkeypatch, _partner_client(None, row_extra=row_extra))
    with pytest.raises(ValidationError, match="could not be read completely"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=tasks, user_type="AGENCY"))
    assert client.writes() == []


def test_partner_write_when_meta_refuses_the_tasks_field(monkeypatch) -> None:
    calls = {"n": 0}
    rows: list[dict[str, Any]] = [{"id": PARTNER, "name": "P"}]

    def agencies(params):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            raise _field_rejection()
        return {"data": rows}

    client = _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): agencies}))
    with pytest.raises(ValidationError, match="refused the permitted_tasks field"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=[], user_type="AGENCY"))
    assert client.writes() == []
    # A partner that is not on the catalog can still be shared with; the read-back cannot confirm tasks.
    rows.clear()
    calls["n"] = 0
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE"], user_type="AGENCY"))
    assert result["sent"] is True
    assert result["verified"] is False


def test_partner_refuses_when_the_share_list_cannot_be_read_completely(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): {"data": [], "paging": {"next": "https://x"}}}))
    with pytest.raises(ValidationError, match="could not be read completely"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE"], user_type="AGENCY"))
    assert client.writes() == []


def test_partner_write_does_not_happen_when_the_current_read_fails(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(errors={(CATALOG, "agencies"): _graph_error()}))
    with pytest.raises(MetaApiError):
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE"], user_type="AGENCY"))
    assert client.writes() == []


def test_partner_nothing_to_remove_and_no_change(monkeypatch) -> None:
    client = _use(monkeypatch, _partner_client(None))
    with pytest.raises(ValidationError, match="nothing to remove"):
        run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=[], user_type="AGENCY"))
    _use(monkeypatch, _partner_client(["ADVERTISE"]))
    same = run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=["advertise"], user_type="AGENCY"))
    assert same["change"] == "no_change" and same["sent"] is False
    assert same["verified_live"] is False
    assert client.writes() == []


def test_partner_unconfirmed_read_back(monkeypatch) -> None:
    client = _partner_client(None)
    edge = client.edges[(CATALOG, "agencies")]
    calls = {"n": 0}

    def flaky(params):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _graph_error()
        return edge(params)

    client.edges[(CATALOG, "agencies")] = flaky
    _use(monkeypatch, client)
    result = run(business_access.set_catalog_access(catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE"], user_type="AGENCY"))
    assert result["sent"] is True and result["verified"] is False
    assert "could not be read back" in result["warnings"][-1]


def test_partner_dry_run_and_preview_match_and_send_nothing(monkeypatch) -> None:
    client = _use(monkeypatch, _partner_client(["ADVERTISE"]))
    preview = run(business_access.preview_catalog_access_change(
        catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE", "AA_ANALYZE"], user_type="AGENCY",
    ))
    dry = run(business_access.set_catalog_access(
        catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE", "AA_ANALYZE"], user_type="AGENCY", dry_run=True,
    ))
    assert client.writes() == []
    assert dry["sent"] is False and dry["dry_run"] is True
    assert dry["would_send"] == preview["would_send"] == {
        "method": "POST", "path": f"{CATALOG}/agencies",
        "data": {"business": PARTNER, "permitted_tasks": ["AA_ANALYZE", "ADVERTISE"]},
    }
    for key in ("previous", "change", "can_no_longer", "gains", "warnings", "verified_live", "verified_live_reason"):
        assert preview[key] == dry[key]
    assert preview["verified_live"] is False
    assert preview["read_only"] is True


def test_partner_access_rejects_business_id_before_any_call(monkeypatch) -> None:
    _no_client(monkeypatch)
    for tool in (business_access.set_catalog_access, business_access.preview_catalog_access_change):
        with pytest.raises(ValidationError, match="business_id is not used"):
            run(tool(catalog_id=CATALOG, user_id=PARTNER, tasks=["ADVERTISE"], business_id=BUSINESS, user_type="AGENCY"))


def test_partner_docstrings_state_the_request_shape_is_unconfirmed() -> None:
    for name in ("list_catalog_partners", "preview_catalog_access_change", "set_catalog_access"):
        doc = inspect.getdoc(getattr(business_access, name)) or ""
        assert "not yet confirmed against Meta's live API" in doc, name
        assert "verified_live=false" in doc, name


@pytest.mark.parametrize("routed", [False, True])
def test_list_catalog_partners_is_allowed_in_read_only(monkeypatch, read_only_mode, routed) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.read_only import read_only_tool_names

    _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): _page([])}))
    assert "list_catalog_partners" in read_only_tool_names()
    assert _contracts()["list_catalog_partners"].effect == "read"
    arguments = {"catalog_id": CATALOG}
    tool, payload = (
        ("call_tool", {"name": "list_catalog_partners", "arguments": arguments}) if routed
        else ("list_catalog_partners", arguments)
    )
    result = run(mcp_server.call_tool(tool, payload))
    assert result.structured_content["verified_live"] is False


def test_partner_write_is_blocked_in_read_only(monkeypatch, read_only_mode) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    _no_client(monkeypatch)
    arguments = {"catalog_id": CATALOG, "user_id": PARTNER, "tasks": [], "user_type": "AGENCY", "dry_run": True}
    with pytest.raises(ToolError, match="META_READ_ONLY"):
        run(mcp_server.call_tool("set_catalog_access", arguments))


# Round four (H18): a caller identity row that cannot be read makes the caller unknown ----------


@pytest.mark.parametrize("preview", [False, True])
def test_h18_unknown_business_on_one_caller_row_blocks_partner_removal(monkeypatch, preview) -> None:
    client = _partner_client(["MANAGE"])
    client.edges[("me", "business_users")] = {"data": [{"id": "50", "business": {"id": "40"}}, {"id": "60"}]}
    _use(monkeypatch, client)
    if preview:
        result = run(business_access.preview_catalog_access_change(CATALOG, PARTNER, [], user_type="AGENCY"))
        assert result["would_be_refused"] is True, result
        assert result["self_check"]["is_caller_business"] is None
        assert "cannot confirm" in result["refusal_reason"]
    else:
        with pytest.raises(ValidationError, match="cannot confirm"):
            run(business_access.set_catalog_access(CATALOG, PARTNER, [], user_type="AGENCY"))
    assert client.writes() == []


@pytest.mark.parametrize("bad_row", [
    {"id": "60", "business": {"id": ""}},
    {"id": "60", "business": {"id": None}},
    {"id": "60", "business": "40"},
    {"id": "60", "business": {"id": {"nested": "40"}}},
    {"id": "60", "business": {"id": True}},
    {"id": "60", "business": {"id": "not-a-number"}},
    "not-a-row",
])
def test_h18_any_unusable_caller_business_row_blocks_partner_removal(monkeypatch, bad_row) -> None:
    client = _partner_client(["ADVERTISE"])
    client.edges[("me", "business_users")] = {"data": [{"id": CALLER, "business": {"id": OTHER_BUSINESS}}, bad_row]}
    _use(monkeypatch, client)
    preview = run(business_access.preview_catalog_access_change(CATALOG, PARTNER, [], user_type="AGENCY"))
    assert preview["would_be_refused"] is True
    assert preview["self_check"]["is_caller_business"] is None
    with pytest.raises(ValidationError, match="cannot confirm"):
        run(business_access.set_catalog_access(CATALOG, PARTNER, [], user_type="AGENCY"))
    assert client.writes() == []


def test_h18_unknown_caller_business_still_allows_explicit_override(monkeypatch) -> None:
    client = _partner_client(["ADVERTISE"])
    client.edges[("me", "business_users")] = {"data": [{"id": CALLER, "business": {"id": OTHER_BUSINESS}}, {"id": "60"}]}
    _use(monkeypatch, client)
    result = run(business_access.set_catalog_access(
        CATALOG, PARTNER, [], user_type="AGENCY", allow_self_removal=True,
    ))
    assert result["sent"] is True
    assert [(w["method"], w["data"]) for w in client.writes()] == [("delete_edge_object", {"business": PARTNER})]


def test_h18_fully_readable_caller_businesses_still_allow_removal(monkeypatch) -> None:
    client = _partner_client(["ADVERTISE"])
    client.edges[("me", "business_users")] = {"data": [
        {"id": CALLER, "business": {"id": OTHER_BUSINESS}}, {"id": "60", "business": {"id": 40}},
    ]}
    _use(monkeypatch, client)
    preview = run(business_access.preview_catalog_access_change(CATALOG, PARTNER, [], user_type="AGENCY"))
    assert preview["would_be_refused"] is False and preview["self_check"]["is_caller_business"] is False


@pytest.mark.parametrize("bad_row", [
    {"business": {"id": BUSINESS}}, {"id": "", "business": {"id": BUSINESS}}, {"id": "abc", "business": {"id": BUSINESS}}, "x",
])
@pytest.mark.parametrize("tasks", [[], ["ADVERTISE"]])
def test_h18_person_removal_refused_when_one_caller_id_row_is_unreadable(monkeypatch, bad_row, tasks) -> None:
    client = _access_client(["MANAGE", "ADVERTISE"])
    client.edges[("me", "business_users")] = {"data": [{"id": CALLER, "business": {"id": BUSINESS}}, bad_row]}
    _use(monkeypatch, client)
    preview = run(business_access.preview_catalog_access_change(CATALOG, PERSON, tasks, business_id=BUSINESS))
    assert preview["would_be_refused"] is True
    assert preview["self_check"]["is_caller"] is None
    with pytest.raises(ValidationError, match="cannot confirm"):
        run(business_access.set_catalog_access(CATALOG, PERSON, tasks, business_id=BUSINESS))
    assert client.writes() == []


def test_h18_person_removal_refused_when_own_user_id_is_unusable(monkeypatch) -> None:
    client = _access_client(["ADVERTISE"])
    client.objects["me"] = {"id": {"nested": "100"}}
    _use(monkeypatch, client)
    with pytest.raises(ValidationError, match="cannot confirm"):
        run(business_access.set_catalog_access(CATALOG, PERSON, [], business_id=BUSINESS))
    assert client.writes() == []


@pytest.mark.parametrize("bad_row", [{"name": "No id", "tasks": ["MANAGE"]}, {"id": None, "tasks": ["MANAGE"]}, "x"])
def test_h18_unidentified_assignment_row_makes_an_absent_person_unknown(monkeypatch, bad_row) -> None:
    client = _access_client(None)
    client.edges[(CATALOG, "assigned_users")] = {"data": [{"id": "1", "tasks": ["ADVERTISE"]}, bad_row]}
    _use(monkeypatch, client)
    with pytest.raises(ValidationError, match="could not be read completely"):
        run(business_access.set_catalog_access(CATALOG, PERSON, ["ADVERTISE"], business_id=BUSINESS))
    assert client.writes() == []
    check = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, business_id=BUSINESS, user_id=PERSON))
    assert check["access_status"] == "UNKNOWN" and check["summary"]["complete"] is False


@pytest.mark.parametrize("bad_row", [{"name": "No id", "permitted_tasks": ["MANAGE"]}, "x"])
def test_h18_unidentified_partner_row_makes_an_absent_partner_unknown(monkeypatch, bad_row) -> None:
    client = _partner_client(None)
    client.edges[(CATALOG, "agencies")] = {"data": [bad_row]}
    _use(monkeypatch, client)
    with pytest.raises(ValidationError, match="could not be read completely"):
        run(business_access.set_catalog_access(CATALOG, PARTNER, ["ADVERTISE"], user_type="AGENCY"))
    assert client.writes() == []


def test_h18_partner_catalog_scan_with_unidentified_partner_row_is_incomplete(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={
        (BUSINESS, "owned_product_catalogs"): _page([{"id": "1", "name": "A"}]),
        ("1", "agencies"): _page([{"id": "88"}, {"name": "No id"}]),
    }))
    result = run(business_access.list_user_assigned_catalogs(user_id="77", user_type="AGENCY", business_id=BUSINESS))
    assert result["items"] == []
    assert result["summary"]["complete"] is False
    assert "1" in result["summary"]["incomplete_reason"]


def test_h18_owned_catalog_without_id_is_not_read_as_none_and_is_a_gap(monkeypatch) -> None:
    client = _use(monkeypatch, FakeClient(edges={
        (BUSINESS, "owned_product_catalogs"): _page([{"id": "1", "name": "A"}, {"name": "No id"}]),
        ("1", "agencies"): _page([{"id": "88"}]),
    }))
    result = run(business_access.list_user_assigned_catalogs(user_id="77", user_type="AGENCY", business_id=BUSINESS))
    assert [call["parent_id"] for call in client.reads_of("agencies")] == ["1"]
    assert result["summary"]["catalogs_checked"] == 1
    assert result["summary"]["complete"] is False
    assert "no readable id" in result["summary"]["incomplete_reason"]


# R4 J6: one canonical identity for every row id ---------------------------------------


@pytest.mark.parametrize("kind", ["PERSON", "AGENCY"])
@pytest.mark.parametrize("unreadable_id", ["unknown", " 30 ", -30])
def test_access_unknown_recipient_identity_cannot_be_assumed_absent(monkeypatch, kind, unreadable_id) -> None:
    """Reviewer reproduction: a MANAGE row with an odd id must never let the change pass as a new grant."""
    edge, tasks_key = ("assigned_users", "tasks") if kind == "PERSON" else ("agencies", "permitted_tasks")
    client = _use(monkeypatch, FakeClient(
        objects={"10": {"id": "10", "business": {"id": "40"}}},
        edges={("10", edge): {"data": [{"id": unreadable_id, tasks_key: ["MANAGE"]}]}},
    ))
    try:
        run(business_access.set_catalog_access("10", "30", ["ADVERTISE"], user_type=kind))
    except ValidationError:
        pass
    assert client.writes() == []
    preview = run(business_access.preview_catalog_access_change("10", "30", ["ADVERTISE"], user_type=kind))
    assert preview["would_be_refused"] is True, preview


@pytest.mark.parametrize("odd_id", ["unknown", -30, "-30", "30.0", 30.0, True, "３０", "3 0", {"id": "30"}])
@pytest.mark.parametrize("kind", ["PERSON", "AGENCY"])
def test_row_whose_identity_cannot_be_read_makes_current_access_unknown(monkeypatch, kind, odd_id) -> None:
    """Any id that is not a plain numeric id is unidentified, even with the caller fully readable."""
    edge, tasks_key = ("assigned_users", "tasks") if kind == "PERSON" else ("agencies", "permitted_tasks")
    recipient = PERSON if kind == "PERSON" else PARTNER
    client = _use(monkeypatch, FakeClient(
        objects={"me": {"id": "100"}},
        edges={
            (CATALOG, edge): {"data": [{"id": odd_id, tasks_key: ["MANAGE"]}]},
            ("me", "business_users"): {"data": [{"id": CALLER, "business": {"id": OTHER_BUSINESS}}]},
        },
    ))
    business = {"business_id": BUSINESS} if kind == "PERSON" else {}
    preview = run(business_access.preview_catalog_access_change(
        CATALOG, recipient, ["ADVERTISE"], user_type=kind, **business,
    ))
    assert preview["change"] == "unknown" and preview["would_be_refused"] is True
    assert preview["previous"]["has_direct_access"] is None
    assert "without a readable id" in preview["current_access_read"]["incomplete_reason"]
    with pytest.raises(ValidationError, match="could not be read completely"):
        run(business_access.set_catalog_access(CATALOG, recipient, ["ADVERTISE"], user_type=kind, **business))
    assert client.writes() == []


@pytest.mark.parametrize("same_id", [f" {PERSON} ", int(PERSON), PERSON])
def test_padded_or_whole_number_id_is_the_recipient_and_keeps_manage_protection(monkeypatch, same_id) -> None:
    """A trimmed numeric id is the recipient's own row: the change replaces MANAGE, it is not a new grant."""
    client = _access_client(["MANAGE"])
    client.edges[(CATALOG, "assigned_users")] = {"data": [{"id": same_id, "tasks": ["MANAGE"]}]}
    _use(monkeypatch, client)
    preview = run(business_access.preview_catalog_access_change(CATALOG, PERSON, ["ADVERTISE"], business_id=BUSINESS))
    assert preview["change"] == "change"
    assert preview["previous"]["has_direct_access"] is True and preview["previous"]["tasks"] == ["MANAGE"]
    assert preview["self_check"]["checked"] is True and preview["self_check"]["is_caller"] is False
    assert "MANAGE" in preview["can_no_longer"]


def test_padded_caller_id_is_still_recognised_as_the_caller(monkeypatch) -> None:
    """The caller's own ids go through the same identity: a padded id still blocks self-removal."""
    client = _access_client(["MANAGE"], caller_ids=(f" {PERSON} ",))
    _use(monkeypatch, client)
    preview = run(business_access.preview_catalog_access_change(CATALOG, PERSON, [], business_id=BUSINESS))
    assert preview["self_check"]["is_caller"] is True and preview["would_be_refused"] is True


@pytest.mark.parametrize("odd_id", ["unknown", -30])
def test_exact_id_search_with_an_unidentified_row_is_not_complete(monkeypatch, odd_id) -> None:
    _use(monkeypatch, FakeClient(edges={(CATALOG, "agencies"): _page([{"id": "88"}, {"id": odd_id}])}))
    result = run(business_access.list_catalog_assigned_users(catalog_id=CATALOG, user_type="AGENCY", user_id="30"))
    assert result["items"] == []
    assert result["summary"]["complete"] is False
    assert "readable numeric id" in result["summary"]["incomplete_reason"]


def test_name_search_with_a_non_object_row_is_not_complete(monkeypatch) -> None:
    _use(monkeypatch, FakeClient(edges={("me", "businesses"): _page([{"id": "1", "name": "Acme"}, "x"])}))
    result = run(business_access.list_businesses(name_contains="acme"))
    assert [item["id"] for item in result["items"]] == ["1"]
    assert result["summary"]["complete"] is False
