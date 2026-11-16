"""Every tool carries the four MCP hints, and clients see them where they look.

The table lives in `meta_ads_mcp/tool_annotations.py` and is applied to the
registered tools at start-up, so no tool definition file carries annotations.
`tool_contracts._tool_effect` reads `destructiveHint` and `readOnlyHint` before
its name rules, so the hints must give every tool the same read/write answer the
name rules gave; otherwise intent routing would change.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from meta_ads_mcp import stdio  # noqa: F401 - import registers all tools
from meta_ads_mcp import tool_annotations
from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.coordinator import ALWAYS_VISIBLE_TOOLS, mcp_server, serialize_search_results_compact
from meta_ads_mcp.read_only import read_only_tool_names
from meta_ads_mcp.tool_annotations import (
    SEARCH_TOOL_ANNOTATIONS,
    TOOL_ANNOTATIONS,
    apply_tool_annotations,
    call_tool_annotations,
    describe_hints,
)
from meta_ads_mcp.tool_contracts import _tool_effect
from meta_ads_mcp.tools import ads, campaigns, execution

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def registered_tools() -> list[Tool]:
    return [c for c in mcp_server.local_provider._components.values() if isinstance(c, Tool)]


def registered_names() -> set[str]:
    return {tool.name for tool in registered_tools()}


def name_rule_effect(tool: Tool) -> str:
    """What _tool_effect answered before annotations existed (tags, then names)."""
    return _tool_effect(tool.model_copy(update={"annotations": None}), tool.name)


def hint(name: str, which: str) -> bool:
    return getattr(TOOL_ANNOTATIONS[name], which)


def names_where(which: str, value: bool = True) -> set[str]:
    return {name for name in TOOL_ANNOTATIONS if hint(name, which) is value}


# --- The table covers the catalogue exactly -------------------------------------


def test_every_registered_tool_has_an_entry_and_every_entry_is_a_registered_tool() -> None:
    missing = registered_names() - set(TOOL_ANNOTATIONS)
    unknown = set(TOOL_ANNOTATIONS) - registered_names()
    assert not missing, f"Tools with no entry in tool_annotations.TOOL_ANNOTATIONS: {sorted(missing)}"
    assert not unknown, f"Entries in TOOL_ANNOTATIONS that name no registered tool: {sorted(unknown)}"
    assert len(TOOL_ANNOTATIONS) == 180


def test_no_tool_is_listed_twice_in_the_table_source() -> None:
    """A repeated key in a dict literal silently keeps the last one; read the source instead."""
    source = Path(tool_annotations.__file__).read_text()
    tree = ast.parse(source)
    table = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "_TABLE"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    )
    assert isinstance(table, ast.Dict)
    keys = [key.value for key in table.keys if isinstance(key, ast.Constant)]
    assert len(keys) == len(table.keys)
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"Listed more than once: {duplicates}"


def test_every_entry_sets_all_four_hints_as_booleans() -> None:
    for name, annotations in TOOL_ANNOTATIONS.items():
        for which in HINTS:
            assert isinstance(getattr(annotations, which), bool), (name, which)


def test_every_registered_tool_carries_its_table_entry() -> None:
    for tool in registered_tools():
        assert tool.annotations is not None, tool.name
        for which in HINTS:
            assert getattr(tool.annotations, which) == hint(tool.name, which), (tool.name, which)


# --- Routing and the read-only gate keep their answers -------------------------


def test_name_rules_today_give_64_writes_and_116_reads() -> None:
    effects = [name_rule_effect(tool) for tool in registered_tools()]
    assert effects.count("write") == 64
    assert effects.count("read") == 116


def test_read_only_hint_is_the_opposite_of_the_name_rule_answer() -> None:
    for tool in registered_tools():
        expected_read_only = name_rule_effect(tool) == "read"
        assert hint(tool.name, "readOnlyHint") is expected_read_only, tool.name


def test_tool_effect_answers_the_same_with_and_without_annotations() -> None:
    for tool in registered_tools():
        assert tool.annotations is not None
        assert _tool_effect(tool, tool.name) == name_rule_effect(tool), tool.name


def test_every_tool_the_read_only_gate_blocks_is_not_marked_read_only() -> None:
    blocked = registered_names() - read_only_tool_names()
    assert len(blocked) == 63
    for name in blocked:
        assert hint(name, "readOnlyHint") is False, name


def test_the_one_write_the_read_only_gate_allows_is_the_local_export_delete() -> None:
    allowed_writes = {name for name in read_only_tool_names() & registered_names() if not hint(name, "readOnlyHint")}
    assert allowed_writes == {"delete_overflow_artifact"}


def test_marked_tools_count() -> None:
    assert len(names_where("readOnlyHint")) == 116
    assert len(names_where("destructiveHint")) == 39
    assert len(names_where("idempotentHint")) == 151
    assert len(names_where("openWorldHint")) == 6


# --- destructiveHint -------------------------------------------------------------


def test_read_only_tools_are_never_destructive_and_always_idempotent() -> None:
    for name in names_where("readOnlyHint"):
        assert hint(name, "destructiveHint") is False, name
        assert hint(name, "idempotentHint") is True, name


def test_every_delete_and_update_and_named_remover_is_destructive() -> None:
    named = {
        "remove_audience_users",
        "disconnect_catalog_event_source",
        "cancel_ad_study",
        "set_catalog_access",
        "batch_update_catalog_products",
        "delete_overflow_artifact",
    }
    prefixed = {name for name in TOOL_ANNOTATIONS if name.startswith(("delete_", "update_"))}
    for name in named | prefixed:
        assert hint(name, "destructiveHint") is True, name


def test_status_tools_are_destructive_because_archived_cannot_be_undone() -> None:
    """ARCHIVED stops delivery for good, so a status change is the worst a status tool can do."""
    assert "ARCHIVED" in execution.SETTABLE_STATUSES
    assert "ARCHIVED" in ads.UPDATABLE_AD_STATUSES
    assert "ARCHIVED" in campaigns._UPDATABLE_ADSET_STATUSES
    for name in ("set_campaign_status", "set_adset_status", "set_ad_status", "update_ad", "update_adset"):
        assert hint(name, "destructiveHint") is True, name
        assert hint(name, "idempotentHint") is True, name


def test_every_tool_that_sets_the_status_of_an_existing_object_is_destructive() -> None:
    """A status parameter on an existing object can archive it (or pass DELETED through to Meta).

    Creates and copies also take a status, but they set it on a new object, so
    nothing that existed before is lost.
    """
    takes_status = {
        tool.name
        for tool in registered_tools()
        if "status" in tool.parameters.get("properties", {})
        and not tool.name.startswith(("create_", "duplicate_"))
    }
    assert takes_status == {
        "set_campaign_status",
        "set_adset_status",
        "set_ad_status",
        "update_campaign",
        "update_adset",
        "update_ad",
        "update_creative",
    }
    for name in takes_status:
        assert hint(name, "destructiveHint") is True, name


def test_adding_tools_are_not_destructive() -> None:
    for name in (
        "add_audience_users",
        "connect_catalog_event_source",
        "boost_instagram_post",
        "upload_creative_asset",
        "upload_ad_video",
        "duplicate_campaign",
        "duplicate_adset",
        "duplicate_ad",
        "create_campaign",
        "create_ad",
        "create_ad_set",
        "setup_ab_test",
        "exchange_code_for_token",
        "refresh_to_long_lived_token",
        "generate_system_user_token",
    ):
        assert hint(name, "destructiveHint") is False, name


def test_creates_that_can_overwrite_or_delete_existing_items_are_destructive() -> None:
    # allow_upsert=true overwrites an existing product with the same retailer_id.
    assert hint("create_catalog_product", "destructiveHint") is True
    # Without update_only=true, items missing from the uploaded file may be deleted.
    assert hint("create_product_feed_upload", "destructiveHint") is True


def test_destructive_tools_list() -> None:
    assert names_where("destructiveHint") == {
        "batch_update_catalog_products",
        "cancel_ad_study",
        "create_catalog_product",
        "create_product_feed_upload",
        "delete_ad",
        "delete_ad_image",
        "delete_adset",
        "delete_audience",
        "delete_campaign",
        "delete_catalog_product",
        "delete_creative",
        "delete_feed_rule",
        "delete_overflow_artifact",
        "delete_product_feed",
        "delete_product_set",
        "disconnect_catalog_event_source",
        "remove_audience_users",
        "set_ad_status",
        "set_adset_status",
        "set_campaign_status",
        "set_catalog_access",
        "update_ad",
        "update_ad_creative",
        "update_ad_creative_enhancements",
        "update_ad_study",
        "update_adset",
        "update_adset_bid_amount",
        "update_adset_bid_strategy",
        "update_adset_budget",
        "update_campaign",
        "update_campaign_bid_strategy",
        "update_campaign_budget",
        "update_catalog",
        "update_catalog_product",
        "update_creative",
        "update_custom_audience",
        "update_feed_rule",
        "update_product_feed",
        "update_product_set",
    }


# --- idempotentHint ----------------------------------------------------------------


def test_tools_that_make_a_new_object_or_upload_rows_are_not_idempotent() -> None:
    writes = names_where("readOnlyHint", False)
    makers = {name for name in writes if name.startswith(("create_", "duplicate_", "upload_"))}
    makers |= {
        "setup_ab_test",
        "boost_instagram_post",
        "add_audience_users",
        "remove_audience_users",
        "batch_update_catalog_products",
        "update_ad_creative_enhancements",
        "exchange_code_for_token",
        "refresh_to_long_lived_token",
        "generate_system_user_token",
    }
    for name in makers:
        assert hint(name, "idempotentHint") is False, name
    assert names_where("idempotentHint", False) == makers


def test_deletes_status_sets_and_absolute_updates_are_idempotent() -> None:
    for name in TOOL_ANNOTATIONS:
        if name.startswith(("delete_", "set_")) or (
            name.startswith("update_") and name != "update_ad_creative_enhancements"
        ):
            assert hint(name, "idempotentHint") is True, name
    assert hint("connect_catalog_event_source", "idempotentHint") is True
    assert hint("disconnect_catalog_event_source", "idempotentHint") is True
    assert hint("cancel_ad_study", "idempotentHint") is True


# --- openWorldHint -----------------------------------------------------------------


def test_open_world_tools_are_the_public_ad_library_and_web_address_imports() -> None:
    assert names_where("openWorldHint") == {
        "search_ads_archive",
        "upload_creative_asset",
        "upload_ad_video",
        "create_product_feed",
        "update_product_feed",
        "create_product_feed_upload",
    }


# --- What a client receives --------------------------------------------------------


def _listed_tools(read_only: bool, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("META_READ_ONLY", "true" if read_only else "false")
    reload_settings()

    async def run():
        async with Client(mcp_server) as client:
            return await client.list_tools()

    return {tool.name: tool for tool in asyncio.run(run())}


def _as_tuple(annotations) -> tuple[bool | None, ...]:
    return tuple(getattr(annotations, which) for which in HINTS)


def test_directly_listed_tools_carry_their_hints(monkeypatch) -> None:
    listed = _listed_tools(False, monkeypatch)
    assert set(listed) == {*ALWAYS_VISIBLE_TOOLS, "search_tools", "call_tool"}
    for name in ALWAYS_VISIBLE_TOOLS:
        assert _as_tuple(listed[name].annotations) == _as_tuple(TOOL_ANNOTATIONS[name]), name
    assert _as_tuple(listed["search_tools"].annotations) == (True, False, True, False)
    assert _as_tuple(listed["call_tool"].annotations) == (False, True, False, True)


def test_call_tool_takes_the_hints_of_the_most_dangerous_tool_it_can_reach() -> None:
    reachable = set(TOOL_ANNOTATIONS)
    expected = ToolAnnotations(
        readOnlyHint=all(hint(name, "readOnlyHint") for name in reachable),
        destructiveHint=any(hint(name, "destructiveHint") for name in reachable),
        idempotentHint=all(hint(name, "idempotentHint") for name in reachable),
        openWorldHint=any(hint(name, "openWorldHint") for name in reachable),
    )
    assert _as_tuple(call_tool_annotations(read_only=False)) == _as_tuple(expected)


def test_call_tool_in_read_only_mode_is_still_not_read_only(monkeypatch) -> None:
    """delete_overflow_artifact stays reachable in read-only mode and deletes a local file."""
    reachable = set(TOOL_ANNOTATIONS) & read_only_tool_names()
    assert "delete_overflow_artifact" in reachable
    assert {name for name in reachable if not hint(name, "readOnlyHint")} == {"delete_overflow_artifact"}
    assert _as_tuple(call_tool_annotations(read_only=True)) == (False, True, True, True)
    listed = _listed_tools(True, monkeypatch)
    assert _as_tuple(listed["call_tool"].annotations) == (False, True, True, True)
    assert _as_tuple(listed["search_tools"].annotations) == (True, False, True, False)


def test_search_tool_annotations_constant() -> None:
    assert _as_tuple(SEARCH_TOOL_ANNOTATIONS) == (True, False, True, False)


def test_hint_text_names_each_hint_plainly() -> None:
    text = describe_hints(TOOL_ANNOTATIONS["delete_campaign"])
    assert text == "read-only: no, destructive: yes, idempotent: yes, open-world: no"
    assert "+" not in text
    assert describe_hints(None) == ""


def test_compact_search_output_gives_every_tool_its_hints() -> None:
    for tool in registered_tools():
        line = serialize_search_results_compact([tool]).splitlines()[1]
        assert line.endswith(f" | {describe_hints(TOOL_ANNOTATIONS[tool.name])}"), tool.name


_MATCH_LINE = re.compile(r"^- `(?P<name>[a-z0-9_]+)` \|")


def _search(query: str) -> list[tuple[str, str]]:
    result = asyncio.run(mcp_server.call_tool("search_tools", {"query": query}))
    text = "".join(block.text for block in result.content)
    matches = []
    for line in text.splitlines():
        found = _MATCH_LINE.match(line)
        if found:
            matches.append((found.group("name"), line))
    return matches


@pytest.mark.parametrize(
    "query",
    [
        "delete campaign",
        "pause ad set status",
        "search public ads library competitor",
        "upload image creative asset",
        "campaign performance insights",
        "create product feed upload",
    ],
)
def test_each_tool_returned_by_search_tools_carries_its_hints(query) -> None:
    matches = _search(query)
    assert matches, query
    for name, line in matches:
        assert line.endswith(f" | {describe_hints(TOOL_ANNOTATIONS[name])}"), line


def test_search_results_show_read_only_destructive_and_open_world_tools() -> None:
    seen = {name for query in ("delete campaign", "search public ads library competitor") for name, _ in _search(query)}
    assert "delete_campaign" in seen
    assert "search_ads_archive" in seen
    destructive_line = dict(_search("delete campaign"))["delete_campaign"]
    assert destructive_line.endswith("read-only: no, destructive: yes, idempotent: yes, open-world: no")
    archive_line = dict(_search("search public ads library competitor"))["search_ads_archive"]
    assert archive_line.endswith("read-only: yes, destructive: no, idempotent: yes, open-world: yes")


# --- How the table is applied -------------------------------------------------------


def test_apply_keeps_order_and_behaviour_and_skips_tools_without_an_entry(monkeypatch) -> None:
    server = FastMCP("annotation-probe")

    @server.tool()
    def first_probe() -> str:
        return "one"

    @server.tool()
    def second_probe() -> str:
        return "two"

    @server.tool()
    def unlisted_probe() -> str:
        return "three"

    table = {
        "first_probe": ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        "second_probe": ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
    }
    monkeypatch.setattr(tool_annotations, "TOOL_ANNOTATIONS", table)
    apply_tool_annotations(server)
    apply_tool_annotations(server)  # a second run changes nothing

    tools = [c for c in server.local_provider._components.values() if isinstance(c, Tool)]
    assert [tool.name for tool in tools] == ["first_probe", "second_probe", "unlisted_probe"]
    assert tools[0].annotations == table["first_probe"]
    assert tools[1].annotations == table["second_probe"]
    assert tools[2].annotations is None

    async def run():
        async with Client(server) as client:
            listed = {tool.name: tool for tool in await client.list_tools()}
            called = await client.call_tool("second_probe", {})
            return listed, called

    listed, called = asyncio.run(run())
    assert listed["second_probe"].annotations.destructiveHint is True
    assert called.data == "two"
