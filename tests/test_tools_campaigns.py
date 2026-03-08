"""Campaign CRUD tool tests."""

from __future__ import annotations

import asyncio
import pytest

from meta_ads_mcp.tools import campaigns


class FakeCampaignClient:
    """Fake campaign client."""

    def __init__(self) -> None:
        self.created_payload = None
        self.updated_payload = None

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.created_payload = {"parent_id": parent_id, "edge": edge, "data": data}
        return {"id": "cmp_123"}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        return {
            "id": object_id,
            "name": "Old name",
            "status": "PAUSED",
            "objective": "OUTCOME_SALES",
            "daily_budget": "5000",
            "lifetime_budget": None,
            "currency": "USD",
        }

    async def update_object(self, object_id: str, *, data):
        self.updated_payload = {"object_id": object_id, "data": data}
        return {"success": True}

    async def delete_object(self, object_id: str):
        return {"success": True, "id": object_id}


class FakeZeroDecimalCampaignClient(FakeCampaignClient):
    """Fake campaign client for zero-decimal currencies."""

    async def get_object(self, object_id: str, *, fields=None, params=None):
        payload = await super().get_object(object_id, fields=fields, params=params)
        payload["currency"] = "JPY"
        return payload


def test_create_campaign_encodes_budget(monkeypatch) -> None:
    client = FakeCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        campaigns.create_campaign(
            account_id="123",
            name="New Campaign",
            objective="OUTCOME_SALES",
            daily_budget=50.0,
        )
    )
    assert result["created"]["id"] == "cmp_123"
    assert client.created_payload["parent_id"] == "act_123"
    assert client.created_payload["data"]["daily_budget"] == 5000


def test_update_campaign_returns_previous_budget(monkeypatch) -> None:
    client = FakeCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        campaigns.update_campaign(campaign_id="cmp_123", name="Updated", daily_budget=75.0)
    )
    assert result["previous"]["daily_budget"] == 50.0
    assert client.updated_payload["data"]["daily_budget"] == 7500
    assert result["current"]["daily_budget"] == 75.0
    assert result["current"]["name"] == "Updated"


def test_update_campaign_rejects_noop_update(monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: FakeCampaignClient())
    with pytest.raises(campaigns.ValidationError):
        asyncio.run(campaigns.update_campaign(campaign_id="cmp_123"))


def test_update_campaign_rejects_both_budgets(monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: FakeCampaignClient())
    with pytest.raises(campaigns.ValidationError):
        asyncio.run(
            campaigns.update_campaign(
                campaign_id="cmp_123",
                daily_budget=10.0,
                lifetime_budget=20.0,
            )
        )


def test_delete_campaign_returns_success(monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: FakeCampaignClient())
    result = asyncio.run(campaigns.delete_campaign(campaign_id="cmp_123"))
    assert result["result"]["success"] is True


def test_create_ad_set_rejects_both_budgets(monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: FakeCampaignClient())
    with pytest.raises(campaigns.ValidationError):
        asyncio.run(
            campaigns.create_ad_set(
                account_id="123",
                campaign_id="cmp_123",
                name="Bad Ad Set",
                billing_event="IMPRESSIONS",
                optimization_goal="OFFSITE_CONVERSIONS",
                targeting={"geo_locations": {"countries": ["US"]}},
                daily_budget=10.0,
                lifetime_budget=20.0,
            )
        )


def test_update_campaign_encodes_zero_decimal_budget_without_cents(monkeypatch) -> None:
    client = FakeZeroDecimalCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        campaigns.update_campaign(campaign_id="cmp_123", daily_budget=7500.0)
    )
    assert result["previous"]["daily_budget"] == 5000.0
    assert result["current"]["daily_budget"] == 7500.0
    assert client.updated_payload["data"]["daily_budget"] == 7500
