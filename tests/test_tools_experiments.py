"""Experiment (A/B test and lift study) tool tests."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import pytest

from meta_ads_mcp.errors import MetaApiError, NotFoundError
from meta_ads_mcp.normalize import collection_fault
from meta_ads_mcp.tools import experiments

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())
FUTURE_START = "2026-12-01T00:00:00+00:00"
FUTURE_END = "2026-12-15T00:00:00+00:00"
FUTURE_START_TS = int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp())
FUTURE_END_TS = int(datetime(2026, 12, 15, tzinfo=timezone.utc).timestamp())


class FakeStudyClient:
    """Records every Graph call and serves canned objects and edges."""

    def __init__(self, objects=None, edges=None, errors=None) -> None:
        self.calls: list[tuple] = []
        self.objects = objects or {}
        self.edges = edges or {}
        self.errors = errors or {}

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or []), params))
        if object_id in self.errors:
            raise self.errors[object_id]
        return dict(self.objects.get(object_id, {"id": object_id}))

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        self.calls.append(("list", parent_id, edge, list(fields or []), params))
        if (parent_id, edge) in self.errors:
            raise self.errors[(parent_id, edge)]
        return self.edges.get((parent_id, edge), {"data": []})

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, data))
        return {"id": "study_new"}

    async def update_object(self, object_id, *, data):
        self.calls.append(("update", object_id, data))
        return {"success": True}

    async def delete_object(self, object_id):
        self.calls.append(("delete", object_id))
        return {"success": True}

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(experiments, "_now", lambda: NOW)


def use(monkeypatch, client: FakeStudyClient) -> FakeStudyClient:
    monkeypatch.setattr(experiments, "get_graph_api_client", lambda: client)
    return client


def no_client(monkeypatch) -> None:
    monkeypatch.setattr(
        experiments, "get_graph_api_client", lambda: pytest.fail("No client should be created")
    )


def run(coro):
    return asyncio.run(coro)


# --- list_ad_studies -------------------------------------------------------------


def _study(study_id, study_type, start, end, canceled=None):
    study = {"id": study_id, "name": f"Study {study_id}", "type": study_type,
             "start_time": start, "end_time": end}
    if canceled:
        study["canceled_time"] = canceled
    return study


def test_list_ad_studies_account_uses_documented_impacting_edge(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(edges={
        ("act_123", "impacting_ad_studies"): {"data": [
            _study("s1", "SPLIT_TEST", "2026-11-01T00:00:00+0000", "2026-11-20T00:00:00+0000"),
            _study("s2", "LIFT", "2026-12-01T00:00:00+0000", "2026-12-20T00:00:00+0000"),
            _study("s3", "LIFT", "2026-10-01T00:00:00+0000", "2026-10-20T00:00:00+0000"),
            _study("s4", "LIFT", "2026-11-01T00:00:00+0000", "2026-11-20T00:00:00+0000",
                   canceled="2026-11-05T00:00:00+0000"),
        ]},
    }))
    result = run(experiments.list_ad_studies(account_id="123"))
    kind, parent, edge, fields, params = client.calls[0]
    assert (kind, parent, edge) == ("list", "act_123", "impacting_ad_studies")
    # Literal field contract from Meta's Ad Study reference, not the module's own constant (F32).
    assert fields == [
        "id", "name", "type", "description", "start_time", "end_time", "canceled_time",
        "results_first_available_date",
    ]
    assert params == {"limit": 50}
    assert [item["state"] for item in result["items"]] == ["running", "scheduled", "ended", "canceled"]
    assert result["summary"]["count"] == 4
    assert result["summary"]["complete"] is True
    assert result["summary"]["next_after"] is None
    assert result["summary"]["source_edge"] == "impacting_ad_studies"


def test_list_ad_studies_business_and_entity_edges(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient())
    run(experiments.list_ad_studies(business_id="biz_1", limit=10, after="cur"))
    run(experiments.list_ad_studies(entity_id="cmp_9"))
    assert client.calls[0][1:3] == ("biz_1", "ad_studies")
    assert client.calls[0][4] == {"limit": 10, "after": "cur"}
    assert client.calls[1][1:3] == ("cmp_9", "ad_studies")


@pytest.mark.parametrize("kwargs", [
    {},
    {"account_id": "123", "business_id": "biz_1"},
    {"account_id": "123", "limit": 0},
    {"account_id": "123", "study_type": "NOT_A_TYPE"},
])
def test_list_ad_studies_validation(monkeypatch, kwargs) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError):
        run(experiments.list_ad_studies(**kwargs))


def test_list_ad_studies_filters_type_locally_and_reports_it(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(edges={
        ("biz_1", "ad_studies"): {
            "data": [_study("s1", "SPLIT_TEST", None, None), _study("s2", "LIFT", None, None)],
            "paging": {"cursors": {"after": "next"}, "next": "https://graph.facebook.com/next"},
        },
    }))
    result = run(experiments.list_ad_studies(business_id="biz_1", study_type="split_test"))
    assert [item["id"] for item in result["items"]] == ["s1"]
    assert result["summary"]["count"] == 1
    assert result["summary"]["complete"] is False
    assert result["summary"]["next_after"] == "next"
    assert result["summary"]["study_type_filter"] == {
        "study_type": "SPLIT_TEST", "applied": "locally to this page", "removed_on_this_page": 1,
    }


def test_list_ad_studies_permission_error_is_not_swallowed(monkeypatch) -> None:
    error = MetaApiError(message="(#200) Permissions error", status_code=403, code=200)
    use(monkeypatch, FakeStudyClient(errors={("act_123", "impacting_ad_studies"): error}))
    with pytest.raises(MetaApiError, match="Permissions error"):
        run(experiments.list_ad_studies(account_id="123"))


# --- get_ad_study ------------------------------------------------------------------


SPLIT_STUDY = {
    "id": "study_1", "name": "Audience test", "type": "SPLIT_TEST",
    "start_time": "2026-10-01T00:00:00+0000", "end_time": "2026-10-15T00:00:00+0000",
}
SPLIT_CELLS = {"data": [
    {"id": "cell_a", "name": "Group A", "treatment_percentage": 50,
     "adsets": {"data": [{"id": "as_1"}]}},
    {"id": "cell_b", "name": "Group B", "treatment_percentage": 50,
     "adsets": {"data": [{"id": "as_2"}]}},
]}


def test_get_ad_study_reads_study_cells_and_objectives(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "name": "Purchases"}]}},
    ))
    result = run(experiments.get_ad_study(study_id=" study_1 "))
    # Literal field contracts from Meta's Ad Study, cell and objective documentation (F32).
    assert client.calls[0] == ("get", "study_1", [
        "id", "name", "type", "description", "start_time", "end_time", "canceled_time",
        "results_first_available_date", "observation_end_time", "cooldown_start_time",
        "created_time", "updated_time", "business",
    ], None)
    assert client.calls[1][1:3] == ("study_1", "cells")
    assert client.calls[1][3] == [
        "id", "name", "treatment_percentage", "control_percentage", "campaigns", "adsets", "adaccounts",
    ]
    assert client.calls[2][1:3] == ("study_1", "objectives")
    assert client.calls[2][3] == ["id", "name", "type", "is_primary", "last_updated_results"]
    assert result["item"]["state"] == "ended"
    assert result["cells"] == [
        {"id": "cell_a", "name": "Group A", "treatment_percentage": 50, "control_percentage": None,
         "campaign_ids": [], "adset_ids": ["as_1"], "ad_account_ids": []},
        {"id": "cell_b", "name": "Group B", "treatment_percentage": 50, "control_percentage": None,
         "campaign_ids": [], "adset_ids": ["as_2"], "ad_account_ids": []},
    ]
    assert result["objectives"][0]["id"] == "obj_1"
    assert "results" not in result


def test_get_ad_study_results_not_ready_is_stated(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "name": "Purchases"}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert "results" in client.calls[2][3]
    assert result["results"]["status"] == "not_ready"
    assert "not published results" in result["results"]["message"]
    assert "winner" not in result["results"]


def test_get_ad_study_split_test_winner_maps_to_cell(monkeypatch) -> None:
    results = [json.dumps({
        "winner_ad_object_id": "as_2", "winner_confidence": 0.93,
        "confidence_by_ad_object_id": {"as_1": 0.07, "as_2": 0.93},
        "high_performer_ids": None,
    })]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS,
               ("study_1", "objectives"): {"data": [
                   {"id": "obj_1", "name": "Purchases", "results": results},
               ]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["results"]["status"] == "available"
    assert result["results"]["kind"] == "split_test"
    assert result["results"]["winner"] == {
        "ad_object_id": "as_2", "cell_id": "cell_b", "cell_name": "Group B", "confidence": 0.93,
    }
    assert result["results"]["confidence_by_ad_object_id"] == {"as_1": 0.07, "as_2": 0.93}


def test_get_ad_study_split_results_without_winner_say_so(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": None, "winner_confidence": None,
                           "high_performer_ids": ["ad_1"]})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": {**SPLIT_STUDY, "type": "SPLIT_TEST_V2"}},
        edges={("study_1", "objectives"): {"data": [{"id": "obj_1", "results": results}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["results"]["status"] == "available"
    assert result["results"]["winner"] is None
    assert result["results"]["high_performer_ids"] == ["ad_1"]
    assert "no single winner" in result["results"]["message"]


def _winner(ad_object_id):
    return [json.dumps({"winner_ad_object_id": ad_object_id, "winner_confidence": 0.9})]


def test_study_winner_uses_primary_objective(monkeypatch) -> None:
    """F15: a secondary objective listed first never decides the reported winner."""
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS,
               ("study_1", "objectives"): {"data": [
                   {"id": "obj_40", "is_primary": False, "results": _winner("as_1")},
                   {"id": "obj_41", "is_primary": True, "results": _winner("as_2")},
               ]}},
    ))
    results = run(experiments.get_ad_study(study_id="study_1", include_results=True))["results"]
    assert results["objective_id"] == "obj_41"
    assert results["winner_basis"] == "primary_objective"
    assert results["winner"]["cell_id"] == "cell_b"
    assert [o["objective_id"] for o in results["objectives"]] == ["obj_40", "obj_41"]
    assert results["objectives"][0]["winner"]["cell_id"] == "cell_a"


@pytest.mark.parametrize("flags", [(None, None), (False, False), (True, True)])
def test_study_without_unique_primary_names_no_winner(monkeypatch, flags) -> None:
    """F15: with no unique primary objective, outcomes are per objective and no winner is named."""
    objectives = [{"id": "obj_40", "results": _winner("as_1")}, {"id": "obj_41", "results": _winner("as_2")}]
    for objective, flag in zip(objectives, flags):
        if flag is not None:
            objective["is_primary"] = flag
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS, ("study_1", "objectives"): {"data": objectives}},
    ))
    results = run(experiments.get_ad_study(study_id="study_1", include_results=True))["results"]
    assert results["winner"] is None
    assert "No single winner can be named" in results["message"]
    assert [o["winner"]["ad_object_id"] for o in results["objectives"]] == ["as_1", "as_2"]
    assert "objective_id" not in results


def test_study_primary_without_results_does_not_fall_back_to_secondary(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS,
               ("study_1", "objectives"): {"data": [
                   {"id": "obj_40", "is_primary": False, "results": _winner("as_1")},
                   {"id": "obj_41", "is_primary": True},
               ]}},
    ))
    results = run(experiments.get_ad_study(study_id="study_1", include_results=True))["results"]
    assert results["winner"] is None
    assert results["objective_id"] == "obj_41"
    assert "no usable result yet" in results["message"]
    assert results["objectives"][1] == {"objective_id": "obj_41", "is_primary": True, "status": "not_ready"}


LIFT_STUDY = {"id": "study_2", "type": "LIFT",
              "start_time": "2026-09-01T00:00:00+0000", "end_time": "2026-10-01T00:00:00+0000"}
LIFT_CELLS = {"data": [
    {"id": "cell_t", "name": "Test", "treatment_percentage": 45, "control_percentage": 5},
    {"id": "cell_u", "name": "Other", "treatment_percentage": 45, "control_percentage": 5},
]}
AGGREGATE_ROW = {"conversions_test": 2300, "conversions_control_scaled": 2000,
                 "conversions_incremental": 300, "conversions_confidence": 0.95}
CELL_ROW = {"cell_id": "cell_t", "conversions_test": 1200, "conversions_control_scaled": 1000,
            "conversions_incremental": 200, "conversions_incremental_lower": 150,
            "conversions_incremental_upper": 250, "conversions_confidence": 0.97,
            "conversions_CPiC": 12.5}


def _lift_client(monkeypatch, *, breakdown=None, errors=None, study=LIFT_STUDY):
    objects = {"study_2": study}
    if breakdown is not None:
        objects["obj_9"] = {"id": "obj_9", "results": breakdown}
    return use(monkeypatch, FakeStudyClient(
        objects=objects,
        errors=errors,
        edges={("study_2", "cells"): LIFT_CELLS,
               ("study_2", "objectives"): {"data": [
                   {"id": "obj_9", "name": "Purchases", "is_primary": True,
                    "last_updated_results": "2026-10-02", "results": [json.dumps(AGGREGATE_ROW)]},
               ]}},
    ))


def test_get_ad_study_lift_results_report_lift_and_confidence(monkeypatch) -> None:
    """F21: per-cell lift comes from the documented cell_id breakdown, kept apart from the aggregate."""
    other = {**CELL_ROW, "cell_id": "cell_u", "conversions_incremental": 100}
    client = _lift_client(monkeypatch, breakdown=[CELL_ROW, other])
    result = run(experiments.get_ad_study(study_id="study_2", include_results=True))
    assert ("get", "obj_9", ["id", "results"], {"breakdowns": ["cell_id"]}) in client.calls
    lift = result["results"]
    assert lift["status"] == "available"
    assert lift["kind"] == "lift"
    objective = lift["objectives"][0]
    assert objective["cells_status"] == "available"
    aggregate = objective["aggregate"][0]
    assert "cell_id" not in aggregate
    assert aggregate["incremental_conversions"] == 300
    assert aggregate["conversions_lift"] == pytest.approx(0.15)
    cell = objective["cells"][0]
    assert cell["cell_id"] == "cell_t"
    assert cell["cell_name"] == "Test"
    assert cell["incremental_conversions"] == 200
    assert cell["conversions_lift"] == pytest.approx(0.2)
    assert cell["conversions_lift_basis"] == "conversions_incremental / conversions_control_scaled"
    assert cell["confidence"] == 0.97
    assert cell["meta_fields"] == CELL_ROW
    assert [row["cell_name"] for row in objective["cells"]] == ["Test", "Other"]


def test_lift_aggregate_is_never_presented_as_a_cell(monkeypatch) -> None:
    """F21: without breakdown rows the aggregate stays aggregate and cells is empty, with the reason."""
    _lift_client(monkeypatch, breakdown=[])
    objective = run(experiments.get_ad_study(study_id="study_2", include_results=True))["results"]["objectives"][0]
    assert objective["cells"] == []
    assert objective["cells_status"] == "none_returned"
    assert "100" in objective["cells_note"]
    assert objective["aggregate"][0]["incremental_conversions"] == 300


def test_lift_cell_breakdown_failure_is_reported(monkeypatch) -> None:
    error = MetaApiError(message="(#100) Unsupported breakdown", status_code=400, code=100)
    _lift_client(monkeypatch, errors={"obj_9": error})
    objective = run(experiments.get_ad_study(study_id="study_2", include_results=True))["results"]["objectives"][0]
    assert objective["cells"] == []
    assert objective["cells_status"] == "unavailable"
    assert "Unsupported breakdown" in objective["cells_note"]
    assert objective["aggregate"][0]["incremental_conversions"] == 300


def test_non_lift_study_does_not_request_undocumented_breakdown(monkeypatch) -> None:
    client = _lift_client(monkeypatch, study={**LIFT_STUDY, "type": "GEO_LIFT"})
    objective = run(experiments.get_ad_study(study_id="study_2", include_results=True))["results"]["objectives"][0]
    assert not any(call[0] == "get" and call[1] == "obj_9" for call in client.calls)
    assert objective["cells_status"] == "not_requested"
    assert objective["cells"] == []


def test_study_nested_membership_paging_is_not_complete(monkeypatch) -> None:
    """F20: a cell whose expanded ad sets have a next page is not a complete membership."""
    cells = {"data": [
        {"id": "cell_a", "name": "Group A",
         "adsets": {"data": [{"id": "as_1"}], "paging": {"next": "https://graph.invalid/n",
                                                        "cursors": {"after": "c"}}}},
        {"id": "cell_b", "name": "Group B", "adsets": {"data": [{"id": "as_2"}]}},
    ]}
    results = [json.dumps({"winner_ad_object_id": "as_9", "winner_confidence": 0.9})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): cells,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "results": results}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    summary = result["summary"]
    assert summary["cells_complete"] is False
    assert summary["cell_nodes_complete"] is True
    assert summary["memberships_complete"] is False
    assert "incomplete" in summary["membership_note"]
    assert result["cells"][0]["membership_truncated"] == ["adset_ids"]
    assert "membership_truncated" not in result["cells"][1]
    winner = result["results"]["winner"]
    assert winner["cell_id"] is None
    assert "incomplete" in winner["cell_mapping_note"]


def test_get_ad_study_requires_id(monkeypatch) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError):
        run(experiments.get_ad_study(study_id=" "))


# --- check_ab_test_eligibility --------------------------------------------------


def _campaign(cid, account="123", status="ACTIVE"):
    return {"id": cid, "name": f"Campaign {cid}", "account_id": account,
            "effective_status": status, "objective": "OUTCOME_SALES", "buying_type": "AUCTION"}


def test_eligibility_is_labeled_local_precheck(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={
        "c1": _campaign("c1"), "c2": _campaign("c2"),
    }))
    result = run(experiments.check_ab_test_eligibility(
        account_id="123", level="campaign", entity_ids="c1, c2",
    ))
    assert result["check_type"] == "local_precheck"
    assert result["meta_verdict"] is False
    assert "not Meta's verdict" in result["label"]
    assert result["precheck_outcome"] == "passed"
    assert result["eligible_by_local_precheck"] is True
    assert result["metrics"]["entities_checked"] == 2
    assert all(rule["source"].startswith("https://developers.facebook.com/") for rule in result["rules_applied"])
    assert result["not_checked"]
    get_calls = [call for call in client.calls if call[0] == "get"]
    assert [call[1] for call in get_calls] == ["c1", "c2"]
    assert "objective" in get_calls[0][2] and "buying_type" in get_calls[0][2]
    edges = {(call[1], call[2]) for call in client.calls if call[0] == "list"}
    assert edges == {("c1", "ad_studies"), ("c2", "ad_studies"), ("act_123", "impacting_ad_studies")}


def test_eligibility_blocks_missing_entity_and_other_account(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"c2": _campaign("c2", account="999")},
        errors={"c1": NotFoundError("Meta object or edge not found: c1")},
    ))
    result = run(experiments.check_ab_test_eligibility(
        account_id="act_123", level="campaign", entity_ids=["c1", "c2"],
    ))
    entities = {entity["entity_id"]: entity for entity in result["entities"]}
    assert entities["c1"]["exists"] is False
    assert entities["c1"]["blockers"]
    assert any("999" in blocker for blocker in entities["c2"]["blockers"])
    assert result["eligible_by_local_precheck"] is False
    assert result["metrics"]["blocked"] == 2


def test_eligibility_entity_in_an_active_study_is_inconclusive(monkeypatch) -> None:
    """F19: Meta documents no rule on concurrent membership, so it can neither pass nor block."""
    use(monkeypatch, FakeStudyClient(
        objects={"as_1": {"id": "as_1", "account_id": "123", "effective_status": "ACTIVE"}},
        edges={("as_1", "ad_studies"): {"data": [
            _study("s1", "SPLIT_TEST", "2026-11-01T00:00:00+0000", "2026-11-30T00:00:00+0000"),
        ]}},
    ))
    result = run(experiments.check_ab_test_eligibility(
        account_id="123", level="adset", entity_ids=["as_1"],
    ))
    entity = result["entities"][0]
    assert entity["active_studies"] == [{"id": "s1", "name": "Study s1", "type": "SPLIT_TEST", "state": "running"}]
    assert entity["unresolved"]
    assert entity["blockers"] == []
    assert entity["outcome"] == "inconclusive"
    assert result["precheck_outcome"] == "inconclusive"
    assert result["eligible_by_local_precheck"] is None
    assert result["metrics"]["inconclusive"] == 1
    assert result["metrics"]["passed_local_precheck"] == 0
    assert "cannot say the test is eligible" in result["suggestions"][0]
    assert "not Meta's verdict" in result["label"]


def test_eligibility_unknown_study_read_is_not_a_pass(monkeypatch) -> None:
    """F19: a permission failure reading an entity's studies leaves the check unperformed."""
    error = MetaApiError(message="(#200) Permissions error", status_code=403, code=200)
    use(monkeypatch, FakeStudyClient(objects={"c1": _campaign("c1")}, errors={("c1", "ad_studies"): error}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["precheck_outcome"] == "inconclusive"
    assert result["eligible_by_local_precheck"] is None
    assert result["entities"][0]["active_studies"] is None
    assert any("Could not read the studies containing the campaign at entity_ids entry 1" in s for s in result["missing_signals"])
    assert any(f["severity"] == "unresolved" for f in result["findings"])


@pytest.mark.parametrize(("objects", "errors"), [
    # Entity unreadable for a reason other than not found: none of its checks were performed.
    ({}, {"c1": MetaApiError(message="(#200) Permissions error", status_code=403, code=200)}),
    # Meta returned no account_id: the same-account check was not performed.
    ({"c1": {"id": "c1", "effective_status": "ACTIVE"}}, {}),
    # Deleted entity: no documented study rule, so it is not settled either way.
    ({"c1": _campaign("c1", status="DELETED")}, {}),
])
def test_eligibility_unperformed_or_unsettled_checks_are_inconclusive(monkeypatch, objects, errors) -> None:
    use(monkeypatch, FakeStudyClient(objects=objects, errors=errors))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["entities"][0]["outcome"] == "inconclusive"
    assert result["precheck_outcome"] == "inconclusive"
    assert result["eligible_by_local_precheck"] is None


def test_eligibility_incomplete_account_count_is_inconclusive(monkeypatch) -> None:
    use(monkeypatch, PagedStudyClient(
        {("act_123", "impacting_ad_studies", None): {
            "data": [], "paging": {"next": "https://graph.invalid/?after=unextracted"}}},
        objects={"c1": _campaign("c1")},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["entities"][0]["outcome"] == "passed"
    assert result["account"]["unresolved"]
    assert result["precheck_outcome"] == "inconclusive"


def test_eligibility_blocker_outranks_unresolved(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1", account="999"), "c2": _campaign("c2", status="ARCHIVED")},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1", "c2"]))
    assert [entity["outcome"] for entity in result["entities"]] == ["failed", "inconclusive"]
    assert result["precheck_outcome"] == "failed"
    assert result["eligible_by_local_precheck"] is False


def test_eligibility_blocks_at_documented_concurrent_study_limit(monkeypatch) -> None:
    active = [_study(f"s{i}", "SPLIT_TEST", "2026-11-01T00:00:00+0000", "2026-11-30T00:00:00+0000")
              for i in range(100)]
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1")},
        edges={("act_123", "impacting_ad_studies"): {"data": active}},
    ))
    result = run(experiments.check_ab_test_eligibility(
        account_id="123", level="campaign", entity_ids=["c1"],
    ))
    assert result["eligible_by_local_precheck"] is False
    assert result["account"]["active_studies"] == 100
    assert result["account"]["blockers"]


