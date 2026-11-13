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
        create_responses: dict[tuple[str, str], Any] | None = None,
        create_errors: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self.objects = objects or {}
        self.edges = edges or {}
        self.get_errors = get_errors or {}
        self.edge_errors = edge_errors or {}
        self.create_responses = create_responses or {}
        self.create_errors = create_errors or {}
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
        if (parent_id, edge) in self.create_errors:
            raise self.create_errors[(parent_id, edge)]
        return self.create_responses.get((parent_id, edge), {"id": "new_1"})

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
    assert call["fields"] == ["id", "name", "vertical", "product_count", "feed_count", "is_catalog_segment"]
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


def test_dpa_catalog_with_no_product_sets_on_a_fully_read_edge_is_not_ready(monkeypatch) -> None:
    """An empty, fully read product_sets edge means zero sets, not an unknown that passes."""
    client = _use(monkeypatch, FakeCatalogClient(edges={
        ("biz_1", "owned_product_catalogs"): {"data": [{"id": "cat_1", "product_count": 5}]},
        ("cat_1", "product_sets"): {"data": []},
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1"}]},
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    finding = result["findings"][0]
    assert finding["signals"]["product_set_count"] == 0
    assert finding["assessment"] == "needs_attention"
    assert result["recommended_catalog_id"] is None
    assert any("no product sets" in reason for reason in finding["reasons"])
    assert client.kinds().count("list") == 4


def test_dpa_product_set_count_comes_from_total_count_when_meta_returns_it(monkeypatch) -> None:
    client = _use(monkeypatch, _dpa_client(edges={
        ("cat_ready", "product_sets"): {"data": [{"id": "ps_1"}], "summary": {"total_count": 3},
                                        "paging": {"next": "n", "cursors": {"after": "s2"}}},
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["signals"]["product_set_count"] == 3
    assert ready["assessment"] == "ready"
    set_calls = [call for _, call in client.calls if call.get("edge") == "product_sets" and call["parent_id"] == "cat_ready"]
    assert len(set_calls) == 1


def test_dpa_paged_diagnostics_do_not_pass_readiness(monkeypatch) -> None:
    """A must-fix group on an unread diagnostics page must not be assumed absent."""
    client = _use(monkeypatch, _dpa_client(edges={
        ("cat_ready", "diagnostics"): [
            {"data": [], "paging": {"next": "n", "cursors": {"after": "d2"}}},
            {"data": [{"type": "MISSING_IMAGE", "severity": "MUST_FIX", "number_of_affected_items": 9}]},
        ],
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["signals"]["da_must_fix_groups"] == 1
    assert ready["signals"]["da_must_fix_affected_items"] == 9
    assert ready["assessment"] == "needs_attention"
    diag_calls = [call for _, call in client.calls if call.get("edge") == "diagnostics" and call["parent_id"] == "cat_ready"]
    assert [call["params"].get("after") for call in diag_calls] == [None, "d2"]


def test_dpa_partly_read_diagnostics_make_the_catalog_unknown(monkeypatch) -> None:
    stalled = {"data": [], "paging": {"next": "n", "cursors": {"after": "more"}}}
    _use(monkeypatch, _dpa_client(edges={("cat_ready", "diagnostics"): [stalled, stalled, stalled]}))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["assessment"] == "unknown"
    assert ready["signals"]["da_must_fix_groups"] is None
    assert ready["partial_signals"][0]["signal"] == "diagnostics"
    assert any("cat_ready: diagnostics was only partly read" in entry for entry in result["missing_signals"])
    assert result["recommended_catalog_id"] is None


@pytest.mark.parametrize("edge", ["product_sets", "external_event_sources"])
def test_dpa_partly_read_empty_supporting_edge_is_unknown_not_zero(monkeypatch, edge) -> None:
    _use(monkeypatch, _dpa_client(edges={
        ("cat_ready", edge): {"data": [], "paging": {"next": "https://graph.invalid/?after=unextracted"}},
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["assessment"] == "unknown"
    assert ready["partial_signals"][0]["signal"] == edge
    assert ready["partial_signals"][0]["count_is_lower_bound"] is False
    assert any(f"cat_ready: {edge} was only partly read" in entry for entry in result["missing_signals"])


def test_dpa_partly_read_nonempty_sources_count_as_a_lower_bound(monkeypatch) -> None:
    _use(monkeypatch, _dpa_client(edges={
        ("cat_ready", "external_event_sources"): [
            {"data": [{"id": "px_1"}], "paging": {"next": "n", "cursors": {"after": "e2"}}},
            {"data": [{"id": "px_2"}], "paging": {"next": "n"}},
        ],
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["signals"]["event_source_count"] == 2
    assert ready["partial_signals"][0]["count_is_lower_bound"] is True
    assert ready["assessment"] == "ready"
    assert any("lower bound" in reason for reason in ready["reasons"])
    assert any("external_event_sources was only partly read" in entry for entry in result["missing_signals"])


def test_dpa_requests_only_the_diagnostic_fields_it_counts(monkeypatch) -> None:
    client = _use(monkeypatch, _dpa_client())
    _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    diag_calls = [call for kind, call in client.calls if kind == "list" and call["edge"] == "diagnostics"]
    assert diag_calls
    for call in diag_calls:
        assert call["fields"] == ["type", "severity", "number_of_affected_items"]
        assert call["params"] == {"affected_channels": ["da"], "severities": ["MUST_FIX"], "limit": 25}


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


def test_dpa_incomplete_catalog_list_recommends_nothing(monkeypatch) -> None:
    """One ready catalog on a partly read list is not 'the only catalog that passed'."""
    listing = {
        "data": [{"id": "cat_ready", "name": "Ready", "product_count": 500}],
        "paging": {"next": "https://graph.invalid/?after=unextracted"},
    }
    _use(monkeypatch, _dpa_client(edges={("biz_1", "owned_product_catalogs"): listing}))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert result["findings"][0]["assessment"] == "ready"
    assert result["recommended_catalog_id"] is None
    assert "incomplete" in result["recommendation_reason"]


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
    assert call["fields"] == [
        "type", "severity", "title", "subtitle", "affected_channels", "affected_entity",
        "affected_features", "error_code", "number_of_affected_items",
        "number_of_affected_entities", "diagnostics",
    ]
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


def test_dynamic_ads_health_partly_read_diagnostics_is_inconclusive(monkeypatch) -> None:
    stalled = {"data": [], "paging": {"next": "n", "cursors": {"after": "more"}}}
    client = _use(monkeypatch, _health_client(edges={("cat_1", "diagnostics"): [stalled, stalled]}))
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))
    assert result["status"] == "inconclusive"
    assert any("product_catalog/diagnostics was only partly read" in entry for entry in result["missing_signals"])
    diag_calls = [call for _, call in client.calls if call.get("edge") == "diagnostics"]
    assert [call["params"].get("after") for call in diag_calls] == [None, "more"]


def test_dynamic_ads_health_reads_must_fix_groups_on_later_pages(monkeypatch) -> None:
    _use(monkeypatch, _health_client(edges={("cat_1", "diagnostics"): [
        {"data": [], "paging": {"next": "n", "cursors": {"after": "d2"}}},
        {"data": [{"title": "Images missing", "severity": "MUST_FIX", "number_of_affected_items": 7}]},
    ]}))
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))
    assert result["status"] == "at_risk"
    assert result["metrics"]["must_fix_groups"] == 1
    assert result["missing_signals"] == []


def test_dynamic_ads_health_partly_read_empty_sources_are_not_reported_as_none(monkeypatch) -> None:
    _use(monkeypatch, _health_client(edges={
        ("cat_1", "external_event_sources"): {"data": [], "paging": {"next": "https://graph.invalid/?after=x"}},
    }))
    result = _run(catalogs.get_catalog_dynamic_ads_health(catalog_id="cat_1"))
    assert result["status"] == "inconclusive"
    assert result["metrics"]["event_source_count"] is None
    assert not any(finding["source"] == "product_catalog/external_event_sources" for finding in result["findings"])
    assert any("external_event_sources was only partly read" in entry for entry in result["missing_signals"])


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
    assert call["fields"] == ["id", "name", "ingestion_source_type", "upload_type", "app_id"]
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
    assert call["fields"] == [
        "id", "retailer_id", "name", "availability", "price", "currency", "sale_price",
        "url", "image_url", "brand", "visibility", "review_status",
    ]
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


_CREATE_PRODUCT_ARGUMENTS = {
    "catalog_id": "cat_1", "retailer_id": "SKU-1", "name": "Shoe", "price": 10, "currency": "USD",
    "url": "https://shop/x", "image_url": "https://shop/x.png",
}


def _call_mcp(tool: str, arguments: dict[str, Any], *, routed: bool):
    from meta_ads_mcp.coordinator import mcp_server

    if routed:
        return asyncio.run(mcp_server.call_tool("call_tool", {"name": tool, "arguments": arguments}))
    return asyncio.run(mcp_server.call_tool(tool, arguments))


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize(("tool", "arguments"), [
    ("create_catalog_product", {**_CREATE_PRODUCT_ARGUMENTS, "price": True}),
    ("create_catalog_product", {**_CREATE_PRODUCT_ARGUMENTS, "price": False}),
    ("create_catalog_product", {**_CREATE_PRODUCT_ARGUMENTS, "sale_price": True}),
    ("update_catalog_product", {"product_id": "p_1", "price": True}),
    ("update_catalog_product", {"product_id": "p_1", "sale_price": False}),
])
def test_boolean_price_is_rejected_at_the_mcp_boundary(monkeypatch, tool, arguments, routed) -> None:
    """Pydantic would coerce true to 1.0; every catalog money input must refuse it before any call."""
    from fastmcp.exceptions import ToolError
    from fastmcp.exceptions import ValidationError as MCPValidationError

    client = _use(monkeypatch, FakeCatalogClient())
    with pytest.raises((ToolError, MCPValidationError)):
        _call_mcp(tool, arguments, routed=routed)
    assert client.calls == []


@pytest.mark.parametrize("routed", [False, True])
def test_numeric_price_still_passes_the_mcp_boundary(monkeypatch, routed) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _call_mcp("create_catalog_product", {**_CREATE_PRODUCT_ARGUMENTS, "price": 12.5, "sale_price": "9.99"},
              routed=routed)
    assert client.calls[0][1]["data"]["price"] == 1250
    assert client.calls[0][1]["data"]["sale_price"] == 999


def test_boolean_price_is_rejected_on_direct_calls(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="price"):
        _run(catalogs.create_catalog_product(**{**_CREATE_PRODUCT_ARGUMENTS, "price": True}))
    with pytest.raises(ValidationError, match="sale_price"):
        _run(catalogs.update_catalog_product(product_id="p_1", sale_price=True))


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


class _ProjectingCatalogClient(FakeCatalogClient):
    """Returns only the requested fields, as the Graph API does."""

    async def get_object(self, object_id, *, fields=None, params=None):
        source = await super().get_object(object_id, fields=fields, params=params)
        return {key: value for key, value in source.items() if key in (fields or [])}


def test_update_catalog_product_params_field_is_read_before_the_update(monkeypatch) -> None:
    client = _use(monkeypatch, _ProjectingCatalogClient(objects={"p_1": {"id": "p_1", "inventory": 100}}))
    result = _run(catalogs.update_catalog_product(product_id="p_1", params={"inventory": 5}))
    assert client.kinds() == ["get", "update"]
    assert "inventory" in client.calls[0][1]["fields"]
    assert result["previous"] == {"inventory": 100}
    assert result["current"] == {"inventory": 5}
    assert "previous_unavailable" not in result


def test_update_catalog_product_unreadable_params_field_is_marked_unavailable(monkeypatch) -> None:
    client = _use(monkeypatch, _ProjectingCatalogClient(objects={"p_1": {"id": "p_1", "name": "Old"}}))
    result = _run(catalogs.update_catalog_product(
        product_id="p_1", name="New", params={"return_policy_days": 30},
    ))
    assert "return_policy_days" not in client.calls[0][1]["fields"]
    assert result["previous"] == {"name": "Old"}
    assert "return_policy_days" not in result["previous"]
    assert set(result["previous_unavailable"]) == {"return_policy_days"}
    assert result["sent"] == {"name": "New", "return_policy_days": 30}


def test_update_catalog_unreadable_params_setting_is_marked_unavailable(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogClient(objects={"cat_1": {"id": "cat_1", "name": "Old"}}))
    result = _run(catalogs.update_catalog(
        catalog_id="cat_1", params={"destination_catalog_settings": {"generate_items_from_pages": True}},
    ))
    assert "destination_catalog_settings" not in result["previous"]
    assert set(result["previous_unavailable"]) == {"destination_catalog_settings"}


def test_g21_catalog_settings_previous_is_read_when_readable(monkeypatch) -> None:
    client = _use(monkeypatch, _ProjectingCatalogClient(objects={
        "10": {"id": "10", "name": "Main", "store_catalog_settings": {"page_id": "20"}},
    }))
    result = _run(catalogs.update_catalog("10", params={"store_catalog_settings": {"page_id": "21"}}))
    assert "store_catalog_settings" in client.calls[0][1]["fields"]
    assert result["previous"].get("store_catalog_settings") == {"page_id": "20"}, result
    assert result["previous"]["name"] == "Main"
    assert "previous_unavailable" not in result
    assert client.calls[1] == ("update", {"object_id": "10", "data": {"store_catalog_settings": {"page_id": "21"}}})


def test_update_catalog_checkout_change_reads_documented_checkout_urls(monkeypatch) -> None:
    urls = [{"country": "US", "url": "https://shop.example/checkout"}]
    client = _use(monkeypatch, _ProjectingCatalogClient(objects={"10": {"id": "10", "checkout_urls": urls}}))
    result = _run(catalogs.update_catalog("10", params={
        "checkout_url": "https://shop.example/new", "checkout_country": "US",
    }))
    assert client.calls[0][1]["fields"].count("checkout_urls") == 1
    assert result["previous"]["checkout_urls"] == urls
    assert "previous_unavailable" not in result


def test_update_catalog_old_style_call_reads_the_same_fields(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={"10": {"id": "10", "name": "Old"}}))
    result = _run(catalogs.update_catalog("10", name="New"))
    assert client.calls == [
        ("get", {"object_id": "10", "fields": catalogs.CATALOG_UPDATE_READ_FIELDS, "params": None}),
        ("update", {"object_id": "10", "data": {"name": "New"}}),
    ]
    assert set(result["previous"]) == {"name", "vertical", "da_display_settings", "default_image_url",
                                       "fallback_image_url"}


class _FieldRefusingClient(_ProjectingCatalogClient):
    """Rejects any read naming one of refused, as Meta does for a field it will not read."""

    def __init__(self, refused: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.refused = refused

    async def get_object(self, object_id, *, fields=None, params=None):
        if self.refused & set(fields or []):
            self.calls.append(("get", {"object_id": object_id, "fields": fields, "params": params}))
            raise MetaApiError(message="(#100) Tried accessing nonexisting field", status_code=400, code=100)
        return await super().get_object(object_id, fields=fields, params=params)


def test_update_catalog_refused_sdk_only_read_still_updates_and_says_so(monkeypatch) -> None:
    client = _use(monkeypatch, _FieldRefusingClient(
        {"store_catalog_settings"}, objects={"10": {"id": "10", "name": "Main"}},
    ))
    result = _run(catalogs.update_catalog("10", params={"store_catalog_settings": {"page_id": "21"}}))
    assert client.kinds() == ["get", "get", "update"]
    assert client.calls[1][1]["fields"] == catalogs.CATALOG_UPDATE_READ_FIELDS
    assert "store_catalog_settings" not in result["previous"]
    assert "refused" in result["previous_unavailable"]["store_catalog_settings"]
    assert result["previous"]["name"] == "Main"


def test_update_catalog_base_read_failure_writes_nothing(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(get_errors={"10": _permission_error()}))
    with pytest.raises(MetaApiError):
        _run(catalogs.update_catalog("10", params={"store_catalog_settings": {"page_id": "21"}}))
    assert "update" not in client.kinds()


def test_update_catalog_never_shows_partner_access_token(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={"10": {"id": "10"}}))
    settings = {"external_access_token": "synthetic-partner-secret", "external_merchant_id": "m1"}
    result = _run(catalogs.update_catalog("10", params={"partner_integration": settings}))
    assert client.calls[-1][1]["data"]["partner_integration"] == settings
    assert "synthetic-partner-secret" not in json.dumps(result)
    assert result["current"]["partner_integration"]["external_merchant_id"] == "m1"


def test_update_catalog_product_sdk_only_field_is_read_and_tolerates_refusal(monkeypatch) -> None:
    client = _use(monkeypatch, _ProjectingCatalogClient(objects={"p_1": {"id": "p_1", "custom_number_0": 7}}))
    result = _run(catalogs.update_catalog_product(product_id="p_1", params={"custom_number_0": 9}))
    assert result["previous"] == {"custom_number_0": 7}
    assert "previous_unavailable" not in result

    refusing = _use(monkeypatch, _FieldRefusingClient(
        {"custom_number_0"}, objects={"p_1": {"id": "p_1", "name": "Old", "custom_number_0": 7}},
    ))
    result = _run(catalogs.update_catalog_product(product_id="p_1", name="New", params={"custom_number_0": 9}))
    assert refusing.kinds() == ["get", "get", "update"]
    assert refusing.calls[1][1]["fields"] == catalogs.PRODUCT_DETAIL_FIELDS
    assert result["previous"] == {"name": "Old"}
    assert "refused" in result["previous_unavailable"]["custom_number_0"]
    assert result["sent"] == {"name": "New", "custom_number_0": 9}


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
    assert client.calls[0][1]["fields"] == [
        "id", "retailer_id", "name", "availability", "price", "currency", "sale_price",
        "url", "image_url", "brand", "visibility", "review_status", "description", "condition",
        "product_type", "category", "retailer_product_group_id", "additional_image_urls",
        "custom_label_0", "custom_label_1", "custom_label_2", "custom_label_3", "custom_label_4",
    ]
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
    # The operator given is not repeated (it could be a misplaced personal value); its position is.
    assert "like" not in message
    assert "conditions[0].operator" in message
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
    assert call["fields"] == ["id", "retailer_id", "name", "price", "availability"]
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


def test_filter_builder_points_price_comparisons_to_price_amount(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="price_amount"):
        _run(catalogs.build_product_set_filter(
            catalog_id="cat_1", conditions=[{"field": "price", "operator": "lt", "value": 100}],
        ))


def test_plain_language_request_maps_onto_attribute_values_and_filter_builder(monkeypatch) -> None:
    """'blue shirts under 100 dollars': read real values, then build one filter from them."""
    pages = {
        "product_type": [{"id": "1", "product_type": "Shirts"}, {"id": "2", "product_type": "Shoes"}],
        "color": [{"id": "1", "color": "Navy Blue"}, {"id": "2", "color": "Black"}],
        "currency": [{"id": "1", "currency": "USD"}, {"id": "2", "currency": "USD"}],
    }

    class Client(FakeCatalogClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
            if "filter" in (params or {}):
                return {"data": [{"id": "1", "name": "Oxford shirt"}], "summary": {"total_count": 1}}
            return {"data": pages[fields[1]]}

    _use(monkeypatch, Client())
    found = {
        attribute: [entry["value"] for entry in
                    _run(catalogs.list_catalog_attribute_values("123", attribute))["values"]]
        for attribute in ("product_type", "color", "currency")
    }
    assert "Shirts" in found["product_type"] and "Navy Blue" in found["color"] and found["currency"] == ["USD"]
    result = _run(catalogs.build_product_set_filter(catalog_id="123", conditions=[
        {"field": "product_type", "operator": "i_contains", "value": "shirt"},
        {"field": "color", "operator": "i_contains", "value": "blue"},
        {"field": "price_amount", "operator": "lt", "value": 10000},
        {"field": "currency", "operator": "eq", "value": "USD"},
    ]))
    assert result["filter"] == {"and": [
        {"product_type": {"i_contains": "shirt"}}, {"color": {"i_contains": "blue"}},
        {"price_amount": {"lt": 10000}}, {"currency": {"eq": "USD"}},
    ]}
    assert result["match_count"] == 1


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
    assert client.calls[0][1]["fields"] == ["id", "name", "filter", "product_count", "retailer_id"]
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


# ------------------------------------------------- batch_update_catalog_products


def _batch_item(method: str, retailer_id: str, **values: Any) -> dict[str, Any]:
    return {"method": method, "retailer_id": retailer_id, **values}


def _full_create(retailer_id: str, **overrides: Any) -> dict[str, Any]:
    item = _batch_item(
        "create", retailer_id, name="Shoe", description="Running shoe", url="https://shop.example/s",
        image_url="https://shop.example/s.png", brand="Acme", availability="in stock",
        condition="new", price=12.5, currency="usd",
    )
    item.update(overrides)
    return item


def _existing(retailer_id: str, product_id: str, **values: Any) -> dict[str, Any]:
    return {"id": product_id, "retailer_id": retailer_id, "name": "Old", "price": "$10.00", **values}


def _batch_client(existing: list[dict[str, Any]] | None = None, *, post: Any = None,
                  status: Any = None, **kwargs) -> FakeCatalogClient:
    edges = {("123", "products"): {"data": existing or []}}
    if status is not None:
        edges[("123", "check_batch_request_status")] = status
    edges.update(kwargs.pop("edges", {}))
    responses = {("123", "items_batch"): post if post is not None else {"handles": ["h1"], "validation_status": []}}
    return FakeCatalogClient(edges=edges, create_responses=responses, **kwargs)


def _no_pause(monkeypatch) -> list[float]:
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(catalogs, "_pause", pause)
    return pauses


def _finished(**row: Any) -> dict[str, Any]:
    return {"data": [{"handle": "h1", "status": "finished", "errors_total_count": 0, **row}]}


def test_batch_products_dry_run_sends_nothing(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([_existing("SKU-2", "p2")]))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123",
        items=[_full_create("SKU-1"), _batch_item("update", "SKU-2", price=9.99, currency="USD")],
        dry_run=True,
    ))
    assert "create" not in client.kinds()
    assert all(call["edge"] != "check_batch_request_status" for kind, call in client.calls if kind == "list")
    assert result["dry_run"] is True
    request = result["request"]
    assert request["endpoint"] == "POST /123/items_batch"
    assert request["data"]["item_type"] == "PRODUCT_ITEM"
    assert request["data"]["allow_upsert"] is False
    assert request["data"]["requests"] == [
        {"method": "CREATE", "data": {
            "id": "SKU-1", "title": "Shoe", "description": "Running shoe", "link": "https://shop.example/s",
            "image": [{"url": "https://shop.example/s.png"}], "brand": "Acme", "availability": "in stock",
            "condition": "new", "price": "12.50 USD",
        }},
        {"method": "UPDATE", "data": {"id": "SKU-2", "price": "9.99 USD"}},
    ]
    outcomes = {item["retailer_id"]: item["outcome"] for item in result["items"]}
    assert outcomes == {"SKU-1": "would_send", "SKU-2": "would_send"}
    update = next(item for item in result["items"] if item["retailer_id"] == "SKU-2")
    assert update["previous"]["price"] == "$10.00"
    assert update["product_id"] == "p2"


def test_batch_products_reads_existing_skus_with_one_filtered_request(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([_existing("SKU-2", "p2")], status=_finished()))
    _run(catalogs.batch_update_catalog_products(
        catalog_id="123",
        items=[_batch_item("update", "SKU-2", name="New"), _batch_item("delete", "SKU-3")],
    ))
    read = client.calls[0][1]
    assert (read["parent_id"], read["edge"]) == ("123", "products")
    assert read["params"]["filter"] == {"retailer_id": {"is_any": ["SKU-2", "SKU-3"]}}
    assert "product_feed" in read["fields"]
    assert "retailer_id" in read["fields"]


def test_batch_products_partial_failure_reports_each_item(monkeypatch) -> None:
    _no_pause(monkeypatch)
    status = _finished(
        errors_total_count=1,
        errors=[{"line": 2, "id": "SKU-2", "message": "Invalid price"}],
        warnings=[{"line": 1, "id": "SKU-1", "message": "Missing GTIN"}],
        ids_of_invalid_requests=["SKU-2"],
    )
    client = _use(monkeypatch, _batch_client(
        [_existing("SKU-1", "p1"), _existing("SKU-2", "p2"), _existing("SKU-3", "p3")],
        post={"handles": ["h1"], "validation_status": [
            {"retailer_id": "SKU-1", "errors": [], "warnings": [{"message": "Short title"}]},
        ]},
        status=status,
    ))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123",
        items=[
            _batch_item("update", "SKU-1", name="A"),
            _batch_item("update", "SKU-2", price=5, currency="USD"),
            _batch_item("delete", "SKU-3"),
        ],
    ))
    by_sku = {item["retailer_id"]: item for item in result["items"]}
    assert by_sku["SKU-1"]["outcome"] == "applied"
    assert set(by_sku["SKU-1"]["warnings"]) == {"Short title", "Missing GTIN"}
    assert by_sku["SKU-2"]["outcome"] == "failed"
    assert by_sku["SKU-2"]["errors"] == ["Invalid price"]
    assert by_sku["SKU-3"]["outcome"] == "applied"
    assert result["summary"]["outcomes"] == {"applied": 2, "failed": 1}
    assert result["batch"]["status"] == "finished"
    assert result["batch"]["complete"] is True
    assert result["ok"] is False
    status_call = next(call for kind, call in client.calls if kind == "list" and call["edge"] == "check_batch_request_status")
    assert status_call["params"] == {"handle": "h1", "load_ids_of_invalid_requests": True}


def test_batch_products_unattributed_errors_leave_items_unknown(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [_existing("SKU-1", "p1"), _existing("SKU-2", "p2")],
        status=_finished(errors_total_count=1, errors=[], ids_of_invalid_requests=[]),
    ))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123",
        items=[_batch_item("update", "SKU-1", name="A"), _batch_item("update", "SKU-2", name="B")],
    ))
    assert [item["outcome"] for item in result["items"]] == ["outcome_unknown", "outcome_unknown"]
    assert "did not name" in result["items"][0]["reason"]


def test_batch_products_unfinished_batch_is_reported_as_processing(monkeypatch) -> None:
    pauses = _no_pause(monkeypatch)
    pending = {"data": [{"handle": "h1", "status": "started"}]}
    client = _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")], status=pending))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")], wait_seconds=6,
    ))
    checks = [call for kind, call in client.calls if kind == "list" and call["edge"] == "check_batch_request_status"]
    assert len(checks) == 3
    assert pauses == [2, 2, 2]
    assert result["items"][0]["outcome"] == "processing"
    one_handle = {
        "handle": "h1", "status": "started", "complete": False, "status_checks": 3,
        "errors_total_count": None, "warnings_total_count": None,
    }
    assert result["batch"] == {**one_handle, "handles": [one_handle]}
    assert result["retry"]["safe_now"] is False
    assert "handle" in result["retry"]["how"]


