"""HTTP entrypoint tests."""

from __future__ import annotations

import asyncio

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
