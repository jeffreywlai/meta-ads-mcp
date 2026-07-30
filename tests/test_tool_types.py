"""Shared LLM-facing type normalization tests."""

from __future__ import annotations

from meta_ads_mcp.tool_types import coerce_csv_string_list, normalize_field_list
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