def test_batch_products_wait_zero_does_not_check_status(monkeypatch) -> None:
    pauses = _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")]))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")], wait_seconds=0,
    ))
    assert pauses == []
    assert all(call["edge"] != "check_batch_request_status" for kind, call in client.calls if kind == "list")
    assert result["items"][0]["outcome"] == "processing"


def test_batch_products_no_handle_means_nothing_ingested(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [_existing("SKU-1", "p1")],
        post={"handles": [], "validation_status": [{"retailer_id": "SKU-1", "errors": [{"message": "Bad"}]}]},
    ))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")],
    ))
    assert result["items"][0]["outcome"] == "not_applied"
    assert result["items"][0]["errors"] == ["Bad"]
    assert result["retry"]["safe_now"] is True


def test_batch_products_states_whether_a_retry_is_safe(monkeypatch) -> None:
    _no_pause(monkeypatch)
    timeout = MetaApiError(
        message="Meta API request failed before a response was received.",
        is_transient=True, mutation_outcome_unknown=True,
    )
    client = _use(monkeypatch, _batch_client(
        [_existing("SKU-1", "p1")], create_errors={("123", "items_batch"): timeout},
    ))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A"), _full_create("SKU-9")],
    ))
    assert client.kinds().count("create") == 1
    assert [item["outcome"] for item in result["items"]] == ["outcome_unknown", "outcome_unknown"]
    retry = result["retry"]
    assert retry["safe_now"] is True
    assert "half" in retry["how"]
    assert "skipped" in retry["why"] and "same values" in retry["why"]
    assert "one product per retailer id" in retry["why"]
    assert result["ok"] is False


