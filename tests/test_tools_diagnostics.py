"""Optimization snapshot tests."""

from __future__ import annotations

import asyncio
import json
import pytest

from meta_ads_mcp.config import reload_settings
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


def test_child_insights_respects_max_rows_cap(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class PagingInsightsClient:
        async def get_insights(self, object_id: str, *, fields, params):
            calls.append(dict(params))
            return {
                "data": [{"campaign_id": f"cmp_{len(calls)}", "spend": "100"}],
                "paging": {"cursors": {"after": f"cursor_{len(calls) + 1}"}, "next": "next"},
            }

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: PagingInsightsClient())
    with pytest.raises(diagnostics.ValidationError, match="No partial diagnostic"):
        asyncio.run(diagnostics._child_insights("act_123", level="campaign", limit=2, max_rows=2))

    assert [call["limit"] for call in calls] == [2, 1]
    assert len(calls) == 2


@pytest.mark.parametrize("cursor", [None, "repeated"])
def test_child_insights_rejects_unusable_pagination(monkeypatch, cursor) -> None:
    class BrokenPagingClient:
        async def get_insights(self, *args, **kwargs):
            return {"data": [{"ad_id": "ad1"}],
                    "paging": {"next": "next", "cursors": {"after": cursor}}}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: BrokenPagingClient())
    with pytest.raises(diagnostics.ValidationError, match="missing or repeated"):
        asyncio.run(diagnostics._child_insights("act_123", level="ad"))


def test_child_insights_allows_complete_report_exactly_at_cap(monkeypatch) -> None:
    class CompleteClient:
        async def get_insights(self, *args, **kwargs):
            return {"data": [{"ad_id": "ad1"}, {"ad_id": "ad2"}]}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: CompleteClient())
    rows = asyncio.run(diagnostics._child_insights("act_123", level="ad", max_rows=2))
    assert [row["ad_id"] for row in rows] == ["ad1", "ad2"]


def test_account_snapshot_ranks_children(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {
            "items": [{"metrics": {"spend": 300.0, "ctr": 0.01, "frequency": 3.1, "conversions": 0.0, "roas": 0.7}}],
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
        metrics = {"spend": 200.0, "roas": 1.4, "ctr": 0.02, "conversions": 3.0}
        return {"items": [{"metrics": metrics}], "summary": {"metrics": metrics}}

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


def test_native_optimization_signals_require_v26_before_client_creation(
    monkeypatch,
) -> None:
    monkeypatch.setenv("META_API_VERSION", "v25.0")
    reload_settings()
    monkeypatch.setattr(
        diagnostics,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )

    with pytest.raises(diagnostics.ValidationError, match="META_API_VERSION=v26.0"):
        asyncio.run(
            diagnostics.get_native_optimization_signals(
                level="campaign",
                object_id="cmp_123",
            )
        )


def test_campaign_snapshot_rejects_native_signals_before_creating_reads(
    monkeypatch,
) -> None:
    monkeypatch.setenv("META_API_VERSION", "v25.0")
    reload_settings()

    def unexpected_read(*args, **kwargs):
        raise AssertionError("insights reads should not be created before v26 validation")

    monkeypatch.setattr(diagnostics, "get_entity_insights", unexpected_read)
    monkeypatch.setattr(diagnostics, "_child_insights", unexpected_read)

    with pytest.raises(diagnostics.ValidationError, match="META_API_VERSION=v26.0"):
        asyncio.run(
            diagnostics.get_campaign_optimization_snapshot(
                campaign_id="cmp_123",
                include_native_signals=True,
            )
        )


@pytest.mark.parametrize(
    ("level", "object_id"),
    [("campaign", "cmp_123"), ("adset", "adset_123"), ("ad", "ad_123")],
)
def test_native_optimization_signals_request_exact_level_fields(
    monkeypatch,
    level: str,
    object_id: str,
) -> None:
    calls: list[tuple[str, list[str]]] = []

    class NativeSignalClient:
        async def get_object(self, requested_id: str, *, fields=None, params=None):
            calls.append((requested_id, fields))
            return {
                "id": requested_id,
                "account_id": "123",
                fields[2]: {"status": "available"},
            }

    async def fake_currency(_client, account_id: str) -> str:
        assert account_id == "act_123"
        return "USD"

    monkeypatch.setenv("META_API_VERSION", "v26.0")
    reload_settings()
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: NativeSignalClient())
    monkeypatch.setattr(diagnostics, "resolve_account_currency", fake_currency)

    result = asyncio.run(
        diagnostics.get_native_optimization_signals(
            level=level,
            object_id=object_id,
        )
    )

    expected = diagnostics.NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL[level]
    assert calls == [(object_id, ["id", "account_id", *expected])]
    assert result["scope"] == {"level": level, "object_id": object_id}
    assert result["available_signals"] == [expected[0]]
    assert result["summary"]["api_version"] == "v26.0"
    assert result["summary"]["returned"] == 1


def test_native_optimization_signals_normalize_budget_remaining(
    monkeypatch,
) -> None:
    class BudgetSignalClient:
        async def get_object(self, object_id: str, *, fields=None, params=None):
            return {
                "id": object_id,
                "account_id": "123",
                "budget_remaining": "1250",
            }

    async def fake_currency(_client, _account_id: str) -> str:
        return "USD"

    monkeypatch.setenv("META_API_VERSION", "v26.0")
    reload_settings()
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: BudgetSignalClient())
    monkeypatch.setattr(diagnostics, "resolve_account_currency", fake_currency)

    result = asyncio.run(
        diagnostics.get_native_optimization_signals(
            level="campaign",
            object_id="cmp_123",
        )
    )

    assert result["signals"]["budget_remaining"] == 12.5
    assert result["summary"]["currency"] == "USD"


