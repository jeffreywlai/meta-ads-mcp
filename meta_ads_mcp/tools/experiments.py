"""A/B test (split test) and lift study tools built on Meta's ad study API."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, NotFoundError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.graph_payload import OMIT, merge_graph_payload
from meta_ads_mcp.normalize import blank_to_none, normalize_collection
from meta_ads_mcp.schemas import analysis_response, creation_response, mutation_response
from meta_ads_mcp.tool_types import StringList, coerce_csv_string_list

STUDY_LIST_FIELDS = [
    "id",
    "name",
    "type",
    "description",
    "start_time",
    "end_time",
    "canceled_time",
    "results_first_available_date",
]
STUDY_FIELDS = [
    *STUDY_LIST_FIELDS,
    "observation_end_time",
    "cooldown_start_time",
    "created_time",
    "updated_time",
    "business",
]
CELL_FIELDS = [
    "id",
    "name",
    "treatment_percentage",
    "control_percentage",
    "campaigns",
    "adsets",
    "adaccounts",
]
OBJECTIVE_FIELDS = ["id", "name", "type", "is_primary", "last_updated_results"]
STUDY_WRITE_READ_FIELDS = [
    "id",
    "name",
    "description",
    "type",
    "start_time",
    "end_time",
    "observation_end_time",
    "canceled_time",
]
STUDY_TYPES = frozenset({
    "LIFT",
    "SPLIT_TEST",
    "SPLIT_TEST_V2",
    "CONTINUOUS_LIFT_CONFIG",
    "GEO_LIFT",
    "BACKEND_AB_TESTING",
    "CREATIVE_SPEND_ENFORCEMENT",
    "PORTFOLIO_OPTIMIZER",
    "VERSION_CONTROL",
})
SPLIT_TEST_TYPES = frozenset({"SPLIT_TEST", "SPLIT_TEST_V2"})
OBJECTIVE_TYPES = frozenset({
    "BRAND", "BRANDLIFT", "CONVERSIONS", "FTL", "MAE", "MAI", "MPC_CONVERSION",
    "NONSALES", "PARTNER", "SALES", "TELCO",
})
OBJECTIVE_SOURCE_KEYS = (
    "adspixels",
    "applications",
    "customconversions",
    "offline_conversion_data_sets",
    "offsite_datasets",
    "product_sets",
    "product_catalogs",
)
EVENT_NAMES_REQUIRED = frozenset({"adspixels", "applications"})
AB_CELL_KEYS = frozenset({"name", "treatment_percentage", "campaign_ids", "adset_ids", "description"})
LIFT_CELL_KEYS = frozenset({
    "name", "treatment_percentage", "control_percentage", "campaign_ids", "ad_account_ids",
    "description",
})
SPLIT_RESULT_KEYS = frozenset({
    "winner_ad_object_id", "winner_confidence", "confidence_by_ad_object_id", "high_performer_ids",
})
MIN_SPLIT_TREATMENT_PERCENTAGE = 10
MAX_CELLS_PER_STUDY = 150
MAX_ENTITIES_PER_CELL = 100
MAX_CONCURRENT_STUDIES = 100
CONCURRENCY_SCAN_PAGES = 10
END_EARLY_GRACE_SECONDS = 60
ACTIVE_STATES = frozenset({"scheduled", "running"})
NON_DELIVERING_STATUSES = frozenset({"DELETED", "ARCHIVED"})

AD_STUDY_DOC = "https://developers.facebook.com/docs/marketing-api/reference/ad-study/"
SPLIT_TEST_GUIDE = "https://developers.facebook.com/docs/marketing-api/guides/split-testing/"
ELIGIBILITY_LABEL = (
    "Local pre-check by this server, not Meta's verdict. Meta publishes no eligibility "
    "endpoint; Meta decides when the test is created."
)
ELIGIBILITY_RULES = [
    {
        "rule": "Split test cells hold campaigns, ad sets or ad accounts.",
        "source": AD_STUDY_DOC,
        "applied_as": "level must be campaign or adset.",
    },
    {
        "rule": "Each cell's treatment_percentage must be at least 10 and all cells together at most 100.",
        "source": AD_STUDY_DOC,
        "applied_as": "warning when more than 10 entities are checked, because they cannot each get their own cell.",
    },
    {
        "rule": "At most 150 cells per study and 100 ad objects per cell.",
        "source": SPLIT_TEST_GUIDE,
        "applied_as": "more than 150 entity_ids is rejected as input.",
    },
    {
        "rule": "At most 100 concurrent studies per advertiser.",
        "source": SPLIT_TEST_GUIDE,
        "applied_as": (
            "blocker when the account's impacting_ad_studies edge already shows 100 "
            "scheduled or running studies."
        ),
    },
]
ELIGIBILITY_NOT_CHECKED = [
    "Objective and buying-type compatibility: Meta's developer documentation states no rule; "
    "objective and buying_type are shown per entity for review, not judged.",
    "Meta's guidance to change only one variable between cells cannot be checked from object settings.",
    "Whether one object may be in two studies at once is not stated in Meta's developer "
    "documentation; active studies are reported as a warning only.",
    "Deliverable status is not stated as a study rule; DELETED or ARCHIVED objects are a warning only.",
    "Test duration limits appear only in Help Center pages that could not be confirmed, so they are not checked.",
]


def _now() -> datetime:
    """Current UTC time; patched in tests."""
    return datetime.now(timezone.utc)


def _parse_meta_time(value: Any) -> datetime | None:
    """Parse a Graph datetime such as 2026-03-01T00:00:00+0000; None when absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _to_unix(value: Any, field_name: str) -> int:
    """Convert an ISO 8601 time with offset, or Unix seconds, to the integer Meta expects."""
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be an ISO 8601 time with offset or Unix seconds.")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and value.strip().isdigit():
        seconds = int(value.strip())
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError(
                f"{field_name} must be an ISO 8601 time with offset (2026-12-01T00:00:00Z) or Unix seconds."
            ) from exc
        if parsed.tzinfo is None:
            raise ValidationError(
                f"{field_name} needs a UTC offset or Z (for example 2026-12-01T00:00:00-05:00) "
                "so the study does not start at the wrong hour."
            )
        seconds = int(parsed.timestamp())
    else:
        raise ValidationError(f"{field_name} must be an ISO 8601 time with offset or Unix seconds.")
    if seconds <= 0:
        raise ValidationError(f"{field_name} must be a positive Unix time.")
    return seconds


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _study_state(study: dict[str, Any], now: datetime) -> str:
    """Classify a study as canceled, scheduled, running, ended or unknown from its times."""
    if _parse_meta_time(study.get("canceled_time")) is not None:
        return "canceled"
    start = _parse_meta_time(study.get("start_time"))
    end = _parse_meta_time(study.get("end_time"))
    if start is not None and start > now:
        return "scheduled"
    if end is not None and end <= now:
        return "ended"
    if start is not None and end is not None:
        return "running"
    return "unknown"


