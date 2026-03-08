"""Recommendation tool tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import recommendations


class FakeRecommendationsClient:
    """Fake recommendations client."""

    async def get_recommendations(self, account_id: str, *, campaign_id=None):
        return {"data": [{"id": "rec_1", "message": "Increase budget", "campaign_id": campaign_id}]}


def test_get_recommendations_returns_supported_collection(monkeypatch) -> None:
    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: FakeRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123", campaign_id="cmp_123"))
    assert result["supported"] is True
    assert result["items"][0]["campaign_id"] == "cmp_123"


def test_get_recommendations_handles_unsupported_surface(monkeypatch) -> None:
    class UnsupportedRecommendationsClient(FakeRecommendationsClient):
        async def get_recommendations(self, account_id: str, *, campaign_id=None):
            raise recommendations.UnsupportedFeatureError("unsupported")

    monkeypatch.setattr(recommendations, "get_graph_api_client", lambda: UnsupportedRecommendationsClient())
    result = asyncio.run(recommendations.get_recommendations(account_id="123"))
    assert result["supported"] is False
    assert result["items"] == []
