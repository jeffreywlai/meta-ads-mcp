"""Graph API client error-classification tests."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import pytest

from meta_ads_mcp.auth import build_appsecret_proof
from meta_ads_mcp.config import Settings
from meta_ads_mcp.errors import MetaApiError, RateLimitError, UnsupportedFeatureError
from meta_ads_mcp.graph_api import (
    _CLIENT_POOL,
    GraphAPIClient,
    _parse_retry_after_seconds,
    close_graph_api_clients,
)


class FakeResponse:
    """Minimal fake httpx response for request error tests."""

    def __init__(
        self,
        status_code: int,
        payload: dict[str, object] | Exception,
        *,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = text

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> dict[str, object]:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeAsyncClient:
    """Minimal async client that returns one configured response."""

    responses: deque[FakeResponse]
    instances: list["FakeAsyncClient"] = []
    requests: list[dict[str, object]] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.is_closed = False
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001, ANN201
        return None

    async def request(self, *args, **kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
        self.requests.append({"args": args, **kwargs})
        return self.responses.popleft()

    async def aclose(self) -> None:
        self.is_closed = True


def _client(
    *,
    max_retries: int = 0,
    access_token: str = "token_123",
    app_secret: str | None = None,
    access_token_override: str | None = None,
) -> GraphAPIClient:
    return GraphAPIClient(
        settings=Settings(
            access_token=access_token,
            api_version="v25.0",
            default_account_id=None,
            app_id=None,
            app_secret=app_secret,
            redirect_uri=None,
            log_level="INFO",
            host="127.0.0.1",
            port=8000,
            request_timeout=30.0,
            max_retries=max_retries,
        ),
        access_token_override=access_token_override,
    )


@pytest.fixture(autouse=True)
def clear_client_pool() -> None:
    _CLIENT_POOL.clear()
    FakeAsyncClient.instances.clear()
    FakeAsyncClient.requests.clear()
    yield
    _CLIENT_POOL.clear()
    FakeAsyncClient.instances.clear()
    FakeAsyncClient.requests.clear()


def test_build_appsecret_proof_matches_meta_hmac_contract() -> None:
    assert build_appsecret_proof("token_123", "secret_456") == (
        "44e47e2eac8b6a54fe7a82ce8a314d259ff7359ad6e5888ad929f8012afaf369"
    )


def test_request_adds_appsecret_proof_without_mutating_caller_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"data": []})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    caller_params = {"limit": 25}

    asyncio.run(
        _client(app_secret="secret_456").request(
            "GET",
            "act_1/campaigns",
            params=caller_params,
        )
    )

    assert caller_params == {"limit": 25}
    assert FakeAsyncClient.requests[0]["params"] == {
        "limit": 25,
        "appsecret_proof": "44e47e2eac8b6a54fe7a82ce8a314d259ff7359ad6e5888ad929f8012afaf369",
    }


def test_request_uses_override_token_for_bearer_header_and_appsecret_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"id": "ok"})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        _client(
            app_secret="secret_456",
            access_token_override="override_token",
        ).request("POST", "act_1/insights", data={"async": "true"})
    )

    request = FakeAsyncClient.requests[0]
    assert request["headers"]["Authorization"] == "Bearer override_token"
    assert request["params"] == {
        "appsecret_proof": build_appsecret_proof("override_token", "secret_456")
    }
    assert request["data"] == {"async": "true"}


def test_request_omits_appsecret_proof_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"data": []})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(_client().request("GET", "act_1/campaigns"))

    assert FakeAsyncClient.requests[0]["params"] is None


def test_unauthenticated_request_does_not_add_appsecret_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"data": {"is_valid": True}})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        _client(app_secret="secret_456").request(
            "GET",
            "debug_token",
            params={"input_token": "input", "access_token": "app|secret"},
            use_auth_header=False,
        )
    )

    request = FakeAsyncClient.requests[0]
    assert "Authorization" not in request["headers"]
    assert request["params"] == {
        "input_token": "input",
        "access_token": "app|secret",
    }


def test_system_user_token_request_adds_proof_for_its_explicit_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"access_token": "generated"})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        _client(app_secret="secret_456").generate_system_user_token(
            "sys_123",
            business_app="app_123",
            scope=["ads_management"],
            access_token="admin_token",
        )
    )

    request = FakeAsyncClient.requests[0]
    assert "Authorization" not in request["headers"]
    assert request["params"] == {
        "access_token": "admin_token",
        "appsecret_proof": build_appsecret_proof("admin_token", "secret_456"),
    }


def test_request_maps_unsupported_get_request_to_unsupported_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {
                    "error": {
                        "message": "Unsupported get request.",
                        "code": 100,
                        "is_transient": True,
                    }
                },
            ),
            FakeResponse(200, {"data": [{"id": "must_not_be_returned"}]}),
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(UnsupportedFeatureError):
        asyncio.run(_client(max_retries=1).request("GET", "bad-edge"))
    assert len(FakeAsyncClient.responses) == 1


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


def test_meta_api_error_tolerates_non_object_error_payload() -> None:
    error = MetaApiError.from_payload({"error": "upstream failure"}, status_code=500)
    assert error.message == "upstream failure"
    assert error.status_code == 500


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


def test_graph_client_accepts_csv_fields_for_direct_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_request(self, method: str, endpoint: str, **kwargs):
        calls.append({"method": method, "endpoint": endpoint, **kwargs})
        return {"id": endpoint}

    monkeypatch.setattr(GraphAPIClient, "request", fake_request)

    result = asyncio.run(_client().get_object("crt_123", fields="id, name, url_tags"))  # type: ignore[arg-type]

    assert result == {"id": "crt_123"}
    assert calls == [
        {
            "method": "GET",
            "endpoint": "crt_123",
            "params": {"fields": "id,name,url_tags"},
        }
    ]


def test_get_async_report_accepts_normalized_completed_status(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_get_object(self, object_id: str, *, fields=None, params=None):
        return {"id": object_id, "async_status": "completed"}

    async def fake_list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        calls.append((parent_id, edge))
        return {"data": [{"spend": "1"}]}

    monkeypatch.setattr(GraphAPIClient, "get_object", fake_get_object)
    monkeypatch.setattr(GraphAPIClient, "list_objects", fake_list_objects)
    result = asyncio.run(_client().get_async_report("rpt_1"))

    assert calls == [("rpt_1", "insights")]
    assert result["rows"]["data"] == [{"spend": "1"}]


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
    with pytest.raises(RateLimitError) as exc_info:
        asyncio.run(_client().request("GET", "retry-edge"))
    assert exc_info.value.retry_after_seconds == 1
    assert exc_info.value.code == 17
    assert exc_info.value.operation == "GET /retry-edge"


def test_read_rate_limit_respects_retry_after_and_returns_usage_when_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                429,
                {"error": {"message": "Too many calls", "code": 17}},
                headers={
                    "Retry-After": "9",
                    "X-Ad-Account-Usage": '{"acc_id_util_pct":99}',
                },
            )
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(RateLimitError) as exc_info:
        asyncio.run(_client().request("GET", "act_1/campaigns"))
    assert exc_info.value.retry_after_seconds == 9
    assert exc_info.value.usage == {
        "x-ad-account-usage": {"acc_id_util_pct": 99}
    }


def test_long_retry_after_is_preserved_without_blocking_automatic_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                429,
                {"error": {"message": "Too many calls", "code": 17}},
                headers={"Retry-After": "120"},
            ),
            FakeResponse(200, {"data": [{"id": "must_not_be_returned"}]}),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    with pytest.raises(RateLimitError) as exc_info:
        asyncio.run(_client(max_retries=1).request("GET", "act_1/campaigns"))

    assert exc_info.value.retry_after_seconds == 120
    assert sleeps == []
    assert len(FakeAsyncClient.responses) == 1


@pytest.mark.parametrize("retry_after", ["5", "60"])
def test_retry_after_within_automatic_wait_budget_is_honored(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                429,
                {"error": {"message": "Too many calls", "code": 17}},
                headers={"Retry-After": retry_after},
            ),
            FakeResponse(200, {"data": [{"id": "ok"}]}),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    result = asyncio.run(_client(max_retries=1).request("GET", "act_1/campaigns"))

    assert result["data"][0]["id"] == "ok"
    assert sleeps == [float(retry_after)]


def test_non_json_long_retry_after_is_preserved_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                429,
                ValueError("not json"),
                headers={"Retry-After": "120", "Content-Type": "text/html"},
                text="rate limited",
            ),
            FakeResponse(200, {"data": [{"id": "must_not_be_returned"}]}),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    with pytest.raises(RateLimitError) as exc_info:
        asyncio.run(_client(max_retries=1).request("GET", "act_1/campaigns"))

    assert exc_info.value.retry_after_seconds == 120
    assert sleeps == []
    assert len(FakeAsyncClient.responses) == 1


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("0", 0.0),
        (" 12.5 ", 12.5),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("not-a-delay", None),
    ],
)
def test_retry_after_numeric_validation(raw_value: str, expected: float | None) -> None:
    assert _parse_retry_after_seconds(raw_value) == expected


def test_retry_after_http_date_preserves_future_deadline() -> None:
    now = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)
    assert _parse_retry_after_seconds(
        "Wed, 12 Aug 2026 21:02:00 GMT",
        now=now,
    ) == 120.0
    assert _parse_retry_after_seconds(
        "Wed, 12 Aug 2026 20:59:00 GMT",
        now=now,
    ) == 0.0


def test_long_retry_after_is_exposed_on_transient_non_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                503,
                {
                    "error": {
                        "message": "Service unavailable",
                        "code": 2,
                        "is_transient": True,
                    }
                },
                headers={"Retry-After": "120"},
            ),
            FakeResponse(200, {"data": [{"id": "must_not_be_returned"}]}),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client(max_retries=1).request("GET", "act_1/campaigns"))

    assert exc_info.value.retry_after_seconds == 120
    assert exc_info.value.to_public_dict()["retry_after_seconds"] == 120
    assert sleeps == []
    assert len(FakeAsyncClient.responses) == 1


def test_mutation_rate_limit_is_not_automatically_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {"error": {"message": "User request limit reached", "code": 17}},
            ),
            FakeResponse(200, {"id": "must_not_be_created"}),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    with pytest.raises(RateLimitError):
        asyncio.run(_client(max_retries=2).request("POST", "act_1/adsets", data={"name": "x"}))
    assert sleeps == []
    assert len(FakeAsyncClient.responses) == 1


def test_ambiguous_mutation_5xx_is_not_retried_and_requires_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(503, {"error": {"message": "Service unavailable", "code": 2}}),
            FakeResponse(200, {"id": "duplicate"}),
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client(max_retries=2).request("POST", "act_1/ads", data={"name": "x"}))
    assert exc_info.value.mutation_outcome_unknown is True
    assert exc_info.value.is_transient is True
    assert "Verify the target" in exc_info.value.to_public_dict()["next_step"]
    assert len(FakeAsyncClient.responses) == 1


def test_transient_graph_write_error_marks_outcome_unknown_even_on_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {
                    "error": {
                        "message": "Temporary upstream failure",
                        "code": 2,
                        "is_transient": True,
                    }
                },
            ),
            FakeResponse(200, {"id": "duplicate"}),
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client(max_retries=2).request("POST", "act_1/ads", data={"name": "x"}))
    assert exc_info.value.is_transient is True
    assert exc_info.value.mutation_outcome_unknown is True
    assert exc_info.value.to_public_dict()["retryable"] is False
    assert len(FakeAsyncClient.responses) == 1


def test_safe_read_retries_transient_503(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(503, {"error": {"message": "Service unavailable", "code": 2}}),
            FakeResponse(200, {"data": [{"id": "ok"}]}),
        ]
    )

    async def fake_sleep(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    result = asyncio.run(_client(max_retries=1).request("GET", "act_1/campaigns"))
    assert result["data"][0]["id"] == "ok"


def test_safe_read_retries_graph_transient_error_on_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {
                    "error": {
                        "message": "Temporary upstream failure",
                        "code": 2,
                        "is_transient": True,
                    }
                },
            ),
            FakeResponse(200, {"data": [{"id": "ok"}]}),
        ]
    )

    async def fake_sleep(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    result = asyncio.run(_client(max_retries=1).request("GET", "act_1/campaigns"))
    assert result["data"][0]["id"] == "ok"


def test_safe_read_retries_transient_error_inside_success_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                200,
                {
                    "error": {
                        "message": "Temporary upstream failure",
                        "code": 2,
                        "is_transient": True,
                    }
                },
            ),
            FakeResponse(200, {"data": [{"id": "ok"}]}),
        ]
    )

    async def fake_sleep(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("meta_ads_mcp.graph_api.asyncio.sleep", fake_sleep)
    result = asyncio.run(_client(max_retries=1).request("GET", "act_1/campaigns"))
    assert result["data"][0]["id"] == "ok"


def test_exhausted_transient_read_remains_explicitly_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque(
        [
            FakeResponse(
                400,
                {
                    "error": {
                        "message": "Temporary upstream failure",
                        "code": 2,
                        "is_transient": True,
                    }
                },
            )
        ]
    )
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as exc_info:
        asyncio.run(_client(max_retries=0).request("GET", "act_1/campaigns"))
    public_error = exc_info.value.to_public_dict()
    assert public_error["retryable"] is True
    assert public_error["mutation_outcome_unknown"] is False


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
