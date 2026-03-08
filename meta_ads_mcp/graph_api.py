"""Direct Meta Graph / Marketing API client."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from .auth import build_app_access_token, build_auth_headers
from .config import Settings, get_settings
from .errors import (
    AsyncJobError,
    MetaApiError,
    NotFoundError,
    RateLimitError,
    UnsupportedFeatureError,
)

USER_AGENT = "meta-ads-fastmcp/0.1.0"


def normalize_account_id(account_id: str) -> str:
    """Ensure account ids use the Graph API act_ prefix."""
    return account_id if account_id.startswith("act_") else f"act_{account_id}"


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
        if use_auth_header:
            headers.update(
                build_auth_headers(self.access_token_override, settings=self.settings)
            )
        url = f"{(base_url or self.base_url).rstrip('/')}/{endpoint.lstrip('/')}"
        retries = self.settings.max_retries + 1
        encoded_params = self._encode_mapping(params)
        encoded_data = self._encode_mapping(data)

        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:
            for attempt in range(retries):
                response = await client.request(
                    method=method,
                    url=url,
                    params=encoded_params,
                    data=encoded_data,
                    headers=headers,
                    files=files,
                )
                if response.status_code == 429:
                    if attempt + 1 < retries:
                        await asyncio.sleep(2**attempt)
                        continue
                    raise RateLimitError("Meta API rate limit reached.")
                if response.status_code == 404:
                    raise NotFoundError(f"Meta object or edge not found: {endpoint}")
                if response.status_code in {501, 503}:
                    if attempt + 1 < retries:
                        await asyncio.sleep(2**attempt)
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
                        raise MetaApiError(
                            message="Non-JSON error response from Meta API",
                            status_code=response.status_code,
                            details=payload,
                        )
                    return payload

                if isinstance(payload, bool):
                    payload = {"success": payload}
                elif isinstance(payload, list):
                    payload = {"data": payload}
                if response.is_error or "error" in payload:
                    error = MetaApiError.from_payload(payload, status_code=response.status_code)
                    if error.status_code == 400 and error.code == 100:
                        raise UnsupportedFeatureError(error.message) from error
                    raise error
                return payload

        raise AsyncJobError(f"Request retries exhausted for endpoint: {endpoint}")

    async def get_object(
        self,
        object_id: str,
        *,
        fields: list[str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch a single object."""
        query = dict(params or {})
        if fields:
            query["fields"] = ",".join(fields)
        return await self.request("GET", object_id, params=query)

    async def list_objects(
        self,
        parent_id: str,
        edge: str,
        *,
        fields: list[str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch an edge collection."""
        query = dict(params or {})
        if fields:
            query["fields"] = ",".join(fields)
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
        fields: list[str],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch insights for an object."""
        query = dict(params or {})
        query["fields"] = ",".join(fields)
        return await self.request("GET", f"{object_id}/insights", params=query)

    async def create_async_insights_report(
        self,
        object_id: str,
        *,
        fields: list[str],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an async insights report."""
        query = dict(params or {})
        query["fields"] = ",".join(fields)
        query["async"] = "true"
        return await self.request("POST", f"{object_id}/insights", data=query)

    async def get_async_report(
        self,
        report_run_id: str,
        *,
        fields: list[str] | None = None,
        limit: int = 100,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Poll report status and fetch results when complete."""
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
        if status.get("async_status") not in {"Job Completed", "COMPLETED"}:
            return {"status": status, "rows": []}

        rows = await self.list_objects(
            report_run_id,
            "insights",
            fields=fields,
            params={"limit": limit, "after": after} if after else {"limit": limit},
        )
        return {"status": status, "rows": rows}

    async def search_interests(
        self,
        *,
        query: str,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search targeting interests."""
        return await self.request(
            "GET",
            "search",
            params={"q": query, "type": "adinterest", "limit": limit},
        )

    async def search_geo_locations(
        self,
        *,
        query: str,
        location_types: list[str] | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search geo locations."""
        params: dict[str, Any] = {
            "type": "adgeolocation",
            "q": query,
            "limit": limit,
        }
        if location_types:
            params["location_types"] = ",".join(location_types)
        return await self.request("GET", "search", params=params)

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
    ) -> dict[str, Any]:
        """List reach frequency predictions."""
        return await self.list_objects(
            normalize_account_id(account_id),
            "reachfrequencypredictions",
            params={"limit": limit},
        )

    async def get_recommendations(
        self,
        account_id: str,
        *,
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch recommendation surfaces when available."""
        params = {"campaign_id": campaign_id} if campaign_id else None
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
        headers = build_auth_headers(access_token or self.access_token_override, settings=self.settings)
        return await self.request(
            "POST",
            f"{system_user_id}/access_tokens",
            data={"business_app": business_app, "scope": scope},
            use_auth_header=False,
            params={"access_token": headers["Authorization"].split(" ", 1)[1]},
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


def get_graph_api_client(access_token_override: str | None = None) -> GraphAPIClient:
    """Return a configured Graph API client."""
    return GraphAPIClient(settings=get_settings(), access_token_override=access_token_override)
