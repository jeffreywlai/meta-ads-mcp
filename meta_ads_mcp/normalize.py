"""Normalization helpers for Meta Ads responses."""

from __future__ import annotations

from typing import Any

from .pagination import extract_paging


ZERO_DECIMAL_CURRENCIES = {
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "JPY",
    "KMF",
    "KRW",
    "MGA",
    "PYG",
    "RWF",
    "UGX",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
}


def to_float(value: Any) -> float | None:
    """Coerce a value to float when possible."""
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    """Coerce a value to int when possible."""
    numeric = to_float(value)
    return None if numeric is None else int(numeric)


def normalize_budget_value(value: Any, currency: str | None = None) -> float | None:
    """Convert minor-unit budgets to human-readable amounts."""
    numeric = to_float(value)
    if numeric is None:
        return None
    if currency and currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return numeric
    return numeric / 100.0


def action_list_to_map(actions: list[dict[str, Any]] | None) -> dict[str, float]:
    """Convert Meta action arrays to a direct mapping."""
    if not actions:
        return {}
    result: dict[str, float] = {}
    for action in actions:
        action_type = action.get("action_type")
        if not action_type:
            continue
        result[action_type] = to_float(action.get("value")) or 0.0
    return result


def first_present(mapping: dict[str, float], keys: list[str]) -> float | None:
    """Return the first present metric from a candidate list."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def normalize_insights_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single insights row."""
    normalized = dict(row)
    normalized["spend"] = to_float(row.get("spend"))
    normalized["impressions"] = to_int(row.get("impressions"))
    normalized["reach"] = to_int(row.get("reach"))
    normalized["clicks"] = to_int(row.get("clicks"))
    normalized["ctr"] = to_float(row.get("ctr"))
    if normalized["ctr"] is not None and normalized["ctr"] > 1:
        normalized["ctr"] = normalized["ctr"] / 100.0
    normalized["cpc"] = to_float(row.get("cpc"))
    normalized["cpm"] = to_float(row.get("cpm"))
    normalized["frequency"] = to_float(row.get("frequency"))
    normalized["actions_map"] = action_list_to_map(row.get("actions"))
    normalized["action_values_map"] = action_list_to_map(row.get("action_values"))
    normalized["results"] = first_present(
        normalized["actions_map"],
        [
            "purchase",
            "omni_purchase",
            "offsite_conversion.purchase",
            "offsite_conversion.fb_pixel_purchase",
            "onsite_conversion.purchase",
            "lead",
            "onsite_conversion.lead",
            "offsite_conversion.fb_pixel_lead",
        ],
    )
    normalized["result_value"] = first_present(
        normalized["action_values_map"],
        [
            "purchase",
            "omni_purchase",
            "offsite_conversion.purchase",
            "offsite_conversion.fb_pixel_purchase",
            "onsite_conversion.purchase",
        ],
    )
    return normalized


def normalize_collection(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a collection response into the shared envelope."""
    items = payload.get("data", [])
    return {
        "items": items,
        "paging": extract_paging(payload),
        "summary": {"count": len(items)},
    }
