"""Research tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.tools import research


class FakeResearchClient:
    """Fake research client."""

    async def search_ads_archive(
        self,
        *,
        search_terms: str,
        ad_reached_countries,
        ad_type: str = "ALL",
        limit: int = 25,
        fields=None,
    ):
        return {
            "data": [
                {
                    "id": "archive_123",
                    "search_terms": search_terms,
                    "ad_reached_countries": ad_reached_countries,
                    "ad_type": ad_type,
                    "fields": fields,
                }
            ]
        }


def test_search_ads_archive_returns_collection(monkeypatch) -> None:
    monkeypatch.setattr(research, "get_graph_api_client", lambda: FakeResearchClient())
    result = asyncio.run(
        research.search_ads_archive(
            search_terms="shirts",
            ad_reached_countries=["US"],
        )
    )
    assert result["summary"]["count"] == 1
    assert result["items"][0]["ad_reached_countries"] == ["US"]


def test_search_ads_archive_requires_countries() -> None:
    with pytest.raises(research.ValidationError):
        asyncio.run(research.search_ads_archive(search_terms="shirts", ad_reached_countries=[]))


def test_search_ads_archive_supports_custom_fields_and_ad_type(monkeypatch) -> None:
    monkeypatch.setattr(research, "get_graph_api_client", lambda: FakeResearchClient())
    result = asyncio.run(
        research.search_ads_archive(
            search_terms="shirts",
            ad_reached_countries=["US"],
            ad_type="POLITICAL_AND_ISSUE_ADS",
            fields=["id", "page_name"],
        )
    )
    assert result["items"][0]["ad_type"] == "POLITICAL_AND_ISSUE_ADS"
    assert result["items"][0]["fields"] == ["id", "page_name"]


def test_search_ads_archive_handles_empty_results(monkeypatch) -> None:
    class EmptyResearchClient(FakeResearchClient):
        async def search_ads_archive(self, **kwargs):
            return {"data": []}

    monkeypatch.setattr(research, "get_graph_api_client", lambda: EmptyResearchClient())
    result = asyncio.run(research.search_ads_archive(search_terms="shirts", ad_reached_countries=["US"]))
    assert result["items"] == []