def test_campaign_snapshot_can_include_native_signals(monkeypatch) -> None:
    async def fake_get_entity_insights(**kwargs):
        return {"summary": {"metrics": {"spend": 100.0, "roas": 1.5}}}

    async def fake_child_insights(*args, **kwargs):
        return []

    async def fake_native_payload(level: str, object_id: str):
        return {
            "scope": {"level": level, "object_id": object_id},
            "signals": {"pacing_type": ["standard"]},
        }

    monkeypatch.setenv("META_API_VERSION", "v26.0")
    reload_settings()
    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    monkeypatch.setattr(diagnostics, "_native_optimization_payload", fake_native_payload)

    result = asyncio.run(
        diagnostics.get_campaign_optimization_snapshot(
            campaign_id="cmp_123",
            include_native_signals=True,
        )
    )

    assert result["native_optimization_signals"]["signals"] == {
        "pacing_type": ["standard"]
    }


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("get_account_health_snapshot", {"account_id": "123"}),
    ("get_account_optimization_snapshot", {"account_id": "123"}),
    ("get_campaign_optimization_snapshot", {"campaign_id": "123"}),
    ("get_budget_pacing_report", {"level": "campaign", "object_id": "123"}),
    ("get_creative_performance_report", {"account_id": "123"}),
    ("get_delivery_risk_report", {"campaign_id": "123"}),
    ("get_audience_performance_report", {"level": "campaign", "object_id": "123"}),
])
@pytest.mark.parametrize("has_rows", [False, True])
def test_snapshot_consumers_distinguish_empty_reports_from_real_zero_rows(
    monkeypatch, tool_name, arguments, has_rows,
) -> None:
    from meta_ads_mcp.tools import insights

    class ZeroClient:
        async def get_insights(self, object_id, *, fields=None, params=None):
            return {"data": [{
                "ad_id": "456", "spend": "0", "impressions": "0", "clicks": "0",
                "actions": [{"action_type": "purchase", "value": "0"}],
            }] if has_rows else []}

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: ZeroClient())
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: ZeroClient())
    result = asyncio.run(getattr(diagnostics, tool_name)(**arguments))

    assert result["findings"][0]["type"] == ("no_pattern_detected" if has_rows else "insufficient_data")
    assert result["metrics"]["spend"] == 0
    if not has_rows:
        assert result["evidence"] == []


@pytest.mark.parametrize(("tool_name", "arguments", "parent_level"), [
    ("get_account_optimization_snapshot", {"account_id": "123"}, "account"),
    ("get_campaign_optimization_snapshot", {"campaign_id": "123"}, "campaign"),
])
@pytest.mark.parametrize("child_data", ["zero", "unknown", "empty"])
def test_snapshot_empty_parent_preserves_known_zero_child_data(
    monkeypatch, tool_name, arguments, parent_level, child_data,
) -> None:
    from meta_ads_mcp.tools import insights

    class EmptyParentClient:
        async def get_insights(self, object_id, *, fields=None, params=None):
            if params["level"] == parent_level or child_data == "empty":
                return {"data": []}
            row = {f"{params['level']}_id": "456"}
            if child_data == "zero":
                row["spend"] = "0"
            return {"data": [row]}

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: EmptyParentClient())
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: EmptyParentClient())
    result = asyncio.run(getattr(diagnostics, tool_name)(**arguments))

    expected = "no_pattern_detected" if child_data == "zero" else "insufficient_data"
    assert [finding["type"] for finding in result["findings"]] == [expected]
    assert result["evidence"] == []  # Empty parent totals never become observed evidence.


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


@pytest.mark.parametrize("date_preset", [None, " "])
def test_account_health_snapshot_treats_blank_inputs_as_default_window(monkeypatch, date_preset) -> None:
    calls: list[dict[str, object]] = []

    async def fake_get_entity_insights(**kwargs):
        calls.append(kwargs)
        return {"summary": {"metrics": {"spend": 300.0, "clicks": 10, "impressions": 1000}}}

    class FixedDate(diagnostics.date):
        @classmethod
        def today(cls):
            return cls(2026, 4, 1)

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    monkeypatch.setattr(diagnostics, "date", FixedDate)
    result = asyncio.run(
        diagnostics.get_account_health_snapshot(
            account_id="123",
            date_preset=date_preset,
            since=" ",
            until=" ",
        )
    )

    assert len(calls) == 3
    assert calls[0]["date_preset"] == "last_30d"
    assert calls[0]["since"] is None
    assert calls[0]["until"] is None
    assert calls[1]["date_preset"] is None
    assert calls[1]["since"] == "2026-01-31"
    assert calls[1]["until"] == "2026-03-01"
    assert calls[2]["date_preset"] is None
    assert calls[2]["since"] == "2025-03-02"
    assert calls[2]["until"] == "2025-03-31"
    assert result["current_window"] == {"date_preset": "last_30d", "since": None, "until": None}
    assert result["previous_window"] == {"since": "2026-01-31", "until": "2026-03-01"}
    assert result["year_over_year_window"] == {"since": "2025-03-02", "until": "2025-03-31"}
    assert "comparisons" in result


