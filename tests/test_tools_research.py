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


def test_search_ads_archive_forwards_after_and_preserves_cursor(monkeypatch) -> None:
    class PagingResearchClient(FakeResearchClient):
        async def search_ads_archive(self, *, after=None, **kwargs):
            assert after == "CURSOR"
            return {
                "data": [{"id": "archive_2"}],
                "paging": {
                    "cursors": {"after": "NEXT_PAGE"},
                    "next": "next",
                },
            }

    monkeypatch.setattr(research, "get_graph_api_client", lambda: PagingResearchClient())
    result = asyncio.run(
        research.search_ads_archive(
            search_terms="shirts",
            ad_reached_countries=["US"],
            after="CURSOR",
        )
    )

    assert result["paging"]["after"] == "NEXT_PAGE"


def test_search_ads_archive_normalizes_blank_after(monkeypatch) -> None:
    class BlankCursorResearchClient(FakeResearchClient):
        async def search_ads_archive(self, **kwargs):
            assert "after" not in kwargs
            return {"data": []}

    monkeypatch.setattr(
        research,
        "get_graph_api_client",
        lambda: BlankCursorResearchClient(),
    )
    result = asyncio.run(
        research.search_ads_archive(
            search_terms="shirts",
            ad_reached_countries=["US"],
            after=" ",
        )
    )

    assert result["items"] == []


# ---------------------------------------------------------------------------
# search_page_ids and ad_active_status (phase two, item 12)
# ---------------------------------------------------------------------------


class RecordingArchiveClient:
    """Records the old search_ads_archive method and the page/status search separately."""

    def __init__(self) -> None:
        self.archive_calls: list[dict] = []
        self.library_calls: list[dict] = []

    async def search_ads_archive(self, **kwargs):
        self.archive_calls.append(kwargs)
        return {"data": [{"id": "archive_1"}]}

    async def search_ads_library(self, **kwargs):
        self.library_calls.append(kwargs)
        return {"data": [{"id": "archive_2"}], "paging": {"cursors": {"after": "N"}, "next": "next"}}


def test_search_ads_archive_with_terms_is_unchanged(monkeypatch) -> None:
    """Calls that pass search_terms reach the same client method with the same arguments as before (eb3ed36)."""
    client = RecordingArchiveClient()
    monkeypatch.setattr(research, "get_graph_api_client", lambda: client)

    asyncio.run(research.search_ads_archive(search_terms="shirts", ad_reached_countries=["US"]))
    asyncio.run(research.search_ads_archive(
        search_terms="shoes", ad_reached_countries="US,GB", ad_type="POLITICAL_AND_ISSUE_ADS", limit=10,
        fields=["id", "page_name"], after="CURSOR",
    ))

    assert client.archive_calls == [
        {
            "search_terms": "shirts", "ad_reached_countries": ["US"], "ad_type": "ALL", "limit": 25,
            "fields": ["id", "page_name", "ad_snapshot_url", "ad_delivery_start_time", "ad_delivery_stop_time"],
        },
        {
            "search_terms": "shoes", "ad_reached_countries": "US,GB", "ad_type": "POLITICAL_AND_ISSUE_ADS",
            "limit": 10, "fields": ["id", "page_name"], "after": "CURSOR",
        },
    ]
    assert client.library_calls == []


def test_search_ads_archive_by_page_without_terms(monkeypatch) -> None:
    client = RecordingArchiveClient()
    monkeypatch.setattr(research, "get_graph_api_client", lambda: client)

    result = asyncio.run(research.search_ads_archive(
        ad_reached_countries=["US"], search_page_ids=["111", "222"], ad_active_status="active",
    ))

    assert client.archive_calls == []
    assert client.library_calls == [{
        "ad_reached_countries": ["US"], "ad_type": "ALL", "limit": 25, "fields": research.ADS_ARCHIVE_FIELDS,
        "after": None, "search_terms": None, "search_page_ids": ["111", "222"], "ad_active_status": "ACTIVE",
    }]
    assert result["items"][0]["id"] == "archive_2"
    assert result["paging"]["after"] == "N"


def test_search_ads_archive_terms_with_active_status_use_the_library_search(monkeypatch) -> None:
    client = RecordingArchiveClient()
    monkeypatch.setattr(research, "get_graph_api_client", lambda: client)
    asyncio.run(research.search_ads_archive(search_terms="shirts", ad_reached_countries=["US"], ad_active_status="ALL"))
    assert client.library_calls[0]["search_terms"] == "shirts"
    assert client.library_calls[0]["ad_active_status"] == "ALL"
    assert client.library_calls[0]["search_page_ids"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ad_reached_countries": ["US"]},
        {"ad_reached_countries": ["US"], "search_page_ids": ["page_1"]},
        {"ad_reached_countries": ["US"], "search_page_ids": [str(index) for index in range(11)]},
        {"ad_reached_countries": ["US"], "search_page_ids": ["1"], "ad_active_status": "PAUSED"},
        {"ad_reached_countries": [], "search_page_ids": ["1"]},
    ],
)
def test_search_ads_archive_new_options_validate_before_any_call(monkeypatch, kwargs) -> None:
    client = RecordingArchiveClient()
    monkeypatch.setattr(research, "get_graph_api_client", lambda: client)
    with pytest.raises(research.ValidationError):
        asyncio.run(research.search_ads_archive(**kwargs))
    assert client.archive_calls == [] and client.library_calls == []


def test_search_ads_library_client_sends_documented_parameters(monkeypatch) -> None:
    from meta_ads_mcp.graph_api import GraphAPIClient

    sent: list[tuple[str, str, dict]] = []

    async def fake_request(self, method, path, *, params=None, **kwargs):
        sent.append((method, path, params))
        return {"data": []}

    monkeypatch.setattr(GraphAPIClient, "request", fake_request)
    client = object.__new__(GraphAPIClient)  # only the request method is used, and it is replaced
    asyncio.run(client.search_ads_library(
        ad_reached_countries=["US"], ad_type="ALL", limit=25, fields=["id", "page_name"], after=None,
        search_terms=None, search_page_ids=["111", "222"], ad_active_status="ACTIVE",
    ))
    assert sent == [("GET", "ads_archive", {
        "ad_reached_countries": ["US"], "ad_type": "ALL", "limit": 25, "fields": "id,page_name",
        "search_page_ids": "111,222", "ad_active_status": "ACTIVE",
    })]