def _edge_ids(value: Any) -> list[str]:
    """Flatten an expanded edge ({data: [{id}]}) or id list into string ids."""
    if isinstance(value, dict):
        value = value.get("data", [])
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        candidate = item.get("id") if isinstance(item, dict) else item
        if candidate not in (None, ""):
            ids.append(str(candidate))
    return ids


def _id_list(value: Any, *, label: str) -> list[str]:
    """Validate a list of ids (or CSV string) and keep them as strings."""
    values = coerce_csv_string_list(value)
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValidationError(f"{label} must be a list of nonblank id strings or a comma-separated string.")
    return list(dict.fromkeys(item.strip() for item in values))


def _percentage(value: Any, *, label: str, minimum: float = 0.0) -> float:
    """Validate a percentage Meta accepts: a positive number with at most two decimals."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{label} must be a number.")
    if value > 100 or value < minimum or value <= 0:
        lower = f"at least {minimum:g}" if minimum else "greater than 0"
        raise ValidationError(f"{label} must be {lower} and at most 100; got {value}.")
    if abs(round(value, 2) - value) > 1e-9:
        raise ValidationError(f"{label} allows at most two decimal places; got {value}.")
    return value


def _check_cell_keys(cell: Any, index: int, accepted: frozenset[str]) -> dict[str, Any]:
    if not isinstance(cell, dict):
        raise ValidationError(f"cells[{index}] must be an object.")
    unknown = sorted(set(cell) - accepted)
    if unknown:
        raise ValidationError(
            f"cells[{index}] has unsupported keys: {', '.join(unknown)}. "
            f"Accepted keys: {', '.join(sorted(accepted))}."
        )
    name = cell.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(f"cells[{index}].name is required.")
    description = cell.get("description")
    if description is not None and not isinstance(description, str):
        raise ValidationError(f"cells[{index}].description must be a string.")
    return cell


def _build_ab_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate A/B test cells and convert them to Meta's cell payload."""
    if not isinstance(cells, list) or len(cells) < 2:
        raise ValidationError("An A/B test needs at least two cells.")
    if len(cells) > MAX_CELLS_PER_STUDY:
        raise ValidationError(f"Meta allows at most {MAX_CELLS_PER_STUDY} cells per study.")
    built: list[dict[str, Any]] = []
    owners: dict[str, int] = {}
    total = 0.0
    for index, raw in enumerate(cells):
        cell = _check_cell_keys(raw, index, AB_CELL_KEYS)
        share = _percentage(
            cell.get("treatment_percentage"),
            label=f"cells[{index}].treatment_percentage",
            minimum=MIN_SPLIT_TREATMENT_PERCENTAGE,
        )
        total += share
        payload: dict[str, Any] = {"name": cell["name"].strip(), "treatment_percentage": share}
        entity_count = 0
        for key, meta_key in (("campaign_ids", "campaigns"), ("adset_ids", "adsets")):
            if cell.get(key) is None:
                continue
            ids = _id_list(cell[key], label=f"cells[{index}].{key}")
            for entity_id in ids:
                if entity_id in owners:
                    raise ValidationError(
                        f"{entity_id} appears in cells[{owners[entity_id]}] and cells[{index}]; "
                        "each campaign or ad set can be in only one cell."
                    )
                owners[entity_id] = index
            if ids:
                payload[meta_key] = ids
                entity_count += len(ids)
        if entity_count == 0:
            raise ValidationError(f"cells[{index}] needs campaign_ids or adset_ids.")
        if entity_count > MAX_ENTITIES_PER_CELL:
            raise ValidationError(f"Meta allows at most {MAX_ENTITIES_PER_CELL} ad objects per cell.")
        if cell.get("description"):
            payload["description"] = cell["description"]
        built.append(payload)
    if round(total, 2) != 100:
        raise ValidationError(f"Cell treatment_percentage values must total 100; they total {round(total, 2):g}.")
    return built


