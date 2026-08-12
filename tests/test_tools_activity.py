"""Activity/change-history tool tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.errors import NotFoundError, ValidationError
from meta_ads_mcp.tools import activity


class FakeActivityClient:
    """Simple fake API client for activity tests."""

    def __init__(self, object_account_id: str = "20141913") -> None:
        self.calls: list[dict[str, object]] = []
        self.object_calls: list[dict[str, object]] = []
        self.object_account_id = object_account_id

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.object_calls.append({"object_id": object_id, "fields": fields, "params": params})
        return {"id": object_id, "account_id": self.object_account_id}

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
    client = FakeActivityClient(object_account_id="123")
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
    assert result["scope"] == {"level": "account", "object_id": "act_123", "account_id": "act_123"}
    assert result["items"][0]["extra_data_parsed"] == {"old_value": "50", "new_value": "75"}
    assert result["summary"]["default_window"].startswith("Meta returns one week")


def test_list_change_history_routes_campaign_scope_through_account_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeActivityClient(object_account_id="123")
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    result = asyncio.run(
        activity.list_change_history(
            account_id="123",
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
        "parent_id": "act_123",
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
            "oid": "cmp_123",
        },
    }
    assert client.object_calls == [
        {"object_id": "cmp_123", "fields": ["account_id"], "params": None}
    ]
    assert result["scope"] == {"level": "campaign", "object_id": "cmp_123", "account_id": "act_123"}
    assert result["summary"]["date_window"] == {"since": "2026-06-01", "until": "2026-06-07"}
    assert result["summary"]["uid"] == 987654321
    assert result["summary"]["object_filter_id"] == "cmp_123"


def test_list_change_history_uses_default_account_for_scoped_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", "456")
    reload_settings()
    client = FakeActivityClient(object_account_id="456")
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    result = asyncio.run(activity.list_change_history(level="adset", object_id="adset_123"))

    assert client.calls[0]["parent_id"] == "act_456"
    assert client.calls[0]["params"] == {"limit": 50, "oid": "adset_123"}
    assert result["scope"] == {"level": "adset", "object_id": "adset_123", "account_id": "act_456"}
    assert client.object_calls == [
        {"object_id": "adset_123", "fields": ["account_id"], "params": None}
    ]


def test_list_change_history_derives_account_from_scoped_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_DEFAULT_ACCOUNT_ID", raising=False)
    reload_settings()
    client = FakeActivityClient()
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    result = asyncio.run(activity.list_change_history(level="campaign", object_id="cmp_123"))

    assert client.object_calls == [
        {"object_id": "cmp_123", "fields": ["account_id"], "params": None}
    ]
    assert client.calls[0]["parent_id"] == "act_20141913"
    assert client.calls[0]["params"] == {"limit": 50, "oid": "cmp_123"}
    assert result["scope"] == {
        "level": "campaign",
        "object_id": "cmp_123",
        "account_id": "act_20141913",
    }


def test_list_change_history_explains_failed_account_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_DEFAULT_ACCOUNT_ID", raising=False)
    reload_settings()

    class MissingAccountClient(FakeActivityClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            return {"id": object_id}

    monkeypatch.setattr(activity, "get_graph_api_client", lambda: MissingAccountClient())

    with pytest.raises(ValidationError, match="could not be derived"):
        asyncio.run(activity.list_change_history(ad_id="ad_123"))


@pytest.mark.parametrize(
    "lookup_error",
    [
        NotFoundError("object is unavailable"),
        httpx.ReadTimeout("ownership lookup timed out"),
    ],
)
def test_list_change_history_explains_account_lookup_failure_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    lookup_error: Exception,
) -> None:
    monkeypatch.delenv("META_DEFAULT_ACCOUNT_ID", raising=False)
    reload_settings()

    class FailedOwnershipClient(FakeActivityClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            raise lookup_error

    monkeypatch.setattr(
        activity,
        "get_graph_api_client",
        lambda: FailedOwnershipClient(),
    )

    with pytest.raises(
        ValidationError,
        match="could not be derived.*Pass account_id explicitly",
    ) as exc_info:
        asyncio.run(
            activity.list_change_history(
                level="campaign",
                object_id="cmp_123",
            )
        )

    assert exc_info.value.__cause__ is lookup_error
    assert str(lookup_error) in str(exc_info.value)


def test_list_change_history_rejects_mismatched_object_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeActivityClient(object_account_id="222")
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    with pytest.raises(ValidationError, match="belongs to account 'act_222'"):
        asyncio.run(
            activity.list_change_history(
                account_id="111",
                campaign_id="cmp_123",
            )
        )

    assert client.object_calls == [
        {"object_id": "cmp_123", "fields": ["account_id"], "params": None}
    ]


def test_list_change_history_uses_supplied_account_when_ownership_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableOwnershipClient(FakeActivityClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            self.object_calls.append(
                {"object_id": object_id, "fields": fields, "params": params}
            )
            if object_id == "missing_account_field":
                return {"id": object_id}
            raise NotFoundError("object is unavailable")

    client = UnavailableOwnershipClient()
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    missing_field = asyncio.run(
        activity.list_change_history(
            account_id="111",
            campaign_id="missing_account_field",
        )
    )
    inaccessible = asyncio.run(
        activity.list_change_history(
            account_id="111",
            campaign_id="inaccessible",
        )
    )

    assert missing_field["scope"]["account_id"] == "act_111"
    assert inaccessible["scope"]["account_id"] == "act_111"
    assert [call["parent_id"] for call in client.calls] == ["act_111", "act_111"]


def test_list_change_history_uses_supplied_account_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutOwnershipClient(FakeActivityClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            raise httpx.ReadTimeout("ownership lookup timed out")

    client = TimedOutOwnershipClient()
    monkeypatch.setattr(activity, "get_graph_api_client", lambda: client)

    result = asyncio.run(
        activity.list_change_history(
            account_id="111",
            campaign_id="cmp_123",
        )
    )

    assert result["scope"]["account_id"] == "act_111"
    assert client.calls[0]["parent_id"] == "act_111"


def test_list_change_history_rejects_conflicting_scopes() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(activity.list_change_history(campaign_id="cmp_123", ad_id="ad_123"))


def test_list_change_history_rejects_conflicting_level_alias() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(activity.list_change_history(level="ad", campaign_id="cmp_123"))


def test_list_change_history_requires_level_with_generic_object_id() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(activity.list_change_history(object_id="cmp_123"))
