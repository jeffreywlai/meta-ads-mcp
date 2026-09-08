"""SDK-backed Marketing API schema audit tests."""

from __future__ import annotations

from pathlib import Path

from meta_ads_mcp import schema_audit
from meta_ads_mcp.tools.diagnostics import NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL


def _field_source(fields: set[str]) -> str:
    assignments = "\n".join(
        f"        field_{index} = {field!r}"
        for index, field in enumerate(sorted(fields))
    )
    return f"class Generated:\n    class Field:\n{assignments}\n"


def _fake_sources(*, missing_target: tuple[str, str] | None = None):
    fields_by_object = {
        object_name: set()
        for object_name in schema_audit.SDK_OBJECT_PATHS
    }
    for _, (object_name, fields) in schema_audit._local_field_surfaces().items():
        fields_by_object[object_name].update(fields)
    for surface_name, exceptions in schema_audit.SDK_FIELD_EXCEPTIONS.items():
        object_name, _ = schema_audit._local_field_surfaces()[surface_name]
        fields_by_object[object_name].difference_update(exceptions)
    for object_name, fields in NATIVE_OPTIMIZATION_FIELDS_BY_LEVEL.items():
        fields_by_object[object_name].update(fields)

    def read_ref_file(_repo: Path, ref: str, path: str) -> str:
        if path.endswith("apiconfig.py"):
            api_version = "v25.0" if ref == "25.0.3" else "v26.0"
            sdk_version = "v25.0.3" if ref == "25.0.3" else "v26.0.0"
            return f"ads_api_config = {{'API_VERSION': {api_version!r}, 'SDK_VERSION': {sdk_version!r}}}\n"
        object_name = next(
            name for name, object_path in schema_audit.SDK_OBJECT_PATHS.items()
            if object_path == path
        )
        fields = set(fields_by_object[object_name])
        if ref == "25.0.3" and object_name == "adsinsights":
            fields.add("removed_metric")
        if ref == "26.0.0" and missing_target == (object_name, "name"):
            fields.discard("name")
        return _field_source(fields)

    return read_ref_file


def test_extract_sdk_fields_does_not_import_generated_source() -> None:
    source = """
class Example:
    class Field:
        account_id = 'account_id'
        name = 'name'
"""
    assert schema_audit.extract_sdk_fields(source) == {"account_id", "name"}


def test_v26_audit_accepts_current_local_field_surfaces(monkeypatch) -> None:
    monkeypatch.setattr(schema_audit, "_read_ref_file", _fake_sources())

    report = schema_audit.audit_sdk_schema(Path("/tmp/fake-sdk"))

    assert report["compatible"] is True
    assert all(surface["compatible"] for surface in report["surfaces"].values())
    assert report["surfaces"]["discovery.page"]["sdk_unverified_fields"] == ["tasks"]
    assert report["schema_changes"]["adsinsights"]["removed"] == ["removed_metric"]
    assert report["gate"]["native_optimization_signals"] == "passed_and_implemented"
    assert report["surfaces"]["diagnostics.native_adset"]["compatible"] is True
    assert report["surfaces"]["insights.instagram_profile_follow"]["compatible"] is True


def test_v26_audit_fails_when_target_removes_a_local_field(monkeypatch) -> None:
    monkeypatch.setattr(
        schema_audit,
        "_read_ref_file",
        _fake_sources(missing_target=("campaign", "name")),
    )

    report = schema_audit.audit_sdk_schema(Path("/tmp/fake-sdk"))

    assert report["compatible"] is False
    assert report["surfaces"]["discovery.campaign"]["missing_in_target"] == ["name"]
    assert report["surfaces"]["diagnostics.learning_campaign"]["missing_in_target"] == ["name"]
