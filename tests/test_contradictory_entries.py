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



# Round thirty-four (review R34, FF1): ids in Meta's answers that cannot be right.


@pytest.mark.parametrize("result", [{"id": True}, {"id": -12}, {"id": 1.5}, {"id": ["9"]}, {"id": {"id": "9"}}, {"id": "900"}, {"id": 900}, {"id": " 900 "}, {"id": "act_800"}, {"id": "800"}])
def test_a_create_answer_whose_id_cannot_be_the_new_object_shows_nothing_as_created(result):
    from meta_ads_mcp.schemas import creation_response

    response = creation_response(action="create_thing", target={"account_id": "act_800", "campaign_id": "900"}, result=result)
    assert response["created"] is None and response["created_id_unresolved"] is True
    assert response["unresolved_result"] == result and "may have been created" in response["warning"]


@pytest.mark.parametrize("result", [{"id": "901"}, {"id": 901}, {"id": "thing_1"}, {"success": True}, {}, {"id": None}, {"id": ""}, {"handles": ["h"]}])
def test_an_ordinary_create_answer_is_shown_as_created_as_before(result):
    from meta_ads_mcp.schemas import creation_response

    response = creation_response(action="create_thing", target={"account_id": "act_800", "parent_id": "900"}, result=result)
    assert response["created"] == result and "created_id_unresolved" not in response and "warning" not in response


def test_validation_only_create_answers_are_never_judged():
    from meta_ads_mcp.schemas import creation_response

    response = creation_response(action="create_thing", target={"account_id": "act_800"}, result={"id": True}, validate_only=True)
    assert response["created"] is None and response["validation"] == {"id": True} and "warning" not in response


def test_catalog_owner_is_not_taken_from_an_answer_about_another_catalog():
    from meta_ads_mcp.errors import ValidationError
    from meta_ads_mcp.tools import business_access

    class Client:
        def __init__(self, answer):
            self.answer = answer

        async def get_object(self, object_id, *, fields=None, params=None):
            return self.answer

    with pytest.raises(ValidationError, match="named a different catalog"):
        asyncio.run(business_access._catalog_business_id(Client({"id": "964999", "business": {"id": "964998"}}), "964000"))
    for answer in ({"id": "964000", "business": {"id": "30"}}, {"id": 964000, "business": {"id": "30"}}, {"business": {"id": "30"}}):
        assert asyncio.run(business_access._catalog_business_id(Client(answer), "964000")) == "30"


@pytest.mark.parametrize("named", ["969999", None, "969001", 969001])
def test_feed_rule_update_reads_its_own_rule_or_stops(named, monkeypatch):
    from meta_ads_mcp.errors import MetaApiError

    rule = {"attribute": "title", "rule_type": "mapping_rule", "params": {"map_from": "name"}}
    if named is not None:
        rule["id"] = named
    writes = []

    class Client:
        async def get_object(self, object_id, *, fields=None, params=None):
            return deepcopy(rule)

        async def update_object(self, object_id, *, data):
            writes.append((object_id, data))
            return {"success": True}

    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: Client())
    call = catalog_feeds.update_feed_rule(rule_id="969001", params={"map_from": "name"})
    if named == "969999":
        with pytest.raises(MetaApiError, match="named a different rule"):
            asyncio.run(call)
    else:
        result = asyncio.run(call)
        assert result["current"]["unchanged"] is True and result["sent"] is False
    assert writes == []


def test_a_batch_error_naming_an_item_that_was_not_sent_confirms_no_item():
    row = {"status": "finished", "errors_total_count": 1, "errors": [], "ids_of_invalid_requests": ["FOREIGN-SKU"]}
    assert catalogs._batch_attribution(row)["complete"] is True
    judged = catalogs._batch_attribution(row, {"QA-SKU"})
    assert judged["complete"] is False and "did not send" in judged["reason"]
    own = {"status": "finished", "errors_total_count": 1, "errors": [], "ids_of_invalid_requests": ["QA-SKU"]}
    assert catalogs._batch_attribution(own, {"QA-SKU", "OTHER"})["complete"] is True
    clean = {"status": "finished", "errors_total_count": 0, "errors": [], "ids_of_invalid_requests": []}
    assert catalogs._batch_attribution(clean, {"QA-SKU"})["complete"] is True