def test_account_health_snapshot_can_skip_default_comparisons(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_get_entity_insights(**kwargs):
        calls.append(kwargs)
        return {"summary": {"metrics": {"spend": 300.0, "clicks": 10, "impressions": 1000}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.get_account_health_snapshot(
            account_id="123",
            include_previous=False,
            include_year_over_year=False,
        )
    )

    assert len(calls) == 1
    assert calls[0]["date_preset"] == "last_30d"
    assert result["current_window"] == {"date_preset": "last_30d", "since": None, "until": None}
    assert "comparisons" not in result


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
        metrics = {"spend": 100.0, "clicks": 10, "impressions": 1000}
        return {"items": [{"metrics": metrics}], "summary": {"metrics": metrics}}

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
    assert result["scope"] == {"level": "campaign", "object_id": "cmp_123"}
    assert result["analyzed_level"] == "ad"


def test_get_ad_feedback_signals_returns_guidance_without_scope() -> None:
    result = asyncio.run(diagnostics.get_ad_feedback_signals())
    assert result["scope"] == {"level": None, "object_id": None}
    assert result["metrics"] == {}
    assert "quality_ranking" in result["available_signals"]
    assert "raw Facebook or Instagram comments" in result["available_signals"][0]
    assert result["unavailable_signals"]
    assert "list_ad_comments" in result["recommended_tools"][0]


def test_get_ad_feedback_signals_treats_blank_scope_args_as_omitted() -> None:
    result = asyncio.run(diagnostics.get_ad_feedback_signals(level=" ", account_id=" ", adset_id=" "))
    assert result["scope"] == {"level": None, "object_id": None}
    assert result["recommended_tools"]


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


@pytest.mark.parametrize("date_preset", [None, " "])
def test_get_ad_feedback_signals_reports_effective_default_window(monkeypatch, date_preset) -> None:
    calls: list[dict[str, object]] = []

    async def fake_child_insights(*args, **kwargs):
        calls.append(kwargs)
        return [{"ad_id": "ad1", "metrics": {"spend": 100.0}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_ad_feedback_signals(campaign_id="cmp_123", date_preset=date_preset))

    assert calls[0]["date_preset"] == "last_30d"
    assert result["window"] == {"date_preset": "last_30d", "since": None, "until": None}


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


def test_get_ad_feedback_signals_ignores_blank_ad_id(monkeypatch) -> None:
    async def fake_child_insights(*args, **kwargs):
        return [{"ad_id": "ad1", "metrics": {"spend": 100.0}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_ad_feedback_signals(ad_id=" ", campaign_id="cmp_123"))
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
            return [{"ad_id": "ad1", "impressions": 1000, "metrics": {"ctr": 0.01, "frequency": 3.0}}]
        return [{"ad_id": "ad1", "impressions": 1000, "metrics": {"ctr": 0.03, "frequency": 2.0}}]

    class FixedDate(diagnostics.date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 8)

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    monkeypatch.setattr(diagnostics, "date", FixedDate)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id="cmp_123", max_creative_lookups=0))
    assert any(finding["type"] == "creative_fatigue_risk" for finding in result["findings"])
    assert result["findings"][0]["evidence"]


@pytest.mark.parametrize(
    ("previous_ctr", "current_ctr", "flagged"),
    [(1.2, 0.8, True), (0.6, 1.05, False), (0.8, 0.4, True)],
)
def test_creative_fatigue_report_handles_small_ctr_rates(monkeypatch, previous_ctr, current_ctr, flagged) -> None:
    class FatigueInsightsClient:
        async def get_object(self, object_id, *, fields):
            return {"id": object_id, "creative": {"id": "crt_1"}}

        async def get_insights(self, object_id: str, *, fields, params):
            assert object_id == "cmp_123"
            current = json.loads(params["time_range"])["since"] == "2026-03-01"
            return {
                "data": [{
                    "ad_id": "ad1",
                    "ctr": str(current_ctr if current else previous_ctr),
                    "clicks": str(round((current_ctr if current else previous_ctr) * 100)),
                    "impressions": "10000",
                    "frequency": "3.0" if current else "2.0",
                }],
            }

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FatigueInsightsClient())
    result = asyncio.run(
        diagnostics.get_creative_fatigue_report(
            campaign_id="cmp_123",
            since="2026-03-01",
            until="2026-03-07",
            previous_since="2026-02-22",
            previous_until="2026-02-28",
        )
    )

    finding = result["findings"][0]
    if not flagged:
        assert finding["type"] == "no_pattern_detected"
        return
    assert finding["type"] == "creative_fatigue_risk"
    assert finding["affected_entities"] == [{
        "ad_id": "ad1", "creative_id": "crt_1", "creative_resolution_status": "resolved",
    }]
    ctr_evidence = next(item for item in finding["evidence"] if item["metric"] == "ctr")
    assert ctr_evidence["value"] == pytest.approx(current_ctr / 100)


@pytest.mark.parametrize("scope", [{"account_id": "123"}, {"level": "account", "object_id": "123"}])
def test_account_fatigue_matches_campaign_sweep_and_includes_names(monkeypatch, scope) -> None:
    calls = []
    creative_calls = []

    class AccountFatigueClient:
        async def get_object(self, object_id, *, fields):
            assert fields == ["id", "name", "creative{id}"]
            creative_calls.append(object_id)
            return {"id": object_id, "creative": {"id": f"crt_{object_id}"}}

        async def get_insights(self, object_id, *, fields, params):
            calls.append(object_id)
            assert params["level"] == "ad"
            current = json.loads(params["time_range"])["since"] == "2026-03-01"
            indices = range(22) if object_id == "act_123" else [int(object_id.removeprefix("cmp_"))]
            return {"data": [{
                "ad_id": f"ad_{index}", "ad_name": f"Ad {index}",
                "campaign_id": f"cmp_{index}", "campaign_name": f"Campaign {index}",
                "adset_id": f"set_{index}", "adset_name": f"Ad set {index}",
                "spend": str(index + 1), "impressions": "1000",
                "clicks": "8" if current else "12", "ctr": "0.8" if current else "1.2",
                "frequency": "3" if current else "2",
            } for index in indices]}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: AccountFatigueClient())
    window = {"since": "2026-03-01", "until": "2026-03-07"}
    result = asyncio.run(diagnostics.get_creative_fatigue_report(**scope, **window))
    assert calls == ["act_123", "act_123"]
    assert len(creative_calls) == 22  # Only flagged ads need current creative-ID reads.
    assert result["scope"] == {"level": "account", "object_id": "act_123"}
    assert result["complete"] is True
    assert result["comparison_count"] == 22
    assert result["ranked_by"] == "evidence_strength"
    entities = [finding["affected_entities"][0] for finding in result["findings"]]
    assert entities[0] == {
        "ad_id": "ad_21", "ad_name": "Ad 21", "campaign_id": "cmp_21",
        "campaign_name": "Campaign 21", "adset_id": "set_21", "adset_name": "Ad set 21",
        "creative_id": "crt_ad_21", "creative_resolution_status": "resolved",
    }
    sweep_ids = set()
    for index in range(22):
        campaign = asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id=f"cmp_{index}", **window))
        sweep_ids.update(finding["affected_entities"][0]["ad_id"] for finding in campaign["findings"])
    assert {entity["ad_id"] for entity in entities} == sweep_ids


