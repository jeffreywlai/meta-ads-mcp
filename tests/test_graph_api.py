"""Graph API client error-classification tests."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from dataclasses import replace
import pytest

from meta_ads_mcp.auth import build_appsecret_proof
from meta_ads_mcp.config import Settings, reload_settings
from meta_ads_mcp.errors import MetaApiError, RateLimitError, UnsupportedFeatureError, ValidationError
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
        FakeAsyncClient.requests.append({"args": args, **kwargs})
        return FakeAsyncClient.responses.popleft()

    async def aclose(self) -> None:
        self.is_closed = True


def test_total_mutation_deadline_is_unknown_and_never_retried(monkeypatch):
    calls = []

    class SlowClient(FakeAsyncClient):
        async def request(self, *args, **kwargs):
            calls.append(kwargs)
            # Mimics a server that keeps the inactivity timeout alive.
            await asyncio.sleep(1)
            return FakeResponse(200, {"success": True})

    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", SlowClient)
    client = _client(max_retries=2)
    client.settings = replace(client.settings, request_timeout=0.01)
    with pytest.raises(MetaApiError) as error:
        asyncio.run(client.request("POST", "123", data={"daily_budget": 1000}))
    assert len(calls) == 1
    assert error.value.to_public_dict()["mutation_outcome_unknown"] is True
    assert error.value.to_public_dict()["retryable"] is False
    assert "Verify" in error.value.to_public_dict()["next_step"]


def _client(
    *,
    max_retries: int = 0,
    api_version: str = "v25.0",
    access_token: str = "token_123",
    app_secret: str | None = None,
    access_token_override: str | None = None,
) -> GraphAPIClient:
    return GraphAPIClient(
        settings=Settings(
            access_token=access_token,
            api_version=api_version,
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


def test_request_omits_appsecret_proof_for_unmatched_override_token(
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
    assert request["params"] is None
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


def test_system_user_token_request_omits_proof_for_unmatched_explicit_access_token(
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
    }


def test_system_user_token_request_adds_proof_for_configured_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"access_token": "generated"})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(
        _client(app_secret="secret_456").generate_system_user_token(
            "sys_123",
            business_app="app_123",
            scope=["ads_management"],
            access_token="token_123",
        )
    )

    request = FakeAsyncClient.requests[0]
    assert request["params"] == {
        "access_token": "token_123",
        "appsecret_proof": build_appsecret_proof("token_123", "secret_456"),
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


@pytest.mark.parametrize("filtered", [False, True])
def test_recommendations_serialize_native_filters_without_changing_unfiltered_defaults(
    monkeypatch, filtered,
) -> None:
    FakeAsyncClient.responses = deque([FakeResponse(200, {"data": []})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    options = {
        "campaign_id": "cmp_123", "limit": 10, "after": " NEXT ",
        "recommendation_names": " BUDGET_LIMITED, AB_TEST ",
        "recommendation_stages": " MFR, PCR, PFR ",
    } if filtered else {}
    api_version = "v26.0" if filtered else "v25.0"
    result = asyncio.run(_client(api_version=api_version).get_recommendations("123", **options))
    assert result == {"data": []}
    request = FakeAsyncClient.requests[0]
    assert request["method"] == "GET"
    assert request["url"] == f"https://graph.facebook.com/{api_version}/act_123/recommendations"
    params = request["params"]
    if filtered:
        assert params["campaign_id"] == "cmp_123"
        assert params["limit"] == 10
        assert params["after"] == "NEXT"
        assert json.loads(params["recommendation_names"]) == ["BUDGET_LIMITED", "AB_TEST"]
        assert json.loads(params["recommendation_stages"]) == ["MFR", "PCR", "PFR"]
    else:
        assert params == {"limit": 25}


@pytest.mark.parametrize("api_version", ["v25.0", "latest", "", " ", "\t\n"])
@pytest.mark.parametrize("options", [
    {"recommendation_names": ["FRAGMENTATION"]},
    {"recommendation_stages": "PCR"},
    {"recommendation_names": []},
    {"recommendation_stages": []},
])
def test_recommendation_filters_use_client_api_version_before_request(monkeypatch, api_version, options) -> None:
    # A direct client's settings, not the process default, determine its API contract.
    monkeypatch.setenv("META_API_VERSION", "v27.0")
    reload_settings()

    async def unexpected_request(*args, **kwargs):
        pytest.fail("request should not be made")

    monkeypatch.setattr(GraphAPIClient, "request", unexpected_request)
    with pytest.raises(ValidationError, match="require META_API_VERSION=v26.0"):
        asyncio.run(_client(api_version=api_version).get_recommendations("123", **options))


@pytest.mark.parametrize("api_version", ["v26.0", "v27.0"])
def test_recommendation_filters_accept_supported_client_versions(monkeypatch, api_version) -> None:
    monkeypatch.setenv("META_API_VERSION", "v25.0")
    reload_settings()
    FakeAsyncClient.responses = deque([FakeResponse(200, {"data": []})])
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    asyncio.run(_client(api_version=api_version).get_recommendations(
        "123", recommendation_stages=["PCR"],
    ))
    request = FakeAsyncClient.requests[0]
    assert request["url"] == f"https://graph.facebook.com/{api_version}/act_123/recommendations"
    assert json.loads(request["params"]["recommendation_stages"]) == ["PCR"]


@pytest.mark.parametrize("options, expected_message", [
    ({"recommendation_names": [""]}, "nonblank strings"),
    ({"recommendation_names": "BUDGET_LIMITED,"}, "nonblank strings"),
    ({"recommendation_names": [123]}, "nonblank strings"),
    ({"recommendation_stages": ["UNKNOWN"]}, "MFR, PCR, or PFR"),
    ({"recommendation_stages": "MFR,,PFR"}, "nonblank strings"),
])
def test_recommendations_reject_invalid_native_filters_before_request(
    monkeypatch, options, expected_message,
) -> None:
    async def unexpected_request(*args, **kwargs):
        pytest.fail("request should not be made")

    monkeypatch.setattr(GraphAPIClient, "request", unexpected_request)
    with pytest.raises(ValidationError, match=expected_message):
        asyncio.run(_client().get_recommendations("123", **options))


@pytest.mark.parametrize("object_id", ["123", "act_123", "12345678901234567890"])
def test_async_report_posts_only_to_the_insights_edge(monkeypatch, object_id) -> None:
    calls = []

    async def fake_request(self, method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {"report_run_id": "report-1"}

    monkeypatch.setattr(GraphAPIClient, "request", fake_request)
    result = asyncio.run(_client().create_async_insights_report(object_id, fields=["spend"]))

    assert result == {"report_run_id": "report-1"}
    assert calls == [("POST", f"{object_id}/insights", {"data": {"fields": "spend", "async": "true"}})]


@pytest.mark.parametrize("object_id", [
    "123?status=PAUSED#", "act_123?method=delete#", "123#", "123/insights",
    "../123", "123%3Fstatus=PAUSED%23", "123\\456", "１２３", "123\n",
    " 123", "", "act_", "ad_123",
])
def test_async_report_rejects_invalid_identifiers_before_request(monkeypatch, object_id) -> None:
    async def unexpected_request(*args, **kwargs):
        pytest.fail("Invalid identifiers must not reach the request builder")

    monkeypatch.setattr(GraphAPIClient, "request", unexpected_request)
    with pytest.raises(ValidationError, match="numeric ID or act_<numeric>"):
        asyncio.run(_client().create_async_insights_report(object_id, fields=["spend"]))


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


class _RecordingClient(FakeAsyncClient):
    """Records every outbound request so tests can prove none was sent."""

    sent: list[dict[str, object]] = []

    async def request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        _RecordingClient.sent.append(kwargs)
        return FakeResponse(200, {"id": "1"})


@pytest.fixture
def recording_client(monkeypatch):
    _RecordingClient.sent = []
    asyncio.run(close_graph_api_clients())
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", _RecordingClient)
    yield _RecordingClient
    asyncio.run(close_graph_api_clients())


@pytest.mark.parametrize(
    "endpoint",
    [
        "123?method=delete",
        "123/products?method=delete",
        "123#fragment",
        "123/../456",
        "123/./456",
        "123 456",
        "123%3Fmethod%3Ddelete",
        "123//products",
        "123\\products",
        "123&method=delete",
        "123;method=delete",
        "",
    ],
)
def test_request_rejects_endpoints_that_could_change_the_request(recording_client, endpoint) -> None:
    """A caller-supplied id must never be able to add a query string or change the path."""
    with pytest.raises(ValidationError):
        asyncio.run(_client().request("GET", endpoint))
    assert recording_client.sent == []


@pytest.mark.parametrize(
    "endpoint",
    ["me", "act_123", "act_123/insights", "123_456/comments", "oauth/access_token",
     "debug_token", "/act_123/adimages", "17841400000000000/media", "dataset_quality"],
)
def test_request_accepts_ordinary_graph_endpoints(recording_client, endpoint) -> None:
    asyncio.run(_client().request("GET", endpoint))
    assert len(recording_client.sent) == 1


@pytest.mark.parametrize("key", ["method", "METHOD", " Method "])
@pytest.mark.parametrize("http_method,kind", [("GET", "params"), ("POST", "data"), ("POST", "params"), ("DELETE", "params")])
def test_request_rejects_http_method_override(recording_client, key, http_method, kind) -> None:
    """Graph honors a `method` parameter that turns a read into a delete; never send one."""
    with pytest.raises(ValidationError, match="method"):
        asyncio.run(_client().request(http_method, "123", **{kind: {key: "delete"}}))
    assert recording_client.sent == []


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_object("123/products"),
        lambda c: c.update_object("123/products", data={"name": "x"}),
        lambda c: c.delete_object("123/products"),
        lambda c: c.list_objects("123/products", "ads"),
        lambda c: c.list_objects("123", "ads/456"),
        lambda c: c.create_edge_object("123/products", "ads", data={"name": "x"}),
        lambda c: c.create_edge_object("123", "ads/456", data={"name": "x"}),
        lambda c: c.delete_edge_object("123/x", "users", data={"a": 1}),
        lambda c: c.delete_edge_object("123", "users/9", data={"a": 1}),
        lambda c: c.get_insights("act_1/campaigns", fields=["spend"], params={}),
        lambda c: c.create_async_insights_report("act_1/campaigns", fields=["spend"], params={}),
        lambda c: c.get_object(""),
        lambda c: c.get_object("me/accounts"),
    ],
)
def test_node_and_edge_helpers_require_single_segment_ids(recording_client, call) -> None:
    """An id with a slash would turn an update of one object into a write on another edge."""
    with pytest.raises(ValidationError):
        asyncio.run(call(_client()))
    assert recording_client.sent == []


def test_node_and_edge_helpers_accept_ordinary_ids(recording_client) -> None:
    client = _client()
    asyncio.run(client.get_object("act_123", fields=["id"]))
    asyncio.run(client.get_object("me"))
    asyncio.run(client.list_objects("act_123", "ads"))
    asyncio.run(client.update_object("123_456", data={"name": "x"}))
    asyncio.run(client.create_edge_object("act_123", "adcreatives", data={"name": "x"}))
    asyncio.run(client.delete_edge_object("123", "users", data={"a": 1}))
    asyncio.run(client.get_insights("act_123", fields=["spend"], params={}))
    assert len(recording_client.sent) == 7


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.estimate_audience_size(account_id="123/ads", targeting_spec={"geo_locations": {"countries": ["US"]}}),
        lambda c: c.preview_ad(account_id="act_123/ads", creative={"object_story_id": "1_2"}, ad_format="DESKTOP_FEED_STANDARD"),
        lambda c: c.get_ad_images_by_hashes("act_1/ads", hashes=["abc"]),
    ],
)
def test_account_scoped_helpers_reject_an_account_id_with_a_path(recording_client, call) -> None:
    with pytest.raises(ValidationError):
        asyncio.run(call(_client()))
    assert recording_client.sent == []


@pytest.mark.parametrize(
    "code",
    [80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014],
)
def test_business_use_case_throttling_codes_are_rate_limits(monkeypatch, code) -> None:
    """Meta's per-product throttling codes carry messages that do not say "rate limit"."""
    FakeAsyncClient.responses = deque(
        [FakeResponse(400, {"error": {"message": "There have been too many requests for this object.", "code": code}})]
    )
    asyncio.run(close_graph_api_clients())
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(RateLimitError):
        asyncio.run(_client().request("GET", "123/products"))
    asyncio.run(close_graph_api_clients())


