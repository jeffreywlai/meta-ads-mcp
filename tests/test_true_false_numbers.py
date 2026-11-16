"""Money and count parameters refuse true and false (design part C2).

MCP validation reads true as 1 and false as 0 for a plain number type, so a budget,
bid, lookalike ratio or retention period sent as true used to reach Meta as 1. Each
one now refuses true and false before the tool starts, and still takes every number
form it took before.
"""

from __future__ import annotations

import asyncio
import typing

import httpx
import pytest
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import mcp_server

TOOLS = {tool.name: tool for tool in mcp_server.local_provider._components.values() if hasattr(tool, "fn")}

MONEY = [
    ("create_ad", "bid_amount"),
    ("create_ad_set", "bid_amount"),
    ("create_ad_set", "daily_budget"),
    ("create_ad_set", "lifetime_budget"),
    ("create_campaign", "daily_budget"),
    ("create_campaign", "lifetime_budget"),
    ("update_campaign", "daily_budget"),
    ("update_campaign", "lifetime_budget"),
    ("update_campaign_budget", "daily_budget"),
    ("update_campaign_budget", "lifetime_budget"),
    ("update_adset_budget", "daily_budget"),
    ("update_adset_budget", "lifetime_budget"),
    ("update_adset_bid_amount", "bid_amount"),
    ("update_adset_bid_strategy", "bid_amount"),
]
RATIOS = [("create_lookalike_audience", "ratio"), ("create_lookalike_audience", "starting_ratio")]
DAYS = [("create_custom_audience", "retention_days"), ("update_custom_audience", "retention_days")]
ALL = MONEY + RATIOS + DAYS


def _adapter(tool: str, parameter: str) -> TypeAdapter:
    hints = typing.get_type_hints(TOOLS[tool].fn, include_extras=True)
    return TypeAdapter(hints[parameter])


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    requests: list[str] = []

    async def refuse(self, method, url, *args, **kwargs):
        requests.append(f"{method} {url}")
        raise AssertionError("no request may be sent in this test")

    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    return requests


@pytest.mark.parametrize(("tool", "parameter"), ALL)
def test_true_and_false_are_refused_before_any_request(tool: str, parameter: str, sent: list[str]) -> None:
    for flag in (True, False):
        with pytest.raises(ToolError) as info:
            asyncio.run(mcp_server.call_tool(tool, {parameter: flag}))
        message = str(info.value)
        assert f"{parameter}: " in message
        if tool not in {"create_ad"}:  # create_ad is a private tool: its validator text is withheld
            assert "not true or false" in message
    assert sent == []


@pytest.mark.parametrize(("tool", "parameter"), ALL)
def test_the_type_refuses_true_and_false(tool: str, parameter: str) -> None:
    adapter = _adapter(tool, parameter)
    for flag in (True, False):
        with pytest.raises(PydanticValidationError, match="not true or false"):
            adapter.validate_python(flag)
        with pytest.raises(PydanticValidationError, match="not true or false"):
            adapter.validate_json("true" if flag else "false")


@pytest.mark.parametrize(("tool", "parameter"), MONEY)
def test_every_amount_form_that_worked_still_works(tool: str, parameter: str) -> None:
    adapter = _adapter(tool, parameter)
    assert adapter.validate_python(12.5) == 12.5
    assert adapter.validate_python(12) == 12.0
    assert adapter.validate_python("12.50") == 12.5
    assert adapter.validate_json("12.5") == 12.5


@pytest.mark.parametrize(("tool", "parameter"), RATIOS)
def test_every_ratio_form_that_worked_still_works(tool: str, parameter: str) -> None:
    adapter = _adapter(tool, parameter)
    assert adapter.validate_python(0.01) == 0.01
    assert adapter.validate_python("0.05") == 0.05
    assert adapter.validate_json("0.1") == 0.1
    assert adapter.validate_python(None) is None


@pytest.mark.parametrize(("tool", "parameter"), DAYS)
def test_every_retention_form_that_worked_still_works(tool: str, parameter: str) -> None:
    adapter = _adapter(tool, parameter)
    assert adapter.validate_python(30) == 30
    assert adapter.validate_python("30") == 30
    assert adapter.validate_python(30.0) == 30
    assert adapter.validate_json("180") == 180
    assert adapter.validate_python(None) is None


@pytest.mark.parametrize(("tool", "parameter"), ALL)
def test_the_input_schema_is_unchanged(tool: str, parameter: str) -> None:
    """Only the check changes; clients see the same number type as before."""
    schema = TOOLS[tool].parameters["properties"][parameter]
    branches = schema.get("anyOf", [schema])
    types = {branch.get("type") for branch in branches}
    assert types <= {"number", "integer", "null"} and types & {"number", "integer"}
    assert "boolean" not in types


def test_a_budget_still_goes_through(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
    from meta_ads_mcp.tools import execution

    class Client:
        def __init__(self) -> None:
            self.updates: list[tuple[str, dict]] = []

        async def get_object(self, object_id, *, fields=None, params=None):
            if object_id.startswith("act_"):
                return {"id": object_id, "currency": "USD"}
            return {"id": object_id, "account_id": "1234567890", "daily_budget": "1000"}

        async def update_object(self, object_id, *, data):
            self.updates.append((object_id, data))
            return {"success": True}

    client = Client()
    monkeypatch.setattr(execution, "get_graph_api_client", lambda: client)
    asyncio.run(mcp_server.call_tool("update_adset_budget", {"adset_id": "1234567890", "daily_budget": 12.5}))
    asyncio.run(mcp_server.call_tool("update_adset_budget", {"adset_id": "1234567890", "daily_budget": "20"}))
    assert [data["daily_budget"] for _, data in client.updates] == [1250, 2000]
    assert sent == []
