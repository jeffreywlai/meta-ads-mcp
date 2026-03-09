"""Shared tool-module registry for all entrypoints."""

from __future__ import annotations

from . import (
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
    targeting,
    utility,
)

TOOL_MODULES = (
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
    targeting,
    utility,
)

__all__ = [
    "TOOL_MODULES",
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
    "targeting",
    "utility",
]