def test_an_ordinary_error_code_is_not_a_rate_limit(monkeypatch) -> None:
    FakeAsyncClient.responses = deque(
        [FakeResponse(400, {"error": {"message": "Invalid parameter", "code": 100}})]
    )
    asyncio.run(close_graph_api_clients())
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    with pytest.raises(MetaApiError) as error:
        asyncio.run(_client().request("GET", "123/products"))
    assert not isinstance(error.value, RateLimitError)
    asyncio.run(close_graph_api_clients())


def test_upload_ad_image_sends_checked_bytes_and_never_reads_a_path(monkeypatch) -> None:
    """The client receives a file's name and bytes already checked by the tool; it has no path to read."""
    import inspect

    calls = []

    async def fake_request(self, method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {"images": {}}

    monkeypatch.setattr(GraphAPIClient, "request", fake_request)
    client = _client()
    assert "file_path" not in inspect.signature(GraphAPIClient.upload_ad_image).parameters

    asyncio.run(client.upload_ad_image("123", file_name="hero.png", file_content=b"image-bytes", name="Hero"))
    asyncio.run(client.upload_ad_image("act_123", file_name="hero.png", file_content=b"image-bytes"))
    asyncio.run(client.upload_ad_image("123", image_url="https://example.com/x.png", name="Hero"))

    assert calls == [
        ("POST", "act_123/adimages", {"data": {"name": "Hero"}, "files": {"filename": ("hero.png", b"image-bytes")}}),
        ("POST", "act_123/adimages", {"data": {}, "files": {"filename": ("hero.png", b"image-bytes")}}),
        ("POST", "act_123/adimages", {"data": {"url": "https://example.com/x.png", "name": "Hero"}, "files": None}),
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"file_name": "hero.png"},
        {"file_content": b"image-bytes"},
        {"file_name": "hero.png", "file_content": b""},
        {"file_name": "", "file_content": b"image-bytes"},
        {"file_name": "hero.png", "file_content": b"image-bytes", "image_url": "https://example.com/x.png"},
    ],
)
def test_upload_ad_image_needs_exactly_one_complete_source(monkeypatch, arguments) -> None:
    async def fake_request(self, method, endpoint, **kwargs):
        pytest.fail("Nothing should be sent")

    monkeypatch.setattr(GraphAPIClient, "request", fake_request)
    with pytest.raises(UnsupportedFeatureError):
        asyncio.run(_client().upload_ad_image("123", **arguments))


