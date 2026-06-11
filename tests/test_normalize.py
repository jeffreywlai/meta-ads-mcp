"""Normalization and metric tests."""

from __future__ import annotations

from meta_ads_mcp.diagnostics import derive_core_metrics
from meta_ads_mcp.normalize import blank_to_none, normalize_insights_row


def test_blank_to_none_strips_optional_strings() -> None:
    assert blank_to_none(None) is None
    assert blank_to_none("  ") is None
    assert blank_to_none(" act_123 ") == "act_123"


def test_normalize_insights_row_extracts_actions() -> None:
    row = normalize_insights_row(
        {
            "spend": "120.50",
            "impressions": "1000",
            "clicks": "50",
            "frequency": "1.2",
            "actions": [{"action_type": "purchase", "value": "4"}],
            "action_values": [{"action_type": "purchase", "value": "400"}],
        }
    )
    assert row["spend"] == 120.50
    assert row["impressions"] == 1000
    assert row["actions_map"]["purchase"] == 4.0
    assert row["result_value"] == 400.0


def test_normalize_insights_row_extracts_pixel_purchase_actions() -> None:
    row = normalize_insights_row(
        {
            "spend": "120.50",
            "actions": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "4"}],
            "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "400"}],
        }
    )

    assert row["results"] == 4.0
    assert row["result_value"] == 400.0


def test_derive_core_metrics_computes_kpis() -> None:
    metrics = derive_core_metrics(
        {
            "spend": 120.0,
            "impressions": 1000,
            "clicks": 50,
            "frequency": 1.3,
            "results": 4.0,
            "result_value": 400.0,
        }
    )
    assert metrics["ctr"] == 0.05
    assert metrics["cpc"] == 2.4
    assert metrics["cpm"] == 120.0
    assert metrics["cpa"] == 30.0
    assert round(metrics["roas"], 2) == 3.33
