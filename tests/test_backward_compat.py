"""Existing artifacts call this server, so no tool on master changes its contract by accident.

`tests/fixtures/master_tool_contract.json` records, for every tool on master at
7fc48d1, its parameters, which are required, and their defaults.

A tool may always gain optional parameters. Anything else (removing or renaming
a tool, removing a parameter, making one required, changing a default) is
allowed only as a deliberate improvement, and must be declared in
`tests/fixtures/intentional_contract_changes.json` with why it is better for the
person using the tool and what a caller has to change. That file is the list
artifacts are reworked from. An undeclared change fails; so does a declaration
that no longer matches a real change.
"""

from __future__ import annotations

import json
from pathlib import Path

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.coordinator import mcp_server

FIXTURES = Path(__file__).parent / "fixtures"
MASTER_CONTRACT = json.loads((FIXTURES / "master_tool_contract.json").read_text())
DECLARED = json.loads((FIXTURES / "intentional_contract_changes.json").read_text())["changes"]

DETECTED_KINDS = {"removed_tool", "removed_parameter", "new_required_parameter", "default_changed"}
ALL_KINDS = DETECTED_KINDS | {"behaviour"}


def current_contract() -> dict[str, dict[str, object]]:
    contract: dict[str, dict[str, object]] = {}
    for tool in mcp_server.local_provider._components.values():
        name = getattr(tool, "name", None)
        schema = getattr(tool, "parameters", None)
        if not isinstance(name, str) or not isinstance(schema, dict):
            continue
        properties = schema.get("properties", {})
        contract[name] = {
            "parameters": set(properties),
            "required": set(schema.get("required", [])),
            "defaults": {key: value["default"] for key, value in properties.items() if "default" in value},
        }
    return contract


def detect_changes(before: dict, now: dict) -> set[tuple[str, str, str]]:
    """Return (tool, kind, parameter) for every contract change; parameter is "" for a removed tool."""
    changes: set[tuple[str, str, str]] = set()
    for name, old in before.items():
        if name not in now:
            changes.add((name, "removed_tool", ""))
            continue
        new = now[name]
        for parameter in set(old["parameters"]) - set(new["parameters"]):
            changes.add((name, "removed_parameter", parameter))
        for parameter in set(new["required"]) - set(old["required"]):
            changes.add((name, "new_required_parameter", parameter))
        for parameter, value in old["defaults"].items():
            if parameter in new["parameters"] and new["defaults"].get(parameter, "<no default>") != value:
                changes.add((name, "default_changed", parameter))
    return changes


def declared_keys() -> set[tuple[str, str, str]]:
    return {
        (entry["tool"], entry["kind"], entry.get("parameter", ""))
        for entry in DECLARED
        if entry["kind"] in DETECTED_KINDS
    }


def test_fixture_covers_master() -> None:
    assert len(MASTER_CONTRACT) == 102


def test_every_contract_change_is_declared() -> None:
    undeclared = detect_changes(MASTER_CONTRACT, current_contract()) - declared_keys()
    assert not undeclared, (
        "A tool on master changed its call contract without a declaration in "
        f"tests/fixtures/intentional_contract_changes.json: {sorted(undeclared)}"
    )


def test_no_declaration_is_stale() -> None:
    stale = declared_keys() - detect_changes(MASTER_CONTRACT, current_contract())
    assert not stale, f"Declared contract changes that are not real changes: {sorted(stale)}"


def test_every_declaration_says_why_and_how_to_migrate() -> None:
    for entry in DECLARED:
        label = f"{entry.get('tool')} {entry.get('kind')} {entry.get('parameter', '')}"
        assert entry["tool"] in MASTER_CONTRACT, f"{label}: not a tool on master"
        assert entry["kind"] in ALL_KINDS, f"{label}: unknown kind"
        assert len(entry.get("why_better", "").strip()) >= 40, f"{label}: say why this is better for the user"
        assert len(entry.get("migration", "").strip()) >= 20, f"{label}: say what a caller must change"
        if entry["kind"] == "behaviour":
            assert len(entry.get("what_changed", "").strip()) >= 20, f"{label}: say what changed"


def test_detector_catches_each_kind_of_change() -> None:
    """The guard itself must be able to fail."""
    before = {"t": {"parameters": ["a", "b"], "required": ["a"], "defaults": {"b": 1}}}
    same = {"t": {"parameters": {"a", "b"}, "required": {"a"}, "defaults": {"b": 1}}}
    assert detect_changes(before, same) == set()
    assert detect_changes(before, {}) == {("t", "removed_tool", "")}
    assert detect_changes(before, {"t": {**same["t"], "parameters": {"a"}}}) == {("t", "removed_parameter", "b")}
    assert detect_changes(before, {"t": {**same["t"], "required": {"a", "b"}}}) == {("t", "new_required_parameter", "b")}
    assert detect_changes(before, {"t": {**same["t"], "defaults": {"b": 2}}}) == {("t", "default_changed", "b")}
    grown = {"t": {"parameters": {"a", "b", "c"}, "required": {"a"}, "defaults": {"b": 1, "c": None}}}
    assert detect_changes(before, grown) == set()
