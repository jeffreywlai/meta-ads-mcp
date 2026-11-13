"""Product catalog feed, feed rule, upload and event source tool tests."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from typing import Any
from urllib.parse import quote, quote_plus

import pytest
from fastmcp.exceptions import ToolError

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.errors import MetaApiError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.secret_scrub import ERROR, scrub_exception
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
        self.calls.append({
            "method": "create_edge_object", "parent_id": parent_id, "edge": edge, "data": data, "files": files,
        })
        return {"id": "created_1"}

    async def update_object(self, object_id: str, *, data):
        self.calls.append({"method": "update_object", "object_id": object_id, "data": data})
        return {"success": True}

    async def delete_object(self, object_id: str):
        self.calls.append({"method": "delete_object", "object_id": object_id})
        return {"success": True}

    async def delete_edge_object(self, parent_id: str, edge: str, *, data):
        self.calls.append({"method": "delete_edge_object", "parent_id": parent_id, "edge": edge, "data": data})
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

# Field lists written out here rather than taken from the module, so a field
# added to or dropped from a request is caught. Each is a ProductFeed,
# ProductFeedUpload, ProductFeedUploadError, ProductFeedRule, ExternalEventSource,
# ProductCatalog, ProductEventStat, DACheck or AdsPixel field.
EXPECTED_UPLOAD_FIELDS = [
    "id", "start_time", "end_time", "input_method", "filename", "url",
    "error_count", "warning_count", "num_detected_items", "num_persisted_items",
    "num_invalid_items", "num_deleted_items",
]
EXPECTED_UPLOAD_ERROR_FIELDS = [
    "id", "summary", "description", "severity", "error_type", "affected_surfaces", "total_count",
]
EXPECTED_FEED_FIELDS = [
    "id", "name", "file_name", "country", "default_currency", "deletion_enabled",
    "delimiter", "encoding", "quoted_fields_mode", "ingestion_source_type",
    "item_sub_type", "override_type", "primary_feeds", "migrated_from_feed_id",
    "product_count", "created_time", "schedule", "update_schedule",
    "latest_upload{id,start_time,end_time,input_method,filename,url,error_count,"
    "warning_count,num_detected_items,num_persisted_items,num_invalid_items,num_deleted_items}",
]
EXPECTED_FEED_LIST_FIELDS = [
    "id", "name", "file_name", "ingestion_source_type", "product_count", "schedule",
    "update_schedule", "latest_upload{id,start_time,end_time,error_count,warning_count}",
]
EXPECTED_FEED_UPDATE_READ_FIELDS = [
    "id", "name", "default_currency", "deletion_enabled", "delimiter", "encoding",
    "quoted_fields_mode", "schedule", "update_schedule", "migrated_from_feed_id",
]
EXPECTED_RULE_FIELDS = ["id", "attribute", "rule_type", "params"]
EXPECTED_EVENT_SOURCE_FIELDS = ["id", "name", "source_type"]
EXPECTED_EVENT_STAT_FIELDS = [
    "date_start", "date_stop", "event", "event_source",
    "total_matched_content_ids", "total_unmatched_content_ids",
    "unique_matched_content_ids", "unique_unmatched_content_ids",
    "total_content_ids_matched_other_catalogs", "unique_content_ids_matched_other_catalogs",
]
EXPECTED_DA_CHECK_FIELDS = ["key", "title", "description", "result", "user_message", "action_uri"]
EXPECTED_CATALOG_SCAN_FIELDS = ["id", "name"]
EXPECTED_PIXEL_CANDIDATE_FIELDS = ["id", "name", "last_fired_time", "is_unavailable"]


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
    assert call["fields"] == [*EXPECTED_FEED_LIST_FIELDS, "supplementary_feeds"]
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
    assert call["fields"] == [*EXPECTED_FEED_FIELDS, "supplementary_feeds"]
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
        delimiter="COMMA",
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
    assert client.calls[0]["fields"] == EXPECTED_FEED_UPDATE_READ_FIELDS
    assert client.calls[1]["object_id"] == "feed_1"
    assert client.calls[1]["data"] == {
        "name": "New",
        "schedule": {"url": "https://example.com/feed.csv", "interval": "DAILY", "hour": 22},
    }
    assert result["action"] == "update_product_feed"
    assert result["target"] == {"feed_id": "feed_1"}
    assert result["previous"] == {"name": "Old", "schedule": {"interval": "DAILY", "url": "https://old"}}
    assert result["current"]["name"] == "New"


class ProjectingFeedClient(FakeCatalogFeedClient):
    """Returns only the fields the GET asked for, as the Graph API does."""

    async def get_object(self, object_id: str, *, fields=None, params=None):
        stored = await super().get_object(object_id, fields=fields, params=params)
        return {key: value for key, value in stored.items() if key in (fields or [])}


def test_update_product_feed_params_change_reads_that_fields_prior_value(monkeypatch) -> None:
    client = _use(monkeypatch, ProjectingFeedClient(objects={
        "feed_1": {"id": "feed_1", "name": "Main", "file_name": "old.csv", "country": "US"},
    }))
    result = run(catalog_feeds.update_product_feed(
        feed_id="feed_1", params={"file_name": "new.csv", "country": "CA"},
    ))
    read_fields = client.calls[0]["fields"]
    assert "file_name" in read_fields and "country" in read_fields
    assert result["previous"] == {"file_name": "old.csv", "country": "US"}
    assert result["current"] == {"file_name": "new.csv", "country": "CA"}
    assert result["previous_unavailable"] == {}


def test_update_product_feed_marks_unreadable_or_unreturned_prior_values_by_name(monkeypatch) -> None:
    client = _use(monkeypatch, ProjectingFeedClient(objects={"feed_1": {"id": "feed_1", "name": "Main"}}))
    result = run(catalog_feeds.update_product_feed(
        feed_id="feed_1",
        update_schedule={**SCHEDULE, "username": "feeduser", "password": "s3cret-value"},
        params={"some_write_only_option": True},
    ))
    assert "some_write_only_option" not in client.calls[0]["fields"]
    assert client.calls[1]["data"]["update_schedule"]["password"] == "s3cret-value"
    assert result["previous"] == {}
    assert set(result["previous_unavailable"]) == {
        "some_write_only_option", "update_schedule", "update_schedule.password",
    }
    assert "s3cret-value" not in repr(result)


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
    assert client.calls[0]["fields"] == EXPECTED_FEED_FIELDS
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
    assert client.calls[0]["fields"] == EXPECTED_UPLOAD_FIELDS
    assert (client.calls[1]["parent_id"], client.calls[1]["edge"]) == ("up_1", "errors")
    assert client.calls[1]["fields"] == EXPECTED_UPLOAD_ERROR_FIELDS
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
    assert first["fields"] == EXPECTED_UPLOAD_FIELDS
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
        "feed_1", "rules", EXPECTED_RULE_FIELDS, {"limit": 100},
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
    assert client.calls[0]["fields"] == EXPECTED_RULE_FIELDS
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
    assert (call["parent_id"], call["edge"], call["fields"]) == ("cat_1", "external_event_sources", EXPECTED_EVENT_SOURCE_FIELDS)
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
    assert first["fields"] == EXPECTED_CATALOG_SCAN_FIELDS
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
    assert result["summary"]["catalogs_scanned"] == 50
    assert "incomplete" in result["summary"]["note"]
    assert any("50-item cap" in reason for reason in result["summary"]["stop_reasons"])


def test_list_event_source_catalogs_next_page_without_cursor_is_not_complete(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("biz_1", "owned_product_catalogs"): {
            "data": [], "paging": {"next": "https://graph.invalid/?after=unextracted"},
        },
    }))
    result = run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1"))
    assert result["summary"]["complete"] is False
    assert any("no cursor" in reason for reason in result["summary"]["stop_reasons"])
    assert "no cursor" in result["summary"]["note"]


def test_list_event_source_catalogs_catalog_sources_without_cursor_is_not_complete(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("biz_1", "owned_product_catalogs"): {"data": [{"id": "cat_1"}]},
        ("cat_1", "external_event_sources"): {
            "data": [{"id": "px_9"}], "paging": {"next": "https://graph.invalid/next"},
        },
    }))
    result = run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1"))
    assert result["summary"]["complete"] is False
    assert result["summary"]["stop_reasons"] == [
        "external_event_sources of cat_1: Meta reported another page but gave no cursor to read it.",
    ]


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
    assert client.methods() == ["list_objects", "delete_edge_object"]
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


def test_connect_and_disconnect_report_complete_prior_list(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): SOURCES}))
    connected = run(catalog_feeds.connect_catalog_event_source(catalog_id="cat_1", event_source_id="px_2"))
    disconnected = run(catalog_feeds.disconnect_catalog_event_source(catalog_id="cat_1", event_source_id="px_1"))
    for result in (connected, disconnected):
        assert result["previous_complete"] is True
        assert result["missing_signals"] == []


def _capped_sources(params):
    """A connected-source list longer than the five pages the pre-read takes."""
    page = int(params.get("after") or 0)
    return {
        "data": [{"id": f"px_{page}_{i}"} for i in range(params["limit"])],
        "paging": {"next": "https://next", "cursors": {"after": str(page + 1)}},
    }


def test_connect_with_capped_pre_read_marks_lists_partial(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): _capped_sources}))
    result = run(catalog_feeds.connect_catalog_event_source(catalog_id="cat_1", event_source_id="px_new"))
    assert client.methods() == ["list_objects"] * 5 + ["create_edge_object"]
    assert result["previous_complete"] is False
    assert len(result["previous"]["connected_event_source_ids"]) == 500
    assert any("partial (500 read)" in signal and "5 pages" in signal for signal in result["missing_signals"])
    assert any("may already have been connected" in signal for signal in result["missing_signals"])


def test_connect_with_partial_pre_read_seen_source_is_already_connected(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1"}], "paging": {"next": "https://graph.invalid/next"}},
    }))
    result = run(catalog_feeds.connect_catalog_event_source(catalog_id="cat_1", event_source_id="px_1"))
    assert client.methods() == ["list_objects"]
    assert result["current"]["already_connected"] is True
    assert result["previous_complete"] is False
    assert any("no cursor" in signal for signal in result["missing_signals"])


def test_disconnect_with_partial_pre_read_sends_request_and_says_prior_state_unconfirmed(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={("cat_1", "external_event_sources"): _capped_sources}))
    result = run(catalog_feeds.disconnect_catalog_event_source(catalog_id="cat_1", event_source_id="px_beyond_cap"))
    assert client.methods() == ["list_objects"] * 5 + ["delete_edge_object"]
    assert client.calls[-1]["data"] == {"external_event_sources": ["px_beyond_cap"]}
    assert result["previous_complete"] is False
    assert result["previous"]["event_source"] is None
    assert not any("is not connected" in signal for signal in result["missing_signals"])
    assert any("not confirmed" in signal for signal in result["missing_signals"])


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
        "connect_catalog_event_source", "disconnect_catalog_event_source", "update_feed_rule",
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
    assert stats_call["fields"] == EXPECTED_EVENT_STAT_FIELDS
    check_calls = [call for call in client.calls if call.get("edge") == "da_checks"]
    assert check_calls and all(call["fields"] == EXPECTED_DA_CHECK_FIELDS for call in check_calls)
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


@pytest.mark.parametrize("result_value", ["unavailable", None, "skipped"])
def test_event_source_health_unevaluated_check_is_a_missing_signal_not_a_pass(monkeypatch, result_value) -> None:
    check = {"key": "pixel_missing_dpa_event", "result": result_value}
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): {"data": [_stat("2026-11-01", "px_1", "ViewContent", 100, 0)]},
        ("px_1", "da_checks"): {"data": [check]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    pixel = result["sources"][0]
    assert pixel["failed_checks"] == []
    assert pixel["unevaluated_checks"] == [check]
    assert any(
        "pixel_missing_dpa_event" in signal and "px_1" in signal and "could not be evaluated" in signal
        for signal in result["missing_signals"]
    )


def test_event_source_health_empty_or_paged_checks_are_missing_signals(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [
            {"id": "px_1", "source_type": "PIXEL"}, {"id": "px_2", "source_type": "PIXEL"},
        ]},
        ("cat_1", "event_stats"): {"data": [
            _stat("2026-11-01", "px_1", "ViewContent", 100, 0),
            _stat("2026-11-01", "px_2", "ViewContent", 100, 0),
        ]},
        ("px_1", "da_checks"): {"data": []},
        ("px_2", "da_checks"): {
            "data": [{"key": "pixel_missing_dpa_event", "result": "passed"}],
            "paging": {"next": "https://next", "cursors": {"after": "c2"}},
        },
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert "Meta returned no Dynamic Ads checks for source px_1." in result["missing_signals"]
    assert "da_checks for source px_2 had more pages than were read." in result["missing_signals"]


@pytest.mark.parametrize("omitted", ["total_matched_content_ids", "total_unmatched_content_ids"])
def test_event_source_health_omitted_counter_is_unknown_not_zero(monkeypatch, omitted) -> None:
    complete_row = _stat("2026-11-01", "px_1", "ViewContent", 100, 0)
    partial_row = _stat("2026-11-02", "px_1", "ViewContent", 50, 50)
    del partial_row[omitted]
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): {"data": [complete_row, partial_row]},
        ("px_1", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    pixel = result["sources"][0]
    assert pixel["window_match_rate"] is None
    if omitted == "total_matched_content_ids":
        unknown, known, known_total = "matched_content_ids", "unmatched_content_ids", 50
    else:
        unknown, known, known_total = "unmatched_content_ids", "matched_content_ids", 150
    assert pixel[unknown] is None
    assert pixel[known] == known_total
    history = {day["date"]: day for day in pixel["match_rate_history"]}
    assert history["2026-11-01"]["match_rate"] == 1.0
    assert history["2026-11-02"]["match_rate"] is None
    assert history["2026-11-02"][unknown] is None
    assert pixel["by_event"]["ViewContent"]["match_rate"] is None
    assert any("lacked a matched or unmatched counter" in signal for signal in result["missing_signals"])
    assert "low_match_rate" not in [finding["type"] for finding in result["findings"]]


def test_recommendations_omitted_counter_makes_totals_unknown(monkeypatch) -> None:
    row = _stat("2026-11-01", "px_1", "ViewContent", 100, 0)
    del row["total_unmatched_content_ids"]
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): {"data": [row]},
        ("biz_1", "adspixels"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1", business_id="biz_1"))
    assert result["metrics"]["matched_content_ids_28d"] == 100
    assert result["metrics"]["unmatched_content_ids_28d"] is None
    assert result["metrics"]["match_rate_28d"] is None
    assert any("lacked a matched or unmatched counter" in signal for signal in result["missing_signals"])


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
    assert pixel_call["fields"] == EXPECTED_PIXEL_CANDIDATE_FIELDS
    checked = [call["parent_id"] for call in client.calls if call.get("edge") == "da_checks"]
    assert checked == ["px_2", "px_3"]
    assert all(
        call["fields"] == EXPECTED_DA_CHECK_FIELDS for call in client.calls if call.get("edge") == "da_checks"
    )
    recommendations = result["recommendations"]
    assert [item["event_source_id"] for item in recommendations] == ["px_2", "px_3"]
    assert recommendations[0]["strength"] == "strong"
    assert recommendations[0]["connection_status"] == "not_connected"
    assert recommendations[0]["evidence"]
    assert recommendations[0]["next_step"]["tool"] == "connect_catalog_event_source"
    assert recommendations[1]["strength"] == "weak"
    finding_types = [finding["type"] for finding in result["findings"]]
    assert "weak_catalog_signal_coverage" in finding_types
    assert result["signals_used"]
    assert "derived" in result["method"].lower()


def _endless(rows):
    """An edge that always reports another page, so a capped scan cannot finish.

    Each page has a new cursor and its own copy of the rows (marked with the page
    number), so the read stops at the page cap rather than at a repeated page.
    """
    def page(params):
        number = int(params.get("after") or 0)
        return {
            "data": [{**row, "page": number} for row in rows],
            "paging": {"next": "https://next", "cursors": {"after": str(number + 1)}},
        }
    return page


def _stalled(rows, cursor: str = "repeat"):
    """An edge that returns the same page and the same cursor on every read."""
    def page(params):
        return {"data": [dict(row) for row in rows], "paging": {"next": "https://next", "cursors": {"after": cursor}}}
    return page


def test_recommendations_report_partial_stats_and_draw_no_coverage_conclusion(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): _endless([_stat("2026-11-01", "px_1", "ViewContent", 0, 1)]),
        ("biz_1", "adspixels"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1", business_id="biz_1"))
    metrics = result["metrics"]
    assert metrics["stats_complete"] is False
    assert metrics["sources_complete"] is True
    assert metrics["matched_content_ids_28d"] is None
    assert metrics["unmatched_content_ids_28d"] is None
    assert metrics["match_rate_28d"] is None
    assert metrics["partial_counts_from_pages_read"] == {
        "event_stat_rows": 10, "matched_content_ids": 0, "unmatched_content_ids": 10,
    }
    assert result["findings"] == []
    assert any("event_stats was only partly read" in signal and "10 pages" in signal for signal in result["missing_signals"])
    assert any("could not be determined" in signal for signal in result["missing_signals"])


def test_recommendations_partial_source_list_draws_no_unconnected_conclusion(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [], "paging": {"next": "https://graph.invalid/next"}},
        ("cat_1", "event_stats"): {"data": []},
        ("biz_1", "adspixels"): {"data": [{"id": "px_2", "name": "Maybe connected"}]},
        ("px_2", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1", business_id="biz_1"))
    assert result["metrics"]["sources_complete"] is False
    assert "no_connected_event_sources" not in [finding["type"] for finding in result["findings"]]
    assert any("whether any source is connected could not be determined" in s for s in result["missing_signals"])
    assert any("no cursor" in signal for signal in result["missing_signals"])
    assert any("may already be connected" in signal for signal in result["missing_signals"])
    [recommendation] = result["recommendations"]
    assert recommendation["connection_status"] == "unknown"


def test_event_source_health_partial_scans_draw_no_absence_conclusions(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "ds_1", "source_type": "OFFLINE"}]},
        ("cat_1", "event_stats"): _endless([_stat("2026-11-01", "px_9", "ViewContent", 1, 99)]),
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert result["metrics"]["stats_complete"] is False
    finding_types = [finding["type"] for finding in result["findings"]]
    assert "no_event_stats" not in finding_types
    assert "low_match_rate" not in finding_types
    assert any("ds_1" in signal and "unknown" in signal for signal in result["missing_signals"])
    assert any("only partly read" in signal for signal in result["missing_signals"])


def test_event_source_health_partial_empty_source_list_is_not_no_sources(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): _endless([]),
        ("cat_1", "event_stats"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert result["metrics"]["sources_complete"] is False
    assert result["findings"] == []
    assert any("could not be determined" in signal for signal in result["missing_signals"])


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


def test_intent_guide_and_mutation_paths_cover_feed_and_event_source_tools(monkeypatch) -> None:
    from meta_ads_mcp.tools import utility

    route = utility.INTENT_GUIDE["manage_product_catalog_feeds_and_event_sources"]
    routed = set(route["recommended_order"]) | set(route["avoid_unless_needed"])
    assert set(utility.TOOL_GROUPS["catalog"]) & {
        "list_product_feeds", "get_catalog_event_source_health",
    } <= routed
    monkeypatch.delenv("META_READ_ONLY", raising=False)
    reload_settings()
    paths = run(utility.list_mutation_tools())["common_paths"]
    assert "disconnect_catalog_event_source" in paths["product_catalog_event_sources"]
    assert "delete_product_feed" in paths["product_catalog_feeds"]


def test_graph_client_delete_edge_object_sends_delete_with_form_data(monkeypatch) -> None:
    from meta_ads_mcp.graph_api import GraphAPIClient

    sent: list[tuple[str, str, Any]] = []

    async def fake_request(self, method, endpoint, *, params=None, data=None, files=None, base_url=None, use_auth_header=True):
        sent.append((method, endpoint, data))
        return {"success": True}

    monkeypatch.setattr(GraphAPIClient, "request", fake_request)
    client = GraphAPIClient(reload_settings())
    result = run(client.delete_edge_object("cat_1", "external_event_sources", data={"external_event_sources": ["px_1"]}))
    assert sent == [("DELETE", "cat_1/external_event_sources", {"external_event_sources": ["px_1"]})]
    assert result == {"success": True}


def test_create_product_feed_redacts_a_password_meta_echoes_back(monkeypatch) -> None:
    """If Meta returns the schedule it was sent, the password must still not come back."""

    class EchoingClient(FakeCatalogFeedClient):
        async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
            return {"id": "feed_new", "schedule": data["schedule"], "update_schedule": data.get("update_schedule")}

    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", EchoingClient)
    secret = "synthetic-feed-secret"
    result = run(
        catalog_feeds.create_product_feed(
            "cat_1",
            "Feed",
            schedule={"url": "https://example.invalid/feed.csv", "interval": "DAILY", "password": secret},
            update_schedule={"url": "https://example.invalid/delta.csv", "interval": "HOURLY", "password": secret},
        )
    )
    assert secret not in repr(result)
    assert result["created"]["id"] == "feed_new"
    assert result["created"]["schedule"]["password"] == "[redacted]"
    assert result["created"]["update_schedule"]["password"] == "[redacted]"


# phase two: update_feed_rule ---------------------------------------------------------------

LETTER_CASE_RULE = {
    "id": "rule_1", "attribute": "title", "rule_type": "letter_case_rule", "params": {"type": "to_upper"},
}


def test_update_feed_rule_reads_then_updates_and_returns_previous(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"rule_1": LETTER_CASE_RULE}))
    result = run(catalog_feeds.update_feed_rule(rule_id="rule_1", params={"type": "to_lower"}))
    assert client.methods() == ["get_object", "update_object"]
    assert client.calls[0]["object_id"] == "rule_1"
    assert client.calls[0]["fields"] == EXPECTED_RULE_FIELDS
    assert client.calls[1]["object_id"] == "rule_1"
    assert client.calls[1]["data"] == {"params": {"type": "to_lower"}}
    assert result["ok"] is True
    assert result["action"] == "update_feed_rule"
    assert result["target"] == {"rule_id": "rule_1"}
    assert result["previous"] == {"attribute": "title", "rule_type": "letter_case_rule", "params": {"type": "to_upper"}}
    assert result["current"] == {"attribute": "title", "rule_type": "letter_case_rule", "params": {"type": "to_lower"}}
    assert result["dry_run"] is False


@pytest.mark.parametrize(("rule", "params"), [
    (LETTER_CASE_RULE, {"type": "shout"}),
    ({**LETTER_CASE_RULE, "rule_type": "regex_replace_rule", "params": {"regex": "a"}}, {"regex": "("}),
    ({**LETTER_CASE_RULE, "rule_type": "mapping_rule", "params": {"map_from": "x"}}, {"other": "y"}),
    ({**LETTER_CASE_RULE, "rule_type": "fallback_rule", "params": {"user_default_value": "x"}}, {"user_default_value": " "}),
    ({**LETTER_CASE_RULE, "rule_type": "value_mapping_rule", "params": {"a": "b"}}, {str(i): "v" for i in range(11)}),
])
def test_update_feed_rule_checks_params_against_the_rules_own_type(monkeypatch, rule, params) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"rule_1": rule}))
    with pytest.raises(ValidationError):
        run(catalog_feeds.update_feed_rule(rule_id="rule_1", params=params))
    assert client.methods() == ["get_object"]


@pytest.mark.parametrize("kwargs", [
    {"rule_id": " ", "params": {"type": "to_lower"}},
    {"rule_id": "rule_1", "params": {}},
    {"rule_id": "rule_1", "params": {"type": 5}},
    {"rule_id": "rule_1", "params": "type=to_lower"},
])
def test_update_feed_rule_rejects_bad_input_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.update_feed_rule(**kwargs))


def test_update_feed_rule_dry_run_reads_but_sends_no_write(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"rule_1": LETTER_CASE_RULE}))
    result = run(catalog_feeds.update_feed_rule(rule_id="rule_1", params={"type": "to_lower"}, dry_run=True))
    assert client.methods() == ["get_object"]
    assert result["dry_run"] is True
    assert result["sent"] is False
    assert result["would_send"] == {"method": "POST", "path": "rule_1", "data": {"params": {"type": "to_lower"}}}
    assert result["previous"]["params"] == {"type": "to_upper"}
    assert result["current"]["params"] == {"type": "to_lower"}


def test_update_feed_rule_read_failure_sends_no_update(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(errors={"rule_1": _permission_error()}))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.update_feed_rule(rule_id="rule_1", params={"type": "to_lower"}))
    assert client.methods() == ["get_object"]


def test_update_feed_rule_with_identical_params_sends_nothing(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"rule_1": LETTER_CASE_RULE}))
    result = run(catalog_feeds.update_feed_rule(rule_id="rule_1", params={"type": "to_upper"}))
    assert client.methods() == ["get_object"]
    assert result["current"]["unchanged"] is True


def test_update_feed_rule_unknown_rule_type_runs_generic_checks_and_says_so(monkeypatch) -> None:
    rule = {"id": "rule_1", "attribute": "title", "params": {"x": "y"}}
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"rule_1": rule}))
    result = run(catalog_feeds.update_feed_rule(rule_id="rule_1", params={"x": "z"}))
    assert client.methods() == ["get_object", "update_object"]
    assert any("rule_type" in note for note in result["missing_signals"])


def test_update_feed_rule_docstring_says_params_replace_and_type_is_fixed() -> None:
    doc = inspect.getdoc(catalog_feeds.update_feed_rule).lower()
    assert "replace" in doc
    assert "attribute" in doc and "rule_type" in doc and "delete" in doc


@pytest.mark.parametrize("routed", [False, True])
def test_update_feed_rule_is_a_write_blocked_in_read_only(monkeypatch, routed) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.read_only import read_only_tool_names
    from meta_ads_mcp.tool_contracts import build_tool_contracts
    from meta_ads_mcp.tools.utility import TOOL_GROUPS

    assert "update_feed_rule" in TOOL_GROUPS["writes"]
    assert all("update_feed_rule" not in names for group, names in TOOL_GROUPS.items() if group != "writes")
    monkeypatch.setenv("META_READ_ONLY", "true")
    reload_settings()
    _no_client(monkeypatch)
    try:
        assert "update_feed_rule" not in read_only_tool_names()
        components = mcp_server.local_provider._components.values()
        contracts = build_tool_contracts([c for c in components if getattr(c, "name", None)])
        assert contracts["update_feed_rule"].effect == "write"
        arguments = {"rule_id": "123", "params": {"type": "to_lower"}}
        tool, payload = ("call_tool", {"name": "update_feed_rule", "arguments": arguments}) if routed else ("update_feed_rule", arguments)
        with pytest.raises(ToolError, match="META_READ_ONLY"):
            run(mcp_server.call_tool(tool, payload))
    finally:
        monkeypatch.delenv("META_READ_ONLY")
        reload_settings()


# phase two: feed credentials and local file uploads -------------------------------------------


def test_feed_credentials_are_never_returned_or_logged(monkeypatch, caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    secret = "synthetic-feed-password-7781"

    class EchoingClient(FakeCatalogFeedClient):
        async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
            await super().create_edge_object(parent_id, edge, data=data, files=files)
            return {"id": "created_1", **data}

    client = _use(monkeypatch, EchoingClient(objects={"feed_1": {"id": "feed_1", "name": "Main"}}))
    schedule = {**SCHEDULE, "username": "feeduser", "password": secret}
    results = [
        run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Feed", schedule=schedule, update_schedule=schedule)),
        run(catalog_feeds.update_product_feed(feed_id="feed_1", schedule=schedule)),
        run(catalog_feeds.create_product_feed_upload(
            feed_id="feed_1", url="https://example.com/feed.csv", username="feeduser", password=secret,
        )),
        run(catalog_feeds.create_product_feed_upload(
            feed_id="feed_1", url="https://example.com/feed.csv", username="feeduser", password=secret, dry_run=True,
        )),
    ]
    upload_posts = [call for call in client.calls if call.get("edge") == "uploads"]
    assert upload_posts[0]["data"] == {
        "url": "https://example.com/feed.csv", "username": "feeduser", "password": secret,
    }
    assert len(upload_posts) == 1
    for result in results:
        assert secret not in repr(result)
    assert results[3]["would_send"]["data"]["password"] == "[redacted]"
    assert secret not in caplog.text


def test_feed_reads_mask_a_password_embedded_in_a_schedule_url(monkeypatch) -> None:
    secret = "synthetic-url-password-4410"
    feed = {"id": "feed_1", "schedule": {"url": f"ftp://user:{secret}@example.com/feed.csv", "interval": "DAILY"}}
    _use(monkeypatch, FakeCatalogFeedClient(
        objects={"feed_1": feed},
        edges={("cat_1", "product_feeds"): {"data": [feed]}},
    ))
    one = run(catalog_feeds.list_product_feeds(feed_id="feed_1"))
    many = run(catalog_feeds.list_product_feeds(catalog_id="cat_1"))
    assert secret not in repr(one) and secret not in repr(many)
    assert one["item"]["schedule"]["url"] == "ftp://user:[redacted]@example.com/feed.csv"


@pytest.mark.parametrize("url", [
    "https://user:pw-in-url@example.com/feed.csv",
    "ftp://user:pw-in-url@example.com/feed.csv",
])
def test_urls_with_embedded_credentials_are_rejected_without_echo(monkeypatch, url) -> None:
    _no_client(monkeypatch)
    calls = [
        lambda: catalog_feeds.create_product_feed(catalog_id="cat_1", name="F", schedule={"url": url, "interval": "DAILY"}),
        lambda: catalog_feeds.update_product_feed(feed_id="feed_1", update_schedule={"url": url, "interval": "DAILY"}),
    ]
    if url.startswith("https"):
        calls.append(lambda: catalog_feeds.create_product_feed_upload(feed_id="feed_1", url=url))
    for call in calls:
        with pytest.raises(ValidationError) as caught:
            run(call())
        message = str(caught.value)
        assert "pw-in-url" not in message
        assert "username" in message and "password" in message


CSV_RECORD = "id,title,price\n4471,Synthetic Shirt,10 USD"


def test_a_csv_like_value_is_never_echoed_in_an_error(monkeypatch) -> None:
    _no_client(monkeypatch)
    calls = [
        lambda: catalog_feeds.create_product_feed_upload(feed_id="feed_1", url=CSV_RECORD),
        lambda: catalog_feeds.create_product_feed(catalog_id="cat_1", name="F", schedule={"url": CSV_RECORD, "interval": "DAILY"}),
        lambda: catalog_feeds.update_product_feed(feed_id="feed_1", schedule={"url": "https://e.com/f.csv", "interval": CSV_RECORD}),
        lambda: catalog_feeds.update_product_feed(
            feed_id="feed_1", schedule={"url": "https://e.com/f.csv", "interval": "WEEKLY", "day_of_week": CSV_RECORD},
        ),
        lambda: catalog_feeds.update_product_feed(
            feed_id="feed_1", schedule={"url": "https://e.com/f.csv", "interval": "DAILY", "hour": CSV_RECORD},
        ),
        lambda: catalog_feeds.update_product_feed(
            feed_id="feed_1", schedule={"url": "https://e.com/f.csv", "interval": "DAILY", CSV_RECORD: "x"},
        ),
    ]
    for call in calls:
        with pytest.raises(ValidationError) as caught:
            run(call())
        assert "Synthetic Shirt" not in str(caught.value)
        assert "4471" not in str(caught.value)


def test_create_product_feed_upload_sends_a_local_file_read_by_the_server(monkeypatch, tmp_path) -> None:
    content = b"id,title,price\n1,Synthetic Mug,5 USD\n"
    path = tmp_path / "products.csv"
    path.write_bytes(content)
    client = _use(monkeypatch, FakeCatalogFeedClient())
    result = run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", file_path=str(path), update_only=True))
    [call] = client.calls
    assert (call["method"], call["parent_id"], call["edge"]) == ("create_edge_object", "feed_1", "uploads")
    assert call["data"] == {"update_only": True}
    assert call["files"] == {"file": ("products.csv", content, "text/csv")}
    assert result["created"] == {"id": "created_1"}
    assert result["upload_source"] == {
        "type": "file", "file_name": "products.csv", "size_bytes": len(content), "content_type": "text/csv",
    }
    assert "Synthetic Mug" not in repr(result)


@pytest.mark.parametrize(("name", "content_type"), [
    ("feed.tsv", "text/tab-separated-values"),
    ("feed.xml", "application/xml"),
    ("feed.csv.gz", "application/gzip"),
    ("feed.zip", "application/zip"),
    ("feed.bz2", "application/x-bzip2"),
])
def test_create_product_feed_upload_sets_the_documented_file_type(monkeypatch, tmp_path, name, content_type) -> None:
    path = tmp_path / name
    path.write_bytes(b"x")
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", file_path=str(path)))
    assert client.calls[0]["files"]["file"][2] == content_type
    assert client.calls[0]["data"] == {}


def test_create_product_feed_upload_file_dry_run_sends_nothing(monkeypatch, tmp_path) -> None:
    path = tmp_path / "products.csv"
    content = b"id,title\n1,Synthetic Lamp\n"
    path.write_bytes(content)
    _no_client(monkeypatch)
    result = run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", file_path=str(path), dry_run=True))
    assert result["dry_run"] is True and result["sent"] is False
    assert result["would_send"] == {
        "method": "POST",
        "path": "feed_1/uploads",
        "data": {},
        "files": {"file": {"file_name": "products.csv", "size_bytes": len(content), "content_type": "text/csv"}},
    }
    assert "Synthetic Lamp" not in repr(result)


def test_create_product_feed_upload_url_call_is_unchanged(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    result = run(catalog_feeds.create_product_feed_upload("feed_1", "https://example.com/feed.csv", True))
    [call] = client.calls
    assert call["data"] == {"url": "https://example.com/feed.csv", "update_only": True}
    assert call["files"] is None
    assert result["created"] == {"id": "created_1"}


def _bad_file_cases(tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    secret_named = tmp_path / "id_rsa"
    secret_named.write_bytes(b"synthetic-key-material")
    good = tmp_path / "ok.csv"
    good.write_bytes(b"id\n1\n")
    return [
        {"file_path": str(tmp_path / "missing.csv")},
        {"file_path": str(tmp_path)},
        {"file_path": str(empty)},
        {"file_path": str(secret_named)},
        {"file_path": " "},
        {"file_path": str(good), "url": "https://example.com/feed.csv"},
        {"file_path": str(good), "username": "u", "password": "p"},
        {},
        {"url": "https://example.com/feed.csv", "username": "u"},
        {"url": "https://example.com/feed.csv", "password": "p"},
    ]


def test_create_product_feed_upload_rejects_bad_sources_before_any_call(monkeypatch, tmp_path) -> None:
    _no_client(monkeypatch)
    for kwargs in _bad_file_cases(tmp_path):
        with pytest.raises(ValidationError) as caught:
            run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", **kwargs))
        message = str(caught.value)
        assert str(tmp_path) not in message
        assert "synthetic-key-material" not in message


def test_create_product_feed_upload_rejects_a_file_over_the_size_cap(monkeypatch, tmp_path) -> None:
    path = tmp_path / "big.csv"
    path.write_bytes(b"0123456789")
    monkeypatch.setattr(catalog_feeds, "MAX_FEED_FILE_BYTES", 5)
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="larger"):
        run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", file_path=str(path)))


# phase two: health scoped to one source, and its rate across catalogs ---------------------------


def _stat_other(day: str, source: str, event: str, matched: int, unmatched: int, other: int | None) -> dict[str, Any]:
    row = _stat(day, source, event, matched, unmatched)
    if other is not None:
        row["total_content_ids_matched_other_catalogs"] = other
    return row


def test_event_source_health_can_be_scoped_to_one_source(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): SOURCES,
        ("cat_1", "event_stats"): {"data": [
            _stat_other("2026-11-01", "px_1", "ViewContent", 60, 40, 30),
            _stat_other("2026-11-01", "app_1", "ViewContent", 5, 5, 0),
        ]},
        ("px_1", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1", event_source_id="px_1"))
    assert [source["id"] for source in result["sources"]] == ["px_1"]
    assert [call["parent_id"] for call in client.calls if call.get("edge") == "da_checks"] == ["px_1"]
    assert result["scope"] == {"catalog_id": "cat_1", "event_source_id": "px_1"}
    assert result["metrics"]["connected_sources"] == 1
    assert not any("app_1" in signal for signal in result["missing_signals"])


def test_event_source_health_reports_the_sources_rate_across_its_catalogs(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): {"data": [
            _stat_other("2026-11-01", "px_1", "ViewContent", 50, 30, 20),
            _stat_other("2026-11-02", "px_1", "Purchase", 10, 10, 10),
        ]},
        ("px_1", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1", event_source_id="px_1"))
    across = result["sources"][0]["across_catalogs"]
    assert across["this_catalog_match_rate"] == 0.6
    assert across["content_ids_matched_other_catalogs"] == 30
    assert across["other_catalogs_match_share"] == 0.3
    assert across["all_catalogs_match_rate_range"] == {"low": 0.6, "high": 0.9}
    assert result["metrics"]["this_catalog_match_rate"] == 0.6
    assert result["metrics"]["all_catalogs_match_rate_range"] == {"low": 0.6, "high": 0.9}
    assert "range" in result["across_catalogs_definition"]


def test_event_source_health_across_catalog_rate_is_unknown_when_meta_omits_the_counter(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): {"data": [
            _stat_other("2026-11-01", "px_1", "ViewContent", 50, 30, 20),
            _stat_other("2026-11-02", "px_1", "ViewContent", 10, 10, None),
        ]},
        ("px_1", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    across = result["sources"][0]["across_catalogs"]
    assert across["this_catalog_match_rate"] == 0.6
    assert across["content_ids_matched_other_catalogs"] is None
    assert across["other_catalogs_match_share"] is None
    assert across["all_catalogs_match_rate_range"] is None


def test_event_source_health_across_catalog_rate_needs_a_finished_read(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): _endless([_stat_other("2026-11-01", "px_1", "ViewContent", 5, 5, 5)]),
        ("px_1", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1", event_source_id="px_1"))
    across = result["sources"][0]["across_catalogs"]
    assert across["this_catalog_match_rate"] is None
    assert across["all_catalogs_match_rate_range"] is None
    assert "partly read" in across["note"]


def test_event_source_health_scope_to_unconnected_source_is_refused_after_a_full_read(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): SOURCES,
        ("cat_1", "event_stats"): {"data": []},
    }))
    with pytest.raises(ValidationError, match="not connected"):
        run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1", event_source_id="px_404"))
    assert not any(call.get("edge") == "da_checks" for call in client.calls)


def test_event_source_health_scope_beyond_a_partial_source_list_is_reported_not_refused(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1"}], "paging": {"next": "https://graph.invalid/next"}},
        ("cat_1", "event_stats"): {"data": [_stat_other("2026-11-01", "px_9", "ViewContent", 9, 1, 0)]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1", event_source_id="px_9"))
    [source] = result["sources"]
    assert source["id"] == "px_9"
    assert source["window_match_rate"] == 0.9
    assert any("px_9" in signal and "not among" in signal for signal in result["missing_signals"])


def test_event_source_health_unscoped_call_is_unchanged_apart_from_new_fields(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): SOURCES,
        ("cat_1", "event_stats"): {"data": [_stat("2026-11-01", "px_1", "ViewContent", 90, 10)]},
        ("px_1", "da_checks"): {"data": [{"key": "pixel_missing_dpa_event", "result": "passed"}]},
        ("app_1", "da_checks"): {"data": [{"key": "x", "result": "passed"}]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert [(c["parent_id"], c["edge"]) for c in client.calls] == [
        ("cat_1", "external_event_sources"), ("cat_1", "event_stats"), ("px_1", "da_checks"), ("app_1", "da_checks"),
    ]
    assert result["scope"] == {"catalog_id": "cat_1"}
    assert [source["id"] for source in result["sources"]] == ["px_1", "app_1"]


# phase two: list_event_source_catalogs match rates ---------------------------------------------


def _scan_edges() -> dict[tuple[str, str], Any]:
    return {
        ("biz_1", "owned_product_catalogs"): {"data": [
            {"id": "cat_1", "name": "A"}, {"id": "cat_2", "name": "B"}, {"id": "cat_3", "name": "C"},
        ]},
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1"}]},
        ("cat_2", "external_event_sources"): {"data": [{"id": "px_1"}, {"id": "px_2"}]},
        ("cat_3", "external_event_sources"): {"data": [{"id": "px_2"}]},
        ("cat_1", "event_stats"): {"data": [
            _stat("2026-11-01", "px_1", "ViewContent", 40, 60),
            _stat("2026-11-01", "px_1", "Purchase", 10, 0),
            _stat("2026-11-01", "px_9", "ViewContent", 1000, 0),
        ]},
        ("cat_2", "event_stats"): {"data": [
            _stat("2026-11-01", "px_1", "ViewContent", 90, 10),
            _stat("2026-11-01", "px_1", "Purchase", 0, 10),
        ]},
    }


def test_list_event_source_catalogs_without_match_rates_is_unchanged(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges=_scan_edges()))
    result = run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1"))
    assert [(c["parent_id"], c["edge"]) for c in client.calls] == [
        ("biz_1", "owned_product_catalogs"),
        ("cat_1", "external_event_sources"),
        ("cat_2", "external_event_sources"),
        ("cat_3", "external_event_sources"),
    ]
    assert result["items"] == [{"id": "cat_1", "name": "A"}, {"id": "cat_2", "name": "B"}]
    assert set(result["summary"]) == {
        "count", "event_source_id", "business_id", "catalogs_scanned", "complete", "stop_reasons", "method", "note",
    }


def test_list_event_source_catalogs_match_rates_per_connected_catalog(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges=_scan_edges()))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True,
    ))
    stats_calls = [c for c in client.calls if c["edge"] == "event_stats"]
    assert [c["parent_id"] for c in stats_calls] == ["cat_1", "cat_2"]
    assert all(c["fields"] == EXPECTED_EVENT_STAT_FIELDS for c in stats_calls)
    items = {item["id"]: item for item in result["items"]}
    assert items["cat_1"]["match_rate"] == {
        "status": "computed", "match_rate": 0.4545, "matched_content_ids": 50, "unmatched_content_ids": 60,
        "event_stat_rows": 2, "events": ["Purchase", "ViewContent"], "stats_complete": True,
    }
    assert items["cat_2"]["match_rate"]["match_rate"] == round(90 / 110, 4)
    rates = result["summary"]["match_rates"]
    assert rates["ranked_by_match_rate"] == ["cat_2", "cat_1"]
    assert rates["ranking_covers_every_connected_catalog"] is True
    assert rates["catalogs_checked"] == 2 and rates["catalogs_with_rate"] == 2
    assert rates["catalogs_not_checked"] == []
    assert "derived" in rates["method"].lower()
    assert "Meta does not publish" in rates["definition"]


def test_list_event_source_catalogs_match_rates_for_one_event(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges=_scan_edges()))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True, event_name="purchase",
    ))
    items = {item["id"]: item for item in result["items"]}
    assert items["cat_1"]["match_rate"]["match_rate"] == 1.0
    assert items["cat_2"]["match_rate"]["match_rate"] == 0.0
    assert result["summary"]["match_rates"]["event_name"] == "Purchase"
    assert result["summary"]["match_rates"]["ranked_by_match_rate"] == ["cat_1", "cat_2"]


@pytest.mark.parametrize("kwargs", [
    {"event_name": "Purchase"},
    {"include_match_rates": True, "event_name": "Checkout"},
])
def test_list_event_source_catalogs_rejects_bad_match_rate_input_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1", **kwargs))


def test_list_event_source_catalogs_match_rate_unknown_when_a_counter_is_omitted(monkeypatch) -> None:
    row = _stat("2026-11-01", "px_1", "ViewContent", 40, 60)
    del row["total_unmatched_content_ids"]
    _use(monkeypatch, FakeCatalogFeedClient(edges=_scan_edges() | {("cat_1", "event_stats"): {"data": [row]}}))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True,
    ))
    first = result["items"][0]["match_rate"]
    assert first["status"] == "counters_missing"
    assert first["match_rate"] is None
    assert first["unmatched_content_ids"] is None
    rates = result["summary"]["match_rates"]
    assert rates["ranked_by_match_rate"] == ["cat_2"]
    assert rates["ranking_covers_every_connected_catalog"] is False


def test_list_event_source_catalogs_match_rate_needs_a_finished_stats_read(monkeypatch) -> None:
    edges = _scan_edges() | {("cat_1", "event_stats"): _endless([_stat("2026-11-01", "px_1", "ViewContent", 1, 1)])}
    _use(monkeypatch, FakeCatalogFeedClient(edges=edges))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True,
    ))
    first = result["items"][0]["match_rate"]
    assert first["status"] == "partial_read"
    assert first["match_rate"] is None
    assert first["stats_complete"] is False
    assert first["partial_counts_from_pages_read"]["matched_content_ids"] > 0
    assert "cat_1" not in result["summary"]["match_rates"]["ranked_by_match_rate"]


def test_list_event_source_catalogs_match_rate_no_events_is_not_zero(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges=_scan_edges() | {("cat_1", "event_stats"): {"data": []}}))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True,
    ))
    first = result["items"][0]["match_rate"]
    assert first["status"] == "no_events_in_window"
    assert first["match_rate"] is None


def test_list_event_source_catalogs_match_rates_are_capped_and_say_which_were_skipped(monkeypatch) -> None:
    catalogs = [{"id": f"cat_{i}"} for i in range(12)]
    edges: dict[tuple[str, str], Any] = {("biz_1", "owned_product_catalogs"): {"data": catalogs}}
    for catalog in catalogs:
        edges[(catalog["id"], "external_event_sources")] = {"data": [{"id": "px_1"}]}
        edges[(catalog["id"], "event_stats")] = {"data": [_stat("2026-11-01", "px_1", "ViewContent", 1, 1)]}
    client = _use(monkeypatch, FakeCatalogFeedClient(edges=edges))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True,
    ))
    assert len([c for c in client.calls if c["edge"] == "event_stats"]) == 10
    rates = result["summary"]["match_rates"]
    assert rates["catalogs_checked"] == 10
    assert rates["catalogs_not_checked"] == ["cat_10", "cat_11"]
    assert rates["ranking_covers_every_connected_catalog"] is False
    assert result["items"][11]["match_rate"]["status"] == "not_checked"


def test_list_event_source_catalogs_incomplete_scan_ranking_is_not_complete(monkeypatch) -> None:
    edges = _scan_edges() | {
        ("biz_1", "owned_product_catalogs"): {"data": [{"id": "cat_1"}], "paging": {"next": "https://graph.invalid/next"}},
    }
    _use(monkeypatch, FakeCatalogFeedClient(edges=edges))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True,
    ))
    assert result["summary"]["complete"] is False
    assert result["summary"]["match_rates"]["ranking_covers_every_connected_catalog"] is False


# phase two: credentials embedded in URLs are masked on every path --------------------------------

URL_SECRET = "pw-in-url-5521"
CRED_URL = f"ftp://feeduser:{URL_SECRET}@feeds.example.com/path/feed.csv"
MASKED_URL = "ftp://feeduser:[redacted]@feeds.example.com/path/feed.csv"


class CredentialUrlClient(FakeCatalogFeedClient):
    """Meta-shaped responses that carry a URL with an embedded password in every place a URL can appear."""

    def __init__(self) -> None:
        upload = {"id": "up_1", "url": CRED_URL, "end_time": "2026-11-01T00:00:00+0000", "error_count": 1}
        feed = {
            "id": "feed_1", "name": "Main",
            "schedule": {"url": CRED_URL, "interval": "DAILY"},
            "update_schedule": {"url": CRED_URL, "interval": "HOURLY"},
            "latest_upload": dict(upload),
        }
        super().__init__(
            objects={"feed_1": feed, "up_1": upload},
            edges={
                ("cat_1", "product_feeds"): {"data": [feed]},
                ("feed_1", "uploads"): {"data": [upload]},
                ("up_1", "errors"): {"data": [{"id": "e1", "description": f"Could not fetch {CRED_URL} today"}]},
            },
        )

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        await super().create_edge_object(parent_id, edge, data=data, files=files)
        return {"id": "created_1", "schedule": {"url": CRED_URL}, "url": CRED_URL, "message": f"fetching {CRED_URL}"}


def test_a_password_embedded_in_any_returned_url_is_masked(monkeypatch, tmp_path) -> None:
    _use(monkeypatch, CredentialUrlClient())
    feed_file = tmp_path / "feed.csv"
    feed_file.write_bytes(b"id\n1\n")
    results = {
        "feed": run(catalog_feeds.list_product_feeds(feed_id="feed_1")),
        "feeds": run(catalog_feeds.list_product_feeds(catalog_id="cat_1")),
        "upload": run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1")),
        "uploads": run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1")),
        "create": run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="F", schedule=SCHEDULE)),
        "update": run(catalog_feeds.update_product_feed(feed_id="feed_1", schedule=SCHEDULE, update_schedule=SCHEDULE)),
        "delete": run(catalog_feeds.delete_product_feed(feed_id="feed_1")),
        "upload_url": run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", url="https://e.com/f.csv")),
        "upload_file": run(catalog_feeds.create_product_feed_upload(feed_id="feed_1", file_path=str(feed_file))),
    }
    for name, result in results.items():
        assert URL_SECRET not in repr(result), name
    assert results["feed"]["item"]["latest_upload"]["url"] == MASKED_URL
    assert results["feed"]["item"]["schedule"]["url"] == MASKED_URL
    assert results["feeds"]["items"][0]["latest_upload"]["url"] == MASKED_URL
    assert results["upload"]["item"]["url"] == MASKED_URL
    assert results["upload"]["item"]["errors"][0]["description"] == f"Could not fetch {MASKED_URL} today"
    assert results["uploads"]["items"][0]["url"] == MASKED_URL
    assert results["update"]["previous"]["schedule"]["url"] == MASKED_URL
    assert results["delete"]["previous"]["update_schedule"]["url"] == MASKED_URL
    assert results["create"]["created"]["schedule"]["url"] == MASKED_URL


def test_dry_run_masks_the_password_but_says_a_credential_is_sent(monkeypatch) -> None:
    _no_client(monkeypatch)
    secret = "dry-run-secret-3390"
    result = run(catalog_feeds.create_product_feed_upload(
        feed_id="feed_1", url="https://e.com/f.csv", username="feeduser", password=secret, dry_run=True,
    ))
    assert secret not in repr(result)
    assert result["would_send"]["data"]["password"] == "[redacted]"
    assert result["would_send"]["data"]["username"] == "feeduser"
    assert result["upload_source"]["credentials_sent"] is True


def test_meta_error_text_quoting_a_credential_url_is_masked(monkeypatch, caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    error = MetaApiError(
        message=f"Could not fetch {CRED_URL}",
        status_code=400,
        code=100,
        user_message=f"Check {CRED_URL}",
        error_data={"url": CRED_URL},
        details={"error": {"message": f"Could not fetch {CRED_URL}"}},
    )
    _use(monkeypatch, FakeCatalogFeedClient(errors={"feed_1": error, ("feed_1", "uploads"): error}))
    for call in (
        lambda: catalog_feeds.list_product_feeds(feed_id="feed_1"),
        lambda: catalog_feeds.list_product_feed_uploads(feed_id="feed_1"),
        lambda: catalog_feeds.update_product_feed(feed_id="feed_1", name="New"),
    ):
        with pytest.raises(MetaApiError) as caught:
            run(call())
        assert URL_SECRET not in str(caught.value)
        assert URL_SECRET not in repr(caught.value.to_public_dict())
        assert URL_SECRET not in repr(caught.value)
        assert "feeds.example.com/path/feed.csv" in str(caught.value)
    assert URL_SECRET not in caplog.text


@pytest.mark.parametrize(("tool", "arguments", "secret"), [
    ("create_product_feed_upload", {"feed_id": "1", "url": "https://e.com/f.csv", "username": "u", "password": 987654321}, "987654321"),
    ("create_product_feed_upload", {"feed_id": "1", "url": ["listed-secret-6612"]}, "listed-secret-6612"),
    ("create_product_feed_upload", {"feed_id": "1", "file_path": ["listed-secret-6612"]}, "listed-secret-6612"),
    ("create_product_feed_upload", {"feed_id": "1", "url": "https://e.com/f.csv", "username": ["listed-secret-6612"], "password": "p"}, "listed-secret-6612"),
    ("create_product_feed", {"catalog_id": "1", "name": "F", "schedule": "url=ftp://u:str-secret-7713@h/f.csv"}, "str-secret-7713"),
    ("update_product_feed", {"feed_id": "1", "update_schedule": ["str-secret-7713"]}, "str-secret-7713"),
])
def test_wrongly_typed_secret_arguments_are_not_quoted_by_the_framework(monkeypatch, caplog, tool, arguments, secret) -> None:
    import logging

    from meta_ads_mcp.coordinator import mcp_server

    caplog.set_level(logging.DEBUG)
    _no_client(monkeypatch)
    with pytest.raises(Exception) as caught:
        run(mcp_server.call_tool(tool, arguments))
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in caplog.text


# phase two: recommendations flag paged da_checks --------------------------------------------------


def test_recommendations_flag_paged_da_checks_and_do_not_guess(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": []},
        ("cat_1", "event_stats"): {"data": []},
        ("biz_1", "adspixels"): {"data": [{"id": "px_2", "name": "Paged"}]},
        ("px_2", "da_checks"): {
            "data": [{"key": "pixel_other_check", "result": "passed"}],
            "paging": {"next": "https://next", "cursors": {"after": "c2"}},
        },
    }))
    result = run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1", business_id="biz_1"))
    [recommendation] = result["recommendations"]
    assert recommendation["strength"] == "unknown"
    assert "more pages" in recommendation["reason"]
    assert "da_checks for pixel px_2 had more pages than were read." in result["missing_signals"]


@pytest.mark.parametrize(
    "url,secret",
    [
        # An email address as the user name puts an "@" before the password.
        ("ftp://feeds@example.com:s3cr3t-pass@ftp.example.invalid/feed.csv", "s3cr3t-pass"),
        ("sftp://ops.team@example.com:p%40ss:w0rd@host.example.invalid:22/dir/feed.tsv", "p%40ss:w0rd"),
        # A password that itself contains "@" or ":".
        ("https://user:pa@ss:word@host.example.invalid/feed.csv", "pa@ss:word"),
        ("https://user:plainsecret@host.example.invalid/feed.csv?x=1#frag", "plainsecret"),
        ("FTP://USER:UPPERsecret@HOST.EXAMPLE.INVALID/feed.csv", "UPPERsecret"),
    ],
)
def test_url_password_masking_matches_how_a_url_is_parsed(url, secret) -> None:
    """The mask must find the password wherever a URL parser would, not only in simple cases."""
    masked = catalog_feeds._mask_url_passwords(f"fetch failed for {url} today")
    assert secret not in masked
    assert "[redacted]@" in masked
    assert masked.startswith("fetch failed for ") and masked.endswith(" today")
    # The host and path stay, so the source can still be identified.
    assert "example.invalid" in masked.lower()


@pytest.mark.parametrize(
    "text",
    [
        "https://host.example.invalid/feed.csv",
        "https://user@host.example.invalid/feed.csv",
        "contact feeds@example.com about https://host.example.invalid/a:b@c",
        "ratio 3:1 @ noon",
    ],
)
def test_url_password_masking_leaves_text_without_a_url_password_alone(text) -> None:
    assert catalog_feeds._mask_url_passwords(text) == text


def test_feed_file_upload_only_reads_files_from_the_upload_folder(monkeypatch, tmp_path) -> None:
    """A path from the model must not be able to send some other file on this machine to Meta."""
    allowed = tmp_path / "uploads"
    allowed.mkdir()
    monkeypatch.setenv("META_UPLOAD_DIR", str(allowed))
    reload_settings()
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    outside = tmp_path / "private-export.csv"
    outside.write_text("id\nsecret-row\n")
    (allowed / "link.csv").symlink_to(outside)
    for path, expected in (
        (str(outside), "inside the upload folder"),
        ("../private-export.csv", "inside the upload folder"),
        ("link.csv", "inside the upload folder"),
    ):
        with pytest.raises(ValidationError, match=expected) as error:
            run(catalog_feeds.create_product_feed_upload(feed_id="123", file_path=path))
        assert "private-export" not in str(error.value)
        assert "secret-row" not in str(error.value)


def test_feed_file_upload_is_off_without_an_upload_folder(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("META_UPLOAD_DIR", raising=False)
    reload_settings()
    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    target = tmp_path / "feed.csv"
    target.write_text("id\n1\n")
    with pytest.raises(ValidationError, match="META_UPLOAD_DIR"):
        run(catalog_feeds.create_product_feed_upload(feed_id="123", file_path=str(target)))


def test_feed_file_upload_from_the_upload_folder_is_sent(monkeypatch, tmp_path) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    (tmp_path / "feed.csv").write_bytes(b"id\n1\n")
    result = run(catalog_feeds.create_product_feed_upload(feed_id="123", file_path="feed.csv"))
    sent = client.calls[-1]
    assert sent["edge"] == "uploads"
    assert sent["files"]["file"][0] == "feed.csv"
    assert sent["files"]["file"][1] == b"id\n1\n"
    assert result["ok"] is True


# round three: G6, a masked error must not keep the password further down its chain ---------------


G6_SECRET = "synthetic-url-password-6061"
G6_URL = f"https://user:{G6_SECRET}@example.invalid/feed.csv"


def _error_chain(error: BaseException) -> list[BaseException]:
    """Every error reachable through __cause__ and __context__, each once."""
    pending: list[BaseException | None] = [error]
    seen: list[BaseException] = []
    while pending:
        current = pending.pop()
        if current is None or any(current is known for known in seen):
            continue
        seen.append(current)
        pending.extend([current.__cause__, current.__context__])
    return seen


def _assert_chain_clean(error: BaseException, secret: str) -> None:
    import traceback

    chain = _error_chain(error)
    for link in chain:
        assert secret not in str(link), repr(link)
        assert secret not in repr(link), repr(link)
        assert secret not in repr(link.args), repr(link)
        if hasattr(link, "to_public_dict"):
            assert secret not in repr(link.to_public_dict()), link.to_public_dict()
    assert secret not in "".join(traceback.format_exception(error))


class _ChainedErrorClient(FakeCatalogFeedClient):
    """Raises the way GraphAPIClient does: a rate-limit or unsupported error from a Meta error."""

    def __init__(self, kind: str = "rate_limit", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.kind = kind

    def _fail(self) -> None:
        from meta_ads_mcp.errors import RateLimitError, UnsupportedFeatureError

        try:
            raise MetaApiError(
                message=f"Fetch {G6_URL} failed",
                code=4,
                user_message=f"Check {G6_URL}",
                error_data={"url": G6_URL},
                details={"error": {"message": f"Fetch {G6_URL} failed"}},
            )
        except MetaApiError as original:
            if self.kind == "rate_limit":
                raise RateLimitError(f"Rate limit fetching {G6_URL}", code=4, operation="GET feed") from original
            if self.kind == "unsupported":
                raise UnsupportedFeatureError(original.message) from original
            # An error raised while handling another, with no explicit cause.
            raise RuntimeError(f"wrapper around {G6_URL}")

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append({"method": "get_object", "object_id": object_id})
        self._fail()


def test_g6_feed_error_chain_never_contains_url_password(monkeypatch) -> None:
    from meta_ads_mcp.errors import RateLimitError

    _use(monkeypatch, _ChainedErrorClient())
    with pytest.raises(RateLimitError) as info:
        run(catalog_feeds.list_product_feeds(feed_id="10"))
    assert len(_error_chain(info.value)) == 2
    _assert_chain_clean(info.value, G6_SECRET)
    # The host and path stay readable, so the failing source can still be found.
    assert "example.invalid/feed.csv" in str(info.value.__cause__)


@pytest.mark.parametrize("kind", ["unsupported", "context"])
def test_g6_every_kind_of_chained_error_is_masked(monkeypatch, kind) -> None:
    _use(monkeypatch, _ChainedErrorClient(kind))
    with pytest.raises(Exception) as info:
        run(catalog_feeds.update_product_feed(feed_id="10", name="New"))
    assert len(_error_chain(info.value)) == 2
    _assert_chain_clean(info.value, G6_SECRET)


def test_g6_error_chain_with_a_cycle_is_masked_and_ends(monkeypatch) -> None:
    class CyclingClient(FakeCatalogFeedClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            first = MetaApiError(message=f"first {G6_URL}")
            second = RuntimeError(f"second {G6_URL}")
            first.__context__ = second
            second.__context__ = first
            raise first

    _use(monkeypatch, CyclingClient())
    with pytest.raises(MetaApiError) as info:
        run(catalog_feeds.list_product_feeds(feed_id="10"))
    _assert_chain_clean(info.value, G6_SECRET)


def test_g6_chained_error_password_reaches_no_mcp_error_or_log(monkeypatch, caplog) -> None:
    import logging

    from meta_ads_mcp.coordinator import mcp_server

    caplog.set_level(logging.DEBUG)
    _use(monkeypatch, _ChainedErrorClient())
    with pytest.raises(ToolError) as info:
        run(mcp_server.call_tool("list_product_feeds", {"feed_id": "10"}))
    _assert_chain_clean(info.value, G6_SECRET)
    assert G6_SECRET not in caplog.text
    for record in caplog.records:
        if record.exc_info:
            _assert_chain_clean(record.exc_info[1], G6_SECRET)


# round three: G15, a page Meta keeps returning is read once and the read says it stalled -----------


def _stats_reads(client: FakeCatalogFeedClient) -> list[dict[str, Any]]:
    return [call for call in client.calls if call["method"] == "list_objects" and call["edge"] == "event_stats"]


def test_g15_feed_stats_repeated_page_is_not_counted_twice(monkeypatch) -> None:
    row = {
        "event_source": "20", "date_start": "2026-11-01",
        "total_matched_content_ids": 7, "total_unmatched_content_ids": 3,
    }
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("10", "external_event_sources"): {"data": [{"id": "20", "source_type": "PIXEL"}]},
        ("10", "event_stats"): _stalled([row]),
    }))
    result = run(catalog_feeds.get_catalog_event_source_health("10"))
    source = result["sources"][0]
    assert source["matched_content_ids"] == 7, source
    assert source["unmatched_content_ids"] == 3, source
    assert len(_stats_reads(client)) <= 2
    # The counts come from a stalled read: they are flagged, and no rate or finding rests on them.
    assert result["metrics"]["stats_complete"] is False
    assert result["metrics"]["event_stat_rows"] == 1
    assert source["counts_complete"] is False
    assert source["window_match_rate"] is None
    assert "low_match_rate" not in [finding["type"] for finding in result["findings"]]
    assert any("same page" in signal for signal in result["missing_signals"]), result["missing_signals"]


def test_g15_repeated_cursor_stops_the_read_after_counting_each_new_page_once(monkeypatch) -> None:
    def pages(params):
        after = params.get("after")
        day = {None: "2026-11-01", "a": "2026-11-02"}.get(after, "2026-11-09")
        # The second page hands back the cursor that led to it.
        return {
            "data": [_stat(day, "px_1", "ViewContent", 4, 1)],
            "paging": {"next": "https://next", "cursors": {"after": "a"}},
        }

    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "OFFLINE"}]},
        ("cat_1", "event_stats"): pages,
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert len(_stats_reads(client)) == 2
    source = result["sources"][0]
    assert source["matched_content_ids"] == 8
    assert [day["date"] for day in source["match_rate_history"]] == ["2026-11-01", "2026-11-02"]
    assert source["counts_complete"] is False
    assert any("cursor" in signal and "already" in signal for signal in result["missing_signals"])


def test_g15_complete_stats_read_is_marked_complete(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "OFFLINE"}]},
        ("cat_1", "event_stats"): {"data": [_stat("2026-11-01", "px_1", "ViewContent", 9, 1)]},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    source = result["sources"][0]
    assert source["counts_complete"] is True
    assert source["window_match_rate"] == 0.9


def test_g15_recommendations_never_total_a_stalled_stats_read(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1", "source_type": "PIXEL"}]},
        ("cat_1", "event_stats"): _stalled([_stat("2026-11-01", "px_1", "ViewContent", 7, 3)]),
        ("biz_1", "adspixels"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_recommendations(catalog_id="cat_1", business_id="biz_1"))
    metrics = result["metrics"]
    assert len(_stats_reads(client)) <= 2
    assert metrics["matched_content_ids_28d"] is None
    assert metrics["match_rate_28d"] is None
    assert metrics["partial_counts_from_pages_read"] == {
        "event_stat_rows": 1, "matched_content_ids": 7, "unmatched_content_ids": 3,
    }
    assert result["findings"] == []


def test_g15_match_rate_per_catalog_from_a_stalled_read_is_partial(monkeypatch) -> None:
    edges = _scan_edges() | {("cat_1", "event_stats"): _stalled([_stat("2026-11-01", "px_1", "ViewContent", 7, 3)])}
    client = _use(monkeypatch, FakeCatalogFeedClient(edges=edges))
    result = run(catalog_feeds.list_event_source_catalogs(
        event_source_id="px_1", business_id="biz_1", include_match_rates=True,
    ))
    first = result["items"][0]["match_rate"]
    assert first["status"] == "partial_read"
    assert first["partial_counts_from_pages_read"] == {"matched_content_ids": 7, "unmatched_content_ids": 3}
    assert "same page" in first["note"]
    assert len([call for call in _stats_reads(client) if call["parent_id"] == "cat_1"]) <= 2


def test_g15_repeated_catalog_page_is_scanned_once(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("biz_1", "owned_product_catalogs"): _stalled([{"id": "cat_1", "name": "A"}]),
        ("cat_1", "external_event_sources"): {"data": [{"id": "px_1"}]},
    }))
    result = run(catalog_feeds.list_event_source_catalogs(event_source_id="px_1", business_id="biz_1"))
    assert result["summary"]["catalogs_scanned"] == 1
    assert result["summary"]["count"] == 1
    assert [item["id"] for item in result["items"]] == ["cat_1"]
    assert result["summary"]["complete"] is False
    assert any("same page" in reason for reason in result["summary"]["stop_reasons"])
    assert len([call for call in client.calls if call.get("edge") == "external_event_sources"]) == 1


def test_g15_repeated_source_page_does_not_duplicate_connected_sources(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): _stalled([{"id": "px_1"}]),
    }))
    result = run(catalog_feeds.connect_catalog_event_source(catalog_id="cat_1", event_source_id="px_2"))
    assert result["previous"]["connected_event_source_ids"] == ["px_1"]
    assert result["current"]["connected_event_source_ids"] == ["px_1", "px_2"]
    assert result["previous_complete"] is False
    assert any("same page" in gap for gap in result["missing_signals"])
    assert len([call for call in client.calls if call.get("edge") == "external_event_sources" and call["method"] == "list_objects"]) <= 2


def test_g15_repeated_empty_pages_stop_on_the_repeated_cursor(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("cat_1", "external_event_sources"): _stalled([]),
        ("cat_1", "event_stats"): {"data": []},
    }))
    result = run(catalog_feeds.get_catalog_event_source_health(catalog_id="cat_1"))
    assert result["metrics"]["sources_complete"] is False
    assert result["findings"] == []
    assert len([call for call in client.calls if call.get("edge") == "external_event_sources"]) == 2
    assert any("already" in signal for signal in result["missing_signals"])


@pytest.mark.parametrize(("tool", "arguments", "parent", "edge"), [
    ("list_product_feeds", {"catalog_id": "cat_1"}, "cat_1", "product_feeds"),
    ("list_product_feed_uploads", {"feed_id": "feed_1"}, "feed_1", "uploads"),
    ("list_feed_rules", {"feed_id": "feed_1"}, "feed_1", "rules"),
    ("list_catalog_event_sources", {"catalog_id": "cat_1"}, "cat_1", "external_event_sources"),
])
def test_g15_list_page_that_hands_back_its_own_cursor_offers_no_next_cursor(monkeypatch, tool, arguments, parent, edge) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={(parent, edge): _stalled([{"id": "1"}], cursor="c1")}))
    result = run(getattr(catalog_feeds, tool)(**arguments, after="c1"))
    assert result["summary"]["complete"] is False
    assert result["summary"]["next_after"] is None
    assert "repeat this page" in result["summary"]["stop_reason"]
    # A page with a new cursor still offers it.
    fresh = run(getattr(catalog_feeds, tool)(**arguments, after="c0"))
    assert fresh["summary"]["next_after"] == "c1"
    assert "stop_reason" not in fresh["summary"]


# round three: named feed type and parsing options -----------------------------------------------


def test_create_product_feed_sends_named_feed_type_and_parsing_options(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed(
        catalog_id="cat_1", name="Hotels", feed_type="hotel", delimiter=",", encoding="utf-8",
        quoted_fields_mode="on", deletion_enabled=False,
    ))
    assert client.calls[0]["data"] == {
        "name": "Hotels",
        "feed_type": "HOTEL",
        "delimiter": "COMMA",
        "encoding": "UTF8",
        "quoted_fields_mode": "ON",
        "deletion_enabled": False,
    }


@pytest.mark.parametrize(("given", "sent"), [
    ("|", "BAR"), (",", "COMMA"), ("\t", "TAB"), ("~", "TILDE"), (";", "SEMICOLON"),
    ("autodetect", "AUTODETECT"), ("semicolon", "SEMICOLON"), ("Tab", "TAB"),
])
def test_feed_delimiter_accepts_meta_names_and_characters(monkeypatch, given, sent) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Feed", delimiter=given))
    assert client.calls[0]["data"]["delimiter"] == sent


@pytest.mark.parametrize(("given", "sent"), [
    ("UTF-8", "UTF8"), ("utf8", "UTF8"), ("ISO-8859-1", "LATIN1"), ("latin1", "LATIN1"),
    ("UTF-16LE", "UTF16LE"), ("UTF-16BE", "UTF16BE"), ("UTF-32LE", "UTF32LE"), ("UTF-32BE", "UTF32BE"),
    ("AUTODETECT", "AUTODETECT"),
])
def test_feed_encoding_accepts_meta_names_and_common_spellings(monkeypatch, given, sent) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Feed", encoding=given))
    assert client.calls[0]["data"]["encoding"] == sent


def test_create_product_feed_sends_a_supplementary_feed_with_its_override(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.create_product_feed(
        catalog_id="cat_1", name="French", ingestion_source_type="SUPPLEMENTARY_FEED",
        primary_feed_ids="111, 222", override_type="language", override_value="fr_XX",
    ))
    assert client.calls[0]["data"] == {
        "name": "French",
        "ingestion_source_type": "supplementary_feed",
        "primary_feed_ids": ["111", "222"],
        "override_type": "LANGUAGE",
        "override_value": "fr_XX",
    }


@pytest.mark.parametrize("kwargs", [
    {"feed_type": "BOATS"},
    {"feed_type": ""},
    {"delimiter": ":"},
    {"encoding": "UTF-7"},
    {"quoted_fields_mode": "maybe"},
    {"ingestion_source_type": "secondary"},
    {"ingestion_source_type": "supplementary_feed", "override_type": "REGION"},
    {"primary_feed_ids": ["111"]},
    {"override_type": "LANGUAGE"},
    {"ingestion_source_type": "primary_feed", "override_type": "LANGUAGE"},
    {"ingestion_source_type": "supplementary_feed", "override_value": "fr_XX"},
    {"ingestion_source_type": "supplementary_feed", "primary_feed_ids": ["feed-one"]},
    {"ingestion_source_type": "supplementary_feed", "primary_feed_ids": []},
    {"delimiter": "COMMA", "params": {"delimiter": "TAB"}},
    {"feed_type": "HOTEL", "params": {"feed_type": "HOTEL"}},
])
def test_create_product_feed_rejects_invalid_named_options_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Feed", **kwargs))


def test_create_product_feed_unsupported_option_error_lists_the_documented_values(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="HOTEL") as caught:
        run(catalog_feeds.create_product_feed(catalog_id="cat_1", name="Feed", feed_type="boats-and-planes"))
    assert "boats-and-planes" not in str(caught.value)


def test_update_product_feed_sends_named_parsing_options_and_returns_previous(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"feed_1": {
        "id": "feed_1", "name": "Main", "delimiter": "AUTODETECT", "encoding": "AUTODETECT",
        "quoted_fields_mode": "AUTODETECT", "deletion_enabled": False,
    }}))
    result = run(catalog_feeds.update_product_feed(
        feed_id="feed_1", delimiter="\t", encoding="ISO-8859-1", quoted_fields_mode="off", deletion_enabled=True,
    ))
    assert client.methods() == ["get_object", "update_object"]
    assert client.calls[0]["fields"] == EXPECTED_FEED_UPDATE_READ_FIELDS
    assert client.calls[1]["data"] == {
        "delimiter": "TAB", "encoding": "LATIN1", "quoted_fields_mode": "OFF", "deletion_enabled": True,
    }
    assert result["previous"] == {
        "delimiter": "AUTODETECT", "encoding": "AUTODETECT", "quoted_fields_mode": "AUTODETECT",
        "deletion_enabled": False,
    }


def test_update_product_feed_refuses_to_turn_off_deletion_once_meta_has_it_on(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"feed_1": {"id": "feed_1", "deletion_enabled": True}}))
    with pytest.raises(ValidationError, match="cannot be turned off"):
        run(catalog_feeds.update_product_feed(feed_id="feed_1", deletion_enabled=False))
    assert client.methods() == ["get_object"]


@pytest.mark.parametrize("kwargs", [
    {"delimiter": "pipe"},
    {"encoding": "ASCII"},
    {"quoted_fields_mode": "yes"},
    {"delimiter": "TAB", "params": {"delimiter": "TAB"}},
    {"deletion_enabled": True, "params": {"deletion_enabled": True}},
])
def test_update_product_feed_rejects_invalid_named_options_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.update_product_feed(feed_id="feed_1", **kwargs))


def test_update_product_feed_says_a_schedule_cannot_be_cleared_here() -> None:
    doc = catalog_feeds.update_product_feed.__doc__ or ""
    assert "cannot be removed" in doc
    assert "delete_product_feed" in doc
    assert "+" not in doc


def test_feed_option_signatures_match_metas_feed_tools() -> None:
    """Every input of Meta's create and update feed tools that its public API documents has a named parameter."""
    create = set(inspect.signature(catalog_feeds.create_product_feed).parameters)
    update = set(inspect.signature(catalog_feeds.update_product_feed).parameters)
    assert {"catalog_id", "name", "feed_type", "country", "default_currency", "schedule"} <= create
    assert {"delimiter", "encoding", "quoted_fields_mode", "deletion_enabled", "override_type"} <= create
    assert {"name", "default_currency", "delimiter", "encoding", "quoted_fields_mode", "schedule", "update_schedule"} <= update