def test_creative_fatigue_report_accepts_level_and_object_id(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_child_insights(object_id: str, *, since: str | None = None, **kwargs):
        calls.append(object_id)
        if since == "2026-03-01":
            return [{"ad_id": "ad1", "impressions": 1000, "metrics": {"ctr": 0.01, "frequency": 3.0}}]
        return [{"ad_id": "ad1", "impressions": 1000, "metrics": {"ctr": 0.03, "frequency": 2.0}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(
        diagnostics.get_creative_fatigue_report(
            level="campaign",
            object_id="cmp_123",
            since="2026-03-01",
            until="2026-03-07",
            max_creative_lookups=0,
        )
    )
    assert calls == ["cmp_123", "cmp_123"]
    assert result["scope"] == {"level": "campaign", "object_id": "cmp_123"}
    assert result["analyzed_level"] == "ad"


def test_creative_fatigue_report_returns_no_pattern_when_no_signal(monkeypatch) -> None:
    async def fake_child_insights(*args, **kwargs):
        return [{"ad_id": "ad1", "impressions": 1000, "metrics": {"ctr": 0.03, "frequency": 2.0}}]

    class FixedDate(diagnostics.date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 8)

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    monkeypatch.setattr(diagnostics, "date", FixedDate)
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: pytest.fail("no flags need metadata"))
    result = asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id="cmp_123"))
    assert result["findings"][0]["type"] == "no_pattern_detected"
    assert result["comparison_count"] == 1
    assert result["creative_lookups_attempted"] == 0
    assert result["creative_groups"] == []
    assert result["findings"][0]["confidence_status"] == "uncalibrated"


@pytest.mark.parametrize("rows", [[], [{"ad_id": "ad1", "metrics": {"ctr": None, "frequency": 2.0}}]])
def test_fatigue_keeps_insufficient_data_for_unusable_comparisons(monkeypatch, rows) -> None:
    async def fake_child_insights(*args, **kwargs):
        return rows

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id="cmp_123"))

    assert result["findings"][0]["type"] == "insufficient_data"
    assert result["comparison_count"] == 0


@pytest.mark.parametrize("low_window", ["current", "previous"])
def test_fatigue_excludes_low_volume_in_either_window(monkeypatch, low_window) -> None:
    async def fake_child_insights(*args, since=None, **kwargs):
        current = since == "2026-03-01"
        low = current == (low_window == "current")
        return [{"ad_id": "ad1", "impressions": 4 if low else 1000,
                 "metrics": {"ctr": 0.01 if current else 0.03, "frequency": 3 if current else 2}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_123", since="2026-03-01", until="2026-03-07",
    ))

    assert result["findings"][0]["type"] == "insufficient_data"
    assert result["excluded_low_volume_count"] == 1
    assert result["comparison_count"] == 0


def test_fatigue_volume_gate_can_be_disabled_without_claiming_confidence(monkeypatch) -> None:
    async def fake_child_insights(*args, since=None, **kwargs):
        current = since == "2026-03-01"
        return [{"ad_id": "ad1", "impressions": 4,
                 "metrics": {"ctr": 0.25 if current else 0.5, "frequency": 3 if current else 2}}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_123", since="2026-03-01", until="2026-03-07", min_impressions=0,
        max_creative_lookups=0,
    ))

    finding = result["findings"][0]
    assert finding["type"] == "creative_fatigue_risk"
    assert finding["confidence"] is None
    evidence = next(item for item in finding["evidence"] if item["metric"] == "ctr_change")
    assert evidence["value"] == -0.5
    assert evidence["inputs"] == {"current_ctr": 0.25, "previous_ctr": 0.5}


def test_fatigue_rejects_negative_volume_floor_before_fetch(monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: pytest.fail("must not fetch"))
    with pytest.raises(diagnostics.ValidationError, match="min_impressions"):
        asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id="cmp_123", min_impressions=-1))


def _patch_fatigue_rows(monkeypatch, specs) -> None:
    async def fake_child_insights(*args, since=None, **kwargs):
        current = since == "2026-03-01"
        rows = []
        for spec in specs:
            impressions = spec.get("impressions", 1000) if current else spec.get("previous_impressions", spec.get("impressions", 1000))
            ctr = round(0.05 * (1 - spec.get("ctr_decline", 0.5)), 12) if current else 0.05
            rows.append({
                "ad_id": spec["ad_id"],
                **{key: spec[key] for key in ("ad_name", "campaign_id") if key in spec},
                "impressions": impressions,
                "clicks": round(impressions * ctr),
                "metrics": {
                    "ctr": ctr,
                    "frequency": 5 * (1 + spec.get("frequency_increase", 0.5)) if current else 5,
                    "spend": spec.get("spend", 10),
                },
            })
        return rows

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)