def _build_lift_cells(cells: list[dict[str, Any]], account_id: str) -> list[dict[str, Any]]:
    """Validate lift study cells (treatment plus holdout) and convert them to Meta's payload."""
    if not isinstance(cells, list) or not cells:
        raise ValidationError("A lift study needs at least one cell.")
    if len(cells) > MAX_CELLS_PER_STUDY:
        raise ValidationError(f"Meta allows at most {MAX_CELLS_PER_STUDY} cells per study.")
    built: list[dict[str, Any]] = []
    population = 0.0
    for index, raw in enumerate(cells):
        cell = _check_cell_keys(raw, index, LIFT_CELL_KEYS)
        treatment = _percentage(cell.get("treatment_percentage"), label=f"cells[{index}].treatment_percentage")
        control = _percentage(cell.get("control_percentage"), label=f"cells[{index}].control_percentage")
        combined = round(treatment + control, 2)
        population += treatment + control
        if combined > 100:
            raise ValidationError(
                f"cells[{index}] treatment_percentage plus control_percentage is {combined:g}; it cannot exceed 100."
            )
        if len(cells) == 1 and combined != 100:
            raise ValidationError(
                "With a single cell, treatment_percentage plus control_percentage must equal 100 "
                f"(Meta's lift study rule); got {combined:g}."
            )
        payload: dict[str, Any] = {
            "name": cell["name"].strip(),
            "treatment_percentage": treatment,
            "control_percentage": control,
        }
        if cell.get("campaign_ids") is not None:
            payload["campaigns"] = _id_list(cell["campaign_ids"], label=f"cells[{index}].campaign_ids")
        if cell.get("ad_account_ids") is not None:
            payload["adaccounts"] = [
                item.removeprefix("act_")
                for item in _id_list(cell["ad_account_ids"], label=f"cells[{index}].ad_account_ids")
            ]
        if not payload.get("campaigns") and not payload.get("adaccounts"):
            payload["adaccounts"] = [account_id.removeprefix("act_")]
        if cell.get("description"):
            payload["description"] = cell["description"]
        built.append(payload)
    if round(population, 2) > 100:
        raise ValidationError(
            "Lift study percentages are shares of the whole study population: treatment_percentage plus "
            f"control_percentage across all cells can total at most 100; these cells total {round(population, 2):g}."
        )
    return built


def _build_objectives(objectives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate lift objectives against Meta's documented objective shape."""
    if not isinstance(objectives, list) or not objectives:
        raise ValidationError("A lift study requires at least one objective.")
    accepted = {"name", "type", "is_primary", *OBJECTIVE_SOURCE_KEYS}
    built: list[dict[str, Any]] = []
    for index, objective in enumerate(objectives):
        if not isinstance(objective, dict):
            raise ValidationError(f"objectives[{index}] must be an object.")
        unknown = sorted(set(objective) - accepted)
        if unknown:
            raise ValidationError(
                f"objectives[{index}] has unsupported keys: {', '.join(unknown)}. "
                f"Accepted keys: {', '.join(sorted(accepted))}."
            )
        name = objective.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"objectives[{index}].name is required.")
        objective_type = str(objective.get("type") or "CONVERSIONS").strip().upper()
        if objective_type not in OBJECTIVE_TYPES:
            raise ValidationError(
                f"objectives[{index}].type must be one of {', '.join(sorted(OBJECTIVE_TYPES))}."
            )
        payload: dict[str, Any] = {"name": name.strip(), "type": objective_type}
        if "is_primary" in objective:
            if not isinstance(objective["is_primary"], bool):
                raise ValidationError(f"objectives[{index}].is_primary must be true or false.")
            payload["is_primary"] = objective["is_primary"]
        for key in OBJECTIVE_SOURCE_KEYS:
            if objective.get(key) is None:
                continue
            sources = objective[key]
            if not isinstance(sources, list) or not sources:
                raise ValidationError(f"objectives[{index}].{key} must be a nonempty list of {{id, event_names}}.")
            for source in sources:
                if not isinstance(source, dict) or not str(source.get("id") or "").strip():
                    raise ValidationError(f"Each objectives[{index}].{key} entry needs an id.")
                event_names = source.get("event_names")
                if key in EVENT_NAMES_REQUIRED and (
                    not isinstance(event_names, list)
                    or not event_names
                    or any(not isinstance(name, str) or not name.strip() for name in event_names)
                ):
                    raise ValidationError(
                        f"Each objectives[{index}].{key} entry needs event_names; Meta requires event "
                        "names for pixel and app sources."
                    )
            payload[key] = sources
        built.append(payload)
    primaries = sum(1 for objective in built if objective.get("is_primary") is True)
    if primaries > 1:
        raise ValidationError("A lift study can have only one primary objective.")
    if primaries == 0 and len(built) == 1:
        built[0]["is_primary"] = True
    return built


def _required_account_id(account_id: Any) -> str:
    """Reject a missing or blank account id before normalizing it to the act_ form."""
    if not isinstance(account_id, str) or not account_id.strip() or account_id.strip() == "act_":
        raise ValidationError("account_id is required.")
    return normalize_account_id(account_id.strip())


async def _resolve_business_id(client: Any, account_id: str, business_id: str | None) -> str:
    """Meta creates studies on a business; use business_id or the account's owning business."""
    if business_id:
        return business_id
    account = await client.get_object(account_id, fields=["business"])
    business = account.get("business")
    resolved = business.get("id") if isinstance(business, dict) else business
    if not resolved:
        raise ValidationError(
            f"Meta returned no owning business for {account_id}. Meta creates studies on a business "
            "(POST /{business_id}/ad_studies); pass business_id."
        )
    return str(resolved)


