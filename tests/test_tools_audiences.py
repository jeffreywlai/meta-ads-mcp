"""Audience tool tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

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
    """Records audience user uploads; can fail on one batch with a value-echoing error.

    num_received follows Meta's add-members guide: the total received in the session so far.
    """

    def __init__(
        self,
        *,
        fail_on_batch: int | None = None,
        error: Exception | None = None,
        invalid_entries: list[int] | None = None,
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self.fail_on_batch = fail_on_batch
        self.error = error
        self.invalid_entries = invalid_entries
        self.received = 0

    def _record(self, method: str, parent_id: str, edge: str, data: dict) -> dict:
        self.requests.append({"method": method, "parent_id": parent_id, "edge": edge, "data": data})
        if self.fail_on_batch == len(self.requests):
            raise self.error
        rows = data["payload"]["data"]
        self.received += len(rows)
        return {
            "audience_id": parent_id,
            "session_id": str(data["session"]["session_id"]),
            "num_received": self.received,
            "num_invalid_entries": self.invalid_entries[len(self.requests) - 1] if self.invalid_entries else 0,
            "invalid_entry_samples": {next(value for value in rows[0] if value): "sample echoing a hash"},
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


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PHONE", "1"),
        ("PHONE", "12345"),
        ("PHONE", "1234567890123456"),
        ("PHONE", "+1 555 ٠١٠ 9999"),
        ("ZIP", "!!!"),
        ("ZIP", "9"),
        ("ST", "123"),
        ("ST", "Москва"),
        ("CT", "Москва"),
        ("CT", "123"),
        ("COUNTRY", "zz"),
        ("COUNTRY", "usa"),
        ("DOBM", "²"),
        ("DOBD", "٣"),
        ("DOBY", "１９９０"),
    ],
)
def test_audience_users_malformed_values_are_dropped_not_hashed(monkeypatch, key, value) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL", key], rows=[["valid@example.invalid", value]],
    ))
    assert client.requests[0]["data"]["payload"]["data"] == [[_sha("valid@example.invalid"), ""]]
    assert result["invalid_values"] == 1


@pytest.mark.parametrize(
    ("schema", "row", "expected"),
    [
        # Meta's normalization table: CT is a-z only; ST is the 2-letter ANSI code for
        # US states and lowercase letters without punctuation or spaces elsewhere.
        (["CT"], ["München"], ["munchen"]),
        (["CT"], ["St. Louis"], ["stlouis"]),
        (["ST"], ["California"], ["ca"]),
        (["ST"], [" NY "], ["ny"]),
        (["ST"], ["New-York"], ["ny"]),
        (["COUNTRY", "ST"], ["US", "California"], ["us", "ca"]),
        (["COUNTRY", "ST"], ["US", "Bavaria"], ["us", ""]),
        (["COUNTRY", "ST"], ["DE", "Bayern"], ["de", "bayern"]),
        (["COUNTRY", "ST"], ["FR", "Île-de-France"], ["fr", "iledefrance"]),
        # ZIP: lowercase without spaces, first 5 digits in the US, Area/District/Sector in the UK.
        (["ZIP"], ["94025-1234"], ["94025"]),
        (["COUNTRY", "ZIP"], ["US", "1234"], ["us", ""]),
        (["COUNTRY", "ZIP"], ["GB", "SW1A 1AA"], ["gb", "sw1a1"]),
        (["COUNTRY", "ZIP"], ["CA", "K1A 0B1"], ["ca", "k1a0b1"]),
        (["ZIP"], ["100-0001"], ["100-0001"]),
        # PHONE: symbols, letters and leading zeros removed; 7 to 15 digits remain.
        (["PHONE"], ["001 555 010 9999"], ["15550109999"]),
        (["FN"], ["Zoë-Ann"], ["zoëann"]),
    ],
)
def test_audience_users_values_follow_published_format(monkeypatch, schema, row, expected) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    asyncio.run(audiences.add_audience_users(audience_id="aud_1", schema=schema, rows=[row]))
    assert client.requests[0]["data"]["payload"]["data"] == [[_sha(value) if value else "" for value in expected]]


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

    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.coordinator import mcp_server

    class EchoingSuccess(FakeUsersClient):
        """Answers like Meta but also echoes everything it was sent, so a leak would show."""

        def _record(self, method, parent_id, edge, data):
            response = super()._record(method, parent_id, edge, data)
            return {**response, "echo": data, "note": f"received {raw}"}

    client = EchoingSuccess()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL"], rows=[[raw]]))
    assert digest in json.dumps(client.requests)
    serialized = json.dumps(result)
    assert raw not in serialized and digest not in serialized

    errors: list[tuple[Exception, type[Exception]]] = [
        (_echoing_errors([raw, raw.upper(), digest])[0], audiences.MetaApiError),
        (_echoing_errors([raw, raw.upper(), digest])[1], audiences.RateLimitError),
        (RuntimeError(f"transport failed while sending {raw} {digest}"), audiences.MetaApiError),
    ]
    for error, expected in errors:
        # The fake really carries the values, so the assertions below can fail.
        assert raw in str(error) and digest in str(error)
        failing = FakeUsersClient(fail_on_batch=1, error=error)
        monkeypatch.setattr(audiences, "get_graph_api_client", lambda failing=failing: failing)
        with pytest.raises(expected) as excinfo:
            asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL"], rows=[[raw]]))
        assert type(excinfo.value) is expected
        rendered = _rendered_chain(excinfo.value)
        assert raw not in rendered.lower() and digest not in rendered
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None

        fresh = FakeUsersClient(fail_on_batch=1, error=error)
        monkeypatch.setattr(audiences, "get_graph_api_client", lambda fresh=fresh: fresh)
        caplog.clear()
        with pytest.raises(ToolError) as tool_error:
            asyncio.run(mcp_server.call_tool(tool, {
                "audience_id": "aud_1", "schema": "EMAIL", "rows": [[raw]],
            }))
        assert json.loads(str(tool_error.value))["error"]["type"] in {"meta_api", "rate_limit"}
        _assert_nothing_echoed(tool_error.value, caplog, raw)
        _assert_nothing_echoed(tool_error.value, caplog, digest)


def _rendered_chain(error: BaseException) -> str:
    """Render an exception, its public payload and every chained cause and context."""
    parts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts += [str(current), repr(current), json.dumps(getattr(current, "to_public_dict", lambda: {})())]
        pending += [current.__cause__, current.__context__]
    return "\n".join(parts)


def _contains_value(text: str, value: str) -> bool:
    """True when value appears in text as a whole token, ignoring case (short values such as "m" included)."""
    return re.search(rf"(?<![^\W_]){re.escape(value)}(?![^\W_])", text, flags=re.IGNORECASE) is not None


def _echoing_errors(values: list[str]) -> list[Exception]:
    text = " ".join(f"'{value}'" for value in values)
    return [
        audiences.MetaApiError(
            message=f"Invalid customer value: {text}", status_code=400, code=100, subcode=1870053,
            user_title=f"Bad {text}", user_message=f"Check {text}", trace_id=f"trace {text}",
            error_data={"sample": text}, details={"error": {"message": text}}, operation=f"op {text}",
            is_transient=False,
        ),
        audiences.RateLimitError(
            message=f"Too many calls for {text}", retry_after_seconds=60.0, code=17, subcode=2446079,
            usage={"echo": text}, operation=f"op {text}",
        ),
    ]


@pytest.mark.parametrize("tool", USER_TOOLS)
@pytest.mark.parametrize(
    ("schema", "raw", "normalized"),
    [
        (["FN"], "Li", "li"),
        (["GEN"], "m", "m"),
        (["DOBM"], "3", "03"),
        (["FN"], "Zoë-Ann", "zoëann"),
        (["CT"], "Saint-Étienne", "saintetienne"),
    ],
)
def test_audience_users_upload_errors_withhold_meta_text(monkeypatch, caplog, tool, schema, raw, normalized) -> None:
    caplog.set_level(logging.DEBUG)
    for error, expected_type in zip(_echoing_errors([raw, normalized, _sha(normalized)]), ["meta_api", "rate_limit"]):
        client = FakeUsersClient(fail_on_batch=1, error=error)
        monkeypatch.setattr(audiences, "get_graph_api_client", lambda client=client: client)
        with pytest.raises((audiences.MetaApiError, audiences.RateLimitError)) as excinfo:
            asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=schema, rows=[[raw]]))
        rendered = _rendered_chain(excinfo.value)
        for value in (raw, normalized, _sha(normalized)):
            assert not _contains_value(rendered, value)
        public = excinfo.value.to_public_dict()
        assert public["type"] == expected_type
        assert (public["code"], public["subcode"]) == ((100, 1870053) if expected_type == "meta_api" else (17, 2446079))
        assert {"user_title", "user_message", "trace_id", "error_data", "usage", "operation"}.isdisjoint(public)
        assert "batch 1 of 1" in public["message"] and "0 rows" in public["message"]
    assert not _contains_value(caplog.text, raw) and not _contains_value(caplog.text, normalized)


def test_audience_users_error_withholds_values_from_earlier_batches(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "AUDIENCE_USERS_BATCH_SIZE", 1)
    error = audiences.MetaApiError(
        message="Session failed for customer Rosalind and rosalind", code=2650, subcode=1870148,
        status_code=400, is_transient=True,
    )
    client = FakeUsersClient(fail_on_batch=2, error=error)
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    with pytest.raises(audiences.MetaApiError) as excinfo:
        asyncio.run(audiences.add_audience_users(
            audience_id="aud_1", schema=["FN"], rows=[["Rosalind"], ["Maxwell"]],
        ))
    rendered = _rendered_chain(excinfo.value)
    assert "rosalind" not in rendered.lower() and "maxwell" not in rendered.lower()
    public = excinfo.value.to_public_dict()
    assert public["retryable"] is True
    assert (public["code"], public["subcode"], public["status_code"]) == (2650, 1870148, 400)
    assert "batch 2 of 2" in public["message"] and "1 earlier batch" in public["message"]
    assert "1 rows" in public["message"] or "1 row " in public["message"]


def test_audience_users_transport_error_is_withheld(monkeypatch) -> None:
    raw = "ab@example.com"
    client = FakeUsersClient(fail_on_batch=1, error=RuntimeError(f"connection reset while sending {raw} {_sha(raw)}"))
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    with pytest.raises(audiences.MetaApiError) as excinfo:
        asyncio.run(audiences.add_audience_users(audience_id="aud_1", schema=["EMAIL"], rows=[[raw]]))
    rendered = _rendered_chain(excinfo.value)
    assert raw not in rendered and _sha(raw) not in rendered
    assert excinfo.value.mutation_outcome_unknown is True
    assert "RuntimeError" in str(excinfo.value)


_PRIVATE_EMAIL = "private.customer@example.invalid"
_PRIVATE_DIGEST = hashlib.sha256(_PRIVATE_EMAIL.encode()).hexdigest()


def _malformed_upload_arguments(value: str) -> list[tuple[dict[str, object], str]]:
    """Wrongly shaped arguments that each carry a customer value, with the expected structural message."""
    return [
        ({"audience_id": "123", "schema": ["EMAIL"], "rows": value}, "rows must be a list"),
        ({"audience_id": "123", "schema": ["EMAIL"], "rows": [value]}, "Row 1 must be a list"),
        ({"audience_id": "123", "schema": ["EMAIL"], "rows": {"customer": value}}, "rows must be a list"),
        ({"audience_id": "123", "schema": ["EMAIL", "PHONE"], "rows": [["a@example.com", 5551234567]]}, "Row 1, PHONE"),
        ({"audience_id": "123", "schema": ["EMAIL"], "rows": [[{"email": value}]]}, "Row 1, EMAIL"),
        ({"audience_id": "123", "schema": ["EMAIL"], "rows": [["a@example.com"], [value, value]]}, "Row 2 must have exactly 1"),
        ({"audience_id": "123", "schema": {"EMAIL": value}, "rows": [["a@example.com"]]}, "schema must list"),
        ({"audience_id": "123", "schema": [value], "rows": [["a@example.com"]]}, "schema position 1"),
        ({"audience_id": "123", "schema": value, "rows": [["a@example.com"]]}, "schema position 1"),
        ({"audience_id": [value], "schema": ["EMAIL"], "rows": [["a@example.com"]]}, "audience_id must be a string"),
        ({"schema": ["EMAIL"], "rows": [[value]]}, "audience_id is required"),
        ({"audience_id": "123", "rows": [[value]]}, "schema must list"),
        ({"audience_id": "123", "schema": ["EMAIL"], "file_path": {"path": value}}, "file_path must be a string"),
        ({"audience_id": "123", "schema": ["EMAIL"], "rows": [[value]], "pre_hashed": value}, "pre_hashed must be true or false"),
    ]


def _assert_nothing_echoed(error: BaseException, caplog, value: str) -> None:
    rendered = _rendered_chain(error)
    assert value not in rendered
    assert value not in caplog.text
    for record in caplog.records:
        assert value not in record.getMessage()
        assert value not in repr(record.args)
        if record.exc_info:
            assert value not in _rendered_chain(record.exc_info[1])


@pytest.mark.parametrize("tool", USER_TOOLS)
@pytest.mark.parametrize("routed", [False, True], ids=["direct", "call_tool_proxy"])
@pytest.mark.parametrize("value", [_PRIVATE_EMAIL, _PRIVATE_DIGEST], ids=["raw", "hashed"])
def test_audience_users_mcp_argument_errors_never_echo_values(monkeypatch, caplog, tool, routed, value) -> None:
    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.coordinator import mcp_server

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    for arguments, message in _malformed_upload_arguments(value):
        name, call_arguments = (("call_tool", {"name": tool, "arguments": arguments}) if routed else (tool, arguments))
        caplog.clear()
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(mcp_server.call_tool(name, call_arguments))
        assert message in str(excinfo.value), (arguments, str(excinfo.value))
        # The server did log the failure (with its traceback), so the log check below is not vacuous.
        assert any(record.exc_info for record in caplog.records)
        _assert_nothing_echoed(excinfo.value, caplog, value)


@pytest.mark.parametrize("value", [_PRIVATE_EMAIL, _PRIVATE_DIGEST], ids=["raw", "hashed"])
@pytest.mark.parametrize("shape", ["list", "nested_list", "number_list"])
def test_call_tool_proxy_rejects_non_object_arguments_without_echo(monkeypatch, caplog, value, shape) -> None:
    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.coordinator import mcp_server

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    arguments = {"list": [value], "nested_list": [[value]], "number_list": [value, 1]}[shape]
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(mcp_server.call_tool("call_tool", {"name": "add_audience_users", "arguments": arguments}))
    assert "arguments must be a JSON object" in str(excinfo.value)
    _assert_nothing_echoed(excinfo.value, caplog, value)


def test_call_tool_proxy_schema_is_unchanged_by_loose_arguments() -> None:
    from meta_ads_mcp.coordinator import mcp_server

    tools = {tool.name: tool for tool in asyncio.run(mcp_server.list_tools())}
    assert tools["call_tool"].parameters["properties"]["arguments"] == {
        "anyOf": [{"additionalProperties": True, "type": "object"}, {"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "Tool arguments as an object or object-valued JSON string",
    }


def test_audience_users_mcp_schema_still_describes_the_expected_shape() -> None:
    from meta_ads_mcp.coordinator import mcp_server, serialize_search_results_compact

    for tool_name in USER_TOOLS:
        tool = asyncio.run(mcp_server.get_tool(tool_name))
        parameters = tool.parameters
        assert parameters["required"] == ["audience_id", "schema"]
        assert parameters["additionalProperties"] is False
        properties = parameters["properties"]
        assert properties["audience_id"]["type"] == "string"
        assert {choice["type"] for choice in properties["schema"]["anyOf"]} == {"string", "array"}
        rows_array = next(choice for choice in properties["rows"]["anyOf"] if choice["type"] == "array")
        assert rows_array["items"] == {"type": "array", "items": {"type": "string"}}
        assert rows_array["maxItems"] == audiences.AUDIENCE_USERS_INLINE_ROW_LIMIT
        assert properties["pre_hashed"]["type"] == "boolean"
        assert properties["pre_hashed"]["default"] is False
        summary = serialize_search_results_compact([tool])
        assert "req: audience_id (string), schema (string or list[string])" in summary
        assert "rows (list[list[string]] or null)" in summary


def test_audience_users_mcp_call_with_valid_arguments_still_uploads(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(mcp_server.call_tool("call_tool", {
        "name": "add_audience_users",
        "arguments": {"audience_id": "aud_1", "schema": "EMAIL,PHONE", "rows": [["a@example.com", None]],
                      "pre_hashed": "false"},
    }))
    assert result.structured_content["rows_sent"] == 1
    assert client.requests[0]["data"]["payload"]["data"] == [[_sha("a@example.com"), ""]]


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
    assert [batch["num_received"] for batch in result["batches"]] == [2, 4, 5]
    # num_received is a session-to-date count, so the latest value is the session total.
    assert result["meta"]["num_received"] == 5 == result["rows_sent"]
    assert "invalid_entry_samples" not in json.dumps(result)


def test_audience_users_received_count_is_latest_session_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "AUDIENCE_USERS_BATCH_SIZE", 1)

    class Snapshots(FakeUsersClient):
        snapshots = iter([10_000, 20_000, None])

        def _record(self, method, parent_id, edge, data):
            response = super()._record(method, parent_id, edge, data)
            response["num_received"] = next(self.snapshots)
            return response

    client = Snapshots()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL"], rows=[[f"u{index}@example.com"] for index in range(3)],
    ))
    # The last batch did not report a count, so the latest reported snapshot is used and labelled.
    assert result["meta"]["num_received"] == 20_000
    assert result["meta"]["num_received_as_of_batch"] == 2
    assert [batch["num_received"] for batch in result["batches"]] == [10_000, 20_000, None]


def test_audience_users_invalid_entry_count_is_not_summed_across_batches(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "AUDIENCE_USERS_BATCH_SIZE", 1)
    client = FakeUsersClient(invalid_entries=[1, 1])
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL"], rows=[["a@example.com"], ["b@example.com"]],
    ))
    # Meta does not document whether this counter is per request or session-to-date.
    assert result["meta"]["num_invalid_entries"] is None
    assert "num_invalid_entries" in result["meta"]["counter_note"]
    assert [batch["num_invalid_entries"] for batch in result["batches"]] == [1, 1]

    single = FakeUsersClient(invalid_entries=[3])
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: single)
    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL"], rows=[["a@example.com"]],
    ))
    assert result["meta"] == {"num_received": 1, "num_received_as_of_batch": 1, "num_invalid_entries": 3}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"audience_id": " ", "schema": ["EMAIL"], "rows": [["a@example.com"]]}, "audience_id is required"),
        ({"audience_id": "aud_1", "schema": [], "rows": [["a@example.com"]]}, "schema"),
        ({"audience_id": "aud_1", "schema": ["EMAIL", "EMAIL_SHA256"], "rows": [["a@example.com", "b"]]},
         "schema position 2 is not a supported key"),
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
    with pytest.raises(audiences.ValidationError, match="does not point to a file"):
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


# update_custom_audience: named rule (phase two, P4)


class RecordingAudienceClient(FakeAudienceClient):
    """Records every read and write in order, so a test can pin the exact requests."""

    def __init__(self, previous: dict | None = None, read_error: Exception | None = None) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.previous = previous
        self.read_error = read_error

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or []), params))
        if self.read_error is not None:
            raise self.read_error
        if self.previous is not None:
            return dict(self.previous)
        return await super().get_object(object_id, fields=fields, params=params)

    async def update_object(self, object_id: str, *, data):
        self.calls.append(("update", object_id, dict(data)))
        return {"success": True}


OLD_AUDIENCE_READ_FIELDS = [
    "id", "name", "subtype", "approximate_count_lower_bound", "approximate_count_upper_bound",
    "time_updated", "operation_status", "description", "customer_file_source", "retention_days",
    "lookalike_spec",
]
WEBSITE_RULE = {
    "inclusions": {
        "operator": "or",
        "rules": [{"event_sources": [{"id": "px_1", "type": "pixel"}], "retention_seconds": 2592000,
                   "filter": {"operator": "and", "filters": [{"field": "url", "operator": "i_contains",
                                                              "value": "checkout"}]}}],
    }
}


def test_update_custom_audience_without_rule_is_unchanged(monkeypatch) -> None:
    """An old-style call (named fields, or rule through params) sends exactly the requests it sent before rule was named."""
    client = RecordingAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.update_custom_audience(
        audience_id="aud_1", name="Renamed", description="New", retention_days=45,
        customer_file_source="USER_PROVIDED_ONLY",
    ))
    assert client.calls == [
        ("get", "aud_1", OLD_AUDIENCE_READ_FIELDS, None),
        ("update", "aud_1", {"name": "Renamed", "description": "New", "retention_days": 45,
                             "customer_file_source": "USER_PROVIDED_ONLY"}),
    ]
    assert result == {
        "ok": True,
        "action": "update_custom_audience",
        "target": {"audience_id": "aud_1"},
        "previous": {"name": "Existing Audience", "description": "Old", "retention_days": 30,
                     "customer_file_source": "USER_PROVIDED_ONLY"},
        "current": {"name": "Renamed", "description": "New", "retention_days": 45,
                    "customer_file_source": "USER_PROVIDED_ONLY"},
    }

    client = RecordingAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.update_custom_audience(audience_id="aud_1", params={"rule": WEBSITE_RULE}))
    # The write is exactly what it was. The read now also asks for the stored rule, so
    # the prior rule comes back and the edit can be undone; the caller only gains a field.
    assert client.calls == [
        ("get", "aud_1", [*OLD_AUDIENCE_READ_FIELDS, "rule"], None),
        ("update", "aud_1", {"rule": WEBSITE_RULE}),
    ]
    previous = dict(result["previous"])
    assert "rule" in previous
    previous.pop("rule")
    assert previous == {"name": "Existing Audience", "description": "Old", "retention_days": 30,
                        "customer_file_source": "USER_PROVIDED_ONLY"}
    assert result["current"] == {"rule": WEBSITE_RULE}


@pytest.mark.parametrize("rule", [WEBSITE_RULE, json.dumps(WEBSITE_RULE), " " + json.dumps(WEBSITE_RULE) + " "])
def test_update_custom_audience_named_rule_reads_rule_first_and_sends_it(monkeypatch, rule) -> None:
    stored_rule = json.dumps({"inclusions": {"operator": "or", "rules": []}})
    client = RecordingAudienceClient(previous={
        "id": "aud_1", "name": "Site visitors", "subtype": "WEBSITE", "description": None, "rule": stored_rule,
    })
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.update_custom_audience(audience_id="aud_1", name="Checkout visitors", rule=rule))
    assert client.calls == [
        ("get", "aud_1", [*OLD_AUDIENCE_READ_FIELDS, "rule"], None),
        ("update", "aud_1", {"name": "Checkout visitors", "rule": WEBSITE_RULE}),
    ]
    assert result["previous"]["rule"] == stored_rule
    assert result["previous"]["name"] == "Site visitors"
    assert result["current"] == {"name": "Checkout visitors", "rule": WEBSITE_RULE}


@pytest.mark.parametrize("rule", [WEBSITE_RULE, json.dumps(WEBSITE_RULE)])
def test_update_custom_audience_rule_through_mcp_boundary(monkeypatch, rule) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    client = RecordingAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(mcp_server.call_tool(
        "update_custom_audience", {"audience_id": "aud_1", "rule": rule},
    )).structured_content
    assert client.calls[-1] == ("update", "aud_1", {"rule": WEBSITE_RULE})
    assert result["current"] == {"rule": WEBSITE_RULE}


def test_update_custom_audience_rule_named_and_in_params_raises_before_network(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match="rule") as excinfo:
        asyncio.run(audiences.update_custom_audience(
            audience_id="aud_1", rule=WEBSITE_RULE, params={"rule": WEBSITE_RULE},
        ))
    assert "params" in str(excinfo.value)


@pytest.mark.parametrize("rule", ["", "   ", "not json", "[1, 2]", "42", {}, "{}"])
def test_update_custom_audience_rejects_a_rule_that_is_not_a_json_object(monkeypatch, rule) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match="rule"):
        asyncio.run(audiences.update_custom_audience(audience_id="aud_1", rule=rule))


def test_update_custom_audience_rule_failed_pre_read_writes_nothing(monkeypatch) -> None:
    from meta_ads_mcp.errors import NotFoundError

    client = RecordingAudienceClient(read_error=NotFoundError("Meta object or edge not found: aud_1"))
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    with pytest.raises(NotFoundError):
        asyncio.run(audiences.update_custom_audience(audience_id="aud_1", rule=WEBSITE_RULE))
    assert [call[0] for call in client.calls] == ["get"]


@pytest.mark.parametrize("tool", ["add_audience_users", "remove_audience_users"])
def test_audience_users_only_read_files_from_the_upload_folder(monkeypatch, tmp_path, tool) -> None:
    """A path from the model must not be able to send some other file on this machine to Meta."""
    from meta_ads_mcp.config import reload_settings

    allowed = tmp_path / "uploads"
    allowed.mkdir()
    monkeypatch.setenv("META_UPLOAD_DIR", str(allowed))
    reload_settings()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    outside = tmp_path / "customers-outside.csv"
    outside.write_text("EMAIL\nprivate.person@example.com\n")
    (allowed / "link.csv").symlink_to(outside)
    (allowed / "list.txt").write_text("EMAIL\nprivate.person@example.com\n")
    function = getattr(audiences, tool)
    for path, expected in (
        (str(outside), "inside the upload folder"),
        ("../customers-outside.csv", "inside the upload folder"),
        ("link.csv", "inside the upload folder"),
        ("list.txt", "must end in"),
    ):
        with pytest.raises(audiences.ValidationError, match=expected) as error:
            asyncio.run(function(audience_id="aud_1", schema=["EMAIL"], file_path=path))
        assert "private.person" not in str(error.value)
        assert "customers-outside" not in str(error.value)


def test_audience_users_file_reading_is_off_without_an_upload_folder(monkeypatch, tmp_path) -> None:
    from meta_ads_mcp.config import reload_settings

    monkeypatch.delenv("META_UPLOAD_DIR", raising=False)
    reload_settings()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    target = tmp_path / "list.csv"
    target.write_text("EMAIL\na@example.com\n")
    with pytest.raises(audiences.ValidationError, match="META_UPLOAD_DIR"):
        asyncio.run(audiences.add_audience_users(audience_id="aud_1", schema=["EMAIL"], file_path=str(target)))


# Round three (P4), G18: no exception raised while parsing customer data may keep that data
# reachable through its chain (__cause__ or __context__), even when the chain is not displayed.

SYNTHETIC_CUSTOMER = "synthetic.private.customer@example.invalid"


def _exception_chain(error: BaseException) -> list[BaseException]:
    """Every exception reachable from error through __cause__ and __context__, cycles included once."""
    found: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        pending += [current.__cause__, current.__context__]
    return found


def _assert_chain_holds_no_customer_data(error: BaseException, *values: str) -> None:
    for link in _exception_chain(error):
        rendered = "\n".join([
            str(link), repr(link), repr(getattr(link, "args", ())), repr(getattr(link, "object", None)),
        ])
        for value in values:
            assert value not in rendered, (type(link).__name__, rendered)
            assert value.encode() not in rendered.encode("utf-8", "backslashreplace"), type(link).__name__


def test_g18_customer_file_decode_error_never_retains_raw_customer(monkeypatch, tmp_path) -> None:
    """The reviewer's case: a UTF-8 decode failure must not keep the file's bytes as __context__."""
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    path = tmp_path / "customers.csv"
    path.write_bytes(b"EMAIL\n" + SYNTHETIC_CUSTOMER.encode() + b"\n\xff")
    with pytest.raises(audiences.ValidationError, match="UTF-8") as info:
        asyncio.run(audiences.add_audience_users("123", schema=["EMAIL"], file_path=str(path)))
    _assert_chain_holds_no_customer_data(info.value, SYNTHETIC_CUSTOMER)
    assert info.value.__context__ is None
    assert info.value.__cause__ is None


@pytest.mark.parametrize(("content", "match"), [
    # Not UTF-8.
    (b"EMAIL,FN\n" + SYNTHETIC_CUSTOMER.encode() + b",Ann\xe9\n", "UTF-8"),
    # Malformed quoting: the csv module's own error is replaced.
    (b'EMAIL,FN\n"' + SYNTHETIC_CUSTOMER.encode() + b',Ann\nnext@example.invalid,Bob\n', "not valid CSV"),
    (b'EMAIL,FN\n"' + SYNTHETIC_CUSTOMER.encode() + b'"x,Ann\n', "not valid CSV"),
    # No header row: the first customer row is read as the header and is not echoed.
    (SYNTHETIC_CUSTOMER.encode() + b",Ann\n", "no column named"),
    (SYNTHETIC_CUSTOMER.encode() + b"," + SYNTHETIC_CUSTOMER.encode() + b"\n", "repeats a column"),
])
@pytest.mark.parametrize("tool", USER_TOOLS)
def test_g18_every_customer_file_parse_failure_has_a_clean_exception_chain(monkeypatch, tmp_path, tool, content, match) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    path = tmp_path / "customers.csv"
    path.write_bytes(content)
    with pytest.raises(audiences.ValidationError, match=match) as info:
        asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL", "FN"], file_path=str(path)))
    _assert_chain_holds_no_customer_data(info.value, SYNTHETIC_CUSTOMER, "Ann", "Bob")
    assert info.value.__context__ is None
    assert info.value.__cause__ is None
    # Nor does any server frame on the error's traceback still hold the file's contents.
    traceback = info.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if "meta_ads_mcp" in frame.f_code.co_filename:
            assert SYNTHETIC_CUSTOMER not in repr(frame.f_locals), frame.f_code.co_name
        traceback = traceback.tb_next


@pytest.mark.parametrize(("rows", "kwargs", "match"), [
    ([SYNTHETIC_CUSTOMER], {}, "must be a list"),
    ([[SYNTHETIC_CUSTOMER, "extra"]], {}, "exactly 1 values"),
    ([[{"email": SYNTHETIC_CUSTOMER}]], {}, "must be strings"),
    ([[SYNTHETIC_CUSTOMER]], {"pre_hashed": True}, "SHA-256"),
    # A value that cannot be encoded as UTF-8 (a lone surrogate) is invalid, never a raw codec error.
    ([[SYNTHETIC_CUSTOMER.replace("@", "\ud800@")]], {}, "No valid values"),
])
@pytest.mark.parametrize("tool", USER_TOOLS)
def test_g18_every_inline_row_failure_has_a_clean_exception_chain(monkeypatch, tool, rows, kwargs, match) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match=match) as info:
        asyncio.run(_users_tool(tool)(audience_id="aud_1", schema=["EMAIL"], rows=rows, **kwargs))
    _assert_chain_holds_no_customer_data(info.value, SYNTHETIC_CUSTOMER, SYNTHETIC_CUSTOMER.split("@")[0])
    assert info.value.__context__ is None
    assert info.value.__cause__ is None


def test_g18_value_that_cannot_be_utf8_encoded_is_counted_invalid_not_raised(monkeypatch) -> None:
    client = FakeUsersClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.add_audience_users(
        audience_id="aud_1", schema=["EMAIL"],
        rows=[[SYNTHETIC_CUSTOMER.replace("@", "\ud800@")], ["ok@example.com"]],
    ))
    assert result["invalid_values"] == 1
    assert result["rows_sent"] == 1
    assert client.requests[0]["data"]["payload"]["data"] == [[_sha("ok@example.com")]]


def test_g18_decode_failure_chain_is_clean_through_the_mcp_boundary(tmp_path, caplog) -> None:
    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.coordinator import mcp_server

    path = tmp_path / "customers.csv"
    path.write_bytes(b"EMAIL\n" + SYNTHETIC_CUSTOMER.encode() + b"\n\xff")
    with pytest.raises(ToolError) as info:
        asyncio.run(mcp_server.call_tool(
            "add_audience_users", {"audience_id": "123", "schema": ["EMAIL"], "file_path": str(path)},
        ))
    _assert_chain_holds_no_customer_data(info.value, SYNTHETIC_CUSTOMER)
    assert SYNTHETIC_CUSTOMER not in caplog.text


# create_custom_audience: named is_value_based and audience_labels (round three, P4)


class CreateRecorder(FakeAudienceClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.calls.append(("create", parent_id, edge, dict(data)))
        return {"id": "aud_created"}


def test_create_custom_audience_old_style_call_is_unchanged(monkeypatch) -> None:
    """Calls made before the named parameters existed send exactly the same request and get the same response."""
    client = CreateRecorder()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    minimal = asyncio.run(audiences.create_custom_audience(account_id="123", name="Audience"))
    full = asyncio.run(audiences.create_custom_audience(
        account_id="act_123", name="VBA", subtype="CUSTOM", description="Value list",
        customer_file_source="PARTNER_PROVIDED_ONLY", retention_days=90, prefill=True,
        params={"is_value_based": 1, "audience_labels": ["HIGH_VALUE_CUSTOMERS"], "use_in_campaigns": True},
    ))
    website = asyncio.run(audiences.create_custom_audience(
        account_id="123", name="Site", subtype="WEBSITE", rule={"inclusions": {"operator": "or"}},
    ))
    assert client.calls == [
        ("create", "act_123", "customaudiences", {"name": "Audience", "subtype": "CUSTOM", "prefill": False}),
        ("create", "act_123", "customaudiences", {
            "name": "VBA", "subtype": "CUSTOM", "prefill": True, "description": "Value list",
            "customer_file_source": "PARTNER_PROVIDED_ONLY", "retention_days": 90,
            "is_value_based": 1, "audience_labels": ["HIGH_VALUE_CUSTOMERS"], "use_in_campaigns": True,
        }),
        ("create", "act_123", "customaudiences", {
            "name": "Site", "subtype": "WEBSITE", "prefill": False, "rule": {"inclusions": {"operator": "or"}},
        }),
    ]
    for result in (minimal, full, website):
        assert result == {
            "ok": True, "action": "create_custom_audience", "target": {"account_id": "act_123"},
            "created": {"id": "aud_created"},
        }


def test_create_custom_audience_named_value_based_and_labels_join_the_request(monkeypatch) -> None:
    client = CreateRecorder()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    asyncio.run(audiences.create_custom_audience(
        account_id="123", name="VBA", customer_file_source="PARTNER_PROVIDED_ONLY",
        is_value_based=True, audience_labels=" high_value_customers, Recent_Purchasers ",
    ))
    asyncio.run(audiences.create_custom_audience(
        account_id="123", name="Not value based", is_value_based=False, audience_labels=["AT_RISK"],
    ))
    assert client.calls == [
        ("create", "act_123", "customaudiences", {
            "name": "VBA", "subtype": "CUSTOM", "prefill": False, "customer_file_source": "PARTNER_PROVIDED_ONLY",
            "is_value_based": True, "audience_labels": ["HIGH_VALUE_CUSTOMERS", "RECENT_PURCHASERS"],
        }),
        ("create", "act_123", "customaudiences", {
            "name": "Not value based", "subtype": "CUSTOM", "prefill": False,
            "is_value_based": False, "audience_labels": ["AT_RISK"],
        }),
    ]


def test_create_custom_audience_named_values_through_the_mcp_boundary(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    client = CreateRecorder()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    asyncio.run(mcp_server.call_tool("create_custom_audience", {
        "account_id": "123", "name": "VBA", "is_value_based": True, "audience_labels": "QUALIFIED_LEADS",
    }))
    assert client.calls[0][3]["is_value_based"] is True
    assert client.calls[0][3]["audience_labels"] == ["QUALIFIED_LEADS"]


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"is_value_based": True, "params": {"is_value_based": 1}}, "is_value_based"),
    ({"audience_labels": ["AT_RISK"], "params": {"audience_labels": ["AT_RISK"]}}, "audience_labels"),
    ({"audience_labels": ["NOT_A_LABEL"]}, "NOT_A_LABEL"),
    ({"audience_labels": [" "]}, "audience_labels"),
    ({"audience_labels": []}, "audience_labels"),
    ({"is_value_based": True, "subtype": "WEBSITE"}, "CUSTOM"),
])
def test_create_custom_audience_named_values_are_validated_before_any_request(monkeypatch, kwargs, match) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match=match):
        asyncio.run(audiences.create_custom_audience(account_id="123", name="Audience", **kwargs))


def test_g18_rule_that_is_not_json_has_a_clean_exception_chain(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    rule = '{"inclusions": "' + SYNTHETIC_CUSTOMER
    with pytest.raises(audiences.ValidationError, match="rule must be") as info:
        asyncio.run(audiences.update_custom_audience(audience_id="aud_1", rule=rule))
    _assert_chain_holds_no_customer_data(info.value, SYNTHETIC_CUSTOMER)
    assert info.value.__context__ is None


# update_custom_audience: named audience_labels (round four, P46)


def test_update_custom_audience_without_labels_is_unchanged(monkeypatch) -> None:
    """Calls made before audience_labels was named send exactly the same requests and get the same response."""
    client = RecordingAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    named = asyncio.run(audiences.update_custom_audience(audience_id="aud_1", name="Renamed"))
    through_params = asyncio.run(audiences.update_custom_audience(
        audience_id="aud_1", description="New", params={"audience_labels": ["AT_RISK"]},
    ))
    assert client.calls == [
        ("get", "aud_1", OLD_AUDIENCE_READ_FIELDS, None),
        ("update", "aud_1", {"name": "Renamed"}),
        ("get", "aud_1", OLD_AUDIENCE_READ_FIELDS, None),
        ("update", "aud_1", {"description": "New", "audience_labels": ["AT_RISK"]}),
    ]
    prior = {"name": "Existing Audience", "description": "Old", "retention_days": 30,
             "customer_file_source": "USER_PROVIDED_ONLY"}
    assert named == {
        "ok": True, "action": "update_custom_audience", "target": {"audience_id": "aud_1"},
        "previous": prior, "current": {"name": "Renamed"},
    }
    assert through_params == {
        "ok": True, "action": "update_custom_audience", "target": {"audience_id": "aud_1"},
        "previous": prior, "current": {"description": "New", "audience_labels": ["AT_RISK"]},
    }


def test_update_custom_audience_named_labels_read_old_labels_first_and_send_them(monkeypatch) -> None:
    client = RecordingAudienceClient(previous={
        "id": "aud_1", "name": "Buyers", "description": None, "audience_labels": ["GENERAL_CUSTOMERS"],
    })
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.update_custom_audience(
        audience_id="aud_1", audience_labels=" high_value_customers, Recent_Purchasers, HIGH_VALUE_CUSTOMERS ",
    ))
    assert client.calls == [
        ("get", "aud_1", [*OLD_AUDIENCE_READ_FIELDS, "audience_labels"], None),
        ("update", "aud_1", {"audience_labels": ["HIGH_VALUE_CUSTOMERS", "RECENT_PURCHASERS"]}),
    ]
    assert result["previous"]["audience_labels"] == ["GENERAL_CUSTOMERS"]
    assert result["current"] == {"audience_labels": ["HIGH_VALUE_CUSTOMERS", "RECENT_PURCHASERS"]}


def test_update_custom_audience_named_labels_with_a_rule_read_both(monkeypatch) -> None:
    client = RecordingAudienceClient(previous={"id": "aud_1", "rule": "{}"})
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.update_custom_audience(
        audience_id="aud_1", rule=WEBSITE_RULE, audience_labels=["ENGAGED_USERS"],
    ))
    assert client.calls[0] == ("get", "aud_1", [*OLD_AUDIENCE_READ_FIELDS, "rule", "audience_labels"], None)
    assert client.calls[1] == ("update", "aud_1", {"rule": WEBSITE_RULE, "audience_labels": ["ENGAGED_USERS"]})
    assert result["previous"]["rule"] == "{}"
    # Meta returned no labels (it omits an empty field), so previous says None rather than dropping the key.
    assert "audience_labels" in result["previous"] and result["previous"]["audience_labels"] is None


def test_update_custom_audience_labels_through_mcp_boundary(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    client = RecordingAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(mcp_server.call_tool(
        "update_custom_audience", {"audience_id": "aud_1", "audience_labels": "qualified_leads"},
    )).structured_content
    assert client.calls[-1] == ("update", "aud_1", {"audience_labels": ["QUALIFIED_LEADS"]})
    assert result["current"] == {"audience_labels": ["QUALIFIED_LEADS"]}


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"audience_labels": ["AT_RISK"], "params": {"audience_labels": ["AT_RISK"]}}, "audience_labels"),
    ({"audience_labels": ["NOT_A_LABEL"]}, "NOT_A_LABEL"),
    ({"audience_labels": ["APP_INSTALLERS"]}, "APP_INSTALLERS"),
    ({"audience_labels": [" "]}, "audience_labels"),
    ({"audience_labels": []}, "audience_labels"),
    ({"audience_labels": ""}, "audience_labels"),
])
def test_update_custom_audience_named_labels_are_validated_before_any_request(monkeypatch, kwargs, match) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(audiences.ValidationError, match=match) as excinfo:
        asyncio.run(audiences.update_custom_audience(audience_id="aud_1", **kwargs))
    if "params" in kwargs:
        assert "params" in str(excinfo.value)


def test_update_custom_audience_labels_failed_pre_read_writes_nothing(monkeypatch) -> None:
    from meta_ads_mcp.errors import NotFoundError

    client = RecordingAudienceClient(read_error=NotFoundError("Meta object or edge not found: aud_1"))
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    with pytest.raises(NotFoundError):
        asyncio.run(audiences.update_custom_audience(audience_id="aud_1", audience_labels=["AT_RISK"]))
    assert [call[0] for call in client.calls] == ["get"]


def test_audience_labels_match_metas_published_list_and_sdk_enum() -> None:
    """The guide's ten labels plus the five more in the SDK 26.0.2 enum; neither lists APP_INSTALLERS or CUSTOMER_LEADS."""
    guide = {"QUALIFIED_LEADS", "DISQUALIFIED_LEADS", "APP_USERS", "TRIAL_USERS", "ENGAGED_USERS",
             "HIGH_VALUE_CUSTOMERS", "LOW_VALUE_CUSTOMERS", "AT_RISK", "DISENGAGED", "GENERAL_CUSTOMERS"}
    sdk_only = {"CART_ABANDONERS", "RECENT_PURCHASERS", "OTHER_1", "OTHER_2", "OTHER_3"}
    assert audiences.AUDIENCE_LABELS == guide | sdk_only