class PagedStudyClient(FakeStudyClient):
    """Serves edge pages keyed by (parent, edge, after cursor)."""

    def __init__(self, pages, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pages = pages

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        self.calls.append(("list", parent_id, edge, list(fields or []), params))
        if (parent_id, edge) in self.errors:
            raise self.errors[(parent_id, edge)]
        return self.pages.get((parent_id, edge, (params or {}).get("after")), {"data": []})


RUNNING = ("2026-11-01T00:00:00+0000", "2026-11-30T00:00:00+0000")


def test_study_count_with_next_page_but_no_cursor_is_not_complete() -> None:
    """F11: a next link without a usable cursor is incomplete, never a completed count."""
    client = PagedStudyClient({
        ("act_10", "impacting_ad_studies", None): {
            "data": [_study("s1", "LIFT", *RUNNING)],
            "paging": {"next": "https://graph.invalid/?after=unextracted"},
        },
    })
    result = run(experiments._count_account_active_studies(client, "act_10", NOW))
    assert result["count_complete"] is False
    assert result["active_studies"] == 1
    assert "no cursor" in result["stop_reason"]


def test_study_count_repeated_cursor_stops_incomplete() -> None:
    paging = {"next": "https://graph.invalid/next", "cursors": {"after": "same"}}
    client = PagedStudyClient({
        ("act_10", "impacting_ad_studies", None): {"data": [_study("s1", "LIFT", *RUNNING)], "paging": paging},
        ("act_10", "impacting_ad_studies", "same"): {"data": [_study("s2", "LIFT", *RUNNING)], "paging": paging},
    })
    result = run(experiments._count_account_active_studies(client, "act_10", NOW))
    assert result["count_complete"] is False
    assert result["pages_read"] == 2
    assert result["active_studies"] == 2
    assert "already read" in result["stop_reason"]


def test_study_count_pages_to_the_last_page() -> None:
    client = PagedStudyClient({
        ("act_10", "impacting_ad_studies", None): {
            "data": [_study("s1", "LIFT", *RUNNING)],
            "paging": {"next": "https://graph.invalid/next", "cursors": {"after": "p2"}},
        },
        ("act_10", "impacting_ad_studies", "p2"): {"data": [_study("s2", "LIFT", *RUNNING)]},
    })
    result = run(experiments._count_account_active_studies(client, "act_10", NOW))
    assert result == {
        "active_studies": 2, "unclassified_studies": 0, "pages_read": 2, "pages_complete": True,
        "count_complete": True, "stop_reason": None, "unclassified_note": None,
    }


def test_eligibility_does_not_miss_entity_study_second_page(monkeypatch) -> None:
    """F20: the entity's ad_studies edge is paged, so a running study on page two is found."""
    use(monkeypatch, PagedStudyClient(
        {("c1", "ad_studies", None): {"data": [], "paging": {"next": "https://graph.invalid/n",
                                                             "cursors": {"after": "p2"}}},
         ("c1", "ad_studies", "p2"): {"data": [_study("s50", "SPLIT_TEST", *RUNNING)]}},
        objects={"c1": _campaign("c1")},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    entity = result["entities"][0]
    assert [study["id"] for study in entity["active_studies"]] == ["s50"]
    assert entity["active_studies_complete"] is True


@pytest.mark.parametrize("paging", [
    {"next": "https://graph.invalid/?after=unextracted"},
    {"next": "https://graph.invalid/n", "cursors": {"after": "p1"}},
])
def test_eligibility_reports_incomplete_entity_study_read(monkeypatch, paging) -> None:
    """F20: a missing cursor or the page cap leaves the entity's study list incomplete, and it is said."""
    monkeypatch.setattr(experiments, "ENTITY_STUDY_SCAN_PAGES", 1)
    use(monkeypatch, PagedStudyClient(
        {("c1", "ad_studies", None): {"data": [], "paging": paging}},
        objects={"c1": _campaign("c1")},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    entity = result["entities"][0]
    assert entity["active_studies"] == []
    assert entity["active_studies_complete"] is False
    assert any("studies containing the campaign at entity_ids entry 1" in signal for signal in result["missing_signals"])
    assert result["precheck_outcome"] == "inconclusive"


@pytest.mark.parametrize("kwargs", [
    {"level": "creative", "entity_ids": ["a1"]},
    {"level": "campaign", "entity_ids": []},
    {"level": "campaign", "entity_ids": [" "]},
])
def test_eligibility_validation(monkeypatch, kwargs) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError):
        run(experiments.check_ab_test_eligibility(account_id="123", **kwargs))


def test_eligibility_checks_up_to_its_own_per_call_limit(monkeypatch) -> None:
    ids = [str(value) for value in range(1000, 1000 + experiments.MAX_ELIGIBILITY_ENTITIES)]
    use(monkeypatch, FakeStudyClient(objects={key: _campaign(key) for key in ids}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="adset", entity_ids=ids))
    assert result["metrics"]["entities_checked"] == experiments.MAX_ELIGIBILITY_ENTITIES


def test_eligibility_entity_limit_is_this_tools_own_not_metas_cell_limit(monkeypatch) -> None:
    """R25 C9: Meta allows 150 cells and 100 entities per cell, so more than 150 entities can form a
    valid test. The per-call cap is this tool's own read limit, and the message says so."""
    no_client(monkeypatch)
    ids = [str(value) for value in range(1000, 1001 + experiments.MAX_ELIGIBILITY_ENTITIES)]
    with pytest.raises(experiments.ValidationError) as error:
        run(experiments.check_ab_test_eligibility(account_id="123", level="adset", entity_ids=ids))
    message = str(error.value)
    assert f"at most {experiments.MAX_ELIGIBILITY_ENTITIES} entities per call" in message
    assert "batches" in message
    assert "Meta" not in message and "cells" not in message
    docstring = experiments.check_ab_test_eligibility.__doc__
    assert f"at most {experiments.MAX_ELIGIBILITY_ENTITIES}" in docstring and "batches" in docstring
    assert "+" not in docstring
    rule = next(rule for rule in experiments.ELIGIBILITY_RULES if "150 cells" in rule["rule"])
    assert "entity_ids" not in rule["applied_as"]


@pytest.mark.parametrize("account_id", ["   ", "", "act_"])
@pytest.mark.parametrize("tool", ["check_ab_test_eligibility", "create_ab_test", "create_lift_study"])
def test_experiment_required_account_cannot_be_blank(monkeypatch, tool, account_id) -> None:
    """F33: a blank account id is rejected before any client is created, even with business_id."""
    no_client(monkeypatch)
    if tool == "check_ab_test_eligibility":
        call = experiments.check_ab_test_eligibility(
            account_id=account_id, level="campaign", entity_ids=["c1"],
        )
    elif tool == "create_ab_test":
        call = _ab(account_id=account_id, cells=_cells(50, 50, key="campaign_ids"))
    else:
        call = _lift(account_id=account_id)
    with pytest.raises(experiments.ValidationError, match="account_id"):
        run(call)


# --- create_ab_test ----------------------------------------------------------------


def _cells(*shares, key="adset_ids"):
    return [{"name": f"Cell {index}", "treatment_percentage": share, key: [f"e{index}"]}
            for index, share in enumerate(shares)]


def _ab(**overrides):
    kwargs = {"account_id": "123", "business_id": "biz_1", "name": "Test",
              "cells": _cells(50, 50), "start_time": FUTURE_START, "end_time": FUTURE_END}
    kwargs.update(overrides)
    return experiments.create_ab_test(**kwargs)


@pytest.mark.parametrize(("shares", "ok"), [
    ((50, 40), True),
    ((10, 10), True),
    ((50, 60), False),
    ((50, 50.01), False),
    ((33.34, 33.33, 33.33), True),
    ((50, 50), True),
])
def test_create_ab_test_shares_total_at_most_100(monkeypatch, shares, ok) -> None:
    """Meta's Ad Study reference: each share at least 10, all shares together at most 100."""
    if ok:
        client = use(monkeypatch, FakeStudyClient())
        run(_ab(cells=_cells(*shares)))
        assert client.kinds() == ["create"]
        assert [cell["treatment_percentage"] for cell in client.calls[0][3]["cells"]] == list(shares)
    else:
        no_client(monkeypatch)
        with pytest.raises(experiments.ValidationError, match="at most 100"):
            run(_ab(cells=_cells(*shares)))


def test_standard_split_test_can_use_less_than_the_whole_population(monkeypatch) -> None:
    """R25 C3: a campaign or ad set split test may cover part of the population."""
    cells = [
        {"name": "A", "treatment_percentage": 40, "campaign_ids": ["10"]},
        {"name": "B", "treatment_percentage": 40, "campaign_ids": ["11"]},
    ]
    client = use(monkeypatch, FakeStudyClient())
    run(experiments.create_ab_test(
        account_id="1", business_id="2", name="A small population test", cells=cells,
        start_time="2027-01-01T00:00:00Z", end_time="2027-01-08T00:00:00Z",
    ))
    assert [cell["treatment_percentage"] for cell in client.calls[-1][-1]["cells"]] == [40, 40]
    preview = run(experiments.create_ab_test(
        account_id="1", business_id="2", name="A small population test", cells=cells,
        start_time="2027-01-01T00:00:00Z", end_time="2027-01-08T00:00:00Z", dry_run=True,
    ))
    assert [cell["treatment_percentage"] for cell in preview["would_send"]["data"]["cells"]] == [40, 40]
    assert preview["would_send"]["data"]["type"] == "SPLIT_TEST"


@pytest.mark.parametrize("shares", [(50, 40), (40, 40, 10)])
def test_creative_test_shares_still_total_exactly_100(monkeypatch, shares) -> None:
    """Meta's split testing guide: a creative test's whole-number shares must sum to 100."""
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError, match="must total 100") as error:
        run(_ab(cells=_ad_cells(*shares), creative_test_lifetime_budget_percentage=50))
    assert str(sum(shares)) not in str(error.value)


def test_create_ab_test_rejects_entity_in_two_cells(monkeypatch) -> None:
    no_client(monkeypatch)
    cells = [{"name": "A", "treatment_percentage": 50, "adset_ids": ["as_1"]},
             {"name": "B", "treatment_percentage": 50, "adset_ids": ["as_2", "as_1"]}]
    with pytest.raises(experiments.ValidationError, match=r"same id appears in cells\[0\] and cells\[1\]") as error:
        run(_ab(cells=cells))
    assert "as_1" not in str(error.value), "any text can arrive in an id list, so it is not repeated"


def test_create_ab_test_requires_two_cells(monkeypatch) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError, match="two cells"):
        run(_ab(cells=_cells(100)))


@pytest.mark.parametrize("cells", [
    [{"name": "A", "treatment_percentage": 95, "adset_ids": ["a"]},
     {"name": "B", "treatment_percentage": 5, "adset_ids": ["b"]}],
    [{"name": "A", "treatment_percentage": 50.555, "adset_ids": ["a"]},
     {"name": "B", "treatment_percentage": 49.445, "adset_ids": ["b"]}],
    [{"name": "A", "treatment_percentage": 50},
     {"name": "B", "treatment_percentage": 50, "adset_ids": ["b"]}],
    [{"name": "A", "treatment_percentage": 50, "adset_ids": ["a"], "budget": 5},
     {"name": "B", "treatment_percentage": 50, "adset_ids": ["b"]}],
    [{"treatment_percentage": 50, "adset_ids": ["a"]},
     {"name": "B", "treatment_percentage": 50, "adset_ids": ["b"]}],
    [{"name": "A", "treatment_percentage": True, "adset_ids": ["a"]},
     {"name": "B", "treatment_percentage": 50, "adset_ids": ["b"]}],
])
def test_create_ab_test_rejects_bad_cells(monkeypatch, cells) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError):
        run(_ab(cells=cells))


@pytest.mark.parametrize("times", [
    {"start_time": FUTURE_END, "end_time": FUTURE_START},
    {"start_time": "2026-12-01T00:00:00"},
    {"start_time": "next tuesday"},
    {"confidence_level": float("nan")},
])
def test_create_ab_test_rejects_bad_times_and_confidence(monkeypatch, times) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError):
        run(_ab(**times))


