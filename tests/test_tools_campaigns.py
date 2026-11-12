"""Campaign CRUD tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.config import reload_settings
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


def test_create_ad_set_rejects_removed_v26_placement_before_api(monkeypatch) -> None:
    monkeypatch.setenv("META_API_VERSION", "v26.0")
    reload_settings()
    monkeypatch.setattr(
        campaigns,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )

    with pytest.raises(campaigns.ValidationError, match="instagram_positions=explore"):
        asyncio.run(
            campaigns.create_ad_set(
                account_id="123",
                campaign_id="cmp_123",
                name="Removed placement",
                billing_event="IMPRESSIONS",
                optimization_goal="OFFSITE_CONVERSIONS",
                targeting={
                    "geo_locations": {"countries": ["US"]},
                    "instagram_positions": ["explore"],
                },
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


class FakeUpdateAdsetClient:
    """Fake client for update_adset that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or [])))
        return {
            "id": object_id,
            "name": "Old set",
            "status": "PAUSED",
            "optimization_goal": "LINK_CLICKS",
            "billing_event": "IMPRESSIONS",
            "targeting": {"geo_locations": {"countries": ["US"]}},
            "end_time": "2026-12-01T00:00:00+0000",
            "frequency_control_specs": [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 2}],
        }

    async def update_object(self, object_id: str, *, data):
        self.calls.append(("update", object_id, data))
        return {"success": True}


def test_update_adset_reads_then_updates_with_before_after(monkeypatch) -> None:
    client = FakeUpdateAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    targeting = {"geo_locations": {"countries": ["US", "CA"]}}

    result = asyncio.run(campaigns.update_adset(
        adset_id="adset_1", name="New set", targeting=targeting, optimization_goal="OFFSITE_CONVERSIONS",
        params={"frequency_control_specs": [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 3}],
                "not_a_readable_field": True},
    ))

    assert [call[0] for call in client.calls] == ["get", "update"]
    read_fields = client.calls[0][2]
    assert {"name", "targeting", "optimization_goal", "frequency_control_specs"} <= set(read_fields)
    assert "not_a_readable_field" not in read_fields
    assert client.calls[1][1] == "adset_1"
    assert client.calls[1][2] == {
        "name": "New set", "targeting": targeting, "optimization_goal": "OFFSITE_CONVERSIONS",
        "frequency_control_specs": [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 3}],
        "not_a_readable_field": True,
    }
    assert result["action"] == "update_adset"
    assert result["target"] == {"adset_id": "adset_1"}
    assert result["previous"] == {
        "name": "Old set", "targeting": {"geo_locations": {"countries": ["US"]}},
        "optimization_goal": "LINK_CLICKS",
        "frequency_control_specs": [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 2}],
    }
    assert result["current"]["name"] == "New set"
    assert result["previous_not_read"] == ["not_a_readable_field"]
    assert result["validation_only"] is False


def test_update_adset_validate_only_sends_execution_option(monkeypatch) -> None:
    client = FakeUpdateAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(adset_id="adset_1", status="ACTIVE", validate_only=True))
    assert client.calls[1][2] == {"status": "ACTIVE", "execution_options": ["validate_only"]}
    assert result["validation_only"] is True
    assert result["previous"] == {"status": "PAUSED"}


@pytest.mark.parametrize(
    ("params", "tool_name"),
    [
        ({"daily_budget": 5000}, "update_adset_budget"),
        ({"lifetime_budget": 5000}, "update_adset_budget"),
        ({"bid_amount": 150}, "update_adset_bid_amount"),
        ({"bid_strategy": "COST_CAP"}, "update_adset_bid_strategy"),
        ({"bid_constraints": {"roas_average_floor": 20000}}, "update_adset_bid_strategy"),
    ],
)
def test_update_adset_rejects_budget_and_bid_fields(monkeypatch, params, tool_name) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(campaigns.ValidationError, match=tool_name):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", params=params))


