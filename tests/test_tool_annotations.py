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
import json
import re
from pathlib import Path

import httpx


def _refuse_http(*_args, **_kwargs):
    raise AssertionError("A test in test_tool_annotations.py tried an outgoing HTTP request.")


async def _refuse_http_async(*_args, **_kwargs):
    _refuse_http()


# Block outgoing HTTP before the server is imported; restored after the imports so other
# test modules keep their own HTTP fakes. The autouse fixture blocks it again per test.
_HTTP_METHODS = [
    (httpx.AsyncClient, "request", _refuse_http_async),
    (httpx.AsyncClient, "send", _refuse_http_async),
    (httpx.Client, "request", _refuse_http),
    (httpx.Client, "send", _refuse_http),
]
_ORIGINAL_HTTP = [(owner, name, getattr(owner, name)) for owner, name, _ in _HTTP_METHODS]
for _owner, _name, _blocker in _HTTP_METHODS:
    setattr(_owner, _name, _blocker)

import pytest  # noqa: E402
from fastmcp import Client, FastMCP  # noqa: E402
from fastmcp.tools import Tool  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from meta_ads_mcp import stdio  # noqa: E402,F401 - import registers all tools
from meta_ads_mcp import tool_annotations  # noqa: E402
from meta_ads_mcp.config import reload_settings  # noqa: E402
from meta_ads_mcp.coordinator import ALWAYS_VISIBLE_TOOLS, mcp_server, serialize_search_results_compact  # noqa: E402
from meta_ads_mcp.read_only import read_only_tool_names  # noqa: E402
from meta_ads_mcp.tool_annotations import (  # noqa: E402
    SEARCH_TOOL_ANNOTATIONS,
    TOOL_ANNOTATIONS,
    apply_tool_annotations,
    call_tool_annotations,
    describe_hints,
)
from meta_ads_mcp.tool_contracts import _tool_effect  # noqa: E402
from meta_ads_mcp.tools import ads, campaigns, execution  # noqa: E402
from meta_ads_mcp.graph_api import GraphAPIClient  # noqa: E402

for _owner, _name, _original in _ORIGINAL_HTTP:
    setattr(_owner, _name, _original)


@pytest.fixture(autouse=True)
def _no_outgoing_http(monkeypatch):
    for owner, name, blocker in _HTTP_METHODS:
        monkeypatch.setattr(owner, name, blocker)


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
    assert len(names_where("openWorldHint")) == 18


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


# The rule. openWorldHint is true when a call can give Meta the web address of a file
# (an image, a video, a thumbnail, a feed or data file) for Meta to fetch and import or
# render, through a named argument or a raw payload the tool forwards, or when the tool
# returns public content from outside the person's own assets. A landing-page or other
# destination link (link_url, a call-to-action link, url_tags, a promoted_object or
# tracking URL) does not count.
#
# Each open-world tool, with the argument path that makes it so.
OPEN_WORLD_PATHS = {
    "search_ads_archive": "returns ads from the public Ad Library",
    "upload_creative_asset": "image_url",
    "upload_ad_video": "video_url",
    "create_ad_creative": "object_story_spec.link_data.picture",
    "create_ad_creative_from_inputs": "image_url",
    "create_ad": "creative_spec.object_story_spec.link_data.picture",
    "preview_ad": "creative.object_story_spec.link_data.picture",
    "create_catalog_product": "image_url",
    "update_catalog_product": "image_url",
    "batch_update_catalog_products": "items[].image_url",
    "update_catalog": "params.default_image_url",
    "create_product_set": "params.metadata.cover_image_url",
    "create_product_feed": "schedule.url",
    "update_product_feed": "schedule.url",
    "create_product_feed_upload": "url",
    "create_feed_rule": "params.user_default_value (fallback_rule on image_link)",
    "update_feed_rule": "params.user_default_value (fallback_rule on image_link)",
    # Meta documents only name, status and labels on this route, but the tool forwards the
    # spec and no live call has shown Meta refuses it, so the cautious marking applies.
    "update_creative": "object_story_spec.link_data.picture",
}

