"""Derived metrics and optimization heuristics."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from .normalize import to_float


def metric_evidence(
    metric: str,
    value: float | None,
    formula: str,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Build a standard evidence record."""
    return {
        "metric": metric,
        "value": value,
        "formula": formula,
        "inputs": inputs,
    }


def derive_core_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Compute stable KPIs from a normalized row."""
    spend = to_float(row.get("spend"))
    impressions = to_float(row.get("impressions"))
    clicks = to_float(row.get("clicks"))
    frequency = to_float(row.get("frequency"))
    results = to_float(row.get("results"))
    result_value = to_float(row.get("result_value"))

    ctr = to_float(row.get("ctr"))
    if ctr is None and clicks and impressions:
        ctr = clicks / impressions

    cpc = to_float(row.get("cpc"))
    if cpc is None and spend is not None and clicks:
        cpc = spend / clicks

    cpm = to_float(row.get("cpm"))
    if cpm is None and spend is not None and impressions:
        cpm = (spend / impressions) * 1000

    cvr = None
    if results is not None and clicks:
        cvr = results / clicks

    cpa = None
    if spend is not None and results:
        cpa = spend / results

    roas = None
    if result_value is not None and spend:
        roas = result_value / spend

    return {
        "spend": spend,
        "impressions": int(impressions) if impressions is not None else None,
        "clicks": int(clicks) if clicks is not None else None,
        "frequency": frequency,
        "ctr": ctr,
        "cpc": cpc,
        "cpm": cpm,
        "conversions": results,
        "conversion_value": result_value,
        "cvr": cvr,
        "cpa": cpa,
        "roas": roas,
    }


def summary_metric_evidence(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Build evidence records for available derived summary metrics."""
    evidence: list[dict[str, Any]] = []
    clicks = to_float(metrics.get("clicks"))
    impressions = to_float(metrics.get("impressions"))
    spend = to_float(metrics.get("spend"))
    frequency = to_float(metrics.get("frequency"))
    conversions = to_float(metrics.get("conversions"))
    conversion_value = to_float(metrics.get("conversion_value"))
    ctr = to_float(metrics.get("ctr"))
    cpc = to_float(metrics.get("cpc"))
    cpm = to_float(metrics.get("cpm"))
    cvr = to_float(metrics.get("cvr"))
    cpa = to_float(metrics.get("cpa"))
    roas = to_float(metrics.get("roas"))
    covered_metrics: set[str] = set()

    if clicks is not None and impressions is not None:
        evidence.append(metric_evidence("ctr", ctr, "clicks / impressions", {"clicks": clicks, "impressions": impressions}))
        covered_metrics.add("ctr")
    if spend is not None and clicks is not None and clicks > 0:
        evidence.append(metric_evidence("cpc", cpc, "spend / clicks", {"spend": spend, "clicks": clicks}))
        covered_metrics.add("cpc")
    if spend is not None and impressions is not None and impressions > 0:
        evidence.append(metric_evidence("cpm", cpm, "(spend / impressions) * 1000", {"spend": spend, "impressions": impressions}))
        covered_metrics.add("cpm")
    if conversions is not None and clicks is not None and clicks > 0:
        evidence.append(metric_evidence("cvr", cvr, "conversions / clicks", {"conversions": conversions, "clicks": clicks}))
        covered_metrics.add("cvr")
    if spend is not None and conversions is not None and conversions > 0:
        evidence.append(metric_evidence("cpa", cpa, "spend / conversions", {"spend": spend, "conversions": conversions}))
        covered_metrics.add("cpa")
    if conversion_value is not None and spend is not None and spend > 0:
        evidence.append(metric_evidence("roas", roas, "conversion_value / spend", {"conversion_value": conversion_value, "spend": spend}))
        covered_metrics.add("roas")

    fallback_metrics = [
        ("frequency", frequency),
        ("ctr", ctr),
        ("cpc", cpc),
        ("cpm", cpm),
        ("cvr", cvr),
        ("cpa", cpa),
        ("roas", roas),
    ]
    for name, value in fallback_metrics:
        if value is None or name in covered_metrics:
            continue
        evidence.append(metric_evidence(name, value, "provided_by_meta", {name: value}))
    return evidence