def test_fatigue_groups_only_flagged_ads_by_exact_current_creative_id(monkeypatch) -> None:
    _patch_fatigue_rows(monkeypatch, [
        {"ad_id": "ad_a", "ad_name": "same filename", "campaign_id": "cmp_1"},
        {"ad_id": "ad_b", "campaign_id": "cmp_2"},
        {"ad_id": "ad_c", "ad_name": "same filename", "campaign_id": "cmp_3"},
        {"ad_id": "ad_healthy", "ctr_decline": 0},
    ])
    calls = []

    class CreativeClient:
        async def get_object(self, object_id, *, fields):
            calls.append(object_id)
            assert fields == ["id", "name", "creative{id}"]
            return {"id": object_id, "name": f"Metadata {object_id}",
                    "creative": {"id": "crt_shared" if object_id in {"ad_a", "ad_b"} else "crt_other"}}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: CreativeClient())
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        account_id="123", since="2026-03-01", until="2026-03-07",
    ))

    assert calls == ["ad_a", "ad_b", "ad_c"]
    assert result["creative_groups"] == [
        {"creative_id": "crt_shared", "ad_ids": ["ad_a", "ad_b"], "campaign_ids": ["cmp_1", "cmp_2"]},
        {"creative_id": "crt_other", "ad_ids": ["ad_c"], "campaign_ids": ["cmp_3"]},
    ]
    assert result["creative_groups_scope"] == "flagged_ads_only"
    assert result["creative_identity_source"] == "current_ad_metadata"
    assert result["historical_creative_identity_established"] is False
    assert result["creative_identity_complete"] is True
    assert result["creative_lookups_attempted"] == 3
    entities = [finding["affected_entities"][0] for finding in result["findings"]]
    assert entities[0]["ad_name"] == "same filename"
    assert entities[1]["ad_name"] == "Metadata ad_b"


@pytest.mark.parametrize("creative", [None, {}, {"id": " "}, "not-a-creative-object"])
def test_fatigue_preserves_flags_when_creative_id_is_missing(monkeypatch, creative) -> None:
    _patch_fatigue_rows(monkeypatch, [{"ad_id": "ad_1", "ad_name": "not an identity"}])

    class MissingCreativeClient:
        async def get_object(self, object_id, *, fields):
            return {"id": object_id, "creative": creative}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: MissingCreativeClient())
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07",
    ))

    finding = result["findings"][0]
    assert finding["type"] == "creative_fatigue_risk"
    assert finding["affected_entities"][0]["creative_id"] is None
    assert finding["affected_entities"][0]["creative_resolution_status"] == "missing_creative_id"
    assert result["creative_identity_complete"] is False
    assert result["creative_groups"] == []


@pytest.mark.parametrize("error_type", [diagnostics.MetaApiError, diagnostics.NotFoundError, diagnostics.UnsupportedFeatureError])
def test_fatigue_preserves_flags_and_continues_after_known_creative_read_errors(monkeypatch, error_type) -> None:
    _patch_fatigue_rows(monkeypatch, [{"ad_id": "ad_a"}, {"ad_id": "ad_b"}])
    calls = []

    class FailingCreativeClient:
        async def get_object(self, object_id, *, fields):
            calls.append(object_id)
            if object_id == "ad_a":
                raise error_type("unavailable")
            return {"id": object_id, "creative": {"id": "crt_b"}}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FailingCreativeClient())
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07",
    ))

    assert calls == ["ad_a", "ad_b"]
    assert [finding["type"] for finding in result["findings"]] == ["creative_fatigue_risk"] * 2
    entities = [finding["affected_entities"][0] for finding in result["findings"]]
    assert entities[0]["creative_id"] is None
    assert entities[0]["creative_resolution_status"] == "lookup_failed"
    assert entities[0]["creative_resolution_error"] == error_type.__name__
    assert entities[1]["creative_id"] == "crt_b"
    assert result["complete"] is True
    assert result["creative_identity_complete"] is False


@pytest.mark.parametrize("limit", [0, 1])
def test_fatigue_creative_lookup_cap_preserves_unresolved_findings(monkeypatch, limit) -> None:
    _patch_fatigue_rows(monkeypatch, [{"ad_id": "ad_b"}, {"ad_id": "ad_a"}])
    calls = []

    class BoundedCreativeClient:
        async def get_object(self, object_id, *, fields):
            calls.append(object_id)
            return {"id": object_id, "creative": {"id": "crt_a"}}

    def client_factory():
        assert limit > 0, "Disabled enrichment must not construct a client"
        return BoundedCreativeClient()

    monkeypatch.setattr(diagnostics, "get_graph_api_client", client_factory)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07", max_creative_lookups=limit,
    ))

    assert calls == ["ad_a"][:limit]
    assert result["creative_lookups_attempted"] == limit
    assert result["max_creative_lookups"] == limit
    assert result["complete"] is True
    assert result["creative_identity_complete"] is False
    entities = [finding["affected_entities"][0] for finding in result["findings"]]
    assert len(entities) == 2
    for entity in entities[limit:]:
        assert entity["creative_id"] is None
        assert entity["creative_resolution_status"] == "lookup_limit_reached"


def test_fatigue_stops_creative_reads_after_rate_limit(monkeypatch) -> None:
    _patch_fatigue_rows(monkeypatch, [{"ad_id": "ad_a"}, {"ad_id": "ad_b"}, {"ad_id": "ad_c"}])
    calls = []

    class RateLimitedClient:
        async def get_object(self, object_id, *, fields):
            calls.append(object_id)
            raise diagnostics.RateLimitError("throttled")

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: RateLimitedClient())
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07",
    ))

    assert calls == ["ad_a"]
    assert result["creative_lookups_attempted"] == 1
    assert result["creative_identity_complete"] is False
    assert len(result["findings"]) == 3
    for finding in result["findings"]:
        entity = finding["affected_entities"][0]
        assert entity["creative_id"] is None
        assert entity["creative_resolution_status"] == "rate_limited"


