"""Round twenty-eight (review R28, J1 and J2): entries from Meta that contradict each other.

When one check says both passed and failed, or one objective's results name two winners, no
verdict is taken from whichever came first. One entry, or entries that agree, are read as before.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import json
import pytest
from meta_ads_mcp.tools import catalog_feeds, catalogs, experiments


class ReadClient:
    """Only the object/edge reads used by the public tools in these reproductions."""

    def __init__(self, *, objects=None, edges=None):
        self.objects = objects or {}
        self.edges = edges or {}
        self.calls = []

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, fields, params))
        return deepcopy(self.objects[object_id])

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        self.calls.append(("list", parent_id, edge, fields, params))
        return deepcopy(self.edges[(parent_id, edge)])


@pytest.mark.parametrize("reverse", [False, True], ids=["passed-first", "failed-first"])
def test_j1_pixel_recommendation_withholds_contradictory_check(reverse, monkeypatch):
    # The same pixel, check key and response page say both passed and failed.
    checks = [
        {"key": "pixel_missing_dpa_event", "result": "passed"},
        {"key": "pixel_missing_dpa_event", "result": "failed"},
    ]
    if reverse:
        checks.reverse()
    client = ReadClient(edges={
        ("900001", "external_event_sources"): {"data": []},
        ("900001", "event_stats"): {"data": []},
        ("910001", "adspixels"): {"data": [{"id": "920001", "name": "Candidate"}]},
        ("920001", "da_checks"): {"data": checks},
    })
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: client)

    result = asyncio.run(catalog_feeds.get_catalog_event_source_recommendations(
        catalog_id="900001", business_id="910001",
    ))

    assert len(result["recommendations"]) == 1
    recommendation = result["recommendations"][0]
    assert recommendation["strength"] == "unknown", recommendation


@pytest.mark.parametrize("reverse", [False, True], ids=["first-cell-first", "second-cell-first"])
def test_j2_split_test_withholds_contradictory_winners(reverse, monkeypatch):
    # One primary objective contains two different single-winner statements,
    # with no attribution window, date or other dimension distinguishing them.
    outcomes = [
        {"winner_ad_object_id": "930001", "winner_confidence": 0.95},
        {"winner_ad_object_id": "930002", "winner_confidence": 0.95},
    ]
    if reverse:
        outcomes.reverse()
    client = ReadClient(
        objects={"940001": {
            "id": "940001", "type": "SPLIT_TEST",
            "start_time": "2026-10-01T00:00:00+0000",
            "end_time": "2026-10-15T00:00:00+0000",
        }},
        edges={
            ("940001", "cells"): {"data": [
                {"id": "950001", "name": "A", "treatment_percentage": 50,
                 "adsets": {"data": [{"id": "930001"}]}},
                {"id": "950002", "name": "B", "treatment_percentage": 50,
                 "adsets": {"data": [{"id": "930002"}]}},
            ]},
            ("940001", "objectives"): {"data": [{
                "id": "960001", "is_primary": True,
                "results": [json.dumps(outcome) for outcome in outcomes],
            }]},
        },
    )
    monkeypatch.setattr(experiments, "get_graph_api_client", lambda: client)

    result = asyncio.run(experiments.get_ad_study(study_id="940001", include_results=True))

    assert result["summary"]["cells_complete"] is True
    assert result["summary"]["objectives_complete"] is True
    assert result["results"]["winner"] is None, result["results"]


@pytest.mark.parametrize(("results", "strength"), [
    (["passed"], "strong"), (["failed"], "weak"), (["passed", "passed"], "strong"),
    (["failed", "failed"], "weak"), (["passed", "unavailable"], "strong"), (["unavailable"], "unknown"),
])
def test_one_check_verdict_or_agreeing_repeats_are_read_as_before(results, strength, monkeypatch):
    client = ReadClient(edges={
        ("900001", "external_event_sources"): {"data": []},
        ("900001", "event_stats"): {"data": []},
        ("910001", "adspixels"): {"data": [{"id": "920001", "name": "Candidate"}]},
        ("920001", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": r} for r in results]},
    })
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: client)
    result = asyncio.run(catalog_feeds.get_catalog_event_source_recommendations(
        catalog_id="900001", business_id="910001",
    ))
    assert result["recommendations"][0]["strength"] == strength


def test_contradictory_check_shows_both_entries_as_evidence(monkeypatch):
    checks = [{"key": "pixel_missing_dpa_event", "result": "passed"}, {"key": "pixel_missing_dpa_event", "result": "failed"}]
    client = ReadClient(edges={
        ("900001", "external_event_sources"): {"data": []},
        ("900001", "event_stats"): {"data": []},
        ("910001", "adspixels"): {"data": [{"id": "920001", "name": "Candidate"}]},
        ("920001", "da_checks"): {"data": checks},
    })
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: client)
    recommendation = asyncio.run(catalog_feeds.get_catalog_event_source_recommendations(
        catalog_id="900001", business_id="910001",
    ))["recommendations"][0]
    assert [e for e in recommendation["evidence"] if e.get("key")] == checks
    assert "opposite results" in recommendation["reason"]


def _study(outcomes):
    return ReadClient(
        objects={"940001": {
            "id": "940001", "type": "SPLIT_TEST",
            "start_time": "2026-10-01T00:00:00+0000", "end_time": "2026-10-15T00:00:00+0000",
        }},
        edges={
            ("940001", "cells"): {"data": [
                {"id": "950001", "name": "A", "treatment_percentage": 50, "adsets": {"data": [{"id": "930001"}]}},
                {"id": "950002", "name": "B", "treatment_percentage": 50, "adsets": {"data": [{"id": "930002"}]}},
            ]},
            ("940001", "objectives"): {"data": [{
                "id": "960001", "is_primary": True, "results": [json.dumps(outcome) for outcome in outcomes],
            }]},
        },
    )


@pytest.mark.parametrize("outcomes", [
    [{"winner_ad_object_id": "930001", "winner_confidence": 0.95}],
    [{"winner_ad_object_id": "930001", "winner_confidence": 0.95}, {"winner_ad_object_id": "930001", "winner_confidence": 0.9}],
    [{"winner_ad_object_id": "930001", "winner_confidence": 0.95}, {"confidence_by_ad_object_id": {"930001": 0.95}}],
])
def test_one_winner_or_results_naming_the_same_winner_are_read_as_before(outcomes, monkeypatch):
    monkeypatch.setattr(experiments, "get_graph_api_client", lambda: _study(outcomes))
    results = asyncio.run(experiments.get_ad_study(study_id="940001", include_results=True))["results"]
    assert results["winner"]["ad_object_id"] == "930001" and results["winner"]["cell_id"] == "950001"
    assert results["message"] == "Meta reported a winning cell."
    assert "conflicting_winner_ids" not in results


def test_contradictory_winners_are_both_listed_and_none_is_named(monkeypatch):
    outcomes = [{"winner_ad_object_id": "930002"}, {"winner_ad_object_id": "930001"}]
    monkeypatch.setattr(experiments, "get_ad_study", experiments.get_ad_study)
    monkeypatch.setattr(experiments, "get_graph_api_client", lambda: _study(outcomes))
    results = asyncio.run(experiments.get_ad_study(study_id="940001", include_results=True))["results"]
    assert results["winner"] is None
    assert results["conflicting_winner_ids"] == ["930001", "930002"]
    assert results["message"] == "Meta's results name more than one winner, so no winner is named."



# Round twenty-nine (review R29, AA1 to AA3): differing high-performer sets, two products under one
# SKU, and occurrence counts that are negative.


@pytest.mark.parametrize("reverse", [False, True], ids=["A-first", "B-first"])
def test_aa1_creative_test_withholds_incompatible_high_performer_sets(reverse, monkeypatch):
    # One primary objective, with no date, attribution window or other dimension
    # distinguishing two incompatible authoritative outcome sets.
    outcomes = [
        {"winner_ad_object_id": None, "winner_confidence": None,
         "high_performer_ids": ["930001"],
         "confidence_by_ad_object_id": {"930001": 0.95, "930002": 0.05}},
        {"winner_ad_object_id": None, "winner_confidence": None,
         "high_performer_ids": ["930002"],
         "confidence_by_ad_object_id": {"930001": 0.05, "930002": 0.95}},
    ]
    if reverse:
        outcomes.reverse()
    client = ReadClient(
        objects={"940001": {
            "id": "940001", "type": "SPLIT_TEST_V2",
            "start_time": "2026-10-01T00:00:00+0000",
            "end_time": "2026-10-15T00:00:00+0000",
        }},
        edges={
            ("940001", "cells"): {"data": [
                {"id": "950001", "name": "A", "treatment_percentage": 50,
                 "ad_ids": ["930001"]},
                {"id": "950002", "name": "B", "treatment_percentage": 50,
                 "ad_ids": ["930002"]},
            ]},
            ("940001", "objectives"): {"data": [{
                "id": "960001", "is_primary": True,
                "results": [json.dumps(outcome) for outcome in outcomes],
            }]},
        },
    )
    monkeypatch.setattr(experiments, "get_graph_api_client", lambda: client)
    result = asyncio.run(experiments.get_ad_study(study_id="940001", include_results=True))
    assert result["summary"]["cells_complete"] is True
    assert result["summary"]["objectives_complete"] is True
    # Keep the raw entries, but do not promote the first set into the study's
    # authoritative high_performer_ids while the other set contradicts it.
    assert result["results"].get("high_performer_ids") is None, result["results"]


@pytest.mark.parametrize("reverse", [False, True], ids=["A-first", "B-first"])
def test_aa2_batch_previous_does_not_choose_one_of_two_products_for_same_sku(reverse, monkeypatch):
    # Meta documents content ids as unique per catalog. Two distinct Graph
    # products with the same SKU and different names are contradictory T2 data.
    rows = [
        {"id": "970001", "retailer_id": "QA-SKU", "name": "Old A"},
        {"id": "970002", "retailer_id": "QA-SKU", "name": "Old B"},
    ]
    if reverse:
        rows.reverse()
    client = ReadClient(edges={("900001", "products"): {"data": rows}})
    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: client)
    result = asyncio.run(catalogs.batch_update_catalog_products(
        catalog_id="900001",
        items=[{"method": "update", "retailer_id": "QA-SKU", "name": "New"}],
        dry_run=True,
    ))
    assert all(call[0] != "create" for call in client.calls)
    # Either refuse an ambiguous pre-read, or keep the update plan while marking
    # the conflicting prior value unavailable. Do not invent which old name it was.
    if result.get("sent") is False:
        return
    item = result["items"][0]
    prior_is_withheld = item.get("previous", {}).get("name") is None
    prior_is_qualified = "name" in item.get("previous_unavailable", [])
    assert prior_is_withheld or prior_is_qualified, item


@pytest.mark.parametrize(
    ("matched", "unmatched", "other"), [(-1, 2, 0), (2, -1, 0), (1, 1, -1)],
    ids=["negative-matched", "negative-unmatched", "negative-other"],
)
def test_aa3_catalog_health_does_not_derive_rates_from_negative_counts(matched, unmatched, other, monkeypatch):
    # A count of content-id occurrences cannot be negative. These are malformed
    # counters, not an ordinary response grouping or a negative financial amount.
    client = ReadClient(edges={
        ("900001", "external_event_sources"): {"data": [
            {"id": "920001", "source_type": "PIXEL"},
        ]},
        ("900001", "event_stats"): {"data": [{
            "date_start": "2026-11-01", "date_stop": "2026-11-01", "event": "Purchase",
            "event_source": {"id": "920001", "source_type": "PIXEL"},
            "total_matched_content_ids": matched,
            "total_unmatched_content_ids": unmatched,
            "total_content_ids_matched_other_catalogs": other,
        }]},
        ("920001", "da_checks"): {"data": []},
    })
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: client)
    result = asyncio.run(catalog_feeds.get_catalog_event_source_health(
        catalog_id="900001", event_source_id="920001",
    ))
    source = result["sources"][0]
    # Retain the upstream evidence, but a negative occurrence count cannot support
    # a known match rate or a probability interval (currently -1.0 or 2.0).
    if matched < 0 or unmatched < 0:
        assert source["window_match_rate"] is None, source
    assert source["across_catalogs"]["all_catalogs_match_rate_range"] is None, source["across_catalogs"]


@pytest.mark.parametrize("sets", [[["930001"]], [["930001"], ["930001"]], [["930001", "930002"], ["930002", "930001"]]])
def test_one_high_performer_set_or_sets_that_agree_are_read_as_before(sets, monkeypatch):
    client = ReadClient(
        objects={"940001": {"id": "940001", "type": "SPLIT_TEST_V2",
                            "start_time": "2026-10-01T00:00:00+0000", "end_time": "2026-10-15T00:00:00+0000"}},
        edges={
            ("940001", "cells"): {"data": [
                {"id": "950001", "name": "A", "treatment_percentage": 50, "ads": {"data": [{"id": "930001"}]}},
                {"id": "950002", "name": "B", "treatment_percentage": 50, "ads": {"data": [{"id": "930002"}]}},
            ]},
            ("940001", "objectives"): {"data": [{
                "id": "960001", "is_primary": True,
                "results": [json.dumps({"winner_ad_object_id": None, "high_performer_ids": ids}) for ids in sets],
            }]},
        },
    )
    monkeypatch.setattr(experiments, "get_graph_api_client", lambda: client)
    results = asyncio.run(experiments.get_ad_study(study_id="940001", include_results=True))["results"]
    assert results["high_performer_ids"] == sets[0]
    assert "conflicting_high_performer_ids" not in results


@pytest.mark.parametrize("rows", [
    [{"id": "970001", "retailer_id": "QA-SKU", "name": "Old A"}],
    [{"id": "970001", "retailer_id": "QA-SKU", "name": "Old A"}, {"id": "970001", "retailer_id": "QA-SKU", "name": "Old A"}],
])
def test_one_product_for_a_sku_shows_its_previous_values_as_before(rows, monkeypatch):
    client = ReadClient(edges={("900001", "products"): {"data": rows}})
    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: client)
    item = asyncio.run(catalogs.batch_update_catalog_products(
        catalog_id="900001", items=[{"method": "update", "retailer_id": "QA-SKU", "name": "New"}], dry_run=True,
    ))["items"][0]
    assert item["previous"] == {"name": "Old A"} and item["product_id"] == "970001"
    assert "previous_note" not in item


def test_two_products_under_one_sku_keep_the_request_and_withhold_the_before_state(monkeypatch):
    rows = [{"id": "970001", "retailer_id": "QA-SKU", "name": "Old A"}, {"id": "970002", "retailer_id": "QA-SKU", "name": "Old B"}]
    client = ReadClient(edges={("900001", "products"): {"data": rows}})
    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: client)
    result = asyncio.run(catalogs.batch_update_catalog_products(
        catalog_id="900001", items=[{"method": "update", "retailer_id": "QA-SKU", "name": "New"}], dry_run=True,
    ))
    item = result["items"][0]
    assert item["outcome"] == "would_send"
    assert result["request"]["data"]["requests"] == [{"method": "UPDATE", "data": {"id": "QA-SKU", "title": "New"}}]
    assert item["previous"] == {} and item["previous_unavailable"] == ["name"] and "product_id" not in item
    assert "more than one product with this retailer_id" in item["previous_note"]


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (5, 5), ("7", 7), (-1, None), ("-3", None), (None, None), ("x", None)])
def test_an_occurrence_count_is_a_whole_number_of_zero_or_more(value, expected):
    assert catalog_feeds._occurrences(value) == expected


def test_zero_and_positive_occurrence_counts_give_the_same_rates_as_before(monkeypatch):
    client = ReadClient(edges={
        ("900001", "external_event_sources"): {"data": [{"id": "920001", "source_type": "PIXEL"}]},
        ("900001", "event_stats"): {"data": [{
            "date_start": "2026-11-01", "date_stop": "2026-11-01", "event": "Purchase",
            "event_source": {"id": "920001", "source_type": "PIXEL"},
            "total_matched_content_ids": 3, "total_unmatched_content_ids": 1,
            "total_content_ids_matched_other_catalogs": 0,
        }]},
        ("920001", "da_checks"): {"data": []},
    })
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: client)
    source = asyncio.run(catalog_feeds.get_catalog_event_source_health(
        catalog_id="900001", event_source_id="920001",
    ))["sources"][0]
    assert source["window_match_rate"] == 0.75
    assert source["across_catalogs"]["all_catalogs_match_rate_range"] == {"low": 0.75, "high": 0.75}
