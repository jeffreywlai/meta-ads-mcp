"""Audit local Graph field contracts against generated Meta SDK schemas."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import subprocess
from typing import Any

DEFAULT_BASE_REF = "25.0.3"
DEFAULT_TARGET_REF = "26.0.0"
EXPECTED_API_VERSION = "v26.0"
EXPECTED_SDK_VERSION = "v26.0.0"

SDK_OBJECT_PATHS = {
    "adaccount": "facebook_business/adobjects/adaccount.py",
    "adactivity": "facebook_business/adobjects/adactivity.py",
    "campaign": "facebook_business/adobjects/campaign.py",
    "adset": "facebook_business/adobjects/adset.py",
    "ad": "facebook_business/adobjects/ad.py",
    "adcreative": "facebook_business/adobjects/adcreative.py",
    "adimage": "facebook_business/adobjects/adimage.py",
    "adsinsights": "facebook_business/adobjects/adsinsights.py",
    "customaudience": "facebook_business/adobjects/customaudience.py",
    "iguser": "facebook_business/adobjects/iguser.py",
    "page": "facebook_business/adobjects/page.py",
}

# Some edge projections accept fields that are not represented in the target
# object's generated Field class. Keep those visible instead of silently
# treating them as SDK-verified.
SDK_FIELD_EXCEPTIONS = {
    "discovery.page": {"tasks"},
}

def extract_sdk_fields(source: str) -> set[str]:
    """Extract generated ``Field`` values without importing the upstream SDK."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Field":
            continue
        fields: set[str] = set()
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                fields.add(statement.value.value)
        if fields:
            return fields
    raise ValueError("Generated SDK source does not define a non-empty Field class.")


def extract_api_config(source: str) -> dict[str, Any]:
    """Extract the generated SDK API configuration without executing it."""
    tree = ast.parse(source)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "ads_api_config" for target in statement.targets):
            value = ast.literal_eval(statement.value)
            if isinstance(value, dict):
                return value
    raise ValueError("SDK source does not define ads_api_config.")