def test_batch_products_refused_request_is_not_applied(monkeypatch) -> None:
    _no_pause(monkeypatch)
    refused = MetaApiError(message="(#100) Invalid parameter", status_code=400, code=100)
    _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")], create_errors={("123", "items_batch"): refused}))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")],
    ))
    assert result["items"][0]["outcome"] == "not_applied"
    assert result["error"]["code"] == 100
    assert result["retry"]["safe_now"] is True


def test_batch_products_retry_skips_work_already_done(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")]))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_full_create("SKU-1"), _batch_item("delete", "SKU-2")],
    ))
    by_sku = {item["retailer_id"]: item for item in result["items"]}
    assert by_sku["SKU-1"]["outcome"] == "skipped_already_exists"
    assert by_sku["SKU-1"]["product_id"] == "p1"
    assert by_sku["SKU-2"]["outcome"] == "skipped_not_found"
    assert "create" not in client.kinds()
    assert result["ok"] is True


def test_batch_products_update_of_missing_sku_is_skipped_unless_upsert(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([]))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-5", name="A")],
    ))
    assert result["items"][0]["outcome"] == "skipped_not_found"
    assert "create" not in client.kinds()
    client = _use(monkeypatch, _batch_client([], status=_finished()))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-5", name="A")], allow_upsert=True,
    ))
    post = next(call for kind, call in client.calls if kind == "create")
    assert post["data"]["allow_upsert"] is True
    assert result["items"][0]["outcome"] == "applied"


def test_batch_products_warn_when_a_feed_supplies_the_product(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [_existing("SKU-1", "p1", product_feed={"id": "feed_7"}), _existing("SKU-2", "p2")],
        status=_finished(),
    ))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123",
        items=[_batch_item("update", "SKU-1", name="A"), _batch_item("delete", "SKU-2")],
    ))
    by_sku = {item["retailer_id"]: item for item in result["items"]}
    assert "feed_7" in by_sku["SKU-1"]["feed_warning"]
    assert "overwrite" in by_sku["SKU-1"]["feed_warning"]
    assert "feed_warning" not in by_sku["SKU-2"]
    assert result["feed_check"]["status"] == "read"
    assert "not a guarantee" in result["feed_check"]["note"]


def test_batch_products_feed_field_rejection_falls_back_without_it(monkeypatch) -> None:
    _no_pause(monkeypatch)
    rejected = MetaApiError(message="(#100) Tried accessing nonexisting field (product_feed)", code=100)

    class Client(FakeCatalogClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            if edge == "products" and "product_feed" in (fields or []):
                self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
                raise rejected
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    client = _use(monkeypatch, Client(
        edges={("123", "products"): {"data": [_existing("SKU-1", "p1")]},
               ("123", "check_batch_request_status"): _finished()},
        create_responses={("123", "items_batch"): {"handles": ["h1"]}},
    ))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")],
    ))
    product_reads = [call for kind, call in client.calls if kind == "list" and call["edge"] == "products"]
    assert len(product_reads) == 2
    assert result["feed_check"]["status"] == "unavailable"
    assert result["items"][0]["outcome"] == "applied"


def test_batch_products_read_failure_writes_nothing(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client(edge_errors={("123", "products"): _permission_error()}))
    with pytest.raises(MetaApiError):
        _run(catalogs.batch_update_catalog_products(
            catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")],
        ))
    assert "create" not in client.kinds()


def test_batch_products_incomplete_read_writes_nothing(monkeypatch) -> None:
    _no_pause(monkeypatch)
    stalled = {"data": [_existing("SKU-1", "p1")], "paging": {"next": "https://graph/next"}}
    client = _use(monkeypatch, _batch_client(edges={("123", "products"): stalled}))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")],
    ))
    assert "create" not in client.kinds()
    assert result["ok"] is False
    assert result["sent"] is False
    assert "cursor" in result["reason"]


def test_batch_products_prices_use_major_units_and_reject_booleans(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")]))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123",
        items=[_batch_item("update", "SKU-1", price=14, sale_price=9.5, currency="gbp")],
        dry_run=True,
    ))
    assert result["request"]["data"]["requests"][0]["data"] == {
        "id": "SKU-1", "price": "14 GBP", "sale_price": "9.50 GBP",
    }
    assert result["items"][0]["changes"] == {"price": 14, "sale_price": 9.5, "currency": "GBP"}
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="price"):
        _run(catalogs.batch_update_catalog_products(
            catalog_id="123", items=[_batch_item("update", "SKU-1", price=True, currency="USD")],
        ))


def test_batch_products_extra_fields_pass_through_from_the_documented_list(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([_existing("SKU-1", "p1", custom_label_0="old")]))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123",
        items=[_batch_item("update", "SKU-1", fields={"custom_label_0": "winter", "shipping_weight": "1 kg"})],
        dry_run=True,
    ))
    assert result["request"]["data"]["requests"][0]["data"] == {
        "id": "SKU-1", "custom_label_0": "winter", "shipping_weight": "1 kg",
    }
    item = result["items"][0]
    assert item["previous"] == {"custom_label_0": "old"}
    assert item["previous_unavailable"] == ["shipping_weight"]
    assert "custom_label_0" in client.calls[0][1]["fields"]


@pytest.mark.parametrize(("items", "match"), [
    ([], "1 to 50"),
    ([{"method": "update", "retailer_id": f"S{i}", "name": "x"} for i in range(51)], "1 to 50"),
    ([{"method": "upsert", "retailer_id": "S1"}], "method"),
    ([{"method": "update", "retailer_id": " "}], "retailer_id"),
    ([{"method": "update", "retailer_id": "S1"}], "at least one"),
    ([{"method": "update", "retailer_id": "S1", "name": "a"}, {"method": "delete", "retailer_id": "S1"}], "more than once"),
    ([{"method": "delete", "retailer_id": "S1", "name": "a"}], "delete"),
    ([{"method": "create", "retailer_id": "S1", "name": "a"}], "missing"),
    ([{"method": "update", "retailer_id": "S1", "price": 5}], "currency"),
    ([{"method": "update", "retailer_id": "S1", "colour": "red"}], r"items\[0\] has 1 unsupported key "),
    ([{"method": "update", "retailer_id": "S1", "fields": {"title": "x"}}], r"title \(use name\)"),
    ([{"method": "update", "retailer_id": "S1", "fields": {"not_a_field": "x"}}], r"items\[0\]\.fields has 1 key"),
    ([{"method": "update", "retailer_id": "S1", "availability": "maybe"}], "availability"),
    (["S1"], "object"),
])
def test_batch_products_validate_before_any_request(monkeypatch, items, match) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=match):
        _run(catalogs.batch_update_catalog_products(catalog_id="123", items=items))


def test_batch_products_validate_options_before_any_request(monkeypatch) -> None:
    _no_client(monkeypatch)
    item = [{"method": "update", "retailer_id": "S1", "name": "a"}]
    with pytest.raises(ValidationError, match="wait_seconds"):
        _run(catalogs.batch_update_catalog_products(catalog_id="123", items=item, wait_seconds=61))
    with pytest.raises(ValidationError, match="either items or handle"):
        _run(catalogs.batch_update_catalog_products(catalog_id="123", items=item, handle="h1"))
    with pytest.raises(ValidationError, match="either items or handle"):
        _run(catalogs.batch_update_catalog_products(catalog_id="123"))


def test_batch_products_handle_reads_an_earlier_batch_without_writing(monkeypatch) -> None:
    pauses = _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client(status=_finished(
        errors_total_count=1, errors=[{"line": 1, "id": "SKU-1", "message": "Bad"}],
        ids_of_invalid_requests=["SKU-1"],
    )))
    result = _run(catalogs.batch_update_catalog_products(catalog_id="123", handle="h1"))
    assert client.kinds() == ["list"]
    assert pauses == []
    assert result["batch"]["status"] == "finished"
    assert result["batch"]["complete"] is True
    assert result["failed_retailer_ids"] == ["SKU-1"]
    assert result["errors"] == [{"line": 1, "id": "SKU-1", "message": "Bad"}]
    assert result["ok"] is False
    assert result["outcome"] == "some_failed"


_G7_STATUS_MATRIX = [
    ({"status": "finished", "errors_total_count": 0}, "applied"),
    ({"status": "finished", "errors_total_count": 1, "errors": [{"id": "bad", "message": "invalid"}],
      "ids_of_invalid_requests": ["bad"]}, "applied"),
    ({"status": "finished", "errors_total_count": 1, "errors": [{"message": "unattributed failure"}]},
     "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 2, "errors": [{"id": "bad", "message": "invalid"}],
      "ids_of_invalid_requests": ["bad"]}, "applied"),
    ({"status": "finished", "errors_total_count": 2,
      "errors": [{"id": "bad", "message": "first"}, {"id": "bad", "message": "second"}]}, "applied"),
    ({"status": "finished", "errors_total_count": 1}, "outcome_unknown"),
    ({"status": "finished"}, "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 0, "ids_of_invalid_requests": ["target"]}, "failed"),
    # Beyond the reviewer's matrix: the same rules hold for every shape of unnamed error.
    ({"status": "finished", "errors_total_count": 2, "errors": [{"id": "bad", "message": "invalid"},
                                                                {"message": "unattributed"}],
      "ids_of_invalid_requests": ["bad"]}, "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 0, "errors": [{"message": "unattributed"}]},
     "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 1, "errors": [{"id": " ", "message": "blank id"}]},
     "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 1, "errors": ["not an object"]}, "outcome_unknown"),
    ({"status": "finished", "errors": [{"id": "bad", "message": "named"}]}, "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 1, "errors": [{"id": "target", "message": "named"}]},
     "failed"),
    ({"status": "finished", "errors_total_count": 3, "errors": [{"id": "bad", "message": "a"}]},
     "outcome_unknown"),
]


@pytest.mark.parametrize(("status", "expected"), _G7_STATUS_MATRIX)
def test_g7_batch_success_requires_all_errors_to_be_attributed(status, expected) -> None:
    item = {"retailer_id": "target", "warnings": [], "errors": []}
    catalogs._settle(item, status)
    assert item["outcome"] == expected, item
    if expected == "outcome_unknown":
        assert item["reason"]


@pytest.mark.parametrize(("status", "expected"), _G7_STATUS_MATRIX)
def test_g7_submit_and_handle_paths_use_the_same_outcome_rules(monkeypatch, status, expected) -> None:
    """A batch whose only item is 'target' is ok on the handle path exactly when the item is applied."""
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client([_existing("target", "p1")], status={"data": [status]}))
    submitted = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "target", name="New")],
    ))
    assert submitted["items"][0]["outcome"] == expected
    _use(monkeypatch, _batch_client(status={"data": [status]}))
    read = _run(catalogs.batch_update_catalog_products(catalog_id="123", handle="h1"))
    named_failure = "target" in read["failed_retailer_ids"] or "bad" in read["failed_retailer_ids"]
    if expected == "applied" and not named_failure:
        assert read["ok"] is True and read["outcome"] == "all_applied"
    else:
        assert read["ok"] is False
    assert submitted["ok"] is (expected == "applied")


