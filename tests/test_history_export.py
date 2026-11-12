"""History retrieval must never confuse a page, an artifact, and complete API coverage."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from meta_ads_mcp.errors import MetaApiError, NotFoundError, ValidationError
from meta_ads_mcp.overflow import OverflowArtifactStore
from meta_ads_mcp.tools import activity


WINDOW = {"since": "2026-06-01", "until": "2026-06-07"}


def event(oid="ad_1", kind="ADGROUP", event_type="update_ad_run_status", **extra):
    return {
        "object_id": oid, "object_type": kind, "event_type": event_type,
        "event_time": "2026-06-04T12:30:00+0000", "actor_id": "actor_1",
        "application_name": "Test app", "extra_data": '{"old_value":2,"new_value":100}',
        **extra,
    }


def page(rows, cursor=None):
    result = {"data": rows}
    if cursor:
        result["paging"] = {"cursors": {"after": cursor}, "next": "https://graph.facebook.com/next"}
    return result


class HistoryClient:
    def __init__(self, pages, parents=None):
        self.pages = pages
        self.parents = parents or {}
        self.calls = []
        self.lookups = []

    async def get_object(self, oid, *, fields=None):
        self.lookups.append((oid, fields))
        if fields == ["account_id"]:
            return {"account_id": "123"}
        result = self.parents.get(oid, NotFoundError("Object unavailable"))
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    async def list_objects(self, parent, edge, *, fields=None, params=None):
        self.calls.append((parent, edge, list(fields), deepcopy(params)))
        result = self.pages[params.get("after")]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    store = OverflowArtifactStore(tmp_path / "exports")
    monkeypatch.setattr(activity, "OVERFLOW_ARTIFACT_STORE", store)

    def install(pages, parents=None):
        client = HistoryClient(pages, parents)
        monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)
        return client, store

    return install


def export(**kwargs):
    return asyncio.run(activity.export_change_history(account_id="123", **{**WINDOW, **kwargs}))


def read_all(store, export_id):
    rows, offset = [], 0
    for _ in range(100):
        result = store.read(export_id, item_offset=offset, item_limit=7)
        rows.extend(result["items"])
        if result["complete"]:
            return rows, result["context"]["tool_result"]["structured_content"]
        assert result["next_item_offset"] > offset
        offset = result["next_item_offset"]
    pytest.fail("Artifact did not finish")


def test_full_export_retains_every_event_duplicate_and_unknown_type(setup):
    rows = [event(str(i)) for i in range(97)] + [event("same", event_type="future_change") for _ in range(2)]
    client, store = setup({None: page(rows[:50], "next"), "next": page(rows[50:])})
    result = export(inline_limit=3)
    saved, context = read_all(store, result["export_id"])
    assert result["complete"] is True and result["has_more"] is False
    assert result["record_count"] == len(saved) == 99
    assert result["returned_count"] == len(result["items"]) == 3
    assert result["summary"]["by_event_type"]["future_change"] == 2
    for original, recovered in zip(rows, saved):
        assert all(recovered[k] == v for k, v in original.items())
    assert context["complete"] is True
    assert result["coverage"]["all_meta_changes_guaranteed"] is False
    assert result["coverage"]["unfiltered_account_all_levels"] is True
    assert all(call[2] == activity.DEFAULT_ACTIVITY_FIELDS for call in client.calls)
    assert all("oid" not in call[3] and "category" not in call[3] for call in client.calls)


def test_one_page_explains_remaining_history_and_freezes_continuation_window(setup):
    client, _ = setup({None: page([event()], "next"), "next": page([event("ad_2")])})
    result = asyncio.run(activity.list_change_history(account_id="123", **WINDOW))
    assert result["complete"] is False and result["has_more"] is True
    assert result["ai_instructions"][0].startswith("MORE HISTORY REMAINS")
    continuation = result["next_call"]
    assert continuation["name"] == "list_change_history"
    assert continuation["arguments"]["after"] == "next"
    assert continuation["arguments"]["until"] == "2026-06-08T00:00:00+00:00"
    legacy_next = urlsplit(result["paging"]["next"])
    assert legacy_next.hostname == "graph.facebook.com"
    assert parse_qs(legacy_next.query)["until"] == [continuation["arguments"]["until"]]
    assert parse_qs(legacy_next.query)["after"] == ["next"]
    tail = asyncio.run(activity.list_change_history(**continuation["arguments"]))
    assert tail["has_more"] is False
    assert tail["complete"] is False  # A final page by itself is not the whole history.
    assert tail["coverage"]["api_pages_exhausted"] is True
    assert tail["coverage"]["starts_at_beginning"] is False
    assert client.calls[0][3]["until"] == client.calls[1][3]["until"]


def test_bounded_export_parts_are_preserved_and_can_be_combined(setup):
    _, store = setup({None: page([event()], "next"), "next": page([event("ad_2")])})
    first = export(max_pages=1, inline_limit=0)
    assert first["complete"] is False and first["coverage"]["stop_reason"] == "page_limit"
    assert first["next_call"]["arguments"]["inline_limit"] == 0
    second = asyncio.run(activity.export_change_history(**first["next_call"]["arguments"]))
    assert second["complete"] is False and second["has_more"] is False
    assert second["coverage"]["api_pages_exhausted"] is True
    assert len(read_all(store, first["export_id"])[0] + read_all(store, second["export_id"])[0]) == 2


def test_campaign_export_resolves_deleted_children_without_current_inventory(setup):
    rows = [event("cmp", "CAMPAIGN_GROUP"), event("deleted_set", "CAMPAIGN"), event("deleted_ad"), event("other_ad")]
    client, store = setup({None: page(rows)}, {
        "deleted_set": {"campaign_id": "cmp"}, "deleted_ad": {"campaign_id": "cmp"},
        "other_ad": {"campaign_id": "other_cmp"},
    })
    result = export(campaign_id="cmp")
    saved, _ = read_all(store, result["export_id"])
    assert [r["object_id"] for r in saved] == ["cmp", "deleted_set", "deleted_ad"]
    assert [r["entity_level"] for r in saved] == ["campaign", "adset", "ad"]
    assert result["complete"] is True
    assert result["coverage"]["includes_descendants"] is True
    assert "oid" not in client.calls[0][3]
    assert {fields[0] for _, fields in client.lookups} == {"account_id", "campaign_id"}


def test_adset_export_keeps_unavailable_and_unknown_candidates_visible(setup):
    client, store = setup({None: page([event("set", "CAMPAIGN"), event("child"), event("gone"), event("new", "NEW_TYPE")])},
        {"child": {"adset_id": "set"}})
    result = export(adset_id="set")
    rows, _ = read_all(store, result["export_id"])
    assert len(rows) == 4
    assert result["coverage"]["unresolved_scope_events"] == 2
    assert result["coverage"]["api_pages_exhausted"] is True
    assert result["complete"] is False and result["has_more"] is False
    assert rows[2]["scope_match"] == {"status": "unresolved", "reason": "parent_lookup_failed"}
    assert rows[3]["entity_level"] == "unknown"
    assert any("unresolved" in message for message in result["ai_instructions"])
    assert ("child", ["adset_id"]) in client.lookups


def test_known_historical_ids_resolve_candidates_without_parent_reads(setup):
    client, store = setup({None: page([event("gone"), event("gone")])})
    result = export(campaign_id="cmp", extra_oids=["gone"])
    assert result["complete"] is True
    assert len(read_all(store, result["export_id"])[0]) == 2
    assert client.lookups == [("cmp", ["account_id"])]


def test_empty_scoped_page_with_more_data_still_has_continuation(setup):
    _, _ = setup({None: page([event("other")], "next")}, {"other": {"campaign_id": "different"}})
    result = asyncio.run(activity.list_change_history(account_id="123", campaign_id="cmp", include_descendants=True, **WINDOW))
    assert result["items"] == []
    assert result["has_more"] is True and result["next_call"]
    assert result["complete"] is False


def test_parent_lookup_budget_does_not_discard_unresolved_events(setup, monkeypatch):
    monkeypatch.setattr(activity, "MAX_PARENT_LOOKUPS", 1)
    _, store = setup({None: page([event("a"), event("b")])}, {"a": {"campaign_id": "cmp"}, "b": {"campaign_id": "cmp"}})
    result = export(campaign_id="cmp")
    rows, _ = read_all(store, result["export_id"])
    assert len(rows) == 2 and result["complete"] is False
    assert rows[1]["scope_match"]["reason"] == "parent_lookup_budget_reached"


@pytest.mark.parametrize("cursor_problem", ["missing", "repeated"])
def test_unusable_cursors_never_claim_complete_or_offer_looping_continuation(setup, cursor_problem):
    second = page([event("b")], "next")
    if cursor_problem == "missing":
        second["paging"].pop("cursors")
    _, store = setup({None: page([event()], "next"), "next": second})
    result = export()
    assert result["complete"] is False and result["has_more"] is True
    assert result["next_call"] is None
    assert result["coverage"]["stop_reason"] == cursor_problem + "_cursor"
    assert len(read_all(store, result["export_id"])[0]) == 2


def test_cursor_in_next_url_is_used_without_following_the_url(setup):
    first = {"data": [event()], "paging": {"next": "https://not-the-api.invalid/path?after=next&access_token=do-not-follow"}}
    client, _ = setup({None: first, "next": page([event("b")])})
    result = export()
    assert result["complete"] is True
    assert all(call[:2] == ("act_123", "activities") for call in client.calls)
    assert client.calls[1][3]["after"] == "next"
    assert "access_token" not in json.dumps(result)


def test_later_api_failure_saves_fetched_events_and_issues_retry(setup):
    _, store = setup({None: page([event()], "next"), "next": MetaApiError("private diagnostic", code=17)})
    result = export()
    assert result["complete"] is False
    assert result["coverage"]["stop_reason"] == "api_error"
    assert result["coverage"]["error"]["code"] == 17
    assert "private diagnostic" not in json.dumps(result)
    assert result["next_call"]["arguments"]["after"] == "next"
    assert len(read_all(store, result["export_id"])[0]) == 1


def test_first_page_failure_is_not_an_empty_success(setup):
    setup({None: MetaApiError("No permission", code=190)})
    with pytest.raises(MetaApiError):
        export()


def test_byte_budget_returns_explicit_continuation(setup, monkeypatch):
    monkeypatch.setattr(activity, "MAX_EXPORT_BYTES", 1)
    _, store = setup({None: page([event()], "next"), "next": page([event("b")])})
    result = export()
    assert result["coverage"]["stop_reason"] == "byte_budget"
    assert result["next_call"]["arguments"]["after"] == "next"
    assert len(read_all(store, result["export_id"])[0]) == 1


def test_categories_are_explicit_filters_and_all_means_unfiltered(setup):
    client, _ = setup({None: page([event()])})
    filtered = export(category="ad set", uid="actor", data_source="tao")
    assert client.calls[-1][3]["category"] == "AD_SET"
    assert filtered["coverage"]["unfiltered_account_all_levels"] is False
    assert any("category is not" in text for text in filtered["ai_instructions"])
    clean_call = filtered["unfiltered_account_export_call"]
    assert "category" not in clean_call["arguments"] and "uid" not in clean_call["arguments"]
    result = export(category="all")
    assert "category" not in client.calls[-1][3]
    assert result["coverage"]["unfiltered_account_all_levels"] is True


def test_projected_list_keeps_identity_and_explains_missing_details(setup):
    client, _ = setup({None: page([event()])})
    result = asyncio.run(activity.list_change_history(account_id="123", fields=["event_type"], **WINDOW))
    assert set(activity.IDENTITY_FIELDS) <= set(client.calls[0][2])
    assert "extra_data" in result["coverage"]["omitted_detail_fields"]
    assert "fields" not in result["export_call"]["arguments"]


@pytest.mark.parametrize(("since", "until", "zone", "hours"), [
    ("2026-03-08", "2026-03-08", "America/New_York", 23),
    ("2026-11-01", "2026-11-01", "America/New_York", 25),
    ("2026-06-07", "2026-06-07", "UTC", 24),
])
def test_date_only_until_includes_the_day_across_dst(since, until, zone, hours):
    window = activity._history_window(since, until, zone)
    assert datetime.fromisoformat(window["until"]) - datetime.fromisoformat(window["since"]) == timedelta(hours=hours)


def test_default_utc_works_without_a_system_timezone_database(monkeypatch):
    def unavailable(_):
        raise activity.ZoneInfoNotFoundError("No timezone data installed")
    monkeypatch.setattr(activity, "ZoneInfo", unavailable)
    window = activity._history_window("2026-06-01", "2026-06-07", "UTC")
    assert window["since"] == "2026-06-01T00:00:00+00:00"
    assert activity._within_window(event(), window)


def test_exclusive_end_boundary_and_unparseable_times_are_handled_without_data_loss(setup):
    rows = [event("inside", event_time="2026-06-07T23:59:59+0000"),
            event("boundary", event_time="2026-06-08T00:00:00+0000"),
            event("unknown", event_time="not-a-time")]
    _, store = setup({None: page(rows)})
    result = export()
    assert [row["object_id"] for row in read_all(store, result["export_id"])[0]] == ["inside", "unknown"]
    assert result["summary"]["excluded_time_boundary_events"] == 1


@pytest.mark.parametrize("kwargs", [
    {"since": ""}, {"until": ""}, {"since": "2026-06-01T12:00:00"},
    {"since": "2026-06-40"}, {"since": "2026-06-09"}, {"time_zone": "not/a-zone"},
    {"limit": 0}, {"limit": 501}, {"max_pages": 0}, {"max_pages": 1001},
    {"inline_limit": -1}, {"inline_limit": 101}, {"category": "made_up"},
    {"data_source": "made_up"}, {"extra_oids": [""]}, {"extra_oids": ["id"]},
])
def test_invalid_queries_fail_before_activity_fetch(setup, kwargs):
    client, _ = setup({None: page([])})
    with pytest.raises(ValidationError):
        export(**kwargs)
    assert client.calls == []


def test_future_end_is_frozen_at_collection_time(setup):
    _, _ = setup({None: page([event()])})
    result = export(until="2099-01-01")
    window = result["coverage"]["window"]
    assert window["future_until_clamped_to_collection_time"] is True
    assert window["until"] == window["collected_at"]
    assert any("future" in message for message in result["ai_instructions"])


def test_storage_failure_is_not_a_successful_export(setup, monkeypatch):
    _, store = setup({None: page([event()])})
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(store, "create", fail)
    with pytest.raises(ValidationError, match="No successful export"):
        export()


def test_raw_before_after_and_minor_units_remain_unchanged(setup):
    details = {"old_value": {"type": "payment_amount", "currency": "USD", "old_value": 1000},
               "new_value": {"type": "payment_amount", "currency": "USD", "new_value": 1200}}
    _, store = setup({None: page([event(extra_data=json.dumps(details)), event("no_detail", extra_data=None)])})
    result = export()
    saved, _ = read_all(store, result["export_id"])
    assert saved[0]["extra_data_parsed"] == details
    assert saved[0]["extra_data_units"][0]["major_unit_divisor"] == 100
    assert saved[0]["change_details_available"]["old_value"] is True
    assert saved[1]["change_details_available"]["new_value"] is False
    assert result["summary"]["events_without_new_value"] == 1


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize(("query", "expected"), [
    ("show all changes to this account", "export_change_history"),
    ("export complete campaign change history", "export_change_history"),
    ("get ad set change history", "export_change_history"),
    ("show every ad change history event", "export_change_history"),
    ("read activity logs", "export_change_history"),
    ("show who changed budgets", "export_change_history"),
    ("get a preview of change history", "list_change_history"),
    ("read the next page of change history", "list_change_history"),
    ('get campaign named "Changes"', "get_campaign"),
    ("get campaign named Changes", "get_campaign"),
    ("get campaign named Change History", "get_campaign"),
    ("get ad creative called Activity Logs", "get_creative"),
    ("export change history for campaign named Page Preview", "export_change_history"),
    ("show all changes for campaign named Changes", "export_change_history"),
    ('get ad creative "Change History"', "get_creative"),
    ('export history of changes for campaign "Page Preview"', "export_change_history"),
])
def test_history_search_prefers_complete_export_except_explicit_pages(monkeypatch, locked, query, expected):
    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.coordinator import mcp_server
    monkeypatch.setenv("META_READ_ONLY", str(locked).lower())
    reload_settings()
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))
    text = " ".join(getattr(item, "text", "") for item in result.content)
    assert f"`{expected}`" in text
    assert "`update_campaign_budget`" not in text


@pytest.mark.parametrize("locked", [False, True])
def test_history_export_executes_through_real_proxy_and_read_only_policy(setup, monkeypatch, locked):
    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.coordinator import mcp_server
    setup({None: page([event()])})
    monkeypatch.setenv("META_READ_ONLY", str(locked).lower())
    reload_settings()
    result = asyncio.run(mcp_server.call_tool("call_tool", {
        "name": "export_change_history", "arguments": {"account_id": "123", **WINDOW},
    }))
    assert result.structured_content["record_count"] == 1
    assert result.structured_content["delivery"]["read_call"]["name"] == "read_overflow_artifact"


def test_oversized_history_notice_exposes_partial_source_coverage(tmp_path):
    from fastmcp.server.middleware.middleware import MiddlewareContext
    from fastmcp.tools.tool import ToolResult
    from mcp.types import CallToolRequestParams
    from meta_ads_mcp.overflow import ArchivedResponseLimitingMiddleware, _serialized_mcp_result
    store = OverflowArtifactStore(tmp_path)
    middleware = ArchivedResponseLimitingMiddleware(max_size=64_000, truncation_suffix="fallback", artifact_store=store)
    payload = {"items": [event(extra_data="x" * 100_000)], "complete": False, "has_more": True,
               "coverage": {"status": "partial_api_query"}, "next_call": {"name": "list_change_history", "arguments": {"after": "next"}}}
    async def respond(_):
        return ToolResult(content=[], structured_content=payload)
    result = asyncio.run(middleware.on_call_tool(MiddlewareContext(message=CallToolRequestParams(name="list_change_history", arguments={})), respond))
    assert len(_serialized_mcp_result(result)) <= 64_000
    assert result.meta["overflow"]["source_complete"] is False
    assert result.meta["overflow"]["source_has_more"] is True
    parts, offset = [], 0
    while True:
        chunk = store.read(result.meta["overflow"]["export_id"], offset=offset)
        parts.append(chunk["data"])
        if chunk["complete"]:
            break
        offset = chunk["next_offset"]
    assert json.loads("".join(parts))["tool_result"]["structured_content"] == payload


@pytest.mark.parametrize("quota", ["default", "one_file", "one_artifact_bytes"])
@pytest.mark.parametrize("via_proxy", [False, True])
def test_oversized_export_preview_reuses_full_artifact_without_eviction(setup, quota, via_proxy):
    from fastmcp.server.middleware.middleware import MiddlewareContext
    from fastmcp.tools.tool import ToolResult
    from mcp.types import CallToolRequestParams
    from meta_ads_mcp.overflow import ArchivedResponseLimitingMiddleware, _serialized_mcp_result
    _, store = setup({None: page([event("large", extra_data="x" * 100_000), event("second")])})
    if quota == "one_file":
        store.max_files = 1
    elif quota == "one_artifact_bytes":
        store.max_total_bytes = 150_000
    payload = export(inline_limit=1)
    assert payload["delivery"]["artifact_bytes"] < store.max_total_bytes
    middleware = ArchivedResponseLimitingMiddleware(max_size=64_000, truncation_suffix="fallback", artifact_store=store)
    async def respond(_):
        return ToolResult(content=[], structured_content=payload)
    request = CallToolRequestParams(name="call_tool" if via_proxy else "export_change_history",
                                   arguments={"name": "export_change_history", "arguments": {}} if via_proxy else {})
    notice = asyncio.run(middleware.on_call_tool(MiddlewareContext(message=request), respond))
    assert notice.meta["overflow"]["full_history_export_id"] == payload["export_id"]
    assert notice.meta["overflow"]["export_id"] == payload["export_id"]
    assert "only an export preview" not in notice.content[0].text
    assert len(_serialized_mcp_result(notice)) <= 64_000
    parts, offset = [], 0
    while True:
        chunk = store.read(payload["export_id"], offset=offset)
        parts.append(chunk["data"])
        if chunk["complete"]:
            break
        offset = chunk["next_offset"]
    original = json.loads("".join(parts))["tool_result"]["structured_content"]
    assert [row["object_id"] for row in original["items"]] == ["large", "second"]
    assert original["items"][0]["extra_data"] == "x" * 100_000
