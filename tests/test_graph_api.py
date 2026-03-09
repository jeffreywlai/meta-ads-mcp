"""Graph API client error-classification tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.config import Settings
from meta_ads_mcp.errors import MetaApiError, UnsupportedFeatureError
from meta_ads_mcp.graph_api import GraphAPIClient


class FakeResponse:
    """Minimal fake httpx response for request error tests."""

    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = ""

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> dict[str, object]:
        return self._payload


class FakeAsyncClient:
    """Minimal async client that returns one configured response."""

    response: FakeResponse

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001, ANN201
        return None

    async def request(self, *args, **kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
        return self.response


def _client() -> GraphAPIClient:
    return GraphAPIClient(
        settings=Settings(
            access_token="token_123",
            api_version="v25.0",
            default_account_id=None,
            app_id=None,
            app_secret=None,
            redirect_uri=None,
            log_level="INFO",
            host="127.0.0.1",
            port=8000,
            request_timeout=30.0,
            max_retries=0,
        )
    )


def test_request_maps_unsupported_get_request_to_unsupported_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.response = FakeResponse(
        400,
        {"error": {"message": "Unsupported get request.", "code": 100}},
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(UnsupportedFeatureError):
        asyncio.run(_client().request("GET", "bad-edge"))


def test_request_keeps_invalid_field_errors_as_meta_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.response = FakeResponse(
        400,
        {"error": {"message": "(#100) Tried accessing nonexisting field (assigned_pages)", "code": 100}},
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client().request("GET", "bad-edge"))
    assert exc_info.value.code == 100


def test_request_keeps_invalid_async_fields_as_meta_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.response = FakeResponse(
        400,
        {
            "error": {
                "message": "(#100) Cannot include adset_id in fields param because it was not in the report run",
                "code": 100,
            }
        },
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client().request("GET", "bad-edge"))
    assert "fields param" in exc_info.value.message
