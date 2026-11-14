"""Round twenty-eight (review R28, J1 and J2): entries from Meta that contradict each other.

When one check says both passed and failed, or one objective's results name two winners, no
verdict is taken from whichever came first. One entry, or entries that agree, are read as before.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import json
import pytest
from meta_ads_mcp.tools import catalog_feeds, experiments


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
