"""Pagination helper tests."""

from __future__ import annotations

from meta_ads_mcp.pagination import extract_paging


def test_extract_paging_handles_full_graph_payload() -> None:
    result = extract_paging(
        {
            "paging": {
                "cursors": {"before": "before_1", "after": "after_1"},
                "next": "https://graph.facebook.com/next",
            }
        }
    )
    assert result == {
        "before": "before_1",
        "after": "after_1",
        "next": "https://graph.facebook.com/next",
    }


def test_extract_paging_defaults_missing_values_to_none() -> None:
    assert extract_paging({}) == {"before": None, "after": None, "next": None}
