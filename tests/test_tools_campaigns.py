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


class RecordingCampaignClient(FakeCampaignClient):
    """Fake campaign client that records every call in order."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.calls.append(("create", parent_id, edge, dict(data)))
        return await super().create_edge_object(parent_id, edge, data=data, files=files)

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or [])))
        return await super().get_object(object_id, fields=fields, params=params)

    async def update_object(self, object_id: str, *, data):
        self.calls.append(("update", object_id, dict(data)))
        return await super().update_object(object_id, data=data)


def test_create_campaign_params_only_call_is_unchanged(monkeypatch) -> None:
    """A call that puts the new fields in params, as callers do today, sends exactly what it sent before."""
    client = RecordingCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    params = {
        "spend_cap": 25000,
        "start_time": "2026-12-01T00:00:00-0500",
        "stop_time": "2026-12-31T00:00:00-0500",
        "promoted_object": {"pixel_id": "999", "custom_event_type": "PURCHASE"},
        "special_ad_category_country": ["US"],
        "adlabels": [{"name": "Q4"}],
    }
    result = asyncio.run(campaigns.create_campaign(
        account_id="123", name="Old style", objective="OUTCOME_SALES",
        daily_budget=50.0, params=dict(params),
    ))
    assert client.calls == [
        ("get", "act_123", ["currency"]),
        ("create", "act_123", "campaigns", {
            "name": "Old style",
            "objective": "OUTCOME_SALES",
            "status": "PAUSED",
            "special_ad_categories": [],
            "daily_budget": 5000,
            **params,
        }),
    ]
    assert list(client.calls[1][3]) == [
        "name", "objective", "status", "special_ad_categories", "daily_budget", *params,
    ]
    assert result == {
        "ok": True, "action": "create_campaign", "target": {"account_id": "act_123"},
        "validation_only": False, "created": {"id": "cmp_123"},
    }


def test_create_ad_set_params_only_call_is_unchanged(monkeypatch) -> None:
    """A call that puts the new fields in params, as callers do today, sends exactly what it sent before."""
    client = RecordingCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    params = {
        "frequency_control_specs": [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 2}],
        "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 7}],
        "adset_schedule": [{"start_minute": 540, "end_minute": 1020, "days": [1, 2, 3]}],
        "pacing_type": ["day_parting"],
        "daily_spend_cap": 9000,
        "lifetime_spend_cap": 90000,
        "daily_min_spend_target": 1000,
        "lifetime_min_spend_target": 10000,
        "dsa_beneficiary": "Acme GmbH",
        "dsa_payor": "Acme GmbH",
        "adlabels": [{"name": "Q4"}],
    }
    targeting = {"geo_locations": {"countries": ["DE"]}}
    result = asyncio.run(campaigns.create_ad_set(
        account_id="act_123", campaign_id="cmp_123", name="Old style set",
        billing_event="IMPRESSIONS", optimization_goal="REACH", targeting=targeting,
        lifetime_budget=500.0, end_time="2026-12-31T00:00:00-0500", params=dict(params),
    ))
    assert client.calls == [
        ("get", "act_123", ["currency"]),
        ("create", "act_123", "adsets", {
            "campaign_id": "cmp_123",
            "name": "Old style set",
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "REACH",
            "targeting": targeting,
            "status": "PAUSED",
            "end_time": "2026-12-31T00:00:00-0500",
            "lifetime_budget": 50000,
            **params,
        }),
    ]
    assert list(client.calls[1][3]) == [
        "campaign_id", "name", "billing_event", "optimization_goal", "targeting", "status",
        "end_time", "lifetime_budget", *params,
    ]
    assert result["created"] == {"id": "cmp_123"}
    assert result["target"] == {"account_id": "act_123", "campaign_id": "cmp_123"}


def _no_client():
    return pytest.fail("No client should be created")


CAMPAIGN_BASE = {"account_id": "123", "name": "C", "objective": "OUTCOME_SALES"}
ADSET_BASE = {
    "account_id": "123", "campaign_id": "cmp_123", "name": "S", "billing_event": "IMPRESSIONS",
    "optimization_goal": "REACH", "targeting": {"geo_locations": {"countries": ["DE"]}},
}


@pytest.mark.parametrize(
    ("named", "params_value"),
    [
        ({"spend_cap": 250.0}, 25000),
        ({"start_time": "2026-12-01T00:00:00Z"}, "2026-12-01T00:00:00Z"),
        ({"stop_time": "2026-12-31T00:00:00Z"}, "2026-12-31T00:00:00Z"),
        ({"promoted_object": {"pixel_id": "1"}}, {"pixel_id": "2"}),
        ({"special_ad_category_country": ["US"]}, ["US"]),
        ({"adlabels": [{"name": "Q4"}]}, [{"name": "Q4"}]),
    ],
)
def test_create_campaign_named_parameter_and_same_params_key_together_raise(monkeypatch, named, params_value) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", _no_client)
    (key, _value), = named.items()
    with pytest.raises(campaigns.ValidationError, match=rf"{key} was given both as a named parameter and inside params"):
        asyncio.run(campaigns.create_campaign(**CAMPAIGN_BASE, **named, params={key: params_value}))


@pytest.mark.parametrize(
    ("named", "params_value"),
    [
        ({"frequency_control_specs": [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 2}]}, []),
        ({"attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 7}]}, []),
        ({"adset_schedule": [{"start_minute": 0, "end_minute": 60, "days": [0]}]}, []),
        ({"daily_spend_cap": 90.0}, 9000),
        ({"lifetime_spend_cap": 900.0}, 90000),
        ({"daily_min_spend_target": 10.0}, 1000),
        ({"lifetime_min_spend_target": 100.0}, 10000),
        ({"dsa_beneficiary": "Acme"}, "Acme"),
        ({"dsa_payor": "Acme"}, "Acme"),
        ({"adlabels": [{"id": "123"}]}, [{"id": "123"}]),
    ],
)
def test_create_ad_set_named_parameter_and_same_params_key_together_raise(monkeypatch, named, params_value) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", _no_client)
    (key, _value), = named.items()
    with pytest.raises(campaigns.ValidationError, match=rf"{key} was given both as a named parameter and inside params"):
        asyncio.run(campaigns.create_ad_set(**ADSET_BASE, **named, params={key: params_value}))


def test_create_campaign_named_parameters_are_sent(monkeypatch) -> None:
    client = RecordingCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.create_campaign(
        **CAMPAIGN_BASE,
        special_ad_categories=["HOUSING"],
        spend_cap=1000.25,
        start_time="2026-12-01T00:00:00-05:00",
        stop_time="2026-12-31T00:00:00-05:00",
        promoted_object={"pixel_id": "999", "custom_event_type": "PURCHASE"},
        special_ad_category_country=["us", "CA"],
        adlabels=[{"name": "Q4"}, {"id": "456"}],
        validate_only=True,
        params={"is_skadnetwork_attribution": False},
    ))
    assert client.calls == [
        ("get", "act_123", ["currency"]),
        ("create", "act_123", "campaigns", {
            "name": "C",
            "objective": "OUTCOME_SALES",
            "status": "PAUSED",
            "special_ad_categories": ["HOUSING"],
            "spend_cap": 100025,
            "start_time": "2026-12-01T00:00:00-05:00",
            "stop_time": "2026-12-31T00:00:00-05:00",
            "promoted_object": {"pixel_id": "999", "custom_event_type": "PURCHASE"},
            "special_ad_category_country": ["US", "CA"],
            "adlabels": [{"name": "Q4"}, {"id": "456"}],
            "execution_options": ["validate_only"],
            "is_skadnetwork_attribution": False,
        }),
    ]
    assert result["validation_only"] is True


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"spend_cap": 0}, "spend_cap must be greater than 0"),
        ({"start_time": "tomorrow"}, "start_time"),
        ({"stop_time": "2026-12-31T00:00:00"}, "UTC offset"),
        ({"start_time": "2026-12-31T00:00:00Z", "stop_time": "2026-12-01T00:00:00Z"}, "stop_time must be after start_time"),
        ({"special_ad_category_country": ["USA"]}, "two-letter"),
        ({"special_ad_category_country": []}, "special_ad_category_country must not be empty"),
        ({"adlabels": [{"color": "red"}]}, "adlabels"),
        ({"adlabels": []}, "adlabels must not be empty"),
        ({"promoted_object": {}}, "promoted_object must not be empty"),
    ],
)
def test_create_campaign_named_parameter_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", _no_client)
    with pytest.raises(campaigns.ValidationError, match=message):
        asyncio.run(campaigns.create_campaign(**CAMPAIGN_BASE, **arguments))


def test_create_ad_set_named_parameters_are_sent(monkeypatch) -> None:
    client = RecordingCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    frequency = [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 2}]
    attribution = [{"event_type": "CLICK_THROUGH", "window_days": 7}]
    schedule = [{"start_minute": 540, "end_minute": 1020, "days": [1, 2, 3]}]
    asyncio.run(campaigns.create_ad_set(
        **ADSET_BASE,
        frequency_control_specs=frequency,
        attribution_spec=attribution,
        adset_schedule=schedule,
        daily_spend_cap=90.5,
        lifetime_spend_cap=900,
        daily_min_spend_target=10,
        lifetime_min_spend_target=100.01,
        dsa_beneficiary="Acme GmbH",
        dsa_payor="Acme Holding",
        adlabels=[{"name": "Q4"}],
        params={"pacing_type": ["day_parting"]},
    ))
    assert client.calls == [
        ("get", "act_123", ["currency"]),
        ("create", "act_123", "adsets", {
            "campaign_id": "cmp_123",
            "name": "S",
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "REACH",
            "targeting": {"geo_locations": {"countries": ["DE"]}},
            "status": "PAUSED",
            "frequency_control_specs": frequency,
            "attribution_spec": attribution,
            "adset_schedule": schedule,
            "daily_spend_cap": 9050,
            "lifetime_spend_cap": 90000,
            "daily_min_spend_target": 1000,
            "lifetime_min_spend_target": 10001,
            "dsa_beneficiary": "Acme GmbH",
            "dsa_payor": "Acme Holding",
            "adlabels": [{"name": "Q4"}],
            "pacing_type": ["day_parting"],
        }),
    ]


def test_create_ad_set_spend_cap_zero_decimal_currency(monkeypatch) -> None:
    client = FakeZeroDecimalCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    asyncio.run(campaigns.create_ad_set(**ADSET_BASE, daily_spend_cap=7000))
    assert client.created_payload["data"]["daily_spend_cap"] == 7000


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"daily_spend_cap": -1}, "daily_spend_cap must be greater than 0"),
        ({"lifetime_min_spend_target": 0}, "lifetime_min_spend_target must be greater than 0"),
        ({"frequency_control_specs": []}, "frequency_control_specs must not be empty"),
        ({"attribution_spec": ["CLICK"]}, "attribution_spec"),
        ({"adset_schedule": [1]}, "adset_schedule"),
        ({"dsa_beneficiary": " "}, "dsa_beneficiary"),
        ({"dsa_payor": ""}, "dsa_payor"),
        ({"adlabels": ["Q4"]}, "adlabels"),
    ],
)
def test_create_ad_set_named_parameter_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", _no_client)
    with pytest.raises(campaigns.ValidationError, match=message):
        asyncio.run(campaigns.create_ad_set(**ADSET_BASE, **arguments))


def test_create_ad_set_rejects_too_many_decimals_for_spend_cap_before_sending(monkeypatch) -> None:
    client = FakeZeroDecimalCampaignClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    with pytest.raises(campaigns.ValidationError, match="decimal places"):
        asyncio.run(campaigns.create_ad_set(**ADSET_BASE, daily_spend_cap=10.5))
    assert client.created_payload is None


class MoneyAdsetClient:
    """Fake client for update_adset money fields: an ad set on an account with a currency."""

    def __init__(self, *, currency: str = "USD", stored: dict | None = None, currency_error: Exception | None = None) -> None:
        self.currency = currency
        self.currency_error = currency_error
        self.stored = {
            "account_id": "123",
            "status": "ACTIVE",
            "daily_spend_cap": "5000",
            "lifetime_spend_cap": "200000",
            "daily_min_spend_target": "1000",
        } if stored is None else stored
        self.calls: list[tuple] = []

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or [])))
        if object_id == "act_123":
            if self.currency_error is not None:
                raise self.currency_error
            return {"id": object_id, "currency": self.currency}
        return {"id": object_id, **{key: value for key, value in self.stored.items() if key in (fields or [])}}

    async def update_object(self, object_id, *, data):
        self.calls.append(("update", object_id, dict(data)))
        return {"success": True}


def test_update_adset_spend_caps_use_major_units(monkeypatch) -> None:
    client = MoneyAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(
        adset_id="adset_1",
        daily_spend_cap=90.5,
        lifetime_spend_cap=2500,
        daily_min_spend_target=12.25,
        lifetime_min_spend_target=1000,
    ))
    assert client.calls == [
        ("get", "adset_1", [
            "id", "daily_spend_cap", "lifetime_spend_cap", "daily_min_spend_target",
            "lifetime_min_spend_target", "account_id",
        ]),
        ("get", "act_123", ["currency"]),
        ("update", "adset_1", {
            "daily_spend_cap": 9050,
            "lifetime_spend_cap": 250000,
            "daily_min_spend_target": 1225,
            "lifetime_min_spend_target": 100000,
        }),
    ]
    assert result["current"] == {
        "daily_spend_cap": 90.5,
        "lifetime_spend_cap": 2500,
        "daily_min_spend_target": 12.25,
        "lifetime_min_spend_target": 1000,
    }
    assert result["previous"] == {
        "daily_spend_cap": 50.0,
        "lifetime_spend_cap": 2000.0,
        "daily_min_spend_target": 10.0,
    }
    assert result["previous_unavailable"] == {
        "lifetime_min_spend_target": "Meta returned no value for it; it may not have been set.",
    }
    assert result["money_units"] == "major units of the ad account currency (USD)"


def test_update_adset_spend_caps_zero_decimal_currency(monkeypatch) -> None:
    client = MoneyAdsetClient(currency="JPY", stored={"account_id": "act_123", "daily_spend_cap": "5000"})
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(adset_id="adset_1", daily_spend_cap=7000))
    assert client.calls[-1] == ("update", "adset_1", {"daily_spend_cap": 7000})
    assert result["previous"] == {"daily_spend_cap": 5000.0}


def test_update_adset_spend_cap_with_other_fields_and_validate_only(monkeypatch) -> None:
    client = MoneyAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(
        adset_id="adset_1", status="PAUSED", daily_spend_cap=75, validate_only=True,
    ))
    assert client.calls[-1] == ("update", "adset_1", {
        "status": "PAUSED", "daily_spend_cap": 7500, "execution_options": ["validate_only"],
    })
    assert result["previous"] == {"status": "ACTIVE", "daily_spend_cap": 50.0}
    assert result["validation_only"] is True
    assert result["applied"] is False


def test_update_adset_without_money_does_not_read_currency(monkeypatch) -> None:
    client = MoneyAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(adset_id="adset_1", status="PAUSED"))
    assert client.calls == [
        ("get", "adset_1", ["id", "status"]),
        ("update", "adset_1", {"status": "PAUSED"}),
    ]
    assert "money_units" not in result


@pytest.mark.parametrize(
    "client",
    [
        MoneyAdsetClient(stored={"daily_spend_cap": "5000"}),
        MoneyAdsetClient(currency_error=RuntimeError("currency read failed")),
        MoneyAdsetClient(currency="XXX"),
        MoneyAdsetClient(stored={"account_id": "123", "daily_spend_cap": "not-a-number"}),
    ],
)
def test_update_adset_money_writes_nothing_when_conversion_cannot_be_trusted(monkeypatch, client) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    with pytest.raises(Exception):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", daily_spend_cap=20))
    assert not [call for call in client.calls if call[0] == "update"]


def test_update_adset_rejects_too_many_decimals_before_writing(monkeypatch) -> None:
    client = MoneyAdsetClient(currency="JPY")
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    with pytest.raises(campaigns.ValidationError, match="decimal places"):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", daily_spend_cap=10.5))
    assert not [call for call in client.calls if call[0] == "update"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"daily_spend_cap": 0}, "daily_spend_cap must be greater than 0"),
        ({"lifetime_min_spend_target": -5}, "lifetime_min_spend_target must be greater than 0"),
        ({"daily_spend_cap": float("nan")}, "daily_spend_cap"),
        ({"params": {"daily_spend_cap": 9000}}, "named daily_spend_cap parameter"),
        ({"params": {"lifetime_spend_cap": 9000}}, "named lifetime_spend_cap parameter"),
        ({"params": {"daily_min_spend_target": 9000}}, "named daily_min_spend_target parameter"),
        ({"params": {"lifetime_min_spend_target": 9000}}, "named lifetime_min_spend_target parameter"),
        ({"daily_spend_cap": 90, "params": {"daily_spend_cap": 9000}}, "daily_spend_cap"),
        ({"params": {"bid_info": {"ACTIONS": 150}}}, "update_adset_bid_amount"),
        ({"params": {"budget_schedule_specs": [{"budget_value": 100}]}}, "budget_schedule_specs"),
        ({"status": "DELETED"}, "delete_adset"),
        ({"status": "archived"}, "status must be"),
    ],
)
def test_update_adset_money_and_status_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(campaigns.ValidationError, match=message):
        asyncio.run(campaigns.update_adset(adset_id="adset_1", **arguments))


def test_update_adset_accepts_archived(monkeypatch) -> None:
    client = FakeUpdateAdsetClient()
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(adset_id="adset_1", status="ARCHIVED"))
    assert client.calls[1] == ("update", "adset_1", {"status": "ARCHIVED"})
    assert result["previous"] == {"status": "PAUSED"}
    assert result["current"] == {"status": "ARCHIVED"}
    assert "cannot be restored to active from this server" in campaigns.update_adset.__doc__


def test_update_adset_names_a_params_field_meta_did_not_return(monkeypatch) -> None:
    """A changed field Meta omits from the pre-read is named, not reported as a null prior value."""
    client = MoneyAdsetClient(stored={"name": "Old"})
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.update_adset(
        adset_id="adset_1", name="New",
        params={"attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 1}], "not_a_readable_field": 1},
    ))
    assert client.calls[0] == ("get", "adset_1", ["id", "name", "attribution_spec"])
    assert result["previous"] == {"name": "Old"}
    assert "attribution_spec" not in result["previous"]
    assert result["previous_unavailable"] == {
        "attribution_spec": "Meta returned no value for it; it may not have been set.",
        "not_a_readable_field": "Not a field this server reads back from an ad set.",
    }
    assert result["previous_not_read"] == ["not_a_readable_field"]
