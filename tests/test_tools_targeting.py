"""Targeting tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.tools import targeting


class FakeTargetingClient:
    """Fake targeting client."""

    async def get_interest_suggestions(self, *, interest_list, limit: int = 25):
        return {"data": [{"id": "int_suggested", "name": interest_list[0]}]}

    async def validate_interests(self, *, interest_list=None, interest_ids=None):
        return {
            "data": [
                {
                    "valid": True,
                    "interest_list": interest_list,
                    "interest_ids": interest_ids,
                }
            ]
        }

    async def search_targeting_categories(
        self,
        *,
        account_id: str,
        category_class: str,
        query: str | None = None,
        limit: int = 25,
    ):
        return {
            "data": [
                {
                    "id": "cat_123",
                    "name": query or category_class,
                    "class": category_class,
                    "account_id": account_id,
                }
            ]
        }


def test_get_interest_suggestions_returns_collection(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(targeting.get_interest_suggestions(interest_list=["running"]))
    assert result["summary"]["count"] == 1
    assert result["items"][0]["id"] == "int_suggested"


def test_validate_interests_supports_interest_ids(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(targeting.validate_interests(interest_ids=["6003139266461"]))
    assert result["items"][0]["interest_ids"] == ["6003139266461"]


def test_validate_interests_supports_names_and_ids_together(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(
        targeting.validate_interests(interest_list=["running"], interest_ids=["6003139266461"])
    )
    assert result["items"][0]["interest_list"] == ["running"]
    assert result["items"][0]["interest_ids"] == ["6003139266461"]


def test_search_behaviors_uses_behavior_class(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(targeting.search_behaviors(query="travel", account_id="123"))
    assert result["items"][0]["class"] == "behaviors"
    assert result["items"][0]["account_id"] == "act_123"


def test_get_targeting_categories_uses_given_class(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(targeting.get_targeting_categories(category_class="life_events", query="new", account_id="123"))
    assert result["items"][0]["class"] == "life_events"
    assert result["items"][0]["account_id"] == "act_123"


def test_search_demographics_supports_unusual_category_class(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(targeting.search_demographics(demographic_class="work", query="manager", account_id="123"))
    assert result["items"][0]["class"] == "work"
    assert result["items"][0]["account_id"] == "act_123"


def test_validate_interests_can_return_empty_results(monkeypatch) -> None:
    class EmptyTargetingClient(FakeTargetingClient):
        async def validate_interests(self, *, interest_list=None, interest_ids=None):
            return {"data": []}

    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: EmptyTargetingClient())
    result = asyncio.run(targeting.validate_interests(interest_list=["running"]))
    assert result["items"] == []


def test_get_interest_suggestions_rejects_empty_interest_list() -> None:
    with pytest.raises(targeting.ValidationError):
        asyncio.run(targeting.get_interest_suggestions(interest_list=[]))


def test_get_targeting_categories_requires_class() -> None:
    with pytest.raises(targeting.ValidationError):
        asyncio.run(targeting.get_targeting_categories(category_class=""))