def _paged_summary(normalized: dict[str, Any]) -> dict[str, Any]:
    has_more = bool(normalized["paging"].get("next"))
    return {
        "complete": not has_more,
        "truncated": has_more,
        "next_after": normalized["paging"].get("after") if has_more else None,
    }


@mcp_server.tool()
async def list_ad_studies(
    account_id: str | None = None,
    business_id: str | None = None,
    study_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 50,
    after: str | None = None,
) -> dict[str, Any]:
    """List A/B tests (split tests) and lift studies with a derived state (scheduled, running, ended, canceled).

    Give account_id (studies touching the account or anything in it), business_id, or entity_id
    (a campaign, ad set or ad id: only studies containing it). study_type filters each page locally.
    Follow summary.next_after with after until summary.complete is true.
    """
    account_id = blank_to_none(account_id)
    business_id = blank_to_none(business_id)
    entity_id = blank_to_none(entity_id)
    if limit < 1:
        raise ValidationError("limit must be positive.")
    if account_id and business_id:
        raise ValidationError("Provide account_id or business_id, not both.")
    if not (account_id or business_id or entity_id):
        raise ValidationError("Provide account_id, business_id, or entity_id.")
    type_filter = blank_to_none(study_type)
    if type_filter is not None:
        type_filter = type_filter.upper()
        if type_filter not in STUDY_TYPES:
            raise ValidationError(f"study_type must be one of {', '.join(sorted(STUDY_TYPES))}.")
    if entity_id:
        parent, edge = entity_id, "ad_studies"
    elif business_id:
        parent, edge = business_id, "ad_studies"
    else:
        parent, edge = normalize_account_id(account_id), "impacting_ad_studies"
    params: dict[str, Any] = {"limit": limit}
    if blank_to_none(after):
        params["after"] = after.strip()
    payload = await get_graph_api_client().list_objects(parent, edge, fields=STUDY_LIST_FIELDS, params=params)
    normalized = normalize_collection(payload)
    now = _now()
    items = [{**item, "state": _study_state(item, now)} for item in normalized["items"]]
    removed = 0
    if type_filter is not None:
        kept = [item for item in items if str(item.get("type") or "").upper() == type_filter]
        removed = len(items) - len(kept)
        items = kept
    normalized["items"] = items
    normalized["summary"] = {"count": len(items), **_paged_summary(normalized), "source_edge": edge}
    if type_filter is not None:
        normalized["summary"]["study_type_filter"] = {
            "study_type": type_filter,
            "applied": "locally to this page",
            "removed_on_this_page": removed,
        }
    return normalized


def _normalize_cell(cell: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cell.get("id"),
        "name": cell.get("name"),
        "treatment_percentage": cell.get("treatment_percentage"),
        "control_percentage": cell.get("control_percentage"),
        "campaign_ids": _edge_ids(cell.get("campaigns")),
        "adset_ids": _edge_ids(cell.get("adsets")),
        "ad_account_ids": _edge_ids(cell.get("adaccounts")),
    }


def _parse_results(raw: Any) -> list[Any]:
    """Meta returns objective results as a list of JSON strings; decode what can be decoded."""
    if raw in (None, "", [], {}):
        return []
    entries = raw if isinstance(raw, list) else [raw]
    parsed: list[Any] = []
    for entry in entries:
        value = entry
        if isinstance(entry, str):
            try:
                value = json.loads(entry)
            except ValueError:
                value = entry
        if isinstance(value, list):
            parsed.extend(value)
        else:
            parsed.append(value)
    return [value for value in parsed if value not in (None, "", [], {})]


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _split_results(
    objectives: list[dict[str, Any]],
    cells: list[dict[str, Any]],
) -> dict[str, Any]:
    for objective in objectives:
        for entry in objective["parsed_results"]:
            if not isinstance(entry, dict) or not SPLIT_RESULT_KEYS & set(entry):
                continue
            winner_id = entry.get("winner_ad_object_id")
            response: dict[str, Any] = {
                "status": "available",
                "kind": "split_test",
                "objective_id": objective.get("id"),
                "winner": None,
                "high_performer_ids": entry.get("high_performer_ids"),
                "confidence_by_ad_object_id": entry.get("confidence_by_ad_object_id"),
                "confidence_scale_note": "Meta does not document the scale of these confidence values.",
            }
            if winner_id not in (None, ""):
                winner_id = str(winner_id)
                cell = next(
                    (
                        cell for cell in cells
                        if winner_id in cell["campaign_ids"] + cell["adset_ids"] + cell["ad_account_ids"]
                    ),
                    None,
                )
                response["winner"] = {
                    "ad_object_id": winner_id,
                    "cell_id": cell["id"] if cell else None,
                    "cell_name": cell["name"] if cell else None,
                    "confidence": entry.get("winner_confidence"),
                }
                response["message"] = "Meta reported a winning cell."
            else:
                response["message"] = (
                    "Meta reported no single winner. For creative tests Meta's authoritative "
                    "outcome is high_performer_ids."
                )
            return response
    return {
        "status": "available",
        "kind": "split_test",
        "winner": None,
        "message": "Meta returned results in a shape this tool does not recognise; see raw_results.",
        "raw_results": [objective["parsed_results"] for objective in objectives if objective["parsed_results"]],
    }