def test_named_feed_options_work_through_the_mcp_boundary(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(mcp_server.call_tool("create_product_feed", {
        "catalog_id": "1", "name": "French", "feed_type": "products", "delimiter": "\t",
        "ingestion_source_type": "supplementary_feed", "primary_feed_ids": "111,222",
        "override_type": "LANGUAGE", "override_value": "fr_XX",
    }))
    assert client.calls[0]["data"] == {
        "name": "French", "feed_type": "PRODUCTS", "delimiter": "TAB",
        "ingestion_source_type": "supplementary_feed", "primary_feed_ids": ["111", "222"],
        "override_type": "LANGUAGE", "override_value": "fr_XX",
    }


# list_product_feeds: supplementary_feeds, tolerated if Meta refuses it (round four, P46) ----------


def _nonexisting_field_error(field: str) -> MetaApiError:
    return MetaApiError(
        message=f"(#100) Tried accessing nonexisting field ({field}) on node type (ProductFeed)",
        status_code=400,
        code=100,
    )


class RefusingFieldClient(FakeCatalogFeedClient):
    """Refuses any read that asks for one of the given fields, as Meta does with error #100."""

    def __init__(self, refused: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.refused = refused

    def _refuse(self, fields) -> None:
        asked = {str(field).split("{", 1)[0] for field in fields or []}
        hit = sorted(asked & self.refused)
        if hit:
            raise _nonexisting_field_error(hit[0])

    async def get_object(self, object_id: str, *, fields=None, params=None):
        result = await super().get_object(object_id, fields=fields, params=params)
        self._refuse(fields)
        return result

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        result = await super().list_objects(parent_id, edge, fields=fields, params=params)
        self._refuse(fields)
        return result


def test_list_product_feeds_reads_supplementary_feeds_by_default(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"feed_1": {"id": "feed_1", "supplementary_feeds": ["feed_9"]}},
        edges={("cat_1", "product_feeds"): {"data": [{"id": "feed_1", "supplementary_feeds": ["feed_9"]}]}},
    ))
    one = run(catalog_feeds.list_product_feeds(feed_id="feed_1"))
    many = run(catalog_feeds.list_product_feeds(catalog_id="cat_1"))
    assert client.calls[0]["fields"] == [*EXPECTED_FEED_FIELDS, "supplementary_feeds"]
    assert client.calls[1]["fields"] == [*EXPECTED_FEED_LIST_FIELDS, "supplementary_feeds"]
    assert one["item"]["supplementary_feeds"] == ["feed_9"]
    assert many["items"][0]["supplementary_feeds"] == ["feed_9"]
    assert "fields_unavailable" not in one["summary"]
    assert "fields_unavailable" not in many["summary"]