def test_g7_handle_read_does_not_call_unknown_error_count_success(monkeypatch) -> None:
    _use(monkeypatch, _batch_client(status={"data": [{
        "status": "finished", "errors": [{"message": "unattributed failure"}],
    }]}))
    result = _run(catalogs.batch_update_catalog_products("123", handle="synthetic-handle"))
    assert result["ok"] is False, result
    assert result["outcome"] == "outcome_unknown"
    assert result["error_attribution"]["complete"] is False
    assert "how many errors" in result["error_attribution"]["reason"]


def test_g7_handle_read_unfinished_batch_is_processing(monkeypatch) -> None:
    _use(monkeypatch, _batch_client(status={"data": [{"status": "started"}]}))
    result = _run(catalogs.batch_update_catalog_products("123", handle="h1"))
    assert result["ok"] is False
    assert result["outcome"] == "processing"
    assert result["error_attribution"]["complete"] is None


def test_batch_products_refused_sdk_only_read_marks_previous_unavailable(monkeypatch) -> None:
    _no_pause(monkeypatch)

    class Refusing(FakeCatalogClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            if edge == "products" and "custom_number_0" in (fields or []):
                self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
                raise MetaApiError(message="(#100) Tried accessing nonexisting field", status_code=400, code=100)
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    client = _use(monkeypatch, Refusing(
        edges={("123", "products"): {"data": [_existing("SKU-1", "p1", custom_number_0=4)]},
               ("123", "check_batch_request_status"): _finished()},
        create_responses={("123", "items_batch"): {"handles": ["h1"]}},
    ))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", fields={"custom_number_0": 5})],
    ))
    reads = [call for kind, call in client.calls if kind == "list" and call["edge"] == "products"]
    assert len(reads) == 2
    assert "custom_number_0" not in reads[1]["fields"] and "product_feed" not in reads[1]["fields"]
    item = result["items"][0]
    assert item["previous_unavailable"] == ["custom_number_0"]
    assert "custom_number_0" not in item["previous"]
    assert item["outcome"] == "applied"
    assert any("custom_number_0" in note for note in result["notes"])


def test_batch_products_sdk_only_field_previous_is_read(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client([_existing("SKU-1", "p1", quantity_to_sell_on_facebook=3)], status=_finished()))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", fields={"quantity_to_sell_on_facebook": 8})],
    ))
    assert result["items"][0]["previous"] == {"quantity_to_sell_on_facebook": 3}
    assert "previous_unavailable" not in result["items"][0]


# --------------------------------------------- list_catalog_products extensions


def test_list_catalog_products_old_style_call_is_unchanged(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _run(catalogs.list_catalog_products(catalog_id="cat_1", limit=10, after="c1"))
    assert client.calls == [("list", {
        "parent_id": "cat_1", "edge": "products",
        "fields": catalogs.PRODUCT_FIELDS,
        "params": {"limit": 10, "after": "c1", "summary": "total_count"},
    })]


def test_list_catalog_products_filters_by_diagnostic(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    result = _run(catalogs.list_catalog_products(
        product_set_id="ps_1", error_type="image_resolution_low", error_priority="high",
    ))
    params = client.calls[0][1]["params"]
    assert params["error_type"] == "IMAGE_RESOLUTION_LOW"
    assert params["error_priority"] == "HIGH"
    assert result["summary"]["scope"] == {
        "product_set_id": "ps_1", "error_type": "IMAGE_RESOLUTION_LOW", "error_priority": "HIGH",
    }


def test_list_catalog_products_product_group_view(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("cat_1", "product_groups"): {
        "data": [{"id": "g1", "retailer_id": "TEE"}], "summary": {"total_count": 7},
    }}))
    result = _run(catalogs.list_catalog_products(catalog_id="cat_1", view="product_groups", limit=5))
    call = client.calls[0][1]
    assert (call["parent_id"], call["edge"]) == ("cat_1", "product_groups")
    assert call["fields"] == ["id", "retailer_id", "variants"]
    assert call["params"] == {"limit": 5, "summary": "total_count"}
    assert result["summary"]["total_count"] == 7
    assert result["summary"]["scope"] == {"catalog_id": "cat_1", "view": "product_groups"}


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"product_id": "p", "error_type": "EMPTY_PRICE"}, "error_type"),
    ({"catalog_id": "c", "error_type": "bad value!"}, "error_type"),
    ({"catalog_id": "c", "error_priority": "urgent"}, "error_priority"),
    ({"catalog_id": "c", "view": "groups"}, "view"),
    ({"product_set_id": "p", "view": "product_groups"}, "catalog_id"),
    ({"catalog_id": "c", "view": "product_groups", "filter": {"brand": {"eq": "A"}}}, "product_groups"),
    ({"catalog_id": "c", "view": "product_groups", "error_type": "EMPTY_PRICE"}, "product_groups"),
])
def test_list_catalog_products_extension_validation(monkeypatch, kwargs, match) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=match):
        _run(catalogs.list_catalog_products(**kwargs))


# ------------------------------------------------ list_catalog_attribute_values


def _scan_pages(*pages: list[dict[str, Any]], total: int | None = None) -> list[dict[str, Any]]:
    payloads = []
    for index, rows in enumerate(pages):
        payload: dict[str, Any] = {"data": rows}
        if index + 1 < len(pages):
            payload["paging"] = {"next": "https://graph/next", "cursors": {"after": f"c{index + 1}"}}
        if total is not None:
            payload["summary"] = {"total_count": total}
        payloads.append(payload)
    return payloads


def test_attribute_values_counts_values_across_a_complete_scan(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("123", "products"): _scan_pages(
        [{"id": "1", "brand": "Acme"}, {"id": "2", "brand": "Zed"}],
        [{"id": "3", "brand": "Acme"}, {"id": "4"}, {"id": "5", "brand": " "}],
        total=5,
    )}))
    result = _run(catalogs.list_catalog_attribute_values(catalog_id="123", attribute="brand"))
    first = client.calls[0][1]
    assert first["fields"] == ["id", "brand"]
    assert first["params"] == {"limit": 100, "summary": "total_count"}
    assert client.calls[1][1]["params"]["after"] == "c1"
    assert result["values"] == [{"value": "Acme", "count": 2}, {"value": "Zed", "count": 1}]
    assert result["coverage"] == {
        "products_examined": 5, "products_in_scope": 5, "complete": True, "stop_reason": None,
        "requests": 2, "counts_are": "exact",
    }
    assert result["products_without_value"] == 2
    assert result["distinct_values"] == 2


