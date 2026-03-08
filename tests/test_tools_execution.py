"""Execution tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.tools import execution


class FakeExecutionClient:
    """Fake execution client."""

    def __init__(self, *, currency: str = "USD") -> None:
        self.currency = currency
        self.updated_payloads: list[tuple[str, dict[str, object]]] = []

    async def get_object(self, object_id: str, *, fields=None, params=None):
        return {
            "id": object_id,
            "status": "PAUSED",
            "effective_status": "ACTIVE",
            "daily_budget": "5000" if self.currency != "JPY" else "5000",
            "lifetime_budget": "15000" if self.currency != "JPY" else "15000",
            "currency": self.currency,
        }

    async def update_object(self, object_id: str, *, data):
        self.updated_payloads.append((object_id, data))
        return {"success": True}


def test_set_campaign_status_returns_previous_and_current(monkeypatch) -> None:
    client = FakeExecutionClient()
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    result = asyncio.run(execution.set_campaign_status(campaign_id="cmp_123", status="ACTIVE"))
    assert result["previous"]["status"] == "PAUSED"
    assert result["current"]["status"] == "ACTIVE"
    assert client.updated_payloads[0] == ("cmp_123", {"status": "ACTIVE"})


def test_set_adset_status_uses_effective_status_when_status_missing(monkeypatch) -> None:
    class EffectiveStatusClient(FakeExecutionClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            payload = await super().get_object(object_id, fields=fields, params=params)
            payload["status"] = None
            return payload

    monkeypatch.setattr(execution, "get_graph_api_client", lambda: EffectiveStatusClient())
    result = asyncio.run(execution.set_adset_status(adset_id="adset_123", status="PAUSED"))
    assert result["previous"]["status"] == "ACTIVE"


def test_set_ad_status_rejects_invalid_status(monkeypatch) -> None:
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: FakeExecutionClient())
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.set_ad_status(ad_id="ad_123", status="DELETED"))


def test_update_campaign_budget_normalizes_previous_zero_decimal_currency(monkeypatch) -> None:
    client = FakeExecutionClient(currency="JPY")
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    result = asyncio.run(execution.update_campaign_budget(campaign_id="cmp_123", daily_budget=7500.0))
    assert result["previous"]["daily_budget"] == 5000.0
    assert result["current"]["daily_budget"] == 7500.0
    assert client.updated_payloads[0][1]["daily_budget"] == 7500


def test_update_adset_budget_requires_exactly_one_budget(monkeypatch) -> None:
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: FakeExecutionClient())
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.update_adset_budget(adset_id="adset_123"))
    with pytest.raises(execution.ValidationError):
        asyncio.run(
            execution.update_adset_budget(adset_id="adset_123", daily_budget=10.0, lifetime_budget=20.0)
        )
