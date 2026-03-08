"""Pagination helpers."""

from __future__ import annotations

from typing import Any


def extract_paging(payload: dict[str, Any]) -> dict[str, str | None]:
    """Normalize Graph API paging information."""
    paging = payload.get("paging", {})
    cursors = paging.get("cursors", {})
    return {
        "before": cursors.get("before"),
        "after": cursors.get("after"),
        "next": paging.get("next"),
    }