def test_create_ab_test_posts_split_test_to_business(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient())
    cells = [{"name": "A", "treatment_percentage": 50, "campaign_ids": "c1, c2"},
             {"name": "B", "treatment_percentage": 50, "campaign_ids": ["c3"], "description": "new bid"}]
    result = run(_ab(cells=cells, confidence_level=0.9, hypothesis="Cost cap lowers CPA",
                     start_time=FUTURE_START_TS))
    assert client.calls == [("create", "biz_1", "ad_studies", {
        "name": "Test",
        "type": "SPLIT_TEST",
        "cells": [
            {"name": "A", "treatment_percentage": 50, "campaigns": ["c1", "c2"]},
            {"name": "B", "treatment_percentage": 50, "campaigns": ["c3"], "description": "new bid"},
        ],
        "start_time": FUTURE_START_TS,
        "end_time": FUTURE_END_TS,
        "confidence_level": 0.9,
        "description": "Cost cap lowers CPA",
    })]
    assert result["ok"] is True
    assert result["action"] == "create_ab_test"
    assert result["created"] == {"id": "study_new"}
    assert result["target"] == {"account_id": "act_123", "business_id": "biz_1"}


def test_create_ab_test_resolves_business_from_account(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={
        "act_123": {"id": "act_123", "business": {"id": "biz_77", "name": "Biz"}},
    }))
    run(_ab(business_id=None))
    assert client.calls[0] == ("get", "act_123", ["business"], None)
    assert client.calls[1][:3] == ("create", "biz_77", "ad_studies")


def test_create_ab_test_without_business_asks_for_business_id(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"act_123": {"id": "act_123"}}))
    with pytest.raises(experiments.ValidationError, match="business_id"):
        run(_ab(business_id=None))
    assert "create" not in client.kinds()


def test_create_ab_test_params_cannot_override_type(monkeypatch) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError, match="type"):
        run(_ab(params={"type": "LIFT"}))


# --- create_lift_study -------------------------------------------------------------


def _lift(**overrides):
    kwargs = {
        "account_id": "act_123", "business_id": "biz_1", "name": "Lift",
        "cells": [{"name": "Test", "treatment_percentage": 90, "control_percentage": 10}],
        "objectives": [{"name": "Purchases",
                        "adspixels": [{"id": "px_1", "event_names": ["fb_pixel_purchase"]}]}],
        "start_time": FUTURE_START, "end_time": FUTURE_END,
    }
    kwargs.update(overrides)
    return experiments.create_lift_study(**kwargs)


def test_create_lift_study_posts_lift_payload(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient())
    result = run(_lift())
    assert client.calls == [("create", "biz_1", "ad_studies", {
        "name": "Lift",
        "type": "LIFT",
        "cells": [{"name": "Test", "treatment_percentage": 90, "control_percentage": 10,
                   "adaccounts": ["123"]}],
        "objectives": [{"name": "Purchases", "type": "CONVERSIONS", "is_primary": True,
                        "adspixels": [{"id": "px_1", "event_names": ["fb_pixel_purchase"]}]}],
        "start_time": FUTURE_START_TS,
        "end_time": FUTURE_END_TS,
        "observation_end_time": FUTURE_END_TS,
    })]
    assert result["action"] == "create_lift_study"
    assert result["created"] == {"id": "study_new"}


def test_explicit_nonprimary_objective_is_not_promoted(monkeypatch) -> None:
    """R25 C11: the single-objective default applies only when is_primary was left out."""
    client = use(monkeypatch, FakeStudyClient())
    run(_lift(objectives=[{"name": "Purchases", "is_primary": False,
                           "customconversions": [{"id": "40"}]}]))
    assert client.calls[-1][-1]["objectives"][0]["is_primary"] is False


@pytest.mark.parametrize(("flags", "sent"), [
    ((None,), [True]),
    ((True,), [True]),
    ((None, None), [None, None]),
    ((False, None), [False, None]),
])
def test_objective_primary_default_only_fills_an_omitted_flag(monkeypatch, flags, sent) -> None:
    client = use(monkeypatch, FakeStudyClient())
    objectives = []
    for index, flag in enumerate(flags):
        objective = {"name": f"Objective {index}", "customconversions": [{"id": f"4{index}"}]}
        if flag is not None:
            objective["is_primary"] = flag
        objectives.append(objective)
    run(_lift(objectives=objectives))
    assert [objective.get("is_primary") for objective in client.calls[-1][-1]["objectives"]] == sent


def test_create_lift_study_uses_campaign_cells_when_given(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient())
    cells = [{"name": "A", "treatment_percentage": 45, "control_percentage": 5, "campaign_ids": ["c1"]},
             {"name": "B", "treatment_percentage": 45, "control_percentage": 5, "campaign_ids": ["c2"]}]
    run(_lift(cells=cells, description="Q1", observation_end_time="2026-12-22T00:00:00Z"))
    payload = client.calls[0][3]
    assert payload["cells"][0] == {"name": "A", "treatment_percentage": 45, "control_percentage": 5,
                                   "campaigns": ["c1"]}
    assert payload["description"] == "Q1"
    assert payload["observation_end_time"] == FUTURE_END_TS + 7 * 86400


def test_lift_cell_allocations_cannot_exceed_population(monkeypatch) -> None:
    """F24: percentages are shares of the whole population, so two 90/10 cells (200%) are rejected."""
    no_client(monkeypatch)
    cells = [{"name": "A", "treatment_percentage": 90, "control_percentage": 10, "campaign_ids": ["c1"]},
             {"name": "B", "treatment_percentage": 90, "control_percentage": 10, "campaign_ids": ["c2"]}]
    with pytest.raises(experiments.ValidationError, match="these cells total more"):
        run(_lift(cells=cells))


@pytest.mark.parametrize("cells", [
    # Meta's multi-cell example: A 50/20 and B 20/10, totalling 100.
    [{"name": "A", "treatment_percentage": 50, "control_percentage": 20, "campaign_ids": ["c1"]},
     {"name": "B", "treatment_percentage": 20, "control_percentage": 10, "campaign_ids": ["c2"]}],
    # Documented under-allocation: three evenly split groups totalling 99.
    [{"name": n, "treatment_percentage": 30, "control_percentage": 3, "campaign_ids": [f"c{n}"]}
     for n in ("A", "B", "C")],
])
def test_lift_cell_allocations_at_or_under_population_are_sent(monkeypatch, cells) -> None:
    client = use(monkeypatch, FakeStudyClient())
    run(_lift(cells=cells))
    assert client.kinds() == ["create"]


@pytest.mark.parametrize("overrides", [
    {"start_time": "2026-11-01T00:00:00Z"},
    {"cells": [{"name": "T", "treatment_percentage": 80, "control_percentage": 10}]},
    {"cells": [{"name": "A", "treatment_percentage": 80, "control_percentage": 30},
               {"name": "B", "treatment_percentage": 50, "control_percentage": 10}]},
    {"cells": []},
    {"objectives": []},
    {"objectives": [{"name": "A", "is_primary": True, "adspixels": [{"id": "p", "event_names": ["x"]}]},
                    {"name": "B", "is_primary": True, "adspixels": [{"id": "p", "event_names": ["y"]}]}]},
    {"objectives": [{"name": "A", "adspixels": [{"id": "p"}]}]},
    {"objectives": [{"name": "A", "type": "MADE_UP", "adspixels": [{"id": "p", "event_names": ["x"]}]}]},
    {"objectives": [{"name": "A", "pixel_id": "p"}]},
    {"observation_end_time": "2026-12-10T00:00:00Z"},
])
def test_create_lift_study_validation(monkeypatch, overrides) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError):
        run(_lift(**overrides))


# --- update_ad_study -----------------------------------------------------------------


RUNNING_STUDY = {
    "id": "study_1", "name": "Old name", "description": "Old", "type": "SPLIT_TEST",
    "start_time": "2026-11-01T00:00:00+0000", "end_time": "2026-11-30T00:00:00+0000",
}


def test_update_ad_study_requires_a_change(monkeypatch) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError, match="At least one"):
        run(experiments.update_ad_study(study_id="study_1"))