def test_list_product_feeds_retries_without_a_refused_field_and_names_it(monkeypatch) -> None:
    client = _use(monkeypatch, RefusingFieldClient(
        {"supplementary_feeds"},
        objects={"feed_1": {"id": "feed_1", "name": "Main"}},
        edges={("cat_1", "product_feeds"): {"data": [{"id": "feed_1"}],
                                             "paging": {"cursors": {"after": "c2"}, "next": "https://next"}}},
    ))
    one = run(catalog_feeds.list_product_feeds(feed_id="feed_1"))
    many = run(catalog_feeds.list_product_feeds(catalog_id="cat_1", after="c1"))
    assert [call["fields"] for call in client.calls] == [
        [*EXPECTED_FEED_FIELDS, "supplementary_feeds"], EXPECTED_FEED_FIELDS,
        [*EXPECTED_FEED_LIST_FIELDS, "supplementary_feeds"], EXPECTED_FEED_LIST_FIELDS,
    ]
    assert client.calls[3]["params"] == {"limit": 25, "after": "c1"}
    assert one["item"] == {"id": "feed_1", "name": "Main"}
    for result in (one, many):
        assert list(result["summary"]["fields_unavailable"]) == ["supplementary_feeds"]
        assert "refused" in result["summary"]["fields_unavailable"]["supplementary_feeds"]
    assert many["summary"]["next_after"] == "c2"


