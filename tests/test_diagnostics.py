"""Diagnostic utility tests."""

from __future__ import annotations

from meta_ads_mcp.diagnostics import compare_metric_sets, detect_snapshot_findings, rank_rows


def test_compare_metric_sets_includes_deltas() -> None:
    comparison = compare_metric_sets(
        {"spend": 100.0, "ctr": 0.02},
        {"spend": 80.0, "ctr": 0.01},
    )
    assert comparison["spend"]["delta"] == 20.0
    assert comparison["ctr"]["pct_delta"] == 1.0


def test_detect_snapshot_findings_flags_high_spend_low_conversion() -> None:
    findings = detect_snapshot_findings({"spend": 200.0, "conversions": 0.0, "ctr": 0.008})
    assert any(finding["type"] == "high_spend_low_conversion" for finding in findings)


def test_rank_rows_supports_nested_metrics() -> None:
    rows = [
        {"id": "a", "metrics": {"roas": 1.2}},
        {"id": "b", "metrics": {"roas": 2.5}},
    ]
    ranked = rank_rows(rows, "roas")
    assert ranked[0]["id"] == "b"
