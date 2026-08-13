"""Direct Meta Graph / Marketing API client."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from .auth import build_app_access_token, build_appsecret_proof, resolve_access_token
from .config import Settings, get_settings
from .errors import (
    AsyncJobError,
    MetaApiError,
    NotFoundError,
    RateLimitError,
    UnsupportedFeatureError,
)
from .tool_types import FieldList, coerce_csv_string_list

USER_AGENT = "meta-ads-fastmcp/0.1.0"
_CLIENT_POOL: dict[tuple[asyncio.AbstractEventLoop, str, str | None, float], httpx.AsyncClient] = {}
SAFE_RETRY_METHODS = {"GET", "HEAD", "OPTIONS"}
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
MAX_AUTOMATIC_RETRY_DELAY_SECONDS = 60.0
USAGE_HEADER_NAMES = (
    "x-ad-account-usage",
    "x-app-usage",
    "x-business-use-case-usage",
)


def normalize_account_id(account_id: str) -> str:
    """Ensure account ids use the Graph API act_ prefix."""
    return account_id if account_id.startswith("act_") else f"act_{account_id}"


def _serialize_fields(fields: FieldList | None) -> str | None:
    """Serialize either validated field arrays or direct CSV client input."""
    normalized_fields = coerce_csv_string_list(fields)
    if not normalized_fields:
        return None
    return ",".join(normalized_fields)


def _normalize_cursor(after: str | None) -> str | None:
    """Treat blank model-supplied cursors as omitted."""
    if after is None:
        return None
    return after.strip() or None


def _is_unsupported_surface_error(error: MetaApiError) -> bool:
    """Decide whether a Graph API error is best treated as an unavailable surface."""
    message = error.message.lower()
    if error.status_code == 400 and error.code == 100:
        return message.startswith("unsupported get request") or message.startswith("unsupported post request")
    if error.status_code == 400 and error.code == 2500:
        return message.startswith("unknown path components")
    return False


def _is_rate_limit_error(error: MetaApiError) -> bool:
    """Decide whether a Graph API error is actually throttling."""
    message = error.message.lower()
    if error.code in {4, 17, 32, 613}:
        return True
    return "rate limit" in message or "limit reached" in message or "too many calls" in message


def _header_value(headers: Any, name: str) -> str | None:
    """Read a response header from httpx or simple test mappings."""
    value = headers.get(name)
    if value is not None:
        return str(value)
    lowered = name.lower()
    for key, candidate in headers.items():
        if str(key).lower() == lowered:
            return str(candidate)
    return None


@dataclass(frozen=True, slots=True)
class RetryTiming:
    """Separate upstream retry guidance from the automatic wait budget."""

    delay_seconds: float
    server_retry_after_seconds: float | None

    @property
    def can_automatically_wait(self) -> bool:
        return self.delay_seconds <= MAX_AUTOMATIC_RETRY_DELAY_SECONDS


def _parse_retry_after_seconds(
    raw_value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse RFC Retry-After delay-seconds or HTTP-date without truncation."""
    if raw_value is None:
        return None
    try:
        numeric = float(raw_value.strip())
    except ValueError:
        numeric = None
    if numeric is not None:
        return numeric if math.isfinite(numeric) and numeric >= 0 else None

    try:
        retry_at = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (retry_at - current).total_seconds())


def _retry_timing(response: Any, attempt: int) -> RetryTiming:
    """Build full public guidance plus a bounded automatic retry decision."""
    server_delay = _parse_retry_after_seconds(
        _header_value(response.headers, "retry-after")
    )
    delay = (
        server_delay
        if server_delay is not None
        else float(min(2**attempt, MAX_AUTOMATIC_RETRY_DELAY_SECONDS))
    )
    return RetryTiming(
        delay_seconds=delay,
        server_retry_after_seconds=server_delay,
    )


def _usage_headers(response: Any) -> dict[str, Any] | None:
    """Return parsed Meta usage headers without exposing unrelated headers."""
    usage: dict[str, Any] = {}
    for name in USAGE_HEADER_NAMES:
        raw_value = _header_value(response.headers, name)
        if raw_value is None:
            continue
        try:
            usage[name] = json.loads(raw_value)
        except json.JSONDecodeError:
            usage[name] = raw_value
    return usage or None