def test_list_product_feeds_other_errors_are_not_retried(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(errors={"feed_1": _permission_error()}))
    with pytest.raises(MetaApiError, match="catalog_management"):
        run(catalog_feeds.list_product_feeds(feed_id="feed_1"))
    assert len(client.calls) == 1


def test_list_product_feeds_a_refusal_that_persists_without_the_field_is_raised(monkeypatch) -> None:
    client = _use(monkeypatch, RefusingFieldClient({"supplementary_feeds", "schedule"}))
    with pytest.raises(MetaApiError, match="schedule"):
        run(catalog_feeds.list_product_feeds(feed_id="feed_1"))
    assert len(client.calls) == 2


def test_delete_product_feed_pre_read_does_not_ask_for_supplementary_feeds(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient())
    run(catalog_feeds.delete_product_feed(feed_id="feed_1"))
    assert client.calls[0]["fields"] == EXPECTED_FEED_FIELDS


# list_product_feed_uploads: error samples and error paging (round four, P46) ----------------------

EXPECTED_SAMPLED_ERROR_FIELDS = [*EXPECTED_UPLOAD_ERROR_FIELDS, "samples.limit(1){id,row_number,retailer_id}"]


def _error_pages(pages: dict[str | None, dict[str, Any]]):
    """An errors edge whose page depends on the after cursor it is asked for."""
    def page(params):
        return pages[params.get("after")]
    return page