def test_fatigue_transient_enrichment_stop_preserves_resolved_groups_and_cached_ids(monkeypatch) -> None:
    _patch_fatigue_rows(monkeypatch, [
        {"ad_id": "ad_a", "campaign_id": "cmp_1"},
        {"ad_id": "ad_a", "campaign_id": "cmp_1"},
        {"ad_id": "ad_b"}, {"ad_id": "ad_c"},
    ])
    calls = []

    class InterruptedCreativeClient:
        async def get_object(self, object_id, *, fields):
            calls.append(object_id)
            if object_id == "ad_b":
                raise diagnostics.MetaApiError("temporary outage", is_transient=True)
            return {"id": object_id, "creative": {"id": "crt_a"}}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: InterruptedCreativeClient())
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07",
    ))

    assert calls == ["ad_a", "ad_b"]
    assert result["creative_lookups_attempted"] == 2
    assert result["complete"] is True
    assert result["creative_identity_complete"] is False
    assert len(result["findings"]) == 4
    entities = [finding["affected_entities"][0] for finding in result["findings"]]
    assert [entity["creative_resolution_status"] for entity in entities] == [
        "resolved", "resolved", "lookup_failed", "skipped_transient_error",
    ]
    assert entities[2]["creative_resolution_error"] == "MetaApiError"
    assert entities[2]["creative_id"] is entities[3]["creative_id"] is None
    assert result["creative_groups"] == [{
        "creative_id": "crt_a", "ad_ids": ["ad_a"], "campaign_ids": ["cmp_1"],
    }]


@pytest.mark.parametrize("failure", ["timeout", "503"])
def test_fatigue_retries_only_one_creative_read_during_transient_outage(monkeypatch, failure) -> None:
    import httpx

    from meta_ads_mcp.config import Settings
    from meta_ads_mcp.graph_api import GraphAPIClient

    _patch_fatigue_rows(monkeypatch, [{"ad_id": "ad_a"}, {"ad_id": "ad_b"}, {"ad_id": "ad_c"}])
    paths = []
    sleeps = []

    def fail_request(request):
        paths.append(request.url.path)
        assert request.url.params["fields"] == "id,name,creative{id}"
        if failure == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(503, json={"error": {"message": "Service unavailable", "code": 2}})

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    settings = Settings(
        access_token="test-token", api_version="v25.0", default_account_id=None,
        app_id=None, app_secret=None, redirect_uri=None, log_level="INFO",
        host="127.0.0.1", port=8000, request_timeout=30, max_retries=2,
    )

    async def run_report():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_request)) as http_client:
            monkeypatch.setattr(GraphAPIClient, "_get_shared_client", lambda self, **kwargs: http_client)
            graph_client = GraphAPIClient(settings=settings, access_token_override="test-token")
            monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: graph_client)
            return await diagnostics.get_creative_fatigue_report(
                campaign_id="cmp_1", since="2026-03-01", until="2026-03-07",
            )

    result = asyncio.run(run_report())

    assert paths == ["/v25.0/ad_a"] * 3  # Initial request plus only this ad's two retries.
    assert sleeps == [1.0, 2.0]
    assert result["creative_lookups_attempted"] == 1
    assert result["complete"] is True
    assert result["creative_identity_complete"] is False
    assert [finding["type"] for finding in result["findings"]] == ["creative_fatigue_risk"] * 3
    entities = [finding["affected_entities"][0] for finding in result["findings"]]
    assert [entity["creative_resolution_status"] for entity in entities] == [
        "lookup_failed", "skipped_transient_error", "skipped_transient_error",
    ]
    assert all(entity["creative_id"] is None for entity in entities)


def test_fatigue_reuses_creative_metadata_for_duplicate_flagged_ad_ids(monkeypatch) -> None:
    _patch_fatigue_rows(monkeypatch, [{"ad_id": "ad_1"}, {"ad_id": "ad_1"}])
    calls = []

    class ReusedCreativeClient:
        async def get_object(self, object_id, *, fields):
            calls.append(object_id)
            return {"id": object_id, "creative": {"id": "crt_1"}}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: ReusedCreativeClient())
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07", max_creative_lookups=1,
    ))

    assert calls == ["ad_1"]
    assert result["creative_identity_complete"] is True
    assert result["creative_groups"] == [{"creative_id": "crt_1", "ad_ids": ["ad_1"], "campaign_ids": []}]


@pytest.mark.parametrize("reverse", [False, True])
def test_fatigue_ranking_uses_observed_evidence_then_stable_id(monkeypatch, reverse) -> None:
    specs = [
        {"ad_id": "sample", "impressions": 9000, "previous_impressions": 6000, "ctr_decline": 0.2, "frequency_increase": 0.2, "spend": 0},
        {"ad_id": "decline", "impressions": 10000, "previous_impressions": 5000, "ctr_decline": 0.8, "frequency_increase": 0.2, "spend": 0},
        {"ad_id": "frequency", "impressions": 5000, "frequency_increase": 1, "spend": 0},
        {"ad_id": "spend", "impressions": 5000, "spend": 20},
        {"ad_id": "ad_b", "impressions": 5000},
        {"ad_id": "ad_a", "impressions": 5000},
    ]
    _patch_fatigue_rows(monkeypatch, list(reversed(specs)) if reverse else specs)
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: pytest.fail("enrichment disabled"))
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07", max_creative_lookups=0,
    ))

    assert [finding["affected_entities"][0]["ad_id"] for finding in result["findings"]] == [
        "sample", "decline", "frequency", "spend", "ad_a", "ad_b",
    ]
    assert result["ranked_by"] == "evidence_strength"
    assert "not statistical significance" in result["ranking_note"]
    assert result["findings"][0]["ranking_values"]["min_window_impressions"] == 6000
    for finding in result["findings"]:
        assert set(finding["ranking_values"]) == {
            "min_window_impressions", "ctr_decline", "frequency_increase", "current_spend",
        }