def _lift_results(objectives: list[dict[str, Any]], cells: list[dict[str, Any]]) -> dict[str, Any]:
    names = {cell["id"]: cell["name"] for cell in cells}
    rendered: list[dict[str, Any]] = []
    for objective in objectives:
        if not objective["parsed_results"]:
            continue
        rows: list[dict[str, Any]] = []
        for row in objective["parsed_results"]:
            if not isinstance(row, dict):
                rows.append({"meta_fields": row})
                continue
            incremental = _to_number(row.get("conversions_incremental"))
            baseline = _to_number(row.get("conversions_control_scaled"))
            cell_id = row.get("cell_id")
            rows.append({
                "cell_id": cell_id,
                "cell_name": names.get(cell_id),
                "incremental_conversions": row.get("conversions_incremental"),
                "incremental_conversions_lower": row.get("conversions_incremental_lower"),
                "incremental_conversions_upper": row.get("conversions_incremental_upper"),
                "conversions_lift": (
                    incremental / baseline if incremental is not None and baseline else None
                ),
                "conversions_lift_basis": "conversions_incremental / conversions_control_scaled",
                "confidence": row.get("conversions_confidence"),
                "cost_per_incremental_conversion": row.get("conversions_CPiC"),
                "meta_fields": row,
            })
        rendered.append({
            "objective_id": objective.get("id"),
            "name": objective.get("name"),
            "is_primary": objective.get("is_primary"),
            "last_updated_results": objective.get("last_updated_results"),
            "cells": rows,
        })
    return {
        "status": "available",
        "kind": "lift",
        "objectives": rendered,
        "notes": [
            "conversions_lift is derived here from Meta's fields as stated in conversions_lift_basis.",
            "Meta's lift guide does not document the scale of conversions_confidence.",
        ],
    }


@mcp_server.tool()
async def get_ad_study(study_id: str, include_results: bool = False) -> dict[str, Any]:
    """Get one A/B test or lift study: settings, cells (shares and the campaigns or ad sets in each) and objectives.

    include_results adds the outcome: the winning cell and confidence for A/B tests, incremental
    conversions, lift and confidence per cell for lift studies, or a plain not_ready status.
    """
    study_id = blank_to_none(study_id)
    if study_id is None:
        raise ValidationError("study_id is required.")
    client = get_graph_api_client()
    study = await client.get_object(study_id, fields=STUDY_FIELDS)
    cells_payload = normalize_collection(
        await client.list_objects(study_id, "cells", fields=CELL_FIELDS, params={"limit": MAX_CELLS_PER_STUDY})
    )
    objective_fields = [*OBJECTIVE_FIELDS, "results"] if include_results else OBJECTIVE_FIELDS
    objectives_payload = normalize_collection(
        await client.list_objects(study_id, "objectives", fields=objective_fields, params={"limit": 100})
    )
    now = _now()
    state = _study_state(study, now)
    cells = [_normalize_cell(cell) for cell in cells_payload["items"]]
    objectives = []
    for objective in objectives_payload["items"]:
        parsed = _parse_results(objective.get("results")) if include_results else []
        objectives.append({
            **{key: value for key, value in objective.items() if key != "results"},
            "parsed_results": parsed,
        })
    response: dict[str, Any] = {
        "item": {**study, "state": state},
        "cells": cells,
        "objectives": [
            {key: value for key, value in objective.items() if key != "parsed_results"}
            for objective in objectives
        ],
        "summary": {
            "count": 1,
            "cell_count": len(cells),
            "objective_count": len(objectives),
            "cells_complete": not cells_payload["paging"].get("next"),
            "objectives_complete": not objectives_payload["paging"].get("next"),
        },
    }
    if not include_results:
        return response
    if not any(objective["parsed_results"] for objective in objectives):
        response["results"] = {
            "status": "not_ready",
            "message": (
                f"Meta has not published results for this study yet (study state: {state}; "
                f"results_first_available_date: {study.get('results_first_available_date')})."
            ),
        }
    elif str(study.get("type") or "").upper() in SPLIT_TEST_TYPES:
        response["results"] = _split_results(objectives, cells)
    else:
        response["results"] = _lift_results(objectives, cells)
    return response


async def _active_studies(client: Any, parent: str, edge: str, now: datetime) -> list[dict[str, Any]]:
    payload = await client.list_objects(parent, edge, fields=STUDY_LIST_FIELDS, params={"limit": 100})
    return [
        {"id": study.get("id"), "name": study.get("name"), "type": study.get("type"), "state": state}
        for study in payload.get("data", [])
        if (state := _study_state(study, now)) in ACTIVE_STATES
    ]


async def _count_account_active_studies(client: Any, account_id: str, now: datetime) -> dict[str, Any]:
    """Count scheduled or running studies on the account, paging until the documented limit is reached."""
    active = 0
    pages = 0
    params: dict[str, Any] = {"limit": 100}
    complete = False
    while pages < CONCURRENCY_SCAN_PAGES:
        normalized = normalize_collection(
            await client.list_objects(account_id, "impacting_ad_studies", fields=STUDY_LIST_FIELDS, params=params)
        )
        pages += 1
        active += sum(1 for study in normalized["items"] if _study_state(study, now) in ACTIVE_STATES)
        cursor = normalized["paging"].get("after")
        if not normalized["paging"].get("next") or not cursor:
            complete = True
            break
        if active >= MAX_CONCURRENT_STUDIES:
            break
        params = {"limit": 100, "after": cursor}
    return {"active_studies": active, "pages_read": pages, "count_complete": complete}


