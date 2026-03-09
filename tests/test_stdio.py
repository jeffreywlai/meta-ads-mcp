"""stdio entrypoint tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp import server
from meta_ads_mcp import stdio


def test_stdio_main_runs_server_with_stdio_transport(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(stdio.mcp_server, "run", fake_run)
    stdio.main()
    assert calls == [{"transport": "stdio", "show_banner": False}]


def test_stdio_and_http_entrypoints_share_the_same_tool_registry() -> None:
    assert stdio.TOOLS is server.TOOLS
    assert [module.__name__ for module in stdio.TOOLS] == [module.__name__ for module in server.TOOLS]


def test_stdio_and_http_entrypoints_expose_the_same_visible_tools() -> None:
    stdio_tools = {tool.name for tool in asyncio.run(stdio.mcp_server.list_tools(run_middleware=False))}
    server_tools = {tool.name for tool in asyncio.run(server.mcp_server.list_tools(run_middleware=False))}
    assert stdio_tools == server_tools
