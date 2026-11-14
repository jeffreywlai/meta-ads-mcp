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


# The values in a create's target that are Graph objects the new object can never be: the account
# or business it belongs to and the parent it is created under. Nothing else in target is
# compared. A target also holds values that are not Graph ids at all (a product's retailer_id
# is the merchant's own SKU, and a numeric SKU can equal the product's Graph id), and comparing
# those refused an ordinary answer (review R35, GG2).
_PARENT_TARGET_KEYS = frozenset({"account_id", "business_id", "catalog_id", "campaign_id", "adset_id", "feed_id"})


def _cannot_be_the_new_object(named: Any, target: dict[str, Any]) -> bool:
    """Whether the id in a create's answer cannot be the new object's id.

    True for a value that is not an id at all (true or false, a container, a fraction, a
    negative number) and for an id that is the account, business or parent named in target
    (_PARENT_TARGET_KEYS). Any other text or whole number is taken as the id, as before: this
    does not judge the form of an id's text, and it compares with no other value in target.
    """
    from meta_ads_mcp.normalize import same_object_id

    if isinstance(named, bool) or not isinstance(named, (str, int)):
        return True
    if isinstance(named, int) and named < 0:
        return True
    return any(same_object_id(named, target.get(key)) for key in _PARENT_TARGET_KEYS)


def creation_response(
    *,
    action: str,
    target: dict[str, Any],
    result: dict[str, Any],
    validate_only: bool = False,
) -> dict[str, Any]:
    """Build a stable creation or validation-only mutation envelope.

    created holds Meta's answer. When that answer names, as the new object's id, something that
    cannot be it (a value that is not an id, or an id the call already knows as another object:
    one in target), nothing is shown as created: created is null, created_id_unresolved is true,
    Meta's answer is kept under unresolved_result and a warning says the object may exist. An
    answer with no id at all is shown as before.
    """
    response: dict[str, Any] = {
        "ok": True,
        "action": action,
        "target": target,
        "validation_only": validate_only,
        "created": None if validate_only else result,
    }
    if validate_only:
        response["validation"] = result
        return response
    named = result.get("id") if isinstance(result, dict) else None
    if named is not None and named != "" and _cannot_be_the_new_object(named, target):
        response["created"] = None
        response["created_id_unresolved"] = True
        response["unresolved_result"] = result
        response["warning"] = (
            "Meta accepted the request, but its answer did not give a usable id for the new object "
            "(it was not an id, or it was the id of an object this call already names). The object "
            "may have been created; look for it before sending the request again."
        )
    return response
