"""Recommendation and opportunity tools."""

from __future__ import annotations

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import normalize_collection


def _resolve_account_id(account_id: str | None) -> str:
    """Resolve account id from input or default config."""
    if account_id:
        return normalize_account_id(account_id)
    if get_settings().default_account_id:
        return normalize_account_id(get_settings().default_account_id)
    raise ValidationError("account_id is required when META_DEFAULT_ACCOUNT_ID is not set.")


@mcp_server.tool()
async def get_recommendations(
    account_id: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, object]:
    """Use this when the user wants Meta-native recommendations or opportunity surfaces for an account or campaign."""
    client = get_graph_api_client()
    try:
        payload = await client.get_recommendations(
            _resolve_account_id(account_id),
            campaign_id=campaign_id,
        )
    except UnsupportedFeatureError as exc:
        return {
            "supported": False,
            "reason": str(exc),
            "items": [],
            "summary": {"count": 0},
        }
    return {"supported": True, **normalize_collection(payload)}
