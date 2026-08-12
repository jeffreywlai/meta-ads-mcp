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
        if object_id == "act_123":
            assert fields == ["currency"]
            return {"id": object_id, "currency": "USD"}
        return {
            "id": object_id,
            "account_id": "123",
            "name": "Old name",
            "status": "PAUSED",
            "objective": "OUTCOME_SALES",
            "daily_budget": "5000",
            "lifetime_budget": None,
        }

    async def update_object(self, object_id: str, *, data):
        self.updated_payload = {"object_id": object_id, "data": data}
        return {"success": True}

    async def delete_object(self, object_id: str):
        return {"success": True, "id": object_id}


class FakeZeroDecimalCampaignClient(FakeCampaignClient):
    """Fake campaign client for zero-decimal currencies."""

    async def get_object(self, object_id: str, *, fields=None, params=None):
        if object_id == "act_123":
            return {"id": object_id, "currency": "JPY"}
        return await super().get_object(object_id, fields=fields, params=params)


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


def test_create_campaign_encodes_exact_decimal_budget(monkeypatch) -> None:
    client = FakeCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    asyncio.run(
        campaigns.create_campaign(
            account_id="123",
            name="New Campaign",
            objective="OUTCOME_SALES",
            daily_budget=19.99,
        )
    )
    assert client.created_payload["data"]["daily_budget"] == 1999


def test_create_campaign_encodes_zero_decimal_budget(monkeypatch) -> None:
    client = FakeZeroDecimalCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    asyncio.run(
        campaigns.create_campaign(
            account_id="123",
            name="JPY Campaign",
            objective="OUTCOME_SALES",
            daily_budget=5000.0,
        )
    )
    assert client.created_payload["data"]["daily_budget"] == 5000


@pytest.mark.parametrize("daily_budget", [0, -1, float("nan")])
def test_create_campaign_rejects_invalid_budget_before_client_lookup(
    monkeypatch,
    daily_budget,
) -> None:
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(campaigns.ValidationError, match="daily_budget"):
        asyncio.run(
            campaigns.create_campaign(
                account_id="123",
                name="Invalid Campaign",
                objective="OUTCOME_SALES",
                daily_budget=daily_budget,
            )
        )


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
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
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


def test_update_campaign_rejects_nonpositive_budget_before_client_lookup(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(campaigns.ValidationError, match="daily_budget"):
        asyncio.run(
            campaigns.update_campaign(campaign_id="cmp_123", daily_budget=-1)
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


def test_create_ad_set_supports_typed_bidding_and_validate_only(monkeypatch) -> None:
    client = FakeCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        campaigns.create_ad_set(
            account_id="123",
            campaign_id="cmp_123",
            name="Target ROAS",
            billing_event="IMPRESSIONS",
            optimization_goal="VALUE",
            targeting={"geo_locations": {"countries": ["US"]}},
            bid_strategy="LOWEST_COST_WITH_MIN_ROAS",
            bid_constraints={"roas_average_floor": 30000},
            validate_only=True,
        )
    )
    assert client.created_payload["data"]["bid_strategy"] == "LOWEST_COST_WITH_MIN_ROAS"
    assert client.created_payload["data"]["bid_constraints"] == {"roas_average_floor": 30000}
    assert client.created_payload["data"]["execution_options"] == ["validate_only"]
    assert result["validation_only"] is True
    assert result["created"] is None
    assert result["validation"] == {"id": "cmp_123"}


def test_create_ad_set_encodes_zero_decimal_budget_and_bid(monkeypatch) -> None:
    client = FakeZeroDecimalCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    asyncio.run(
        campaigns.create_ad_set(
            account_id="123",
            campaign_id="cmp_123",
            name="JPY Ad Set",
            billing_event="IMPRESSIONS",
            optimization_goal="OFFSITE_CONVERSIONS",
            targeting={"geo_locations": {"countries": ["JP"]}},
            daily_budget=5000.0,
            bid_amount=1250.0,
        )
    )
    assert client.created_payload["data"]["daily_budget"] == 5000
    assert client.created_payload["data"]["bid_amount"] == 1250


@pytest.mark.parametrize(
    "kwargs",
    [{"daily_budget": -1}, {"bid_amount": 0}],
)
def test_create_ad_set_rejects_invalid_money_before_client_lookup(
    monkeypatch,
    kwargs,
) -> None:
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(campaigns.ValidationError):
        asyncio.run(
            campaigns.create_ad_set(
                account_id="123",
                campaign_id="cmp_123",
                name="Invalid Ad Set",
                billing_event="IMPRESSIONS",
                optimization_goal="OFFSITE_CONVERSIONS",
                targeting={"geo_locations": {"countries": ["US"]}},
                **kwargs,
            )
        )


def test_create_ad_set_rejects_params_overriding_typed_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(campaigns.ValidationError, match="cannot override typed fields"):
        asyncio.run(
            campaigns.create_ad_set(
                account_id="123",
                campaign_id="cmp_123",
                name="Safe name",
                billing_event="IMPRESSIONS",
                optimization_goal="VALUE",
                targeting={"geo_locations": {"countries": ["US"]}},
                params={"name": "Hidden override"},
            )
        )


def test_campaign_params_cannot_supply_omitted_typed_or_control_fields(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )

    with pytest.raises(campaigns.ValidationError, match="daily_budget"):
        asyncio.run(
            campaigns.create_campaign(
                account_id="123",
                name="Campaign",
                objective="OUTCOME_SALES",
                params={"daily_budget": 25},
            )
        )
    with pytest.raises(campaigns.ValidationError, match="execution_options"):
        asyncio.run(
            campaigns.create_campaign(
                account_id="123",
                name="Campaign",
                objective="OUTCOME_SALES",
                params={"execution_options": ["validate_only"]},
            )
        )
    with pytest.raises(campaigns.ValidationError, match="daily_budget"):
        asyncio.run(
            campaigns.update_campaign(
                campaign_id="cmp_123",
                params={"daily_budget": 25},
            )
        )
    with pytest.raises(campaigns.ValidationError, match="bid_amount"):
        asyncio.run(
            campaigns.create_ad_set(
                account_id="123",
                campaign_id="cmp_123",
                name="Ad set",
                billing_event="IMPRESSIONS",
                optimization_goal="VALUE",
                targeting={"geo_locations": {"countries": ["US"]}},
                params={"bid_amount": 25},
            )
        )


def test_create_ad_set_requires_strategy_for_bid_constraints(monkeypatch) -> None:
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(campaigns.ValidationError, match="bid_strategy is required"):
        asyncio.run(
            campaigns.create_ad_set(
                account_id="123",
                campaign_id="cmp_123",
                name="Missing strategy",
                billing_event="IMPRESSIONS",
                optimization_goal="VALUE",
                targeting={"geo_locations": {"countries": ["US"]}},
                bid_constraints={"roas_average_floor": 30000},
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


def test_update_campaign_decodes_previous_money_before_mutating(monkeypatch) -> None:
    class MalformedPreviousClient(FakeCampaignClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            payload = await super().get_object(object_id, fields=fields, params=params)
            if object_id != "act_123":
                payload["daily_budget"] = "not-a-number"
            return payload

    client = MalformedPreviousClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    with pytest.raises(campaigns.ValidationError, match="daily_budget"):
        asyncio.run(
            campaigns.update_campaign(campaign_id="cmp_123", daily_budget=75.0)
        )
    assert client.updated_payload is None