def test_update_ad_study_returns_previous_and_current(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": RUNNING_STUDY}))
    result = run(experiments.update_ad_study(
        study_id="study_1", name="New name", end_time=FUTURE_END,
    ))
    assert client.calls[0][:2] == ("get", "study_1")
    assert client.calls[1] == ("update", "study_1", {"name": "New name", "end_time": FUTURE_END_TS})
    assert result["action"] == "update_ad_study"
    assert result["previous"]["name"] == "Old name"
    assert result["previous"]["end_time"] == "2026-11-30T00:00:00+0000"
    assert result["previous"]["state"] == "running"
    assert result["current"] == {"name": "New name", "end_time": "2026-12-15T00:00:00+00:00"}


def test_update_ad_study_empty_description_is_sent_to_clear_it(monkeypatch) -> None:
    """An empty description is a value to send, not an omitted one."""
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": {**RUNNING_STUDY, "description": "Old"}}))
    result = run(experiments.update_ad_study(study_id="study_1", description=""))
    assert client.calls[-1] == ("update", "study_1", {"description": ""})
    assert result["previous"]["description"] == "Old"
    assert result["current"] == {"description": ""}


def test_update_ad_study_rejects_start_change_after_start_without_writing(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": RUNNING_STUDY}))
    with pytest.raises(experiments.ValidationError, match="start_time"):
        run(experiments.update_ad_study(study_id="study_1", start_time=FUTURE_START))
    assert client.kinds() == ["get"]


def test_update_ad_study_rejects_past_end_time(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": RUNNING_STUDY}))
    with pytest.raises(experiments.ValidationError, match="future"):
        run(experiments.update_ad_study(study_id="study_1", end_time="2026-11-10T00:00:00Z"))
    assert client.kinds() == ["get"]


SCHEDULED_STUDY = {
    "id": "study_1", "name": "Planned", "type": "SPLIT_TEST",
    "start_time": "2026-12-01T00:00:00+0000", "end_time": "2026-12-15T00:00:00+0000",
}
SCHEDULED_LIFT = {
    **SCHEDULED_STUDY, "type": "LIFT", "observation_end_time": "2026-12-15T00:00:00+0000",
}


@pytest.mark.parametrize(("study", "kwargs"), [
    # F23: new start after the stored end.
    (SCHEDULED_STUDY, {"start_time": "2026-12-20T00:00:00Z"}),
    # F23: new end before the stored (future) start.
    (SCHEDULED_STUDY, {"end_time": "2026-11-20T00:00:00Z"}),
    # Lift study: new end past the stored observation end with no new observation end.
    (SCHEDULED_LIFT, {"end_time": "2026-12-20T00:00:00Z"}),
    # Lift study: an observation end supplied in params that is before the new end.
    (SCHEDULED_LIFT, {"end_time": "2026-12-20T00:00:00Z",
                      "params": {"observation_end_time": "2026-12-18T00:00:00Z"}}),
])
def test_update_study_one_sided_edit_is_checked_against_stored_values(monkeypatch, study, kwargs) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": study}))
    with pytest.raises(experiments.ValidationError):
        run(experiments.update_ad_study(study_id="study_1", **kwargs))
    assert client.kinds() == ["get"]


@pytest.mark.parametrize(("study", "kwargs"), [
    (SCHEDULED_STUDY, {"start_time": "2026-12-10T00:00:00Z"}),
    (SCHEDULED_STUDY, {"end_time": "2026-12-20T00:00:00Z"}),
    (SCHEDULED_LIFT, {"end_time": "2026-12-20T00:00:00Z",
                      "params": {"observation_end_time": "2026-12-27T00:00:00Z"}}),
])
def test_update_study_valid_one_sided_edit_is_sent(monkeypatch, study, kwargs) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": study}))
    run(experiments.update_ad_study(study_id="study_1", **kwargs))
    assert client.kinds() == ["get", "update"]


def test_update_study_bad_params_observation_end_fails_before_network(monkeypatch) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError, match="observation_end_time"):
        run(experiments.update_ad_study(study_id="study_1", params={"observation_end_time": "soon"}))


def test_update_ad_study_passes_params_and_reports_them(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": RUNNING_STUDY}))
    result = run(experiments.update_ad_study(study_id="study_1", params={"confidence_level": 0.8}))
    assert client.calls[1] == ("update", "study_1", {"confidence_level": 0.8})
    assert result["current"] == {"confidence_level": 0.8}
    # F25: confidence_level is writable but not a readable Ad Study field, so no prior value is invented.
    assert "confidence_level" not in client.calls[0][2]
    assert "confidence_level" not in result["previous"]
    assert list(result["previous"]["unavailable_previous_values"]) == ["confidence_level"]
    assert "could not be read" in result["previous"]["unavailable_previous_values"]["confidence_level"]


def test_update_ad_study_params_change_reads_previous_value(monkeypatch) -> None:
    """F25: a readable field changed through params is in the pre-read, so previous is its real value."""
    study = {**SCHEDULED_LIFT, "cooldown_start_time": "2026-12-01T00:00:00+0000"}
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": study}))
    result = run(experiments.update_ad_study(
        study_id="study_1", params={"cooldown_start_time": FUTURE_START_TS + 3600},
    ))
    assert client.calls[0] == ("get", "study_1", [
        "id", "name", "description", "type", "start_time", "end_time", "observation_end_time",
        "canceled_time", "cooldown_start_time",
    ], None)
    assert result["previous"]["cooldown_start_time"] == "2026-12-01T00:00:00+0000"
    assert "unavailable_previous_values" not in result["previous"]


# --- cancel_ad_study -----------------------------------------------------------------


def test_cancel_ad_study_returns_prior_state(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": RUNNING_STUDY}))
    result = run(experiments.cancel_ad_study(study_id="study_1"))
    assert client.calls[1] == ("update", "study_1", {"end_time": NOW_TS + 60})
    assert result["ok"] is True
    assert result["action"] == "cancel_ad_study"
    assert result["previous"]["state"] == "running"
    assert result["previous"]["name"] == "Old name"
    assert result["previous"]["end_time"] == "2026-11-30T00:00:00+0000"
    assert result["current"]["operation"] == "end_early"
    assert result["current"]["end_time"] == "2026-11-12T12:01:00+00:00"


def test_cancel_ad_study_deletes_a_study_that_has_not_started(monkeypatch) -> None:
    scheduled = {**RUNNING_STUDY, "start_time": "2026-12-01T00:00:00+0000",
                 "end_time": "2026-12-20T00:00:00+0000"}
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": scheduled}))
    result = run(experiments.cancel_ad_study(study_id="study_1"))
    assert client.kinds() == ["get", "delete"]
    assert result["previous"]["state"] == "scheduled"
    assert result["current"]["operation"] == "delete"


@pytest.mark.parametrize("study", [
    {**RUNNING_STUDY, "end_time": "2026-11-05T00:00:00+0000"},
    {**RUNNING_STUDY, "canceled_time": "2026-11-03T00:00:00+0000"},
])
def test_cancel_ad_study_finished_study_sends_nothing(monkeypatch, study) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": study}))
    result = run(experiments.cancel_ad_study(study_id="study_1"))
    assert client.kinds() == ["get"]
    assert result["ok"] is False
    assert result["current"]["operation"] == "none"
    assert result["previous"]["state"] in {"ended", "canceled"}


@pytest.mark.parametrize("end_time", ["2026-11-12T12:00:30+0000", "2026-11-12T12:01:00+0000"])
def test_cancel_study_never_extends_running_study(monkeypatch, end_time) -> None:
    """F4: a running study already ending within the grace interval is left alone, never extended."""
    study = {**RUNNING_STUDY, "end_time": end_time}
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": study}))
    result = run(experiments.cancel_ad_study(study_id="study_1"))
    assert client.kinds() == ["get"]
    assert result["previous"]["state"] == "running"
    assert result["current"]["operation"] == "none"
    assert datetime.fromisoformat(result["current"]["end_time"]) == datetime.fromisoformat(end_time)
    assert "finish on its own shortly" in result["message"]
    assert "Nothing was sent" in result["message"]


def test_cancel_study_moves_end_earlier_when_it_ends_after_grace(monkeypatch) -> None:
    study = {**RUNNING_STUDY, "end_time": "2026-11-12T12:01:01+0000"}
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": study}))
    result = run(experiments.cancel_ad_study(study_id="study_1"))
    assert client.calls[1] == ("update", "study_1", {"end_time": NOW_TS + 60})
    assert result["current"]["operation"] == "end_early"


def test_cancel_ad_study_failed_pre_read_sends_no_write(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(errors={"gone": NotFoundError("not found: gone")}))
    with pytest.raises(NotFoundError):
        run(experiments.cancel_ad_study(study_id="gone"))
    assert client.kinds() == ["get"]


# --- registry and read-only -----------------------------------------------------------


def test_experiment_tools_are_grouped_and_classified() -> None:
    from meta_ads_mcp.read_only import read_only_tool_names
    from meta_ads_mcp.tools import utility

    reads = {"list_ad_studies", "get_ad_study", "check_ab_test_eligibility"}
    writes = {"create_ab_test", "create_lift_study", "update_ad_study", "cancel_ad_study"}
    assert set(utility.TOOL_GROUPS["experiments"]) == reads
    assert writes <= set(utility.TOOL_GROUPS["writes"])
    allowed = read_only_tool_names()
    assert reads <= allowed
    assert not writes & allowed


def test_experiment_intent_and_mutation_paths() -> None:
    from meta_ads_mcp.tools import utility

    exact = run(utility.get_capabilities(intent="run_ab_tests_or_lift_studies"))
    assert exact["selected_intent"]["recommended_order"][:3] == [
        "list_ad_studies", "get_ad_study", "check_ab_test_eligibility",
    ]
    fuzzy = run(utility.get_capabilities(intent="split test winner"))
    assert fuzzy["closest_intents"][0]["intent"] == "run_ab_tests_or_lift_studies"
    mutations = run(utility.list_mutation_tools())
    assert mutations["common_paths"]["experiments"] == [
        "create_ab_test", "create_lift_study", "update_ad_study", "cancel_ad_study",
    ]


# --- phase two (P4): backward compatibility of create_ab_test ------------------------


def test_create_ab_test_campaign_and_adset_cells_are_unchanged(monkeypatch) -> None:
    """Campaign and ad set cells send exactly the request they sent before ad-level cells existed."""
    client = use(monkeypatch, FakeStudyClient(objects={"act_123": {"id": "act_123", "business": {"id": "biz_9"}}}))
    result = run(experiments.create_ab_test(
        account_id="123", name=" Bid test ", start_time=FUTURE_START, end_time=FUTURE_END,
        cells=[{"name": "A", "treatment_percentage": 50, "campaign_ids": ["c1"], "adset_ids": "as_1"},
               {"name": "B", "treatment_percentage": 50, "adset_ids": ["as_2"]}],
        params={"viewers": [5], "cooldown_start_time": FUTURE_START_TS},
    ))
    assert client.calls == [
        ("get", "act_123", ["business"], None),
        ("create", "biz_9", "ad_studies", {
            "name": "Bid test",
            "type": "SPLIT_TEST",
            "cells": [
                {"name": "A", "treatment_percentage": 50, "campaigns": ["c1"], "adsets": ["as_1"]},
                {"name": "B", "treatment_percentage": 50, "adsets": ["as_2"]},
            ],
            "start_time": FUTURE_START_TS,
            "end_time": FUTURE_END_TS,
            "viewers": [5],
            "cooldown_start_time": FUTURE_START_TS,
        }),
    ]
    assert result == {
        "ok": True,
        "action": "create_ab_test",
        "target": {"account_id": "act_123", "business_id": "biz_9"},
        "created": {"id": "study_new"},
        "validation_only": False,
    }


# --- phase two (P4): ad-level (creative) cells -----------------------------------------


def _ad_cells(*shares):
    return [{"name": f"Ad {index}", "treatment_percentage": share, "ad_ids": [f"ad_{index}"]}
            for index, share in enumerate(shares)]


def test_create_ab_test_ad_cells_send_documented_creative_test(monkeypatch) -> None:
    """Meta's split testing guide: SPLIT_TEST_V2, one ad per cell, creative_test_config, cooldown and observation times."""
    client = use(monkeypatch, FakeStudyClient(objects={"act_123": {"id": "act_123", "currency": "USD"}}))
    result = run(_ab(cells=_ad_cells(50, 50), creative_test_daily_budget=150.25, hypothesis="Video beats image"))
    assert client.calls == [
        ("get", "act_123", ["currency"], None),
        ("create", "biz_1", "ad_studies", {
            "name": "Test",
            "type": "SPLIT_TEST_V2",
            "cells": [
                {"name": "Ad 0", "treatment_percentage": 50, "ads": ["ad_0"]},
                {"name": "Ad 1", "treatment_percentage": 50, "ads": ["ad_1"]},
            ],
            "start_time": FUTURE_START_TS,
            "end_time": FUTURE_END_TS,
            "cooldown_start_time": FUTURE_START_TS,
            "observation_end_time": FUTURE_END_TS,
            "creative_test_config": {"daily_budget": 15025},
            "description": "Video beats image",
        }),
    ]
    assert result["ok"] is True
    assert result["created"] == {"id": "study_new"}
    creative = result["creative_test"]
    assert creative["type"] == "SPLIT_TEST_V2"
    assert creative["budget"] == {"daily_budget": 150.25, "currency": "USD", "sent_as": 15025}
    assert any("minor units" in note for note in creative["notes"])


def test_create_ab_test_ad_cells_with_lifetime_budget_share(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient())
    run(_ab(cells=_ad_cells(34, 33, 33), creative_test_lifetime_budget_percentage=20))
    assert client.kinds() == ["create"]
    payload = client.calls[0][3]
    assert payload["creative_test_config"] == {"lifetime_budget_percentage": 20}
    assert [cell["ads"] for cell in payload["cells"]] == [["ad_0"], ["ad_1"], ["ad_2"]]


def test_create_ab_test_more_than_five_ad_cells_is_sent_with_a_warning(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient())
    result = run(_ab(cells=_ad_cells(15, 15, 14, 14, 14, 14, 14), creative_test_lifetime_budget_percentage=20))
    assert client.kinds() == ["create"]
    assert any("2 to 5" in warning for warning in result["creative_test"]["warnings"])


@pytest.mark.parametrize(("cells", "extra", "match"), [
    ([{"name": "A", "treatment_percentage": 50, "ad_ids": ["ad_1", "ad_2"]},
      {"name": "B", "treatment_percentage": 50, "ad_ids": ["ad_3"]}],
     {"creative_test_daily_budget": 10}, "exactly one ad"),
    ([{"name": "A", "treatment_percentage": 50, "ad_ids": ["ad_1"]},
      {"name": "B", "treatment_percentage": 50, "adset_ids": ["as_1"]}],
     {"creative_test_daily_budget": 10}, "every cell"),
    ([{"name": "A", "treatment_percentage": 50, "ad_ids": ["ad_1"], "adset_ids": ["as_1"]},
      {"name": "B", "treatment_percentage": 50, "ad_ids": ["ad_2"]}],
     {"creative_test_daily_budget": 10}, "every cell"),
    (_ad_cells(50, 50), {}, "creative_test_daily_budget"),
    (_ad_cells(50, 50), {"creative_test_daily_budget": 10, "creative_test_lifetime_budget_percentage": 20},
     "not both"),
    (_cells(50, 50), {"creative_test_daily_budget": 10}, "ad_ids"),
    (_cells(50, 50), {"creative_test_lifetime_budget_percentage": 20}, "ad_ids"),
    (_ad_cells(13, 13, 13, 13, 12, 12, 12, 12), {"creative_test_daily_budget": 10}, "7"),
    (_ad_cells(50.5, 49.5), {"creative_test_daily_budget": 10}, "whole number"),
    ([{"name": "A", "treatment_percentage": 50, "ad_ids": ["ad_1"]},
      {"name": "B", "treatment_percentage": 50, "ad_ids": ["ad_1"]}],
     {"creative_test_daily_budget": 10}, r"same ad id appears in cells\[0\] and cells\[1\]"),
    (_ad_cells(50, 50), {"creative_test_daily_budget": 0}, "creative_test_daily_budget"),
    (_ad_cells(50, 50), {"creative_test_daily_budget": float("nan")}, "creative_test_daily_budget"),
    (_ad_cells(50, 50), {"creative_test_lifetime_budget_percentage": 0}, "creative_test_lifetime_budget_percentage"),
    (_ad_cells(50, 50), {"creative_test_lifetime_budget_percentage": 101}, "creative_test_lifetime_budget_percentage"),
    (_ad_cells(50, 50), {"creative_test_lifetime_budget_percentage": True}, "creative_test_lifetime_budget_percentage"),
    (_ad_cells(50, 50), {"creative_test_daily_budget": 10, "params": {"creative_test_config": {"daily_budget": 1}}},
     "creative_test_config"),
    (_ad_cells(50, 50), {"creative_test_daily_budget": 10, "params": {"cooldown_start_time": 1}},
     "cooldown_start_time"),
    (_ad_cells(50, 50), {"creative_test_daily_budget": 10, "params": {"observation_end_time": 1}},
     "observation_end_time"),
])
def test_create_ab_test_ad_cells_validation(monkeypatch, cells, extra, match) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError, match=match):
        run(_ab(cells=cells, **extra))


def test_create_ab_test_daily_budget_with_too_many_decimals_fails_before_the_write(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"act_123": {"id": "act_123", "currency": "JPY"}}))
    with pytest.raises(experiments.ValidationError, match="decimal"):
        run(_ab(cells=_ad_cells(50, 50), creative_test_daily_budget=10.5))
    assert "create" not in client.kinds()


@pytest.mark.parametrize("kwargs", [
    {"cells": _ad_cells(50, 50), "creative_test_lifetime_budget_percentage": 20},
    {"cells": _cells(50, 50)},
])
def test_create_ab_test_dry_run_sends_nothing(monkeypatch, kwargs) -> None:
    client = use(monkeypatch, FakeStudyClient())
    result = run(_ab(dry_run=True, **kwargs))
    assert "create" not in client.kinds()
    assert result["dry_run"] is True
    assert result["created"] is None
    would_send = result["would_send"]
    assert (would_send["method"], would_send["parent_id"], would_send["edge"]) == ("POST", "biz_1", "ad_studies")
    real = use(monkeypatch, FakeStudyClient())
    run(_ab(**kwargs))
    assert would_send["data"] == real.calls[-1][3]


def test_create_ab_test_creative_dry_run_through_mcp_boundary(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    client = use(monkeypatch, FakeStudyClient())
    result = run(mcp_server.call_tool("create_ab_test", {
        "account_id": "123", "business_id": "biz_1", "name": "Creative", "cells": _ad_cells(50, 50),
        "start_time": FUTURE_START, "end_time": FUTURE_END,
        "creative_test_lifetime_budget_percentage": 20, "dry_run": True,
    })).structured_content
    assert client.calls == []
    assert result["would_send"]["data"]["type"] == "SPLIT_TEST_V2"
    assert result["would_send"]["data"]["creative_test_config"] == {"lifetime_budget_percentage": 20}


# --- phase two (P4): leftovers in list_ad_studies, get_ad_study, eligibility --------------


def test_list_ad_studies_next_page_without_cursor_states_the_stop_reason(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(edges={
        ("act_123", "impacting_ad_studies"): {
            "data": [_study("s1", "LIFT", None, None)],
            "paging": {"next": "https://graph.invalid/?after=unextracted"},
        },
    }))
    summary = run(experiments.list_ad_studies(account_id="123"))["summary"]
    assert summary["complete"] is False
    assert summary["next_after"] is None
    assert "no cursor" in summary["stop_reason"]


def test_list_ad_studies_continuable_or_complete_page_has_no_stop_reason(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(edges={
        ("act_123", "impacting_ad_studies"): {
            "data": [], "paging": {"next": "https://graph.invalid/n", "cursors": {"after": "p2"}},
        },
    }))
    summary = run(experiments.list_ad_studies(account_id="123"))["summary"]
    assert summary["complete"] is False
    assert summary["next_after"] == "p2"
    assert summary["stop_reason"] is None


def _cell(cell_id, adset_id):
    return {"id": cell_id, "name": f"Group {cell_id}", "treatment_percentage": 50,
            "adsets": {"data": [{"id": adset_id}]}}


def test_get_ad_study_pages_cells_and_objectives(monkeypatch) -> None:
    client = use(monkeypatch, PagedStudyClient(
        {
            ("study_1", "cells", None): {"data": [_cell("a", "as_1")],
                                         "paging": {"next": "https://graph.invalid/n", "cursors": {"after": "c2"}}},
            ("study_1", "cells", "c2"): {"data": [_cell("b", "as_2")]},
            ("study_1", "objectives", None): {"data": [{"id": "obj_1"}],
                                              "paging": {"next": "https://graph.invalid/n",
                                                         "cursors": {"after": "o2"}}},
            ("study_1", "objectives", "o2"): {"data": [{"id": "obj_2", "is_primary": True}]},
        },
        objects={"study_1": SPLIT_STUDY},
    ))
    result = run(experiments.get_ad_study(study_id="study_1"))
    lists = [call for call in client.calls if call[0] == "list"]
    assert [(call[2], call[4]) for call in lists] == [
        ("cells", {"limit": 150}), ("cells", {"limit": 150, "after": "c2"}),
        ("objectives", {"limit": 100}), ("objectives", {"limit": 100, "after": "o2"}),
    ]
    assert [cell["id"] for cell in result["cells"]] == ["a", "b"]
    assert [objective["id"] for objective in result["objectives"]] == ["obj_1", "obj_2"]
    summary = result["summary"]
    assert summary["cell_nodes_complete"] is True
    assert summary["objectives_complete"] is True
    assert summary["cells_pages_read"] == 2
    assert summary["objectives_pages_read"] == 2
    assert summary["cells_stop_reason"] is None
    assert summary["objectives_stop_reason"] is None


@pytest.mark.parametrize(("pages", "cap", "reason"), [
    ({("study_1", "cells", None): {"data": [_cell("a", "as_1")],
                                   "paging": {"next": "https://graph.invalid/?after=x"}}}, 10, "no cursor"),
    ({("study_1", "cells", None): {"data": [_cell("a", "as_1")],
                                   "paging": {"next": "https://graph.invalid/n", "cursors": {"after": "c2"}}},
      ("study_1", "cells", "c2"): {"data": [_cell("b", "as_2")],
                                   "paging": {"next": "https://graph.invalid/n", "cursors": {"after": "c2"}}}},
     10, "already read"),
    ({("study_1", "cells", None): {"data": [_cell("a", "as_1")],
                                   "paging": {"next": "https://graph.invalid/n", "cursors": {"after": "c2"}}}},
     1, "1-page"),
])
def test_get_ad_study_incomplete_cell_read_states_why(monkeypatch, pages, cap, reason) -> None:
    monkeypatch.setattr(experiments, "STUDY_EDGE_PAGE_CAP", cap)
    use(monkeypatch, PagedStudyClient(pages, objects={"study_1": SPLIT_STUDY}))
    result = run(experiments.get_ad_study(study_id="study_1"))
    summary = result["summary"]
    assert summary["cell_nodes_complete"] is False
    assert summary["cells_complete"] is False
    assert reason in summary["cells_stop_reason"]


def test_get_ad_study_incomplete_objective_read_states_why(monkeypatch) -> None:
    use(monkeypatch, PagedStudyClient(
        {("study_1", "objectives", None): {"data": [{"id": "obj_1"}],
                                           "paging": {"next": "https://graph.invalid/?after=x"}}},
        objects={"study_1": SPLIT_STUDY},
    ))
    summary = run(experiments.get_ad_study(study_id="study_1"))["summary"]
    assert summary["objectives_complete"] is False
    assert "no cursor" in summary["objectives_stop_reason"]


CREATIVE_STUDY = {**SPLIT_STUDY, "type": "SPLIT_TEST_V2"}
CREATIVE_CELLS = {"data": [
    {"id": "cell_a", "name": "Image", "treatment_percentage": 50, "ad_ids": ["ad_1"]},
    {"id": "cell_b", "name": "Video", "treatment_percentage": 50, "ad_ids": ["ad_2"]},
]}


def test_get_ad_study_creative_test_reads_ad_ids_and_maps_high_performers(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": None, "winner_confidence": None,
                           "high_performer_ids": ["ad_2"], "confidence_by_ad_object_id": {"ad_2": 0.8}})]
    client = use(monkeypatch, FakeStudyClient(
        objects={"study_1": CREATIVE_STUDY},
        edges={("study_1", "cells"): CREATIVE_CELLS,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "results": results}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    cell_call = next(call for call in client.calls if call[0] == "list" and call[2] == "cells")
    assert cell_call[3] == [*experiments.CELL_FIELDS, "ad_ids"]
    assert [cell["ad_ids"] for cell in result["cells"]] == [["ad_1"], ["ad_2"]]
    assert result["results"]["high_performer_ids"] == ["ad_2"]
    assert result["results"]["high_performer_cells"] == [
        {"ad_id": "ad_2", "cell_id": "cell_b", "cell_name": "Video"},
    ]


def test_get_ad_study_creative_test_falls_back_when_ad_ids_cannot_be_read(monkeypatch) -> None:
    class RejectAdIds(FakeStudyClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            if edge == "cells" and "ad_ids" in (fields or []):
                self.calls.append(("list", parent_id, edge, list(fields), params))
                raise MetaApiError(message="(#100) Tried accessing nonexisting field (ad_ids)", code=100)
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    client = use(monkeypatch, RejectAdIds(
        objects={"study_1": CREATIVE_STUDY},
        edges={("study_1", "cells"): {"data": [{"id": "cell_a", "name": "Image"}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1"))
    cell_calls = [call for call in client.calls if call[0] == "list" and call[2] == "cells"]
    assert [("ad_ids" in call[3]) for call in cell_calls] == [True, False]
    assert result["cells"][0]["id"] == "cell_a"
    assert "ad_ids" not in result["cells"][0]
    assert "ad_ids" in result["summary"]["ad_membership_note"]


def test_get_ad_study_non_creative_split_does_not_request_ad_ids(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": SPLIT_STUDY},
                                              edges={("study_1", "cells"): SPLIT_CELLS}))
    run(experiments.get_ad_study(study_id="study_1"))
    cell_call = next(call for call in client.calls if call[0] == "list" and call[2] == "cells")
    assert "ad_ids" not in cell_call[3]


@pytest.mark.parametrize("error", [
    MetaApiError(message="(#200) Permissions error", status_code=403, code=200),
    NotFoundError("Meta object or edge not found: act_123/impacting_ad_studies"),
])
def test_eligibility_account_count_error_is_not_performed_and_inconclusive(monkeypatch, error) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1")},
        errors={("act_123", "impacting_ad_studies"): error},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["entities"][0]["outcome"] == "passed"
    account = result["account"]
    assert account["performed"] is False
    assert account["active_studies"] is None
    assert account["count_complete"] is False
    assert account["unresolved"]
    assert result["precheck_outcome"] == "inconclusive"
    assert result["eligible_by_local_precheck"] is None
    assert any("not performed" in signal for signal in result["missing_signals"])


def test_eligibility_account_count_error_does_not_hide_a_blocker(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1", account="999")},
        errors={("act_123", "impacting_ad_studies"): MetaApiError(message="boom", code=1)},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["precheck_outcome"] == "failed"


def test_eligibility_account_count_performed_is_marked(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(objects={"c1": _campaign("c1")}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["account"]["performed"] is True
    assert result["precheck_outcome"] == "passed"


# --- round three (P4): true and false never become the number 1 (G12) -------------------


def _mcp_call(name, arguments):
    from meta_ads_mcp.coordinator import mcp_server

    return run(mcp_server.call_tool(name, arguments))


_CREATIVE_AB_ARGS = {
    "account_id": "10", "business_id": "30", "name": "Creative test",
    "cells": [{"name": "A", "treatment_percentage": 50, "ad_ids": ["40"]},
              {"name": "B", "treatment_percentage": 50, "ad_ids": ["41"]}],
    "start_time": "2099-01-01T00:00:00Z", "end_time": "2099-01-15T00:00:00Z",
}
_SPLIT_AB_ARGS = {
    "account_id": "10", "business_id": "30", "name": "Split test",
    "cells": [{"name": "A", "treatment_percentage": 50, "adset_ids": ["40"]},
              {"name": "B", "treatment_percentage": 50, "adset_ids": ["41"]}],
    "start_time": "2099-01-01T00:00:00Z", "end_time": "2099-01-15T00:00:00Z",
}
_LIFT_ARGS = {
    "account_id": "10", "business_id": "30", "name": "Lift",
    "cells": [{"name": "Test", "treatment_percentage": 90, "control_percentage": 10}],
    "objectives": [{"name": "Purchases", "adspixels": [{"id": "1", "event_names": ["fb_pixel_purchase"]}]}],
    "start_time": "2099-01-01T00:00:00Z", "end_time": "2099-01-15T00:00:00Z",
}


@pytest.mark.parametrize(("tool", "arguments"), [
    # The reviewer's case: true was sent as a daily budget of 100 cents.
    ("create_ab_test", {**_CREATIVE_AB_ARGS, "creative_test_daily_budget": True}),
    ("create_ab_test", {**_CREATIVE_AB_ARGS, "creative_test_daily_budget": False}),
    ("create_ab_test", {**_CREATIVE_AB_ARGS, "creative_test_lifetime_budget_percentage": True}),
    ("create_ab_test", {**_SPLIT_AB_ARGS, "confidence_level": True}),
    ("create_ab_test", {**_SPLIT_AB_ARGS, "start_time": True}),
    ("create_ab_test", {**_SPLIT_AB_ARGS, "end_time": True}),
    ("create_lift_study", {**_LIFT_ARGS, "start_time": True}),
    ("create_lift_study", {**_LIFT_ARGS, "end_time": True}),
    ("create_lift_study", {**_LIFT_ARGS, "observation_end_time": True}),
    ("update_ad_study", {"study_id": "50", "start_time": True}),
    ("update_ad_study", {"study_id": "50", "end_time": True}),
    ("list_ad_studies", {"account_id": "10", "limit": True}),
])
def test_g12_true_or_false_is_refused_at_the_mcp_boundary_before_any_request(monkeypatch, tool, arguments) -> None:
    """MCP reads true as 1 for number and time parameters; every one of them must refuse it with no request sent."""
    from fastmcp.exceptions import ToolError
    from fastmcp.exceptions import ValidationError as MCPValidationError

    client = use(monkeypatch, FakeStudyClient(objects={
        "act_10": {"id": "act_10", "currency": "USD", "business": {"id": "30"}},
        "50": {"id": "50", "start_time": "2099-01-01T00:00:00+0000", "end_time": "2099-01-15T00:00:00+0000"},
    }))
    with pytest.raises((ToolError, MCPValidationError)):
        _mcp_call(tool, arguments)
    assert client.calls == []


def test_g12_numbers_still_pass_the_mcp_boundary(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"act_10": {"id": "act_10", "currency": "USD"}}))
    _mcp_call("create_ab_test", {**_CREATIVE_AB_ARGS, "creative_test_daily_budget": 12.34})
    assert client.calls[-1][3]["creative_test_config"] == {"daily_budget": 1234}
    _mcp_call("create_ab_test", {**_SPLIT_AB_ARGS, "confidence_level": 80, "start_time": 4070908800,
                                 "end_time": 4072118400})
    payload = client.calls[-1][3]
    assert (payload["confidence_level"], payload["start_time"], payload["end_time"]) == (80, 4070908800, 4072118400)
    _mcp_call("list_ad_studies", {"account_id": "10", "limit": 5})
    assert client.calls[-1][4] == {"limit": 5}


# --- round three (P4): a page Meta repeats is never counted twice (G15) -----------------


def test_g15_study_counter_does_not_count_a_repeated_page_twice() -> None:
    """The reviewer's case: one page of 60 active studies served again for every cursor."""
    studies = [{"id": str(100 + index), "start_time": "2026-11-01T00:00:00Z", "end_time": "2026-12-01T00:00:00Z"}
               for index in range(60)]
    client = FakeStudyClient(edges={("act_10", "impacting_ad_studies"): {
        "data": studies, "paging": {"next": "next", "cursors": {"after": "repeat"}},
    }})
    result = run(experiments._count_account_active_studies(client, "act_10", datetime(2026, 11, 12, tzinfo=timezone.utc)))
    assert result["active_studies"] == 60
    assert result["count_complete"] is False
    assert "already been read" in result["stop_reason"]
    assert result["pages_read"] == 2


def test_g15_repeated_study_page_does_not_falsely_cross_the_concurrency_limit(monkeypatch) -> None:
    studies = [_study(str(100 + index), "SPLIT_TEST", *RUNNING) for index in range(60)]
    use(monkeypatch, FakeStudyClient(
        objects={"1": _campaign("1")},
        edges={("act_123", "impacting_ad_studies"): {
            "data": studies, "paging": {"next": "next", "cursors": {"after": "repeat"}},
        }},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["1"]))
    assert result["account"]["active_studies"] == 60
    assert result["account"]["blockers"] == []
    assert result["precheck_outcome"] == "inconclusive"


def test_g15_a_study_id_on_two_different_pages_is_counted_once() -> None:
    client = PagedStudyClient({
        ("act_10", "impacting_ad_studies", None): {
            "data": [_study("s1", "LIFT", *RUNNING), _study("s2", "LIFT", *RUNNING)],
            "paging": {"next": "https://graph.invalid/n", "cursors": {"after": "p2"}},
        },
        ("act_10", "impacting_ad_studies", "p2"): {"data": [_study("s2", "LIFT", *RUNNING),
                                                            _study("s3", "LIFT", *RUNNING)]},
    })
    result = run(experiments._count_account_active_studies(client, "act_10", NOW))
    assert result["active_studies"] == 3
    assert result["count_complete"] is True


def test_g15_entity_study_scan_stops_on_a_repeated_page(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"1": _campaign("1")},
        edges={("1", "ad_studies"): {"data": [_study("s1", "SPLIT_TEST", *RUNNING)],
                                     "paging": {"next": "next", "cursors": {"after": "again"}}}},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["1"]))
    entity = result["entities"][0]
    assert [study["id"] for study in entity["active_studies"]] == ["s1"]
    assert entity["active_studies_complete"] is False
    assert any("already been read" in message for message in entity["unresolved"])


def test_g15_study_cells_and_objectives_on_a_repeated_page_are_read_once(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={
            ("study_1", "cells"): {"data": [_cell("a", "as_1")],
                                   "paging": {"next": "next", "cursors": {"after": "again"}}},
            ("study_1", "objectives"): {"data": [{"id": "obj_1", "is_primary": True}],
                                        "paging": {"next": "next", "cursors": {"after": "again"}}},
        },
    ))
    result = run(experiments.get_ad_study(study_id="study_1"))
    assert [cell["id"] for cell in result["cells"]] == ["a"]
    assert [objective["id"] for objective in result["objectives"]] == ["obj_1"]
    summary = result["summary"]
    assert summary["cell_nodes_complete"] is False
    assert summary["objectives_complete"] is False
    assert "already been read" in summary["cells_stop_reason"]
    assert "already been read" in summary["objectives_stop_reason"]


# --- round three (P4): ad-level eligibility for creative tests --------------------------


def _ad(ad_id, adset_id="20", campaign_id="30", account="123", status="ACTIVE"):
    return {"id": ad_id, "name": f"Ad {ad_id}", "account_id": account, "adset_id": adset_id,
            "campaign_id": campaign_id, "effective_status": status}


def _adset(adset_id, campaign_id="30", account="123", status="ACTIVE"):
    return {"id": adset_id, "name": f"Ad set {adset_id}", "account_id": account, "campaign_id": campaign_id,
            "effective_status": status, "optimization_goal": "OFFSITE_CONVERSIONS", "billing_event": "IMPRESSIONS"}


def test_ad_level_eligibility_reads_each_ad_its_parents_and_their_studies(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={
        "41": _ad("41"), "42": _ad("42"), "20": _adset("20"), "30": _campaign("30"),
    }))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="ad", entity_ids=["41", "42"]))
    gets = [(call[1], call[2]) for call in client.calls if call[0] == "get"]
    # Shared parents are read once, with documented Ad, Ad Set and Campaign fields.
    assert gets == [
        ("41", ["id", "name", "account_id", "adset_id", "campaign_id", "effective_status"]),
        ("20", ["id", "name", "account_id", "campaign_id", "effective_status", "optimization_goal", "billing_event"]),
        ("30", ["id", "name", "account_id", "effective_status", "objective", "buying_type"]),
        ("42", ["id", "name", "account_id", "adset_id", "campaign_id", "effective_status"]),
    ]
    edges = [(call[1], call[2]) for call in client.calls if call[0] == "list"]
    # No ad_studies read on the ads themselves: Meta documents no such edge on ads.
    assert edges == [("20", "ad_studies"), ("30", "ad_studies"), ("act_123", "impacting_ad_studies")]
    assert result["precheck_outcome"] == "passed"
    entity = result["entities"][0]
    assert entity["level"] == "ad"
    assert (entity["adset_id"], entity["campaign_id"], entity["effective_status"]) == ("20", "30", "ACTIVE")
    assert entity["parents"]["adset"]["effective_status"] == "ACTIVE"
    assert entity["parents"]["campaign"]["objective"] == "OUTCOME_SALES"
    assert entity["active_studies"] == []
    assert entity["active_studies_complete"] is True
    assert entity["own_study_membership"].startswith("not_checked")
    assert any("ad_studies edge on ads" in item for item in result["not_checked"])
    assert any("2 to 7 cells" in rule["rule"] for rule in result["rules_applied"])


def test_ad_level_eligibility_settles_what_the_parents_show(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={
            "41": _ad("41", account="999"),
            "42": _ad("42", adset_id="21"),
            "21": _adset("21", status="ARCHIVED"),
            "30": _campaign("30"),
            "20": _adset("20"),
        },
        edges={("30", "ad_studies"): {"data": [_study("s9", "SPLIT_TEST", *RUNNING)]}},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="ad", entity_ids=["41", "42"]))
    first, second = result["entities"]
    assert first["outcome"] == "failed"
    assert any("999" in blocker for blocker in first["blockers"])
    assert second["outcome"] == "inconclusive"
    assert any("parent ad set 21" in message and "ARCHIVED" in message for message in second["unresolved"])
    assert any("parent campaign 30 is already in 1" in message for message in second["unresolved"])
    assert second["active_studies"] == [
        {"id": "s9", "name": "Study s9", "type": "SPLIT_TEST", "state": "running",
         "found_on": "campaign", "found_on_id": "30"},
    ]
    assert result["precheck_outcome"] == "failed"


def test_ad_level_eligibility_unreadable_parent_is_inconclusive(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"41": _ad("41"), "30": _campaign("30")},
        errors={"20": MetaApiError(message="(#200) Permissions error", code=200),
                ("20", "ad_studies"): MetaApiError(message="(#200) Permissions error", code=200)},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="ad", entity_ids=["41"]))
    entity = result["entities"][0]
    assert entity["outcome"] == "inconclusive"
    assert entity["parents"]["adset"]["read_error"]
    assert entity["active_studies_complete"] is False
    assert any("parent ad set 20" in signal for signal in result["missing_signals"])
    assert result["precheck_outcome"] == "inconclusive"