@mcp_server.tool()
async def check_ab_test_eligibility(
    account_id: str,
    level: str,
    entity_ids: StringList,
) -> dict[str, Any]:
    """Pre-check whether campaigns or ad sets (level campaign or adset) can go into an A/B test.

    This is a local pre-check against Meta's documented rules, not Meta's verdict: Meta has no
    public eligibility endpoint. Reports per entity: exists, belongs to the account, status,
    objective, buying type, and any scheduled or running study it is already in.
    """
    account_id = _required_account_id(account_id)
    normalized_level = (blank_to_none(level) or "").lower().replace("ad_set", "adset")
    if normalized_level not in {"campaign", "adset"}:
        raise ValidationError("level must be campaign or adset.")
    ids = _id_list(entity_ids, label="entity_ids")
    if not ids:
        raise ValidationError("entity_ids needs at least one campaign or ad set id.")
    if len(ids) > MAX_CELLS_PER_STUDY:
        raise ValidationError(f"Check at most {MAX_CELLS_PER_STUDY} entities, Meta's documented maximum cells per study.")
    account_number = account_id.removeprefix("act_")
    fields = (
        ["id", "name", "account_id", "effective_status", "objective", "buying_type"]
        if normalized_level == "campaign"
        else ["id", "name", "account_id", "campaign_id", "effective_status", "optimization_goal", "billing_event"]
    )
    client = get_graph_api_client()
    now = _now()
    entities: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    missing_signals: list[str] = []
    for entity_id in ids:
        result: dict[str, Any] = {"entity_id": entity_id, "level": normalized_level, "blockers": [], "warnings": []}
        try:
            entity = await client.get_object(entity_id, fields=fields)
        except (NotFoundError, MetaApiError, UnsupportedFeatureError) as exc:
            result.update({"exists": False, "active_studies": None})
            result["blockers"].append(f"Could not read {entity_id}: {exc}")
            entities.append(result)
            continue
        result["exists"] = True
        for key in fields[1:]:
            result[key] = entity.get(key)
        entity_account = str(entity.get("account_id") or "").removeprefix("act_")
        if not entity_account:
            result["warnings"].append("Meta returned no account_id, so the account match was not checked.")
        elif entity_account != account_number:
            result["blockers"].append(
                f"{entity_id} belongs to ad account {entity_account}, not {account_number}."
            )
        status = str(entity.get("effective_status") or "").upper()
        if status in NON_DELIVERING_STATUSES:
            result["warnings"].append(f"effective_status is {status}, so it cannot deliver.")
        try:
            active = await _active_studies(client, entity_id, "ad_studies", now)
        except (NotFoundError, MetaApiError, UnsupportedFeatureError) as exc:
            result["active_studies"] = None
            missing_signals.append(f"Could not read the studies containing {entity_id}: {exc}")
        else:
            result["active_studies"] = active
            if active:
                result["warnings"].append(
                    f"Already in {len(active)} scheduled or running study(ies): "
                    f"{', '.join(str(study['id']) for study in active)}. Meta's developer documentation "
                    "does not say whether this blocks a new test; Meta decides at creation."
                )
        entities.append(result)
    account_check = await _count_account_active_studies(client, account_id, now)
    account_check["blockers"] = []
    account_check["warnings"] = []
    if account_check["active_studies"] >= MAX_CONCURRENT_STUDIES:
        account_check["blockers"].append(
            f"The account already has {account_check['active_studies']} scheduled or running studies; "
            f"Meta documents a maximum of {MAX_CONCURRENT_STUDIES} concurrent studies per advertiser."
        )
    elif not account_check["count_complete"]:
        missing_signals.append(
            f"Counted active studies on the first {account_check['pages_read']} pages only."
        )
    if len(ids) > 100 // MIN_SPLIT_TREATMENT_PERCENTAGE:
        account_check["warnings"].append(
            f"{len(ids)} entities cannot each get their own cell: Meta requires every cell's "
            f"treatment_percentage to be at least {MIN_SPLIT_TREATMENT_PERCENTAGE}, so at most "
            f"{100 // MIN_SPLIT_TREATMENT_PERCENTAGE} cells fit."
        )
    for entity in entities:
        findings.extend(
            {"entity_id": entity["entity_id"], "severity": "blocker", "message": message}
            for message in entity["blockers"]
        )
        findings.extend(
            {"entity_id": entity["entity_id"], "severity": "warning", "message": message}
            for message in entity["warnings"]
        )
    findings.extend({"entity_id": account_id, "severity": "blocker", "message": m} for m in account_check["blockers"])
    findings.extend({"entity_id": account_id, "severity": "warning", "message": m} for m in account_check["warnings"])
    blocked = sum(1 for entity in entities if entity["blockers"])
    eligible = blocked == 0 and not account_check["blockers"]
    return analysis_response(
        scope={"account_id": account_id, "level": normalized_level, "entity_ids": ids},
        metrics={
            "entities_checked": len(entities),
            "passed_local_precheck": len(entities) - blocked,
            "blocked": blocked,
            "with_warnings": sum(1 for entity in entities if entity["warnings"]),
        },
        findings=findings,
        suggestions=(
            ["Create the test with create_ab_test; Meta makes the final eligibility decision."]
            if eligible
            else ["Resolve the blockers, then run this check again."]
        ),
        missing_signals=missing_signals,
        extra={
            "check_type": "local_precheck",
            "meta_verdict": False,
            "label": ELIGIBILITY_LABEL,
            "eligible_by_local_precheck": eligible,
            "entities": entities,
            "account": account_check,
            "rules_applied": ELIGIBILITY_RULES,
            "not_checked": ELIGIBILITY_NOT_CHECKED,
        },
    )