FILE_ADDRESS = "https://assets.example.invalid/annotation-probe.png"
FEED_ADDRESS = "https://assets.example.invalid/annotation-probe-feed.csv"
_STORY = {
    "page_id": "5555555555",
    "link_data": {"link": "https://shop.example.invalid/", "picture": FILE_ADDRESS, "message": "probe"},
}

# A real call per open-world tool, carrying a file address in the listed argument path.
OPEN_WORLD_CALLS = {
    "upload_creative_asset": ({"account_id": "1234567890", "image_url": FILE_ADDRESS}, FILE_ADDRESS),
    "upload_ad_video": ({"account_id": "1234567890", "video_url": FILE_ADDRESS}, FILE_ADDRESS),
    "create_ad_creative": (
        {"account_id": "1234567890", "name": "probe", "object_story_spec": _STORY},
        FILE_ADDRESS,
    ),
    "create_ad_creative_from_inputs": (
        {
            "account_id": "1234567890",
            "name": "probe",
            "page_id": "5555555555",
            "link_url": "https://shop.example.invalid/",
            "image_url": FILE_ADDRESS,
        },
        FILE_ADDRESS,
    ),
    "create_ad": (
        {
            "account_id": "1234567890",
            "name": "probe",
            "adset_id": "2222222222",
            "creative_spec": {"object_story_spec": _STORY},
        },
        FILE_ADDRESS,
    ),
    "preview_ad": ({"account_id": "1234567890", "creative": {"object_story_spec": _STORY}}, FILE_ADDRESS),
    "create_catalog_product": (
        {
            "catalog_id": "6666666666",
            "retailer_id": "PROBE-SKU",
            "name": "probe",
            "price": 12.5,
            "currency": "USD",
            "url": "https://shop.example.invalid/probe",
            "image_url": FILE_ADDRESS,
        },
        FILE_ADDRESS,
    ),
    "update_catalog_product": ({"product_id": "8888888888", "image_url": FILE_ADDRESS}, FILE_ADDRESS),
    "batch_update_catalog_products": (
        {
            "catalog_id": "6666666666",
            "wait_seconds": 0,
            "items": [{"method": "update", "retailer_id": "PROBE-BATCH", "image_url": FILE_ADDRESS}],
        },
        FILE_ADDRESS,
    ),
    "update_catalog": (
        {"catalog_id": "6666666666", "params": {"default_image_url": FILE_ADDRESS}},
        FILE_ADDRESS,
    ),
    "create_product_set": (
        {
            "catalog_id": "6666666666",
            "name": "probe",
            "filter": {},
            "params": {"metadata": {"cover_image_url": FILE_ADDRESS}},
        },
        FILE_ADDRESS,
    ),
    "create_product_feed": (
        {
            "catalog_id": "6666666666",
            "name": "probe",
            "schedule": {"url": FEED_ADDRESS, "interval": "DAILY", "hour": 3},
        },
        FEED_ADDRESS,
    ),
    "update_product_feed": (
        {"feed_id": "7777777777", "schedule": {"url": FEED_ADDRESS, "interval": "DAILY", "hour": 3}},
        FEED_ADDRESS,
    ),
    "create_product_feed_upload": ({"feed_id": "7777777777", "url": FEED_ADDRESS}, FEED_ADDRESS),
    "create_feed_rule": (
        {
            "feed_id": "7777777777",
            "attribute": "image_link",
            "rule_type": "fallback_rule",
            "params": {"user_default_value": FILE_ADDRESS},
        },
        FILE_ADDRESS,
    ),
    "update_creative": ({"creative_id": "3333333333", "object_story_spec": _STORY}, FILE_ADDRESS),
    "update_feed_rule": (
        {"rule_id": "4444444444", "params": {"user_default_value": FILE_ADDRESS}},
        FILE_ADDRESS,
    ),
}

# Top-level parameter names that say the value is a file's web address.
_FILE_ADDRESS_NAME = re.compile(r"(image|video|thumbnail|file|picture|cover_image|feed)_url|image_link")