def test_ad_level_eligibility_missing_parent_ids_are_not_a_pass(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(objects={"41": {"id": "41", "account_id": "123", "effective_status": "ACTIVE"}}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="ad", entity_ids=["41"]))
    entity = result["entities"][0]
    assert entity["outcome"] == "inconclusive"
    assert entity["parents"] == {"adset": None, "campaign": None}
    assert entity["active_studies_complete"] is False


def test_ad_level_eligibility_warns_beyond_seven_ads(monkeypatch) -> None:
    ads = {str(40 + index): _ad(str(40 + index)) for index in range(8)}
    use(monkeypatch, FakeStudyClient(objects={**ads, "20": _adset("20"), "30": _campaign("30")}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="ad", entity_ids=list(ads)))
    assert any("2 to 7 cells" in warning for warning in result["account"]["warnings"])
    assert not any("at least 10" in warning for warning in result["account"]["warnings"])


def test_ad_level_eligibility_through_the_mcp_boundary(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(objects={"41": _ad("41"), "20": _adset("20"), "30": _campaign("30")}))
    result = _mcp_call("check_ab_test_eligibility", {"account_id": "123", "level": "ad", "entity_ids": "41"})
    assert result.structured_content["precheck_outcome"] == "passed"


# --- R13 (review R5): a study whose state cannot be read is never dropped --------------------