# Round thirty-five (review R35, GG2): a value in target that is not a Graph id is never compared.


@pytest.mark.parametrize("target", [
    {"catalog_id": "976000", "retailer_id": "976010"}, {"catalog_id": "976000", "retailer_id": 976010},
    {"account_id": "act_1", "instagram_media_id": "976010"}, {"account_id": "act_1", "name": "976010"},
    {"account_id": "act_1", "parent_id": "976010"},
])
def test_a_new_id_equal_to_a_target_value_that_is_not_a_parent_is_shown_as_created(target):
    """A product's retailer_id is the merchant's SKU: an upsert can return a Graph id with the same digits."""
    from meta_ads_mcp.schemas import creation_response

    response = creation_response(action="create_thing", target=target, result={"id": "976010"})
    assert response["created"] == {"id": "976010"} and "created_id_unresolved" not in response


def test_a_product_upsert_whose_sku_has_the_same_digits_as_its_id_is_shown_as_created(monkeypatch):
    class Client:
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            return {"id": "976010"}

    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: Client())
    result = asyncio.run(catalogs.create_catalog_product(
        catalog_id="976000", retailer_id="976010", allow_upsert=True, name="QA upsert", price=12.5, currency="USD",
        url="https://example.com/product", image_url="https://example.com/image.jpg",
    ))
    assert result["created"] == {"id": "976010"} and "created_id_unresolved" not in result


# Round thirty-five (review R35, GG1, the members that decide a write).


@pytest.mark.parametrize("business", [True, -12, {"id": True}, {"id": -12}, {"id": "982000"}, {"id": ["9"]}])
def test_an_owning_business_that_is_not_an_id_or_is_the_object_itself_is_not_used(business):
    from meta_ads_mcp.errors import ValidationError
    from meta_ads_mcp.tools import business_access

    class Client:
        async def get_object(self, object_id, *, fields=None, params=None):
            return {"id": "982000", "business": business}

    if isinstance(business, dict):
        with pytest.raises(ValidationError, match="pass business_id"):
            asyncio.run(business_access._catalog_business_id(Client(), "982000"))
    with pytest.raises(ValidationError, match="pass business_id"):
        asyncio.run(experiments._resolve_business_id(Client(), "act_982000", None))


@pytest.mark.parametrize("business", [{"id": "30"}, {"id": 30}, {"id": "biz_1"}, "30"])
def test_an_ordinary_owning_business_is_used_as_before(business):
    from meta_ads_mcp.tools import business_access

    class Client:
        async def get_object(self, object_id, *, fields=None, params=None):
            return {"id": "982000", "business": business}

    expected = str(business["id"] if isinstance(business, dict) else business)
    assert asyncio.run(experiments._resolve_business_id(Client(), "act_982000", None)) == expected
    if isinstance(business, dict):
        assert asyncio.run(business_access._catalog_business_id(Client(), "982000")) == expected


def test_a_copy_listed_as_the_copy_of_another_source_is_a_contradiction():
    from meta_ads_mcp.tools import campaigns

    check = lambda rows: campaigns._copy_list_contradicts("983020", rows, source_id="983001", own_type="campaign")
    assert check([{"ad_object_type": "campaign", "source_id": "983010", "copied_id": "983020"}]) is True
    assert check([{"ad_object_type": "campaign", "source_id": "983001", "copied_id": "983020"}]) is False
    assert check([{"ad_object_type": "campaign", "source_id": " 983001 ", "copied_id": 983020}]) is False
    assert check([{"ad_object_type": "campaign", "copied_id": "983020"}]) is False
    assert check([{"ad_object_type": "ad_set", "source_id": "983010", "copied_id": "983021"}]) is False
    assert check(None) is False
