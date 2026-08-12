"""Pure validation for change-history scope arguments.

Both the runtime tool and exact-call router use this module so dependency and
agreement rules cannot diverge.  Graph lookups and configured defaults remain
in the activity tool; this module only validates explicitly supplied values.
"""

from __future__ import annotations

from dataclasses import dataclass


LEVEL_ALIASES = {
    "account": "account",
    "campaign": "campaign",
    "adset": "adset",
    "ad_set": "adset",
    "adgroup": "adset",
    "ad_group": "adset",
    "ad": "ad",
}


@dataclass(frozen=True, slots=True)
class ActivityScopeArguments:
    """Normalized, structurally valid explicit scope arguments."""

    level: str | None
    object_id: str | None
    account_id: str | None
    object_alias: tuple[str, str] | None


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _canonical_account_id(value: str) -> str:
    stripped = value.strip()
    return stripped[4:] if stripped.startswith("act_") else stripped


def normalize_activity_level(level: str | None) -> str | None:
    """Normalize public level aliases while preserving omission."""
    normalized_input = _blank_to_none(level)
    if normalized_input is None:
        return None
    normalized = LEVEL_ALIASES.get(normalized_input.lower())
    if normalized is None:
        raise ValueError(
            f"level must be one of {sorted(set(LEVEL_ALIASES.values()))}."
        )
    return normalized


def validate_activity_scope_arguments(
    *,
    level: str | None,
    object_id: str | None,
    account_id: str | None,
    campaign_id: str | None,
    adset_id: str | None,
    ad_id: str | None,
) -> ActivityScopeArguments:
    """Validate change-history level, dependency, and agreement rules."""
    normalized_level = normalize_activity_level(level)
    normalized_object_id = _blank_to_none(object_id)
    normalized_account_id = _blank_to_none(account_id)
    aliases = tuple(
        (alias_level, alias_id)
        for alias_level, raw_id in (
            ("campaign", campaign_id),
            ("adset", adset_id),
            ("ad", ad_id),
        )
        if (alias_id := _blank_to_none(raw_id)) is not None
    )
    if len(aliases) > 1:
        raise ValueError(
            "Provide only one of campaign_id, adset_id, or ad_id."
        )
    alias = aliases[0] if aliases else None

    if normalized_object_id is not None:
        if normalized_level is None:
            raise ValueError("Provide level when using object_id.")
        if alias is not None and (
            alias[0] != normalized_level
            or alias[1] != normalized_object_id
        ):
            raise ValueError(
                "Conflicting scope arguments. Use either level/object_id "
                "or one entity-specific id."
            )
        if (
            normalized_level == "account"
            and normalized_account_id is not None
            and _canonical_account_id(normalized_object_id)
            != _canonical_account_id(normalized_account_id)
        ):
            raise ValueError("Conflicting account scope arguments.")
    elif alias is not None:
        if normalized_level is not None and normalized_level != alias[0]:
            raise ValueError(
                "Conflicting scope arguments. Use either level/object_id "
                "or one entity-specific id."
            )
    elif normalized_level not in (None, "account"):
        raise ValueError(
            "Provide object_id or the matching entity-specific id for "
            "non-account levels."
        )

    return ActivityScopeArguments(
        level=normalized_level,
        object_id=normalized_object_id,
        account_id=normalized_account_id,
        object_alias=alias,
    )