UNREADABLE_PAGES = [
    pytest.param({}, id="no-data"),
    pytest.param({"data": None}, id="null-data"),
    pytest.param({"data": "x"}, id="data-not-a-list"),
    pytest.param({"data": {"id": "30"}}, id="data-an-object"),
]
UNKNOWN_STATE_ROWS = [
    pytest.param({"id": "30", "name": "Unknown state", "type": "SPLIT_TEST"}, id="no-times"),
    pytest.param({"id": "30", "type": "SPLIT_TEST", "start_time": RUNNING[0]}, id="no-end"),
    pytest.param({"id": "30", "type": "SPLIT_TEST", "start_time": "soon", "end_time": "later"}, id="unparseable"),
    pytest.param(
        {"id": "30", "type": "SPLIT_TEST", "start_time": RUNNING[0], "end_time": RUNNING[1], "canceled_time": "?"},
        id="unreadable-cancel-time",
    ),
    pytest.param(None, id="not-an-object"),
    pytest.param("x", id="a-string"),
]


def test_r5_unclassifiable_study_rows_do_not_pass_eligibility(monkeypatch) -> None:
    class Client:
        async def get_object(self, object_id, **kwargs):
            return {"id": object_id, "account_id": "10", "effective_status": "ACTIVE"}

        async def list_objects(self, parent_id, edge, **kwargs):
            return {"data": [{"id": "30", "name": "Unknown state", "type": "SPLIT_TEST"}]}

    monkeypatch.setattr(experiments, "get_graph_api_client", Client)
    result = run(experiments.check_ab_test_eligibility("10", "campaign", ["20"]))
    assert result["precheck_outcome"] == "inconclusive", result
    assert result["account"]["count_complete"] is False


@pytest.mark.parametrize("row", UNKNOWN_STATE_ROWS)
def test_unclassified_study_on_the_entity_edge_makes_membership_inconclusive(monkeypatch, row) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1")}, edges={("c1", "ad_studies"): {"data": [row]}},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    entity = result["entities"][0]
    assert entity["outcome"] == "inconclusive"
    assert entity["active_studies"] == [] and entity["active_studies_complete"] is False
    assert entity["unclassified_studies"] == 1
    assert any("scheduled or running is unknown" in message for message in entity["unresolved"])
    assert any("containing the campaign at entity_ids entry 1" in signal for signal in result["missing_signals"])
    assert result["precheck_outcome"] == "inconclusive"
    assert result["eligible_by_local_precheck"] is None
    # The account edge read nothing unknown, so its own count stays complete.
    assert result["account"]["count_complete"] is True


@pytest.mark.parametrize("row", UNKNOWN_STATE_ROWS)
def test_unclassified_study_on_the_account_edge_leaves_the_count_incomplete(monkeypatch, row) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1")},
        edges={("act_123", "impacting_ad_studies"): {"data": [_study("s1", "LIFT", *RUNNING), row]}},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    account = result["account"]
    assert account["active_studies"] == 1 and account["unclassified_studies"] == 1
    assert account["count_complete"] is False and account["pages_complete"] is True
    assert account["blockers"] == []
    assert len(account["unresolved"]) == 1 and "may be too low" in account["unresolved"][0]
    assert "not confirmed" in account["unresolved"][0]
    assert result["precheck_outcome"] == "inconclusive"


def test_unclassified_studies_keep_a_definite_concurrency_blocker(monkeypatch) -> None:
    studies = [_study(str(100 + index), "SPLIT_TEST", *RUNNING) for index in range(100)]
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1")},
        edges={("act_123", "impacting_ad_studies"): {"data": [*studies, {"id": "999", "type": "LIFT"}]}},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["account"]["active_studies"] == 100
    assert result["account"]["count_complete"] is False
    assert len(result["account"]["blockers"]) == 1
    assert result["precheck_outcome"] == "failed"


def test_unclassified_studies_keep_a_definite_entity_blocker(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"c1": _campaign("c1", account="999")},
        edges={("c1", "ad_studies"): {"data": [{"id": "30"}]}},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["entities"][0]["outcome"] == "failed"
    assert result["precheck_outcome"] == "failed"


@pytest.mark.parametrize("payload", UNREADABLE_PAGES)
@pytest.mark.parametrize("where", ["entity", "account"])
def test_unreadable_study_page_is_never_an_empty_complete_read(monkeypatch, payload, where) -> None:
    edge = ("c1", "ad_studies") if where == "entity" else ("act_123", "impacting_ad_studies")
    use(monkeypatch, FakeStudyClient(objects={"c1": _campaign("c1")}, edges={edge: payload}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["precheck_outcome"] == "inconclusive"
    if where == "entity":
        entity = result["entities"][0]
        assert entity["active_studies_complete"] is False
        assert any("readable data list" in message for message in entity["unresolved"])
    else:
        assert result["account"]["count_complete"] is False
        assert "readable data list" in result["account"]["unresolved"][0]


def test_unreadable_second_study_page_leaves_the_count_incomplete() -> None:
    client = PagedStudyClient({
        ("act_10", "impacting_ad_studies", None): {
            "data": [_study("s1", "LIFT", *RUNNING)],
            "paging": {"next": "https://graph.invalid/next", "cursors": {"after": "p2"}},
        },
        ("act_10", "impacting_ad_studies", "p2"): {"data": None},
    })
    result = run(experiments._count_account_active_studies(client, "act_10", NOW))
    assert result["active_studies"] == 1
    assert result["count_complete"] is False and result["pages_complete"] is False
    assert "readable data list" in result["stop_reason"]


def test_ad_level_parent_with_unclassified_study_is_inconclusive(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"41": _ad("41"), "20": _adset("20"), "30": _campaign("30")},
        edges={("20", "ad_studies"): {"data": [{"id": "77", "type": "SPLIT_TEST"}]}},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="ad", entity_ids=["41"]))
    entity = result["entities"][0]
    assert entity["outcome"] == "inconclusive" and entity["active_studies_complete"] is False
    assert any("(77)" in message for message in entity["unresolved"])


@pytest.mark.parametrize("status", [None, "", 5])
def test_entity_without_a_readable_status_is_inconclusive(monkeypatch, status) -> None:
    campaign = _campaign("c1")
    campaign["effective_status"] = status
    use(monkeypatch, FakeStudyClient(objects={"c1": campaign}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["precheck_outcome"] == "inconclusive"
    assert any("effective_status" in message for message in result["entities"][0]["unresolved"])


@pytest.mark.parametrize("payload", UNREADABLE_PAGES)
def test_get_ad_study_unreadable_objectives_do_not_say_results_are_not_ready(monkeypatch, payload) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS, ("study_1", "objectives"): payload},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["summary"]["objectives_complete"] is False
    assert "readable data list" in result["summary"]["objectives_stop_reason"]
    assert result["results"]["status"] == "unknown"
    assert "not published" not in result["results"]["message"]


@pytest.mark.parametrize("payload", UNREADABLE_PAGES)
def test_get_ad_study_unreadable_cells_page_is_incomplete(monkeypatch, payload) -> None:
    results = [json.dumps({"winner_ad_object_id": "as_9", "winner_confidence": 0.9})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): payload,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "is_primary": True, "results": results}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["cells"] == []
    assert result["summary"]["cells_complete"] is False and result["summary"]["cell_nodes_complete"] is False
    assert "readable data list" in result["summary"]["cells_stop_reason"]
    winner = result["results"]["winner"]
    assert winner["cell_id"] is None and "not every cell could be read" in winner["cell_mapping_note"]


def test_get_ad_study_rows_that_are_not_objects_make_the_reads_incomplete(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): {"data": [*SPLIT_CELLS["data"], None]},
               ("study_1", "objectives"): {"data": [{"id": "obj_1"}, "x"]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    summary = result["summary"]
    assert summary["cell_nodes_complete"] is False and summary["cells_unreadable_rows"] == 1
    assert summary["objectives_complete"] is False and summary["objectives_unreadable_rows"] == 1
    assert "not objects" in summary["objectives_stop_reason"]
    assert result["results"]["status"] == "unknown"


def test_get_ad_study_unreadable_objective_row_is_not_taken_as_the_only_objective(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": "as_1", "winner_confidence": 0.9})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "results": results}, None]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["results"]["winner"] is None
    assert "not every objective was read" in result["results"]["message"]


@pytest.mark.parametrize("adsets", [{"paging": {}}, {"data": None}, "as_9", {"data": [{"name": "No id"}]}])
def test_get_ad_study_unreadable_cell_membership_is_incomplete(monkeypatch, adsets) -> None:
    results = [json.dumps({"winner_ad_object_id": "as_9", "winner_confidence": 0.9})]
    cells = {"data": [SPLIT_CELLS["data"][0], {"id": "cell_b", "name": "Group B", "treatment_percentage": 50,
                                               "adsets": adsets}]}
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): cells,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "is_primary": True, "results": results}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["cells"][1]["membership_unreadable"] == ["adset_ids"]
    summary = result["summary"]
    assert summary["memberships_complete"] is False and summary["cells_complete"] is False
    assert "membership_unreadable_note" in summary and "membership_note" not in summary
    winner = result["results"]["winner"]
    assert winner["cell_id"] is None and "could not be read" in winner["cell_mapping_note"]


def test_get_ad_study_without_a_type_does_not_guess_the_result_kind(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": "as_1", "winner_confidence": 0.9})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": {key: value for key, value in SPLIT_STUDY.items() if key != "type"}},
        edges={("study_1", "cells"): SPLIT_CELLS,
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "results": results}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["results"]["kind"] == "unknown" and result["results"]["raw_results"]


@pytest.mark.parametrize("payload", UNREADABLE_PAGES)
def test_list_ad_studies_unreadable_page_is_not_complete(monkeypatch, payload) -> None:
    use(monkeypatch, FakeStudyClient(edges={("act_123", "impacting_ad_studies"): payload}))
    result = run(experiments.list_ad_studies(account_id="123"))
    assert result["items"] == []
    assert result["summary"]["complete"] is False and result["summary"]["next_after"] is None
    assert result["summary"]["stop_reason"] == collection_fault(payload)


def test_list_ad_studies_shows_rows_that_are_not_objects_without_failing(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(edges={
        ("act_123", "impacting_ad_studies"): {"data": [_study("s1", "SPLIT_TEST", *RUNNING), None]},
    }))
    result = run(experiments.list_ad_studies(account_id="123"))
    assert result["items"][0]["state"] == "running" and result["items"][1] is None
    filtered = run(experiments.list_ad_studies(account_id="123", study_type="SPLIT_TEST"))
    assert [item["id"] for item in filtered["items"]] == ["s1"]
    assert filtered["summary"]["study_type_filter"]["unreadable_rows_removed"] == 1


def test_cancel_study_with_unreadable_cancel_time_sends_nothing(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": {**RUNNING_STUDY, "canceled_time": "?"}}))
    result = run(experiments.cancel_ad_study(study_id="study_1"))
    assert result["ok"] is False and client.kinds() == ["get"]
    assert "cancel time could not be read" in result["message"]


@pytest.mark.parametrize(("stored", "change"), [
    ({**SCHEDULED_STUDY, "start_time": None}, {"start_time": FUTURE_START}),
    ({**RUNNING_STUDY, "end_time": None}, {"end_time": FUTURE_END}),
    ({**RUNNING_STUDY, "canceled_time": "?"}, {"end_time": FUTURE_END}),
])
def test_update_study_refuses_a_time_change_when_the_state_cannot_be_read(monkeypatch, stored, change) -> None:
    previous = stored
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": previous}))
    with pytest.raises(experiments.ValidationError, match="could not be read"):
        run(experiments.update_ad_study(study_id="study_1", **change))
    assert client.kinds() == ["get"]


# --- R14 (review S4): caller text never reaches an error or a logged exception chain ----------


def _routed_call(name, arguments, routed):
    import meta_ads_mcp.server  # noqa: F401 -- register the complete real server
    from meta_ads_mcp.coordinator import mcp_server

    if routed:
        name, arguments = "call_tool", {"name": name, "arguments": arguments}
    return run(mcp_server.call_tool(name, arguments))


def _private_marker() -> str:
    return "".join(["Synthetic", "Private", "Value", "_", "x7y9"])


def _lift_args() -> dict:
    return {
        "account_id": "10", "name": "Example", "business_id": "30",
        "cells": [{"name": "Cell", "treatment_percentage": 90, "control_percentage": 10}],
        "objectives": [{"name": "Purchase", "adspixels": [{"id": "20", "event_names": ["Purchase"]}]}],
        "start_time": "2027-01-01T00:00:00Z", "end_time": "2027-01-05T00:00:00Z",
    }


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "proxy"])
@pytest.mark.parametrize("where", ["cell_key", "objective_key", "time"])
def test_study_validation_never_logs_or_returns_private_input(monkeypatch, caplog, routed, where) -> None:
    from fastmcp.exceptions import ToolError

    caplog.set_level(logging.DEBUG)
    private = _private_marker()
    args = _lift_args()
    if where == "cell_key":
        args["cells"][0][private] = "unexpected"
    elif where == "objective_key":
        args["objectives"][0][private] = "unexpected"
    else:
        args["start_time"] = private
    with pytest.raises(ToolError) as caught:
        _routed_call("create_lift_study", args, routed)
    leaks = {"mcp_error": private in str(caught.value), "logs": private in caplog.text}
    assert not any(leaks.values()), leaks


