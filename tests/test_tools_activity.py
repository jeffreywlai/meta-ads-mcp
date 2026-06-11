"""Activity/change-history tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.tools import activity


class FakeActivityClient:
    """Simple fake API client for activity tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.calls.append(
            {
                "parent_id": parent_id,
                "edge": edge,
                "fields": fields,
                "params": params,
            }
        )
        return {
            "data": [
                {
                    "actor_id": "user_123",
                    "actor_name": "Test User",
                    "application_name": "Meta Ads Manager",
                    "event_time": "1770000000",
                    "event_type": "update_campaign_budget",
                    "extra_data": '{"old_value":"50","new_value":"75"}',
                    "object_id": "cmp_123",
                    "object_name": "Campaign 123",
                    "object_type": "campaign",
                    "translated_event_type": "Updated campaign budget",
                }
            ],
            "paging": {"cursors": {"after": "after_1"}},
        }


def test_list_change_history_defaults_to_configured_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "123")
    reload_settings()
    client = FakeActivityClient()
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    result = asyncio.run(activity.list_change_history(limit=25))

    assert client.calls == [
        {
            "parent_id": "act_123",
            "edge": "activities",
            "fields": activity.DEFAULT_ACTIVITY_FIELDS,
            "params": {"limit": 25},
        }
    ]
    assert result["scope"] == {"level": "account", "object_id": "act_123"}
    assert result["items"][0]["extra_data_parsed"] == {"old_value": "50", "new_value": "75"}
    assert result["summary"]["default_window"].startswith("Meta returns one week")


def test_list_change_history_supports_campaign_filters_and_custom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeActivityClient()
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    result = asyncio.run(
        activity.list_change_history(
            campaign_id="cmp_123",
            since="2026-06-01",
            until="2026-06-07",
            category="ad_set",
            business_id="biz_123",
            uid=987654321,
            fields=["event_time", "event_type", "object_id"],
            after="cursor_1",
        )
    )

    assert client.calls[0] == {
        "parent_id": "cmp_123",
        "edge": "activities",
        "fields": ["event_time", "event_type", "object_id"],
        "params": {
            "limit": 50,
            "after": "cursor_1",
            "since": "2026-06-01",
            "until": "2026-06-07",
            "category": "AD_SET",
            "business_id": "biz_123",
            "uid": 987654321,
        },
    }
    assert result["scope"] == {"level": "campaign", "object_id": "cmp_123"}
    assert result["summary"]["date_window"] == {"since": "2026-06-01", "until": "2026-06-07"}
    assert result["summary"]["uid"] == 987654321


def test_list_change_history_rejects_conflicting_scopes() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(activity.list_change_history(campaign_id="cmp_123", ad_id="ad_123"))


def test_list_change_history_rejects_conflicting_level_alias() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(activity.list_change_history(level="ad", campaign_id="cmp_123"))


def test_list_change_history_requires_level_with_generic_object_id() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(activity.list_change_history(object_id="cmp_123"))