def compare_metric_sets(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, dict[str, float | None]]:
    """Compare current and previous metrics."""
    comparison: dict[str, dict[str, float | None]] = {}
    keys = sorted(set(current) | set(previous))
    for key in keys:
        current_value = to_float(current.get(key))
        previous_value = to_float(previous.get(key))
        delta = None
        pct_delta = None
        if current_value is not None and previous_value is not None:
            delta = current_value - previous_value
            if previous_value != 0:
                pct_delta = delta / previous_value
        comparison[key] = {
            "current": current_value,
            "previous": previous_value,
            "delta": delta,
            "pct_delta": pct_delta,
        }
    return comparison


def rank_rows(
    rows: Iterable[dict[str, Any]],
    metric: str,
    *,
    reverse: bool = True,
) -> list[dict[str, Any]]:
    """Sort rows by a metric with null-safe ordering."""
    def metric_value(row: dict[str, Any]) -> float:
        direct = to_float(row.get(metric))
        if direct is not None:
            return direct
        nested = row.get("metrics", {})
        return to_float(nested.get(metric)) or 0.0

    return sorted(rows, key=metric_value, reverse=reverse)


def annotate_share_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add spend_share and result_share to row metrics when totals are available."""
    def row_metric(row: dict[str, Any], name: str) -> float | None:
        nested = to_float((row.get("metrics") or {}).get(name))
        if nested is not None:
            return nested
        return to_float(row.get(name))

    total_spend = sum((row_metric(row, "spend") or 0.0) for row in rows)
    total_results = sum((row_metric(row, "conversions") or 0.0) for row in rows)
    for row in rows:
        metrics = row.setdefault("metrics", {})
        spend = row_metric(row, "spend")
        conversions = row_metric(row, "conversions")
        if total_spend > 0 and spend is not None:
            metrics["spend_share"] = spend / total_spend
        if total_results > 0 and conversions is not None:
            metrics["result_share"] = conversions / total_results
    return rows


def build_finding(
    finding_type: str,
    summary: str,
    *,
    severity: str = "medium",
    confidence: float = 0.5,
    evidence: list[dict[str, Any]] | None = None,
    affected_entities: list[dict[str, Any]] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Build a machine-readable finding."""
    return {
        "type": finding_type,
        "severity": severity,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence or [],
        "affected_entities": affected_entities or [],
        "next_actions": next_actions or [],
    }


def detect_snapshot_findings(
    summary_metrics: dict[str, Any],
    child_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate coarse optimization findings."""
    findings: list[dict[str, Any]] = []

    spend = to_float(summary_metrics.get("spend"))
    ctr = to_float(summary_metrics.get("ctr"))
    frequency = to_float(summary_metrics.get("frequency"))
    conversions = to_float(summary_metrics.get("conversions"))
    roas = to_float(summary_metrics.get("roas"))

    if spend and spend > 0 and not conversions:
        findings.append(
            build_finding(
                "high_spend_low_conversion",
                "Spend is present but no conversions were observed in the selected window.",
                severity="high",
                confidence=0.85,
            )
        )

    if frequency and frequency >= 2.5 and ctr is not None and ctr < 0.01:
        findings.append(
            build_finding(
                "high_frequency_declining_ctr",
                "Frequency is elevated while click-through rate is weak.",
                severity="medium",
                confidence=0.7,
            )
        )

    if roas is not None and roas < 1:
        findings.append(
            build_finding(
                "low_roas",
                "Observed ROAS is below 1.0 in the selected window.",
                severity="medium",
                confidence=0.75,
            )
        )

    if child_rows:
        sorted_rows = rank_rows(child_rows, "spend")
        def row_spend(row: dict[str, Any]) -> float:
            direct = to_float(row.get("spend"))
            if direct is not None:
                return direct
            return to_float((row.get("metrics") or {}).get("spend")) or 0.0

        total_spend = sum(row_spend(row) for row in sorted_rows)
        top_three_spend = sum(row_spend(row) for row in sorted_rows[:3])
        if total_spend and (top_three_spend / total_spend) >= 0.8:
            findings.append(
                build_finding(
                    "budget_concentration",
                    "Spend is highly concentrated in a small number of child entities.",
                    severity="medium",
                    confidence=0.8,
                )
            )

    if not findings:
        findings.append(
            build_finding(
                "insufficient_data",
                "No strong optimization signal was detected from the selected metrics.",
                severity="low",
                confidence=0.4,
            )
        )

    return findings


def previous_window(
    since: date,
    until: date,
) -> tuple[date, date]:
    """Return the immediately preceding date window."""
    window_days = (until - since).days + 1
    previous_until = since - timedelta(days=1)
    previous_since = previous_until - timedelta(days=window_days - 1)
    return previous_since, previous_until
