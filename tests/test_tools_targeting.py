"""Targeting tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.config import reload_settings
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

    async def estimate_audience_size(
        self,
        account_id: str,
        *,
        targeting_spec,
        optimization_goal=None,
    ):
        return {"data": [{"estimate_mau": 12000}]}

    def __init__(self, pages=None) -> None:
        # pages: cursor (None for the first page) -> Meta's answer for that page.
        self.pages = pages or {None: {"data": [
            {"id": "6000000000001", "name": "Frequent Travelers", "type": "behaviors"},
            {"id": "6000000000002", "name": "Console gamers", "type": "behaviors"},
            {"id": "6000000000003", "name": "Business travelers", "type": "behaviors"},
        ]}}
        self.category_calls: list[dict[str, object]] = []

    async def search_targeting_categories(self, *, category_class: str, after: str | None = None):
        self.category_calls.append({"category_class": category_class, "after": after})
        return self.pages[after]


def test_get_interest_suggestions_returns_collection(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(targeting.get_interest_suggestions(interest_list=["running"]))
    assert result["summary"]["count"] == 1
    assert result["items"][0]["id"] == "int_suggested"


def test_estimate_audience_size_rejects_silently_removed_v26_placement(
    monkeypatch,
) -> None:
    monkeypatch.setenv("META_API_VERSION", "v26.0")
    reload_settings()
    monkeypatch.setattr(
        targeting,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )

    with pytest.raises(targeting.ValidationError, match="messenger_positions=story"):
        asyncio.run(
            targeting.estimate_audience_size(
                account_id="123",
                targeting_spec={
                    "geo_locations": {"countries": ["US"]},
                    "messenger_positions": ["story"],
                },
            )
        )


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


def _category_page(rows, after=None):
    page = {"data": rows}
    if after:
        page["paging"] = {"cursors": {"after": after}, "next": f"https://graph.facebook.com/search?after={after}"}
    return page


def test_search_behaviors_matches_the_query_against_names_locally(monkeypatch) -> None:
    """F1: Meta's search route takes no name filter for categories, so the query is matched here."""
    client = FakeTargetingClient()
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(query="TRAVEL", account_id="123"))
    assert client.category_calls == [{"category_class": "behaviors", "after": None}]
    assert [item["id"] for item in result["items"]] == ["6000000000001", "6000000000003"]
    assert result["items"][0] == {"id": "6000000000001", "name": "Frequent Travelers", "type": "behaviors"}
    summary = result["summary"]
    assert summary["count"] == 2 and summary["rows_read"] == 3 and summary["rows_matched"] == 2
    assert summary["pages_read"] == 1 and summary["complete"] is True and summary["next_after"] is None
    assert result["paging"] == {"before": None, "after": None, "next": None}


def test_category_tools_need_no_account(monkeypatch) -> None:
    """The search route is not account-scoped, so a missing account id is no longer an error."""
    monkeypatch.delenv("META_DEFAULT_ACCOUNT_ID", raising=False)
    reload_settings()
    client = FakeTargetingClient()
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    assert asyncio.run(targeting.search_behaviors())["summary"]["count"] == 3
    assert asyncio.run(targeting.search_demographics())["summary"]["count"] == 3
    assert asyncio.run(targeting.get_targeting_categories(category_class="life_events"))["summary"]["count"] == 3
    assert [call["category_class"] for call in client.category_calls] == ["behaviors", "demographics", "life_events"]


def test_get_targeting_categories_and_search_demographics_send_the_given_class(monkeypatch) -> None:
    client = FakeTargetingClient()
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    asyncio.run(targeting.get_targeting_categories(category_class="life_events", query="new", account_id="123"))
    asyncio.run(targeting.search_demographics(demographic_class="work", query="manager", account_id="123"))
    assert [call["category_class"] for call in client.category_calls] == ["life_events", "work"]


