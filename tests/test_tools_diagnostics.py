"""Optimization snapshot tests."""

from __future__ import annotations

import asyncio
import pytest

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
    assert result["evidence"]
    assert "spend_share" in result["top_spend_drivers"][0]["metrics"]


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
    assert result["evidence"]
    assert "spend_share" in result["top_adsets_by_spend"][0]["metrics"]


def test_account_snapshot_supports_explicit_since_until(monkeypatch) -> None:
    entity_calls: list[dict[str, object]] = []
    child_calls: list[dict[str, object]] = []

    async def fake_get_entity_insights(**kwargs):
        entity_calls.append(kwargs)
        return {"items": [], "summary": {"metrics": {"spend": 200.0, "clicks": 20, "impressions": 1000}}}

    async def fake_child_insights(object_id: str, *, level: str, **kwargs):
        child_calls.append({"object_id": object_id, "level": level, **kwargs})
        return []

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    asyncio.run(
        diagnostics.get_account_optimization_snapshot(
            account_id="act_123",
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert entity_calls[0]["since"] == "2026-03-01"
    assert entity_calls[0]["until"] == "2026-03-07"
    assert entity_calls[0]["date_preset"] is None
    assert child_calls[0]["since"] == "2026-03-01"
    assert child_calls[0]["until"] == "2026-03-07"
    assert child_calls[0]["date_preset"] is None


def test_account_snapshot_normalizes_numeric_account_id(monkeypatch) -> None:
    entity_calls: list[dict[str, object]] = []
    child_calls: list[dict[str, object]] = []

    async def fake_get_entity_insights(**kwargs):
        entity_calls.append(kwargs)
        return {"items": [], "summary": {"metrics": {}}}

    async def fake_child_insights(object_id: str, *, level: str, **kwargs):
        child_calls.append({"object_id": object_id, "level": level, **kwargs})
        return []

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_account_optimization_snapshot(account_id="123"))
    assert entity_calls[0]["object_id"] == "act_123"
    assert child_calls[0]["object_id"] == "act_123"
    assert result["scope"]["object_id"] == "act_123"


def test_budget_pacing_report_handles_no_rows(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {"items": [], "summary": {"metrics": {}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(diagnostics.get_budget_pacing_report(level="campaign", object_id="cmp_123"))
    assert result["daily_rows"] == []
    assert result["trend_summary"]["days"] == 0
    assert result["trend_summary"]["first_day_spend"] is None


def test_budget_pacing_report_supports_explicit_since_until(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_get_entity_insights(**kwargs):
        calls.append(kwargs)
        return {"items": [], "summary": {"metrics": {"spend": 100.0, "clicks": 10, "impressions": 1000}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.get_budget_pacing_report(
            level="campaign",
            object_id="cmp_123",
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert calls[0]["since"] == "2026-03-01"
    assert calls[0]["until"] == "2026-03-07"
    assert calls[0]["date_preset"] is None
    assert result["evidence"]


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
    assert "spend_share" in result["top_creatives"][0]["metrics"]


def test_creative_performance_report_accepts_level_and_object_id(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_child_insights(object_id: str, *, level: str, **kwargs):
        calls.append({"object_id": object_id, "level": level, **kwargs})
        return [{"ad_id": "ad1", "metrics": {"spend": 100.0, "roas": 1.2}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(
        diagnostics.get_creative_performance_report(level="campaign", object_id="cmp_123")
    )
    assert calls[0]["object_id"] == "cmp_123"
    assert calls[0]["level"] == "ad"
    assert result["scope"]["object_id"] == "cmp_123"


def test_creative_performance_report_rejects_conflicting_scope_inputs(monkeypatch) -> None:
    with pytest.raises(diagnostics.ValidationError):
        asyncio.run(
            diagnostics.get_creative_performance_report(
                level="campaign",
                object_id="cmp_123",
                adset_id="adset_123",
            )
        )


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
    assert result["findings"][0]["evidence"]


def test_creative_fatigue_report_accepts_level_and_object_id(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_child_insights(object_id: str, *, since: str | None = None, **kwargs):
        calls.append(object_id)
        if since == "2026-03-01":
            return [{"ad_id": "ad1", "metrics": {"ctr": 0.01, "frequency": 3.0}}]
        return [{"ad_id": "ad1", "metrics": {"ctr": 0.03, "frequency": 2.0}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(
        diagnostics.get_creative_fatigue_report(
            level="campaign",
            object_id="cmp_123",
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert calls == ["cmp_123", "cmp_123"]
    assert result["scope"]["object_id"] == "cmp_123"


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


def test_creative_fatigue_report_supports_explicit_windows(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_child_insights(object_id: str, *, since: str | None = None, until: str | None = None, **kwargs):
        calls.append((since or "", until or ""))
        return []

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(
        diagnostics.get_creative_fatigue_report(
            campaign_id="cmp_123",
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert calls == [("2026-03-01", "2026-03-07"), ("2026-02-22", "2026-02-28")]
    assert result["current_window"] == {"since": "2026-03-01", "until": "2026-03-07"}
    assert result["previous_window"] == {"since": "2026-02-22", "until": "2026-02-28"}


def test_creative_fatigue_report_rejects_invalid_explicit_dates() -> None:
    with pytest.raises(diagnostics.ValidationError):
        asyncio.run(
            diagnostics.get_creative_fatigue_report(
                campaign_id="cmp_123",
                since="bad-date",
                until="2026-03-07",
            )
        )


def test_creative_fatigue_report_rejects_reversed_explicit_windows() -> None:
    with pytest.raises(diagnostics.ValidationError):
        asyncio.run(
            diagnostics.get_creative_fatigue_report(
                campaign_id="cmp_123",
                since="2026-03-07",
                until="2026-03-01",
            )
        )


def test_audience_performance_report_uses_segment_breakdown(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {
            "items": [
                {"country": "US", "metrics": {"roas": 2.1, "spend": 60.0, "conversions": 3.0}},
                {"country": "CA", "metrics": {"roas": 0.9, "spend": 40.0, "conversions": 1.0}},
            ],
            "summary": {"metrics": {"spend": 100.0, "roas": 1.5}},
        }

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.get_audience_performance_report(level="campaign", object_id="cmp_123", segment_by="country")
    )
    assert result["segment_by"] == "country"
    assert result["top_segments"][0]["country"] == "US"
    assert "result_share" in result["top_segments"][0]["metrics"]


def test_delivery_risk_report_includes_metric_evidence(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {"summary": {"metrics": {"frequency": 3.0, "ctr": 0.005, "roas": 0.8}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(diagnostics.get_delivery_risk_report(campaign_id="cmp_123"))
    assert any(item["metric"] == "ctr" for item in result["evidence"])
    assert any(finding["type"] == "low_roas" for finding in result["findings"])


def test_delivery_risk_report_accepts_level_and_object_id(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_get_entity_insights(**kwargs):
        calls.append(kwargs)
        return {"summary": {"metrics": {"frequency": 3.0, "ctr": 0.005, "roas": 0.8}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(diagnostics.get_delivery_risk_report(level="adset", object_id="adset_123"))
    assert calls[0]["level"] == "adset"
    assert calls[0]["object_id"] == "adset_123"
    assert result["scope"]["level"] == "adset"


def test_learning_phase_report_returns_missing_signal_note(monkeypatch) -> None:
    class FakeClient:
        async def get_object(self, object_id: str, *, fields=None, params=None):
            return {"id": object_id, "name": "Ad Set", "status": "ACTIVE"}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FakeClient())
    result = asyncio.run(diagnostics.get_learning_phase_report(adset_id="adset_123"))
    assert result["scope"]["level"] == "adset"
    assert result["missing_signals"]
    assert result["item"]["id"] == "adset_123"


def test_learning_phase_report_accepts_level_and_object_id(monkeypatch) -> None:
    class FakeClient:
        async def get_object(self, object_id: str, *, fields=None, params=None):
            return {"id": object_id, "name": "Campaign", "status": "ACTIVE"}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FakeClient())
    result = asyncio.run(diagnostics.get_learning_phase_report(level="campaign", object_id="cmp_123"))
    assert result["scope"]["level"] == "campaign"
    assert result["item"]["id"] == "cmp_123"
