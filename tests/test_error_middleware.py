"""Structured MCP error boundary tests."""

from __future__ import annotations

import asyncio
import json

import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import MiddlewareContext

from meta_ads_mcp.error_middleware import StructuredMetaErrorMiddleware
from meta_ads_mcp.errors import MetaApiError, RateLimitError


def _context() -> MiddlewareContext:
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name="failing_tool", arguments={})
    )


def test_meta_api_errors_are_allowlisted_and_actionable() -> None:
    middleware = StructuredMetaErrorMiddleware()

    async def fail(_context):
        raise MetaApiError(
            "Invalid parameter",
            status_code=400,
            code=100,
            subcode=1885316,
            user_title="Invalid bid constraint",
            user_message="Set a supported ROAS floor.",
            trace_id="trace-1",
            error_data={
                "blame_field_specs": [["bid_constraints"]],
                "nested": {"access_token": "must-not-leak"},
            },
            details={"access_token": "must-not-leak"},
        )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(middleware.on_call_tool(_context(), fail))
    payload = json.loads(str(exc_info.value))["error"]
    assert payload["code"] == 100
    assert payload["user_message"] == "Set a supported ROAS floor."
    assert payload["error_data"] == {
        "blame_field_specs": [["bid_constraints"]],
        "nested": {},
    }
    assert "access_token" not in str(exc_info.value)


def test_rate_limit_errors_include_retry_and_usage_guidance() -> None:
    middleware = StructuredMetaErrorMiddleware()

    async def fail(_context):
        raise RateLimitError(
            "User request limit reached",
            retry_after_seconds=12,
            code=17,
            usage={"x-ad-account-usage": {"acc_id_util_pct": 99}},
            operation="GET /act_1/campaigns",
        )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(middleware.on_call_tool(_context(), fail))
    payload = json.loads(str(exc_info.value))["error"]
    assert payload["retry_after_seconds"] == 12
    assert payload["retryable"] is True
    assert payload["operation"] == "GET /act_1/campaigns"


def test_fastmcp_wrapped_meta_errors_are_recovered_from_cause() -> None:
    middleware = StructuredMetaErrorMiddleware()
    meta_error = MetaApiError("Invalid parameter", code=100)

    async def fail(_context):
        raise ToolError("Error calling tool 'x': Invalid parameter") from meta_error

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(middleware.on_call_tool(_context(), fail))
    assert json.loads(str(exc_info.value))["error"]["code"] == 100