def _errors(start: int, count: int) -> list[dict[str, Any]]:
    return [{"id": f"e{n}", "total_count": 1000 - n, "severity": "warning"} for n in range(start, start + count)]


def test_list_product_feed_uploads_without_new_options_is_unchanged(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 2}},
        edges={("up_1", "errors"): _upload_errors},
    ))
    run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1"))
    assert client.calls[1]["fields"] == EXPECTED_UPLOAD_ERROR_FIELDS
    assert client.calls[1]["params"] == {"limit": 100}
    assert len(client.calls) == 2


def test_list_product_feed_uploads_include_samples_returns_one_short_sample_per_error(monkeypatch) -> None:
    long_id = "SKU-" + "x" * 300
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 2}},
        edges={("up_1", "errors"): {"data": [
            {"id": "e1", "total_count": 5, "samples": {"data": [{"id": "9", "row_number": 4, "retailer_id": "SKU-1"}]}},
            {"id": "e2", "total_count": 3, "samples": {"data": [{"id": "8", "row_number": 7, "retailer_id": long_id}]}},
            {"id": "e3", "total_count": 1},
        ]}},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", include_samples=True))
    assert client.calls[1]["fields"] == EXPECTED_SAMPLED_ERROR_FIELDS
    errors = {error["id"]: error for error in result["item"]["errors"]}
    assert errors["e1"]["sample"] == {"id": "9", "row_number": 4, "retailer_id": "SKU-1"}
    assert "samples" not in errors["e1"]
    long_sample = errors["e2"]["sample"]
    assert len(long_sample["retailer_id"]) == catalog_feeds.MAX_SAMPLE_CHARS
    assert long_id.startswith(long_sample["retailer_id"])
    assert long_sample["retailer_id_truncated"] is True
    assert errors["e3"]["sample"] is None
    assert "row" in result["item"]["samples_note"]


