"""Product catalog feed, feed rule, upload and event source tool tests."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.errors import MetaApiError, ValidationError
from meta_ads_mcp.tools import catalog_feeds


def _permission_error() -> MetaApiError:
    return MetaApiError(
        message="(#200) Requires catalog_management permission to manage the object",
        status_code=403,
        code=200,
    )


class FakeCatalogFeedClient:
    """Fake Graph client recording every call made by the catalog feed tools."""

    def __init__(
        self,
        *,
        objects: dict[str, dict[str, Any]] | None = None,
        edges: dict[tuple[str, str], Any] | None = None,
        errors: dict[Any, Exception] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.objects = objects or {}
        self.edges = edges or {}
        self.errors = errors or {}

    def _maybe_raise(self, key: Any) -> None:
        if key in self.errors:
            raise self.errors[key]

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append({"method": "get_object", "object_id": object_id, "fields": fields, "params": params})
        self._maybe_raise(object_id)
        return dict(self.objects.get(object_id, {"id": object_id}))

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.calls.append({
            "method": "list_objects", "parent_id": parent_id, "edge": edge,
            "fields": fields, "params": params,
        })
        self._maybe_raise((parent_id, edge))
        value = self.edges.get((parent_id, edge), {"data": []})
        if callable(value):
            return value(params or {})
        return value

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.calls.append({"method": "create_edge_object", "parent_id": parent_id, "edge": edge, "data": data})
        return {"id": "created_1"}

    async def update_object(self, object_id: str, *, data):
        self.calls.append({"method": "update_object", "object_id": object_id, "data": data})
        return {"success": True}

    async def delete_object(self, object_id: str):
        self.calls.append({"method": "delete_object", "object_id": object_id})
        return {"success": True}

    async def delete_from_edge(self, parent_id: str, edge: str, *, data):
        self.calls.append({"method": "delete_from_edge", "parent_id": parent_id, "edge": edge, "data": data})
        return {"success": True}

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]


def _use(monkeypatch, client: FakeCatalogFeedClient) -> FakeCatalogFeedClient:
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: client)
    return client


def _no_client(monkeypatch) -> None:
    monkeypatch.setattr(
        catalog_feeds,
        "get_graph_api_client",
        lambda: pytest.fail("No client should be created for invalid input"),
    )


def run(coro):
    return asyncio.run(coro)


SCHEDULE = {"url": "https://example.com/feed.csv", "interval": "daily", "hour": 22}


# list_product_feeds -----------------------------------------------------------------


def test_list_product_feeds_lists_catalog_edge_with_paging(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "product_feeds"): {
            "data": [{"id": "feed_1", "name": "Main"}],
            "paging": {"cursors": {"after": "c2"}, "next": "https://graph.facebook.com/next"},
        },
    }))
    result = run(catalog_feeds.list_product_feeds(catalog_id="cat_1", limit=10, after="c1"))
    call = client.calls[0]
    assert call["method"] == "list_objects"
    assert (call["parent_id"], call["edge"]) == ("cat_1", "product_feeds")
    assert call["params"] == {"limit": 10, "after": "c1"}
    assert call["fields"] == catalog_feeds.FEED_LIST_FIELDS
    assert result["items"] == [{"id": "feed_1", "name": "Main"}]
    assert result["summary"] == {"count": 1, "complete": False, "truncated": True, "next_after": "c2"}


def test_list_product_feeds_one_feed_returns_settings_schedule_and_latest_upload(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={
        "feed_1": {"id": "feed_1", "schedule": {"interval": "DAILY"}, "latest_upload": {"id": "up_1"}},
    }))
    result = run(catalog_feeds.list_product_feeds(feed_id="feed_1"))
    call = client.calls[0]
    assert call["method"] == "get_object"
    assert call["object_id"] == "feed_1"
    assert call["fields"] == catalog_feeds.FEED_FIELDS
    assert "schedule" in call["fields"] and "update_schedule" in call["fields"]
    assert any(field.startswith("latest_upload{") for field in call["fields"])
    assert result["item"]["latest_upload"] == {"id": "up_1"}
    assert result["summary"] == {"count": 1}


@pytest.mark.parametrize("kwargs", [{}, {"catalog_id": "cat_1", "feed_id": "feed_1"}, {"catalog_id": " "}, {"catalog_id": "cat_1", "limit": 0}])
def test_list_product_feeds_requires_exactly_one_scope(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.list_product_feeds(**kwargs))


def test_list_product_feeds_permission_error_is_not_swallowed(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(errors={("cat_1", "product_feeds"): _permission_error()}))
    with pytest.raises(MetaApiError, match="catalog_management"):
        run(catalog_feeds.list_product_feeds(catalog_id="cat_1"))


# create_product_feed ----------------------------------------------------------------


def test_create_product_feed_sends_name_schedule_and_options(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    result = run(catalog_feeds.create_product_feed(
        catalog_id="cat_1", name="Main feed", schedule=SCHEDULE,
        update_schedule={"url": "http://example.com/prices.csv", "interval": "HOURLY"},
        file_name="feed.csv", default_currency="usd", country="us",
        params={"delimiter": "COMMA"},
    ))
    call = client.calls[0]
    assert (call["method"], call["parent_id"], call["edge"]) == ("create_edge_object", "cat_1", "product_feeds")
    assert call["data"] == {
        "name": "Main feed",
        "schedule": {"url": "https://example.com/feed.csv", "interval": "DAILY", "hour": 22},
        "update_schedule": {"url": "http://example.com/prices.csv", "interval": "HOURLY"},
        "file_name": "feed.csv",
        "default_currency": "USD",
        "country": "US",
        "delimiter": "COMMA",
    }
    assert result["ok"] is True
    assert result["action"] == "create_product_feed"
    assert result["target"] == {"catalog_id": "cat_1"}
    assert result["created"] == {"id": "created_1"}


def test_create_product_feed_without_schedule_sends_only_name(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Manual feed"))
    assert client.calls[0]["data"] == {"name": "Manual feed"}


@pytest.mark.parametrize("schedule", [
    {"url": "https://example.com/feed.csv", "interval": "YEARLY"},
    {"url": "https://example.com/feed.csv"},
    {"interval": "DAILY"},
    {"url": "file:///etc/passwd", "interval": "DAILY"},
    {"url": "example.com/feed.csv", "interval": "DAILY"},
    {"url": "https://example.com/feed.csv", "interval": "DAILY", "hour": 24},
    {"url": "https://example.com/feed.csv", "interval": "DAILY", "minute": 60},
    {"url": "https://example.com/feed.csv", "interval": "WEEKLY", "day_of_week": "FUNDAY"},
    {"url": "https://example.com/feed.csv", "interval": "DAILY", "cron": "0 * * * *"},
])
def test_create_feed_schedule_validates_interval_and_url(monkeypatch, schedule) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Feed", schedule=schedule))
    with pytest.raises(ValidationError):
        run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Feed", update_schedule=schedule))


def test_create_feed_schedule_accepts_documented_ftp_url(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed(
        catalog_id="cat_1", name="Feed",
        schedule={"url": "ftp://example.com/feed.csv", "interval": "weekly", "day_of_week": "monday"},
    ))
    assert client.calls[0]["data"]["schedule"] == {
        "url": "ftp://example.com/feed.csv", "interval": "WEEKLY", "day_of_week": "MONDAY",
    }


def test_create_product_feed_response_never_echoes_schedule_password(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    result = run(catalog_feeds.create_product_feed(
        catalog_id="cat_1", name="Feed",
        schedule={**SCHEDULE, "username": "feeduser", "password": "s3cret-value"},
    ))
    assert client.calls[0]["data"]["schedule"]["password"] == "s3cret-value"
    assert "s3cret-value" not in repr(result)


@pytest.mark.parametrize("kwargs", [
    {"catalog_id": " ", "name": "Feed"},
    {"catalog_id": "cat_1", "name": " "},
    {"catalog_id": "cat_1", "name": "Feed", "params": {"name": "other"}},
    {"catalog_id": "cat_1", "name": "Feed", "params": {"schedule": {}}},
])
def test_create_product_feed_rejects_bad_input_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.create_product_feed(**kwargs))


# update_product_feed ----------------------------------------------------------------


def test_update_product_feed_reads_first_and_returns_previous(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={
        "feed_1": {"id": "feed_1", "name": "Old", "schedule": {"interval": "DAILY", "url": "https://old"}, "default_currency": "USD"},
    }))
    result = run(catalog_feeds.update_product_feed(feed_id="feed_1", name="New", schedule=SCHEDULE))
    assert client.methods() == ["get_object", "update_object"]
    assert client.calls[0]["fields"] == catalog_feeds.FEED_UPDATE_READ_FIELDS
    assert client.calls[1]["object_id"] == "feed_1"
    assert client.calls[1]["data"] == {
        "name": "New",
        "schedule": {"url": "https://example.com/feed.csv", "interval": "DAILY", "hour": 22},
    }
    assert result["action"] == "update_product_feed"
    assert result["target"] == {"feed_id": "feed_1"}
    assert result["previous"] == {"name": "Old", "schedule": {"interval": "DAILY", "url": "https://old"}}
    assert result["current"]["name"] == "New"


def test_update_product_feed_requires_a_change(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="At least one"):
        run(catalog_feeds.update_product_feed(feed_id="feed_1"))


def test_update_product_feed_validates_schedule_before_any_call(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.update_product_feed(feed_id="feed_1", schedule={"url": "https://x", "interval": "SOMETIMES"}))


def test_update_product_feed_read_failure_sends_no_update(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(errors={"feed_1": _permission_error()}))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.update_product_feed(feed_id="feed_1", name="New"))
    assert client.methods() == ["get_object"]


# delete_product_feed ----------------------------------------------------------------


def test_delete_product_feed_reads_then_deletes_and_returns_previous(monkeypatch) -> None:
    feed = {"id": "feed_1", "name": "Main", "schedule": {"interval": "DAILY"}, "product_count": 40}
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"feed_1": feed}))
    result = run(catalog_feeds.delete_product_feed(feed_id="feed_1"))
    assert client.methods() == ["get_object", "delete_object"]
    assert client.calls[0]["fields"] == catalog_feeds.FEED_FIELDS
    assert client.calls[1]["object_id"] == "feed_1"
    assert result["action"] == "delete_product_feed"
    assert result["previous"] == feed
    assert result["current"] == {"deleted": True, "result": {"success": True}}


def test_delete_product_feed_missing_object_sends_no_delete(monkeypatch) -> None:
    from meta_ads_mcp.errors import NotFoundError

    client = _use(monkeypatch, FakeCatalogFeedClient(errors={"feed_1": NotFoundError("gone")}))
    with pytest.raises(NotFoundError):
        run(catalog_feeds.delete_product_feed(feed_id="feed_1"))
    assert client.methods() == ["get_object"]


def test_delete_feed_docstring_warns_products_are_removed() -> None:
    doc = inspect.getdoc(catalog_feeds.delete_product_feed).lower()
    assert "products" in doc and "removed" in doc
    assert "unless another source" in doc
    assert "explicit" in doc


def test_delete_product_feed_requires_id(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.delete_product_feed(feed_id=" "))


# create_product_feed_upload -----------------------------------------------------------


def test_create_product_feed_upload_posts_url_and_update_only(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    result = run(catalog_feeds.create_product_feed_upload(
        feed_id="feed_1", url="https://example.com/feed.csv", update_only=True,
    ))
    call = client.calls[0]
    assert (call["method"], call["parent_id"], call["edge"]) == ("create_edge_object", "feed_1", "uploads")
    assert call["data"] == {"url": "https://example.com/feed.csv", "update_only": True}
    assert result["action"] == "create_product_feed_upload"
    assert result["target"] == {"feed_id": "feed_1"}
    assert result["created"] == {"id": "created_1"}


def test_create_product_feed_upload_omits_update_only_by_default(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", url="http://example.com/f.csv"))
    assert client.calls[0]["data"] == {"url": "http://example.com/f.csv"}


@pytest.mark.parametrize("url", ["", " ", "ftp://example.com/feed.csv", "file:///tmp/feed.csv", "example.com/feed.csv", "https://"])
def test_feed_upload_requires_http_url(monkeypatch, url) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="http"):
        run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", url=url))


# list_product_feed_uploads ------------------------------------------------------------


def _upload_errors(_params):
    return {"data": [
        {"id": "e1", "summary": "Missing price", "severity": "warning", "total_count": 3},
        {"id": "e2", "summary": "Invalid image", "severity": "fatal", "total_count": 120},
        {"id": "e3", "summary": "Bad URL", "severity": "fatal", "total_count": 3},
        {"id": "e4", "summary": "No count", "severity": "warning"},
    ]}


def test_feed_uploads_errors_ordered_by_rows_affected(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "start_time": "2026-11-01T00:00:00+0000", "end_time": "2026-11-01T00:05:00+0000", "error_count": 2, "warning_count": 2}},
        edges={("up_1", "errors"): _upload_errors},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1"))
    assert [call["method"] for call in client.calls] == ["get_object", "list_objects"]
    assert client.calls[0]["fields"] == catalog_feeds.UPLOAD_FIELDS
    assert (client.calls[1]["parent_id"], client.calls[1]["edge"]) == ("up_1", "errors")
    assert client.calls[1]["fields"] == catalog_feeds.UPLOAD_ERROR_FIELDS
    item = result["item"]
    assert [error["id"] for error in item["errors"]] == ["e2", "e3", "e1", "e4"]
    assert item["status"] == "finished"
    assert item["errors_complete"] is True


def test_list_product_feed_uploads_lists_feed_edge_and_fetches_errors_only_when_present(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("feed_1", "uploads"): {"data": [
            {"id": "up_2", "start_time": "2026-11-02T00:00:00+0000"},
            {"id": "up_1", "end_time": "2026-11-01T00:05:00+0000", "error_count": 0, "warning_count": 0},
            {"id": "up_0", "end_time": "2026-10-31T00:05:00+0000", "error_count": 1, "warning_count": 0},
        ]},
        ("up_0", "errors"): _upload_errors,
        ("up_2", "errors"): {"data": []},
    }))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", limit=3))
    first = client.calls[0]
    assert (first["parent_id"], first["edge"], first["params"]) == ("feed_1", "uploads", {"limit": 3})
    assert first["fields"] == catalog_feeds.UPLOAD_FIELDS
    error_parents = [call["parent_id"] for call in client.calls if call.get("edge") == "errors"]
    assert error_parents == ["up_2", "up_0"]
    items = {item["id"]: item for item in result["items"]}
    assert items["up_2"]["status"] == "in_progress"
    assert items["up_1"]["errors"] == []
    assert items["up_0"]["errors"][0]["id"] == "e2"
    assert result["summary"]["complete"] is True


def test_list_product_feed_uploads_reports_truncated_error_list(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 500}},
        edges={("up_1", "errors"): {"data": [{"id": "e1", "total_count": 1}], "paging": {"next": "https://next", "cursors": {"after": "x"}}}},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1"))
    assert result["item"]["errors_complete"] is False


@pytest.mark.parametrize("kwargs", [{"feed_id": " "}, {"feed_id": "feed_1", "limit": 0}])
def test_list_product_feed_uploads_validates_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.list_product_feed_uploads(**kwargs))


# feed rules -----------------------------------------------------------------------------


def test_list_feed_rules_reads_rules_edge(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("feed_1", "rules"): {"data": [{"id": "rule_1", "attribute": "title", "rule_type": "letter_case_rule"}]},
    }))
    result = run(catalog_feeds.list_feed_rules(feed_id="feed_1"))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"], call["fields"], call["params"]) == (
        "feed_1", "rules", catalog_feeds.RULE_FIELDS, {"limit": 100},
    )
    assert result["items"][0]["id"] == "rule_1"
    assert result["summary"]["complete"] is True


def test_list_feed_rules_requires_feed_id(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.list_feed_rules(feed_id=""))


@pytest.mark.parametrize(("rule_type", "params"), [
    ("mapping_rule", {"map_from": "product_title"}),
    ("value_mapping_rule", {"new": "NEW", "used": "USED"}),
    ("letter_case_rule", {"type": "to_upper"}),
    ("regex_replace_rule", {"regex": "\\s+"}),
    ("fallback_rule", {"user_default_value": "in stock"}),
])
def test_create_feed_rule_posts_documented_shape(monkeypatch, rule_type, params) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    result = run(catalog_feeds.create_feed_rule(
        feed_id="feed_1", attribute="title", rule_type=rule_type.upper(), params=params,
    ))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"]) == ("feed_1", "rules")
    assert call["data"] == {"attribute": "title", "rule_type": rule_type, "params": params}
    assert result["action"] == "create_feed_rule"
    assert result["created"] == {"id": "created_1"}


@pytest.mark.parametrize("kwargs", [
    {"rule_type": "split_rule", "params": {"x": "y"}},
    {"rule_type": "mapping_rule", "params": {}},
    {"rule_type": "letter_case_rule", "params": {"type": "shout"}},
    {"rule_type": "regex_replace_rule", "params": {"regex": "("}},
    {"rule_type": "fallback_rule", "params": {"user_default_value": 5}},
    {"rule_type": "value_mapping_rule", "params": {str(i): "v" for i in range(11)}},
    {"rule_type": "value_mapping_rule", "params": {"a" * 21: "v"}},
    {"rule_type": "mapping_rule", "params": {"map_from": "x"}, "attribute": " "},
])
def test_create_feed_rule_rejects_bad_input_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    arguments = {"feed_id": "feed_1", "attribute": "title", **kwargs}
    with pytest.raises(ValidationError):
        run(catalog_feeds.create_feed_rule(**arguments))


def test_create_feed_rule_unknown_type_lists_accepted_types(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="mapping_rule.*value_mapping_rule|fallback_rule"):
        run(catalog_feeds.create_feed_rule(feed_id="f", attribute="a", rule_type="nope", params={}))


def test_delete_feed_rule_reads_then_deletes_and_returns_previous(monkeypatch) -> None:
    rule = {"id": "rule_1", "attribute": "title", "rule_type": "letter_case_rule", "params": {"type": "to_upper"}}
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"rule_1": rule}))
    result = run(catalog_feeds.delete_feed_rule(rule_id="rule_1"))
    assert client.methods() == ["get_object", "delete_object"]
    assert client.calls[0]["fields"] == catalog_feeds.RULE_FIELDS
    assert client.calls[1]["object_id"] == "rule_1"
    assert result["action"] == "delete_feed_rule"
    assert result["target"] == {"rule_id": "rule_1"}
    assert result["previous"] == rule
    assert result["current"]["deleted"] is True


def test_delete_feed_rule_read_failure_sends_no_delete(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(errors={"rule_1": _permission_error()}))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.delete_feed_rule(rule_id="rule_1"))
    assert client.methods() == ["get_object"]


def test_delete_feed_rule_docstring_says_feed_and_products_unaffected() -> None:
    doc = inspect.getdoc(catalog_feeds.delete_feed_rule).lower()
    assert "unaffected" in doc


# event sources ---------------------------------------------------------------------------

SOURCES = {"data": [
    {"id": "px_1", "name": "Site pixel", "source_type": "PIXEL"},
    {"id": "app_1", "name": "Shop app", "source_type": "APP"},
]}


def test_list_catalog_event_sources_reads_external_event_sources_edge(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): SOURCES}))
    result = run(catalog_feeds.list_catalog_event_sources(catalog_id="cat_1"))
    call = client.calls[0]
    assert (call["parent_id"], call["edge"], call["fields"]) == ("cat_1", "external_event_sources", catalog_feeds.EVENT_SOURCE_FIELDS)
    assert call["params"] == {"limit": 100}
    assert [item["id"] for item in result["items"]] == ["px_1", "app_1"]


def test_list_catalog_event_sources_permission_error_is_not_swallowed(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(errors={("cat_1", "external_event_sources"): _permission_error()}))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.list_catalog_event_sources(catalog_id="cat_1"))


def test_list_event_source_catalogs_scans_business_catalogs(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("biz_1", "owned_product_catalogs"): {"data": [{"id": "cat_1", "name": "A"}, {"id": "cat_2", "name": "B"}]},
        ("cat_1", "external_event_sources"): SOURCES,
        ("cat_2", "external_event_sources"): {"data": [{"id": "px_9"}]},
    }))
    result = run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1"))
    first = client.calls[0]
    assert (first["parent_id"], first["edge"]) == ("biz_1", "owned_product_catalogs")
    assert first["fields"] == catalog_feeds.CATALOG_SCAN_FIELDS
    assert [item["id"] for item in result["items"]] == ["cat_1"]
    assert result["summary"]["catalogs_scanned"] == 2
    assert result["summary"]["complete"] is True
    assert result["summary"]["business_id"] == "biz_1"
    assert "derived" in result["summary"]["method"]


def test_list_event_source_catalogs_uses_pixel_owner_business(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"px_1": {"id": "px_1", "owner_business": {"id": "biz_7", "name": "Biz"}}},
        edges={("biz_7", "owned_product_catalogs"): {"data": []}},
    ))
    result = run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1"))
    assert client.calls[0]["method"] == "get_object"
    assert client.calls[0]["fields"] == ["id", "owner_business"]
    assert client.calls[1]["parent_id"] == "biz_7"
    assert result["items"] == []
    assert result["summary"]["business_id"] == "biz_7"


def test_list_event_source_catalogs_without_owner_business_asks_for_business_id(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(objects={"px_1": {"id": "px_1"}}))
    with pytest.raises(ValidationError, match="business_id"):
        run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1"))


def test_list_event_source_catalogs_reports_incomplete_scan(monkeypatch) -> None:
    def endless(params):
        page = int(params.get("after") or 0)
        return {
            "data": [{"id": f"cat_{page}_{i}"} for i in range(params["limit"])],
            "paging": {"next": "https://next", "cursors": {"after": str(page + 1)}},
        }

    client = FakeCatalogFeedClient(edges={("biz_1", "owned_product_catalogs"): endless})
    _use(monkeypatch, client)
    result = run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1"))
    assert result["summary"]["complete"] is False
    assert result["summary"]["catalogs_scanned"] == catalog_feeds.MAX_CATALOGS_SCANNED
    assert "incomplete" in result["summary"]["note"]


def test_list_event_source_catalogs_permission_error_is_not_swallowed(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(
        edges={("biz_1", "owned_product_catalogs"): {"data": [{"id": "cat_1"}]}},
        errors={("cat_1", "external_event_sources"): _permission_error()},
    ))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1"))


def test_connect_catalog_event_source_reads_then_posts(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): {"data": [{"id": "app_1"}]}}))
    result = run(catalog_feeds.connect_catalog_event_source(catalog_id="cat_1", event_source_id="px_1"))
    assert client.methods() == ["list_objects", "create_edge_object"]
    post = client.calls[1]
    assert (post["parent_id"], post["edge"], post["data"]) == ("cat_1", "external_event_sources", {"external_event_sources": ["px_1"]})
    assert result["action"] == "connect_catalog_event_source"
    assert result["previous"] == {"connected_event_source_ids": ["app_1"]}
    assert result["current"] == {"connected_event_source_ids": ["app_1", "px_1"]}


def test_connect_already_connected_source_sends_no_write(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): SOURCES}))
    result = run(catalog_feeds.connect_catalog_event_source(catalog_id="cat_1", event_source_id="px_1"))
    assert client.methods() == ["list_objects"]
    assert result["ok"] is True
    assert result["current"]["already_connected"] is True


def test_disconnect_catalog_event_source_reads_then_deletes_with_parameter(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): SOURCES}))
    result = run(catalog_feeds.disconnect_catalog_event_source(catalog_id="cat_1", event_source_id="px_1"))
    assert client.methods() == ["list_objects", "delete_from_edge"]
    delete = client.calls[1]
    assert (delete["parent_id"], delete["edge"], delete["data"]) == ("cat_1", "external_event_sources", {"external_event_sources": ["px_1"]})
    assert result["previous"]["connected_event_source_ids"] == ["px_1", "app_1"]
    assert result["previous"]["event_source"] == {"id": "px_1", "name": "Site pixel", "source_type": "PIXEL"}
    assert result["current"] == {"connected_event_source_ids": ["app_1"]}


def test_disconnect_source_not_connected_sends_no_delete(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): SOURCES}))
    with pytest.raises(ValidationError, match="not connected"):
        run(catalog_feeds.disconnect_catalog_event_source(catalog_id="cat_1", event_source_id="px_404"))
    assert client.methods() == ["list_objects"]


def test_disconnect_read_failure_sends_no_delete(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(errors={("cat_1", "external_event_sources"): _permission_error()}))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.disconnect_catalog_event_source(catalog_id="cat_1", event_source_id="px_1"))
    assert client.methods() == ["list_objects"]


@pytest.mark.parametrize("tool", ["connect_catalog_event_source", "disconnect_catalog_event_source"])
@pytest.mark.parametrize("kwargs", [{"catalog_id": " ", "event_source_id": "px_1"}, {"catalog_id": "cat_1", "event_source_id": ""}])
def test_connect_and_disconnect_validate_ids_before_any_call(monkeypatch, tool, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(getattr(catalog_feeds, tool)(**kwargs))


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("name", ["connect_catalog_event_source", "disconnect_catalog_event_source"])
def test_connect_and_disconnect_are_blocked_in_read_only(monkeypatch, name, routed) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.read_only import read_only_tool_names
    from meta_ads_mcp.tool_contracts import build_tool_contracts

    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    _no_client(monkeypatch)
    try:
        assert name not in read_only_tool_names()
        components = mcp_server.local_provider._components.values()
        contracts = build_tool_contracts([c for c in components if getattr(c, "name", None)])
        assert contracts[name].effect == "write"
        arguments = {"catalog_id": "cat_1", "event_source_id": "px_1"}
        tool, payload = ("call_tool", {"name": name, "arguments": arguments}) if routed else (name, arguments)
        with pytest.raises(ToolError, match="META_READ_ONLY"):
            run(mcp_server.call_tool(tool, payload))
    finally:
        monkeypatch.delenv("META_READ_ONLY")
        reload_settings()


def test_feed_tools_are_grouped_reads_in_catalog_and_writes_in_writes() -> None:
    from meta_ads_mcp.tools.utility import TOOL_GROUPS

    reads = {
        "list_product_feeds", "list_product_feed_uploads", "list_feed_rules",
        "list_catalog_event_sources", "list_event_source_catalogs",
        "get_catalog_event_source_health", "get_catalog_event_source_recommendations",
    }
    writes = {
        "create_product_feed", "update_product_feed", "delete_product_feed",
        "create_product_feed_upload", "create_feed_rule", "delete_feed_rule",
        "connect_catalog_event_source", "disconnect_catalog_event_source",
    }
    assert reads <= set(TOOL_GROUPS["catalog"])
    assert writes <= set(TOOL_GROUPS["writes"])
    for group, names in TOOL_GROUPS.items():
        if group != "writes":
            assert not writes & set(names), group
        if group != "catalog":
            assert not reads & set(names), group


# health -------------------------------------------------------------------------------------


def _stat(day: str, source: str, event: str, matched: int, unmatched: int) -> dict[str, Any]:
    return {
        "date_start": day, "date_stop": day, "event": event,
        "event_source": {"id": source, "source_type": "PIXEL"},
        "total_matched_content_ids": matched, "total_unmatched_content_ids": unmatched,
        "unique_matched_content_ids": matched, "unique_unmatched_content_ids": unmatched,
    }


def test_event_source_health_includes_28_day_history_when_present_and_says_when_absent(monkeypatch) -> None:
    stats = {"data": [
        _stat("2026-11-01", "px_1", "ViewContent", 90, 10),
        _stat("2026-11-01", "px_1", "Purchase", 10, 0),
        _stat("2026-11-02", "px_1", "ViewContent", 40, 60),
    ]}
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): SOURCES,
        ("cat_1", "event_stats"): stats,
        ("px_1", "da_checks"): {"data": [
            {"key": "pixel_missing_dpa_event", "title": "Pixel is not sending DPA events", "result": "failed"},
            {"key": "pixel_decline", "title": "Decline", "result": "passed"},
        ]},
        ("app_1", "da_checks"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    stats_call = next(call for call in client.calls if call.get("edge") == "event_stats")
    assert stats_call["fields"] == catalog_feeds.EVENT_STAT_FIELDS
    sources = {source["id"]: source for source in result["sources"]}

    pixel = sources["px_1"]
    assert pixel["history_available"] is True
    assert pixel["match_rate_history"] == [
        {"date": "2026-11-01", "matched_content_ids": 100, "unmatched_content_ids": 10, "match_rate": 0.9091},
        {"date": "2026-11-02", "matched_content_ids": 40, "unmatched_content_ids": 60, "match_rate": 0.4},
    ]
    assert pixel["window_match_rate"] == round(140 / 210, 4)
    assert pixel["by_event"]["ViewContent"]["match_rate"] == round(130 / 200, 4)
    assert [check["key"] for check in pixel["failed_checks"]] == ["pixel_missing_dpa_event"]

    app = sources["app_1"]
    assert app["history_available"] is False
    assert app["match_rate_history"] == []
    assert any("app_1" in signal and "28" in signal for signal in result["missing_signals"])

    finding_types = [finding["type"] for finding in result["findings"]]
    assert "failed_dynamic_ads_check" in finding_types
    assert "no_event_stats" in finding_types
    assert "low_match_rate" in finding_types
    assert result["signals_used"]
    assert any("event_stats" in signal for signal in result["signals_used"])
    assert "derived" in result["method"].lower()


def test_event_source_health_no_connected_sources_is_a_finding(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": []},
        ("cat_1", "event_stats"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert result["sources"] == []
    assert result["findings"][0]["type"] == "no_connected_event_sources"


def test_event_source_health_skips_checks_for_unconfirmed_source_types(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "ds_1", "source_type": "OFFLINE"}]},
        ("cat_1", "event_stats"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert not any(call.get("edge") == "da_checks" for call in client.calls)
    assert any("ds_1" in signal and "da_checks" in signal for signal in result["missing_signals"])


def test_event_source_health_permission_error_is_not_swallowed(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(
        edges={("cat_1", "external_event_sources"): SOURCES},
        errors={("cat_1", "event_stats"): _permission_error()},
    ))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))


@pytest.mark.parametrize("kwargs", [{"catalog_id": " "}, {"catalog_id": "cat_1", "match_rate_threshold": 1.5}])
def test_event_source_health_validates_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.get_catalog_event_source_health(**kwargs))


# recommendations ------------------------------------------------------------------------------


def test_recommendations_rank_unconnected_pixels_with_evidence(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): {"data": [_stat("2026-11-01", "px_1", "ViewContent", 1, 99)]},
        ("biz_1", "adspixels"): {"data": [
            {"id": "px_1", "name": "Connected"},
            {"id": "px_2", "name": "Sends catalog events", "last_fired_time": "2026-11-11T10:00:00+0000"},
            {"id": "px_3", "name": "Quiet", "last_fired_time": "2025-01-01T00:00:00+0000"},
            {"id": "px_4", "name": "Gone", "is_unavailable": True},
        ]},
        ("px_2", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
        ("px_3", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "failed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1", business_id="biz_1"))
    pixel_call = next(call for call in client.calls if call.get("edge") == "adspixels")
    assert pixel_call["parent_id"] == "biz_1"
    assert pixel_call["fields"] == catalog_feeds.PIXEL_CANDIDATE_FIELDS
    checked = [call["parent_id"] for call in client.calls if call.get("edge") == "da_checks"]
    assert checked == ["px_2", "px_3"]
    recommendations = result["recommendations"]
    assert [item["event_source_id"] for item in recommendations] == ["px_2", "px_3"]
    assert recommendations[0]["strength"] == "strong"
    assert recommendations[0]["evidence"]
    assert recommendations[0]["next_step"]["tool"] == "connect_catalog_event_source"
    assert recommendations[1]["strength"] == "weak"
    finding_types = [finding["type"] for finding in result["findings"]]
    assert "weak_catalog_signal_coverage" in finding_types
    assert result["signals_used"]
    assert "derived" in result["method"].lower()


def test_recommendations_without_business_reads_catalog_business(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"cat_1": {"id": "cat_1", "business": {"id": "biz_9", "name": "Biz"}}},
        edges={
            ("cat_1", "external_event_sources"): {"data": []},
            ("cat_1", "event_stats"): {"data": []},
            ("biz_9", "adspixels"): {"data": []},
        },
    ))
    result = run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1"))
    assert client.calls[0]["method"] == "get_object"
    assert client.calls[0]["fields"] == ["id", "business"]
    assert result["scope"]["business_id"] == "biz_9"
    assert result["findings"][0]["type"] == "no_connected_event_sources"
    assert any("no pixel" in signal.lower() for signal in result["missing_signals"])


def test_recommendations_permission_error_is_not_swallowed(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(
        edges={("cat_1", "external_event_sources"): {"data": []}, ("cat_1", "event_stats"): {"data": []}},
        errors={("biz_1", "adspixels"): _permission_error()},
    ))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1", business_id="biz_1"))


def test_graph_client_delete_from_edge_sends_delete_with_form_data(monkeypatch) -> None:
    from meta_ads_mcp.graph_api import GraphAPIClient

    sent: list[tuple[str, str, Any]] = []

    async def fake_request(self, method, endpoint, *, params=None, data=None, files=None, base_url=None, use_auth_header=True):
        sent.append((method, endpoint, data))
        return {"success": True}

    monkeypatch.setattr(GraphAPIClient, "request", fake_request)
    client = GraphAPIClient(reload_settings())
    result = run(client.delete_from_edge("cat_1", "external_event_sources", data={"external_event_sources": ["px_1"]}))
    assert sent == [("DELETE", "cat_1/external_event_sources", {"external_event_sources": ["px_1"]})]
    assert result == {"success": True}
