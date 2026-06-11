"""Shared tool-module registry for all entrypoints."""

from __future__ import annotations

from . import (
    activity,
    ads,
    audiences,
    auth_tools,
    campaigns,
    creatives,
    diagnostics,
    discovery,
    docs,
    execution,
    insights,
    recommendations,
    research,
    social_feedback,
    targeting,
    utility,
)

TOOL_MODULES = (
    activity,
    ads,
    audiences,
    auth_tools,
    campaigns,
    creatives,
    diagnostics,
    discovery,
    docs,
    execution,
    insights,
    recommendations,
    research,
    social_feedback,
    targeting,
    utility,
)

__all__ = [
    "TOOL_MODULES",
    "activity",
    "ads",
    "audiences",
    "auth_tools",
    "campaigns",
    "creatives",
    "diagnostics",
    "discovery",
    "docs",
    "execution",
    "insights",
    "recommendations",
    "research",
    "social_feedback",
    "targeting",
    "utility",
]