def test_list_product_feed_uploads_samples_refused_are_dropped_and_said(monkeypatch) -> None:
    refusal = MetaApiError(message="(#100) Unknown path components: /samples", status_code=400, code=100)
    calls: list[list[str]] = []

    class SampleRefusingClient(FakeCatalogFeedClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            calls.append(list(fields or []))
            if any(str(field).startswith("samples") for field in fields or []):
                self.calls.append({"method": "list_objects", "edge": edge, "fields": fields, "params": params})
                raise refusal
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    _use(monkeypatch, SampleRefusingClient(
        objects={"up_1": {"id": "up_1", "error_count": 2}},
        edges={("up_1", "errors"): _upload_errors},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", include_samples=True))
    assert calls == [EXPECTED_SAMPLED_ERROR_FIELDS, EXPECTED_UPLOAD_ERROR_FIELDS]
    item = result["item"]
    assert len(item["errors"]) == 4
    assert all("sample" not in error for error in item["errors"])
    assert "could not be read" in item["samples_unavailable"]


def test_list_product_feed_uploads_pages_errors_past_the_first_100(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 250}},
        edges={("up_1", "errors"): _error_pages({
            None: {"data": _errors(0, 100), "paging": {"cursors": {"after": "c1"}, "next": "https://n1"}},
            "c1": {"data": _errors(100, 100), "paging": {"cursors": {"after": "c2"}, "next": "https://n2"}},
            "c2": {"data": _errors(200, 50)},
        })},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", error_limit=1000))
    error_calls = [call for call in client.calls if call.get("edge") == "errors"]
    assert [call["params"] for call in error_calls] == [
        {"limit": 100}, {"limit": 100, "after": "c1"}, {"limit": 100, "after": "c2"},
    ]
    item = result["item"]
    assert len(item["errors"]) == 250
    assert item["errors_complete"] is True
    assert item["errors_next_after"] is None
    assert "errors_stop_reason" not in item


def test_list_product_feed_uploads_error_limit_stops_and_offers_a_cursor(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 250}},
        edges={("up_1", "errors"): _error_pages({
            None: {"data": _errors(0, 100), "paging": {"cursors": {"after": "c1"}, "next": "https://n1"}},
            "c1": {"data": _errors(100, 50), "paging": {"cursors": {"after": "c2"}, "next": "https://n2"}},
        })},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", error_limit=150))
    error_calls = [call for call in client.calls if call.get("edge") == "errors"]
    # The second page asks only for the 50 errors still wanted.
    assert [call["params"] for call in error_calls] == [{"limit": 100}, {"limit": 50, "after": "c1"}]
    item = result["item"]
    assert len(item["errors"]) == 150
    assert item["errors_complete"] is False
    assert item["errors_next_after"] == "c2"
    assert "error_limit" in item["errors_stop_reason"]


def test_list_product_feed_uploads_error_after_continues_from_a_cursor(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 250}},
        edges={("up_1", "errors"): _error_pages({"c2": {"data": _errors(200, 50)}})},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", error_after="c2"))
    assert client.calls[1]["params"] == {"limit": 100, "after": "c2"}
    assert result["item"]["errors_complete"] is True
    assert result["item"]["errors_from_start"] is False


def test_list_product_feed_uploads_repeated_error_cursor_stops_the_read(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 500}},
        edges={("up_1", "errors"): _error_pages({
            None: {"data": _errors(0, 100), "paging": {"cursors": {"after": "c1"}, "next": "https://n1"}},
            "c1": {"data": _errors(100, 100), "paging": {"cursors": {"after": "c1"}, "next": "https://n1"}},
        })},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", error_limit=1000))
    assert len([call for call in client.calls if call.get("edge") == "errors"]) == 2
    item = result["item"]
    assert len(item["errors"]) == 200
    assert item["errors_complete"] is False
    assert item["errors_next_after"] is None
    assert "already read" in item["errors_stop_reason"]


def test_list_product_feed_uploads_next_page_without_cursor_is_incomplete(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 500}},
        edges={("up_1", "errors"): {"data": _errors(0, 100), "paging": {"next": "https://n1"}}},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", error_limit=1000))
    item = result["item"]
    assert item["errors_complete"] is False
    assert item["errors_next_after"] is None
    assert "no cursor" in item["errors_stop_reason"]


def test_list_product_feed_uploads_same_page_again_is_not_counted_twice(monkeypatch) -> None:
    page = {"data": _errors(0, 100), "paging": {"cursors": {"after": "c1"}, "next": "https://n1"}}
    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 500}},
        edges={("up_1", "errors"): _error_pages({None: page, "c1": {**page, "paging": {
            "cursors": {"after": "c2"}, "next": "https://n2"}}})},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", upload_id="up_1", error_limit=1000))
    assert len([call for call in client.calls if call.get("edge") == "errors"]) == 2
    assert len(result["item"]["errors"]) == 100
    assert result["item"]["errors_complete"] is False


def test_list_product_feed_uploads_error_requests_are_capped(monkeypatch) -> None:
    def endless(params):
        n = int(params.get("after") or 0)
        return {"data": _errors(n * 100, 100), "paging": {"cursors": {"after": str(n + 1)}, "next": "https://n"}}

    client = _use(monkeypatch, FakeCatalogFeedClient(
        objects={"up_1": {"id": "up_1", "error_count": 99999}},
        edges={("up_1", "errors"): endless},
    ))
    result = run(catalog_feeds.list_product_feed_uploads(
        feed_id="feed_1", upload_id="up_1", error_limit=catalog_feeds.MAX_UPLOAD_ERRORS,
    ))
    error_calls = [call for call in client.calls if call.get("edge") == "errors"]
    assert len(error_calls) == catalog_feeds.MAX_UPLOAD_ERRORS // 100
    assert len(result["item"]["errors"]) == catalog_feeds.MAX_UPLOAD_ERRORS
    assert result["item"]["errors_complete"] is False
    assert result["item"]["errors_next_after"] == str(len(error_calls))


def test_list_product_feed_uploads_list_mode_offers_an_error_cursor(monkeypatch) -> None:
    _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("feed_1", "uploads"): {"data": [{"id": "up_1", "error_count": 300}]},
        ("up_1", "errors"): {"data": _errors(0, 100), "paging": {"cursors": {"after": "c1"}, "next": "https://n1"}},
    }))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1"))
    item = result["items"][0]
    assert item["errors_complete"] is False
    assert item["errors_next_after"] == "c1"
    assert "upload_id" in item["errors_stop_reason"]


@pytest.mark.parametrize("kwargs", [
    {"error_after": "c1"},
    {"error_limit": 200},
    {"upload_id": "up_1", "error_limit": 0},
    {"upload_id": "up_1", "error_limit": 1001},
    {"upload_id": "up_1", "error_limit": True},
    {"upload_id": "up_1", "include_samples": "yes"},
])
def test_list_product_feed_uploads_new_options_are_validated_before_any_call(monkeypatch, kwargs) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError):
        run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1", **kwargs))


# round five: known secrets in Meta's error text, feed error reads, caller text in messages -------


_KNOWN_SECRET = "".join(("R5-sensitive-", "credential-x9"))


class _EchoingSecretClient(FakeCatalogFeedClient):
    """Refuses every write with an error that repeats the secret, in its text and behind it."""

    def __init__(self, secret: str, *, cycle: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.secret = secret
        # Python never builds a cause/context cycle itself, and the rich log handler cannot
        # print one, so only the direct calls use it to prove the scrub ends.
        self.cycle = cycle

    def _refusal(self) -> MetaApiError:
        from urllib.parse import quote

        inner = MetaApiError(
            "Inner refusal for " + quote(self.secret, safe=""), code=100,
            error_data={"blame": self.secret, self.secret: "key"},
        )
        try:
            raise inner
        except MetaApiError:
            error = MetaApiError(
                "Rejected supplied credential " + self.secret, code=100,
                user_message="Invalid " + self.secret, details={"error": {"message": self.secret}},
            )
            error.__cause__ = inner
            if self.cycle:
                inner.__context__ = error
            return error

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        await super().create_edge_object(parent_id, edge, data=data, files=files)
        raise self._refusal()

    async def update_object(self, object_id: str, *, data):
        await super().update_object(object_id, data=data)
        raise self._refusal()


def _secret_arguments(tool: str, secret: str) -> dict[str, Any]:
    integration = {"params": {"partner_integration": {"external_access_token": secret}}}
    schedule = {"url": "https://example.invalid/feed.csv", "interval": "DAILY", "username": "user", "password": secret}
    return {
        "create_catalog": {"business_id": "10", "name": "C", **integration},
        "update_catalog": {"catalog_id": "10", **integration},
        "create_product_feed": {"catalog_id": "10", "name": "F", "schedule": schedule},
        "update_product_feed": {"feed_id": "10", "schedule": schedule},
        "create_product_feed_upload": {
            "feed_id": "10", "url": "https://example.invalid/feed.csv", "username": "user", "password": secret,
        },
    }[tool]


def _all_error_text(error: BaseException) -> str:
    texts: list[str] = []
    pending: list[BaseException | None] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        texts.append(repr(current))
        texts.append(str(current))
        if isinstance(current, MetaApiError):
            texts.append(repr(current.to_public_dict()))
            texts.append(repr(current.details))
        pending.extend([current.__cause__, current.__context__])
    return "\n".join(texts)


_SECRET_TOOLS = ["create_catalog", "update_catalog", "create_product_feed", "update_product_feed", "create_product_feed_upload"]


@pytest.mark.parametrize("tool", _SECRET_TOOLS)
@pytest.mark.parametrize("routed", [False, True])
def test_known_password_or_integration_token_is_scrubbed_from_error_text(monkeypatch, caplog, tool, routed) -> None:
    import logging

    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.tools import catalogs

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    client = _EchoingSecretClient(_KNOWN_SECRET)
    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: client)
    _use(monkeypatch, client)
    arguments = _secret_arguments(tool, _KNOWN_SECRET)
    name, payload = ("call_tool", {"name": tool, "arguments": arguments}) if routed else (tool, arguments)
    with pytest.raises(ToolError) as error:
        run(mcp_server.call_tool(name, payload))
    assert _KNOWN_SECRET not in _all_error_text(error.value)
    assert _KNOWN_SECRET not in caplog.text


@pytest.mark.parametrize("tool", _SECRET_TOOLS)
def test_known_secret_is_scrubbed_through_the_whole_error_chain(monkeypatch, tool) -> None:
    from urllib.parse import quote, quote_plus

    from meta_ads_mcp.tools import catalogs

    secret = " " + _KNOWN_SECRET + " & more/"
    client = _EchoingSecretClient(secret, cycle=True)
    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: client)
    _use(monkeypatch, client)
    module = catalogs if tool in {"create_catalog", "update_catalog"} else catalog_feeds
    with pytest.raises(MetaApiError) as error:
        run(getattr(module, tool)(**_secret_arguments(tool, secret)))
    text = _all_error_text(error.value)
    for form in (secret, secret.strip(), quote(secret, safe=""), quote_plus(secret), quote(secret.strip(), safe="")):
        assert form not in text
    assert "[redacted]" in error.value.message


def test_short_secret_is_removed_only_as_a_whole_token() -> None:
    error = MetaApiError("Bad password abc for the feed; abcdef is fine; (abc) too", code=100)
    scrub_exception(error, ["abc"], policy=ERROR)
    assert error.message == "Bad password [redacted] for the feed; abcdef is fine; ([redacted]) too"


def test_long_secret_is_removed_wherever_it_occurs() -> None:
    error = MetaApiError("prefix" + _KNOWN_SECRET + "suffix", code=100)
    scrub_exception(error, [_KNOWN_SECRET], policy=ERROR)
    assert error.message == "prefix[redacted]suffix"


def test_secret_forms_cover_encoded_and_escaped_repeats() -> None:
    from meta_ads_mcp.tools import catalogs

    secret = ' pa"ss word/1 '
    found = catalog_feeds._caller_secrets({"schedule": {"password": secret}})
    assert found == [secret.strip()]
    for form in (secret, secret.strip(), "pa%22ss%20word%2F1", "pa%22ss+word%2F1", 'pa\\"ss word/1'):
        error = MetaApiError("echo " + form + " end", code=100)
        scrub_exception(error, found, policy=ERROR)
        assert form.strip() not in error.message and "[redacted]" in error.message
    assert catalog_feeds._caller_secrets(
        {"params": {"partner_integration": {"api_key": "k" * 9}}}, catalogs._CATALOG_CREDENTIAL_PATHS,
    ) == ["k" * 9]
    assert catalog_feeds._caller_secrets({"name": "plain", "params": {"note": "text"}}) == []
    url_password = catalog_feeds._caller_secrets({"params": {"x": "see https://u:" + _KNOWN_SECRET + "@h.invalid/"}})
    assert _KNOWN_SECRET in url_password


