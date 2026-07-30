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
    assert spec["location_spec"]["countries"] == ["US"]
    assert spec["ratio"] == 0.02


def test_list_audiences_returns_collection(monkeypatch) -> None:
    client = FakeAudienceClient()
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: client)
    result = asyncio.run(audiences.list_audiences(account_id="123"))
    assert result["summary"]["count"] == 1
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


def test_create_lookalike_audience_supports_countries(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(
        audiences.create_lookalike_audience(
            account_id="123",
            name="LAL",
            origin_audience_id="aud_origin",
            countries=["US", "CA"],
            starting_ratio=0.01,
        )
    )
    spec = result["created"]["payload"]["lookalike_spec"]
    assert spec["location_spec"]["countries"] == ["US", "CA"]
    assert spec["starting_ratio"] == 0.01


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


def test_delete_audience_returns_success(monkeypatch) -> None:
    monkeypatch.setattr(audiences, "get_graph_api_client", lambda: FakeAudienceClient())
    result = asyncio.run(audiences.delete_audience(audience_id="aud_1"))
    assert result["result"]["success"] is True
