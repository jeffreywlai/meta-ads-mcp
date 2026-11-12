"""Shared LLM-facing type normalization tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.tool_types import coerce_csv_string_list, normalize_field_list
from meta_ads_mcp.tools import insights
from meta_ads_mcp.tools.insights import _comparison_fields, _insights_fields


def test_csv_fields_split_only_at_top_level_commas() -> None:
    value = (
        "id,creative{id,name,asset_feed_spec{images{hash,url}}},"
        'labeling("a,b"),url_tags'
    )

    assert coerce_csv_string_list(value) == [
        "id",
        "creative{id,name,asset_feed_spec{images{hash,url}}}",
        'labeling("a,b")',
        "url_tags",
    ]


def test_direct_insights_helpers_normalize_csv_fields() -> None:
    assert _insights_fields("impressions,spend") == ["impressions", "spend"]
    assert _comparison_fields("campaign", "impressions,spend") == [
        "impressions",
        "spend",
        "campaign_name",
        "campaign_id",
    ]


def test_normalize_field_list_preserves_list_inputs() -> None:
    fields = ["id", "creative{id,name}"]
    assert normalize_field_list(fields) == fields


def test_create_async_insights_normalizes_direct_csv_metadata(monkeypatch) -> None:
    class FakeClient:
        async def create_async_insights_report(self, object_id, *, fields, params):
            assert fields == ["impressions", "actions{action_type,value}"]
            return {"report_run_id": "run_123"}

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeClient())
    result = asyncio.run(
        insights.create_async_insights_report(
            level="campaign",
            object_id="cmp_123",
            fields="impressions,actions{action_type,value}",
        )
    )

    assert result["requested_fields"] == [
        "impressions",
        "actions{action_type,value}",
    ]


def test_graph_id_and_account_id_types_accept_real_ids_and_reject_everything_else() -> None:
    from pydantic import TypeAdapter
    from pydantic import ValidationError as PydanticValidationError

    from meta_ads_mcp.tool_types import AccountId, GraphId

    graph_id = TypeAdapter(GraphId)
    account_id = TypeAdapter(AccountId)
    assert graph_id.validate_python("1234567890") == "1234567890"
    assert account_id.validate_python("act_123") == "act_123"
    assert account_id.validate_python("123") == "123"
    for bad in ("", " ", "act_123", "12 3", "123/products", "123?x=1", "abc", "1.5", "-1"):
        with pytest.raises(PydanticValidationError):
            graph_id.validate_python(bad)
    for bad in ("", "act_", "act_abc", "123/ads", "act_1?x=1", "me"):
        with pytest.raises(PydanticValidationError):
            account_id.validate_python(bad)
    # Ids stay strings: a number is not silently converted.
    with pytest.raises(PydanticValidationError):
        graph_id.validate_python(123)