class _ProjectingPagesClient(FakeCatalogClient):
    """Serves ("123", "products") pages from source rows, returning only the requested fields."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self.pages = pages

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
        after = (params or {}).get("after")
        index = int(after[1:]) if after else 0
        rows = [{key: value for key, value in row.items() if key in (fields or [])} for row in self.pages[index]]
        payload: dict[str, Any] = {"data": rows}
        if index + 1 < len(self.pages):
            payload["paging"] = {"next": "https://graph/next", "cursors": {"after": f"c{index + 1}"}}
        return payload


def test_g8_attribute_scan_counts_distinct_products_with_identical_values(monkeypatch) -> None:
    _use(monkeypatch, _ProjectingPagesClient([[{"id": "1", "brand": "Acme"}], [{"id": "2", "brand": "Acme"}]]))
    result = _run(catalogs.list_catalog_attribute_values("123", "brand"))
    assert result["coverage"]["complete"] is True, result
    assert result["values"] == [{"value": "Acme", "count": 2}], result
    assert result["coverage"]["products_examined"] == 2


def test_g8_attribute_scan_with_many_identical_pages_reaches_the_end(monkeypatch) -> None:
    pages = [[{"id": f"{page}-{row}", "availability": "in stock"} for row in range(3)] for page in range(4)]
    _use(monkeypatch, _ProjectingPagesClient(pages))
    result = _run(catalogs.list_catalog_attribute_values("123", "availability"))
    assert result["coverage"]["complete"] is True
    assert result["values"] == [{"value": "in stock", "count": 12}]


def test_g8_a_product_seen_again_is_counted_once_and_a_repeated_page_stops(monkeypatch) -> None:
    pages = [
        [{"id": "1", "brand": "Acme"}, {"id": "2", "brand": "Zed"}],
        [{"id": "2", "brand": "Zed"}, {"id": "3", "brand": "Acme"}],
        [{"id": "2", "brand": "Zed"}, {"id": "3", "brand": "Acme"}],
        [{"id": "4", "brand": "Acme"}],
    ]
    client = _use(monkeypatch, _ProjectingPagesClient(pages))
    result = _run(catalogs.list_catalog_attribute_values("123", "brand"))
    assert len(client.calls) == 3
    assert result["coverage"]["complete"] is False
    assert "already been read" in result["coverage"]["stop_reason"]
    assert result["values"] == [{"value": "Acme", "count": 2}, {"value": "Zed", "count": 1}]
    assert result["coverage"]["products_examined"] == 3


def test_g8_rows_without_ids_on_an_identical_page_stop_as_ambiguous(monkeypatch) -> None:
    """Diagnostics groups carry no id: an identical page is neither counted twice nor called a repeat."""
    group = {"type": "IMAGE", "severity": "MUST_FIX", "number_of_affected_items": 4}
    client = _dpa_client(edges={("cat_ready", "diagnostics"): [
        {"data": [group], "paging": {"next": "n", "cursors": {"after": "d1"}}},
        {"data": [group], "paging": {"next": "n", "cursors": {"after": "d2"}}},
    ]})
    _use(monkeypatch, client)
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["signals"]["da_must_fix_affected_items"] == 4
    # A must-fix group was read, so the catalog is not ready either way; the count is a lower bound.
    assert ready["assessment"] == "needs_attention"
    assert ready["partial_signals"][0]["count_is_lower_bound"] is True
    assert any("no id" in entry for entry in result["missing_signals"])


def test_g8_product_sets_with_new_ids_on_later_pages_are_all_counted(monkeypatch) -> None:
    client = _dpa_client(edges={("cat_ready", "product_sets"): [
        {"data": [{"id": "ps_1"}], "paging": {"next": "n", "cursors": {"after": "s1"}}},
        {"data": [{"id": "ps_2"}]},
    ]})
    _use(monkeypatch, client)
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["signals"]["product_set_count"] == 2
    assert ready["partial_signals"] == []


def test_attribute_values_scan_reports_coverage(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("123", "products"): _scan_pages(
        [{"id": str(i), "custom_label_0": "sale" if i % 2 else "core"} for i in range(100)],
        [{"id": str(i), "custom_label_0": "core"} for i in range(100, 200)],
        [{"id": "x", "custom_label_0": "never read"}],
        total=8391,
    )}))
    result = _run(catalogs.list_catalog_attribute_values(
        catalog_id="123", attribute="custom_label_0", max_products=200,
    ))
    assert len([call for kind, call in client.calls if kind == "list"]) == 2
    coverage = result["coverage"]
    assert coverage["products_examined"] == 200
    assert coverage["products_in_scope"] == 8391
    assert coverage["complete"] is False
    assert coverage["counts_are"] == "lower_bounds"
    assert "200" in coverage["stop_reason"]
    assert result["values"][0] == {"value": "core", "count": 150}
    assert any("200 of 8391" in note for note in result["notes"])


def test_attribute_values_verify_counts_reads_exact_totals(monkeypatch) -> None:
    pages = _scan_pages([{"id": "1", "brand": "Acme"}, {"id": "2", "brand": "Zed"}],
                        [{"id": "3", "brand": "Acme"}], total=9)

    class Client(FakeCatalogClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            if params and "filter" in params:
                self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
                value = params["filter"]["brand"]["eq"]
                return {"data": [], "summary": {"total_count": {"Acme": 6, "Zed": 3}[value]}}
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    client = _use(monkeypatch, Client(edges={("123", "products"): pages}))
    result = _run(catalogs.list_catalog_attribute_values(
        catalog_id="123", attribute="brand", max_products=200, verify_top=2,
    ))
    verify_calls = [call for kind, call in client.calls if "filter" in (call.get("params") or {})]
    assert verify_calls[0]["params"] == {"filter": {"brand": {"eq": "Acme"}}, "summary": "total_count", "limit": 1}
    assert result["values"] == [
        {"value": "Acme", "count": 2, "verified_count": 6},
        {"value": "Zed", "count": 1, "verified_count": 3},
    ]


def test_attribute_values_product_set_scope_and_validation(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    result = _run(catalogs.list_catalog_attribute_values(
        catalog_id="123", product_set_id="456", attribute="product_type",
    ))
    assert client.calls[0][1]["parent_id"] == "456"
    assert result["scope"] == {"catalog_id": "123", "product_set_id": "456", "attribute": "product_type"}
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="attribute"):
        _run(catalogs.list_catalog_attribute_values(catalog_id="123", attribute="url"))
    with pytest.raises(ValidationError, match="max_products"):
        _run(catalogs.list_catalog_attribute_values(catalog_id="123", attribute="brand", max_products=0))
    with pytest.raises(ValidationError, match="max_products"):
        _run(catalogs.list_catalog_attribute_values(catalog_id="123", attribute="brand", max_products=5001))
    with pytest.raises(ValidationError, match="verify_top"):
        _run(catalogs.list_catalog_attribute_values(catalog_id="123", attribute="brand", verify_top=11))


# --------------------------------------------- list_dpa_eligible_catalogs context


def test_dpa_without_context_reads_no_products_and_skips_empty_catalogs(monkeypatch) -> None:
    client = _use(monkeypatch, _dpa_client())
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    edges = [(call["parent_id"], call["edge"]) for kind, call in client.calls if kind == "list"]
    assert ("cat_ready", "products") not in edges
    assert all(parent != "cat_empty" for parent, _ in edges)
    assert all("context_match" not in finding for finding in result["findings"])


def test_dpa_skips_supporting_reads_for_catalogs_without_products(monkeypatch) -> None:
    client = _use(monkeypatch, _dpa_client())
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", ad_url="https://shop.example/x"))
    assert all(call["parent_id"] != "cat_empty" for kind, call in client.calls if kind == "list")
    empty = next(f for f in result["findings"] if f["catalog_id"] == "cat_empty")
    assert empty["assessment"] == "blocked"


def test_dpa_prefers_catalog_matching_the_ad_domain_and_pixel(monkeypatch) -> None:
    client = _use(monkeypatch, _dpa_client(edges={
        ("cat_nosrc", "external_event_sources"): {"data": [{"id": "777"}]},
        ("cat_ready", "products"): {"data": [{"id": "a", "url": "https://other.example/p"}]},
        ("cat_nosrc", "products"): {"data": [
            {"id": "b", "url": "https://www.shop.example/p1"}, {"id": "c", "url": "https://shop.example/p2"},
        ]},
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(
        business_id="biz_1", ad_url="https://www.shop.example/sale", pixel_id="777",
    ))
    assert [f["catalog_id"] for f in result["findings"]] == ["cat_nosrc", "cat_ready", "cat_empty"]
    top = result["findings"][0]
    assert top["context_match"]["ad_domain"] == {
        "domain": "shop.example", "sampled_urls": 2, "matching_urls": 2, "match": "yes",
    }
    assert top["context_match"]["pixel"] == {"pixel_id": "777", "connected": True}
    assert any("shop.example" in reason for reason in top["reasons"])
    assert result["recommended_catalog_id"] == "cat_nosrc"
    assert "ad_url" in result["recommendation_reason"]
    url_read = next(call for kind, call in client.calls if kind == "list" and call["edge"] == "products")
    assert url_read["fields"] == ["url"]
    assert url_read["params"] == {"limit": 5}


def test_dpa_context_with_no_matching_ready_catalog_recommends_nothing(monkeypatch) -> None:
    _use(monkeypatch, _dpa_client(edges={
        ("cat_ready", "products"): {"data": [{"id": "a", "url": "https://other.example/p"}]},
        ("cat_nosrc", "products"): {"data": []},
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", ad_url="shop.example"))
    assert result["recommended_catalog_id"] is None
    assert "matched" in result["recommendation_reason"]
    nosrc = next(f for f in result["findings"] if f["catalog_id"] == "cat_nosrc")
    assert nosrc["context_match"]["ad_domain"]["match"] == "unknown"


def test_dpa_context_validation(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="ad_url"):
        _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", ad_url="not a url"))
    with pytest.raises(ValidationError, match="ad_format"):
        _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", ad_format="CAROUSEL"))


def test_dpa_page_id_prefers_catalogs_the_page_lists(monkeypatch) -> None:
    client = _use(monkeypatch, _dpa_client(edges={
        ("cat_nosrc", "external_event_sources"): {"data": [{"id": "px_2"}]},
        ("555", "product_catalogs"): {"data": [{"id": "cat_nosrc"}, {"id": "cat_elsewhere"}]},
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", page_id="555"))
    page_read = next(call for kind, call in client.calls if kind == "list" and call["edge"] == "product_catalogs")
    assert page_read == {"parent_id": "555", "edge": "product_catalogs", "fields": ["id"], "params": {}}
    assert [f["catalog_id"] for f in result["findings"]][:2] == ["cat_nosrc", "cat_ready"]
    top = result["findings"][0]
    assert top["context_match"]["page"] == {"page_id": "555", "linked": True}
    assert any("Page 555" in reason for reason in top["reasons"])
    other = result["findings"][1]
    assert other["context_match"]["page"] == {"page_id": "555", "linked": False}
    assert result["recommended_catalog_id"] == "cat_nosrc"
    assert "page_id" in result["recommendation_reason"]
    assert result["basis"]["page_catalogs_not_evaluated"] == ["cat_elsewhere"]
    assert "page_id" in result["basis"]["rules"]


def test_dpa_page_read_failure_is_unknown_and_recommends_nothing(monkeypatch) -> None:
    _use(monkeypatch, _dpa_client(edge_errors={("555", "product_catalogs"): _permission_error()}))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", page_id="555"))
    ready = next(f for f in result["findings"] if f["catalog_id"] == "cat_ready")
    assert ready["context_match"]["page"] == {"page_id": "555", "linked": None}
    assert result["recommended_catalog_id"] is None
    assert any("Page 555" in entry and "could not be read" in entry for entry in result["missing_signals"])


def test_dpa_partly_read_page_catalogs_leave_unlisted_catalogs_unknown(monkeypatch) -> None:
    _use(monkeypatch, _dpa_client(edges={
        ("555", "product_catalogs"): {"data": [{"id": "cat_nosrc"}], "paging": {"next": "n"}},
    }))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", page_id="555"))
    by_id = {f["catalog_id"]: f for f in result["findings"]}
    assert by_id["cat_nosrc"]["context_match"]["page"]["linked"] is True
    assert by_id["cat_ready"]["context_match"]["page"]["linked"] is None
    assert any("partly read" in entry for entry in result["missing_signals"])


def test_dpa_product_extensions_do_not_require_an_event_source(monkeypatch) -> None:
    _use(monkeypatch, _dpa_client())
    default = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    assert {f["catalog_id"]: f["assessment"] for f in default["findings"]}["cat_nosrc"] == "needs_attention"
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", ad_format="pe"))
    by_id = {f["catalog_id"]: f for f in result["findings"]}
    assert by_id["cat_nosrc"]["assessment"] == "ready"
    assert any("does not list one as a requirement" in reason for reason in by_id["cat_nosrc"]["reasons"])
    assert by_id["cat_empty"]["assessment"] == "blocked"
    assert result["basis"]["ad_format"] == "PE"
    assert "product set" in result["basis"]["rules"]["ready"]
    assert result["recommended_catalog_id"] is None
    assert result["metrics"]["ready_count"] == 2


def test_dpa_product_extensions_still_need_a_product_set_and_readable_sets(monkeypatch) -> None:
    _use(monkeypatch, _dpa_client(
        edges={("cat_nosrc", "product_sets"): {"data": [], "summary": {"total_count": 0}}},
        edge_errors={("cat_ready", "external_event_sources"): _permission_error()},
    ))
    result = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", ad_format="PE"))
    by_id = {f["catalog_id"]: f for f in result["findings"]}
    assert by_id["cat_nosrc"]["assessment"] == "needs_attention"
    # An unreadable event source does not decide PE readiness, but is still reported.
    assert by_id["cat_ready"]["assessment"] == "ready"
    assert any("external_event_sources" in entry for entry in result["missing_signals"])
    assert result["recommended_catalog_id"] == "cat_ready"


def test_dpa_default_format_matches_an_explicit_a_plus_ca_call(monkeypatch) -> None:
    first = _use(monkeypatch, _dpa_client())
    default = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1"))
    second = _use(monkeypatch, _dpa_client())
    explicit = _run(catalogs.list_dpa_eligible_catalogs(business_id="biz_1", ad_format="A_PLUS_CA"))
    assert first.calls == second.calls
    assert default == explicit
    assert default["basis"]["ad_format"] == "A_PLUS_CA"


# ---------------------------------------------------------------- registry


WRITE_TOOLS = {
    "create_catalog", "update_catalog", "create_catalog_product", "update_catalog_product",
    "delete_catalog_product", "create_product_set", "update_product_set", "delete_product_set",
    "batch_update_catalog_products",
}
READ_TOOLS = {
    "list_catalogs", "list_dpa_eligible_catalogs", "get_catalog_diagnostics",
    "get_catalog_dynamic_ads_health", "list_catalog_data_sources", "list_catalog_products",
    "build_product_set_filter", "list_product_sets", "list_catalog_attribute_values",
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


def test_batch_item_with_submission_errors_is_not_called_applied() -> None:
    """Meta's answer to the submission listed errors for the item; a clean finished batch cannot overrule that."""
    clean = {"status": "finished", "errors_total_count": 0, "errors": []}
    flagged = {"retailer_id": "target", "warnings": [], "errors": ["price is missing"]}
    catalogs._settle(flagged, clean)
    assert flagged["outcome"] == "outcome_unknown"
    assert "when the batch was submitted" in flagged["reason"]
    assert flagged["errors"] == ["price is missing"]
    untouched = {"retailer_id": "other", "warnings": [], "errors": []}
    catalogs._settle(untouched, clean)
    assert untouched["outcome"] == "applied"


# ------------------------------------------------- round four: URL logins, submission errors, handles

_LOGIN_SECRET = "synthetic-url-password"
_LOGIN_URL = f"https://user:{_LOGIN_SECRET}@example.invalid/p"


@pytest.mark.parametrize("mode", ["single_update", "batch_preview"])
def test_h8_new_catalog_writes_refuse_login_urls(monkeypatch, mode) -> None:
    client = _use(monkeypatch, FakeCatalogClient(
        objects={"20": {"id": "20", "url": "https://example.invalid"}},
        edges={("10", "products"): {"data": [{"id": "20", "retailer_id": "sku"}]}},
    ))
    with pytest.raises(ValidationError) as info:
        if mode == "single_update":
            _run(catalogs.update_catalog_product("20", url=_LOGIN_URL))
        else:
            _run(catalogs.batch_update_catalog_products(
                "10", items=[{"method": "update", "retailer_id": "sku", "url": _LOGIN_URL}], dry_run=True,
            ))
    assert _LOGIN_SECRET not in str(info.value)
    assert client.calls == []


_LOGIN_INPUTS = [
    ("create_catalog_product", lambda: catalogs.create_catalog_product(
        "10", "sku", "Shoe", 10, "USD", "https://shop.example/s", _LOGIN_URL)),
    ("create_catalog_product.params", lambda: catalogs.create_catalog_product(
        "10", "sku", "Shoe", 10, "USD", "https://shop.example/s", "https://shop.example/s.png",
        params={"additional_image_urls": ["https://shop.example/a.png", _LOGIN_URL]})),
    ("update_catalog_product.image_url", lambda: catalogs.update_catalog_product("20", image_url=_LOGIN_URL)),
    ("update_catalog_product.params", lambda: catalogs.update_catalog_product(
        "20", params={"mobile_link": _LOGIN_URL})),
    ("update_catalog_product.text", lambda: catalogs.update_catalog_product(
        "20", description=f"See {_LOGIN_URL} for details")),
    ("batch.image_url", lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "sku", "image_url": _LOGIN_URL}], dry_run=True)),
    ("batch.additional_image_link", lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "sku",
                      "fields": {"additional_image_link": ["https://shop.example/a.png", _LOGIN_URL]}}],
        dry_run=True)),
    ("batch.video", lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "sku", "fields": {"video": [{"url": _LOGIN_URL}]}}],
        dry_run=True)),
    ("batch.applink", lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "sku",
                      "fields": {"applink": {"ios_url": "app://x", "web_url": _LOGIN_URL}}}], dry_run=True)),
    ("batch.create", lambda: catalogs.batch_update_catalog_products(
        "10", items=[_full_create("sku", url=_LOGIN_URL)], dry_run=True)),
    ("create_catalog.params", lambda: catalogs.create_catalog(
        "1", "Shop", params={"default_image_url": _LOGIN_URL})),
    ("update_catalog.params", lambda: catalogs.update_catalog(
        "10", params={"checkout_url": _LOGIN_URL, "checkout_country": "US"})),
    ("update_catalog.fallback", lambda: catalogs.update_catalog(
        "10", params={"fallback_image_url": [_LOGIN_URL]})),
    ("create_product_set.metadata", lambda: catalogs.create_product_set(
        "10", "Set", {}, params={"metadata": {"cover_image_url": _LOGIN_URL}})),
    ("create_product_set.filter", lambda: catalogs.create_product_set(
        "10", "Set", {"url": {"eq": _LOGIN_URL}})),
    ("update_product_set.filter", lambda: catalogs.update_product_set(
        "ps_1", filter={"url": {"i_contains": _LOGIN_URL}})),
    ("list_catalog_products.filter", lambda: catalogs.list_catalog_products(
        catalog_id="10", filter={"url": {"eq": _LOGIN_URL}})),
    ("build_product_set_filter", lambda: catalogs.build_product_set_filter(
        "10", [{"field": "url", "operator": "eq", "value": _LOGIN_URL}])),
    ("list_dpa_eligible_catalogs.ad_url", lambda: catalogs.list_dpa_eligible_catalogs(
        business_id="1", ad_url=_LOGIN_URL)),
]