@pytest.mark.parametrize(("decline", "rise", "expected"), [
    (0.2, 0.2, "medium"), (0.5, 0.5, "high"),
    (0.499, 0.5, "medium"), (0.5, 0.499, "medium"),
])
def test_fatigue_severity_is_explicit_prioritization_not_probability(monkeypatch, decline, rise, expected) -> None:
    _patch_fatigue_rows(monkeypatch, [{"ad_id": "ad_1", "ctr_decline": decline, "frequency_increase": rise}])
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: pytest.fail("enrichment disabled"))
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07", max_creative_lookups=0,
    ))

    finding = result["findings"][0]
    assert finding["type"] == "creative_fatigue_risk"
    assert finding["severity"] == expected
    assert finding["confidence"] is None
    assert finding["confidence_status"] == result["confidence_status"] == "uncalibrated"
    assert "prioritization policy, not a probability" in result["severity_policy"]


@pytest.mark.parametrize(("previous_ctr", "current_ctr", "previous_frequency", "current_frequency", "severity"), [
    ("1.0", "0.5", "1.6", "2.4", "high"),
    ("1.0", "0.8", "2.0", "2.4", "medium"),
    ("0.7", "0.56", "5.0", "6.0", "medium"),
    ("1.0", "0.8", "2.0", "2.399998", None),
    ("1.0", "0.800001", "2.0", "2.4", None),
    ("1.0", "0.5", "1.6", "2.3999984", "medium"),
    ("1.0", "0.500001", "1.6", "2.4", "medium"),
])
def test_fatigue_decimal_boundaries_tolerate_only_float_noise(
    monkeypatch, previous_ctr, current_ctr, previous_frequency, current_frequency, severity,
) -> None:
    class DecimalBoundaryClient:
        async def get_insights(self, object_id, *, fields, params):
            current = json.loads(params["time_range"])["since"] == "2026-03-01"
            ctr = current_ctr if current else previous_ctr
            return {"data": [{
                "ad_id": "ad_1", "ctr": ctr, "impressions": "10000",
                "clicks": str(float(ctr) * 100),
                "frequency": current_frequency if current else previous_frequency,
            }]}

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: DecimalBoundaryClient())
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07", max_creative_lookups=0,
    ))

    finding = result["findings"][0]
    if severity is None:
        assert finding["type"] == "no_pattern_detected"
    else:
        assert finding["type"] == "creative_fatigue_risk"
        assert finding["severity"] == severity
        assert finding["confidence"] is None


def test_fatigue_ranking_preserves_unknown_counts_and_spend(monkeypatch) -> None:
    async def fake_child_insights(*args, since=None, **kwargs):
        current = since == "2026-03-01"
        return [{"ad_id": "ad_1", "metrics": {
            "ctr": 0.025 if current else 0.05,
            "frequency": 7.5 if current else 5,
        }}]

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: pytest.fail("enrichment disabled"))
    result = asyncio.run(diagnostics.get_creative_fatigue_report(
        campaign_id="cmp_1", since="2026-03-01", until="2026-03-07",
        min_impressions=0, max_creative_lookups=0,
    ))

    ranking = result["findings"][0]["ranking_values"]
    assert ranking["min_window_impressions"] is None
    assert ranking["current_spend"] is None
    assert "remains null" in result["ranking_note"]


@pytest.mark.parametrize("limit", [-1, 1001])
def test_fatigue_rejects_creative_lookup_bounds_before_client_or_reads(monkeypatch, limit) -> None:
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: pytest.fail("must not construct a client"))
    monkeypatch.setattr(diagnostics, "_child_insights", lambda *args, **kwargs: pytest.fail("must not read"))
    with pytest.raises(diagnostics.ValidationError, match="max_creative_lookups"):
        asyncio.run(diagnostics.get_creative_fatigue_report(account_id="123", max_creative_lookups=limit))


@pytest.mark.parametrize("max_ads", [0, -1, 10001])
def test_fatigue_rejects_invalid_scan_bound_before_client(monkeypatch, max_ads) -> None:
    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(diagnostics.ValidationError, match="max_ads"):
        asyncio.run(diagnostics.get_creative_fatigue_report(account_id="123", max_ads=max_ads))


def test_fatigue_scan_bound_is_forwarded_to_both_windows(monkeypatch) -> None:
    limits = []

    async def fake_child_insights(*args, **kwargs):
        limits.append(kwargs["max_rows"])
        return []

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    result = asyncio.run(diagnostics.get_creative_fatigue_report(account_id="123", max_ads=5000))
    assert limits == [5000, 5000]
    assert result["max_ads"] == 5000


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("dates", [
    {},
    {"since": "", "until": " \t"},
    {"previous_since": " \t", "previous_until": ""},
    {"since": " \t", "until": "", "previous_since": "", "previous_until": " \n"},
])
def test_creative_fatigue_report_blank_dates_use_default_windows(monkeypatch, routed, dates) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_child_insights(object_id: str, *, since: str, until: str, **kwargs):
        calls.append((since, until))
        return []

    class FixedDate(diagnostics.date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 8)

    monkeypatch.setattr(diagnostics, "_child_insights", fake_child_insights)
    monkeypatch.setattr(diagnostics, "date", FixedDate)
    arguments = {"campaign_id": "cmp_123", "current_window_days": 3, "previous_window_days": 5, **dates}
    result = asyncio.run(diagnostics.mcp_server.call_tool(
        "call_tool" if routed else "get_creative_fatigue_report",
        {"name": "get_creative_fatigue_report", "arguments": arguments} if routed else arguments,
    )).structured_content

    assert calls == [("2026-03-05", "2026-03-07"), ("2026-02-28", "2026-03-04")]
    assert result["current_window"] == {"since": "2026-03-05", "until": "2026-03-07"}
    assert result["previous_window"] == {"since": "2026-02-28", "until": "2026-03-04"}


