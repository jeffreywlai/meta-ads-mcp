"""Graph API client error-classification tests."""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from meta_ads_mcp.config import Settings
from meta_ads_mcp.errors import MetaApiError, RateLimitError, UnsupportedFeatureError
from meta_ads_mcp.graph_api import GraphAPIClient, _CLIENT_POOL, close_graph_api_clients


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

    responses: deque[FakeResponse]
    instances: list["FakeAsyncClient"] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.is_closed = False
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001, ANN201
        return None

    async def request(self, *args, **kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
        return self.responses.popleft()

    async def aclose(self) -> None:
        self.is_closed = True


def _client(*, max_retries: int = 0) -> GraphAPIClient:
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
            max_retries=max_retries,
        )
    )


@pytest.fixture(autouse=True)
def clear_client_pool() -> None:
    _CLIENT_POOL.clear()
    FakeAsyncClient.instances.clear()
    yield
    _CLIENT_POOL.clear()
    FakeAsyncClient.instances.clear()


def test_request_maps_unsupported_get_request_to_unsupported_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {"error": {"message": "Unsupported get request.", "code": 100}},
            )
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(UnsupportedFeatureError):
        asyncio.run(_client().request("GET", "bad-edge"))


def test_request_keeps_invalid_field_errors_as_meta_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {"error": {"message": "(#100) Tried accessing nonexisting field (assigned_pages)", "code": 100}},
            )
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client().request("GET", "bad-edge"))
    assert exc_info.value.code == 100


def test_request_keeps_invalid_async_fields_as_meta_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {
                    "error": {
                        "message": "(#100) Cannot include adset_id in fields param because it was not in the report run",
                        "code": 100,
                    }
                },
            )
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client().request("GET", "bad-edge"))
    assert "fields param" in exc_info.value.message


def test_request_retries_payload_rate_limit_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {"error": {"message": "User request limit reached", "code": 17, "error_subcode": 2446079}},
            ),
            FakeResponse(200, {"data": [{"id": "ok"}]}),
        ]
    )
    async def fake_sleep(*_args, **_kwargs) -> None:
        return None

    client = _client(max_retries=1)
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    result = asyncio.run(client.request("GET", "retry-edge"))
    assert result["data"][0]["id"] == "ok"


def test_request_raises_rate_limit_error_for_exhausted_payload_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {"error": {"message": "User request limit reached", "code": 17, "error_subcode": 2446079}},
            )
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(RateLimitError):
        asyncio.run(_client().request("GET", "retry-edge"))


def test_request_reuses_shared_async_client_within_one_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(200, {"data": [{"id": "first"}]}),
            FakeResponse(200, {"data": [{"id": "second"}]}),
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    client = _client()
    async def run_two() -> tuple[dict[str, object], dict[str, object]]:
        first_result = await client.request("GET", "one")
        second_result = await client.request("GET", "two")
        return first_result, second_result

    first, second = asyncio.run(run_two())
    assert first["data"][0]["id"] == "first"
    assert second["data"][0]["id"] == "second"
    assert len(FakeAsyncClient.instances) == 1


def test_request_uses_separate_clients_across_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(200, {"data": [{"id": "first"}]}),
            FakeResponse(200, {"data": [{"id": "second"}]}),
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    client = _client()
    asyncio.run(client.request("GET", "one"))
    asyncio.run(client.request("GET", "two"))
    assert len(FakeAsyncClient.instances) == 2


def test_close_graph_api_clients_closes_pooled_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"data": [{"id": "first"}]})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    asyncio.run(_client().request("GET", "one"))
    assert len(FakeAsyncClient.instances) == 1
    assert not FakeAsyncClient.instances[0].is_closed
    asyncio.run(close_graph_api_clients())
    assert FakeAsyncClient.instances[0].is_closed is True
    assert _CLIENT_POOL == {}