def test_update_adset_requires_a_change(monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(campaigns.ValidationError, match="At least one field"):
        asyncio.run(campaigns.update_adset(adset_id="adset_1"))
    with pytest.raises(campaigns.ValidationError, match="At least one field"):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", params={}, validate_only=True))


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"adset_id": " ", "name": "x"}, "adset_id is required"),
        ({"adset_id": "adset_1", "status": "DELETED"}, "status must be"),
        ({"adset_id": "adset_1", "params": {"daily_spend_cap": 100}}, "minor units"),
        ({"adset_id": "adset_1", "name": "y", "params": {"name": "x"}}, "name"),
        ({"adset_id": "adset_1", "params": {"execution_options": ["validate_only"]}}, "execution_options"),
    ],
)
def test_update_adset_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(campaigns.ValidationError, match=message):
        asyncio.run(campaigns.update_adset(**arguments))


@pytest.mark.parametrize(
    ("times", "message"),
    [
        ({"start_time": "2026-12-20T00:00:00Z", "end_time": "2026-12-01T00:00:00Z"}, "after start_time"),
        ({"start_time": "2026-12-01T00:00:00Z", "end_time": "2026-12-01T00:00:00Z"}, "after start_time"),
        ({"start_time": "2026-12-01T12:00:00"}, "UTC offset"),
        ({"end_time": "2026-12-01"}, "UTC offset"),
        ({"end_time": "tomorrow"}, "ISO 8601"),
        ({"start_time": " "}, "ISO 8601"),
        ({"start_time": "0"}, "positive"),
    ],
)
def test_update_adset_dates_are_validated_before_network(monkeypatch, times, message) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(campaigns.ValidationError, match=message):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", **times))


@pytest.mark.parametrize(
    "times",
    [
        {"start_time": "2026-11-20T00:00:00Z", "end_time": "2026-12-20T00:00:00-0500"},
        {"start_time": "1795000000", "end_time": "1796000000"},
        {"start_time": "2026-11-20T00:00:00+00:00", "end_time": "0"},
    ],
)
def test_update_adset_valid_schedule_is_sent_as_given(monkeypatch, times) -> None:
    client = FakeUpdateAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    asyncio.run(campaigns.update_adset(adset_id="adset_1", **times))
    assert [call[0] for call in client.calls] == ["get", "update"]
    assert client.calls[1][2] == times


def test_update_adset_one_sided_start_is_checked_against_stored_end(monkeypatch) -> None:
    client = FakeUpdateAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    with pytest.raises(campaigns.ValidationError, match="stored end_time"):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", start_time="2026-12-05T00:00:00Z"))
    assert [call[0] for call in client.calls] == ["get"]
    assert set(client.calls[0][2]) == {"id", "start_time", "end_time"}


def test_update_adset_one_sided_end_is_checked_against_stored_start(monkeypatch) -> None:
    class StartedClient(FakeUpdateAdsetClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            stored = await super().get_object(object_id, fields=fields, params=params)
            return {**stored, "start_time": "2026-11-15T00:00:00+0000"}

    client = StartedClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    with pytest.raises(campaigns.ValidationError, match="stored start_time"):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", end_time="2026-11-14T23:00:00-0100"))
    assert [call[0] for call in client.calls] == ["get"]


def test_update_adset_one_sided_start_before_stored_end_is_written(monkeypatch) -> None:
    client = FakeUpdateAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(adset_id="adset_1", start_time="2026-11-20T00:00:00Z"))
    assert [call[0] for call in client.calls] == ["get", "update"]
    assert client.calls[1][2] == {"start_time": "2026-11-20T00:00:00Z"}
    assert "end_time" not in result["previous"]


def test_update_adset_missing_object_sends_no_update(monkeypatch) -> None:
    from meta_ads_mcp.errors import MetaApiError

    class GoneClient(FakeUpdateAdsetClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            raise MetaApiError(message="Object does not exist", status_code=400, code=100)

    client = GoneClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    with pytest.raises(MetaApiError):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", name="x"))
    assert client.calls == []
