"""HTTP entrypoint tests."""

from __future__ import annotations

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
