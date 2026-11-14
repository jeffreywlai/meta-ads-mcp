"""Normalization helpers for Meta Ads responses."""

from __future__ import annotations

from typing import Any

from .money import ZERO_DECIMAL_CURRENCIES as ZERO_DECIMAL_CURRENCIES
from .money import from_minor_units
from .pagination import extract_paging


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


def blank_to_none(value: str | None) -> str | None:
    """Treat blank optional strings as omitted."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize_budget_value(value: Any, currency: str | None = None) -> float | None:
    """Convert minor-unit budgets to human-readable amounts."""
    return from_minor_units(value, currency, field_name="budget")


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
    if normalized["ctr"] is not None:
        # Meta reports CTR as a percentage, including values at or below 1%.
        normalized["ctr"] = normalized["ctr"] / 100.0
    normalized["cpc"] = to_float(row.get("cpc"))
    normalized["cpm"] = to_float(row.get("cpm"))
    normalized["frequency"] = to_float(row.get("frequency"))
    if "instagram_profile_follow" in row:
        normalized["instagram_profile_follow"] = to_int(
            row.get("instagram_profile_follow")
        )
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


def read_collection_page(payload: Any) -> tuple[list[dict[str, Any]], int, bool]:
    """A collection page's rows that are objects, how many entries are not, and whether the page could not be read.

    Only a data list is a readable page, and an empty list is an empty page. A payload that
    is not an object, or whose data is missing, null or not a list, is an unreadable page:
    rows may have been there, so it must never pass as empty, as complete, or as the last
    page. Use this wherever what a read found, or did not find, decides something: whether
    to write, whether a list is complete, or whether "none found" is true.
    normalize_collection is the lenient shaping helper for showing a page as it came.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return [], 0, True
    data = payload["data"]
    rows = [row for row in data if isinstance(row, dict)]
    return rows, len(data) - len(rows), False


def collection_fault(payload: Any) -> str | None:
    """Why a collection page cannot be called complete, or None when every entry was read.

    A page whose data list is missing, or that holds an entry that is not an object, may
    hold rows that were not read. A tool that shapes such a page must say the result is
    incomplete and give this reason; it must not report an empty or complete list.
    """
    _, unreadable_rows, page_unreadable = read_collection_page(payload)
    if page_unreadable:
        return "Meta's answer had no list of results, so what it holds is not known."
    if unreadable_rows:
        entries = "1 entry" if unreadable_rows == 1 else f"{unreadable_rows} entries"
        return f"{entries} in Meta's answer could not be read, so the list may be missing rows."
    return None


def normalize_collection(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a collection response into the shared envelope."""
    items = payload.get("data", [])
    return {
        "items": items,
        "paging": extract_paging(payload),
        "summary": {"count": len(items)},
    }


def _plain_id(value: Any) -> str | None:
    """The digits of a Graph id given as text or a whole number, without an act_ prefix; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("act_"):
        text = text[4:]
    return text if text.isdigit() else None


def names_other_object(requested_id: Any, answer: Any, *, key: str = "id") -> bool:
    """True only when the answer carries a readable id under key that is a different object.

    For a tool about to act on Meta's answer about one object (or on the id of an object it just
    created): an answer that explicitly identifies another object is not about this one. The
    comparison is by value, so "123", 123 and act_123 are the same id. It is never a mismatch
    when the answer is not an object, has no id under key, or either id is not a plain id (an
    alias such as me, a boolean, a container): a missing or unusual id is not evidence of
    another object. Read live on 2026-11-14: accounts, campaigns, ad sets, ads, creatives,
    audiences and a study each answered a read with the id asked for.
    """
    if not isinstance(answer, dict):
        return False
    asked, named = _plain_id(requested_id), _plain_id(answer.get(key))
    return asked is not None and named is not None and asked != named