@pytest.mark.parametrize("routed", [False, True], ids=["direct", "proxy"])
@pytest.mark.parametrize("case", [
    "ab_cell_key", "ab_duplicate_id", "creative_duplicate_id", "ab_end_time", "update_start_time",
    "eligibility_unknown_level",
])
def test_other_study_validation_never_logs_or_returns_private_input(monkeypatch, caplog, routed, case) -> None:
    from fastmcp.exceptions import ToolError

    caplog.set_level(logging.DEBUG)
    use(monkeypatch, FakeStudyClient(objects={"study_1": SCHEDULED_STUDY}))
    private = _private_marker()
    ab = {"account_id": "10", "business_id": "30", "name": "Test",
          "start_time": FUTURE_START, "end_time": FUTURE_END,
          "cells": [{"name": "A", "treatment_percentage": 50, "adset_ids": ["1"]},
                    {"name": "B", "treatment_percentage": 50, "adset_ids": ["2"]}]}
    if case == "ab_cell_key":
        ab["cells"][1][private] = "x"
        name, args = "create_ab_test", ab
    elif case == "ab_duplicate_id":
        ab["cells"][0]["adset_ids"] = [private]
        ab["cells"][1]["adset_ids"] = [private]
        name, args = "create_ab_test", ab
    elif case == "creative_duplicate_id":
        ab["cells"] = [{"name": "A", "treatment_percentage": 50, "ad_ids": [private]},
                       {"name": "B", "treatment_percentage": 50, "ad_ids": [private]}]
        ab["creative_test_daily_budget"] = 10
        name, args = "create_ab_test", ab
    elif case == "ab_end_time":
        ab["end_time"] = private
        name, args = "create_ab_test", ab
    elif case == "update_start_time":
        name, args = "update_ad_study", {"study_id": "study_1", "start_time": private}
    else:
        name, args = "check_ab_test_eligibility", {"account_id": "10", "level": private, "entity_ids": ["1"]}
    with pytest.raises(ToolError) as caught:
        _routed_call(name, args, routed)
    leaks = {"mcp_error": private in str(caught.value), "logs": private in caplog.text}
    assert not any(leaks.values()), leaks


def test_unknown_keys_are_counted_and_the_accepted_keys_listed(monkeypatch) -> None:
    no_client(monkeypatch)
    args = _lift_args()
    args["cells"][0].update({"first": 1, "second": 2})
    with pytest.raises(experiments.ValidationError) as error:
        run(experiments.create_lift_study(**args))
    message = str(error.value)
    assert "cells[0] has 2 unsupported keys" in message and "first" not in message and "second" not in message
    assert "treatment_percentage" in message


def test_bad_study_time_error_has_no_exception_chain(monkeypatch) -> None:
    no_client(monkeypatch)
    args = _lift_args()
    args["start_time"] = _private_marker()
    with pytest.raises(experiments.ValidationError) as error:
        run(experiments.create_lift_study(**args))
    assert error.value.__cause__ is None and error.value.__context__ is None


# --- R14 (review S5): an entry or a type that cannot be read never yields a complete list ---------


@pytest.mark.parametrize("study_type", [None, "LIFT"])
def test_list_ad_studies_with_an_entry_that_is_not_an_object_is_incomplete(monkeypatch, study_type) -> None:
    payload = {"data": [_study("s1", "LIFT", *RUNNING), None]}
    use(monkeypatch, FakeStudyClient(edges={("act_123", "impacting_ad_studies"): payload}))
    result = run(experiments.list_ad_studies(account_id="123", study_type=study_type))
    assert result["items"][0]["id"] == "s1"
    assert result["summary"]["complete"] is False
    assert collection_fault(payload) in result["summary"]["stop_reason"]


@pytest.mark.parametrize("row", [
    {"id": "20", "name": "No type"}, {"id": "20", "type": None}, {"id": "20", "type": 5}, {"id": "20", "type": " "},
])
def test_list_ad_studies_row_without_a_type_is_not_a_definite_nonmatch(monkeypatch, row) -> None:
    use(monkeypatch, FakeStudyClient(edges={
        ("act_123", "impacting_ad_studies"): {"data": [row, _study("s2", "SPLIT_TEST", *RUNNING)]},
    }))
    result = run(experiments.list_ad_studies(account_id="123", study_type="LIFT"))
    assert result["items"] == []
    summary = result["summary"]
    assert summary["complete"] is False and summary["unjudged_rows"] == 1
    assert summary["study_type_filter"]["removed_on_this_page"] == 1
    assert summary["study_type_filter"]["unjudged_rows"] == 1
    assert "readable study type" in summary["stop_reason"]


def test_list_ad_studies_definite_type_mismatch_keeps_the_page_complete(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(edges={
        ("act_123", "impacting_ad_studies"): {"data": [_study("s1", "lift", *RUNNING), _study("s2", "SPLIT_TEST", *RUNNING)]},
    }))
    result = run(experiments.list_ad_studies(account_id="123", study_type="LIFT"))
    assert [item["id"] for item in result["items"]] == ["s1"]
    assert result["summary"]["complete"] is True and result["summary"]["stop_reason"] is None


# --- R15 (review U3 sweep): copies of one id that disagree are unknown, never first-wins ----------

ENDED = ("2026-10-01T00:00:00+0000", "2026-10-15T00:00:00+0000")


@pytest.mark.parametrize("same_page", [True, False])
@pytest.mark.parametrize("where", ["entity", "account"])
def test_study_copies_with_different_states_are_unclassified(monkeypatch, same_page, where) -> None:
    edge = ("c1", "ad_studies") if where == "entity" else ("act_123", "impacting_ad_studies")
    first, second = _study("s9", "SPLIT_TEST", *ENDED), _study("s9", "SPLIT_TEST", *RUNNING)
    if same_page:
        pages = {(*edge, None): {"data": [first, second]}}
    else:
        pages = {
            (*edge, None): {"data": [first], "paging": {"next": "https://graph.invalid/n", "cursors": {"after": "p2"}}},
            (*edge, "p2"): {"data": [second]},
        }
    use(monkeypatch, PagedStudyClient(pages, objects={"c1": _campaign("c1")}))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=["c1"]))
    assert result["precheck_outcome"] == "inconclusive"
    if where == "entity":
        entity = result["entities"][0]
        assert entity["unclassified_studies"] == 1 and entity["active_studies_complete"] is False
        assert any("(s9)" in message and "disagree" in message for message in entity["unresolved"])
    else:
        assert result["account"]["unclassified_studies"] == 1 and result["account"]["count_complete"] is False


def test_identical_study_copies_are_counted_once() -> None:
    study = _study("s1", "LIFT", *RUNNING)
    client = PagedStudyClient({("act_10", "impacting_ad_studies", None): {"data": [study, dict(study)]}})
    result = run(experiments._count_account_active_studies(client, "act_10", NOW))
    assert result["active_studies"] == 1 and result["count_complete"] is True


def test_study_copies_differing_only_in_name_agree_on_state() -> None:
    client = PagedStudyClient({("act_10", "impacting_ad_studies", None): {"data": [
        _study("s1", "LIFT", *RUNNING), {**_study("s1", "LIFT", *RUNNING), "name": "Renamed"},
    ]}})
    result = run(experiments._count_account_active_studies(client, "act_10", NOW))
    assert result["active_studies"] == 1 and result["count_complete"] is True


def test_get_ad_study_objective_copies_that_disagree_do_not_decide_the_winner(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": "as_1", "winner_confidence": 0.9})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): SPLIT_CELLS, ("study_1", "objectives"): {"data": [
            {"id": "obj_1", "is_primary": True, "results": results},
            {"id": "obj_1", "is_primary": False, "results": results},
        ]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert result["summary"]["objectives_complete"] is False
    assert "obj_1" in result["summary"]["objectives_stop_reason"]
    assert result["results"]["status"] == "unknown"


def test_get_ad_study_cell_copies_that_disagree_leave_the_winner_unmapped(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": "as_1", "winner_confidence": 0.9})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): {"data": [_cell("cell_a", "as_1"), _cell("cell_a", "as_9"), _cell("cell_b", "as_2")]},
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "is_primary": True, "results": results}]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1", include_results=True))
    assert [cell["id"] for cell in result["cells"]] == ["cell_b"]
    assert result["summary"]["cell_nodes_complete"] is False and "cell_a" in result["summary"]["cells_stop_reason"]
    winner = result["results"]["winner"]
    assert winner["cell_id"] is None and "not every cell could be read" in winner["cell_mapping_note"]


def test_get_ad_study_identical_cell_copies_are_kept_once(monkeypatch) -> None:
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): {"data": [*SPLIT_CELLS["data"], SPLIT_CELLS["data"][0]]}},
    ))
    result = run(experiments.get_ad_study(study_id="study_1"))
    assert [cell["id"] for cell in result["cells"]] == ["cell_a", "cell_b"]
    assert result["summary"]["cell_nodes_complete"] is True


def test_winner_in_two_cells_is_not_mapped_to_the_first(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": "as_1", "winner_confidence": 0.9})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": SPLIT_STUDY},
        edges={("study_1", "cells"): {"data": [_cell("cell_a", "as_1"), _cell("cell_b", "as_1")]},
               ("study_1", "objectives"): {"data": [{"id": "obj_1", "is_primary": True, "results": results}]}},
    ))
    winner = run(experiments.get_ad_study(study_id="study_1", include_results=True))["results"]["winner"]
    assert winner["cell_id"] is None and "2 cells" in winner["cell_mapping_note"]


def test_high_performer_in_two_cells_is_not_mapped_to_the_first(monkeypatch) -> None:
    results = [json.dumps({"winner_ad_object_id": None, "high_performer_ids": ["ad_1"]})]
    use(monkeypatch, FakeStudyClient(
        objects={"study_1": {**SPLIT_STUDY, "type": "SPLIT_TEST_V2"}},
        edges={("study_1", "cells"): {"data": [
            {"id": "cell_a", "name": "A", "treatment_percentage": 50, "ad_ids": ["ad_1"]},
            {"id": "cell_b", "name": "B", "treatment_percentage": 50, "ad_ids": ["ad_1"]},
        ]}, ("study_1", "objectives"): {"data": [{"id": "obj_1", "is_primary": True, "results": results}]}},
    ))
    outcome = run(experiments.get_ad_study(study_id="study_1", include_results=True))["results"]
    assert outcome["high_performer_cells"] == [{"ad_id": "ad_1", "cell_id": None, "cell_name": None}]
    assert outcome["high_performer_ambiguous_ids"] == ["ad_1"]


# --- R16 (review V4): no message repeats a value the caller supplied, of any type -----------------
#
# Rule: an error, warning or note names the field, its position and what is accepted; it never
# repeats a value the caller supplied (text, numbers, dates, booleans, ids the caller typed).
# It may show ids and names Meta returned, values from a fixed list of accepted options, the
# tool's own limits, positions and counts. Ids: a caller-typed id is never repeated in a message,
# even after Meta answered for it; it stays in the structured fields (target, scope, entity_id,
# request paths). An id read from Meta's answer (a parent, a row, a study) may be named.


@pytest.mark.parametrize("tool_name", ["create_ab_test", "create_lift_study"])
@pytest.mark.parametrize("routed", [False, True], ids=["direct-mcp", "proxy-mcp"])
def test_misplaced_numeric_personal_value_is_not_quoted_by_percentage_error(caplog, tool_name, routed) -> None:
    from fastmcp.exceptions import ToolError

    caplog.set_level(logging.DEBUG)
    # Fictional NANP number, assembled at runtime, standing for a personal number in the wrong field.
    private_value = int("202555" + "0198")
    arguments = {
        "account_id": "10", "name": "Review",
        "cells": [{"name": "A", "treatment_percentage": private_value, "campaign_ids": ["20"]},
                  {"name": "B", "treatment_percentage": 50, "campaign_ids": ["21"]}],
        "start_time": "2027-01-01T00:00:00Z", "end_time": "2027-01-08T00:00:00Z",
    }
    if tool_name == "create_lift_study":
        arguments["cells"] = [{"name": "A", "treatment_percentage": private_value, "control_percentage": 10}]
        arguments["objectives"] = [{"name": "Purchases", "type": "CONVERSIONS", "is_primary": True,
                                    "adspixels": [{"id": "30", "event_names": ["Purchase"]}]}]
    with pytest.raises(ToolError) as caught:
        _routed_call(tool_name, arguments, routed)
    assert str(private_value) not in str(caught.value)
    assert str(private_value) not in caplog.text
    assert "cells[0].treatment_percentage must be" in str(caught.value)


@pytest.mark.parametrize(("cells", "match"), [
    ([{"name": "A", "treatment_percentage": 33.333, "campaign_ids": ["20"]},
      {"name": "B", "treatment_percentage": 50, "campaign_ids": ["21"]}], "at most two decimal places"),
    ([{"name": "A", "treatment_percentage": 47.5, "campaign_ids": ["20"]},
      {"name": "B", "treatment_percentage": 60, "campaign_ids": ["21"]}],
     "can total at most 100; these cells total more"),
])
def test_ab_cell_errors_name_the_rule_not_the_numbers(monkeypatch, cells, match) -> None:
    no_client(monkeypatch)
    with pytest.raises(experiments.ValidationError, match=match) as error:
        run(_ab(cells=cells))
    assert "33.333" not in str(error.value) and "107.5" not in str(error.value)
    assert "47.5" not in str(error.value)


@pytest.mark.parametrize(("cells", "match"), [
    ([{"name": "A", "treatment_percentage": 60, "control_percentage": 50}], "cannot exceed 100"),
    ([{"name": "A", "treatment_percentage": 60, "control_percentage": 30}], "must equal 100"),
])
def test_lift_cell_errors_name_the_rule_not_the_numbers(monkeypatch, cells, match) -> None:
    no_client(monkeypatch)
    args = {**_lift_args(), "cells": cells}
    with pytest.raises(experiments.ValidationError, match=match) as error:
        run(experiments.create_lift_study(**args))
    assert "110" not in str(error.value) and "90" not in str(error.value)


def test_study_time_errors_do_not_repeat_the_times(monkeypatch) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"study_1": SCHEDULED_STUDY}))
    with pytest.raises(experiments.ValidationError, match="end_time would not be after its start_time") as error:
        run(experiments.update_ad_study(study_id="study_1", end_time="2026-11-20T00:00:00Z"))
    assert "2026-11-20" not in str(error.value) and "2026-12-01" not in str(error.value)
    assert client.kinds() == ["get"]


def test_eligibility_messages_name_entities_by_position_not_by_typed_id(monkeypatch) -> None:
    typed = "".join(["2025", "550198"])
    use(monkeypatch, FakeStudyClient(
        objects={typed: _campaign(typed, account="999")},
        errors={(typed, "ad_studies"): MetaApiError(message="(#100) Unsupported", code=100)},
    ))
    result = run(experiments.check_ab_test_eligibility(account_id="123", level="campaign", entity_ids=[typed]))
    messages = [finding["message"] for finding in result["findings"]] + result["missing_signals"]
    assert messages and not any(typed in message for message in messages)
    assert any("campaign at entity_ids entry 1" in message for message in messages)
    assert any("belongs to ad account 999" in message for message in messages), "Meta's own value is still shown"
    assert result["entities"][0]["entity_id"] == typed, "the structured field keeps the id"