@pytest.mark.parametrize("case", [case for _, case in _LOGIN_INPUTS], ids=[name for name, _ in _LOGIN_INPUTS])
def test_every_caller_url_with_a_login_is_refused_before_any_request(monkeypatch, case) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="username or password") as info:
        _run(case())
    assert _LOGIN_SECRET not in str(info.value)


def test_url_with_only_a_user_name_is_refused_too(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="username or password"):
        _run(catalogs.update_catalog_product("20", url="https://someone@example.invalid/p"))


def test_ordinary_text_with_at_signs_is_not_refused(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={"20": {"id": "20"}}))
    _run(catalogs.update_catalog_product(
        "20", description="Mail help@shop.example or see https://shop.example/@brand?ref=a@b",
    ))
    assert client.kinds() == ["get", "update"]


def test_h9_catalog_previous_checkout_urls_mask_existing_password(monkeypatch) -> None:
    secret = "synthetic-existing-checkout-password"
    _use(monkeypatch, FakeCatalogClient(objects={"10": {"id": "10", "checkout_urls": [
        {"country": "US", "url": f"https://user:{secret}@example.invalid/checkout"},
    ]}}))
    result = _run(catalogs.update_catalog("10", params={
        "checkout_url": "https://example.invalid/new-checkout", "checkout_country": "US",
    }))
    assert secret not in json.dumps(result)
    assert result["previous"]["checkout_urls"] == [
        {"country": "US", "url": "https://user:[redacted]@example.invalid/checkout"},
    ]


def test_stored_url_passwords_are_masked_in_every_read_back_value(monkeypatch) -> None:
    secret = "synthetic-stored-password"
    stored = f"https://user:{secret}@example.invalid/x"
    product = {"id": "20", "retailer_id": "sku", "url": stored, "image_url": stored,
               "additional_image_urls": [stored]}
    _use(monkeypatch, FakeCatalogClient(
        objects={"20": product, "10": {"id": "10", "default_image_url": stored, "fallback_image_url": [stored]},
                 "ps_1": {"id": "ps_1", "name": "S", "filter": json.dumps({"url": {"eq": stored}})}},
        edges={("10", "products"): {"data": [product]},
               ("10", "product_sets"): {"data": [{"id": "ps_1", "filter": {"url": {"eq": stored}}}]},
               ("10", "diagnostics"): {"data": [{"type": "X", "severity": "MUST_FIX", "title": stored,
                                                 "diagnostics": [{"url": stored}]}]}},
    ))
    results = [
        _run(catalogs.update_catalog_product("20", url="https://example.invalid/new")),
        _run(catalogs.update_catalog_product("20", params={"additional_image_urls": ["https://example.invalid/a"]})),
        _run(catalogs.delete_catalog_product("20")),
        _run(catalogs.update_catalog("10", params={"fallback_image_url": ["https://example.invalid/f"]})),
        _run(catalogs.list_catalog_products(catalog_id="10")),
        _run(catalogs.list_catalog_products(product_id="20", fields=["url"])),
        _run(catalogs.list_catalogs(catalog_id="10", fields=["default_image_url"])),
        _run(catalogs.list_product_sets(catalog_id="10")),
        _run(catalogs.update_product_set("ps_1", name="New")),
        _run(catalogs.delete_product_set("ps_1")),
        _run(catalogs.get_catalog_diagnostics("10", include_details=True)),
        _run(catalogs.get_catalog_diagnostics("10")),
        _run(catalogs.batch_update_catalog_products(
            "10", items=[{"method": "update", "retailer_id": "sku", "url": "https://example.invalid/n"}],
            dry_run=True)),
        _run(catalogs.batch_update_catalog_products(
            "10", items=[{"method": "delete", "retailer_id": "sku"}], dry_run=True)),
    ]
    for result in results:
        text = json.dumps(result)
        assert secret not in text, text
    assert results[0]["previous"]["url"] == "https://user:[redacted]@example.invalid/x"


def test_meta_error_text_with_a_url_password_is_masked(monkeypatch) -> None:
    secret = "synthetic-error-password"
    refused = MetaApiError(message=f"(#100) Bad image https://user:{secret}@example.invalid/i", code=100)
    _use(monkeypatch, FakeCatalogClient(get_errors={"20": refused}))
    with pytest.raises(MetaApiError) as info:
        _run(catalogs.update_catalog_product("20", name="New"))
    assert secret not in str(info.value)
    assert secret not in json.dumps(info.value.to_public_dict())


def test_batch_submission_and_status_errors_with_url_passwords_are_masked(monkeypatch) -> None:
    _no_pause(monkeypatch)
    secret = "synthetic-status-password"
    _use(monkeypatch, _batch_client(
        [_existing("SKU-1", "p1")],
        post={"handles": ["h1"], "validation_status": [
            {"retailer_id": "SKU-1", "errors": [], "warnings": [{"message": f"Slow https://u:{secret}@x.invalid/a"}]},
        ]},
        status=_finished(warnings=[{"id": "SKU-1", "message": f"see https://u:{secret}@x.invalid/b"}]),
    ))
    result = _run(catalogs.batch_update_catalog_products("123", items=[_batch_item("update", "SKU-1", name="A")]))
    assert secret not in json.dumps(result)
    status = _run(catalogs.batch_update_catalog_products("123", handle="h1"))
    assert secret not in json.dumps(status)


def test_h10_submission_error_without_message_still_blocks_applied(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [{"id": "20", "retailer_id": "sku", "name": "Old"}],
        post={"handles": ["h1"], "validation_status": [{"retailer_id": "sku", "errors": [{"code": 100}]}]},
        status={"data": [{"status": "finished", "errors_total_count": 0}]},
    ))
    result = _run(catalogs.batch_update_catalog_products(
        "123", items=[{"method": "update", "retailer_id": "sku", "name": "New"}], wait_seconds=2,
    ))
    item = result["items"][0]
    assert item["outcome"] != "applied"
    assert item["outcome"] == "outcome_unknown"
    assert item["submission_error_count"] == 1
    assert item["errors"] and all(isinstance(text, str) and text for text in item["errors"])
    assert result["ok"] is False


@pytest.mark.parametrize("errors", [[{}], [{"message": ""}], [{"message": None, "code": 1}], [""], {"code": 7}])
def test_submission_errors_of_any_shape_are_counted(monkeypatch, errors) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [_existing("sku", "20")],
        post={"handles": ["h1"], "validation_status": [{"retailer_id": "sku", "errors": errors}]},
        status=_finished(),
    ))
    result = _run(catalogs.batch_update_catalog_products("123", items=[_batch_item("update", "sku", name="N")]))
    assert result["items"][0]["outcome"] == "outcome_unknown"
    assert result["ok"] is False


def test_submission_error_for_no_named_item_leaves_every_item_unknown(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [_existing("SKU-1", "p1"), _existing("SKU-2", "p2")],
        post={"handles": ["h1"], "validation_status": [{"errors": [{"message": "Something failed"}]}]},
        status=_finished(),
    ))
    result = _run(catalogs.batch_update_catalog_products(
        "123", items=[_batch_item("update", "SKU-1", name="A"), _batch_item("update", "SKU-2", name="B")],
    ))
    assert [item["outcome"] for item in result["items"]] == ["outcome_unknown", "outcome_unknown"]
    assert "submitted" in result["items"][0]["reason"]
    assert result["submission"]["unattributed_errors"] == ["Something failed"]


def test_empty_submission_errors_do_not_block_applied(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [_existing("sku", "20")],
        post={"handles": ["h1"], "validation_status": [{"retailer_id": "sku", "errors": [], "warnings": []}]},
        status=_finished(),
    ))
    result = _run(catalogs.batch_update_catalog_products("123", items=[_batch_item("update", "sku", name="N")]))
    assert result["items"][0]["outcome"] == "applied"
    assert "submission_error_count" not in result["items"][0]


class _ShardClient(FakeCatalogClient):
    """Answers each handle's status from its own row."""

    def __init__(self, rows: dict[str, Any], handles: list[str], **kwargs) -> None:
        super().__init__(
            edges={("123", "products"): {"data": [_existing("sku", "20"), _existing("sku2", "21")]}},
            create_responses={("123", "items_batch"): {"handles": handles}}, **kwargs,
        )
        self.rows = rows

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        if edge != "check_batch_request_status":
            return await super().list_objects(parent_id, edge, fields=fields, params=params)
        self.calls.append(("status", {"handle": params["handle"]}))
        row = self.rows[params["handle"]]
        if isinstance(row, Exception):
            raise row
        return {"data": [row]}


def test_h11_multiple_batch_handles_are_not_settled_from_first_only(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _ShardClient(
        {"h1": {"status": "finished", "errors_total_count": 0}, "h2": {"status": "processing"}}, ["h1", "h2"],
    ))
    result = _run(catalogs.batch_update_catalog_products(
        "123", items=[{"method": "update", "retailer_id": "sku", "name": "New"}], wait_seconds=2,
    ))
    assert result["ok"] is False
    assert result["items"][0]["outcome"] == "processing"
    assert [call["handle"] for kind, call in client.calls if kind == "status"] == ["h1", "h2"]
    handles = {entry["handle"]: entry for entry in result["batch"]["handles"]}
    assert handles["h1"]["complete"] is True and handles["h2"]["complete"] is False
    assert handles["h2"]["status"] == "processing"
    assert result["batch"]["complete"] is False
    assert "h2" in result["retry"]["how"] and "h1" not in result["retry"]["how"]


def test_multiple_handles_all_finished_settle_from_every_row(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _ShardClient({
        "h1": {"status": "finished", "errors_total_count": 0},
        "h2": {"status": "finished", "errors_total_count": 1, "errors": [{"id": "sku2", "message": "Bad"}]},
    }, ["h1", "h2"]))
    result = _run(catalogs.batch_update_catalog_products(
        "123", items=[_batch_item("update", "sku", name="A"), _batch_item("update", "sku2", name="B")],
        wait_seconds=4,
    ))
    by_sku = {item["retailer_id"]: item for item in result["items"]}
    assert by_sku["sku"]["outcome"] == "applied"
    assert by_sku["sku2"]["outcome"] == "failed" and by_sku["sku2"]["errors"] == ["Bad"]
    assert result["batch"]["complete"] is True
    assert result["batch"]["errors_total_count"] == 1
    # A finished handle is not checked again.
    assert [call["handle"] for kind, call in client.calls if kind == "status"] == ["h1", "h2"]


def test_multiple_handles_one_unattributed_error_leaves_others_unknown(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _ShardClient({
        "h1": {"status": "finished", "errors_total_count": 0},
        "h2": {"status": "finished", "errors_total_count": 1, "errors": [{"message": "Something"}]},
    }, ["h1", "h2"]))
    result = _run(catalogs.batch_update_catalog_products(
        "123", items=[_batch_item("update", "sku", name="A")], wait_seconds=2,
    ))
    assert result["items"][0]["outcome"] == "outcome_unknown"
    assert result["ok"] is False


def test_multiple_handles_status_read_failure_is_reported_per_handle(monkeypatch) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _ShardClient({
        "h1": {"status": "finished", "errors_total_count": 0},
        "h2": MetaApiError(message="(#100) Unknown handle", code=100),
    }, ["h1", "h2"]))
    result = _run(catalogs.batch_update_catalog_products(
        "123", items=[_batch_item("update", "sku", name="A")], wait_seconds=6,
    ))
    assert result["items"][0]["outcome"] == "processing"
    handles = {entry["handle"]: entry for entry in result["batch"]["handles"]}
    assert "Unknown handle" in handles["h2"]["status_error"]
    assert handles["h2"]["status_checks"] == 0
    assert result["ok"] is False


def test_handle_parameter_still_reads_one_handle(monkeypatch) -> None:
    client = _use(monkeypatch, _ShardClient({"h2": {"status": "finished", "errors_total_count": 0}}, []))
    result = _run(catalogs.batch_update_catalog_products("123", handle="h2"))
    assert [call["handle"] for kind, call in client.calls if kind == "status"] == ["h2"]
    assert result["outcome"] == "all_applied"


def test_create_product_set_names_retailer_id(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _run(catalogs.create_product_set(catalog_id="cat_1", name="Shoes", filter={}, retailer_id=" set-1 "))
    assert client.calls[0][1]["data"] == {"name": "Shoes", "filter": {}, "retailer_id": "set-1"}
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="retailer_id"):
        _run(catalogs.create_product_set(catalog_id="cat_1", name="Shoes", filter={}, retailer_id="  "))
    with pytest.raises(ValidationError, match="retailer_id"):
        _run(catalogs.create_product_set(
            catalog_id="cat_1", name="Shoes", filter={}, retailer_id="a", params={"retailer_id": "b"},
        ))


