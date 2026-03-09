"""Recommendation tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import recommendations


class FakeRecommendationsClient:
    """Fake recommendations client."""

    async def get_recommendations(self, account_id: str, *, campaign_id=None):
        return {
            "data": [
                {"id": "rec_1", "message": "Increase budget on strong ad sets", "campaign_id": campaign_id},
                {"id": "rec_2", "title": "Refresh creative image assets", "campaign_id": campaign_id},
                {"id": "rec_3", "description": "Broaden audience targeting", "campaign_id": campaign_id},
                {"id": "rec_4", "message": "Fix delivery and learning limitations", "campaign_id": campaign_id},
                {"id": "rec_5", "recommendation_type": "BID_CAP_ADJUSTMENT", "campaign_id": campaign_id},
            ]
        }


def test_get_recommendations_returns_supported_collection(monkeypatch) -> None:
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FakeRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123", campaign_id="cmp_123"))
    assert result["supported"] is True
    assert result["items"][0]["campaign_id"] == "cmp_123"
    assert result["summary"]["category_counts"]["budget"] == 1
    assert "opportunity_categories" in result["items"][0]


def test_typed_opportunity_tools_filter_by_category(monkeypatch) -> None:
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FakeRecommendationsClient())

    budget = asyncio.run(recommendations.get_budget_opportunities(account_id="123"))
    creative = asyncio.run(recommendations.get_creative_opportunities(account_id="123"))
    audience = asyncio.run(recommendations.get_audience_opportunities(account_id="123"))
    delivery = asyncio.run(recommendations.get_delivery_opportunities(account_id="123"))
    bidding = asyncio.run(recommendations.get_bidding_opportunities(account_id="123"))

    assert budget["items"][0]["id"] == "rec_1"
    assert creative["items"][0]["id"] == "rec_2"
    assert audience["items"][0]["id"] == "rec_3"
    assert delivery["items"][0]["id"] == "rec_4"
    assert bidding["items"][0]["id"] == "rec_5"
    assert budget["summary"]["filtered_from_total"] == 5


def test_get_recommendations_flattens_nested_recommendation_payloads(monkeypatch) -> None:
    class NestedRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            return {
                "data": [
                    {
                        "recommendations": [
                            {
                                "id": "rec_nested_1",
                                "type": "VALUE_OPTIMIZATION_GOAL",
                                "recommendation_content": {
                                    "body": "Duplicate ad sets to maximize value of conversions.",
                                    "lift_estimate": "7% higher ROAS",
                                },
                            },
                            {
                                "id": "rec_nested_2",
                                "type": "REELS_PC_RECOMMENDATION",
                                "recommendation_content": {
                                    "body": "Add fullscreen vertical video.",
                                },
                            },
                        ]
                    }
                ]
            }

    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: NestedRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123"))
    bidding = asyncio.run(recommendations.get_bidding_opportunities(account_id="123"))
    creative = asyncio.run(recommendations.get_creative_opportunities(account_id="123"))
    assert result["summary"]["count"] == 2
    assert result["items"][0]["message"] == "Duplicate ad sets to maximize value of conversions."
    assert bidding["items"][0]["id"] == "rec_nested_1"
    assert creative["items"][0]["id"] == "rec_nested_2"


def test_get_recommendations_handles_unsupported_surface(monkeypatch) -> None:
    class UnsupportedRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            raise recommendations.UnsupportedFeatureError("unsupported")

    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: UnsupportedRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123"))
    assert result["supported"] is False
    assert result["items"] == []


def test_typed_opportunity_tools_preserve_unsupported_surface(monkeypatch) -> None:
    class UnsupportedRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            raise recommendations.UnsupportedFeatureError("unsupported")

    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: UnsupportedRecommendationsClient())
    result = asyncio.run(recommendations.get_budget_opportunities(account_id="123"))
    assert result["supported"] is False
    assert result["category"] == "budget"
