"""Optimization snapshot tests."""

from __future__ import annotations

import asyncio
import pytest

from meta_ads_mcp.tools import diagnostics


def test_child_insights_paginates_before_returning_rows(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class PagingInsightsClient:
        async def get_insights(self, object_id: str, *, fields, params):
            calls.append(dict(params))
            if "after" not in params:
                return {
                    "data": [{"campaign_id": "cmp_1", "spend": "100"}],
                    "paging": {"cursors": {"after": "cursor_2"}, "next": "next"},
                }
            assert params["after"] == "cursor_2"
            return {
                "data": [{"campaign_id": "cmp_2", "spend": "200"}],
                "paging": {"cursors": {"after": None}},
            }

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: PagingInsightsClient())
    rows = asyncio.run(diagnostics._child_insights("act_123", level="campaign"))

    assert [row["campaign_id"] for row in rows] == ["cmp_1", "cmp_2"]
    assert [row["metrics"]["spend"] for row in rows] == [100.0, 200.0]
    assert len(calls) == 2


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


def test_account_health_snapshot_compares_explicit_windows(monkeypatch) -> None:
    calls: list[tuple[str | None, str | None]] = []

    async def fake_get_entity_insights(*, since: str | None = None, until: str | None = None, **kwargs):
        calls.append((since, until))
        spend = 300.0 if since == "2026-03-01" else 200.0
        return {"summary": {"metrics": {"spend": spend, "clicks": 10, "impressions": 1000}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.get_account_health_snapshot(
            account_id="123",
            since="2026-03-01",
            until="2026-03-31",
        )
    )
    assert result["scope"]["object_id"] == "act_123"
    assert result["current_window"] == {"date_preset": None, "since": "2026-03-01", "until": "2026-03-31"}
    assert result["comparisons"]["previous"]["comparison"]["spend"]["delta"] == 100.0
    assert calls[1] == ("2026-02-01", "2026-02-28")
    assert calls[2] == ("2025-03-01", "2025-03-31")


def test_account_health_snapshot_uses_equal_length_previous_window_for_multi_month_ranges(monkeypatch) -> None:
    calls: list[tuple[str | None, str | None]] = []

    async def fake_get_entity_insights(*, since: str | None = None, until: str | None = None, **kwargs):
        calls.append((since, until))
        return {"summary": {"metrics": {"spend": 300.0, "clicks": 10, "impressions": 1000}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.get_account_health_snapshot(
            account_id="123",
            since="2026-01-01",
            until="2026-03-31",
            include_year_over_year=False,
        )
    )

    assert result["previous_window"] == {"since": "2025-10-03", "until": "2025-12-31"}
    assert calls[1] == ("2025-10-03", "2025-12-31")


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
    assert result["daily_row_detail"] == "compact"
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


def test_budget_pacing_report_compacts_daily_rows_by_default(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {
            "items": [
                {
                    "date_start": "2026-03-01",
                    "date_stop": "2026-03-01",
                    "spend": 100.0,
                    "actions": [{"action_type": "purchase", "value": "2"}],
                    "action_values": [{"action_type": "purchase", "value": "250"}],
                    "actions_map": {"purchase": 2.0},
                    "action_values_map": {"purchase": 250.0},
                    "metrics": {"spend": 100.0, "roas": 2.5},
                }
            ],
            "summary": {"metrics": {"spend": 100.0, "roas": 2.5}},
        }

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(diagnostics.get_budget_pacing_report(level="campaign", object_id="cmp_123"))
    assert result["daily_row_detail"] == "compact"
    assert result["daily_rows"] == [
        {
            "date_start": "2026-03-01",
            "date_stop": "2026-03-01",
            "metrics": {"spend": 100.0, "roas": 2.5},
        }
    ]


def test_budget_pacing_report_can_return_full_daily_rows(monkeypatch) -> None:
    row = {
        "date_start": "2026-03-01",
        "date_stop": "2026-03-01",
        "spend": 100.0,
        "actions_map": {"purchase": 2.0},
        "metrics": {"spend": 100.0, "roas": 2.5},
    }

    async def fake_get_entity_insights(**kwargs):
        return {"items": [row], "summary": {"metrics": {"spend": 100.0, "roas": 2.5}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.get_budget_pacing_report(
            level="campaign",
            object_id="cmp_123",
            include_full_daily_rows=True,
        )
    )
    assert result["daily_row_detail"] == "full"
    assert result["daily_rows"] == [row]


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
    assert "quality_ranking" in calls[0]["fields"]
    assert "actions" in calls[0]["fields"]
    assert "action_values" in calls[0]["fields"]
    assert result["scope"]["object_id"] == "cmp_123"


def test_get_ad_feedback_signals_returns_guidance_without_scope() -> None:
    result = asyncio.run(diagnostics.get_ad_feedback_signals())
    assert result["scope"] == {"level": None, "object_id": None}
    assert result["metrics"] == {}
    assert "quality_ranking" in result["available_signals"]
    assert "raw Facebook or Instagram comments" in result["available_signals"][0]
    assert result["unavailable_signals"]
    assert "list_ad_comments" in result["recommended_tools"][0]


def test_get_ad_feedback_signals_flags_weak_quality(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_child_insights(*args, **kwargs):
        calls.append(kwargs)
        return [
            {
                "ad_id": "ad1",
                "ad_name": "Ad One",
                "quality_ranking": "BELOW_AVERAGE_20",
                "metrics": {"spend": 100.0, "impressions": 1000},
            }
        ]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(
        diagnostics.get_ad_feedback_signals(campaign_id="cmp_123", since="2026-03-01", until="2026-03-07")
    )
    assert result["findings"][0]["type"] == "weak_ad_quality_ranking"
    assert result["weak_quality_ads"][0]["ad_id"] == "ad1"
    assert result["window"] == {"date_preset": None, "since": "2026-03-01", "until": "2026-03-07"}
    assert calls[0]["date_preset"] is None
    assert result["missing_signals"]


def test_get_ad_feedback_signals_rejects_conflicting_ad_scope_inputs() -> None:
    with pytest.raises(diagnostics.ValidationError):
        asyncio.run(diagnostics.get_ad_feedback_signals(ad_id="ad_1", campaign_id="cmp_123"))


def test_get_ad_feedback_signals_rejects_multiple_entity_scopes() -> None:
    with pytest.raises(diagnostics.ValidationError):
        asyncio.run(diagnostics.get_ad_feedback_signals(campaign_id="cmp_123", adset_id="adset_123"))


def test_get_ad_feedback_signals_ignores_blank_alias_scope(monkeypatch) -> None:
    async def fake_child_insights(*args, **kwargs):
        return [{"ad_id": "ad1", "metrics": {"spend": 100.0}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_ad_feedback_signals(campaign_id="cmp_123", account_id="  "))
    assert result["scope"] == {"level": "campaign", "object_id": "cmp_123"}


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


def test_learning_phase_report_uses_level_specific_fields(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        async def get_object(self, object_id: str, *, fields=None, params=None):
            calls.append({"object_id": object_id, "fields": fields})
            return {"id": object_id, "name": "Campaign", "status": "ACTIVE"}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FakeClient())
    result = asyncio.run(diagnostics.get_learning_phase_report(level="campaign", object_id="cmp_123"))
    assert "optimization_goal" not in calls[0]["fields"]
    assert "objective" in calls[0]["fields"]
    assert any("optimization_goal is available on ad sets" in item for item in result["missing_signals"])


def test_detect_auction_overlap_flags_shared_platform(monkeypatch) -> None:
    insight_calls: list[dict[str, object]] = []

    class FakeClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            assert parent_id == "act_123"
            assert edge == "campaigns"
            return {
                "data": [
                    {"id": "cmp_1", "name": "Campaign One"},
                    {"id": "cmp_2", "name": "Campaign Two"},
                ]
            }

    async def fake_get_entity_insights(*, object_id: str, **kwargs):
        insight_calls.append(kwargs)
        return {
            "items": [
                {
                    "publisher_platform": "facebook",
                    "reach": 1000,
                    "metrics": {"spend": 50.0 if object_id == "cmp_1" else 25.0, "frequency": 1.5, "cpm": 10.0},
                }
            ],
            "summary": {"metrics": {"spend": 50.0 if object_id == "cmp_1" else 25.0}},
        }

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FakeClient())
    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.detect_auction_overlap(account_id="123", since="2026-03-01", until="2026-03-07")
    )
    assert result["findings"][0]["type"] == "potential_auction_overlap"
    assert "facebook" in result["overlap_platforms"]
    assert result["window"] == {"date_preset": None, "since": "2026-03-01", "until": "2026-03-07"}
    assert insight_calls[0]["date_preset"] is None
    assert "publisher_platform" not in insight_calls[0]["fields"]
    assert "actions" not in insight_calls[0]["fields"]
    assert "cost_per_action_type" not in insight_calls[0]["fields"]


def test_detect_auction_overlap_deduplicates_campaign_ids(monkeypatch) -> None:
    insight_object_ids: list[str] = []

    async def fake_get_entity_insights(*, object_id: str, **kwargs):
        insight_object_ids.append(object_id)
        return {
            "items": [
                {
                    "publisher_platform": "facebook",
                    "reach": 1000,
                    "metrics": {"spend": 50.0, "frequency": 1.5, "cpm": 10.0},
                }
            ],
            "summary": {"metrics": {"spend": 50.0}},
        }

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.detect_auction_overlap(account_id="123", campaign_ids=["cmp_1", "cmp_1"])
    )
    assert insight_object_ids == ["cmp_1"]
    assert result["campaign_count"] == 1
    assert result["findings"][0]["type"] == "no_platform_overlap_detected"
    assert result["overlap_platforms"] == {}