def test_create_product_set_without_retailer_id_sends_the_same_request(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    _run(catalogs.create_product_set(catalog_id="cat_1", name="Shoes", filter={"brand": {"eq": "A"}}))
    assert client.calls[0][1]["data"] == {"name": "Shoes", "filter": {"brand": {"eq": "A"}}}


# round five: caller text in validation errors, login URLs as keys, unreadable batch handles ------


_PRIVATE_KEY = "".join(("private.person", "@r5.invalid"))


def _private_body_case(mode: str) -> tuple[str, dict[str, Any]]:
    if mode == "nested_login_key":
        return "update_catalog", {"catalog_id": "10", "params": {_PRIVATE_KEY: "https://u:p@example.invalid/x"}}
    if mode == "bad_filter_operator":
        return "build_product_set_filter", {
            "catalog_id": "10", "conditions": [{"field": "brand", "operator": _PRIVATE_KEY, "value": "A"}],
        }
    item: dict[str, Any] = {"method": "update", "retailer_id": "sku", "name": "New"}
    if mode == "unknown_batch_key":
        item[_PRIVATE_KEY] = "x"
    else:
        item["fields"] = {_PRIVATE_KEY: "x"}
    return "batch_update_catalog_products", {"catalog_id": "10", "items": [item], "dry_run": True}


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("mode", ["unknown_batch_key", "unknown_batch_field", "nested_login_key", "bad_filter_operator"])
def test_catalog_body_errors_never_echo_private_keys(monkeypatch, caplog, mode, routed) -> None:
    import logging

    from fastmcp.exceptions import ToolError
    from fastmcp.exceptions import ValidationError as MCPValidationError

    from meta_ads_mcp.config import reload_settings

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    _no_client(monkeypatch)
    tool, arguments = _private_body_case(mode)
    with pytest.raises((ToolError, MCPValidationError)) as error:
        _call_mcp(tool, arguments, routed=routed)
    assert _PRIVATE_KEY not in str(error.value)
    assert _PRIVATE_KEY not in caplog.text


@pytest.mark.parametrize(("call", "expected"), [
    (lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "s", "name": "n", _PRIVATE_KEY: "x", "b@x": 1}]),
     ["items[0]", "2 unsupported keys", "fields"]),
    (lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "s", "fields": {"color": "red", _PRIVATE_KEY: "x"}}]),
     ["items[0].fields", "1 key", "color"]),
    (lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": _PRIVATE_KEY, "name": "a"},
                     {"method": "update", "retailer_id": _PRIVATE_KEY, "name": "b"}]),
     ["items[0]", "items[1]", "retailer_id"]),
    (lambda: catalogs.build_product_set_filter(
        "10", [{"field": "brand", "operator": "eq", "value": "A"}, {"field": "b", "operator": _PRIVATE_KEY, "value": 1}]),
     ["conditions[1].operator", "eq"]),
    (lambda: catalogs.update_catalog("10", params={"a": {_PRIVATE_KEY: "https://u:p@example.invalid/x"}}),
     ["params", "username or password"]),
    (lambda: catalogs.update_catalog("10", params={"a": {"b": ["https://u:p@example.invalid/" + _PRIVATE_KEY]}}),
     ["params", "username or password"]),
])
def test_catalog_validation_messages_name_position_and_accepted_values(monkeypatch, call, expected) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError) as error:
        _run(call())
    message = str(error.value)
    assert _PRIVATE_KEY not in message
    for part in expected:
        assert part in message, message


_KEY_LOGIN_SECRET = "".join(("R5-url-", "credential-x9"))
_KEY_LOGIN_URL = "https://user:" + _KEY_LOGIN_SECRET + "@example.invalid/x"


@pytest.mark.parametrize("case", [
    lambda: catalogs.update_catalog_product("20", params={"custom_data": {_KEY_LOGIN_URL: "x"}}),
    lambda: catalogs.update_catalog_product("20", params={"custom_data": [{"note": {_KEY_LOGIN_URL: "x"}}]}),
    lambda: catalogs.create_catalog_product(**{**_CREATE_PRODUCT_ARGUMENTS, "params": {_KEY_LOGIN_URL: "x"}}),
    lambda: catalogs.update_catalog("10", params={"x": {"see " + _KEY_LOGIN_URL + " now": 1}}),
    lambda: catalogs.create_product_set("10", name="S", filter={_KEY_LOGIN_URL: {"eq": "x"}}),
    lambda: catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "s", "fields": {"color": {_KEY_LOGIN_URL: 1}}}]),
])
def test_catalog_login_url_in_custom_data_key_is_refused_before_write(monkeypatch, case) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="username or password") as error:
        _run(case())
    assert _KEY_LOGIN_SECRET not in str(error.value)
    assert "example.invalid" not in str(error.value)


@pytest.mark.parametrize("channel", ["response", "error_attribute"])
def test_catalog_masking_covers_passwords_in_mapping_keys(monkeypatch, channel) -> None:
    class Data(FakeCatalogClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            if channel == "error_attribute":
                raise MetaApiError("Refused", code=100, error_data={_KEY_LOGIN_URL: "invalid"})
            return {"id": "20", "custom_data": {_KEY_LOGIN_URL: "invalid"}}

    _use(monkeypatch, Data())
    if channel == "error_attribute":
        with pytest.raises(MetaApiError) as error:
            _run(catalogs.list_catalog_products(product_id="20", fields=["id", "custom_data"]))
        assert _KEY_LOGIN_SECRET not in repr(error.value.to_public_dict())
        assert _KEY_LOGIN_SECRET not in repr(error.value.error_data)
    else:
        result = _run(catalogs.list_catalog_products(product_id="20", fields=["id", "custom_data"]))
        assert _KEY_LOGIN_SECRET not in json.dumps(result)
        assert result["items"][0]["custom_data"] == {"https://user:[redacted]@example.invalid/x": "invalid"}


def test_masked_mapping_keys_that_collide_keep_every_entry(monkeypatch) -> None:
    first = "https://user:one-" + _KEY_LOGIN_SECRET + "@example.invalid/x"
    second = "https://user:two-" + _KEY_LOGIN_SECRET + "@example.invalid/x"
    data = {first: "a", second: "b", "https://user:[redacted]@example.invalid/x": "c"}
    _use(monkeypatch, FakeCatalogClient(objects={"20": {"id": "20", "custom_data": data}}))
    result = _run(catalogs.list_catalog_products(product_id="20", fields=["id", "custom_data"]))
    shown = result["items"][0]["custom_data"]
    assert _KEY_LOGIN_SECRET not in json.dumps(result)
    assert sorted(shown.values()) == ["a", "b", "c"]
    assert len(shown) == 3


@pytest.mark.parametrize("malformed", [{}, None, True, [], ["h2"], {"id": "h2"}, 7.5, " "])
def test_unreadable_batch_handle_is_not_silently_discarded(monkeypatch, malformed) -> None:
    class Batch(FakeCatalogClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            self.calls.append(("create", {"parent_id": parent_id, "edge": edge, "data": data}))
            return {"handles": ["h1", malformed]}

        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
            if edge == "products":
                return {"data": [{"id": "20", "retailer_id": "sku", "name": "Old"}]}
            return {"data": [{"status": "finished", "errors_total_count": 0}]}

    async def no_pause(seconds):
        return None

    monkeypatch.setattr(catalogs, "_pause", no_pause)
    _use(monkeypatch, Batch())
    result = _run(catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "sku", "name": "New"}], wait_seconds=2,
    ))
    assert result["ok"] is False
    item = result["items"][0]
    assert item["outcome"] == "outcome_unknown", result
    assert "could not be read" in item["reason"]
    assert result["batch"]["unreadable_handles"] == 1
    assert result["batch"]["complete"] is False
    assert [entry["handle"] for entry in result["batch"]["handles"]] == ["h1"]
    assert result["retry"]["safe_now"] is True


def test_only_unreadable_batch_handles_do_not_claim_nothing_was_ingested(monkeypatch) -> None:
    class Batch(FakeCatalogClient):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            self.calls.append(("create", {"parent_id": parent_id, "edge": edge, "data": data}))
            return {"handles": [{"x": 1}]}

        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.calls.append(("list", {"parent_id": parent_id, "edge": edge, "fields": fields, "params": params}))
            return {"data": [{"id": "20", "retailer_id": "sku", "name": "Old"}]}

    _use(monkeypatch, Batch())
    result = _run(catalogs.batch_update_catalog_products(
        "10", items=[{"method": "update", "retailer_id": "sku", "name": "New"}], wait_seconds=0,
    ))
    assert result["ok"] is False
    assert result["items"][0]["outcome"] == "outcome_unknown"
    assert result["batch"]["unreadable_handles"] == 1


# round six: credentials by key name, data maps, unreadable submission evidence ----------------


@pytest.mark.parametrize("token", ["abc", "".join(("tk", "9x2q")), "".join(("integration-", "token-r6"))])
def test_catalog_credential_key_is_masked_in_a_result_whatever_its_length(monkeypatch, token) -> None:
    _use(monkeypatch, FakeCatalogClient(create_responses={("10", "owned_product_catalogs"): {
        "id": "1", "partner_integration": {"external_access_token": token}, "access_token": token,
    }}))
    result = _run(catalogs.create_catalog(
        "10", "C", params={"partner_integration": {"external_access_token": token}},
    ))
    assert result["created"]["partner_integration"] == {"external_access_token": "[redacted]"}
    assert result["created"]["access_token"] == "[redacted]"


def test_catalog_token_in_successful_text_is_removed_by_its_length(monkeypatch) -> None:
    token = "".join(("tk", "9x2q"))
    _use(monkeypatch, FakeCatalogClient(create_responses={("10", "owned_product_catalogs"): {
        "id": "1", "note": "linked with " + token + "; batch x" + token + "9 kept",
    }}))
    result = _run(catalogs.create_catalog(
        "10", "C", params={"partner_integration": {"external_access_token": token}},
    ))
    assert result["created"]["note"] == "linked with [redacted]; batch x" + token + "9 kept"


_CUSTOM_DATA = {"password": "Yes", "Secret": "Secret Escapes", "access_token": "Gold tier"}


def test_product_custom_data_keys_are_data_not_credentials(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(objects={"20": {"id": "20", "custom_data": {"password": "No"}}}))
    result = _run(catalogs.update_catalog_product("20", params={"custom_data": dict(_CUSTOM_DATA)}))
    update = next(call for kind, call in client.calls if kind == "update")
    assert update["data"] == {"custom_data": _CUSTOM_DATA}
    # What the response says was sent is what was sent.
    assert result["sent"] == update["data"]
    assert result["current"]["custom_data"] == _CUSTOM_DATA
    assert result["previous"]["custom_data"] == {"password": "No"}
    _use(monkeypatch, FakeCatalogClient(objects={"20": {"id": "20", "custom_data": dict(_CUSTOM_DATA)}}))
    listed = _run(catalogs.list_catalog_products(product_id="20", fields=["id", "custom_data"]))
    assert listed["items"][0]["custom_data"] == _CUSTOM_DATA


def test_product_set_filter_on_a_credential_like_field_is_shown_as_sent(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient(edges={("10", "products"): {"data": [], "summary": {"total_count": 3}}}))
    built = _run(catalogs.build_product_set_filter(
        "10", conditions=[{"field": "password", "operator": "eq", "value": "Yes"}],
    ))
    listed = next(call for kind, call in client.calls if kind == "list")
    assert built["filter"] == listed["params"]["filter"] == {"password": {"eq": "Yes"}}
    client = _use(monkeypatch, FakeCatalogClient(objects={"30": {"id": "30", "filter": "{}"}}))
    updated = _run(catalogs.update_product_set("30", filter={"secret": {"eq": "Secret Escapes"}}))
    update = next(call for kind, call in client.calls if kind == "update")
    assert updated["current"]["filter"] == update["data"]["filter"] == {"secret": {"eq": "Secret Escapes"}}


def test_batch_dry_run_request_equals_the_request_a_real_run_sends(monkeypatch) -> None:
    _no_pause(monkeypatch)
    items = [_batch_item("update", "sku", name="Secret Escapes", fields={"custom_label_0": "password"})]
    _use(monkeypatch, _batch_client([_existing("sku", "20")], status=_finished()))
    planned = _run(catalogs.batch_update_catalog_products("123", items=json.loads(json.dumps(items)), dry_run=True))
    real = _use(monkeypatch, _batch_client([_existing("sku", "20")], status=_finished()))
    sent = _run(catalogs.batch_update_catalog_products("123", items=json.loads(json.dumps(items))))
    posted = next(call for kind, call in real.calls if kind == "create")
    assert planned["request"]["data"] == posted["data"]
    assert planned["items"][0]["changes"] == sent["items"][0]["changes"] == {
        "name": "Secret Escapes", "custom_label_0": "password",
    }


def _submitted(monkeypatch, validation_status: Any, *, status: Any = None, skus: tuple[str, ...] = ("sku",)) -> dict:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client(
        [_existing(sku, f"2{index}") for index, sku in enumerate(skus)],
        post={"handles": ["h1"], "validation_status": validation_status},
        status=status if status is not None else _finished(),
    ))
    return _run(catalogs.batch_update_catalog_products(
        "123", items=[_batch_item("update", sku, name="New") for sku in skus], wait_seconds=2,
    ))


