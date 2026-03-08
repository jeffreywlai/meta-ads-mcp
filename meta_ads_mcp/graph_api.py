"""Direct Meta Graph / Marketing API client."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
from typing import Any

import httpx

from .auth import build_auth_headers
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

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a Graph API request with basic retries."""
        headers = {
            **build_auth_headers(self.access_token_override, settings=self.settings),
            "User-Agent": USER_AGENT,
        }
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        retries = self.settings.max_retries + 1

        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:
            for attempt in range(retries):
                response = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    headers=headers,
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

                payload = response.json()
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
            fields=["id", "async_status", "async_percent_completion"],
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
        account_id: str,
        *,
        query: str,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search targeting interests."""
        return await self.list_objects(
            normalize_account_id(account_id),
            "targetingsearch",
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


def get_graph_api_client(access_token_override: str | None = None) -> GraphAPIClient:
    """Return a configured Graph API client."""
    return GraphAPIClient(settings=get_settings(), access_token_override=access_token_override)
