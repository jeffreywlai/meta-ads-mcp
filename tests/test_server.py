"""HTTP entrypoint tests."""

from __future__ import annotations

import asyncio
import json

import httpx
from mcp.server.streamable_http_manager import DEFAULT_MAX_REQUEST_BODY_SIZE

from meta_ads_mcp import server
from meta_ads_mcp.config import Settings


def test_server_main_runs_streamable_http_with_settings(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(server.mcp_server, "run", fake_run)
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: Settings(
            access_token="token_123",
            api_version="v25.0",
            default_account_id=None,
            app_id=None,
            app_secret=None,
            redirect_uri=None,
            log_level="INFO",
            host="0.0.0.0",
            port=8080,
            request_timeout=30.0,
            max_retries=2,
        ),
    )
    server.main()
    assert calls == [
        {
            "transport": "streamable-http",
            "host": "0.0.0.0",
            "port": 8080,
            "show_banner": False,
        }
    ]


def test_streamable_http_rejects_oversized_request_bodies() -> None:
    async def send_oversized_request() -> httpx.Response:
        app = server.mcp_server.http_app(transport="streamable-http")
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.post(
                    "/mcp",
                    content=b"x" * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1),
                    headers={"content-type": "application/json"},
                )

    response = asyncio.run(send_oversized_request())

    assert response.status_code == 413
    assert response.text == "Request body too large"


def test_streamable_http_protocol_round_trip_and_session_cleanup() -> None:
    def message(response: httpx.Response) -> dict:
        response.raise_for_status()
        if response.headers["content-type"].startswith("application/json"):
            return response.json()
        data = next(line[5:] for line in response.text.splitlines() if line.startswith("data:"))
        return json.loads(data)

    async def round_trip() -> None:
        app = server.mcp_server.http_app(transport="streamable-http")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                headers={"accept": "application/json, text/event-stream"},
            ) as client:
                initialized = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "dependency-smoke-test", "version": "1"},
                    },
                })
                assert message(initialized)["result"]["protocolVersion"] == "2025-11-25"
                client.headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
                client.headers["mcp-protocol-version"] = "2025-11-25"
                notified = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                })
                assert notified.status_code == 202

                listed = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/list",
                })
                assert {tool["name"] for tool in message(listed)["result"]["tools"]} == {
                    "health_check", "get_capabilities", "list_ad_accounts", "search_tools", "call_tool",
                }
                called = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "get_capabilities", "arguments": {"tool_name": "get_insights"}},
                })
                result = message(called)["result"]
                assert not result.get("isError", False)
                assert result["structuredContent"]["tool"]["name"] == "get_insights"
                assert "include_raw_actions" in result["structuredContent"]["tool"]["input_schema"]["properties"]

                deleted = await client.delete("/mcp")
                assert deleted.status_code == 200
                reused = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 4, "method": "tools/list",
                })
                assert reused.status_code == 404

    asyncio.run(round_trip())