@pytest.mark.parametrize(("previous_dates", "expected_previous"), [
    ({}, ("2026-02-22", "2026-02-28")),
    ({"previous_since": "", "previous_until": " \t"}, ("2026-02-22", "2026-02-28")),
    ({"previous_since": "2026-02-01", "previous_until": "2026-02-07"}, ("2026-02-01", "2026-02-07")),
])
def test_creative_fatigue_report_supports_explicit_windows(monkeypatch, previous_dates, expected_previous) -> None:
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
            **previous_dates,
        )
    )
    assert calls == [("2026-03-01", "2026-03-07"), expected_previous]
    assert result["current_window"] == {"since": "2026-03-01", "until": "2026-03-07"}
    assert result["previous_window"] == {"since": expected_previous[0], "until": expected_previous[1]}


@pytest.mark.parametrize(("dates", "message"), [
    ({"since": "", "until": "2026-03-07"}, "both since and until"),
    ({"since": "2026-03-01", "until": " \t"}, "both since and until"),
    ({"since": "2026-03-01", "until": "2026-03-07", "previous_since": "2026-02-22", "previous_until": ""},
     "both previous_since and previous_until"),
    ({"since": "2026-03-01", "until": "2026-03-07", "previous_since": " \t", "previous_until": "2026-02-28"},
     "both previous_since and previous_until"),
    ({"previous_since": "2026-02-22", "previous_until": "2026-02-28"}, "both since and until"),
    ({"since": "2026-03-01", "until": "2026-03-07", "previous_since": "bad-date", "previous_until": "2026-02-28"},
     "previous_since must be a valid ISO date"),
    ({"since": "2026-03-01", "until": "2026-03-07", "previous_since": "2026-02-28", "previous_until": "2026-02-22"},
     "previous_until must be on or after previous_since"),
])
def test_creative_fatigue_report_rejects_partial_or_invalid_windows_before_reads(monkeypatch, dates, message) -> None:
    monkeypatch.setattr(diagnostics, "_child_insights", lambda *args, **kwargs: pytest.fail("must not read"))
    with pytest.raises(diagnostics.ValidationError, match=message):
        asyncio.run(diagnostics.get_creative_fatigue_report(campaign_id="cmp_123", **dates))


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
        metrics = {"frequency": 3.0, "ctr": 0.005, "roas": 0.8}
        return {"items": [{"metrics": metrics}], "summary": {"metrics": metrics}}

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


def test_detect_auction_overlap_normalizes_campaign_ids(monkeypatch) -> None:
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
        diagnostics.detect_auction_overlap(account_id="123", campaign_ids=[" cmp_1 ", "cmp_1"])
    )
    assert insight_object_ids == ["cmp_1"]
    assert result["campaigns"][0]["campaign_id"] == "cmp_1"
    assert result["campaigns"][0]["campaign_name"] == "cmp_1"


def test_detect_auction_overlap_returns_insufficient_data_for_empty_campaign_selection(monkeypatch) -> None:
    class FailIfCalledClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            raise AssertionError("discovery should not run for an explicit empty campaign selection")

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("insights should not be fetched without valid campaigns")

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FailIfCalledClient())
    monkeypatch.setattr(diagnostics, "get_entity_insights", fail_if_called)
    for campaign_ids in ([], [" ", ""]):
        result = asyncio.run(diagnostics.detect_auction_overlap(account_id="123", campaign_ids=campaign_ids))

        assert result["campaign_count"] == 0
        assert result["campaigns"] == []
        assert result["overlap_platforms"] == {}
        assert result["findings"][0]["type"] == "insufficient_data"
        assert "no_platform_overlap_detected" not in {finding["type"] for finding in result["findings"]}


def test_detect_auction_overlap_returns_insufficient_data_when_discovery_is_empty(monkeypatch) -> None:
    class EmptyClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            return {"data": []}

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("insights should not be fetched without discovered campaigns")

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: EmptyClient())
    monkeypatch.setattr(diagnostics, "get_entity_insights", fail_if_called)
    result = asyncio.run(diagnostics.detect_auction_overlap(account_id="123"))

    assert result["campaign_count"] == 0
    assert result["findings"][0]["type"] == "insufficient_data"


def test_detect_auction_overlap_rejects_non_positive_max_campaigns(monkeypatch) -> None:
    class FailIfCalledClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            raise AssertionError("max_campaigns validation should happen before discovery")

    monkeypatch.setattr(diagnostics, "get_graph_api_client", lambda: FailIfCalledClient())
    with pytest.raises(diagnostics.ValidationError, match="max_campaigns must be at least 1"):
        asyncio.run(diagnostics.detect_auction_overlap(account_id="123", max_campaigns=0))


@pytest.mark.parametrize("date_preset", [None, " "])
def test_detect_auction_overlap_ignores_blank_inputs_for_default_window(monkeypatch, date_preset) -> None:
    insight_calls: list[dict[str, object]] = []

    async def fake_get_entity_insights(*, object_id: str, **kwargs):
        insight_calls.append(kwargs)
        return {"items": [], "summary": {"metrics": {"spend": 0.0}}}

    monkeypatch.setattr(diagnostics, "get_entity_insights", fake_get_entity_insights)
    result = asyncio.run(
        diagnostics.detect_auction_overlap(
            account_id="123",
            campaign_ids=["cmp_1"],
            date_preset=date_preset,
            since=" ",
            until=" ",
        )
    )
    assert insight_calls[0]["date_preset"] == "last_30d"
    assert insight_calls[0]["since"] is None
    assert insight_calls[0]["until"] is None
    assert result["window"] == {"date_preset": "last_30d", "since": None, "until": None}
