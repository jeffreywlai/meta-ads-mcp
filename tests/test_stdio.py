"""stdio entrypoint tests."""

from __future__ import annotations

from meta_ads_mcp import stdio


def test_stdio_main_runs_server_with_stdio_transport(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(stdio.mcp_server, "run", fake_run)
    stdio.main()
    assert calls == [{"transport": "stdio", "show_banner": False}]