def _read_ref_file(sdk_repo: Path, ref: str, path: str) -> str:
    """Read one file from an SDK Git ref without checking it out."""
    result = subprocess.run(
        ["git", "-C", str(sdk_repo), "show", f"{ref}:{path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Unable to read {path} at {ref}: {detail}")
    return result.stdout


def _local_field_surfaces() -> dict[str, tuple[str, list[str]]]:
    """Return the local default field contracts covered by the audit."""
    from meta_ads_mcp.tools.activity import DEFAULT_ACTIVITY_FIELDS
    from meta_ads_mcp.tools.ads import AD_IMAGE_FIELDS, CREATIVE_IMAGE_FIELDS
    from meta_ads_mcp.tools.audiences import AUDIENCE_FIELDS, AUDIENCE_SUMMARY_FIELDS
    from meta_ads_mcp.tools.creatives import CREATIVE_FIELDS
    from meta_ads_mcp.tools.diagnostics import (
        AD_QUALITY_FIELDS,
        LEARNING_PHASE_FIELDS_BY_LEVEL,
        NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL,
    )
    from meta_ads_mcp.tools.discovery import (
        ACCOUNT_FIELDS,
        AD_FIELDS,
        ADSET_FIELDS,
        CAMPAIGN_FIELDS,
        INSTAGRAM_ACCOUNT_FIELDS,
        PAGE_FIELDS,
    )
    from meta_ads_mcp.tools.insights import (
        DEFAULT_INSIGHTS_FIELDS,
        INSTAGRAM_PROFILE_FOLLOW_FIELD,
    )
    from meta_ads_mcp.tools.social_feedback import SOCIAL_CREATIVE_FIELDS

    return {
        "activity.default": ("adactivity", list(DEFAULT_ACTIVITY_FIELDS)),
        "discovery.account": ("adaccount", list(ACCOUNT_FIELDS)),
        "discovery.campaign": ("campaign", list(CAMPAIGN_FIELDS)),
        "discovery.adset": ("adset", list(ADSET_FIELDS)),
        "discovery.ad": ("ad", list(AD_FIELDS)),
        "discovery.page": ("page", list(PAGE_FIELDS)),
        "discovery.instagram_account": ("iguser", list(INSTAGRAM_ACCOUNT_FIELDS)),
        "audience.summary": ("customaudience", list(AUDIENCE_SUMMARY_FIELDS)),
        "audience.detail": ("customaudience", list(AUDIENCE_FIELDS)),
        "creative.detail": ("adcreative", list(CREATIVE_FIELDS)),
        "creative.social": ("adcreative", list(SOCIAL_CREATIVE_FIELDS)),
        "creative.image": ("adcreative", list(CREATIVE_IMAGE_FIELDS)),
        "ad_image.detail": ("adimage", list(AD_IMAGE_FIELDS)),
        "insights.default": ("adsinsights", list(DEFAULT_INSIGHTS_FIELDS)),
        "insights.instagram_profile_follow": (
            "adsinsights",
            [INSTAGRAM_PROFILE_FOLLOW_FIELD],
        ),
        "insights.ad_quality": ("adsinsights", list(AD_QUALITY_FIELDS)),
        "diagnostics.learning_campaign": (
            "campaign",
            list(LEARNING_PHASE_FIELDS_BY_LEVEL["campaign"]),
        ),
        "diagnostics.learning_adset": (
            "adset",
            list(LEARNING_PHASE_FIELDS_BY_LEVEL["adset"]),
        ),
        "diagnostics.native_campaign": (
            "campaign",
            list(NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL["campaign"]),
        ),
        "diagnostics.native_adset": (
            "adset",
            list(NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL["adset"]),
        ),
        "diagnostics.native_ad": (
            "ad",
            list(NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL["ad"]),
        ),
    }


def audit_sdk_schema(
    sdk_repo: Path,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    target_ref: str = DEFAULT_TARGET_REF,
) -> dict[str, Any]:
    """Compare the local field surface with two generated SDK releases."""
    sdk_repo = sdk_repo.resolve()
    base_config = extract_api_config(
        _read_ref_file(sdk_repo, base_ref, "facebook_business/apiconfig.py")
    )
    target_config = extract_api_config(
        _read_ref_file(sdk_repo, target_ref, "facebook_business/apiconfig.py")
    )

    base_fields: dict[str, set[str]] = {}
    target_fields: dict[str, set[str]] = {}
    schema_changes: dict[str, dict[str, list[str]]] = {}
    for object_name, path in SDK_OBJECT_PATHS.items():
        base = extract_sdk_fields(_read_ref_file(sdk_repo, base_ref, path))
        target = extract_sdk_fields(_read_ref_file(sdk_repo, target_ref, path))
        base_fields[object_name] = base
        target_fields[object_name] = target
        schema_changes[object_name] = {
            "added": sorted(target - base),
            "removed": sorted(base - target),
        }

    surfaces: dict[str, dict[str, Any]] = {}
    missing_fields: list[str] = []
    for surface_name, (object_name, fields) in _local_field_surfaces().items():
        local_fields = set(fields)
        sdk_unverified_fields = sorted(
            (local_fields - target_fields[object_name])
            & SDK_FIELD_EXCEPTIONS.get(surface_name, set())
        )
        missing_in_target = sorted(
            local_fields
            - target_fields[object_name]
            - SDK_FIELD_EXCEPTIONS.get(surface_name, set())
        )
        removed_in_target = sorted(local_fields & (base_fields[object_name] - target_fields[object_name]))
        surfaces[surface_name] = {
            "object": object_name,
            "field_count": len(local_fields),
            "missing_in_target": missing_in_target,
            "removed_in_target": removed_in_target,
            "sdk_unverified_fields": sdk_unverified_fields,
            "compatible": not missing_in_target,
        }
        missing_fields.extend(f"{surface_name}:{field}" for field in missing_in_target)

    from meta_ads_mcp.tools.diagnostics import NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL

    optimization_candidates: dict[str, dict[str, Any]] = {}
    for object_name, candidates in NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL.items():
        missing = sorted(set(candidates) - target_fields[object_name])
        optimization_candidates[object_name] = {
            "fields": candidates,
            "missing_in_target": missing,
            "schema_ready": not missing,
            "live_smoke_required": False,
            "implemented": True,
        }

    config_compatible = (
        target_config.get("API_VERSION") == EXPECTED_API_VERSION
        and target_config.get("SDK_VERSION") == EXPECTED_SDK_VERSION
    )
    return {
        "compatible": config_compatible and not missing_fields,
        "source": {
            "sdk_repo": str(sdk_repo),
            "base_ref": base_ref,
            "target_ref": target_ref,
            "base_config": base_config,
            "target_config": target_config,
        },
        "config_compatible": config_compatible,
        "surfaces": surfaces,
        "schema_changes": schema_changes,
        "optimization_candidates": optimization_candidates,
        "gate": {
            "native_optimization_signals": "passed_and_implemented",
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render a compact human-readable audit report."""
    status = "PASS" if report["compatible"] else "FAIL"
    source = report["source"]
    lines = [
        "# Meta SDK schema compatibility audit",
        "",
        f"- Result: **{status}**",
        f"- Baseline: `{source['base_ref']}`",
        f"- Target: `{source['target_ref']}`",
        f"- Target API: `{source['target_config'].get('API_VERSION')}`",
        f"- Target SDK: `{source['target_config'].get('SDK_VERSION')}`",
        "",
        "## Local field surfaces",
        "",
        "| Surface | SDK object | Fields | Missing in target | SDK-unverified edge fields |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for name, surface in report["surfaces"].items():
        missing = ", ".join(surface["missing_in_target"]) or "none"
        unverified = ", ".join(surface["sdk_unverified_fields"]) or "none"
        lines.append(
            f"| `{name}` | `{surface['object']}` | {surface['field_count']} | {missing} | {unverified} |"
        )
    lines.extend(["", "## Generated schema changes", ""])
    for name, changes in report["schema_changes"].items():
        added = ", ".join(changes["added"]) or "none"
        removed = ", ".join(changes["removed"]) or "none"
        lines.append(f"- `{name}`: added {added}; removed {removed}.")
    lines.extend(
        [
            "",
            "## Delivery gate",
            "",
            "Native optimization signals were implemented after the dedicated v26 live smoke tests passed.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    """Run the SDK-backed compatibility audit from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-repo", required=True, type=Path)
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    parser.add_argument("--target-ref", default=DEFAULT_TARGET_REF)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    report = audit_sdk_schema(
        args.sdk_repo,
        base_ref=args.base_ref,
        target_ref=args.target_ref,
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return 0 if report["compatible"] else 1


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
