"""Audience tool tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pytest

from meta_ads_mcp.tools import audiences


class FakeAudienceClient:
    """Fake audience client."""

    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.list_calls.append(
            {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}
        )
        return {"data": [{"id": "aud_1", "name": "Audience One"}]}

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        return {"id": "aud_created", "payload": data}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.get_calls.append(
            {"object_id": object_id, "fields": fields, "params": params}
        )
        return {
            "id": object_id,
            "name": "Existing Audience",
            "description": "Old",
            "retention_days": 30,
            "customer_file_source": "USER_PROVIDED_ONLY",
        }

    async def update_object(self, object_id: str, *, data):
        return {"success": True}

    async def delete_object(self, object_id: str):
        return {"success": True}


def test_create_lookalike_audience_builds_lookalike_spec(monkeypatch) -> None:
    client = FakeAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        audiences.create_lookalike_audience(
            account_id="123",
            name="LAL",
            origin_audience_id="aud_origin",
            country="US",
            ratio=0.02,
        )
    )
    spec = result["created"]["payload"]["lookalike_spec"]
    assert spec == {"country": "US", "type": "custom_ratio", "ratio": 0.02}


def test_list_audiences_returns_collection(monkeypatch) -> None:
    client = FakeAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.list_audiences(account_id="123"))
    assert result["summary"]["count"] == 1
    assert result["summary"]["complete"] is True
    assert result["summary"]["truncated"] is False
    assert result["summary"]["next_after"] is None
    assert client.list_calls[0]["fields"] == audiences.AUDIENCE_SUMMARY_FIELDS
    assert "lookalike_spec" not in audiences.AUDIENCE_SUMMARY_FIELDS


def test_list_audiences_supports_empty_collection(monkeypatch) -> None:
    class EmptyAudienceClient(FakeAudienceClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            return {"data": []}

    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: EmptyAudienceClient())
    result = asyncio.run(audiences.list_audiences(account_id="123"))
    assert result["items"] == []


def test_get_audience_returns_detailed_single_object(monkeypatch) -> None:
    client = FakeAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)

    result = asyncio.run(
        audiences.get_audience(
            audience_id="aud_1",
            fields="id,name,lookalike_spec",
        )
    )

    assert result["item"]["id"] == "aud_1"
    assert result["summary"]["count"] == 1


def test_get_audience_normalizes_and_validates_id(monkeypatch) -> None:
    client = FakeAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)

    asyncio.run(audiences.get_audience(audience_id=" aud_1 "))
    assert client.get_calls[0]["object_id"] == "aud_1"
    with pytest.raises(audiences.ValidationError, match="audience_id is required"):
        asyncio.run(audiences.get_audience(audience_id=" "))


def test_create_custom_audience_returns_created_payload(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(
        audiences.create_custom_audience(
            account_id="123",
            name="Audience",
            customer_file_source="USER_PROVIDED_ONLY",
        )
    )
    assert result["created"]["id"] == "aud_created"
    assert result["created"]["payload"]["customer_file_source"] == "USER_PROVIDED_ONLY"


@pytest.mark.parametrize("countries", [["US", "CA", "AU"], ["US", "CA", "AU", "GB", "DE", "IE"]])
def test_create_lookalike_audience_supports_countries(monkeypatch, countries) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(
        audiences.create_lookalike_audience(
            account_id="123",
            name="LAL",
            origin_audience_id="aud_origin",
            countries=countries,
            ratio=0.02,
            starting_ratio=0.01,
        )
    )
    spec = result["created"]["payload"]["lookalike_spec"]
    assert spec == {
        "type": "custom_ratio", "ratio": 0.02, "starting_ratio": 0.01,
        "location_spec": {"geo_locations": {"countries": countries}},
    }


@pytest.mark.parametrize("lookalike_type", ["similarity", "reach"])
def test_create_lookalike_preserves_explicit_type_without_ratio(monkeypatch, lookalike_type) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(audiences.create_lookalike_audience(
        account_id="123", name="LAL", origin_audience_id="seed", country="US",
        lookalike_type=lookalike_type,
    ))
    assert result["created"]["payload"]["lookalike_spec"] == {
        "country": "US", "type": lookalike_type,
    }


@pytest.mark.parametrize("arguments", [
    {"starting_ratio": 0.01},
    {"ratio": 0.01, "starting_ratio": 0.01},
    {"ratio": 0.01, "starting_ratio": -0.01},
    {"ratio": float("nan")}, {"ratio": 0},
    {"countries": ["CA"]},
])
def test_create_lookalike_rejects_conflicting_spec_before_write(monkeypatch, arguments) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError):
        asyncio.run(audiences.create_lookalike_audience(
            account_id="123", name="LAL", origin_audience_id="seed", country="US", **arguments,
        ))


@pytest.mark.parametrize("lookalike_type", ["similarity", "reach", "custom_ratio"])
@pytest.mark.parametrize("ratio", [None, 0.01])
def test_create_lookalike_preserves_explicit_type_for_meta_validation(monkeypatch, lookalike_type, ratio):
    monkeypatch.setattr(audiences, "get_graph_api_client", FakeAudienceClient)
    result = asyncio.run(audiences.create_lookalike_audience(
        account_id="123", name="LAL", origin_audience_id="seed", country="US",
        lookalike_type=lookalike_type, ratio=ratio,
    ))
    spec = result["created"]["payload"]["lookalike_spec"]
    assert spec["type"] == lookalike_type
    assert spec.get("ratio") == ratio


def test_create_lookalike_wrapper_accepts_country_csv_and_describes_params(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(mcp_server.call_tool("call_tool", {
        "name": "create_lookalike_audience",
        "arguments": {"account_id": "123", "name": "LAL", "origin_audience_id": "seed",
                      "countries": "us, CA, US", "ratio": 0.01},
    })).structured_content
    assert result["created"]["payload"]["lookalike_spec"] == {
        "type": "custom_ratio", "ratio": 0.01,
        "location_spec": {"geo_locations": {"countries": ["US", "CA"]}},
    }
    tool = asyncio.run(mcp_server.get_tool("create_lookalike_audience"))
    assert "Cannot supply lookalike_spec" in tool.parameters["properties"]["params"]["description"]


def test_list_audiences_exposes_page_completeness_and_next_cursor(monkeypatch) -> None:
    class PagedAudienceClient(FakeAudienceClient):
        async def list_objects(self, parent_id, edge, *, fields, params):
            assert params == {"limit": 200, "subtype": "LOOKALIKE", "after": "previous"}
            return {"data": [{"id": "aud1"}], "paging": {
                "next": "https://graph.facebook.com/customaudiences?after=next-cursor",
                "cursors": {"after": "next-cursor"},
            }}

    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: PagedAudienceClient())
    result = asyncio.run(audiences.list_audiences(
        account_id="123", subtype="LOOKALIKE", limit=200, after="previous",
    ))
    assert result["summary"] == {
        "count": 1, "complete": False, "truncated": True, "next_after": "next-cursor",
    }
    assert result["paging"]["after"] == "next-cursor"


def test_create_lookalike_audience_requires_country_or_countries(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    with pytest.raises(audiences.ValidationError):
        asyncio.run(
            audiences.create_lookalike_audience(
                account_id="123",
                name="Bad LAL",
                origin_audience_id="aud_origin",
            )
        )


def test_update_custom_audience_returns_previous_and_current(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(
        audiences.update_custom_audience(audience_id="aud_1", name="Updated Audience", retention_days=45)
    )
    assert result["previous"]["name"] == "Existing Audience"
    assert result["current"]["retention_days"] == 45


def test_audience_params_cannot_supply_omitted_typed_fields(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())

    with pytest.raises(audiences.ValidationError, match="retention_days"):
        asyncio.run(
            audiences.create_custom_audience(
                account_id="123",
                name="Audience",
                params={"retention_days": 90},
            )
        )
    with pytest.raises(audiences.ValidationError, match="description"):
        asyncio.run(
            audiences.create_lookalike_audience(
                account_id="123",
                name="Lookalike",
                origin_audience_id="aud_origin",
                country="US",
                params={"description": "bypass"},
            )
        )
    with pytest.raises(audiences.ValidationError, match="lookalike_spec"):
        asyncio.run(audiences.create_lookalike_audience(
            account_id="123", name="LAL", origin_audience_id="seed", country="US",
            params={"lookalike_spec": {"ratio": 0.01}},
        ))
    with pytest.raises(audiences.ValidationError, match="retention_days"):
        asyncio.run(
            audiences.update_custom_audience(
                audience_id="aud_1",
                params={"retention_days": 90},
            )
        )


def test_delete_audience_returns_success(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(audiences.delete_audience(audience_id="aud_1"))
    assert result["result"]["success"] is True


# add_audience_users / remove_audience_users


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeUsersClient:
    """Records audience user uploads; can fail on one batch with a value-echoing error."""

    def __init__(self, *, fail_on_batch: int | None = None, error: Exception | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self.fail_on_batch = fail_on_batch
        self.error = error

    def _record(self, method: str, parent_id: str, edge: str, data: dict) -> dict:
        self.requests.append({"method": method, "parent_id": parent_id, "edge": edge, "data": data})
        if self.fail_on_batch == len(self.requests):
            raise self.error
        rows = data["payload"]["data"]
        return {
            "audience_id": parent_id,
            "session_id": str(data["session"]["session_id"]),
            "num_received": len(rows),
            "num_invalid_entries": 0,
            "invalid_entry_samples": {rows[0][0]: "sample echoing a hash"},
        }

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        return self._record("POST", parent_id, edge, data)

    async def delete_edge_object(self, parent_id: str, edge: str, *, data):
        return self._record("DELETE", parent_id, edge, data)


def _users_tool(name: str):
    return getattr(audiences, name)


USER_TOOLS = ["add_audience_users", "remove_audience_users"]


@pytest.mark.parametrize("tool", USER_TOOLS)
def test_audience_users_requires_exactly_one_of_file_path_or_rows(monkeypatch, tmp_path, tool) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    csv_path = tmp_path / "list.csv"
    csv_path.write_text("email\na@example.com\n")
    with pytest.raises(audiences.ValidationError, match="exactly one of file_path or rows"):
        asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL"]))
    with pytest.raises(audiences.ValidationError, match="exactly one of file_path or rows"):
        asyncio.run(_users_tool(tool)(
            audience_id="aud_1", schema=["EMAIL"], file_path=str(csv_path), rows=[["a@example.com"]],
        ))


@pytest.mark.parametrize(("tool", "method"), [("add_audience_users", "POST"), ("remove_audience_users", "DELETE")])
def test_audience_users_file_path_reads_csv_by_header(monkeypatch, tmp_path, tool, method) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    csv_path = tmp_path / "customers.csv"
    csv_path.write_text(
        "email,phone,notes\n"
        "Jane@Example.com,+1 (555) 010-9999,vip-note-alpha\n"
        "bob@example.org,0044 20 7946 0000,vip-note-beta\n"
    )

    result = asyncio.run(_users_tool(tool)(
        audience_id="aud_1", schema=["EMAIL", "PHONE"], file_path=str(csv_path),
    ))

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["method"] == method
    assert (request["parent_id"], request["edge"]) == ("aud_1", "users")
    payload = request["data"]["payload"]
    assert payload["schema"] == ["EMAIL", "PHONE"]
    assert payload["data"] == [
        [_sha("jane@example.com"), _sha("15550109999")],
        [_sha("bob@example.org"), _sha("442079460000")],
    ]
    sent = json.dumps(request["data"])
    assert "vip-note" not in sent
    assert "notes" not in sent
    assert result["rows_read"] == 2
    assert result["rows_sent"] == 2
    assert result["source"] == "file_path"
    assert result["action"] == tool


@pytest.mark.parametrize("tool", USER_TOOLS)
def test_audience_users_inline_rows_capped_at_1000(monkeypatch, tool) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    rows = [[f"user{index}@example.com"] for index in range(1001)]
    with pytest.raises(audiences.ValidationError, match="file_path"):
        asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL"], rows=rows))


def test_audience_users_blank_and_malformed_values_are_dropped_not_hashed(monkeypatch) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)

    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL"], rows=[["a@example.com"], [""], ["   "]],
    ))

    assert client.requests[0]["data"]["payload"]["data"] == [[_sha("a@example.com")]]
    assert _sha("") not in json.dumps(client.requests[0]["data"])
    assert result["invalid_values"] == 2
    assert result["rows_dropped"] == 2
    assert result["rows_sent"] == 1


def test_audience_users_multi_key_rows_blank_only_the_invalid_key(monkeypatch) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)

    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema="EMAIL,PHONE,FN,COUNTRY",
        rows=[["not-an-email", "(555) 010-1234", "Zoë-Ann", "US"], ["", "no digits", " ", "usa"]],
    ))

    assert client.requests[0]["data"]["payload"]["data"] == [
        ["", _sha("5550101234"), _sha("zoëann"), _sha("us")],
    ]
    assert result["invalid_values"] == 5
    assert result["rows_dropped"] == 1


def test_audience_users_normalizes_before_hashing(monkeypatch) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL", "GEN", "DOBM", "ZIP", "CT"],
        rows=[[" A@Example.COM ", "Female", "3", "94025-1234", "San Francisco"]],
    ))
    assert client.requests[0]["data"]["payload"]["data"] == [[
        _sha("a@example.com"), _sha("f"), _sha("03"), _sha("94025"), _sha("sanfrancisco"),
    ]]


def test_audience_users_pre_hashed_rejects_non_digests(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match="SHA-256") as excinfo:
        asyncio.run(audiences.add_audience_users(
            audience_id="aud_1", schema=["EMAIL"], rows=[["a@example.com"]], pre_hashed=True,
        ))
    assert "a@example.com" not in str(excinfo.value)


def test_audience_users_pre_hashed_sends_digests_and_drops_empty_string_hash(monkeypatch) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    digest = _sha("a@example.com")
    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL"], rows=[[digest.upper()], [_sha("")]], pre_hashed=True,
    ))
    assert client.requests[0]["data"]["payload"]["data"] == [[digest]]
    assert result["invalid_values"] == 1
    assert result["hashing"] == "pre_hashed_by_caller"


@pytest.mark.parametrize("tool", USER_TOOLS)
def test_audience_users_never_echo_rows(monkeypatch, caplog, tool) -> None:
    raw = "secret.person@example.com"
    digest = _sha(raw)
    caplog.set_level(logging.DEBUG)

    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL"], rows=[[raw]]))
    serialized = json.dumps(result)
    assert raw not in serialized and digest not in serialized

    for error in (
        audiences.MetaApiError(
            message=f"Invalid entry {raw} with hash {digest}",
            status_code=400, code=100, user_message=f"Bad {digest}",
            error_data={"sample": digest}, details={"error": {"message": raw}},
        ),
        RuntimeError(f"transport failed while sending {raw} {digest}"),
    ):
        failing = FakeUsersClient(fail_on_batch=1, error=error)
        monkeypatch.setattr(audiences, "get_graph_api_client", lambda failing=failing: failing)
        with pytest.raises(Exception) as excinfo:
            asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL"], rows=[[raw]]))
        rendered = " ".join([
            str(excinfo.value), repr(excinfo.value),
            json.dumps(getattr(excinfo.value, "to_public_dict", lambda: {})()),
        ])
        assert raw not in rendered and digest not in rendered
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None

        from fastmcp.exceptions import ToolError

        from meta_ads_mcp.coordinator import mcp_server

        fresh = FakeUsersClient(fail_on_batch=1, error=error)
        monkeypatch.setattr(audiences, "get_graph_api_client", lambda fresh=fresh: fresh)
        with pytest.raises(ToolError) as tool_error:
            asyncio.run(mcp_server.call_tool(tool, {
                "audience_id": "aud_1", "schema": "EMAIL", "rows": [[raw]],
            }))
        assert raw not in str(tool_error.value) and digest not in str(tool_error.value)

    assert raw not in caplog.text and digest not in caplog.text


def test_audience_users_error_mid_upload_reports_progress(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "AUDIENCE_USERS_BATCH_SIZE", 1)
    error = audiences.MetaApiError(message="(#2650) Failed to update", status_code=400, code=2650)
    client = FakeUsersClient(fail_on_batch=2, error=error)
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    with pytest.raises(audiences.MetaApiError, match="batch 2 of 2") as excinfo:
        asyncio.run(audiences.add_audience_users(
            audience_id="aud_1", schema=["EMAIL"], rows=[["a@example.com"], ["b@example.com"]],
        ))
    assert "1 earlier batch" in str(excinfo.value)
    assert excinfo.value.code == 2650


def test_audience_users_batches_share_one_session(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "AUDIENCE_USERS_BATCH_SIZE", 2)
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)

    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL"], rows=[[f"u{index}@example.com"] for index in range(5)],
    ))

    sessions = [request["data"]["session"] for request in client.requests]
    assert len(sessions) == 3
    assert len({session["session_id"] for session in sessions}) == 1
    assert isinstance(sessions[0]["session_id"], int) and sessions[0]["session_id"] > 0
    assert [session["batch_seq"] for session in sessions] == [1, 2, 3]
    assert [session["last_batch_flag"] for session in sessions] == [False, False, True]
    assert all(session["estimated_num_total"] == 5 for session in sessions)
    assert [len(request["data"]["payload"]["data"]) for request in client.requests] == [2, 2, 1]
    assert result["session"]["batches"] == 3
    assert result["session"]["session_id"] == str(sessions[0]["session_id"])
    assert result["meta"] == {"num_received": 5, "num_invalid_entries": 0}
    assert "invalid_entry_samples" not in json.dumps(result)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"audience_id": " ", "schema": ["EMAIL"], "rows": [["a@example.com"]]}, "audience_id is required"),
        ({"audience_id": "aud_1", "schema": [], "rows": [["a@example.com"]]}, "schema"),
        ({"audience_id": "aud_1", "schema": ["EMAIL_SHA256"], "rows": [["a@example.com"]]}, "Unsupported schema key"),
        ({"audience_id": "aud_1", "schema": ["EMAIL", "email"], "rows": [["a", "b"]]}, "more than once"),
        ({"audience_id": "aud_1", "schema": ["EMAIL", "PHONE"], "rows": [["a@example.com"]]}, "Row 1"),
        ({"audience_id": "aud_1", "schema": ["EMAIL"], "rows": [[""], ["nope"]]}, "No valid values"),
        ({"audience_id": "aud_1", "schema": ["EMAIL"], "rows": [[123]]}, "Row 1"),
    ],
)
def test_audience_users_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match=message):
        asyncio.run(audiences.add_audience_users(**arguments))


def test_remove_audience_users_sends_delete_with_body_not_query(monkeypatch) -> None:
    import httpx

    from meta_ads_mcp.config import Settings
    from meta_ads_mcp.graph_api import GraphAPIClient

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"audience_id": "aud_1", "session_id": "1", "num_received": 1,
                                         "num_invalid_entries": 0})

    settings = Settings(
        access_token="test-token", api_version="v26.0", default_account_id=None,
        app_id=None, app_secret=None, redirect_uri=None, log_level="INFO",
        host="127.0.0.1", port=8000, request_timeout=30, max_retries=0,
    )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            monkeypatch.setattr(GraphAPIClient, "_get_shared_client", lambda self, **kwargs: http_client)
            graph_client = GraphAPIClient(settings=settings, access_token_override="test-token")
            monkeypatch.setattr(audiences, "get_graph_api_client", lambda: graph_client)
            return await audiences.remove_audience_users(
                audience_id="aud_1", schema=["EMAIL"], rows=[["a@example.com"]],
            )

    result = asyncio.run(run())
    request = seen[0]
    assert request.method == "DELETE"
    assert request.url.path == "/v26.0/aud_1/users"
    assert "payload" not in str(request.url)
    body = request.content.decode()
    assert "payload=" in body and "session=" in body
    assert _sha("a@example.com") in httpx.QueryParams(body)["payload"]
    assert result["meta"]["num_received"] == 1


def test_audience_users_file_errors_make_no_call(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match="readable file"):
        asyncio.run(audiences.add_audience_users(
            audience_id="aud_1", schema=["EMAIL"], file_path=str(tmp_path / "missing.csv"),
        ))
    csv_path = tmp_path / "list.csv"
    csv_path.write_text("mail,phone\nsecret@example.com,1\n")
    with pytest.raises(audiences.ValidationError, match="EMAIL") as excinfo:
        asyncio.run(audiences.add_audience_users(audience_id="aud_1", schema=["EMAIL"], file_path=str(csv_path)))
    assert "secret@example.com" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("content", "line"),
    [
        # An unterminated quote would otherwise swallow the following records.
        ('email,fn\n"valid@example.invalid,Alice\nnext@example.invalid,Bob\n', 2),
        ('email,fn\nfirst@example.invalid,Ann\n"second@example.invalid"x,Bob\n', 3),
    ],
)
@pytest.mark.parametrize("tool", USER_TOOLS)
def test_audience_users_malformed_csv_is_rejected_before_upload(monkeypatch, tmp_path, tool, content, line) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    csv_path = tmp_path / "ragged.csv"
    csv_path.write_text(content)
    with pytest.raises(audiences.ValidationError, match=f"line {line}") as excinfo:
        asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL", "FN"], file_path=str(csv_path)))
    message = str(excinfo.value)
    for value in ("valid@example.invalid", "next@example.invalid", "second@example.invalid", "Alice", "Bob", "Ann"):
        assert value not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_audience_users_csv_keeps_bom_header_matching_and_blank_columns(monkeypatch, tmp_path) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    csv_path = tmp_path / "bom.csv"
    csv_path.write_bytes('﻿Email,"First, Name",fn\n"a@example.com","x",Ann\nb@example.com,,\n\n'.encode("utf-8"))
    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL", "FN"], file_path=str(csv_path),
    ))
    assert client.requests[0]["data"]["payload"]["data"] == [
        [_sha("a@example.com"), _sha("ann")],
        [_sha("b@example.com"), ""],
    ]
    assert result["rows_read"] == 2
    assert result["invalid_values"] == 1
