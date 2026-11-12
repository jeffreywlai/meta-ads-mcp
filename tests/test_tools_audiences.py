"""Audience tool tests."""

from __future__ import annotations

import asyncio
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
