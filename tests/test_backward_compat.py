"""Existing artifacts call this server, so a tool already on master keeps its call contract.

`tests/fixtures/master_tool_contract.json` records, for every tool on master at
7fc48d1, its parameters, which are required, and their defaults. A tool may gain
optional parameters. It may not be removed or renamed, lose a parameter, gain a
required parameter, or change a default. To change the contract on purpose,
update the fixture in the same commit and say why in the commit message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp.coordinator import mcp_server

MASTER_CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "master_tool_contract.json").read_text()
)


def _current_contract() -> dict[str, dict[str, object]]:
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


CURRENT = _current_contract()


def test_fixture_covers_master() -> None:
    assert len(MASTER_CONTRACT) == 102


def test_no_master_tool_was_removed_or_renamed() -> None:
    assert sorted(set(MASTER_CONTRACT) - set(CURRENT)) == []


@pytest.mark.parametrize("name", sorted(MASTER_CONTRACT))
def test_master_tool_keeps_its_call_contract(name: str) -> None:
    before = MASTER_CONTRACT[name]
    now = CURRENT[name]
    removed = set(before["parameters"]) - now["parameters"]
    assert not removed, f"{name} lost parameters: {sorted(removed)}"
    newly_required = now["required"] - set(before["required"])
    assert not newly_required, f"{name} gained required parameters: {sorted(newly_required)}"
    changed = {
        key: (value, now["defaults"].get(key, "<no default>"))
        for key, value in before["defaults"].items()
        if now["defaults"].get(key, "<no default>") != value
    }
    assert not changed, f"{name} changed defaults (before, now): {changed}"
