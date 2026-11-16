"""Shared tool-module registry for all entrypoints."""

from __future__ import annotations

from . import (
    activity,
    ads,
    assets,
    audiences,
    auth_tools,
    business_access,
    campaigns,
    catalogs,
    catalog_feeds,
    creatives,
    creative_formats,
    datasets,
    diagnostics,
    discovery,
    docs,
    execution,
    experiments,
    insights,
    performance_signals,
    recommendations,
    research,
    social_feedback,
    targeting,
    utility,
)

TOOL_MODULES = (
    activity,
    ads,
    assets,
    audiences,
    auth_tools,
    business_access,
    campaigns,
    catalogs,
    catalog_feeds,
    creatives,
    creative_formats,
    datasets,
    diagnostics,
    discovery,
    docs,
    execution,
    experiments,
    insights,
    performance_signals,
    recommendations,
    research,
    social_feedback,
    targeting,
    utility,
)

def _apply_id_formats() -> None:
    """After every tool is registered: show each checked id's pattern in its input schema."""
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.id_formats import apply_id_formats

    apply_id_formats(mcp_server)


_apply_id_formats()

__all__ = [
    "TOOL_MODULES",
    "activity",
    "ads",
    "assets",
    "audiences",
    "auth_tools",
    "business_access",
    "campaigns",
    "catalogs",
    "catalog_feeds",
    "creatives",
    "creative_formats",
    "datasets",
    "diagnostics",
    "discovery",
    "docs",
    "execution",
    "experiments",
    "insights",
    "performance_signals",
    "recommendations",
    "research",
    "social_feedback",
    "targeting",
    "utility",
]