# Every message f-string (one whose literal text has a space) in these modules may interpolate only
# constants, counts from len(), plural endings, and the expressions audited below for each function.
# Each audited expression is a position, a count, a fixed option, the tool's own limit, a reason built
# from those, or a value or id read from Meta's answer. Adding an interpolation of anything else (a
# parameter, a caller-typed id, a number the caller sent) fails this test until it is audited here.
_MESSAGE_GUARD_MODULES = ["business_access", "experiments", "datasets", "assets", "audiences"]
_AUDITED_INTERPOLATIONS: dict[str, dict[str, set[str]]] = {
    "assets": {
        "_media_filter": {"', '.join(sorted(allowed))", "name"},  # name: the parameter's own name
        "_require_id": {"name"},
        "_scan_note": {"examined", "noun", "rows_note", "scan.get('read_error')", "scan['pages_read']",
                       "scan['pages_read'] + 1", "unreadable"},
        "_unjudged_reason": {"count", "needs"},
        "_validate_limit": {"maximum"},
        "get_field_reference": {"normalized_level"},  # one of campaign, adset, ad
        "list_audience_adsets": {"ads_without_adset", "unreadable_targeting"},
        "list_creative_ads": {"without_creative_id"},
    },
    "audiences": {
        "_audience_labels": {"unknown"},
        "_audience_schema": {"', '.join(unsupported)"},  # positions
        "_hash_rows": {"key", "row_number"},  # key: a supported schema key
        "_parse_customer_csv": {"', '.join(missing)", "lines_read + 1"},  # missing: supported schema keys
        # The upload's progress: the tool name, counts, the locally chosen session id and a fixed verb.
        "_upload_progress": {"accepted", "action", "batch_seq", "rows_accepted", "session_id", "total_batches", "verb"},
        "_sanitized_upload_error": {"progress", "type(exc).__name__", "withheld"},
        # The audience or session asked for and the id Meta's answer named (a plain id: names_other_object
        # read it); what is "audience" or "upload session".
        "_other_identity_error": {"asked", "batch_seq", "progress", "str(answered_id).strip()", "what"},
    },
    "business_access": {
        "_lookup_identity": {"copies", "problems[0]", "scan['reason']", "task_field", "unidentified"},
        "_non_object_rows_reason": {"count"},
        "_normalize_tasks": {"accepted", "unknown"},
        "_numeric_id": {"name"},
        "_partner_error": {"error.message"},
        "_partner_item": {"reason"},
        "_plan_access_change": {"found['reason']", "task", "what"},
        "_plan_partner_change": {"found['reason']", "task", "what"},
        "_scan": {"max_pages"},
        "_search_response": {"' or '.join(text_fields)", "needs", "scan['reason']", "text_fields[0]", "unsearchable"},
        "_tasks_state": {"field", "unknown"},
        "list_catalog_assigned_users": {"found['reason']", "problem"},
        "list_catalog_partners": {"found['reason']"},
        # catalog_id and unreadable are ids from Meta's owned_product_catalogs answer.
        "list_user_assigned_catalogs": {"', '.join(unreadable)", "catalog_id", "owned['reason']",
                                        "partners['reason']", "unidentified", "unidentified_catalogs"},
        # after_tasks and the proposed tasks are task names from the fixed list.
        "set_catalog_access": {"after['reason']", "after_tasks or 'none'", "detail",
                               "plan['proposed']['tasks'] or 'none'"},
    },
    "datasets": {
        "_offline_quality_response": {"event['composite_score']", "name"},  # Meta's event name and score
        "_parse_day": {"name"},
        "_quality_rows": {"bad", "key"},
        "_required_id": {"name"},
        "get_dataset_quality": {"event['emq_score']", "event['event_coverage_goal_percentage']",
                                "event['event_coverage_percentage']", "name"},
        "get_dataset_stats": {"' and '.join(unread)", "conflict_reason", "mixed_reason", "pages", "stop_reason",
                              "uncounted_entries", "unread_reason", "unreadable_counts", "unreadable_hours"},
    },
    "experiments": {
        "_build_ab_cells": {"index", "owners[entity_id]"},  # positions
        "_build_creative_cells": {"index", "owners[ad_id]"},
        "_build_lift_cells": {"index"},
        "_build_objectives": {"index", "key"},  # key: a documented objective source key
        # subject names a parent by the id Meta returned on the ad; the rest is Meta's answer.
        "_check_account_and_status": {"' for ' + subject if subject else ''", "'The ' + subject + ' has ' if subject else ''",
                                      "'The ' + subject if subject else 'It'", "entity_account", "status"},
        "_check_ad_parents": {"id_field", "noun", "parent_id", "read_error", "subject"},
        "_check_cell_keys": {"index"},
        "_conflicting_ids_reason": {"', '.join(ids)"},  # study, cell or objective ids from Meta
        "_id_list": {"label"},
        "_listing_page": {"needs", "unjudged"},
        "_non_object_rows_reason": {"count"},
        "_note_studies": {"', '.join((str(study['id']) for study in active))",
                          "_unclassified_message(scan, f'containing {subject}')", "error", "lead",
                          "scan['pages_read']", "scan['stop_reason']", "subject"},
        "_percentage": {"label", "lower", "minimum"},  # minimum: this tool's own lower limit
        "_read_cell_breakdowns": {"exc"},
        "_read_edge_pages": {"max_pages"},
        "_scan_active_studies": {"max_pages", "stop_at"},
        "_split_results": {"basis", "deciding.get('id')"},
        "_to_unix": {"field_name"},
        "_unclassified_message": {"', '.join(ids)", "named", "scan['unclassified']", "where"},
        "_unknown_keys_error": {"', '.join(sorted(accepted))", "count", "where"},
        "cancel_ad_study": {"state", "stored_end.isoformat()"},  # the end time Meta holds
        "check_ab_test_eligibility": {"' '.join(gaps)", "100 // MIN_SPLIT_TREATMENT_PERCENTAGE",
                                      "account_check['active_studies']", "account_check['pages_read']",
                                      "account_check['stop_reason']", "account_check['unclassified_note']",
                                      "exc", "position", "where"},
        "get_ad_study": {"exc", "objectives_read['stop_reason']", "state", "study.get('results_first_available_date')"},
        "list_ad_studies": {"bad_rows"},
        "update_ad_study": {"state"},
    },
}


def _message_interpolations(module_name: str) -> dict[str, set[str]]:
    import importlib
    from pathlib import Path

    module = importlib.import_module(f"meta_ads_mcp.tools.{module_name}")
    return _message_interpolations_in(Path(module.__file__).read_text(encoding="utf-8"))


def _message_interpolations_in(source: str) -> dict[str, set[str]]:
    import ast

    def constant(expr) -> bool:
        if isinstance(expr, ast.Constant):
            return True
        if isinstance(expr, ast.Name):
            return expr.id.lstrip("_").isupper()
        if isinstance(expr, ast.Subscript):
            return constant(expr.value)
        if isinstance(expr, ast.Call):
            name = getattr(expr.func, "id", None) or getattr(expr.func, "attr", None)
            if name == "len":
                return True
            if name in {"sorted", "list", "join"}:
                return all(constant(arg) for arg in expr.args)
        if isinstance(expr, ast.IfExp):
            return constant(expr.body) and constant(expr.orelse)
        return False

    tree = ast.parse(source)
    found: dict[str, set[str]] = {}
    for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(function):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(
                part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if " " not in literal:
                continue  # a key, a Graph path or a sort value, not a message
            for part in node.values:
                if isinstance(part, ast.FormattedValue) and not constant(part.value):
                    found.setdefault(function.name, set()).add(ast.unparse(part.value))
    return found


@pytest.mark.parametrize("module_name", _MESSAGE_GUARD_MODULES)
def test_messages_interpolate_only_audited_values(module_name) -> None:
    found = _message_interpolations(module_name)
    audited = _AUDITED_INTERPOLATIONS[module_name]
    unaudited = {
        f"{function}: {expression}"
        for function, expressions in found.items() for expression in expressions
        if expression not in audited.get(function, set())
    }
    assert not unaudited, sorted(unaudited)
    stale = {
        f"{function}: {expression}"
        for function, expressions in audited.items() for expression in expressions
        if expression not in found.get(function, set())
    }
    assert not stale, f"audited interpolations no longer in the code: {sorted(stale)}"


@pytest.mark.parametrize("module_name", _MESSAGE_GUARD_MODULES)
def test_validation_errors_are_never_raised_inside_an_except_block(module_name) -> None:
    import ast
    import importlib
    from pathlib import Path

    module = importlib.import_module(f"meta_ads_mcp.tools.{module_name}")
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    problems = [
        f"line {node.lineno}"
        for handler in ast.walk(tree) if isinstance(handler, ast.ExceptHandler)
        for node in ast.walk(handler)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
        and getattr(node.exc.func, "id", None) == "ValidationError"
    ]
    assert problems == [], problems


def test_message_guard_catches_an_interpolated_parameter() -> None:
    """The guard itself must be able to fail: the reported line, and a typed id in a warning."""
    source = (
        'def _percentage(value, *, label):\n'
        '    raise ValidationError(f"{label} must be at most 100; got {value}.")\n'
        'def _plan(user_id, warnings):\n'
        '    warnings.append(f"MANAGE gives user {user_id} full control.")\n'
        '    path = f"{user_id}/assigned_users"\n'
        '    raise ValidationError(f"limit must be at most {MAX_LIMIT}; {len(warnings)} given.")\n'
    )
    assert _message_interpolations_in(source) == {"_percentage": {"label", "value"}, "_plan": {"user_id"}}


# --- round thirty-three (review R33, EE3): reads naming a different object ---------------
# The account read that infers the owning business, and the study read that decides between
# DELETE and moving the end time, must not act on an answer about a different object. The
# reviewer's cases go through the real Graph client; the controls show ordinary answers (the
# same id as text, as a number, with or without act_, or no id) behave as before.


@pytest.fixture
def r33_real_graph(monkeypatch):
    from contextlib import asynccontextmanager

    import httpx

    from meta_ads_mcp.config import get_settings
    from meta_ads_mcp.graph_api import GraphAPIClient

    @asynccontextmanager
    async def real_graph(handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = GraphAPIClient(get_settings())
            monkeypatch.setattr(GraphAPIClient, "_get_shared_client", lambda self, **kwargs: http)
            monkeypatch.setattr(experiments, "get_graph_api_client", lambda: client)
            yield client

    return real_graph


@pytest.mark.parametrize("kind", ["split", "lift"])
@pytest.mark.asyncio
async def test_ee3_study_creation_does_not_use_another_accounts_owning_business(monkeypatch, r33_real_graph, kind):
    import httpx

    writes = []
    monkeypatch.setattr(experiments, "_now", lambda: datetime(2026, 11, 14, tzinfo=timezone.utc))

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": "act_953999", "business": {"id": "953798"}})
        writes.append(request.url.path)
        return httpx.Response(200, json={"id": "953799"})

    async with r33_real_graph(handler):
        common = dict(account_id="953000", name="QA study",
                      start_time="2027-01-01T00:00:00Z", end_time="2027-01-08T00:00:00Z")
        with pytest.raises(experiments.ValidationError, match="different ad account.*business_id"):
            if kind == "split":
                await experiments.create_ab_test(**common, cells=[
                    {"name": "A", "treatment_percentage": 50, "campaign_ids": ["953701"]},
                    {"name": "B", "treatment_percentage": 50, "campaign_ids": ["953702"]},
                ])
            else:
                await experiments.create_lift_study(
                    **common,
                    cells=[{"name": "test", "treatment_percentage": 80, "control_percentage": 20}],
                    objectives=[{"name": "purchases", "adspixels": [{"id": "953721", "event_names": ["Purchase"]}]}],
                )
    assert not writes, writes


@pytest.mark.asyncio
async def test_ee3_cancel_does_not_choose_delete_using_another_studys_schedule(monkeypatch, r33_real_graph):
    import httpx

    writes = []
    monkeypatch.setattr(experiments, "_now", lambda: datetime(2026, 11, 14, tzinfo=timezone.utc))

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={
                "id": "953499", "start_time": "2027-01-01T00:00:00+0000", "end_time": "2027-01-08T00:00:00+0000",
            })
        writes.append((request.method, request.url.path))
        return httpx.Response(200, json={"success": True})

    async with r33_real_graph(handler):
        with pytest.raises(MetaApiError, match="different study.*Nothing was sent"):
            await experiments.cancel_ad_study(study_id="953400")
    assert not writes, writes


@pytest.mark.parametrize("tool", ["ab", "lift", "ab-dry-run"])
def test_owning_business_from_an_answer_about_another_account_is_not_used(monkeypatch, tool) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={
        "act_123": {"id": "act_999", "business": {"id": "biz_77"}},
    }))
    with pytest.raises(experiments.ValidationError, match="different ad account.*pass business_id"):
        if tool == "lift":
            run(_lift(business_id=None))
        else:
            run(_ab(business_id=None, dry_run=tool == "ab-dry-run"))
    assert "create" not in client.kinds()


@pytest.mark.parametrize("named", ["act_123", "123", 123, " act_123 ", None, "me"], ids=[
    "act-text", "bare-text", "number", "padded", "no-id", "alias",
])
@pytest.mark.parametrize("tool", ["ab", "lift"])
def test_owning_business_from_an_answer_about_the_same_account_is_used_as_before(monkeypatch, tool, named) -> None:
    answer = {"business": {"id": "biz_77"}}
    if named is not None:
        answer["id"] = named
    client = use(monkeypatch, FakeStudyClient(objects={"act_123": answer}))
    run(_lift(business_id=None) if tool == "lift" else _ab(business_id=None))
    assert client.calls[-1][:3] == ("create", "biz_77", "ad_studies")


def test_a_given_business_id_needs_no_account_read(monkeypatch) -> None:
    """Control: with business_id the account is not read, so no answer can be compared."""
    client = use(monkeypatch, FakeStudyClient(objects={"act_123": {"id": "act_999"}}))
    run(_ab(business_id="biz_1"))
    assert client.kinds() == ["create"]


SCHEDULED_STUDY = {**RUNNING_STUDY, "id": "770001", "start_time": "2026-12-01T00:00:00+0000",
                   "end_time": "2026-12-20T00:00:00+0000"}


@pytest.mark.parametrize("study", [SCHEDULED_STUDY, {**RUNNING_STUDY, "id": "770001"}], ids=["scheduled", "running"])
def test_cancel_stops_when_the_study_read_names_another_study(monkeypatch, study) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"770000": study}))
    with pytest.raises(MetaApiError, match="different study.*Nothing was sent"):
        run(experiments.cancel_ad_study(study_id="770000"))
    assert client.kinds() == ["get"]


@pytest.mark.parametrize("named", ["770000", 770000, None], ids=["same-text", "same-number", "no-id"])
def test_cancel_reads_an_ordinary_study_answer_as_before(monkeypatch, named) -> None:
    study = {key: value for key, value in SCHEDULED_STUDY.items() if key != "id"}
    if named is not None:
        study["id"] = named
    client = use(monkeypatch, FakeStudyClient(objects={"770000": study}))
    result = run(experiments.cancel_ad_study(study_id="770000"))
    assert client.kinds() == ["get", "delete"]
    assert result["current"]["operation"] == "delete"


@pytest.mark.parametrize("change", [
    {"end_time": FUTURE_END},
    {"start_time": "2026-12-02T00:00:00+00:00"},
    {"params": {"observation_end_time": "2026-12-20T00:00:00+00:00"}},
], ids=["end-time", "start-time", "observation-end-time"])
def test_update_ad_study_stops_when_its_deciding_read_names_another_study(monkeypatch, change) -> None:
    client = use(monkeypatch, FakeStudyClient(objects={"770000": {**SCHEDULED_STUDY, "id": "770001"}}))
    with pytest.raises(MetaApiError, match="different study.*Nothing was changed"):
        run(experiments.update_ad_study(study_id="770000", **change))
    assert client.kinds() == ["get"]


@pytest.mark.parametrize("named", ["770000", 770000, None], ids=["same-text", "same-number", "no-id"])
def test_update_ad_study_reads_an_ordinary_study_answer_as_before(monkeypatch, named) -> None:
    study = {key: value for key, value in SCHEDULED_STUDY.items() if key != "id"}
    if named is not None:
        study["id"] = named
    client = use(monkeypatch, FakeStudyClient(objects={"770000": study}))
    run(experiments.update_ad_study(study_id="770000", end_time=FUTURE_END))
    assert client.calls[-1] == ("update", "770000", {"end_time": FUTURE_END_TS})


def test_update_ad_study_name_change_with_a_read_naming_another_study_is_left_as_before(monkeypatch) -> None:
    """Accepted class: a name change does not depend on the read, which only supplies previous."""
    client = use(monkeypatch, FakeStudyClient(objects={"770000": {**SCHEDULED_STUDY, "id": "770001"}}))
    run(experiments.update_ad_study(study_id="770000", name="New"))
    assert client.calls[-1] == ("update", "770000", {"name": "New"})
