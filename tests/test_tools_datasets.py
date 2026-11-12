"""Dataset, pixel, and custom conversion tool tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from meta_ads_mcp.errors import MetaApiError, ValidationError
from meta_ads_mcp.tools import datasets, utility


class FakeDatasetClient:
    """Fake Graph client that records every call and returns canned payloads."""

    def __init__(
        self,
        *,
        list_pages: dict[str, list[dict[str, Any]]] | None = None,
        object_payloads: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.list_pages = list_pages or {}
        self.object_payloads = object_payloads or {}

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.list_calls.append(
            {"parent_id": parent_id, "edge": edge, "fields": fields, "params": dict(params or {})}
        )
        pages = self.list_pages.get(edge)
        if pages is None:
            return {"data": [{"id": f"{edge}_1", "name": f"{edge} one"}]}
        index = sum(1 for call in self.list_calls if call["edge"] == edge) - 1
        return pages[min(index, len(pages) - 1)]

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.get_calls.append({"object_id": object_id, "fields": fields, "params": dict(params or {})})
        return self.object_payloads.get(object_id, {"id": object_id, "name": "Pixel One"})


def _no_client():
    pytest.fail("No Graph client may be created when input validation fails.")


def _use(monkeypatch, client) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", lambda: client)


def _permission_error() -> MetaApiError:
    return MetaApiError(
        message="(#200) Missing Permission",
        status_code=403,
        code=200,
    )


# ---------------------------------------------------------------- list_datasets


def test_list_datasets_reads_account_adspixels_with_paging(monkeypatch) -> None:
    client = FakeDatasetClient(list_pages={"adspixels": [{
        "data": [{"id": "px1", "name": "Main pixel", "last_fired_time": "2026-11-11T10:00:00+0000"}],
        "paging": {"next": "https://graph/next", "cursors": {"after": "cur2"}},
    }]})
    _use(monkeypatch, client)

    result = asyncio.run(datasets.list_datasets(account_id="123", limit=10, after="cur1"))

    call = client.list_calls[0]
    assert call["parent_id"] == "act_123"
    assert call["edge"] == "adspixels"
    assert call["params"] == {"limit": 10, "after": "cur1"}
    assert call["fields"] == datasets.DATASET_SUMMARY_FIELDS
    assert result["items"][0]["id"] == "px1"
    assert result["summary"]["count"] == 1
    assert result["summary"]["complete"] is False
    assert result["summary"]["truncated"] is True
    assert result["summary"]["next_after"] == "cur2"
    assert result["summary"]["scope"] == {"account_id": "act_123"}


def test_list_datasets_reads_business_adspixels_and_custom_fields(monkeypatch) -> None:
    client = FakeDatasetClient()
    _use(monkeypatch, client)

    result = asyncio.run(datasets.list_datasets(business_id=" 987 ", fields="id,name"))

    call = client.list_calls[0]
    assert call["parent_id"] == "987"
    assert call["edge"] == "adspixels"
    assert call["fields"] == ["id", "name"]
    assert call["params"] == {"limit": 50}
    assert result["summary"]["complete"] is True
    assert result["summary"]["next_after"] is None
    assert result["summary"]["scope"] == {"business_id": "987"}


@pytest.mark.parametrize("arguments", [
    {},
    {"account_id": "123", "business_id": "987"},
    {"account_id": "  ", "business_id": ""},
])
def test_list_datasets_requires_exactly_one_of_account_or_business(monkeypatch, arguments) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    with pytest.raises(ValidationError, match="exactly one of account_id or business_id"):
        asyncio.run(datasets.list_datasets(**arguments))


def test_list_datasets_rejects_non_positive_limit(monkeypatch) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    with pytest.raises(ValidationError, match="limit"):
        asyncio.run(datasets.list_datasets(account_id="123", limit=0))


def test_permission_error_is_not_swallowed(monkeypatch) -> None:
    class DeniedClient(FakeDatasetClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            raise _permission_error()

        async def get_object(self, object_id, *, fields=None, params=None):
            raise _permission_error()

    _use(monkeypatch, DeniedClient())
    with pytest.raises(MetaApiError) as raised:
        asyncio.run(datasets.list_datasets(account_id="123"))
    assert raised.value.code == 200
    for call in (
        lambda: datasets.list_custom_conversions(account_id="123"),
        lambda: datasets.get_dataset(dataset_id="px1"),
        lambda: datasets.get_dataset_quality(dataset_id="px1"),
        lambda: datasets.get_dataset_stats(dataset_id="px1", since="2026-11-01", until="2026-11-02"),
    ):
        with pytest.raises(MetaApiError):
            asyncio.run(call())


# ------------------------------------------------------------------ get_dataset


def test_get_dataset_reads_pixel_node_with_default_fields(monkeypatch) -> None:
    client = FakeDatasetClient(object_payloads={"px1": {"id": "px1", "name": "Main", "last_upload_app": "Shopify"}})
    _use(monkeypatch, client)

    result = asyncio.run(datasets.get_dataset(dataset_id=" px1 "))

    assert client.get_calls == [{"object_id": "px1", "fields": datasets.DATASET_FIELDS, "params": {}}]
    assert result == {"item": {"id": "px1", "name": "Main", "last_upload_app": "Shopify"}, "summary": {"count": 1}}
    for field in ("data_use_setting", "is_unavailable", "last_upload_app", "enable_automatic_matching"):
        assert field in datasets.DATASET_FIELDS
    assert "code" not in datasets.DATASET_FIELDS


def test_get_dataset_accepts_custom_fields(monkeypatch) -> None:
    client = FakeDatasetClient()
    _use(monkeypatch, client)
    asyncio.run(datasets.get_dataset(dataset_id="px1", fields="id, name, code"))
    assert client.get_calls[0]["fields"] == ["id", "name", "code"]


def test_get_dataset_requires_id(monkeypatch) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    with pytest.raises(ValidationError, match="dataset_id is required"):
        asyncio.run(datasets.get_dataset(dataset_id=" "))


# ------------------------------------------------------------ get_dataset_stats


def _stats_page(rows, *, next_after: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"data": rows}
    if next_after:
        payload["paging"] = {"next": f"https://graph/next?after={next_after}", "cursors": {"after": next_after}}
    return payload


def test_dataset_stats_requests_stats_edge_and_totals_by_value(monkeypatch) -> None:
    client = FakeDatasetClient(list_pages={"stats": [
        _stats_page([
            {"aggregation": "event", "start_time": "2026-11-01T00:00:00+0000",
             "data": [{"value": "PageView", "count": 10}, {"value": "Purchase", "count": "2"}]},
        ], next_after="c2"),
        _stats_page([
            {"aggregation": "event", "start_time": "2026-11-01T01:00:00+0000",
             "data": [{"value": "PageView", "count": 5}, {"value": "Lead", "count": 3}]},
        ]),
    ]})
    _use(monkeypatch, client)
    monkeypatch.setattr(datasets, "_utc_now", lambda: datetime(2026, 11, 12, 12, tzinfo=timezone.utc))

    result = asyncio.run(datasets.get_dataset_stats(
        dataset_id="px1", since="2026-11-01", until="2026-11-02",
    ))

    first, second = client.list_calls
    assert first["parent_id"] == "px1"
    assert first["edge"] == "stats"
    start = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp())
    assert first["params"] == {"aggregation": "event", "start_time": start, "end_time": end}
    assert second["params"] == {"aggregation": "event", "start_time": start, "end_time": end, "after": "c2"}
    assert result["items"] == [
        {"value": "PageView", "count": 15},
        {"value": "Lead", "count": 3},
        {"value": "Purchase", "count": 2},
    ]
    summary = result["summary"]
    assert summary["total_count"] == 20
    assert summary["complete"] is True
    assert summary["pages_fetched"] == 2
    assert summary["hours_with_data"] == 2
    assert summary["first_hour"] == "2026-11-01T00:00:00+0000"
    assert summary["last_hour"] == "2026-11-01T01:00:00+0000"
    assert result["scope"] == {
        "dataset_id": "px1", "since": "2026-11-01", "until": "2026-11-02",
        "aggregation": "event", "event": None, "event_source": None,
        "start_time": start, "end_time": end,
    }
    assert "hourly" not in result


def test_dataset_stats_clamps_end_time_to_now_and_can_return_hourly(monkeypatch) -> None:
    client = FakeDatasetClient(list_pages={"stats": [_stats_page([
        {"aggregation": "device_type", "start_time": "2026-11-12T00:00:00+0000",
         "data": [{"value": "desktop", "count": 4}]},
    ])]})
    _use(monkeypatch, client)
    now = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(datasets, "_utc_now", lambda: now)

    result = asyncio.run(datasets.get_dataset_stats(
        dataset_id="px1", since="2026-11-12", until="2026-11-12",
        aggregation="device_type", event_source="server_only", include_hourly=True,
    ))

    params = client.list_calls[0]["params"]
    assert params["end_time"] == int(now.timestamp())
    assert params["aggregation"] == "device_type"
    assert params["event_source"] == "SERVER_ONLY"
    assert result["hourly"] == [
        {"start_time": "2026-11-12T00:00:00+0000", "data": [{"value": "desktop", "count": 4}]},
    ]


def test_dataset_stats_defaults_to_last_seven_days(monkeypatch) -> None:
    client = FakeDatasetClient(list_pages={"stats": [_stats_page([])]})
    _use(monkeypatch, client)
    monkeypatch.setattr(datasets, "_utc_now", lambda: datetime(2026, 11, 12, 6, tzinfo=timezone.utc))

    result = asyncio.run(datasets.get_dataset_stats(dataset_id="px1"))

    assert result["scope"]["since"] == "2026-11-06"
    assert result["scope"]["until"] == "2026-11-12"
    assert result["items"] == []
    assert result["summary"]["total_count"] == 0
    assert result["summary"]["complete"] is True


def test_dataset_stats_custom_data_field_sends_event(monkeypatch) -> None:
    client = FakeDatasetClient(list_pages={"stats": [_stats_page([])]})
    _use(monkeypatch, client)
    monkeypatch.setattr(datasets, "_utc_now", lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
    asyncio.run(datasets.get_dataset_stats(
        dataset_id="px1", since="2026-11-01", until="2026-11-02",
        aggregation="custom_data_field", event="Purchase",
    ))
    assert client.list_calls[0]["params"]["event"] == "Purchase"


def test_dataset_stats_reports_incomplete_scan_at_page_cap(monkeypatch) -> None:
    endless = _stats_page([
        {"aggregation": "event", "start_time": "2026-11-01T00:00:00+0000", "data": [{"value": "PageView", "count": 1}]},
    ], next_after="more")
    client = FakeDatasetClient(list_pages={"stats": [endless]})
    _use(monkeypatch, client)
    monkeypatch.setattr(datasets, "_utc_now", lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(datasets, "MAX_STATS_PAGES", 3)

    result = asyncio.run(datasets.get_dataset_stats(dataset_id="px1", since="2026-11-01", until="2026-11-02"))

    assert len(client.list_calls) == 3
    assert result["summary"]["complete"] is False
    assert result["summary"]["pages_fetched"] == 3
    assert result["summary"]["total_count"] == 3
    assert "incomplete" in result["summary"]["note"].lower()


def test_dataset_stats_rejects_windows_over_28_days(monkeypatch) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    monkeypatch.setattr(datasets, "_utc_now", lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
    with pytest.raises(ValidationError, match="28 days"):
        asyncio.run(datasets.get_dataset_stats(dataset_id="px1", since="2026-10-01", until="2026-10-29"))


def test_dataset_stats_accepts_exactly_28_days(monkeypatch) -> None:
    client = FakeDatasetClient(list_pages={"stats": [_stats_page([])]})
    _use(monkeypatch, client)
    monkeypatch.setattr(datasets, "_utc_now", lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
    result = asyncio.run(datasets.get_dataset_stats(dataset_id="px1", since="2026-10-01", until="2026-10-28"))
    assert result["summary"]["complete"] is True


def test_dataset_stats_rejects_until_before_since(monkeypatch) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    with pytest.raises(ValidationError, match="until must not be before since"):
        asyncio.run(datasets.get_dataset_stats(dataset_id="px1", since="2026-11-05", until="2026-11-04"))


@pytest.mark.parametrize(("arguments", "message"), [
    ({"dataset_id": " "}, "dataset_id is required"),
    ({"dataset_id": "px1", "since": "11/01/2026", "until": "2026-11-02"}, "YYYY-MM-DD"),
    ({"dataset_id": "px1", "since": "2026-11-01", "until": "2026-11-02", "aggregation": "country"}, "browser_type"),
    ({"dataset_id": "px1", "since": "2026-11-01", "until": "2026-11-02", "aggregation": "custom_data_field"}, "event is required"),
    ({"dataset_id": "px1", "since": "2026-11-01", "until": "2026-11-02", "event_source": "APP"}, "WEB_ONLY"),
    ({"dataset_id": "px1", "since": "2026-11-20", "until": "2026-11-21"}, "future"),
])
def test_dataset_stats_validation_errors_before_any_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    monkeypatch.setattr(datasets, "_utc_now", lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
    with pytest.raises(ValidationError, match=message):
        asyncio.run(datasets.get_dataset_stats(**arguments))


# ---------------------------------------------------------- get_dataset_quality


QUALITY_PAYLOAD = {
    "web": [
        {
            "event_name": "Purchase",
            "event_match_quality": {
                "composite_score": 8.1,
                "match_key_feedback": [
                    {"identifier": "email", "coverage": {"percentage": 92.5}},
                    {"identifier": "phone", "coverage": {"percentage": 40}},
                ],
                "diagnostics": [],
            },
            "event_coverage": {"percentage": 80, "goal_percentage": 75},
            "data_freshness": {"upload_frequency": "real_time", "description": "Real time"},
        },
        {
            "event_name": "Lead",
            "event_match_quality": {
                "composite_score": 4.2,
                "match_key_feedback": [{"identifier": "email", "coverage": {"percentage": 10}}],
                "diagnostics": [{"name": "Missing email", "percentage": 90}],
            },
            "event_coverage": {"percentage": 50, "goal_percentage": 75},
            "data_freshness": {"upload_frequency": "hourly"},
        },
        {"event_name": "PageView"},
    ]
}


def test_dataset_quality_requests_dataset_quality_root_edge(monkeypatch) -> None:
    client = FakeDatasetClient(object_payloads={"dataset_quality": QUALITY_PAYLOAD})
    _use(monkeypatch, client)

    asyncio.run(datasets.get_dataset_quality(dataset_id="px1", agent_name=" shopify "))

    call = client.get_calls[0]
    assert call["object_id"] == "dataset_quality"
    assert call["params"] == {"dataset_id": "px1", "agent_name": "shopify"}
    assert call["fields"] == [datasets.DATASET_QUALITY_FIELDS]
    assert call["fields"][0].startswith("web{event_name,event_match_quality{composite_score,")


def test_dataset_quality_flags_weak_scores_with_the_threshold_used(monkeypatch) -> None:
    client = FakeDatasetClient(object_payloads={"dataset_quality": QUALITY_PAYLOAD})
    _use(monkeypatch, client)

    result = asyncio.run(datasets.get_dataset_quality(dataset_id="px1"))

    weak = [finding for finding in result["findings"] if finding["type"] == "weak_event_match_quality"]
    assert [finding["event_name"] for finding in weak] == ["Lead"]
    assert weak[0]["composite_score"] == 4.2
    assert weak[0]["threshold"] == datasets.WEAK_EMQ_SCORE_BELOW == 6.0
    coverage = [finding for finding in result["findings"] if finding["type"] == "low_event_coverage"]
    assert [(finding["event_name"], finding["threshold"]) for finding in coverage] == [("Lead", 75)]
    assert result["thresholds"]["weak_emq_score_below"] == 6.0
    assert "source" in result["thresholds"]
    assert result["metrics"] == {
        "events": 3,
        "events_with_emq_score": 2,
        "weak_emq_events": 1,
        "low_coverage_events": 1,
    }
    purchase = next(event for event in result["events"] if event["event_name"] == "Purchase")
    assert purchase["emq_score"] == 8.1
    assert purchase["emq_is_weak"] is False
    assert purchase["match_key_coverage"] == [
        {"identifier": "email", "coverage_percentage": 92.5},
        {"identifier": "phone", "coverage_percentage": 40},
    ]
    assert purchase["data_freshness"] == {"upload_frequency": "real_time", "description": "Real time"}
    lead = next(event for event in result["events"] if event["event_name"] == "Lead")
    assert lead["diagnostics"] == [{"name": "Missing email", "percentage": 90}]
    assert any("PageView" in signal for signal in result["missing_signals"])
    assert result["scope"] == {"dataset_id": "px1", "channel": "web", "agent_name": None}


def test_dataset_quality_without_web_data_says_so(monkeypatch) -> None:
    client = FakeDatasetClient(object_payloads={"dataset_quality": {}})
    _use(monkeypatch, client)
    result = asyncio.run(datasets.get_dataset_quality(dataset_id="px1"))
    assert result["events"] == []
    assert result["findings"] == []
    assert result["missing_signals"]
    assert "web" in result["missing_signals"][0]


def test_dataset_quality_requires_id(monkeypatch) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    with pytest.raises(ValidationError, match="dataset_id is required"):
        asyncio.run(datasets.get_dataset_quality(dataset_id=""))


# ------------------------------------------------------ list_custom_conversions


def test_list_custom_conversions_reads_account_edge(monkeypatch) -> None:
    client = FakeDatasetClient(list_pages={"customconversions": [{
        "data": [{"id": "cc1", "name": "Booked", "custom_event_type": "LEAD"}],
        "paging": {"cursors": {"after": "x"}},
    }]})
    _use(monkeypatch, client)

    result = asyncio.run(datasets.list_custom_conversions(account_id="act_123", limit=5, after="a"))

    call = client.list_calls[0]
    assert (call["parent_id"], call["edge"]) == ("act_123", "customconversions")
    assert call["params"] == {"limit": 5, "after": "a"}
    assert call["fields"] == datasets.CUSTOM_CONVERSION_FIELDS
    assert result["items"][0]["custom_event_type"] == "LEAD"
    assert result["summary"]["complete"] is True
    assert result["summary"]["next_after"] is None


def test_list_custom_conversions_validates_input(monkeypatch) -> None:
    monkeypatch.setattr(datasets, "get_graph_api_client", _no_client)
    with pytest.raises(ValidationError, match="account_id is required"):
        asyncio.run(datasets.list_custom_conversions(account_id=" "))
    with pytest.raises(ValidationError, match="limit"):
        asyncio.run(datasets.list_custom_conversions(account_id="123", limit=0))


# --------------------------------------------------------------- registration


SIGNAL_TOOLS = {
    "list_datasets",
    "get_dataset",
    "get_dataset_stats",
    "get_dataset_quality",
    "list_custom_conversions",
}


def test_dataset_tools_are_read_only_signal_tools() -> None:
    from meta_ads_mcp.read_only import read_only_tool_names

    assert SIGNAL_TOOLS <= set(utility.TOOL_GROUPS["signals"])
    assert SIGNAL_TOOLS <= read_only_tool_names()
    assert not SIGNAL_TOOLS & set(utility.TOOL_GROUPS["writes"])


PIXEL_RULE_AND_PARAMETER_TOOL_NAMES = (
    "list_pixel_event_rules", "create_pixel_event_rule", "update_pixel_event_rule", "delete_pixel_event_rule",
    "list_pixel_parameters", "create_pixel_parameter", "update_pixel_parameter", "delete_pixel_parameter",
)


def test_pixel_rule_and_parameter_tools_are_not_registered() -> None:
    """Meta rejects GET {pixel_id}/event_rules (code 190) and {pixel_id}/extractors (#100) live."""
    from meta_ads_mcp.coordinator import mcp_server

    registered = {getattr(component, "name", None) for component in mcp_server.local_provider._components.values()}
    grouped = {name for names in utility.TOOL_GROUPS.values() for name in names}
    for name in PIXEL_RULE_AND_PARAMETER_TOOL_NAMES:
        assert name not in registered
        assert name not in grouped
        assert not hasattr(datasets, name)


def test_intent_guide_does_not_mention_pixel_rules_or_extractors() -> None:
    route = utility.INTENT_GUIDE["inspect_pixels_datasets_or_tracking"]
    text = json.dumps(route).lower()
    assert "event rule" not in text
    assert "extractor" not in text


def test_dataset_quality_response_is_json_serializable(monkeypatch) -> None:
    _use(monkeypatch, FakeDatasetClient(object_payloads={"dataset_quality": QUALITY_PAYLOAD}))
    json.dumps(asyncio.run(datasets.get_dataset_quality(dataset_id="px1")))


def test_intent_guide_routes_pixel_questions_to_signal_tools() -> None:
    route = utility.INTENT_GUIDE["inspect_pixels_datasets_or_tracking"]
    routed = set(route["recommended_order"]) | set(route["avoid_unless_needed"])
    assert routed == SIGNAL_TOOLS
    closest = utility._closest_intents("check pixel event match quality")
    assert closest[0]["intent"] == "inspect_pixels_datasets_or_tracking"
