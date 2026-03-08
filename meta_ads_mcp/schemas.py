"""Shared response builders."""

from __future__ import annotations

from typing import Any


def collection_response(
    items: list[dict[str, Any]],
    *,
    paging: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a collection envelope."""
    return {
        "items": items,
        "paging": paging or {"before": None, "after": None, "next": None},
        "summary": summary or {"count": len(items)},
    }


def analysis_response(
    *,
    scope: dict[str, Any],
    metrics: dict[str, Any],
    findings: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    suggestions: list[str] | None = None,
    missing_signals: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an analysis envelope."""
    payload = {
        "scope": scope,
        "metrics": metrics,
        "findings": findings or [],
        "evidence": evidence or [],
        "suggestions": suggestions or [],
        "missing_signals": missing_signals or [],
    }
    if extra:
        payload.update(extra)
    return payload


def mutation_response(
    *,
    action: str,
    target: dict[str, Any],
    previous: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    ok: bool = True,
) -> dict[str, Any]:
    """Build a mutation envelope."""
    return {
        "ok": ok,
        "action": action,
        "target": target,
        "previous": previous or {},
        "current": current or {},
    }