@pytest.mark.parametrize("validation_status", [
    [{"retailer_id": "sku", "errors": [None]}],
    [None],
    ["unreadable validation entry"],
])
def test_unreadable_submission_evidence_cannot_be_reported_as_applied(monkeypatch, validation_status) -> None:
    result = _submitted(monkeypatch, validation_status)
    assert result["ok"] is False
    assert result["items"][0]["outcome"] == "outcome_unknown"
    assert "could not be read" in result["items"][0]["reason"]


def test_unreadable_error_entry_for_one_item_leaves_only_that_item_unknown(monkeypatch) -> None:
    result = _submitted(monkeypatch, [{"retailer_id": "sku", "errors": [None, None]}], skus=("sku", "sku2"))
    first, second = result["items"]
    assert first["outcome"] == "outcome_unknown"
    assert first["submission_unreadable_error_count"] == 2
    assert "2 error entries for this item" in first["reason"]
    assert "submission_error_count" not in first
    assert second["outcome"] == "applied"
    assert "submission" not in result


@pytest.mark.parametrize(("validation_status", "counts"), [
    ([None, "text", 5], {"unreadable_entries": 3}),
    ({"errors": [None]}, {"unreadable_error_entries": 1}),
    ([{"retailer_id": "other", "errors": [None, {"message": "Bad"}]}],
     {"unreadable_error_entries": 1, "unattributed_error_count": 1}),
    ("unreadable", {"unreadable_entries": 1}),
])
def test_unreadable_submission_evidence_naming_no_item_leaves_every_item_unknown(
    monkeypatch, validation_status, counts,
) -> None:
    result = _submitted(monkeypatch, validation_status, skus=("sku", "sku2"))
    assert [item["outcome"] for item in result["items"]] == ["outcome_unknown", "outcome_unknown"]
    assert all(item["reason"] for item in result["items"])
    for key, value in counts.items():
        assert result["submission"][key] == value
    assert "could not be read" in result["submission"]["note"]
    assert result["ok"] is False


def test_unreadable_submission_evidence_does_not_hide_a_named_failure_or_processing(monkeypatch) -> None:
    failed = _submitted(monkeypatch, [None], status=_finished(
        errors_total_count=1, errors=[{"id": "sku", "message": "Bad price"}],
    ))
    assert failed["items"][0]["outcome"] == "failed"
    assert failed["items"][0]["errors"] == ["Bad price"]
    processing = _submitted(monkeypatch, [None], status={"data": [{"status": "in_progress"}]})
    assert processing["items"][0]["outcome"] == "processing"


@pytest.mark.parametrize("validation_status", [None, [], {}, "", [{"retailer_id": "sku", "errors": []}]])
def test_empty_submission_evidence_still_allows_applied(monkeypatch, validation_status) -> None:
    result = _submitted(monkeypatch, validation_status)
    assert result["items"][0]["outcome"] == "applied"
    assert "submission" not in result


_R6_INVALID_ID_MATRIX = [
    # An invalid-request entry that cannot be read as an id could be any item.
    ({"status": "finished", "errors_total_count": 1, "errors": [{"id": "bad", "message": "m"}],
      "ids_of_invalid_requests": ["bad", None]}, "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 1, "errors": [{"id": "bad", "message": "m"}],
      "ids_of_invalid_requests": [" "]}, "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 1, "errors": [{"id": "bad", "message": "m"}],
      "ids_of_invalid_requests": [True]}, "outcome_unknown"),
    ({"status": "finished", "errors_total_count": 0, "ids_of_invalid_requests": [{"id": "target"}]},
     "outcome_unknown"),
    # One id given without a list is read as that id, never character by character.
    ({"status": "finished", "errors_total_count": 1, "ids_of_invalid_requests": "target"}, "failed"),
    ({"status": "finished", "errors_total_count": 1, "ids_of_invalid_requests": "bad"}, "applied"),
]


@pytest.mark.parametrize(("status", "expected"), _R6_INVALID_ID_MATRIX)
def test_unreadable_invalid_request_ids_are_not_skipped(status, expected) -> None:
    item = {"retailer_id": "target", "warnings": [], "errors": []}
    catalogs._settle(item, status)
    assert item["outcome"] == expected, item
    if expected == "outcome_unknown":
        assert "could not be read" in item["reason"]


def test_handle_read_reports_unreadable_invalid_ids_and_a_single_error_object(monkeypatch) -> None:
    _use(monkeypatch, _batch_client(status=_finished(
        errors_total_count=1, errors={"id": "SKU-1", "message": "Bad"}, ids_of_invalid_requests=["SKU-1", None],
    )))
    result = _run(catalogs.batch_update_catalog_products(catalog_id="123", handle="h1"))
    assert result["outcome"] == "some_failed_others_unknown"
    assert result["error_attribution"]["unreadable_invalid_ids"] == 1
    assert "could not be read" in result["error_attribution"]["reason"]
    assert result["errors"] == [{"id": "SKU-1", "message": "Bad"}]
    assert result["ok"] is False


@pytest.mark.parametrize("answer", [[], ["h1"], "ok", 7, True])
def test_batch_answer_that_is_not_an_object_leaves_every_item_unknown(monkeypatch, answer) -> None:
    # The write has gone out; an answer that cannot be read says nothing about whether it was ingested.
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")], post=answer))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="A")],
    ))
    assert client.kinds().count("create") == 1
    assert result["items"][0]["outcome"] == "outcome_unknown"
    assert "could not be read" in result["items"][0]["reason"]
    assert result["batch"] == {"complete": False, "handles": [], "response_unreadable": True}
    assert result["ok"] is False


# round seven -----------------------------------------------------------------------------
# L9: a status answer that cannot be read after a batch submission keeps the write's result.


@pytest.mark.parametrize("status_reply", [None, "unreadable", 7])
def test_l9_batch_malformed_status_preserves_postwrite_outcome(monkeypatch, status_reply) -> None:
    """Through the real Graph client over an in-process transport (no socket)."""
    import httpx

    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.graph_api import GraphAPIClient

    monkeypatch.setenv("META_ACCESS_TOKEN", "test-token")
    graph = GraphAPIClient(reload_settings())

    class Batch(FakeCatalogClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            if edge == "products":
                return {"data": [{"id": "20", "retailer_id": "sku", "name": "Old"}]}
            return await graph.list_objects(parent_id, edge, fields=fields, params=params)

    client = _use(monkeypatch, Batch(create_responses={("10", "items_batch"): {"handles": ["h1"]}}))
    _no_pause(monkeypatch)

    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, content=json.dumps(status_reply), headers={"content-type": "application/json"},
        ))
        async with httpx.AsyncClient(transport=transport) as http:
            monkeypatch.setattr(GraphAPIClient, "_get_shared_client", lambda self, *, base_url=None: http)
            return await catalogs.batch_update_catalog_products(
                "10", [{"method": "update", "retailer_id": "sku", "name": "New"}], wait_seconds=4,
            )

    result = _run(scenario())
    assert "create" in client.kinds()
    assert result["ok"] is False
    assert result["items"][0]["outcome"] == "processing"
    assert result["batch"]["handle"] == "h1"
    assert result["batch"]["status_error"]
    assert result["batch"]["status_checks"] == 1  # an unreadable status is not asked for again
    assert result["retry"]["safe_now"] is False and "h1" in result["retry"]["how"]


@pytest.mark.parametrize("status", [{"data": []}, {"data": [None]}, {"json_response": None},
                                    {"text_response": "<html>", "content_type": "text/html", "status_code": 200}])
def test_batch_status_without_a_readable_row_is_reported_unread(monkeypatch, status) -> None:
    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")], status=status))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="New")],
    ))
    item = result["items"][0]
    assert item["outcome"] == "processing" and "could not be read" in item["reason"]
    assert result["batch"]["handles"][0]["status_error"]


def test_unexpected_failure_while_reading_status_keeps_the_handle(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _batch_client([_existing("SKU-1", "p1")], edge_errors={("123", "check_batch_request_status"): KeyError("x")})
    _use(monkeypatch, client)
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="New")],
    ))
    assert result["batch"]["handle"] == "h1"
    assert "KeyError" in result["batch"]["status_error"]
    assert result["items"][0]["outcome"] == "processing"


def test_cancellation_while_reading_status_is_not_swallowed(monkeypatch) -> None:
    _no_pause(monkeypatch)
    client = _batch_client(
        [_existing("SKU-1", "p1")], edge_errors={("123", "check_batch_request_status"): asyncio.CancelledError()},
    )
    _use(monkeypatch, client)
    with pytest.raises(asyncio.CancelledError):
        _run(catalogs.batch_update_catalog_products(
            catalog_id="123", items=[_batch_item("update", "SKU-1", name="New")],
        ))


def test_unexpected_failure_reading_the_submission_answer_keeps_items_unresolved(monkeypatch) -> None:
    class Odd(dict):
        def get(self, key, default=None):
            raise RuntimeError("odd answer")

    _no_pause(monkeypatch)
    _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")], post=Odd(handles=["h1"])))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="New")],
    ))
    assert result["items"][0]["outcome"] == "outcome_unknown"
    assert "RuntimeError" in result["answer_error"] and result["ok"] is False


@pytest.mark.parametrize("posted", [
    {"json_response": None}, {"json_response": "accepted"}, {"json_response": 3},
    {"text_response": "<html>ok</html>", "content_type": "text/html", "status_code": 200},
])
def test_wrapped_submission_answer_is_unreadable_not_nothing_ingested(monkeypatch, posted) -> None:
    _no_pause(monkeypatch)
    client = _use(monkeypatch, _batch_client([_existing("SKU-1", "p1")], post=posted))
    result = _run(catalogs.batch_update_catalog_products(
        catalog_id="123", items=[_batch_item("update", "SKU-1", name="New")],
    ))
    assert [item["outcome"] for item in result["items"]] == ["outcome_unknown"]
    assert result["batch"]["response_unreadable"] is True
    assert all(call["edge"] != "check_batch_request_status" for kind, call in client.calls if kind == "list")


def test_handle_read_with_an_unreadable_status_says_so(monkeypatch) -> None:
    _use(monkeypatch, _batch_client(status={"json_response": None}))
    result = _run(catalogs.batch_update_catalog_products(catalog_id="123", handle="h1"))
    assert result["ok"] is False and result["outcome"] == "status_unreadable"
    assert result["batch"]["handle"] == "h1" and result["batch"]["status_error"]


# L1: a credential passed as the JSON text of its documented object has the object's privacy.

_INTEGRATION_SECRET = "".join(("R7integration", "Credential-987"))


def _integration_text(secret: str = _INTEGRATION_SECRET) -> str:
    from meta_ads_mcp.graph_api import GraphAPIClient

    integration = {"external_access_token": secret, "external_merchant_id": "merchant-1"}
    text = json.dumps(integration)
    # Exactly the form value the real Graph client sends for the object.
    assert GraphAPIClient._encode_value(integration) == text
    return text


@pytest.mark.parametrize("operation", ["successful_update", "failed_create"])
def test_l1_serialized_integration_has_the_same_privacy_as_an_object(monkeypatch, operation) -> None:
    text = _integration_text()
    refusal = MetaApiError("Invalid external credential " + _INTEGRATION_SECRET, code=100)
    client = _use(monkeypatch, FakeCatalogClient(create_errors={("10", "owned_product_catalogs"): refusal}))
    if operation == "successful_update":
        result = _run(catalogs.update_catalog("10", params={"partner_integration": text}))
        assert _INTEGRATION_SECRET not in json.dumps(result)
        # The request sent is unchanged; what is shown is that text with the token masked.
        update = next(call for kind, call in client.calls if kind == "update")
        assert update["data"] == {"partner_integration": text}
        assert json.loads(result["current"]["partner_integration"]) == {
            "external_access_token": "[redacted]", "external_merchant_id": "merchant-1",
        }
    else:
        with pytest.raises(MetaApiError) as raised:
            _run(catalogs.create_catalog("10", "Catalog", params={"partner_integration": text}))
        assert _INTEGRATION_SECRET not in str(raised.value)


def test_short_token_in_serialized_integration_is_masked_by_key(monkeypatch) -> None:
    # Three characters: never removed by value from a result, so only the key masks it.
    text = _integration_text("".join(("x", "9z")))
    _use(monkeypatch, FakeCatalogClient())
    result = _run(catalogs.update_catalog("10", params={"partner_integration": text}))
    assert json.loads(result["current"]["partner_integration"])["external_access_token"] == "[redacted]"


def test_integration_text_that_is_not_json_is_sent_and_shown_as_before(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogClient())
    result = _run(catalogs.update_catalog("10", params={"partner_integration": "not json"}))
    assert next(call for kind, call in client.calls if kind == "update")["data"] == {"partner_integration": "not json"}
    assert result["current"]["partner_integration"] == "not json"


def test_serialized_integration_token_is_collected_as_a_secret() -> None:
    from meta_ads_mcp.tools.catalog_feeds import _caller_secrets

    arguments = {"params": {"partner_integration": _integration_text(), "custom_data": json.dumps({"token": "Gold"})}}
    assert _caller_secrets(arguments, catalogs._CATALOG_CREDENTIAL_PATHS) == [_INTEGRATION_SECRET]