def test_known_secret_echoed_in_a_successful_result_is_removed(monkeypatch) -> None:
    class Echo(FakeCatalogFeedClient):
        async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
            await super().create_edge_object(parent_id, edge, data=data, files=files)
            return {"id": "u1", "note": "fetched with " + _KNOWN_SECRET}

    _use(monkeypatch, Echo())
    result = run(catalog_feeds.create_product_feed_upload(
        feed_id="10", url="https://example.invalid/feed.csv", username="user", password=_KNOWN_SECRET,
    ))
    assert _KNOWN_SECRET not in repr(result)


def test_feed_params_with_a_login_url_key_or_value_are_refused(monkeypatch) -> None:
    _no_client(monkeypatch)
    login = "https://user:" + _KNOWN_SECRET + "@example.invalid/x"
    for params in ({"custom": {login: 1}}, {"custom": ["see " + login]}):
        with pytest.raises(ValidationError, match="username or password") as error:
            run(catalog_feeds.create_product_feed(catalog_id="1", name="F", params=params))
        assert _KNOWN_SECRET not in str(error.value)
        with pytest.raises(ValidationError, match="username or password"):
            run(catalog_feeds.update_product_feed(feed_id="1", params=params))


def test_feed_masking_covers_passwords_in_mapping_keys(monkeypatch) -> None:
    login = "https://user:" + _KNOWN_SECRET + "@example.invalid/x"
    _use(monkeypatch, FakeCatalogFeedClient(objects={"f1": {"id": "f1", "schedule": {login: "x"}}}))
    result = run(catalog_feeds.list_product_feeds(feed_id="f1"))
    assert _KNOWN_SECRET not in repr(result)


def test_schedule_unknown_keys_are_counted_not_named(monkeypatch) -> None:
    _no_client(monkeypatch)
    private = "".join(("private_person", "_r5"))
    schedule = {**SCHEDULE, private: 1, "a@b.invalid": 2}
    with pytest.raises(ValidationError) as error:
        run(catalog_feeds.create_product_feed(catalog_id="1", name="F", schedule=schedule))
    message = str(error.value)
    assert private not in message and "a@b.invalid" not in message
    assert "2 unsupported keys" in message and "interval" in message


def test_invalid_regex_error_does_not_quote_the_pattern(monkeypatch) -> None:
    _no_client(monkeypatch)
    private = "".join(("privateperson", "r5"))
    with pytest.raises(ValidationError, match="character 4") as error:
        run(catalog_feeds.create_feed_rule(
            feed_id="1", attribute="title", rule_type="regex_replace_rule",
            params={"regex": "(?P<" + private + "!>x)", "replace": "y"},
        ))
    assert private not in str(error.value)
    assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize("mode", ["later_page_sample_refusal", "no_upload_identity"])
def test_feed_error_read_never_loses_available_errors_or_claims_unknown_complete(monkeypatch, mode) -> None:
    class Feed(FakeCatalogFeedClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            return {"id": "20", "error_count": 2} if mode == "later_page_sample_refusal" else {"error_count": 2}

        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.calls.append({"method": "list_objects", "parent_id": parent_id, "fields": fields, "params": params})
            if params.get("after") and any(field.startswith("samples") for field in fields):
                raise MetaApiError("Samples unavailable for this page", code=100)
            if params.get("after"):
                return {"data": [{"id": "e2", "total_count": 1}]}
            return {
                "data": [{"id": "e1", "total_count": 1, "samples": {"data": [{"id": "s1", "row_number": 4}]}}],
                "paging": {"next": "more", "cursors": {"after": "a"}},
            }

    client = _use(monkeypatch, Feed())
    result = run(catalog_feeds.list_product_feed_uploads("10", upload_id="20", include_samples=True, error_limit=200))
    item = result["item"]
    if mode == "later_page_sample_refusal":
        assert [row["id"] for row in item["errors"]] == ["e1", "e2"]
        assert item["errors_complete"] is True
        assert item["samples_unavailable"]
        by_id = {row["id"]: row for row in item["errors"]}
        assert by_id["e1"]["sample"] == {"id": "s1", "row_number": 4}
        assert by_id["e2"].get("sample_unavailable") is True and "sample" not in by_id["e2"]
        assert item["samples_unavailable_after_errors"] == 1
    else:
        # Meta's row has no id, so the requested upload id is used to read the errors.
        assert [row["id"] for row in item["errors"]] == ["e1", "e2"]
        assert {call.get("parent_id") for call in client.calls if call.get("method") == "list_objects"} == {"20"}


def test_upload_without_id_in_a_list_is_not_reported_complete(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(edges={
        ("feed_1", "uploads"): {"data": [{"error_count": 2}, {"error_count": 0, "warning_count": 0}]},
    }))
    result = run(catalog_feeds.list_product_feed_uploads(feed_id="feed_1"))
    first, second = result["items"]
    assert first["errors"] == [] and first["errors_complete"] is False
    assert "id" in first["errors_stop_reason"]
    assert second["errors_complete"] is True
    assert [call for call in client.calls if call.get("edge") == "errors"] == []


def test_later_page_refusal_without_samples_still_raises(monkeypatch) -> None:
    def pages(params):
        if params.get("after"):
            raise MetaApiError("Gone", code=190)
        return {"data": [{"id": "e1"}], "paging": {"next": "more", "cursors": {"after": "a"}}}

    _use(monkeypatch, FakeCatalogFeedClient(
        objects={"20": {"id": "20", "error_count": 2}}, edges={("20", "errors"): pages},
    ))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.list_product_feed_uploads("10", upload_id="20", include_samples=True, error_limit=200))


# round six: encoded secrets in errors, short passwords in results, data maps -----------------


_R6_SECRET = "".join(("R5+credential", "/part=9"))
# Every kind of character an encoder treats differently: space, slash, plus, quote, backslash,
# HTML-sensitive characters, characters some encoders leave as they are, a non-ASCII letter,
# and a character outside the Basic Multilingual Plane.
_R6_ENCODED_SECRET = "".join(("R6 pass/w", "örd+\"x\\<&>~!*'()=", "\U0001F511"))


def _lowercase_escapes(text: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), text)


def _encoded_forms(secret: str) -> dict[str, str]:
    """The ways an upstream error or result can plausibly write a supplied secret."""
    escaped = json.dumps(secret)[1:-1]
    return {
        "url_encoded": quote(secret, safe=""),
        "url_encoded_lowercase": _lowercase_escapes(quote(secret, safe="")),
        "slash_preserved": quote(secret),
        "slash_preserved_lowercase": _lowercase_escapes(quote(secret)),
        "component_safe_set": quote(secret, safe="/~!*'()"),
        "form_encoded": quote_plus(secret),
        "form_encoded_lowercase": _lowercase_escapes(quote_plus(secret)),
        "double_encoded": quote(quote(secret, safe=""), safe=""),
        "double_encoded_lowercase": _lowercase_escapes(quote(quote(secret, safe=""), safe="")),
        "form_then_url_encoded": quote(quote_plus(secret), safe=""),
        "json": escaped,
        "json_unicode": json.dumps(secret, ensure_ascii=False)[1:-1],
        "json_uppercase_unicode_escapes": re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m[1].upper(), escaped),
        "json_escaped_slash": escaped.replace("/", "\\/"),
        "json_html_safe": escaped.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
        "json_of_url_encoded": json.dumps(quote(secret))[1:-1].replace("/", "\\/"),
    }


@pytest.mark.parametrize("tool", ["create_product_feed_upload", "create_catalog"])
@pytest.mark.parametrize("encoding", ["slash_preserved", "lowercase_escapes"])
def test_known_credentials_are_removed_from_encoded_errors(monkeypatch, caplog, tool, encoding) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.tools import catalogs

    secret = _R6_SECRET
    encoded = quote(secret) if encoding == "slash_preserved" else _lowercase_escapes(quote(secret, safe=""))

    class Echo(FakeCatalogFeedClient):
        async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
            raise MetaApiError("Rejected credential " + encoded, code=100, user_message="Invalid " + encoded)

    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    client = Echo()
    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: client)
    _use(monkeypatch, client)
    arguments = {
        "create_product_feed_upload": {
            "feed_id": "10", "url": "https://example.invalid/feed.csv", "username": "feed-user", "password": secret,
        },
        "create_catalog": {
            "business_id": "10", "name": "C", "params": {"partner_integration": {"external_access_token": secret}},
        },
    }[tool]
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ToolError) as error:
        run(mcp_server.call_tool(tool, arguments))
    assert encoded not in str(error.value)
    assert encoded not in _all_error_text(error.value)
    assert encoded not in caplog.text


@pytest.mark.parametrize("form", list(_encoded_forms(_R6_ENCODED_SECRET)))
def test_known_secret_is_removed_from_an_error_in_every_encoding(form) -> None:
    text = _encoded_forms(_R6_ENCODED_SECRET)[form]
    error = MetaApiError("before " + text + " after", code=100, user_message="see " + text)
    scrub_exception(error, [_R6_ENCODED_SECRET], policy=ERROR)
    assert error.message == "before [redacted] after"
    assert error.user_message == "see [redacted]"


@pytest.mark.parametrize("form", list(_encoded_forms(_R6_ENCODED_SECRET)))
def test_known_secret_is_removed_from_a_successful_result_in_every_encoding(monkeypatch, form) -> None:
    text = _encoded_forms(_R6_ENCODED_SECRET)[form]
    result, _ = _upload_answering(monkeypatch, _R6_ENCODED_SECRET, {"id": "20", "message": "fetched " + text})
    assert result["created"]["message"] == "fetched [redacted]"


def test_letters_of_a_secret_stay_case_sensitive() -> None:
    # Only the hex digits of an escape match in either case; the secret's own letters do not.
    other = _R6_SECRET.upper()
    for text in (other, quote(other, safe=""), _lowercase_escapes(quote(other, safe=""))):
        error = MetaApiError("echo " + text, code=100)
        scrub_exception(error, [_R6_SECRET], policy=ERROR)
        assert error.message == "echo " + text


class _AnsweringUploadClient(FakeCatalogFeedClient):
    def __init__(self, answer: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.answer = answer

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        await super().create_edge_object(parent_id, edge, data=data, files=files)
        return dict(self.answer)


def _upload_answering(monkeypatch, password: str, answer: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    client = _use(monkeypatch, _AnsweringUploadClient(answer))
    result = run(catalog_feeds.create_product_feed_upload(
        feed_id="10", url="https://example.invalid/feed.csv", username="feed-user", password=password,
    ))
    return result, client


def test_realistic_short_password_is_removed_from_successful_upstream_text(monkeypatch) -> None:
    secret = "".join(("S3", "cur3!"))
    result, _ = _upload_answering(monkeypatch, secret, {"id": "20", "message": "Fetched with credential " + secret})
    assert secret not in json.dumps(result)
    assert result["created"]["message"] == "Fetched with credential [redacted]"


_SEVEN = "".join(("S3", "cur3!"))


@pytest.mark.parametrize(("password", "message", "shown"), [
    # Seven characters inside a sentence, beside punctuation, and between underscores.
    (_SEVEN, "Login " + _SEVEN + " failed, retried (" + _SEVEN + ").",
     "Login [redacted] failed, retried ([redacted])."),
    (_SEVEN, "id_" + _SEVEN + "_x", "id_[redacted]_x"),
    # The same seven characters inside a longer word or id are left alone.
    (_SEVEN, "item x" + _SEVEN + " and " + _SEVEN + "9 and abc" + _SEVEN + "def",
     "item x" + _SEVEN + " and " + _SEVEN + "9 and abc" + _SEVEN + "def"),
    # Four characters: the floor of the whole-token band.
    ("".join(("ab", "cd")), "user abcd logged in; abcdef kept", "user [redacted] logged in; abcdef kept"),
    # Eight characters or more: removed wherever they occur.
    ("".join(("pass", "word8")), "xxpassword8yy", "xx[redacted]yy"),
])
def test_successful_result_scrubs_a_supplied_password_by_its_length(monkeypatch, password, message, shown) -> None:
    result, _ = _upload_answering(monkeypatch, password, {"id": "20", "message": message})
    assert result["created"]["message"] == shown


def test_three_character_password_leaves_ordinary_words_in_a_result(monkeypatch) -> None:
    message = "Fetched feed.csv as csv; csv rows read"
    result, client = _upload_answering(monkeypatch, "csv", {"id": "20", "message": message})
    assert result["created"]["message"] == message
    assert client.calls[0]["data"]["password"] == "csv"


@pytest.mark.parametrize("password", ["csv", "".join(("S3", "cur3!")), "".join(("long", "password-9"))])
def test_value_under_a_password_key_is_masked_whatever_its_length(monkeypatch, password) -> None:
    answer = {"id": "20", "request": {"username": "feed-user", "password": password}, "schedule": {"password": "x"}}
    result, _ = _upload_answering(monkeypatch, password, answer)
    assert result["created"]["request"] == {"username": "feed-user", "password": "[redacted]"}
    assert result["created"]["schedule"] == {"password": "[redacted]"}


@pytest.mark.parametrize("password", ["".join(("x", "9")), "csv"])
def test_short_password_is_removed_from_errors_whatever_its_length(monkeypatch, caplog, password) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    class Refuse(FakeCatalogFeedClient):
        async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
            raise MetaApiError("Bad login " + password + " for the feed", code=100)

    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    _use(monkeypatch, Refuse())
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ToolError) as error:
        run(mcp_server.call_tool("create_product_feed_upload", {
            "feed_id": "10", "url": "https://example.invalid/feed.csv", "username": "feed-user", "password": password,
        }))
    assert "Bad login [redacted] for the feed" in _all_error_text(error.value)
    assert "login " + password + " " not in _all_error_text(error.value)
    assert "login " + password + " " not in caplog.text


def test_value_mapping_product_text_is_not_mistaken_for_a_credential(monkeypatch) -> None:
    client = _use(monkeypatch, FakeCatalogFeedClient(objects={"20": {
        "id": "20", "attribute": "brand", "rule_type": "value_mapping_rule", "params": {"secret": "Old Brand"},
    }}))
    new_params = {"secret": "Secret Escapes"}
    result = run(catalog_feeds.update_feed_rule("20", new_params, dry_run=True))
    assert result["current"]["params"] == new_params
    assert result["would_send"]["data"]["params"] == new_params
    assert client.methods() == ["get_object"]


_PRODUCT_TEXT_MAPPING = {"password": "Kennwort", "Secret": "Secret Escapes", "token": "Jeton", "api_key": "Key"}


def test_feed_rule_value_mapping_is_shown_and_sent_as_written(monkeypatch) -> None:
    rule = {"id": "20", "attribute": "brand", "rule_type": "value_mapping_rule", "params": {"password": "Old"}}
    dry = _use(monkeypatch, FakeCatalogFeedClient(objects={"20": dict(rule)}))
    planned = run(catalog_feeds.update_feed_rule("20", dict(_PRODUCT_TEXT_MAPPING), dry_run=True))
    assert dry.methods() == ["get_object"]
    real = _use(monkeypatch, FakeCatalogFeedClient(objects={"20": dict(rule)}))
    sent = run(catalog_feeds.update_feed_rule("20", dict(_PRODUCT_TEXT_MAPPING)))
    update = next(call for call in real.calls if call["method"] == "update_object")
    # The dry run shows exactly the request a real run sends.
    assert planned["would_send"]["data"] == update["data"] == {"params": _PRODUCT_TEXT_MAPPING}
    assert planned["current"]["params"] == sent["current"]["params"] == _PRODUCT_TEXT_MAPPING
    assert planned["previous"]["params"] == sent["previous"]["params"] == {"password": "Old"}


def test_listed_and_deleted_feed_rules_show_their_value_mapping_as_read(monkeypatch) -> None:
    rule = {"id": "20", "attribute": "brand", "rule_type": "value_mapping_rule", "params": dict(_PRODUCT_TEXT_MAPPING)}
    _use(monkeypatch, FakeCatalogFeedClient(objects={"20": dict(rule)}, edges={("10", "rules"): {"data": [rule]}}))
    listed = run(catalog_feeds.list_feed_rules("10"))
    assert listed["items"][0]["params"] == _PRODUCT_TEXT_MAPPING
    deleted = run(catalog_feeds.delete_feed_rule("20"))
    assert deleted["previous"]["params"] == _PRODUCT_TEXT_MAPPING


def test_feed_rule_product_text_is_kept_in_an_error(monkeypatch) -> None:
    class Refuse(FakeCatalogFeedClient):
        async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
            raise MetaApiError("Duplicate mapping for Secret Escapes", code=100)

    _use(monkeypatch, Refuse())
    with pytest.raises(MetaApiError) as error:
        run(catalog_feeds.create_feed_rule("10", "brand", "value_mapping_rule", {"secret": "Secret Escapes"}))
    assert error.value.message == "Duplicate mapping for Secret Escapes"


def test_url_password_inside_a_data_map_is_still_a_secret(monkeypatch) -> None:
    secret = "".join(("url-pass", "word-r6"))
    params = {"user_default_value": "https://u:" + secret + "@h.invalid/default.png"}

    class Refuse(FakeCatalogFeedClient):
        async def update_object(self, object_id: str, *, data):
            raise MetaApiError("Rejected value " + quote(secret), code=100)

    _use(monkeypatch, Refuse(objects={"20": {"id": "20", "rule_type": "fallback_rule", "params": {}}}))
    with pytest.raises(MetaApiError) as error:
        run(catalog_feeds.update_feed_rule("20", params))
    assert error.value.message == "Rejected value [redacted]"


def test_secrets_are_collected_only_where_keys_are_field_names() -> None:
    from meta_ads_mcp.tools import catalogs

    feed_arguments = {
        "password": "upload-pass-1",
        "schedule": {"url": "https://h.invalid/f.csv", "password": "schedule-pass-2", "username": "u"},
        "params": {
            "update_schedule": {"password": "params-schedule-pass-3"},
            "custom": {"token": "Gold tier", "nested": {"secret": "Secret Escapes"}},
        },
    }
    assert set(catalog_feeds._caller_secrets(feed_arguments)) == {
        "upload-pass-1", "schedule-pass-2", "params-schedule-pass-3",
    }
    # A feed rule's params is a value mapping: its keys are product values.
    assert catalog_feeds._caller_secrets(
        {"params": {"secret": "Secret Escapes", "password": "Kennwort"}}, catalog_feeds._ARGUMENTS_ONLY,
    ) == []
    catalog_arguments = {
        "params": {
            "partner_integration": {"external_access_token": "integration-token-4"},
            "custom_data": {"password": "Yes", "access_token": "Gold"},
            "store_catalog_settings": {"token": "x"},
        },
        "filter": {"secret": {"eq": "Secret Escapes"}},
        "conditions": [{"field": "password", "operator": "eq", "value": "x"}],
        "items": [{"method": "update", "retailer_id": "sku", "fields": {"token": "Gold"}}],
    }
    assert catalog_feeds._caller_secrets(catalog_arguments, catalogs._CATALOG_CREDENTIAL_PATHS) == [
        "integration-token-4",
    ]
    # A password inside a URL is a secret wherever it is, in a key or a value.
    url_password = "".join(("url-pass", "word-5"))
    login = "https://u:" + url_password + "@h.invalid/x"
    for arguments in ({"params": {"custom_data": {"k": login}}}, {"filter": {login: 1}}, {"items": [["see " + login]]}):
        assert catalog_feeds._caller_secrets(arguments, catalogs._CATALOG_CREDENTIAL_PATHS) == [url_password]


# round seven -----------------------------------------------------------------------------


@pytest.mark.parametrize("surroundings", ["url", "message", "json_newlines"])
def test_l6_short_secret_tokens_survive_encoded_surroundings_privately(monkeypatch, caplog, surroundings) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    secret = "".join(("Cr3d", "!9"))
    if surroundings == "url":
        echoed = quote("https://feed-user:" + secret + "@example.invalid/f.csv", safe="")
    elif surroundings == "message":
        echoed = quote("Invalid credential " + secret + " on import", safe="")
    else:
        echoed = json.dumps("Invalid credential\n" + secret + "\nretry")[1:-1]
    exposed = secret if surroundings == "json_newlines" else quote(secret, safe="")

    class Echo(FakeCatalogFeedClient):
        async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
            raise MetaApiError("Upstream detail " + echoed, code=100)

    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    _use(monkeypatch, Echo())
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ToolError) as error:
        run(mcp_server.call_tool("create_product_feed_upload", {
            "feed_id": "10", "url": "https://example.invalid/f.csv", "username": "feed-user", "password": secret,
        }))
    assert exposed not in str(error.value)
    assert exposed not in _all_error_text(error.value)
    assert exposed not in caplog.text
    assert "[redacted]" in str(error.value)