def test_empty_query_returns_every_row_read(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    for query in (None, "", "  "):
        result = asyncio.run(targeting.search_behaviors(query=query))
        assert result["summary"]["count"] == 3 and result["summary"]["rows_matched"] == 3


def test_rows_read_but_none_matching_is_told_apart_from_nothing_read(monkeypatch) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    no_match = asyncio.run(targeting.search_behaviors(query="no such name"))
    assert no_match["items"] == []
    assert no_match["summary"]["rows_read"] == 3 and no_match["summary"]["rows_matched"] == 0
    assert no_match["summary"]["complete"] is True
    assert "3" in no_match["summary"]["note"] and "none" in no_match["summary"]["note"]

    empty = FakeTargetingClient(pages={None: {"data": []}})
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: empty)
    nothing = asyncio.run(targeting.get_targeting_categories(category_class="not_a_class"))
    assert nothing["summary"]["rows_read"] == 0 and nothing["summary"]["rows_matched"] == 0
    assert "no categories" in nothing["summary"]["note"] and "not_a_class" in nothing["summary"]["note"]


def test_rows_without_a_text_name_do_not_match_a_query(monkeypatch) -> None:
    client = FakeTargetingClient(pages={None: {"data": [{"id": "1"}, {"id": "2", "name": None}, {"id": "3", "name": "Travel"}]}})
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(query="travel"))
    assert [item["id"] for item in result["items"]] == ["3"]


@pytest.mark.parametrize("row", [{"id": "6000000000001"}, {"id": "6000000000001", "name": None},
                                 {"id": "6000000000001", "name": 7}, {"id": "6000000000001", "name": ["Travel"]}])
def test_a_row_without_a_readable_name_leaves_a_query_unjudged(monkeypatch, row) -> None:
    """Whether a row without a text name matches the query is not known, so the answer is not complete."""
    client = FakeTargetingClient(pages={None: {"data": [row]}})
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(query="travel"))
    summary = result["summary"]
    assert result["items"] == []
    assert summary["complete"] is False
    assert "name" in summary["incomplete_reason"] and "1 row" in summary["incomplete_reason"]
    assert "note" not in summary


def test_a_row_without_a_name_changes_nothing_without_a_query(monkeypatch) -> None:
    client = FakeTargetingClient(pages={None: {"data": [{"id": "1"}, {"id": "2", "name": "Travel"}]}})
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors())
    assert [item["id"] for item in result["items"]] == ["1", "2"]
    assert result["summary"]["complete"] is True and "incomplete_reason" not in result["summary"]


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": "x"}, ["not", "an", "object"]])
def test_an_unreadable_category_answer_is_not_an_empty_result(monkeypatch, payload) -> None:
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient(pages={None: payload}))
    result = asyncio.run(targeting.search_behaviors(query="travel"))
    assert result["items"] == []
    summary = result["summary"]
    assert summary["complete"] is False and summary["rows_read"] == 0
    assert summary["incomplete_reason"]
    assert "note" not in summary


def test_entries_that_are_not_objects_leave_the_result_incomplete(monkeypatch) -> None:
    client = FakeTargetingClient(pages={None: {"data": [{"id": "1", "name": "Travel"}, None]}})
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(query="travel"))
    assert [item["id"] for item in result["items"]] == ["1"]
    assert result["summary"]["complete"] is False and "could not be read" in result["summary"]["incomplete_reason"]


def test_category_search_follows_metas_pages_until_enough_rows_match(monkeypatch) -> None:
    client = FakeTargetingClient(pages={
        None: _category_page([{"id": "1", "name": "Gamers"}], after="c2"),
        "c2": _category_page([{"id": "2", "name": "Travel A"}], after="c3"),
        "c3": _category_page([{"id": "3", "name": "Travel B"}]),
    })
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(query="travel"))
    assert [call["after"] for call in client.category_calls] == [None, "c2", "c3"]
    assert [item["id"] for item in result["items"]] == ["2", "3"]
    summary = result["summary"]
    assert summary["pages_read"] == 3 and summary["rows_read"] == 3 and summary["complete"] is True


def test_category_search_stops_at_a_page_boundary_once_limit_rows_match(monkeypatch) -> None:
    client = FakeTargetingClient(pages={
        "start": _category_page([{"id": "1", "name": "Travel A"}], after="c2"),
        "c2": _category_page([{"id": "2", "name": "Travel B"}]),
    })
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(query="travel", limit=1, after="start"))
    assert [call["after"] for call in client.category_calls] == ["start"]
    assert [item["id"] for item in result["items"]] == ["1"]
    summary = result["summary"]
    assert summary["complete"] is False and summary["truncated"] is True and summary["next_after"] == "c2"
    assert result["paging"]["after"] == "c2"
    continued = asyncio.run(targeting.search_behaviors(query="travel", limit=1, after=summary["next_after"]))
    assert [item["id"] for item in continued["items"]] == ["2"] and continued["summary"]["complete"] is True