def test_open_world_tools_are_exactly_the_listed_ones_and_each_has_a_path() -> None:
    assert names_where("openWorldHint") == set(OPEN_WORLD_PATHS)
    assert all(OPEN_WORLD_PATHS.values())
    # Every listed tool except the public Ad Library search is proved by a real call below.
    assert set(OPEN_WORLD_CALLS) == set(OPEN_WORLD_PATHS) - {"search_ads_archive"}


class _RecordingGraph:
    """Stands in for GraphAPIClient.request: records each request, answers with fixed data."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []

    async def request(self, _client, method, endpoint, **kwargs):
        self.requests.append((method, endpoint, kwargs))
        if endpoint.endswith(("/previews", "/generatepreviews")):
            return {"data": [{"body": "<iframe></iframe>"}]}
        if endpoint == "ads_archive":
            return {"data": []}
        if method == "GET" and endpoint.endswith("/products") and "PROBE-BATCH" in json.dumps(kwargs):
            # The batch tool reads the catalog first and updates only a product that exists.
            return {"data": [{
                "id": "8888888888", "retailer_id": "PROBE-BATCH", "name": "probe",
                "image_url": "https://assets.example.invalid/old.png",
            }]}
        if method == "GET" and endpoint == "4444444444":
            return {
                "id": "4444444444",
                "attribute": "image_link",
                "rule_type": "fallback_rule",
                "params": {"user_default_value": "https://assets.example.invalid/old.png"},
            }
        if method == "GET":
            return {"id": endpoint.split("/")[0], "currency": "USD", "account_id": "1234567890", "data": []}
        return {"id": "9999999999", "success": True, "handles": ["probe-handle"]}

    def install(self, monkeypatch) -> None:
        recorder = self

        async def request(client, method, endpoint, **kwargs):
            return await recorder.request(client, method, endpoint, **kwargs)

        monkeypatch.setattr(GraphAPIClient, "request", request)

    def sent(self) -> str:
        """Everything the tool put in its requests, as one text, for a containment check."""
        return json.dumps(
            [{"endpoint": endpoint, **kwargs} for _method, endpoint, kwargs in self.requests],
            default=str,
        )


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("name", sorted(OPEN_WORLD_CALLS))
def test_each_open_world_tool_really_sends_meta_the_file_address(monkeypatch, name, routed) -> None:
    monkeypatch.setenv("META_READ_ONLY", "false")
    reload_settings()
    recorder = _RecordingGraph()
    recorder.install(monkeypatch)
    arguments, address = OPEN_WORLD_CALLS[name]
    tool_name, tool_arguments = (
        ("call_tool", {"name": name, "arguments": arguments}) if routed else (name, arguments)
    )

    asyncio.run(mcp_server.call_tool(tool_name, tool_arguments))

    assert recorder.requests, f"{name} built no request"
    assert address in recorder.sent(), f"{name}: the file address is not in any request it built"
    assert hint(name, "openWorldHint") is True


def test_the_ad_library_search_reads_meta_public_archive(monkeypatch) -> None:
    recorder = _RecordingGraph()
    recorder.install(monkeypatch)
    asyncio.run(
        mcp_server.call_tool("search_ads_archive", {"search_terms": "probe", "ad_reached_countries": ["US"]})
    )
    assert [endpoint for _method, endpoint, _kwargs in recorder.requests] == ["ads_archive"]


def test_every_parameter_named_as_a_file_address_is_on_an_open_world_tool() -> None:
    flagged = set()
    for tool in registered_tools():
        for parameter in tool.parameters.get("properties", {}):
            names_a_file = bool(_FILE_ADDRESS_NAME.search(parameter)) or (
                parameter == "url" and "feed" in tool.name
            )
            if names_a_file:
                flagged.add(tool.name)
                assert hint(tool.name, "openWorldHint") is True, (tool.name, parameter)
    assert flagged >= {"upload_creative_asset", "upload_ad_video", "create_product_feed_upload"}


def test_open_world_tools_shown_by_get_capabilities() -> None:
    for name in sorted(OPEN_WORLD_PATHS):
        answer = asyncio.run(mcp_server.call_tool("get_capabilities", {"tool_name": name}))
        assert answer.structured_content["tool"]["annotations"]["openWorldHint"] is True, name


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
