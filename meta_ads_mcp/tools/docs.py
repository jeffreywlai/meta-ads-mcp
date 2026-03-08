"""Documentation tools and resources."""

from __future__ import annotations

from importlib.resources import files

from meta_ads_mcp.coordinator import mcp_server


def _read_doc(name: str) -> str:
    """Read a packaged markdown context file."""
    return files("meta_ads_mcp").joinpath("context", name).read_text(encoding="utf-8")


@mcp_server.resource(uri="meta://docs/object-model")
def resource_object_model() -> str:
    """Return the object model resource."""
    return _read_doc("object_model.md")


@mcp_server.resource(uri="meta://docs/insights-metrics")
def resource_insights_metrics() -> str:
    """Return the metrics reference resource."""
    return _read_doc("insights_metrics.md")


@mcp_server.resource(uri="meta://docs/v25-notes")
def resource_v25_notes() -> str:
    """Return v25 notes."""
    return _read_doc("v25_notes.md")


@mcp_server.resource(uri="meta://docs/optimization-playbook")
def resource_optimization_playbook() -> str:
    """Return optimization playbook notes."""
    return _read_doc("optimization_playbook.md")


@mcp_server.tool()
async def get_meta_object_model() -> dict[str, str]:
    """Use this when Claude needs a refresher on how accounts, campaigns, ad sets, and ads relate."""
    return {"name": "object_model", "content": _read_doc("object_model.md")}


@mcp_server.tool()
async def get_metrics_reference() -> dict[str, str]:
    """Use this when Claude needs metric or breakdown reference help before building an insights query."""
    return {"name": "insights_metrics", "content": _read_doc("insights_metrics.md")}


@mcp_server.tool()
async def get_v25_notes() -> dict[str, str]:
    """Use this when Claude needs v25-specific implementation notes or deprecation guidance."""
    return {"name": "v25_notes", "content": _read_doc("v25_notes.md")}


@mcp_server.tool()
async def get_optimization_playbook() -> dict[str, str]:
    """Use this when Claude wants packaged optimization heuristics before interpreting account performance."""
    return {"name": "optimization_playbook", "content": _read_doc("optimization_playbook.md")}