def test_matches_cut_by_limit_are_reported_not_skipped(monkeypatch) -> None:
    """Rows cut from the middle of a page cannot be reached with Meta's cursor, so no cursor is offered."""
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: FakeTargetingClient())
    result = asyncio.run(targeting.search_behaviors(limit=2))
    summary = result["summary"]
    assert summary["count"] == 2 and summary["rows_matched"] == 3
    assert summary["complete"] is False and summary["truncated"] is True and summary["next_after"] is None
    assert "limit" in summary["incomplete_reason"]


def test_category_search_reads_at_most_ten_pages_per_call(monkeypatch) -> None:
    pages = {None: _category_page([{"id": "0", "name": "Gamers"}], after="c1")}
    pages.update({f"c{n}": _category_page([{"id": str(n), "name": "Gamers"}], after=f"c{n + 1}") for n in range(1, 20)})
    client = FakeTargetingClient(pages=pages)
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(query="travel"))
    assert len(client.category_calls) == 10
    summary = result["summary"]
    assert summary["pages_read"] == 10 and summary["rows_read"] == 10 and summary["rows_matched"] == 0
    assert summary["complete"] is False and summary["next_after"] == "c10"
    assert "10 pages" in summary["incomplete_reason"]


@pytest.mark.parametrize("bad_paging", [
    {"next": "https://graph.facebook.com/search"},
    {"next": "https://graph.facebook.com/search", "cursors": {"after": "same"}},
])
def test_category_search_stops_on_a_missing_or_repeated_cursor(monkeypatch, bad_paging) -> None:
    client = FakeTargetingClient(pages={
        "same": {"data": [{"id": "1", "name": "Travel"}], "paging": bad_paging},
    })
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    result = asyncio.run(targeting.search_behaviors(after="same"))
    assert len(client.category_calls) == 1
    assert result["summary"]["complete"] is False and result["summary"]["next_after"] is None
    assert result["summary"]["incomplete_reason"]


def test_category_search_refuses_a_limit_below_one() -> None:
    with pytest.raises(targeting.ValidationError, match="limit"):
        asyncio.run(targeting.search_behaviors(limit=0))


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


def test_paginated_targeting_tools_forward_after_and_preserve_cursor(
    monkeypatch,
) -> None:
    class PagingTargetingClient:
        def __init__(self) -> None:
            self.after_values: list[str | None] = []

        def payload(self):
            return {
                "data": [{"id": "page_item"}],
                "paging": {"cursors": {"after": "NEXT_PAGE"}, "next": "next"},
            }

        async def search_interests(self, *, query, limit=25, after=None):
            self.after_values.append(after)
            return self.payload()

        async def get_interest_suggestions(
            self,
            *,
            interest_list,
            limit=25,
            after=None,
        ):
            self.after_values.append(after)
            return self.payload()

        async def validate_interests(
            self,
            *,
            interest_list=None,
            interest_ids=None,
            after=None,
        ):
            self.after_values.append(after)
            return self.payload()

        async def search_geo_locations(
            self,
            *,
            query,
            location_types=None,
            limit=25,
            after=None,
        ):
            self.after_values.append(after)
            return self.payload()

        async def get_reach_frequency_predictions(
            self,
            account_id,
            *,
            limit=25,
            after=None,
        ):
            self.after_values.append(after)
            return self.payload()

    client = PagingTargetingClient()
    monkeypatch.setattr(targeting, "get_graph_api_client", lambda: client)
    calls = [
        targeting.search_interests(query="running", after="CURSOR"),
        targeting.get_interest_suggestions(
            interest_list=["running"],
            after="CURSOR",
        ),
        targeting.validate_interests(
            interest_ids=["6003139266461"],
            after="CURSOR",
        ),
        # The three category tools read pages themselves; their paging is tested above.
        targeting.search_geo_locations(query="New York", after="CURSOR"),
        targeting.get_reach_frequency_predictions(
            account_id="123",
            after="CURSOR",
        ),
    ]

    results = [asyncio.run(call) for call in calls]
    blank_result = asyncio.run(
        targeting.search_interests(query="running", after=" ")
    )

    assert client.after_values == ["CURSOR"] * 5 + [None]
    assert all(result["paging"]["after"] == "NEXT_PAGE" for result in results)
    assert blank_result["paging"]["after"] == "NEXT_PAGE"