@mcp_server.tool()
async def create_ab_test(
    account_id: str,
    name: str,
    cells: list[dict[str, Any]],
    start_time: str | int,
    end_time: str | int,
    confidence_level: float | None = None,
    hypothesis: str | None = None,
    business_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an A/B test (Meta split test) comparing campaigns or ad sets. Run check_ab_test_eligibility first.

    cells: at least two, each {name, treatment_percentage, campaign_ids or adset_ids, description?}.
    Shares are at least 10 each and must total 100; no campaign or ad set may be in two cells.
    Times are ISO 8601 with offset or Unix seconds. Meta creates studies on the account's business;
    pass business_id if the account has none. hypothesis is stored as the study description.
    """
    account_id = _required_account_id(account_id)
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name is required.")
    built_cells = _build_ab_cells(cells)
    start = _to_unix(start_time, "start_time")
    end = _to_unix(end_time, "end_time")
    if end <= start:
        raise ValidationError("end_time must be after start_time.")
    if confidence_level is not None and (
        isinstance(confidence_level, bool) or not math.isfinite(confidence_level) or confidence_level <= 0
    ):
        raise ValidationError("confidence_level must be a positive number.")
    payload = merge_graph_payload(
        {
            "name": name.strip(),
            "type": "SPLIT_TEST",
            "cells": built_cells,
            "start_time": start,
            "end_time": end,
            "confidence_level": OMIT if confidence_level is None else confidence_level,
            "description": blank_to_none(hypothesis) or OMIT,
        },
        params,
    )
    client = get_graph_api_client()
    owner = await _resolve_business_id(client, account_id, blank_to_none(business_id))
    result = await client.create_edge_object(owner, "ad_studies", data=payload)
    return creation_response(
        action="create_ab_test",
        target={"account_id": account_id, "business_id": owner},
        result=result,
    )


@mcp_server.tool()
async def create_lift_study(
    account_id: str,
    name: str,
    cells: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    start_time: str | int,
    end_time: str | int,
    business_id: str | None = None,
    description: str | None = None,
    observation_end_time: str | int | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a conversion lift study (holdout test). Meta limits lift studies to approved advertisers.

    cells: each {name, treatment_percentage, control_percentage, campaign_ids or ad_account_ids?};
    a cell without ids measures the whole account. Percentages are shares of the whole population,
    so all cells' treatment and control together total at most 100. objectives: each {name, type (default
    CONVERSIONS), is_primary, adspixels or applications [{id, event_names}], ...}; one primary.
    start_time must be in the future. observation_end_time defaults to end_time.
    """
    account_id = _required_account_id(account_id)
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name is required.")
    built_cells = _build_lift_cells(cells, account_id)
    built_objectives = _build_objectives(objectives)
    start = _to_unix(start_time, "start_time")
    end = _to_unix(end_time, "end_time")
    if start <= int(_now().timestamp()):
        raise ValidationError("start_time must be in the future for a lift study.")
    if end <= start:
        raise ValidationError("end_time must be after start_time.")
    observation_end = end if observation_end_time is None else _to_unix(observation_end_time, "observation_end_time")
    if observation_end < end:
        raise ValidationError("observation_end_time cannot be before end_time.")
    payload = merge_graph_payload(
        {
            "name": name.strip(),
            "type": "LIFT",
            "cells": built_cells,
            "objectives": built_objectives,
            "start_time": start,
            "end_time": end,
            "observation_end_time": observation_end,
            "description": blank_to_none(description) or OMIT,
        },
        params,
    )
    client = get_graph_api_client()
    owner = await _resolve_business_id(client, account_id, blank_to_none(business_id))
    result = await client.create_edge_object(owner, "ad_studies", data=payload)
    return creation_response(
        action="create_lift_study",
        target={"account_id": account_id, "business_id": owner},
        result=result,
    )


def _previous_study(previous: dict[str, Any], state: str) -> dict[str, Any]:
    return {
        "name": previous.get("name"),
        "description": previous.get("description"),
        "type": previous.get("type"),
        "state": state,
        "start_time": previous.get("start_time"),
        "end_time": previous.get("end_time"),
        "observation_end_time": previous.get("observation_end_time"),
        "canceled_time": previous.get("canceled_time"),
    }


def _stored_unix(study: dict[str, Any], key: str) -> int | None:
    parsed = _parse_meta_time(study.get(key))
    return None if parsed is None else int(parsed.timestamp())


def _check_effective_times(
    previous: dict[str, Any],
    start: int | None,
    end: int | None,
    observation_end: int | None,
) -> None:
    """Validate the times the study would have after the edit, filling unchanged bounds from the pre-read."""
    if start is None and end is None and observation_end is None:
        return
    effective_start = start if start is not None else _stored_unix(previous, "start_time")
    effective_end = end if end is not None else _stored_unix(previous, "end_time")
    if (
        (start is not None or end is not None)
        and effective_start is not None
        and effective_end is not None
        and effective_end <= effective_start
    ):
        raise ValidationError(
            f"After this edit the study would start at {_iso(effective_start)} and end at "
            f"{_iso(effective_end)}; end_time must be after start_time. Unchanged times are "
            "taken from the study's current values."
        )
    stored_observation = _stored_unix(previous, "observation_end_time")
    is_lift = str(previous.get("type") or "").upper() == "LIFT"
    effective_observation = (
        observation_end if observation_end is not None else (stored_observation if is_lift else None)
    )
    if (
        (end is not None or observation_end is not None)
        and effective_observation is not None
        and effective_end is not None
        and effective_observation < effective_end
    ):
        raise ValidationError(
            f"After this edit observation_end_time ({_iso(effective_observation)}) would be before "
            f"end_time ({_iso(effective_end)}); Meta's lift guide sets observation_end_time at or after "
            "end_time. Pass a later observation_end_time in params as well."
        )


@mcp_server.tool()
async def update_ad_study(
    study_id: str,
    name: str | None = None,
    description: str | None = None,
    start_time: str | int | None = None,
    end_time: str | int | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit an A/B test or lift study and return previous and current values.

    Meta does not allow changing start_time once a study has started; end_time can move to any
    future time until the study ends. Times are ISO 8601 with offset or Unix seconds.
    """
    study_id = blank_to_none(study_id)
    if study_id is None:
        raise ValidationError("study_id is required.")
    if name is not None and not name.strip():
        raise ValidationError("name cannot be blank.")
    start = None if start_time is None else _to_unix(start_time, "start_time")
    end = None if end_time is None else _to_unix(end_time, "end_time")
    payload = merge_graph_payload(
        {
            "name": OMIT if name is None else name.strip(),
            "description": OMIT if description is None else description,
            "start_time": OMIT if start is None else start,
            "end_time": OMIT if end is None else end,
        },
        params,
    )
    if not payload:
        raise ValidationError("At least one field must be provided for update_ad_study.")
    if start is not None and end is not None and end <= start:
        raise ValidationError("end_time must be after start_time.")
    observation_end = (
        _to_unix(payload["observation_end_time"], "params.observation_end_time")
        if payload.get("observation_end_time") is not None
        else None
    )
    client = get_graph_api_client()
    previous = await client.get_object(study_id, fields=STUDY_WRITE_READ_FIELDS)
    now = _now()
    state = _study_state(previous, now)
    previous_start = _parse_meta_time(previous.get("start_time"))
    if start is not None and previous_start is not None and previous_start <= now:
        raise ValidationError("Meta does not allow changing start_time after a study has started.")
    if end is not None:
        if state in {"ended", "canceled"}:
            raise ValidationError(f"end_time can only change before a study ends; this study is {state}.")
        if end <= int(now.timestamp()):
            raise ValidationError("end_time must be in the future; use cancel_ad_study to end a study now.")
    _check_effective_times(previous, start, end, observation_end)
    await client.update_object(study_id, data=payload)
    current = {
        key: _iso(value) if key in {"start_time", "end_time"} and isinstance(value, int) else value
        for key, value in payload.items()
    }
    return mutation_response(
        action="update_ad_study",
        target={"study_id": study_id},
        previous=_previous_study(previous, state),
        current=current,
    )


@mcp_server.tool()
async def cancel_ad_study(study_id: str) -> dict[str, Any]:
    """End or cancel an A/B test or lift study. Use only when the user explicitly asks to stop a study.

    A study that has not started is deleted. A running study is ended by moving its end_time to
    one minute from now, which keeps its results; one already ending within that minute is left
    to finish, so its end never moves later. A study already ended or canceled is left alone.
    Returns the prior state.
    """
    study_id = blank_to_none(study_id)
    if study_id is None:
        raise ValidationError("study_id is required.")
    client = get_graph_api_client()
    previous = await client.get_object(study_id, fields=STUDY_WRITE_READ_FIELDS)
    now = _now()
    state = _study_state(previous, now)
    prior = _previous_study(previous, state)
    if state == "scheduled":
        result = await client.delete_object(study_id)
        current: dict[str, Any] = {"operation": "delete", "result": result}
    elif state == "running":
        new_end = int((now + timedelta(seconds=END_EARLY_GRACE_SECONDS)).timestamp())
        stored_end = _parse_meta_time(previous.get("end_time"))
        if stored_end is not None and int(stored_end.timestamp()) <= new_end:
            # Moving end_time to now plus the grace interval would push this end later, not earlier.
            response = mutation_response(
                action="cancel_ad_study",
                target={"study_id": study_id},
                previous=prior,
                current={"operation": "none", "end_time": stored_end.isoformat()},
                ok=False,
            )
            response["message"] = (
                f"Nothing was sent: the study already ends at {stored_end.isoformat()}, within the "
                f"{END_EARLY_GRACE_SECONDS} seconds this tool would set, so it will finish on its own shortly. "
                "Changing end_time now would only make it end later."
            )
            return response
        result = await client.update_object(study_id, data={"end_time": new_end})
        current = {"operation": "end_early", "end_time": _iso(new_end), "result": result}
    else:
        response = mutation_response(
            action="cancel_ad_study",
            target={"study_id": study_id},
            previous=prior,
            current={"operation": "none"},
            ok=False,
        )
        response["message"] = (
            f"Nothing was sent: the study is {state}."
            if state != "unknown"
            else "Nothing was sent: the study's start and end times could not be read."
        )
        return response
    return mutation_response(
        action="cancel_ad_study",
        target={"study_id": study_id},
        previous=prior,
        current=current,
    )
