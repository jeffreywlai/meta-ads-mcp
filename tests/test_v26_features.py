"""v26 SDK additions use existing generic inputs without extra tool layers."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.tools import ads, creatives, discovery, insights


V26_INSIGHTS_FIELDS = [
    "playable_average_game_length",
    "playable_game_start_rate",
    "configurable_audience_overlap_with_conv_action",
    "configurable_audience_overlap_with_conv_converters",
    "configurable_audience_overlap_with_conv_exposure_cost",
    "configurable_audience_overlap_with_conv_exposure_impressions",
    "configurable_audience_overlap_with_conv_exposure_reach",
    "configurable_placement_ptc_conversions",
    "configurable_placement_ptc_converters",
    "configurable_placement_ptc_reach",
    "msa_seller_budget",
    "shop_clicks",
]
V26_BREAKDOWNS = [
    "affiliate_click_region",
    "affiliate_link_url",
    "creative_fingerprint_details",
    "creative_media_type_breakdown",
    "msa_seller_name",
    "placement_path",
]


@pytest.mark.parametrize("tool_name", ["get_entity_insights", "get_insights"])
@pytest.mark.parametrize("breakdown", V26_BREAKDOWNS)
def test_v26_insights_fields_and_breakdowns_preserve_vendor_values(monkeypatch, tool_name, breakdown) -> None:
    # Synthetic values prove preservation, not field eligibility or numeric semantics.
    row = {field: str(index + 1) for index, field in enumerate(V26_INSIGHTS_FIELDS)}
    row[breakdown] = {"opaque_vendor_dimension": ["fixture"]}

    class Client:
        async def get_insights(self, object_id, *, fields, params):
            assert object_id == "act_123"
            assert fields == V26_INSIGHTS_FIELDS
            assert params["breakdowns"] == breakdown
            return {"data": [row]}

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: Client())
    result = asyncio.run(mcp_server.call_tool("call_tool", {
        "name": tool_name,
        "arguments": {
            "level": "account", "object_id": "act_123",
            "fields": ",".join(V26_INSIGHTS_FIELDS), "breakdowns": breakdown,
        },
    })).structured_content

    for key, value in row.items():
        assert result["items"][0][key] == value


@pytest.mark.parametrize(("tool_name", "arguments", "expected_field", "value"), [
    ("list_campaigns", {"account_id": "123"}, "bid_constraints", {"roas_average_floor": 10000}),
    ("get_campaign", {"campaign_id": "cmp_123"}, "bid_constraints", {"roas_average_floor": 10000}),
    ("list_ads", {"account_id": "123"}, "dataset_split_specs", [{"fixture_only": True}]),
    ("list_ads", {"account_id": "123"}, "creative_audience_pairing_persona", {"age_min": 25}),
    ("get_creative", {"creative_id": "crt_123"}, "media_optimization_spec", {"fixture_only": True}),
])
def test_v26_entity_fields_use_existing_read_tools(monkeypatch, tool_name, arguments, expected_field, value) -> None:
    fields = ["id", expected_field]

    class Client:
        async def list_objects(self, parent_id, edge, *, fields, params):
            assert fields == ["id", expected_field]
            return {"data": [{"id": "entity_123", expected_field: value}]}

        async def get_object(self, object_id, *, fields):
            assert fields == ["id", expected_field]
            return {"id": object_id, expected_field: value}

    client = Client()
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: client)
    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: client)
    result = asyncio.run(mcp_server.call_tool(tool_name, {**arguments, "fields": fields})).structured_content

    item = result["item"] if tool_name in {"get_campaign", "get_creative"} else result["items"][0]
    assert item[expected_field] == value


def test_v26_creative_create_preserves_media_and_voiceover_specs(monkeypatch) -> None:
    media_spec = {"fixture_only": True}
    freedom_spec = {"creative_features_spec": {"video_voiceover": {"enroll_status": "OPT_OUT"}}}

    class Client:
        async def create_edge_object(self, parent_id, edge, *, data):
            assert parent_id == "act_123"
            assert edge == "adcreatives"
            assert data["media_optimization_spec"] == media_spec
            assert data["degrees_of_freedom_spec"] == freedom_spec
            return {"id": "crt_123"}

    monkeypatch.setattr(creatives, "get_graph_api_client", lambda: Client())
    result = asyncio.run(creatives.create_ad_creative(
        account_id="123", name="Fixture", degrees_of_freedom_spec=freedom_spec,
        params={"media_optimization_spec": media_spec},
    ))
    assert result["created"]["id"] == "crt_123"


def test_v26_ad_create_preserves_dataset_and_persona_params(monkeypatch) -> None:
    params = {
        "dataset_split_specs": [{"fixture_only": True}],
        "creative_audience_pairing_persona": {"age_min": 25, "age_max": 55, "genders": [1, 2]},
    }

    class Client:
        async def create_edge_object(self, parent_id, edge, *, data):
            assert parent_id == "act_123"
            assert edge == "ads"
            assert all(data[key] == value for key, value in params.items())
            return {"id": "ad_123"}

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: Client())
    result = asyncio.run(ads.create_ad(
        account_id="123", name="Fixture", adset_id="as_123", creative_id="crt_123", params=params,
    ))
    assert result["created"]["id"] == "ad_123"


@pytest.mark.parametrize("fields", [["daily_budget"], "daily_budget"])
def test_selectable_campaign_budget_keeps_currency_dependency(monkeypatch, fields) -> None:
    class Client:
        async def get_object(self, object_id, *, fields):
            if object_id == "cmp_123":
                assert fields == ["daily_budget", "account_id"]
                return {"daily_budget": "1000", "account_id": "123"}
            assert object_id == "act_123"
            assert fields == ["currency"]
            return {"currency": "USD"}

    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: Client())
    result = asyncio.run(discovery.get_campaign(campaign_id="cmp_123", fields=fields))
    assert result["item"]["daily_budget"] == 10.0