@pytest.mark.parametrize("body", [None, "ok", "an error word in plain text", 7, 1.5])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_a_json_answer_that_is_not_an_object_is_an_error_not_a_result(monkeypatch, body, method) -> None:
    # Meta documents object answers. A bare null, text or number is unreadable: it must not
    # crash the client (null and numbers raised TypeError), must not reach a caller as
    # something that looks like an object (a read-before-write would then be followed by
    # the write), and after a write it must say the outcome is not known.
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.responses = deque([FakeResponse(200, body)])
    with pytest.raises(MetaApiError) as error:
        asyncio.run(_client().request(method, "123", data=None if method == "GET" else {"name": "x"}))
    public = error.value.to_public_dict()
    assert "not a JSON object" in public["message"]
    assert error.value.status_code == 200
    assert public["mutation_outcome_unknown"] is (method != "GET")
    assert public["retryable"] is False
    assert len(FakeAsyncClient.requests) == 1


@pytest.mark.parametrize("body", [None, "refused", 7])
def test_an_error_status_with_a_non_object_json_answer_is_a_meta_error(monkeypatch, body) -> None:
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.responses = deque([FakeResponse(400, body)])
    with pytest.raises(MetaApiError) as error:
        asyncio.run(_client().request("POST", "123", data={"name": "x"}))
    assert error.value.status_code == 400
    assert error.value.to_public_dict()["mutation_outcome_unknown"] is False


@pytest.mark.parametrize("code", [[], {}, "613", 1.5, True])
def test_an_error_code_of_the_wrong_type_does_not_crash_the_client(monkeypatch, code) -> None:
    # A list code raised TypeError (unhashable) while classifying the error, after a write had gone out.
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.responses = deque([FakeResponse(200, {"error": {"message": "Malformed reply", "code": code, "error_subcode": code}})])
    with pytest.raises(MetaApiError) as error:
        asyncio.run(_client().request("POST", "123", data={"name": "x"}))
    assert error.value.message == "Malformed reply"
    assert error.value.code is None and error.value.subcode is None


def test_bool_and_list_answers_keep_their_existing_wrappers(monkeypatch) -> None:
    monkeypatch.setattr("meta_ads_mcp.graph_api.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.responses = deque([FakeResponse(200, True), FakeResponse(200, [{"id": "1"}])])
    client = _client()
    assert asyncio.run(client.request("GET", "123")) == {"success": True}
    assert asyncio.run(client.request("GET", "123")) == {"data": [{"id": "1"}]}
