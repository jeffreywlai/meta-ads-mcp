"""Product catalog, product and product set tool tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from meta_ads_mcp.errors import MetaApiError, NotFoundError, ValidationError
from meta_ads_mcp.tools import catalogs


class FakeCatalogClient:
    """Fake Graph client recording every call made by the product catalog tools."""

    def __init__(
        self,
        *,
        objects: dict[str, dict[str, Any]] | None = None,
        edges: dict[tuple[str, str], Any] | None = None,
        get_errors: dict[str, Exception] | None = None,
        edge_errors: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self.objects = objects or {}
        self.edges = edges or {}
        self.get_errors = get_errors or {}
        self.edge_errors = edge_errors or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", {"object_id": object_id, "fields": fields, "params": params}))
        if object_id in self.get_errors:
            raise self.get_errors[object_id]
        return dict(self.objects.get(object_id, {"id": object_id}))

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
        if (parent_id, edge) in self.edge_errors:
            raise self.edge_errors[(parent_id, edge)]
        response = self.edges.get((parent_id, edge), {"data": []})
        if isinstance(response, list):
            return response.pop(0)
        return response

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", {"parent_id": parent_id, "edge": edge, "data": data}))
        return {"id": "new_1"}

    async def update_object(self, object_id, *, data):
        self.calls.append(("update", {"object_id": object_id, "data": data}))
        return {"success": True}

    async def delete_object(self, object_id):
        self.calls.append(("delete", {"object_id": object_id}))
        return {"success": True}


def _use(monkeypatch, client) -> Any:
    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: client)
    return client


def _no_client(monkeypatch) -> None:
    monkeypatch.setattr(
        catalogs, "get_graph_api_client", lambda: pytest.fail("No client call expected")
    )


def _run(coro):
    return asyncio.run(coro)


def _permission_error() -> MetaApiError:
    return MetaApiError(
        message="(#200) Requires catalog_management permission to manage the object",
        status_code=403,
        code=200,
    )


# ---------------------------------------------------------------- list_catalogs


def test_list_catalogs_requires_one_scope(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="exactly one of business_id, account_id, or catalog_id"):
        _run(catalogs.list_catalogs())
    with pytest.raises(ValidationError, match="exactly one of business_id, account_id, or catalog_id"):
        _run(catalogs.list_catalogs(business_id="1", catalog_id="2"))
    with pytest.raises(ValidationError, match="exactly one"):
        _run(catalogs.list_catalogs(business_id=" ", account_id=""))


def test_list_catalogs_for_business_reads_owned_edge_with_paging(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={
        ("biz_1", "owned_product_catalogs"): {
            "data": [{"id": "cat_1", "name": "Main", "product_count": 10}],
            "paging": {"cursors": {"after": "c2"}, "next": "https://graph/next"},
        },
    }))
    result = _run(catalogs.list_catalogs(business_id="biz_1", limit=10, after="c1"))
    kind, call = client.calls[0]
    assert kind == "list"
    assert call["parent_id"] == "biz_1"
    assert call["edge"] == "owned_product_catalogs"
    assert call["fields"] == catalogs.CATALOG_FIELDS
    assert call["params"] == {"limit": 10, "after": "c1"}
    assert result["items"][0]["id"] == "cat_1"
    assert result["summary"]["complete"] is False
    assert result["summary"]["next_after"] == "c2"
    assert result["summary"]["scope"] == {"business_id": "biz_1", "ownership": "owned"}


def test_list_catalogs_client_ownership_reads_client_edge(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _run(catalogs.list_catalogs(business_id="biz_1", ownership="client"))
    assert client.calls[0][1]["edge"] == "client_product_catalogs"
    with pytest.raises(ValidationError, match="ownership"):
        _run(catalogs.list_catalogs(business_id="biz_1", ownership="shared"))


def test_list_catalogs_for_account_uses_owning_business(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(
        objects={"act_42": {"id": "act_42", "business": {"id": "biz_9", "name": "Biz"}}},
        edges={("biz_9", "owned_product_catalogs"): {"data": [{"id": "cat_1"}]}},
    ))
    result = _run(catalogs.list_catalogs(account_id="42"))
    assert client.calls[0] == ("get", {"object_id": "act_42", "fields": ["business"], "params": None})
    assert client.calls[1][1]["parent_id"] == "biz_9"
    assert result["summary"]["scope"] == {
        "account_id": "act_42", "business_id": "biz_9", "ownership": "owned",
        "note": "Ad accounts have no documented catalog edge; these are catalogs of the business that owns the ad account.",
    }


def test_list_catalogs_for_account_without_business_raises(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={"act_42": {"id": "act_42"}}))
    with pytest.raises(ValidationError, match="business_id"):
        _run(catalogs.list_catalogs(account_id="act_42"))
    assert client.kinds() == ["get"]


def test_list_catalogs_single_catalog_returns_item_with_counts(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={
        "cat_1": {"id": "cat_1", "name": "Main", "product_count": 5, "feed_count": 2},
    }))
    result = _run(catalogs.list_catalogs(catalog_id="cat_1", fields="id, name"))
    assert client.calls[0][1]["fields"] == ["id", "name"]
    assert result["items"] == [{"id": "cat_1", "name": "Main", "product_count": 5, "feed_count": 2}]
    assert result["summary"]["complete"] is True


def test_permission_error_is_not_swallowed(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogClient(
        edge_errors={("biz_1", "owned_product_catalogs"): _permission_error()},
    ))
    with pytest.raises(MetaApiError, match="catalog_management") as raised:
        _run(catalogs.list_catalogs(business_id="biz_1"))
    assert raised.value.code == 200


def test_list_catalogs_rejects_nonpositive_limit(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="limit"):
        _run(catalogs.list_catalogs(business_id="biz_1", limit=0))


# ---------------------------------------------------- list_dpa_eligible_catalogs


def _dpa_client(**overrides) -> FakeCatalogClient:
    edges = {
        ("biz_1", "owned_product_catalogs"): {"data": [
            {"id": "cat_ready", "name": "Ready", "vertical": "commerce", "product_count": 500},
            {"id": "cat_empty", "name": "Empty", "vertical": "commerce", "product_count": 0},
            {"id": "cat_nosrc", "name": "No source", "vertical": "commerce", "product_count": 900},
        ]},
        ("cat_ready", "product_sets"): {"data": [{"id": "ps_1"}], "summary": {"total_count": 3}},
        ("cat_ready", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_ready", "diagnostics"): {"data": []},
        ("cat_empty", "product_sets"): {"data": [{"id": "ps_2"}], "summary": {"total_count": 1}},
        ("cat_empty", "external_event_sources"): {"data": [{"id": "px_1"}]},
        ("cat_empty", "diagnostics"): {"data": []},
        ("cat_nosrc", "product_sets"): {"data": [{"id": "ps_3"}], "summary": {"total_count": 1}},
        ("cat_nosrc", "external_event_sources"): {"data": []},
        ("cat_nosrc", "diagnostics"): {"data": []},
    }
    edges.update(overrides.pop("edges", {}))
    return FakeCatalogClient(edges=edges, **overrides)


def test_dpa_eligible_catalogs_ranks_with_reasons_and_recommends_clear_choice(monkeypatch) -> None:
    client = _use(monkeypatch, _dpa_client())
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ranked = [finding["catalog_id"] for finding in result["findings"]]
    assert ranked == ["cat_ready", "cat_nosrc", "cat_empty"]
    assessments = [finding["assessment"] for finding in result["findings"]]
    assert assessments == ["ready", "needs_attention", "blocked"]
    assert all(finding["reasons"] for finding in result["findings"])
    assert any("no products" in reason for reason in result["findings"][2]["reasons"])
    assert any("event source" in reason for reason in result["findings"][1]["reasons"])
    assert result["recommended_catalog_id"] == "cat_ready"
    assert result["metrics"]["catalogs_evaluated"] == 3
    assert result["metrics"]["eligible_count"] == 2
    assert set(result["basis"]["signals_used"]) >= {
        "product_catalog.product_count", "product_catalog/product_sets",
        "product_catalog/external_event_sources", "product_catalog/diagnostics",
    }
    assert "derived" in result["basis"]["note"]
    diag_call = next(call for kind, call in client.calls if kind == "list" and call["edge"] == "diagnostics")
    assert diag_call["params"]["affected_channels"] == ["da"]
    assert diag_call["params"]["severities"] == ["MUST_FIX"]


def test_dpa_eligible_catalogs_no_recommendation_when_several_ready(monkeypatch) -> None:
    client = _dpa_client(edges={
        ("cat_nosrc", "external_event_sources"): {"data": [{"id": "px_2"}]},
    })
    _use(monkeypatch, client)
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert result["recommended_catalog_id"] is None
    assert "2 catalogs" in result["recommendation_reason"]


def test_dpa_eligible_catalogs_must_fix_diagnostics_need_attention(monkeypatch) -> None:
    client = _dpa_client(edges={
        ("cat_ready", "diagnostics"): {"data": [
            {"title": "Missing images", "severity": "MUST_FIX", "number_of_affected_items": 40},
        ]},
    })
    _use(monkeypatch, client)
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["assessment"] == "needs_attention"
    assert ready["signals"]["da_must_fix_affected_items"] == 40
    assert result["recommended_catalog_id"] is None


def test_dpa_eligible_catalogs_reports_unavailable_signal_instead_of_hiding_it(monkeypatch) -> None:
    client = _dpa_client(edge_errors={("cat_ready", "external_event_sources"): _permission_error()})
    _use(monkeypatch, client)
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["assessment"] == "unknown"
    assert ready["unavailable_signals"][0]["signal"] == "external_event_sources"
    assert ready["unavailable_signals"][0]["code"] == 200
    assert any("cat_ready" in entry for entry in result["missing_signals"])


def test_dpa_eligible_catalogs_listing_permission_error_raises(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogClient(edge_errors={("biz_1", "owned_product_catalogs"): _permission_error()}))
    with pytest.raises(MetaApiError):
        _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))


def test_dpa_catalog_listing_next_page_without_cursor_is_incomplete(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={
        ("biz_1", "owned_product_catalogs"): {
            "data": [], "paging": {"next": "https://graph.invalid/?after=unextracted"},
        },
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert result["metrics"]["complete"] is False
    assert any("no cursor" in entry for entry in result["missing_signals"])
    assert client.kinds() == ["list"]


def test_dpa_catalog_listing_empty_cursor_cycle_terminates(monkeypatch) -> None:
    class Cycle(FakeCatalogClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
            assert len(self.calls) <= 6, "catalog listing loops over an empty A/B cursor cycle"
            following = "B" if params.get("after") == "A" else "A"
            return {"data": [], "paging": {"next": "next", "cursors": {"after": following}}}

    client = _use(monkeypatch, Cycle())
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert result["metrics"]["complete"] is False
    assert [call["params"].get("after") for _, call in client.calls] == [None, "A", "B"]
    assert any("already been read" in entry for entry in result["missing_signals"])


def test_dpa_catalog_listing_empty_pages_stop_at_request_cap(monkeypatch) -> None:
    pages = [{"data": [], "paging": {"next": "next", "cursors": {"after": f"c{index}"}}} for index in range(20)]
    client = _use(monkeypatch, FakeCatalogClient(edges={("biz_1", "owned_product_catalogs"): pages}))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert len(client.calls) == catalogs._DPA_MAX_LIST_PAGES
    assert result["metrics"]["complete"] is False


def test_dpa_catalog_listing_repeated_page_is_not_added_twice(monkeypatch) -> None:
    page = {"data": [{"id": "cat_ready", "name": "Ready", "product_count": 500}],
            "paging": {"next": "next", "cursors": {"after": "x1"}}}
    again = {"data": [{"id": "cat_ready", "name": "Ready", "product_count": 500}],
             "paging": {"next": "next", "cursors": {"after": "x2"}}}
    client = _dpa_client(edges={("biz_1", "owned_product_catalogs"): [page, again]})
    _use(monkeypatch, client)
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert [finding["catalog_id"] for finding in result["findings"]] == ["cat_ready"]
    assert result["metrics"]["complete"] is False


def test_dpa_catalog_listing_follows_cursors_to_the_last_page(monkeypatch) -> None:
    client = _dpa_client(edges={("biz_1", "owned_product_catalogs"): [
        {"data": [{"id": "cat_ready", "product_count": 500}], "paging": {"next": "n", "cursors": {"after": "p2"}}},
        {"data": [{"id": "cat_nosrc", "product_count": 900}], "paging": {"cursors": {"after": "p3"}}},
    ]})
    _use(monkeypatch, client)
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert result["metrics"]["complete"] is True
    assert {finding["catalog_id"] for finding in result["findings"]} == {"cat_ready", "cat_nosrc"}
    listing_calls = [call for _, call in client.calls if call.get("edge") == "owned_product_catalogs"]
    assert [call["params"] for call in listing_calls] == [{"limit": 50}, {"limit": 50, "after": "p2"}]


def test_dpa_eligible_catalogs_requires_one_scope(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="exactly one of business_id or account_id"):
        _run(catalogs.list_dpa_eligible_catalogs())
    with pytest.raises(ValidationError, match="exactly one of business_id or account_id"):
        _run(catalogs.list_dpa_eligible_catalogs(business_id="1", account_id="2"))


# ---------------------------------------------------------- get_catalog_diagnostics


def test_diagnostics_ordered_by_products_affected(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "diagnostics"): {"data": [
        {"title": "Small", "severity": "OPPORTUNITY", "number_of_affected_items": 3},
        {"title": "Big", "severity": "MUST_FIX", "number_of_affected_items": 300},
        {"title": "Unknown"},
        {"title": "Medium", "severity": "MUST_FIX", "number_of_affected_items": "40"},
    ]}}))
    result = _run(catalogs.get_catalog_diagnostics(catalog_id="cat_1"))
    assert [item["title"] for item in result["items"]] == ["Big", "Medium", "Small", "Unknown"]
    assert result["summary"]["ordered_by"] == "number_of_affected_items"
    assert result["summary"]["by_severity"] == {"MUST_FIX": 2, "OPPORTUNITY": 1, "UNKNOWN": 1}
    call = client.calls[0][1]
    assert (call["parent_id"], call["edge"]) == ("cat_1", "diagnostics")
    assert call["fields"] == catalogs.DIAGNOSTIC_FIELDS
    assert call["params"] == {"limit": 100}


def test_diagnostics_severity_filter_is_sent_and_validated(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _run(catalogs.get_catalog_diagnostics(catalog_id="cat_1", severity="must_fix", limit=5, after="x"))
    assert client.calls[0][1]["params"] == {"limit": 5, "after": "x", "severities": ["MUST_FIX"]}
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="MUST_FIX, OPPORTUNITY"):
        _run(catalogs.get_catalog_diagnostics(catalog_id="cat_1", severity="warning"))
    with pytest.raises(ValidationError, match="catalog_id"):
        _run(catalogs.get_catalog_diagnostics(catalog_id=" "))


def _heavy_nested_entry(index: int) -> dict[str, Any]:
    """A nested diagnostic entry shaped like the 12-key, multi-KB entries Meta returned live."""
    return {
        "type": f"NESTED_TYPE_{index}",
        "severity": "MUST_FIX",
        "title": f"Nested problem {index}",
        "subtitle": "Some items are missing a required attribute. " * 8,
        "call_to_action": {"label": "Fix items", "uri": "https://business.facebook.com/" + "x" * 200},
        "affected_items": [{"id": f"item_{n}", "retailer_id": f"sku_{n}", "name": "Product " * 6} for n in range(25)],
        "sample_values": ["value " * 10 for _ in range(20)],
        "error_code": 1000 + index,
        "number_of_affected_items": 400 + index,
        "description": "Long description of the problem. " * 20,
        "learn_more": "https://www.facebook.com/business/help/" + "y" * 150,
        "affected_features": ["dynamic_ads", "shops"],
    }


def _heavy_group(index: int, entries: int = 5) -> dict[str, Any]:
    return {
        "type": f"GROUP_TYPE_{index}",
        "severity": "MUST_FIX" if index % 2 else "OPPORTUNITY",
        "title": f"Group {index}",
        "subtitle": f"{index} kinds of item problems",
        "affected_channels": ["da", "shops"],
        "affected_entity": "product_item",
        "affected_features": ["dynamic_ads"],
        "error_code": 2000 + index,
        "number_of_affected_items": 1000 - index,
        "number_of_affected_entities": 10 + index,
        "diagnostics": [_heavy_nested_entry(n) for n in range(entries)],
    }


def test_diagnostics_default_returns_compact_groups_with_capped_detail_sample(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "diagnostics"): {"data": [
        _heavy_group(1), _heavy_group(2),
    ]}}))
    result = _run(catalogs.get_catalog_diagnostics(catalog_id="cat_1", limit=5))
    first = result["items"][0]
    assert set(first) == {
        "type", "severity", "title", "subtitle", "affected_entity", "error_code",
        "number_of_affected_items", "number_of_affected_entities", "detail_count", "detail_sample",
    }
    assert first["title"] == "Group 1"
    assert first["number_of_affected_items"] == 999
    assert first["detail_count"] == 5
    assert len(first["detail_sample"]) <= catalogs.DIAGNOSTIC_DETAIL_SAMPLE_SIZE
    sample = first["detail_sample"][0]
    assert "affected_items" not in sample and "sample_values" not in sample
    assert sample["title"] == "Nested problem 0"
    assert all(len(value) <= catalogs.DIAGNOSTIC_DETAIL_TEXT_LIMIT + 3
               for value in sample.values() if isinstance(value, str))
    summary = result["summary"]
    assert summary["details_included"] is False
    assert summary["detail_entries_total"] == 10
    assert "include_details" in summary["detail_note"]


def test_diagnostics_include_details_returns_full_nested_data(monkeypatch) -> None:
    group = _heavy_group(1)
    _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "diagnostics"): {"data": [group]}}))
    result = _run(catalogs.get_catalog_diagnostics(catalog_id="cat_1", include_details=True))
    assert result["items"][0] == group
    assert result["summary"]["details_included"] is True
    assert "detail_note" not in result["summary"]


def test_diagnostics_default_response_stays_small_for_heavy_groups(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "diagnostics"): {"data": [
        _heavy_group(index) for index in range(20)
    ]}}))
    result = _run(catalogs.get_catalog_diagnostics(catalog_id="cat_1"))
    assert len(result["items"]) == 20
    assert len(json.dumps(result).encode()) < 20_000


# --------------------------------------------------- get_catalog_dynamic_ads_health


def _health_client(**overrides) -> FakeCatalogClient:
    objects = {
        "cat_1": {"id": "cat_1", "name": "Main", "product_count": 120},
        "ps_1": {"id": "ps_1", "name": "Shoes", "product_count": 0, "product_catalog": {"id": "cat_1"}},
    }
    edges = {
        ("cat_1", "diagnostics"): {"data": []},
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
    }
    objects.update(overrides.pop("objects", {}))
    edges.update(overrides.pop("edges", {}))
    return FakeCatalogClient(objects=objects, edges=edges, **overrides)


def _assert_no_da_checks(client: FakeCatalogClient, result: dict[str, Any]) -> None:
    """Meta rejects catalog and product set da_checks live, so the tool must neither read nor mention them."""
    assert not any(call.get("edge") == "da_checks" for _, call in client.calls)
    assert "da_checks" not in json.dumps(result)


def test_dynamic_ads_health_no_blockers(monkeypatch) -> None:
    client = _use(monkeypatch, _health_client())
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))
    assert result["status"] == "no_blockers_found"
    assert result["findings"] == []
    assert result["basis"]["signals_used"] == [
        "product_catalog.product_count", "product_catalog/diagnostics", "product_catalog/external_event_sources",
    ]
    assert "failed_checks" not in result["metrics"]
    diag_call = next(call for kind, call in client.calls if kind == "list" and call["edge"] == "diagnostics")
    assert diag_call["params"]["affected_channels"] == ["da"]
    assert "diagnostics" not in diag_call["fields"]
    _assert_no_da_checks(client, result)


def test_dynamic_ads_health_empty_product_set_blocks(monkeypatch) -> None:
    client = _use(monkeypatch, _health_client())
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1", product_set_id="ps_1"))
    assert result["status"] == "blocked"
    assert result["findings"][0]["severity"] == "blocking"
    assert "will not deliver" in result["findings"][0]["message"]
    assert result["metrics"]["product_set_product_count"] == 0
    _assert_no_da_checks(client, result)


def test_dynamic_ads_health_must_fix_diagnostics_are_at_risk(monkeypatch) -> None:
    client = _use(monkeypatch, _health_client(edges={
        ("cat_1", "diagnostics"): {"data": [
            {"title": "Images missing", "severity": "MUST_FIX", "number_of_affected_items": 7},
            {"title": "Better titles", "severity": "OPPORTUNITY", "number_of_affected_items": 70},
        ]},
    }))
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))
    assert result["status"] == "at_risk"
    assert [finding["source"] for finding in result["findings"]] == ["product_catalog/diagnostics"]
    assert result["findings"][0]["affected_items"] == 7
    assert result["metrics"]["must_fix_groups"] == 1
    assert result["metrics"]["opportunity_groups"] == 1
    _assert_no_da_checks(client, result)


def test_dynamic_ads_health_no_products_and_no_event_source(monkeypatch) -> None:
    _use(monkeypatch, _health_client(
        objects={"cat_1": {"id": "cat_1", "product_count": 0}},
        edges={("cat_1", "external_event_sources"): {"data": []}},
    ))
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))
    assert result["status"] == "blocked"
    messages = " ".join(finding["message"] for finding in result["findings"])
    assert "no products" in messages
    assert "event source" in messages


def test_dynamic_ads_health_unavailable_signal_is_inconclusive(monkeypatch) -> None:
    client = _use(monkeypatch, _health_client(edge_errors={("cat_1", "diagnostics"): _permission_error()}))
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))
    assert result["status"] == "inconclusive"
    assert any("product_catalog/diagnostics" in entry for entry in result["missing_signals"])
    assert "product_catalog/diagnostics" not in result["basis"]["signals_used"]
    _assert_no_da_checks(client, result)


def test_dynamic_ads_health_rejects_product_set_from_other_catalog(monkeypatch) -> None:
    client = _use(monkeypatch, _health_client(objects={
        "ps_1": {"id": "ps_1", "product_count": 4, "product_catalog": {"id": "cat_other"}},
    }))
    with pytest.raises(ValidationError, match="cat_other"):
        _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1", product_set_id="ps_1"))
    assert "update" not in client.kinds()


def test_dynamic_ads_health_catalog_read_error_raises(monkeypatch) -> None:
    _use(monkeypatch, _health_client(get_errors={"cat_1": _permission_error()}))
    with pytest.raises(MetaApiError):
        _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))


def test_catalogs_module_never_reads_da_checks() -> None:
    import inspect

    assert "da_checks" not in inspect.getsource(catalogs)
    assert not hasattr(catalogs, "DA_CHECK_FIELDS")


# ------------------------------------------------------- list_catalog_data_sources


def test_list_catalog_data_sources_requests_all_sources(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "data_sources"): {"data": [
        {"id": "ds_1", "upload_type": "SCHEDULED", "ingestion_source_type": "PRIMARY_FEED"},
        {"id": "ds_2", "upload_type": "SCHEDULED", "ingestion_source_type": "SUPPLEMENTARY_FEED"},
        {"id": "ds_3", "upload_type": "API"},
    ]}}))
    result = _run(catalogs.list_catalog_data_sources(catalog_id="cat_1"))
    call = client.calls[0][1]
    assert (call["parent_id"], call["edge"]) == ("cat_1", "data_sources")
    assert call["fields"] == catalogs.DATA_SOURCE_FIELDS
    assert call["params"] == {"limit": 100, "ingestion_source_type": "ALL"}
    assert result["summary"]["by_upload_type"] == {"SCHEDULED": 2, "API": 1}
    assert result["summary"]["complete"] is True


def test_list_catalog_data_sources_validates_type(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="ALL, PRIMARY, SUPPLEMENTARY"):
        _run(catalogs.list_catalog_data_sources(catalog_id="cat_1", ingestion_source_type="FEED"))


# ------------------------------------------------------------ create/update catalog


def test_create_catalog_posts_to_business_owned_edge(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    result = _run(catalogs.create_catalog(business_id="biz_1", name="Shoes", vertical="commerce"))
    assert client.calls == [("create", {
        "parent_id": "biz_1", "edge": "owned_product_catalogs",
        "data": {"name": "Shoes", "vertical": "commerce"},
    })]
    assert result["action"] == "create_catalog"
    assert result["created"] == {"id": "new_1"}
    assert result["target"] == {"business_id": "biz_1"}


def test_create_catalog_validates_before_network(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="vertical"):
        _run(catalogs.create_catalog(business_id="biz_1", name="X", vertical="shoes"))
    with pytest.raises(ValidationError, match="name"):
        _run(catalogs.create_catalog(business_id="biz_1", name=" "))
    with pytest.raises(ValidationError, match="name"):
        _run(catalogs.create_catalog(business_id="biz_1", name="X", params={"name": "Y"}))


def test_update_catalog_reads_then_updates(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={"cat_1": {"id": "cat_1", "name": "Old"}}))
    result = _run(catalogs.update_catalog(
        catalog_id="cat_1", name="New", params={"default_image_url": "https://x/img.png"},
    ))
    assert client.kinds() == ["get", "update"]
    assert client.calls[1][1] == {
        "object_id": "cat_1", "data": {"name": "New", "default_image_url": "https://x/img.png"},
    }
    assert result["previous"]["name"] == "Old"
    assert result["current"] == {"name": "New", "default_image_url": "https://x/img.png"}


def test_update_catalog_requires_a_change(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="At least one"):
        _run(catalogs.update_catalog(catalog_id="cat_1"))


# ---------------------------------------------------------------- products


def test_list_catalog_products_by_catalog_with_filter(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "products"): {
        "data": [{"id": "p_1"}], "summary": {"total_count": 41},
    }}))
    result = _run(catalogs.list_catalog_products(
        catalog_id="cat_1", filter='{"brand": {"eq": "Acme"}}', limit=10,
    ))
    call = client.calls[0][1]
    assert (call["parent_id"], call["edge"]) == ("cat_1", "products")
    assert call["fields"] == catalogs.PRODUCT_FIELDS
    assert call["params"] == {"limit": 10, "summary": "total_count", "filter": {"brand": {"eq": "Acme"}}}
    assert result["summary"]["total_count"] == 41


def test_list_catalog_products_by_set_and_single_product(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={"p_9": {"id": "p_9", "name": "Shoe"}}))
    _run(catalogs.list_catalog_products(product_set_id="ps_1"))
    assert (client.calls[0][1]["parent_id"], client.calls[0][1]["edge"]) == ("ps_1", "products")
    result = _run(catalogs.list_catalog_products(product_id="p_9", fields=["id", "name"]))
    assert client.calls[1] == ("get", {"object_id": "p_9", "fields": ["id", "name"], "params": None})
    assert result["items"] == [{"id": "p_9", "name": "Shoe"}]


def test_list_catalog_products_validation(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="exactly one of catalog_id, product_set_id, or product_id"):
        _run(catalogs.list_catalog_products())
    with pytest.raises(ValidationError, match="exactly one"):
        _run(catalogs.list_catalog_products(catalog_id="c", product_set_id="p"))
    with pytest.raises(ValidationError, match="filter"):
        _run(catalogs.list_catalog_products(catalog_id="c", filter="{not json"))
    with pytest.raises(ValidationError, match="filter"):
        _run(catalogs.list_catalog_products(catalog_id="c", filter="[1, 2]"))
    with pytest.raises(ValidationError, match="filter"):
        _run(catalogs.list_catalog_products(product_id="p", filter={"brand": {"eq": "A"}}))


def test_product_price_uses_major_units(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    result = _run(catalogs.create_catalog_product(
        catalog_id="cat_1", retailer_id="SKU-1", name="Shoe", price=12.5, currency="usd",
        url="https://shop/x", image_url="https://shop/x.png",
    ))
    kind, call = client.calls[0]
    assert kind == "create"
    assert (call["parent_id"], call["edge"]) == ("cat_1", "products")
    # Meta: "Price of the item with 2 digits added for cents (ex: use "100" for 1 or "599" for 5.99)"
    assert call["data"]["price"] == 1250
    assert call["data"]["currency"] == "USD"
    assert result["created"] == {"id": "new_1"}


def test_create_catalog_product_defaults_to_staging_without_upsert(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _run(catalogs.create_catalog_product(
        catalog_id="cat_1", retailer_id="SKU-1", name="Shoe", price=5.99, currency="EUR",
        url="https://shop/x", image_url="https://shop/x.png", sale_price=4,
    ))
    assert client.calls[0][1]["data"] == {
        "retailer_id": "SKU-1", "name": "Shoe", "price": 599, "currency": "EUR",
        "url": "https://shop/x", "image_url": "https://shop/x.png",
        "availability": "in stock", "visibility": "staging", "allow_upsert": False,
        "sale_price": 400,
    }


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"price": 1.234}, "2 decimal places"),
    ({"price": -1}, "price"),
    ({"price": float("nan")}, "price"),
    ({"currency": "US"}, "currency"),
    ({"availability": "available"}, "availability"),
    ({"visibility": "hidden"}, "visibility"),
    ({"condition": "broken"}, "condition"),
    ({"retailer_id": " "}, "retailer_id"),
    ({"params": {"price": 100}}, "price"),
])
def test_create_catalog_product_validates_before_network(monkeypatch, kwargs, match) -> None:
    _no_client(monkeypatch)
    arguments = {
        "catalog_id": "cat_1", "retailer_id": "SKU-1", "name": "Shoe", "price": 10,
        "currency": "USD", "url": "https://shop/x", "image_url": "https://shop/x.png",
    }
    arguments.update(kwargs)
    with pytest.raises(ValidationError, match=match):
        _run(catalogs.create_catalog_product(**arguments))


def test_update_catalog_product_reads_then_updates(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={
        "p_1": {"id": "p_1", "name": "Old", "price": "$10.00", "currency": "USD", "availability": "in stock"},
    }))
    result = _run(catalogs.update_catalog_product(product_id="p_1", price=8, availability="out of stock"))
    assert client.kinds() == ["get", "update"]
    assert client.calls[1][1] == {"object_id": "p_1", "data": {"price": 800, "availability": "out of stock"}}
    assert result["previous"]["price"] == "$10.00"
    assert result["previous"]["availability"] == "in stock"
    assert result["current"] == {"price": 8, "availability": "out of stock"}
    assert result["sent"] == {"price": 800, "availability": "out of stock"}


def test_update_catalog_product_requires_a_change(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="At least one"):
        _run(catalogs.update_catalog_product(product_id="p_1"))
    with pytest.raises(ValidationError, match="2 decimal places"):
        _run(catalogs.update_catalog_product(product_id="p_1", price=1.001))


def test_delete_product_reads_then_deletes_and_returns_previous(monkeypatch) -> None:
    product = {"id": "p_1", "retailer_id": "SKU-1", "name": "Shoe", "price": "$10.00"}
    client = _use(monkeypatch, FakeCatalogClient(objects={"p_1": product}))
    result = _run(catalogs.delete_catalog_product(product_id="p_1"))
    assert client.kinds() == ["get", "delete"]
    assert client.calls[0][1]["fields"] == catalogs.PRODUCT_DETAIL_FIELDS
    assert client.calls[1][1] == {"object_id": "p_1"}
    assert result["action"] == "delete_catalog_product"
    assert result["previous"] == product
    assert result["current"] == {"deleted": True}
    assert result["result"] == {"success": True}


def test_delete_product_missing_object_sends_no_delete(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(get_errors={"p_1": NotFoundError("gone")}))
    with pytest.raises(NotFoundError):
        _run(catalogs.delete_catalog_product(product_id="p_1"))
    assert client.kinds() == ["get"]


# ------------------------------------------------------- build_product_set_filter


def test_filter_builder_rejects_unknown_operator_listing_accepted_ones(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as raised:
        _run(catalogs.build_product_set_filter(
            catalog_id="cat_1", conditions=[{"field": "brand", "operator": "like", "value": "A"}],
        ))
    message = str(raised.value)
    assert "like" in message
    for operator in catalogs.FILTER_OPERATORS:
        assert operator in message


def test_filter_builder_all_and_any_produce_and_or_shapes(monkeypatch) -> None:
    conditions = [
        {"field": "brand", "operator": "eq", "value": "Acme"},
        {"field": "price_amount", "operator": "lt", "value": 5000},
    ]
    _use(monkeypatch, FakeCatalogClient())
    all_result = _run(catalogs.build_product_set_filter(catalog_id="cat_1", conditions=conditions))
    any_result = _run(catalogs.build_product_set_filter(catalog_id="cat_1", conditions=conditions, match="any"))
    assert all_result["filter"] == {"and": [{"brand": {"eq": "Acme"}}, {"price_amount": {"lt": 5000}}]}
    assert any_result["filter"] == {"or": [{"brand": {"eq": "Acme"}}, {"price_amount": {"lt": 5000}}]}
    single = _run(catalogs.build_product_set_filter(catalog_id="cat_1", conditions=conditions[:1]))
    assert single["filter"] == {"brand": {"eq": "Acme"}}
    assert json.loads(single["filter_json"]) == single["filter"]


def test_filter_builder_returns_match_count_and_sample(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "products"): {
        "data": [{"id": f"p_{index}"} for index in range(5)],
        "summary": {"total_count": 123},
    }}))
    result = _run(catalogs.build_product_set_filter(
        catalog_id="cat_1",
        conditions=[{"field": "retailer_id", "operator": "is_any", "value": ["a", "b"]}],
    ))
    call = client.calls[0][1]
    assert (call["parent_id"], call["edge"]) == ("cat_1", "products")
    assert call["params"] == {
        "filter": {"retailer_id": {"is_any": ["a", "b"]}}, "summary": "total_count", "limit": 5,
    }
    assert call["fields"] == catalogs.FILTER_SAMPLE_FIELDS
    assert result["match_count"] == 123
    assert len(result["sample"]) == 5
    assert client.kinds() == ["list"]


def test_filter_builder_warns_on_zero_matches_and_missing_count(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "products"): [
        {"data": [], "summary": {"total_count": 0}},
        {"data": [{"id": "p_1"}]},
    ]}))
    conditions = [{"field": "brand", "operator": "eq", "value": "None"}]
    empty = _run(catalogs.build_product_set_filter(catalog_id="cat_1", conditions=conditions))
    assert empty["match_count"] == 0
    assert any("will not deliver" in warning for warning in empty["warnings"])
    missing = _run(catalogs.build_product_set_filter(catalog_id="cat_1", conditions=conditions))
    assert missing["match_count"] is None
    assert missing["missing_signals"]


@pytest.mark.parametrize(("conditions", "match"), [
    ([], "conditions"),
    ([{"field": "", "operator": "eq", "value": "x"}], "field"),
    ([{"field": "brand", "operator": "eq"}], "value"),
    ([{"field": "brand", "operator": "is_any", "value": "x"}], "list"),
    ([{"field": "brand", "operator": "is_any", "value": []}], "list"),
    ([{"field": "price_amount", "operator": "gt", "value": "10"}], "number"),
    ([{"field": "price_amount", "operator": "gt", "value": True}], "number"),
    ([{"field": "brand", "operator": "contains", "value": 3}], "string"),
    ([{"field": "brand", "operator": "starts_with", "value": "A"}], "category"),
    (["brand eq x"], "object"),
])
def test_filter_builder_validates_conditions(monkeypatch, conditions, match) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=match):
        _run(catalogs.build_product_set_filter(catalog_id="cat_1", conditions=conditions))


def test_filter_builder_rejects_unknown_match(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="all or any"):
        _run(catalogs.build_product_set_filter(
            catalog_id="cat_1", conditions=[{"field": "brand", "operator": "eq", "value": "A"}], match="some",
        ))


def test_filter_builder_is_read_only_allowed() -> None:
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.read_only import read_only_tool_names
    from meta_ads_mcp.tool_contracts import build_tool_contracts
    from meta_ads_mcp.tools import utility

    assert "build_product_set_filter" in read_only_tool_names()
    assert "build_product_set_filter" in utility.TOOL_GROUPS["catalog"]
    components = mcp_server.local_provider._components.values()
    contracts = build_tool_contracts([c for c in components if getattr(c, "name", None)])
    assert contracts["build_product_set_filter"].effect == "read"


def test_filter_builder_runs_in_read_only_mode(monkeypatch) -> None:
    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.coordinator import mcp_server

    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "products"): {"data": [], "summary": {"total_count": 0}}}))
    try:
        result = asyncio.run(mcp_server.call_tool("build_product_set_filter", {
            "catalog_id": "cat_1",
            "conditions": [{"field": "brand", "operator": "eq", "value": "A"}],
        })).structured_content
        assert result["match_count"] == 0
    finally:
        monkeypatch.delenv("META_READ_ONLY")
        reload_settings()


# ---------------------------------------------------------------- product sets


def test_list_product_sets_three_scopes(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(
        objects={"ps_1": {"id": "ps_1", "name": "Shoes"}},
        edges={("cat_1", "product_sets"): {"data": [{"id": "ps_1"}], "summary": {"total_count": 1}}},
    ))
    by_catalog = _run(catalogs.list_product_sets(catalog_id="cat_1", limit=5))
    assert client.calls[0][1]["edge"] == "product_sets"
    assert client.calls[0][1]["params"] == {"limit": 5, "summary": "total_count"}
    assert client.calls[0][1]["fields"] == catalogs.PRODUCT_SET_FIELDS
    assert by_catalog["summary"]["total_count"] == 1
    one = _run(catalogs.list_product_sets(product_set_id="ps_1"))
    assert client.calls[1][0] == "get"
    assert one["items"] == [{"id": "ps_1", "name": "Shoes"}]
    _run(catalogs.list_product_sets(product_id="p_1"))
    assert (client.calls[2][1]["parent_id"], client.calls[2][1]["edge"]) == ("p_1", "product_sets")
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="exactly one of catalog_id, product_set_id, or product_id"):
        _run(catalogs.list_product_sets(catalog_id="cat_1", product_id="p_1"))


def test_create_product_set_posts_filter(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    result = _run(catalogs.create_product_set(
        catalog_id="cat_1", name="Shoes", filter={"product_type": {"i_contains": "shoe"}},
        params={"retailer_id": "set-1"},
    ))
    assert client.calls == [("create", {
        "parent_id": "cat_1", "edge": "product_sets",
        "data": {"name": "Shoes", "filter": {"product_type": {"i_contains": "shoe"}}, "retailer_id": "set-1"},
    })]
    assert result["created"] == {"id": "new_1"}


def test_create_product_set_accepts_json_string_and_validates(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _run(catalogs.create_product_set(catalog_id="cat_1", name="All", filter="{}"))
    assert client.calls[0][1]["data"]["filter"] == {}
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="filter"):
        _run(catalogs.create_product_set(catalog_id="cat_1", name="X", filter="nope"))
    with pytest.raises(ValidationError, match="name"):
        _run(catalogs.create_product_set(catalog_id="cat_1", name="", filter={}))


def test_update_product_set_reads_then_updates(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={
        "ps_1": {"id": "ps_1", "name": "Old", "filter": '{"brand":{"eq":"A"}}', "product_count": 4},
    }))
    result = _run(catalogs.update_product_set(product_set_id="ps_1", filter={"brand": {"eq": "B"}}))
    assert client.kinds() == ["get", "update"]
    assert client.calls[1][1] == {"object_id": "ps_1", "data": {"filter": {"brand": {"eq": "B"}}}}
    assert result["previous"]["filter"] == '{"brand":{"eq":"A"}}'
    assert result["previous"]["product_count"] == 4
    assert result["current"] == {"filter": {"brand": {"eq": "B"}}}


def test_update_product_set_requires_a_change(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="At least one"):
        _run(catalogs.update_product_set(product_set_id="ps_1"))


def test_delete_product_set_reads_then_deletes(monkeypatch) -> None:
    product_set = {"id": "ps_1", "name": "Shoes", "filter": "{}", "product_count": 3}
    client = _use(monkeypatch, FakeCatalogClient(objects={"ps_1": product_set}))
    result = _run(catalogs.delete_product_set(product_set_id="ps_1"))
    assert client.kinds() == ["get", "delete"]
    assert result["previous"] == product_set
    assert result["current"] == {"deleted": True}


def test_delete_product_set_read_failure_sends_no_delete(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(get_errors={"ps_1": _permission_error()}))
    with pytest.raises(MetaApiError):
        _run(catalogs.delete_product_set(product_set_id="ps_1"))
    assert client.kinds() == ["get"]


# ---------------------------------------------------------------- registry


WRITE_TOOLS = {
    "create_catalog", "update_catalog", "create_catalog_product", "update_catalog_product",
    "delete_catalog_product", "create_product_set", "update_product_set", "delete_product_set",
}
READ_TOOLS = {
    "list_catalogs", "list_dpa_eligible_catalogs", "get_catalog_diagnostics",
    "get_catalog_dynamic_ads_health", "list_catalog_data_sources", "list_catalog_products",
    "build_product_set_filter", "list_product_sets",
}


def test_catalog_tools_are_grouped_by_effect() -> None:
    from meta_ads_mcp.read_only import read_only_tool_names
    from meta_ads_mcp.tools import utility

    assert set(utility.TOOL_GROUPS["catalog"]) >= READ_TOOLS
    assert WRITE_TOOLS <= set(utility.TOOL_GROUPS["writes"])
    assert not WRITE_TOOLS & read_only_tool_names()
    assert READ_TOOLS <= read_only_tool_names()


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
@pytest.mark.parametrize("routed", [True, False])
def test_catalog_writes_are_blocked_in_read_only(monkeypatch, name, routed) -> None:
    from fastmcp.exceptions import ToolError

    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.coordinator import mcp_server

    _no_client(monkeypatch)
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    try:
        tool = "call_tool" if routed else name
        arguments = {"name": name, "arguments": {}} if routed else {}
        with pytest.raises(ToolError, match="META_READ_ONLY"):
            asyncio.run(mcp_server.call_tool(tool, arguments))
    finally:
        monkeypatch.delenv("META_READ_ONLY")
        reload_settings()
