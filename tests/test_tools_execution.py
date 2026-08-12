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
        if object_id == "act_123":
            assert fields == ["currency"]
            return {"id": object_id, "currency": self.currency}
        return {
            "id": object_id,
            "account_id": "123",
            "status": "PAUSED",
            "effective_status": "ACTIVE",
            "bid_amount": "1250" if self.currency != "JPY" else "1250",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "daily_budget": "5000" if self.currency != "JPY" else "5000",
            "lifetime_budget": "15000" if self.currency != "JPY" else "15000",
        }

    async def update_object(self, object_id: str, *, data):
        self.updated_payloads.append((object_id, data))
        return {"success": True}

    async def delete_object(self, object_id: str):
        return {"success": True, "id": object_id}


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


def test_delete_ad_and_adset_use_explicit_destructive_tools(monkeypatch) -> None:
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: FakeExecutionClient())
    ad = asyncio.run(execution.delete_ad(ad_id="ad_123"))
    adset = asyncio.run(execution.delete_adset(adset_id="adset_123"))
    assert ad["action"] == "delete_ad"
    assert ad["result"]["success"] is True
    assert adset["action"] == "delete_adset"


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


@pytest.mark.parametrize("daily_budget", [0, -1, float("nan")])
def test_update_adset_budget_rejects_invalid_value_before_client_lookup(
    monkeypatch,
    daily_budget,
) -> None:
    monkeypatch.setattr(
        execution,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(execution.ValidationError, match="daily_budget"):
        asyncio.run(
            execution.update_adset_budget(
                adset_id="adset_123",
                daily_budget=daily_budget,
            )
        )


def test_update_adset_bid_amount_returns_previous_and_current(monkeypatch) -> None:
    client = FakeExecutionClient()
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    result = asyncio.run(execution.update_adset_bid_amount(adset_id="adset_123", bid_amount=15.0))
    assert result["previous"]["bid_amount"] == 12.5
    assert result["current"]["bid_amount"] == 15.0
    assert client.updated_payloads[0] == ("adset_123", {"bid_amount": 1500})


def test_update_campaign_bid_strategy_updates_only_supported_campaign_field(monkeypatch) -> None:
    client = FakeExecutionClient()
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        execution.update_campaign_bid_strategy(
            campaign_id="cmp_123",
            bid_strategy="COST_CAP",
        )
    )
    assert result["previous"]["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"
    assert result["current"] == {"bid_strategy": "COST_CAP"}
    assert client.updated_payloads[0] == ("cmp_123", {"bid_strategy": "COST_CAP"})


def test_update_adset_bid_amount_encodes_exact_decimal(monkeypatch) -> None:
    client = FakeExecutionClient()
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    asyncio.run(
        execution.update_adset_bid_amount(adset_id="adset_123", bid_amount=19.99)
    )
    assert client.updated_payloads[0] == ("adset_123", {"bid_amount": 1999})


def test_update_adset_bid_strategy_rejects_invalid_inputs(monkeypatch) -> None:
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: FakeExecutionClient())
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.update_adset_bid_strategy(adset_id="adset_123", bid_strategy=""))
    with pytest.raises(execution.ValidationError):
        asyncio.run(execution.update_adset_bid_strategy(adset_id="adset_123", bid_strategy="COST_CAP", bid_amount=0))


def test_update_adset_bid_strategy_supports_bid_constraints(monkeypatch) -> None:
    client = FakeExecutionClient()
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        execution.update_adset_bid_strategy(
            adset_id="adset_123",
            bid_strategy="LOWEST_COST_WITH_MIN_ROAS",
            bid_constraints={"roas_average_floor": 30000},
        )
    )
    assert client.updated_payloads[0][1] == {
        "bid_strategy": "LOWEST_COST_WITH_MIN_ROAS",
        "bid_constraints": {"roas_average_floor": 30000},
    }
    assert result["current"]["bid_constraints"] == {"roas_average_floor": 30000}


@pytest.mark.parametrize(
    ("tool", "kwargs", "malformed_field"),
    [
        (
            execution.update_adset_budget,
            {"adset_id": "adset_123", "daily_budget": 20.0},
            "daily_budget",
        ),
        (
            execution.update_adset_bid_amount,
            {"adset_id": "adset_123", "bid_amount": 20.0},
            "bid_amount",
        ),
        (
            execution.update_adset_bid_strategy,
            {"adset_id": "adset_123", "bid_strategy": "COST_CAP"},
            "bid_amount",
        ),
    ],
)
def test_execution_decodes_previous_money_before_mutating(
    monkeypatch,
    tool,
    kwargs,
    malformed_field,
) -> None:
    class MalformedPreviousClient(FakeExecutionClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            payload = await super().get_object(object_id, fields=fields, params=params)
            if object_id != "act_123":
                payload[malformed_field] = "not-a-number"
            return payload

    client = MalformedPreviousClient()
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    with pytest.raises(execution.ValidationError, match=malformed_field):
        asyncio.run(tool(**kwargs))
    assert client.updated_payloads == []