@dataclass(slots=True)
class GraphAPIClient:
    """Thin async client around the Marketing API."""

    settings: Settings
    access_token_override: str | None = None

    @property
    def base_url(self) -> str:
        """Return the base Graph API URL."""
        return f"https://graph.facebook.com/{self.settings.api_version}"

    @staticmethod
    def _encode_value(value: Any) -> Any:
        """Convert Graph payload values into transport-safe forms."""
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return value

    def _encode_mapping(self, mapping: dict[str, Any] | None) -> dict[str, Any] | None:
        """Encode dict/list/bool values for querystring or form data."""
        if mapping is None:
            return None
        return {key: self._encode_value(value) for key, value in mapping.items()}

    def _client_key(
        self,
        *,
        base_url: str | None = None,
    ) -> tuple[asyncio.AbstractEventLoop, str, str | None, float]:
        """Build the shared-client pool key."""
        loop = asyncio.get_running_loop()
        return (
            loop,
            (base_url or self.base_url).rstrip("/"),
            self.access_token_override or self.settings.access_token,
            self.settings.request_timeout,
        )

    def _get_shared_client(self, *, base_url: str | None = None) -> httpx.AsyncClient:
        """Return a pooled async client for repeated Graph API calls."""
        key = self._client_key(base_url=base_url)
        client = _CLIENT_POOL.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=self.settings.request_timeout, http2=True)
            _CLIENT_POOL[key] = client
        return client

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        base_url: str | None = None,
        use_auth_header: bool = True,
    ) -> dict[str, Any]:
        """Make a Graph API request with basic retries."""
        headers = {"User-Agent": USER_AGENT}
        effective_access_token: str | None = None
        if use_auth_header:
            effective_access_token = resolve_access_token(
                self.access_token_override,
                settings=self.settings,
            )
            headers["Authorization"] = f"Bearer {effective_access_token}"
        url = f"{(base_url or self.base_url).rstrip('/')}/{endpoint.lstrip('/')}"
        retries = self.settings.max_retries + 1
        encoded_params = self._encode_mapping(params)
        if effective_access_token is not None and self.settings.app_secret:
            encoded_params = dict(encoded_params or {})
            encoded_params["appsecret_proof"] = build_appsecret_proof(
                effective_access_token,
                self.settings.app_secret,
            )
        encoded_data = self._encode_mapping(data)

        client = self._get_shared_client(base_url=base_url)
        normalized_method = method.upper()
        operation = f"{normalized_method} /{endpoint.lstrip('/')}"
        safe_to_retry = normalized_method in SAFE_RETRY_METHODS
        for attempt in range(retries):
            try:
                response = await client.request(
                    method=normalized_method,
                    url=url,
                    params=encoded_params,
                    data=encoded_data,
                    headers=headers,
                    files=files,
                )
            except httpx.RequestError as exc:
                delay = float(min(2**attempt, 60))
                if safe_to_retry and attempt + 1 < retries:
                    await asyncio.sleep(delay)
                    continue
                raise MetaApiError(
                    message="Meta API request failed before a response was received.",
                    operation=operation,
                    is_transient=True,
                    mutation_outcome_unknown=not safe_to_retry,
                    details={"exception_type": type(exc).__name__},
                ) from exc

            retry_timing = _retry_timing(response, attempt)
            delay = retry_timing.delay_seconds
            usage = _usage_headers(response)
            if response.status_code == 404:
                raise NotFoundError(f"Meta object or edge not found: {endpoint}")
            if (
                response.status_code in TRANSIENT_HTTP_STATUSES
                and safe_to_retry
                and attempt + 1 < retries
                and retry_timing.can_automatically_wait
            ):
                await asyncio.sleep(delay)
                continue

            try:
                payload = response.json()
            except ValueError:
                payload = {
                    "text_response": response.text,
                    "content_type": response.headers.get("content-type"),
                    "status_code": response.status_code,
                }
                if response.is_error:
                    if response.status_code == 429:
                        if (
                            safe_to_retry
                            and attempt + 1 < retries
                            and retry_timing.can_automatically_wait
                        ):
                            await asyncio.sleep(delay)
                            continue
                        raise RateLimitError(
                            "Meta API rate limit reached.",
                            retry_after_seconds=delay,
                            usage=usage,
                            operation=operation,
                        )
                    raise MetaApiError(
                        message="Non-JSON error response from Meta API",
                        status_code=response.status_code,
                        details=payload,
                        operation=operation,
                        is_transient=response.status_code in TRANSIENT_HTTP_STATUSES,
                        retry_after_seconds=(
                            retry_timing.server_retry_after_seconds
                        ),
                        mutation_outcome_unknown=(
                            not safe_to_retry
                            and response.status_code in TRANSIENT_HTTP_STATUSES
                        ),
                    )
                return payload

            if isinstance(payload, bool):
                payload = {"success": payload}
            elif isinstance(payload, list):
                payload = {"data": payload}
            if response.is_error or "error" in payload:
                error = MetaApiError.from_payload(payload, status_code=response.status_code)
                error.operation = operation
                error.retry_after_seconds = retry_timing.server_retry_after_seconds
                error.is_transient = bool(
                    error.is_transient
                    or response.status_code in TRANSIENT_HTTP_STATUSES
                )
                error.mutation_outcome_unknown = (
                    not safe_to_retry
                    and bool(error.is_transient)
                )
                if response.status_code == 429 or _is_rate_limit_error(error):
                    if (
                        safe_to_retry
                        and attempt + 1 < retries
                        and retry_timing.can_automatically_wait
                    ):
                        await asyncio.sleep(delay)
                        continue
                    raise RateLimitError(
                        error.message,
                        retry_after_seconds=delay,
                        code=error.code,
                        subcode=error.subcode,
                        usage=usage,
                        operation=operation,
                    ) from error
                if _is_unsupported_surface_error(error):
                    raise UnsupportedFeatureError(error.message) from error
                if (
                    error.is_transient
                    and safe_to_retry
                    and attempt + 1 < retries
                    and retry_timing.can_automatically_wait
                ):
                    await asyncio.sleep(delay)
                    continue
                raise error
            return payload

        raise AsyncJobError(f"Request retries exhausted for endpoint: {endpoint}")

    async def get_object(
        self,
        object_id: str,
        *,
        fields: FieldList | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch a single object."""
        query = dict(params or {})
        if serialized_fields := _serialize_fields(fields):
            query["fields"] = serialized_fields
        return await self.request("GET", object_id, params=query)

    async def list_objects(
        self,
        parent_id: str,
        edge: str,
        *,
        fields: FieldList | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch an edge collection."""
        query = dict(params or {})
        if serialized_fields := _serialize_fields(fields):
            query["fields"] = serialized_fields
        return await self.request("GET", f"{parent_id}/{edge}", params=query)

    async def update_object(
        self,
        object_id: str,
        *,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Send an object update."""
        return await self.request("POST", object_id, data=data)

    async def create_edge_object(
        self,
        parent_id: str,
        edge: str,
        *,
        data: dict[str, Any],
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an object on a collection edge."""
        return await self.request("POST", f"{parent_id}/{edge}", data=data, files=files)

    async def delete_object(self, object_id: str) -> dict[str, Any]:
        """Delete an object."""
        return await self.request("DELETE", object_id)

    async def get_insights(
        self,
        object_id: str,
        *,
        fields: FieldList,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch insights for an object."""
        query = dict(params or {})
        query["fields"] = _serialize_fields(fields) or ""
        return await self.request("GET", f"{object_id}/insights", params=query)

    async def create_async_insights_report(
        self,
        object_id: str,
        *,
        fields: FieldList,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an async insights report."""
        query = dict(params or {})
        query["fields"] = _serialize_fields(fields) or ""
        query["async"] = "true"
        return await self.request("POST", f"{object_id}/insights", data=query)

    async def get_async_report(
        self,
        report_run_id: str,
        *,
        fields: FieldList | None = None,
        limit: int = 100,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Poll report status and fetch results when complete."""
        cursor = _normalize_cursor(after)
        status = await self.get_object(
            report_run_id,
            fields=[
                "id",
                "async_status",
                "async_percent_completion",
                "error_code",
                "error_message",
                "error_subcode",
                "error_user_title",
                "error_user_msg",
            ],
        )
        normalized_status = " ".join(
            str(status.get("async_status") or "").replace("_", " ").lower().split()
        )
        if normalized_status not in {"job completed", "completed"}:
            return {"status": status, "rows": []}

        rows = await self.list_objects(
            report_run_id,
            "insights",
            fields=fields,
            params={"limit": limit, "after": cursor} if cursor else {"limit": limit},
        )
        return {"status": status, "rows": rows}

    async def search_interests(
        self,
        *,
        query: str,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Search targeting interests."""
        cursor = _normalize_cursor(after)
        return await self.request(
            "GET",
            "search",
            params={
                "q": query,
                "type": "adinterest",
                "limit": limit,
                **({"after": cursor} if cursor else {}),
            },
        )

    async def get_interest_suggestions(
        self,
        *,
        interest_list: list[str],
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Fetch interest suggestions related to seed interests."""
        cursor = _normalize_cursor(after)
        return await self.request(
            "GET",
            "search",
            params={
                "type": "adinterestsuggestion",
                "interest_list": interest_list,
                "limit": limit,
                **({"after": cursor} if cursor else {}),
            },
        )

    async def validate_interests(
        self,
        *,
        interest_list: list[str] | None = None,
        interest_ids: list[str] | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Validate interest names or ids against Meta's targeting search."""
        cursor = _normalize_cursor(after)
        params: dict[str, Any] = {"type": "adinterestvalid"}
        if interest_list:
            params["interest_list"] = interest_list
        if interest_ids:
            params["interest_fbid_list"] = interest_ids
        if cursor:
            params["after"] = cursor
        return await self.request("GET", "search", params=params)

    async def search_geo_locations(
        self,
        *,
        query: str,
        location_types: list[str] | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Search geo locations."""
        cursor = _normalize_cursor(after)
        params: dict[str, Any] = {
            "type": "adgeolocation",
            "q": query,
            "limit": limit,
        }
        if location_types:
            params["location_types"] = ",".join(location_types)
        if cursor:
            params["after"] = cursor
        return await self.request("GET", "search", params=params)

    async def search_targeting_categories(
        self,
        *,
        account_id: str,
        category_class: str,
        query: str | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Search targeting categories such as behaviors or demographics."""
        cursor = _normalize_cursor(after)
        params: dict[str, Any] = {
            "class": category_class,
            "limit": limit,
        }
        if query:
            params["query"] = query
        if cursor:
            params["after"] = cursor
        return await self.request(
            "GET",
            f"{normalize_account_id(account_id)}/broadtargetingcategories",
            params=params,
        )

    async def estimate_audience_size(
        self,
        account_id: str,
        *,
        targeting_spec: dict[str, Any],
        optimization_goal: str | None = None,
    ) -> dict[str, Any]:
        """Fetch reach estimate data for a targeting spec."""
        return await self.request(
            "GET",
            f"{normalize_account_id(account_id)}/reachestimate",
            params={
                "targeting_spec": json.dumps(targeting_spec),
                **({"optimization_goal": optimization_goal} if optimization_goal else {}),
            },
        )

    async def get_reach_frequency_predictions(
        self,
        account_id: str,
        *,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """List reach frequency predictions."""
        cursor = _normalize_cursor(after)
        return await self.list_objects(
            normalize_account_id(account_id),
            "reachfrequencypredictions",
            params={
                "limit": limit,
                **({"after": cursor} if cursor else {}),
            },
        )

    async def get_recommendations(
        self,
        account_id: str,
        *,
        campaign_id: str | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Fetch recommendation surfaces when available."""
        cursor = _normalize_cursor(after)
        params: dict[str, Any] = {"limit": limit}
        if campaign_id:
            params["campaign_id"] = campaign_id
        if cursor:
            params["after"] = cursor
        return await self.list_objects(
            normalize_account_id(account_id),
            "recommendations",
            params=params,
        )

    async def oauth_access_token(self, params: dict[str, Any]) -> dict[str, Any]:
        """Exchange OAuth credentials or codes for access tokens."""
        return await self.request(
            "GET",
            "oauth/access_token",
            params=params,
            base_url=self.base_url,
            use_auth_header=False,
        )

    async def debug_token(
        self,
        *,
        input_token: str,
        debug_access_token: str | None = None,
    ) -> dict[str, Any]:
        """Inspect token metadata via debug_token."""
        params = {
            "input_token": input_token,
            "access_token": debug_access_token
            or build_app_access_token(settings=self.settings),
        }
        return await self.request("GET", "debug_token", params=params, use_auth_header=False)

    async def generate_system_user_token(
        self,
        system_user_id: str,
        *,
        business_app: str,
        scope: list[str],
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Request a system user token."""
        effective_access_token = resolve_access_token(
            access_token or self.access_token_override,
            settings=self.settings,
        )
        params = {"access_token": effective_access_token}
        if self.settings.app_secret:
            params["appsecret_proof"] = build_appsecret_proof(
                effective_access_token,
                self.settings.app_secret,
            )
        return await self.request(
            "POST",
            f"{system_user_id}/access_tokens",
            data={"business_app": business_app, "scope": scope},
            use_auth_header=False,
            params=params,
        )

    async def preview_ad(
        self,
        *,
        ad_id: str | None = None,
        account_id: str | None = None,
        creative_id: str | None = None,
        creative: dict[str, Any] | None = None,
        ad_format: str = "DESKTOP_FEED_STANDARD",
    ) -> dict[str, Any]:
        """Generate an ad preview from an ad or creative."""
        if ad_id:
            return await self.list_objects(ad_id, "previews", params={"ad_format": ad_format})
        if not account_id:
            raise UnsupportedFeatureError("account_id is required when previewing from creative input.")
        params: dict[str, Any] = {"ad_format": ad_format}
        if creative_id:
            params["creative_id"] = creative_id
        if creative:
            params["creative"] = creative
        return await self.request(
            "GET",
            f"{normalize_account_id(account_id)}/generatepreviews",
            params=params,
        )

    async def upload_ad_image(
        self,
        account_id: str,
        *,
        file_path: str | None = None,
        image_url: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Upload an image asset for creative use."""
        if bool(file_path) == bool(image_url):
            raise UnsupportedFeatureError("Provide exactly one of file_path or image_url.")
        data: dict[str, Any] = {}
        files: dict[str, Any] | None = None
        if image_url:
            data["url"] = image_url
        if file_path:
            path = Path(file_path).expanduser()
            files = {"filename": (path.name, path.read_bytes())}
        if name:
            data["name"] = name
        return await self.create_edge_object(normalize_account_id(account_id), "adimages", data=data, files=files)

    async def get_ad_images_by_hashes(
        self,
        account_id: str,
        *,
        hashes: list[str],
        fields: FieldList | None = None,
    ) -> dict[str, Any]:
        """Resolve ad image hashes into hosted image metadata."""
        params: dict[str, Any] = {"hashes": hashes}
        if serialized_fields := _serialize_fields(fields):
            params["fields"] = serialized_fields
        return await self.request(
            "GET",
            f"{normalize_account_id(account_id)}/adimages",
            params=params,
        )

    async def search_ads_archive(
        self,
        *,
        search_terms: str,
        ad_reached_countries: list[str],
        ad_type: str = "ALL",
        limit: int = 25,
        fields: FieldList | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Search the public Ads Library / archive endpoint."""
        cursor = _normalize_cursor(after)
        params: dict[str, Any] = {
            "search_terms": search_terms,
            "ad_reached_countries": ad_reached_countries,
            "ad_type": ad_type,
            "limit": limit,
        }
        if serialized_fields := _serialize_fields(fields):
            params["fields"] = serialized_fields
        if cursor:
            params["after"] = cursor
        return await self.request("GET", "ads_archive", params=params)


def get_graph_api_client(access_token_override: str | None = None) -> GraphAPIClient:
    """Return a configured Graph API client."""
    return GraphAPIClient(settings=get_settings(), access_token_override=access_token_override)


async def close_graph_api_clients() -> None:
    """Close all pooled async HTTP clients."""
    clients = list(_CLIENT_POOL.values())
    _CLIENT_POOL.clear()
    for client in clients:
        if not client.is_closed:
            await client.aclose()
