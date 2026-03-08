"""Optimization snapshot tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp.tools import diagnostics


def test_account_snapshot_ranks_children(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {
            "items": [],
            "summary": {
                "metrics": {
                    "spend": 300.0,
                    "ctr": 0.01,
                    "frequency": 3.1,
                    "conversions": 0.0,
                    "roas": 0.7,
                }
            },
        }

    async def fake_child_insights(*args, **kwargs):
        return [
            {"campaign_id": "1", "spend": 200.0, "roas": 0.5},
            {"campaign_id": "2", "spend": 50.0, "roas": 2.5},
            {"campaign_id": "3", "spend": 50.0, "roas": 1.2},
        ]

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_account_optimization_snapshot(account_id="act_123"))
    assert result["top_spend_drivers"][0]["campaign_id"] == "1"
    assert any(finding["type"] == "high_spend_low_conversion" for finding in result["findings"])


def test_campaign_snapshot_includes_top_adsets_and_ads(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {"summary": {"metrics": {"spend": 200.0, "roas": 1.4, "ctr": 0.02, "conversions": 3.0}}}

    async def fake_child_insights(object_id: str, *, level: str, **kwargs):
        if level == "adset":
            return [
                {"adset_id": "a1", "metrics": {"spend": 120.0, "roas": 0.9}},
                {"adset_id": "a2", "metrics": {"spend": 80.0, "roas": 2.1}},
            ]
        return [
            {"ad_id": "ad1", "metrics": {"spend": 100.0, "roas": 0.7}},
            {"ad_id": "ad2", "metrics": {"spend": 50.0, "roas": 3.0}},
        ]

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_campaign_optimization_snapshot(campaign_id="cmp_123"))
    assert result["top_adsets_by_spend"][0]["adset_id"] == "a1"
    assert result["top_ads_by_roas"][0]["ad_id"] == "ad2"


def test_budget_pacing_report_handles_no_rows(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {"items": [], "summary": {"metrics": {}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(diagnostics.get_budget_pacing_report(level="campaign", object_id="cmp_123"))
    assert result["daily_rows"] == []
    assert result["trend_summary"]["days"] == 0
    assert result["trend_summary"]["first_day_spend"] is None


def test_creative_performance_report_ranks_mixed_roas(monkeypatch) -> None:
    async def fake_child_insights(*args, **kwargs):
        return [
            {"ad_id": "ad1", "metrics": {"spend": 100.0, "roas": 0.8}},
            {"ad_id": "ad2", "metrics": {"spend": 50.0, "roas": 3.1}},
            {"ad_id": "ad3", "metrics": {"spend": 25.0, "roas": 1.5}},
        ]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_creative_performance_report(account_id="act_123", top_n=2))
    assert result["top_creatives"][0]["ad_id"] == "ad2"
    assert result["worst_creatives"][0]["ad_id"] == "ad1"


def test_creative_fatigue_report_detects_declining_ctr_with_rising_frequency(monkeypatch) -> None:
    async def fake_child_insights(object_id: str, *, since: str | None = None, **kwargs):
        if since == "2026-03-01":
            return [{"ad_id": "ad1", "metrics": {"ctr": 0.01, "frequency": 3.0}}]
        return [{"ad_id": "ad1", "metrics": {"ctr": 0.03, "frequency": 2.0}}]

    class FixedDate(diagnostics.date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 8)

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    monkeypatch.setattr(diagnostics, "date", FixedDate)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id="cmp_123"))
    assert any(finding["type"] == "creative_fatigue_risk" for finding in result["findings"])


def test_creative_fatigue_report_returns_insufficient_data_when_no_signal(monkeypatch) -> None:
    async def fake_child_insights(*args, **kwargs):
        return [{"ad_id": "ad1", "metrics": {"ctr": 0.03, "frequency": 2.0}}]

    class FixedDate(diagnostics.date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 8)

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    monkeypatch.setattr(diagnostics, "date", FixedDate)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id="cmp_123"))
    assert result["findings"][0]["type"] == "insufficient_data"


def test_audience_performance_report_uses_segment_breakdown(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {
            "items": [
                {"country": "US", "metrics": {"roas": 2.1}},
                {"country": "CA", "metrics": {"roas": 0.9}},
            ],
            "summary": {"metrics": {"spend": 100.0, "roas": 1.5}},
        }

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.get_audience_performance_report(level="campaign", object_id="cmp_123", segment_by="country")
    )
    assert result["segment_by"] == "country"
    assert result["top_segments"][0]["country"] == "US"


def test_delivery_risk_report_includes_metric_evidence(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {"summary": {"metrics": {"frequency": 3.0, "ctr": 0.005, "roas": 0.8}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(diagnostics.get_delivery_risk_report(campaign_id="cmp_123"))
    assert result["evidence"][0]["metric"] == "frequency"
    assert any(finding["type"] == "low_roas" for finding in result["findings"])


def test_learning_phase_report_returns_missing_signal_note(monkeypatch) -> None:
    class FakeClient:
        async def get_object(self, object_id: str, *, fields=None, params=None):
            return {"id": object_id, "name": "Ad Set", "status": "ACTIVE"}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FakeClient())
    result = asyncio.run(diagnostics.get_learning_phase_report(adset_id="adset_123"))
    assert result["scope"]["level"] == "adset"
    assert result["missing_signals"]
    assert result["item"]["id"] == "adset_123"