def _within(seconds: float, call):
    """Run call under a deadline that interrupts it, so a slow matcher fails rather than hangs."""
    import signal
    import time

    def expired(_signum, _frame):
        raise TimeoutError(f"took longer than {seconds} s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    began = time.perf_counter()
    try:
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        assert time.perf_counter() - began < seconds


@pytest.mark.parametrize("size", [32, 4000])
def test_l7_secret_matcher_completes_a_small_nonmatch_in_bounded_time(monkeypatch, size) -> None:
    # The reviewer's dry run (a 33-character password, a 65-character username of backslashes)
    # and the same scaled up; before round seven 24 backslashes did not finish.
    _no_client(monkeypatch)
    password = "\\" * size + "Z"
    username = "\\" * (2 * size) + "Y"
    result = _within(2.0, lambda: run(catalog_feeds.create_product_feed_upload(
        "10", url="https://example.invalid/f.csv", username=username, password=password, dry_run=True,
    )))
    assert result["sent"] is False
    assert result["would_send"]["data"]["username"] == username
    assert result["would_send"]["data"]["password"] == "[redacted]"


_SCHEDULE_SECRET = "".join(("schedule-json-", "pass-7"))


def _schedule_text(password: str = _SCHEDULE_SECRET) -> str:
    return json.dumps({"url": "https://example.invalid/f.csv", "interval": "DAILY", "username": "u", "password": password})


@pytest.mark.parametrize("tool", ["create_product_feed", "update_product_feed"])
@pytest.mark.parametrize("key", ["schedule", "update_schedule"])
def test_schedule_given_as_json_text_is_refused_without_repeating_it(monkeypatch, caplog, tool, key) -> None:
    from meta_ads_mcp.coordinator import mcp_server

    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    _no_client(monkeypatch)
    arguments = {"catalog_id": "1", "name": "F"} if tool == "create_product_feed" else {"feed_id": "1"}
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ToolError) as error:
        run(mcp_server.call_tool(tool, {**arguments, key: _schedule_text()}))
    assert "must be an object" in str(error.value)
    assert _SCHEDULE_SECRET not in _all_error_text(error.value) and _SCHEDULE_SECRET not in caplog.text


def test_schedule_json_text_is_searched_as_its_object_for_secrets() -> None:
    for arguments in ({"schedule": _schedule_text()}, {"update_schedule": _schedule_text()},
                      {"params": {"schedule": _schedule_text()}}, {"params": {"update_schedule": _schedule_text()}}):
        assert catalog_feeds._caller_secrets(arguments) == [_SCHEDULE_SECRET]
    # Elsewhere JSON text is data, as an object there would be.
    assert catalog_feeds._caller_secrets({"params": {"custom": _schedule_text()}}) == []
    assert catalog_feeds._caller_secrets({"schedule": "not json {"}) == []


@pytest.mark.parametrize("password", ["".join(("x", "9")), _SCHEDULE_SECRET])
def test_schedule_returned_as_json_text_has_its_password_masked(monkeypatch, password) -> None:
    feed = {"id": "f1", "schedule": _schedule_text(password), "update_schedule": "no json"}
    _use(monkeypatch, FakeCatalogFeedClient(objects={"f1": feed}))
    result = run(catalog_feeds.list_product_feeds(feed_id="f1"))
    shown = json.loads(result["item"]["schedule"])
    assert shown["password"] == "[redacted]" and shown["username"] == "u"
    assert result["item"]["update_schedule"] == "no json"


def _retired_endpoint_error() -> MetaApiError:
    # Meta's live answer to DELETE /{rule_id}, seen on 2026-11-13 at v26.0.
    return MetaApiError(
        message="Accesssing Deprecated Platform API Endpoint - GraphProductFeedRuleDelete",
        status_code=400, code=100, subcode=33,
    )


def test_delete_feed_rule_says_plainly_when_meta_has_retired_the_delete(monkeypatch) -> None:
    rule = {"id": "rule_1", "attribute": "title", "rule_type": "letter_case_rule", "params": {"type": "to_upper"}}

    class RetiredDelete(FakeCatalogFeedClient):
        async def delete_object(self, object_id: str):
            self.calls.append({"method": "delete_object", "object_id": object_id})
            raise _retired_endpoint_error()

    client = _use(monkeypatch, RetiredDelete(objects={"rule_1": rule}))
    with pytest.raises(UnsupportedFeatureError) as error:
        run(catalog_feeds.delete_feed_rule(rule_id="rule_1"))
    text = str(error.value)
    assert "retired" in text and "not deleted" in text
    assert "delete_product_feed" in text and "Commerce Manager" in text
    assert client.methods() == ["get_object", "delete_object"]


def test_delete_feed_rule_keeps_any_other_meta_error(monkeypatch) -> None:
    rule = {"id": "rule_1", "attribute": "title", "rule_type": "letter_case_rule", "params": {"type": "to_upper"}}

    class RefusedDelete(FakeCatalogFeedClient):
        async def delete_object(self, object_id: str):
            raise _permission_error()

    _use(monkeypatch, RefusedDelete(objects={"rule_1": rule}))
    with pytest.raises(MetaApiError):
        run(catalog_feeds.delete_feed_rule(rule_id="rule_1"))


# round nine: an answer that is not JSON never lets a write go ahead or count as done ----------

_NON_JSON = {"text_response": "<html>busy</html>", "content_type": "text/html", "status_code": 200}
_FEED_WRITES = ("create_edge_object", "update_object", "delete_object", "delete_edge_object")


class _NonJsonFeedClient(FakeCatalogFeedClient):
    """Answers the named method with the Graph client's object for a 2xx body that was not JSON."""

    def __init__(self, non_json: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.non_json = non_json

    async def get_object(self, object_id: str, *, fields=None, params=None):
        answer = await super().get_object(object_id, fields=fields, params=params)
        return dict(_NON_JSON) if self.non_json == "get_object" else answer

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        answer = await super().list_objects(parent_id, edge, fields=fields, params=params)
        return dict(_NON_JSON) if self.non_json == "list_objects" else answer

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        answer = await super().create_edge_object(parent_id, edge, data=data, files=files)
        return dict(_NON_JSON) if self.non_json == "create_edge_object" else answer

    async def update_object(self, object_id: str, *, data):
        answer = await super().update_object(object_id, data=data)
        return dict(_NON_JSON) if self.non_json == "update_object" else answer

    async def delete_object(self, object_id: str):
        answer = await super().delete_object(object_id)
        return dict(_NON_JSON) if self.non_json == "delete_object" else answer

    async def delete_edge_object(self, parent_id: str, edge: str, *, data):
        answer = await super().delete_edge_object(parent_id, edge, data=data)
        return dict(_NON_JSON) if self.non_json == "delete_edge_object" else answer


_RULE = {"id": "20", "attribute": "brand", "rule_type": "mapping_rule", "params": {"map_from": "old"}}
_CONNECTED = {("10", "external_event_sources"): {"data": [{"id": "30", "name": "Pixel"}]}}

# Every tool here that reads before it writes: the call, the read, and what the fake needs.
_FEED_PREREADS = {
    "update_product_feed": (lambda: catalog_feeds.update_product_feed("f1", name="New"), "get_object", {}),
    "delete_product_feed": (lambda: catalog_feeds.delete_product_feed("f1"), "get_object", {}),
    "update_feed_rule": (lambda: catalog_feeds.update_feed_rule("20", {"map_from": "new"}), "get_object",
                         {"objects": {"20": dict(_RULE)}}),
    "delete_feed_rule": (lambda: catalog_feeds.delete_feed_rule("20"), "get_object", {"objects": {"20": dict(_RULE)}}),
    "connect_catalog_event_source": (lambda: catalog_feeds.connect_catalog_event_source("10", "31"), "list_objects", {}),
    "disconnect_catalog_event_source": (lambda: catalog_feeds.disconnect_catalog_event_source("10", "30"),
                                        "list_objects", {"edges": dict(_CONNECTED)}),
}


@pytest.mark.parametrize("tool", list(_FEED_PREREADS))
def test_a_non_json_pre_read_stops_the_write(monkeypatch, tool) -> None:
    call, read, setup = _FEED_PREREADS[tool]
    client = _use(monkeypatch, _NonJsonFeedClient(read, **setup))
    with pytest.raises(MetaApiError, match="not JSON") as raised:
        run(call())
    assert raised.value.mutation_outcome_unknown is False
    assert not [method for method in client.methods() if method in _FEED_WRITES]


# Every write here: the call, the write, and what the fake needs.
_FEED_WRITE_CALLS = {
    "create_product_feed": (lambda: catalog_feeds.create_product_feed("10", "Feed"), "create_edge_object", {}),
    "update_product_feed": (lambda: catalog_feeds.update_product_feed("f1", name="New"), "update_object", {}),
    "delete_product_feed": (lambda: catalog_feeds.delete_product_feed("f1"), "delete_object", {}),
    "create_product_feed_upload": (lambda: catalog_feeds.create_product_feed_upload(
        "10", url="https://example.invalid/f.csv"), "create_edge_object", {}),
    "create_feed_rule": (lambda: catalog_feeds.create_feed_rule("10", "brand", "mapping_rule", {"map_from": "b"}),
                         "create_edge_object", {}),
    "update_feed_rule": (lambda: catalog_feeds.update_feed_rule("20", {"map_from": "new"}), "update_object",
                         {"objects": {"20": dict(_RULE)}}),
    "delete_feed_rule": (lambda: catalog_feeds.delete_feed_rule("20"), "delete_object", {"objects": {"20": dict(_RULE)}}),
    "connect_catalog_event_source": (lambda: catalog_feeds.connect_catalog_event_source("10", "31"),
                                     "create_edge_object", {}),
    "disconnect_catalog_event_source": (lambda: catalog_feeds.disconnect_catalog_event_source("10", "30"),
                                        "delete_edge_object", {"edges": dict(_CONNECTED)}),
}


@pytest.mark.parametrize("tool", list(_FEED_WRITE_CALLS))
def test_a_non_json_answer_to_a_write_is_not_reported_as_done(monkeypatch, tool) -> None:
    call, write, setup = _FEED_WRITE_CALLS[tool]
    client = _use(monkeypatch, _NonJsonFeedClient(write, **setup))
    with pytest.raises(MetaApiError, match="not known") as raised:
        run(call())
    assert raised.value.mutation_outcome_unknown is True
    assert client.methods().count(write) == 1
    assert "busy" not in str(raised.value)  # the body itself is never quoted


@pytest.mark.parametrize("body", [None, 0, "text", "non-json"])
def test_unreadable_pre_read_stops_a_feed_delete_over_the_real_client(monkeypatch, body) -> None:
    """R7 M2's shape, extended to a 2xx body that is not JSON at all."""
    import httpx

    from meta_ads_mcp.graph_api import GraphAPIClient

    monkeypatch.setenv("META_ACCESS_TOKEN", "test-token")
    methods: list[str] = []

    def respond(request):
        methods.append(request.method)
        if request.method != "GET":
            return httpx.Response(200, content=json.dumps(True))
        if body == "non-json":
            return httpx.Response(200, content=b"<html>busy</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})

    async def scenario():
        graph = GraphAPIClient(reload_settings())
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            monkeypatch.setattr(GraphAPIClient, "_get_shared_client", lambda self, **kwargs: http)
            monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: graph)
            with pytest.raises(MetaApiError):
                await catalog_feeds.delete_product_feed("10")

    run(scenario())
    assert methods == ["GET"]
