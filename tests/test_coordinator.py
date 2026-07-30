"""Coordinator / FastMCP server configuration tests."""

from __future__ import annotations

import asyncio

from meta_ads_mcp import stdio  # noqa: F401 - ensures tools are registered
from meta_ads_mcp.config import Settings
from meta_ads_mcp.coordinator import (
    ALWAYS_VISIBLE_TOOLS,
    MAX_TOOL_RESPONSE_BYTES,
    RESPONSE_LIMIT_HINT,
    mcp_server,
    serialize_search_results_compact,
)
from meta_ads_mcp.tools import discovery, insights, utility


def test_fastmcp_31_search_transform_is_configured() -> None:
    transforms = getattr(mcp_server, "transforms", [])
    assert transforms
    transform = transforms[0]
    assert type(transform).__name__ == "IntentAwareBM25SearchTransform"
    assert sorted(getattr(transform, "_always_visible", set())) == sorted(ALWAYS_VISIBLE_TOOLS)
    assert getattr(transform, "_search_result_serializer", None) is serialize_search_results_compact


def test_response_size_guard_is_configured() -> None:
    middleware = mcp_server.middleware[-1]
    assert type(middleware).__name__ == "ArchivedResponseLimitingMiddleware"
    assert middleware.max_size == MAX_TOOL_RESPONSE_BYTES
    assert middleware.truncation_suffix == RESPONSE_LIMIT_HINT


def test_list_tools_exposes_compact_search_surface() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    names = [tool.name for tool in tools]
    assert names == [
        "list_ad_accounts",
        "health_check",
        "get_capabilities",
        "search_tools",
        "call_tool",
    ]


def test_historical_missing_tools_remain_visible_on_compact_surface() -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.list_tools())}
    assert {"health_check", "list_ad_accounts"} <= names


def test_historical_missing_tools_respond_through_tool_layer(monkeypatch) -> None:
    class FakeDiscoveryClient:
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            assert parent_id == "me"
            assert edge == "adaccounts"
            return {"data": [{"id": "act_123", "name": "Test Account", "account_status": 1}]}

    monkeypatch.setattr(
        utility,
        "get_settings",
        lambda: Settings(
            access_token=None,
            api_version="v25.0",
            default_account_id=None,
            app_id=None,
            app_secret=None,
            redirect_uri=None,
            log_level="INFO",
            host="127.0.0.1",
            port=8000,
            request_timeout=30.0,
            max_retries=2,
        ),
    )
    monkeypatch.setattr(discovery, "get_graph_api_client", lambda: FakeDiscoveryClient())

    health = asyncio.run(mcp_server.call_tool("health_check", {}))
    accounts = asyncio.run(mcp_server.call_tool("list_ad_accounts", {"limit": 1}))

    assert health.structured_content["status"] == "unhealthy"
    assert accounts.structured_content["items"][0]["id"] == "act_123"


def test_search_routes_feedback_and_action_count_workflows() -> None:
    async def search(query: str) -> str:
        result = await mcp_server.call_tool("search_tools", {"query": query})
        return result.structured_content["result"]

    feedback = asyncio.run(search("feedback reviews testimonials"))
    raw_comments = asyncio.run(search("facebook ad comments"))
    page_reviews = asyncio.run(search("page reviews testimonials"))
    actions = asyncio.run(search("how many appointments campaign trailing 30 days"))
    campaign_lookup = asyncio.run(search("find campaign by name"))
    terse_campaigns = asyncio.run(search("campaigns"))
    terse_actions = asyncio.run(search("appointments last 30 days"))
    terse_pause = asyncio.run(search("pause ad set"))
    creative_read = asyncio.run(
        search("full creative spec by id including url_tags asset_feed_spec degrees_of_freedom")
    )
    terse_creative_reads = [
        asyncio.run(search("fetch creative 123")),
        asyncio.run(search("creative 123 details")),
        asyncio.run(search("what is creative 123")),
        asyncio.run(search("read creative 123 do not change it")),
        asyncio.run(search("do not delete creative 123 just read it")),
        asyncio.run(search("read-only creative 123")),
        asyncio.run(search("creative details, not delete")),
        asyncio.run(search("do not rename creative 123, just inspect it")),
        asyncio.run(search("do not upload creative 123, show details")),
        asyncio.run(search("never create a creative, just inspect existing creative 123")),
        asyncio.run(search("show updated creative 123 details")),
        asyncio.run(search("get modified creative 123")),
        asyncio.run(search("view edited creative 123")),
        asyncio.run(search("retrieve deleted creative 123")),
        asyncio.run(search("show created creative 123")),
    ]
    delete_creative = asyncio.run(search("remove creative 123"))
    update_creative = asyncio.run(search("update creative 123"))
    performance_report = asyncio.run(search("get creative performance report"))
    mixed_intent = asyncio.run(search("do not delete creative; update its name instead"))
    creative_list = asyncio.run(search("show creatives in account 123"))
    creative_comments = asyncio.run(search("read creative comments"))
    creative_image = asyncio.run(search("get creative image for ad 123"))
    narrow_remove = asyncio.run(search("remove image from creative 123"))
    detach_creative = asyncio.run(search("remove creative from ad 123"))
    overflow_read = asyncio.run(search("get full oversized MCP response"))
    overflow_download = asyncio.run(search("download oversized response"))
    overflow_negated_delete = asyncio.run(
        search("do not delete overflow artifact, read it")
    )
    plural_delete = asyncio.run(search("delete creatives 123 and 456"))
    plural_comments = asyncio.run(search("show comments on creatives"))
    mixed_results = asyncio.run(
        search("do not delete creative; update its name instead")
    )
    creative_history = asyncio.run(search("creative update history"))
    comment_replies = asyncio.run(search("continue comment replies with cursor"))
    embedded_replies = asyncio.run(search("get ad comments including replies"))

    assert feedback.splitlines()[1].startswith("- `get_ad_feedback_signals`")
    assert raw_comments.splitlines()[1].startswith("- `list_ad_comments`")
    assert page_reviews.splitlines()[1].startswith("- `list_page_recommendations`")
    assert actions.splitlines()[1].startswith("- `summarize_actions`")
    assert campaign_lookup.splitlines()[1].startswith("- `list_campaigns`")
    assert "client-side name lookup" in campaign_lookup.splitlines()[1]
    assert "find campaign by name" not in campaign_lookup.splitlines()[1]
    assert terse_campaigns.splitlines()[1].startswith("- `list_campaigns`")
    assert terse_actions.splitlines()[1].startswith("- `summarize_actions`")
    assert terse_pause.splitlines()[1].startswith("- `set_adset_status`")
    assert creative_read.splitlines()[1].startswith("- `get_creative`")
    assert all(result.splitlines()[1].startswith("- `get_creative`") for result in terse_creative_reads)
    assert all(
        not any(
            f"`{prefix}" in result
            for prefix in ("`create_", "`update_", "`delete_", "`set_", "`upload_")
        )
        for result in terse_creative_reads
    )
    assert all("`setup_ab_test`" not in result for result in terse_creative_reads)
    assert delete_creative.splitlines()[1].startswith("- `delete_creative`")
    assert update_creative.splitlines()[1].startswith("- `update_creative`")
    assert performance_report.splitlines()[1].startswith(
        "- `get_creative_performance_report`"
    )
    assert mixed_intent.splitlines()[1].startswith("- `update_creative`")
    assert creative_list.splitlines()[1].startswith("- `list_creatives`")
    assert creative_comments.splitlines()[1].startswith("- `list_ad_comments`")
    assert creative_image.splitlines()[1].startswith("- `get_ad_image`")
    assert not narrow_remove.splitlines()[1].startswith("- `delete_creative`")
    assert not detach_creative.splitlines()[1].startswith("- `delete_creative`")
    assert overflow_read.splitlines()[1].startswith("- `read_overflow_artifact`")
    assert "`delete_overflow_artifact`" not in overflow_read
    assert overflow_download.splitlines()[1].startswith("- `read_overflow_artifact`")
    assert overflow_negated_delete.splitlines()[1].startswith(
        "- `read_overflow_artifact`"
    )
    generic_reads = {
        "read audience 123 do not delete it": "get_audience",
        "get audience details by id": "get_audience",
        "read campaign 123 do not update it": "get_campaign",
        "get campaign details by id": "get_campaign",
        "show adset 123 do not pause it": "get_adset",
        "get ad set details by id": "get_adset",
        "read ad 123 do not delete it": "get_ad",
        "get ad details by id": "get_ad",
        "ad 123 details, do not change it": "get_ad",
    }
    for query, expected_tool in generic_reads.items():
        result = asyncio.run(search(query))
        assert result.splitlines()[1].startswith(f"- `{expected_tool}`")
        assert "`delete_" not in result
        assert "`update_" not in result
        assert "`set_" not in result
    assert plural_delete.splitlines()[1].startswith("- `delete_creative`")
    assert plural_comments.splitlines()[1].startswith("- `list_ad_comments`")
    assert mixed_results.splitlines()[1].startswith("- `update_creative`")
    assert "`delete_creative`" not in mixed_results
    assert creative_history.splitlines()[1].startswith("- `list_change_history`")
    assert comment_replies.splitlines()[1].startswith("- `list_comment_replies`")
    assert embedded_replies.splitlines()[1].startswith("- `list_ad_comments`")
    generic_mixed = {
        "do not delete campaign 123, update its name": (
            "update_campaign",
            "delete_campaign",
        ),
        "do not delete campaign update its name": (
            "update_campaign",
            "delete_campaign",
        ),
        "do not delete audience 123, update its name": (
            "update_custom_audience",
            "delete_audience",
        ),
        "do not delete audience update its name": (
            "update_custom_audience",
            "delete_audience",
        ),
    }
    for query, (expected_tool, forbidden_tool) in generic_mixed.items():
        result = asyncio.run(search(query))
        assert result.splitlines()[1].startswith(f"- `{expected_tool}`")
        assert f"`{forbidden_tool}`" not in result
    assert asyncio.run(
        search("do not pause adset resume it")
    ).splitlines()[1].startswith("- `set_adset_status`")
    assert asyncio.run(
        search("do not pause ad resume it")
    ).splitlines()[1].startswith("- `set_ad_status`")
    campaign_performance = asyncio.run(search("get campaign performance report"))
    assert campaign_performance.splitlines()[1].startswith("- `get_entity_insights`")
    for query in ("list ad accounts", "get ad accounts", "ad accounts"):
        accounts_result = asyncio.run(search(query))
        assert accounts_result.splitlines()[1].startswith("- `list_ad_accounts`")
    status_routes = {
        "update ad status": "set_ad_status",
        "change ad status": "set_ad_status",
        "set ad status": "set_ad_status",
        "update adset status": "set_adset_status",
    }
    for query, expected_tool in status_routes.items():
        status_result = asyncio.run(search(query))
        assert status_result.splitlines()[1].startswith(f"- `{expected_tool}`")
    for query in (
        "creative insights",
        "get creative insights",
        "get full creative performance for ad 123",
    ):
        insights_result = asyncio.run(search(query))
        assert insights_result.splitlines()[1].startswith(
            "- `get_creative_performance_report`"
        )
    fatigue_result = asyncio.run(search("get full creative fatigue report"))
    assert fatigue_result.splitlines()[1].startswith(
        "- `get_creative_fatigue_report`"
    )
    entity_mutations = {
        "pause ad 456 in campaign 123": "set_ad_status",
        "pause ad 456 in ad set 123": "set_ad_status",
        "do not pause campaign 123, pause ad 456": "set_ad_status",
        "update ad 456 status": "set_ad_status",
        "change ad 456 status": "set_ad_status",
        "set ad 456 status": "set_ad_status",
        "update adset 456 status": "set_adset_status",
        "pause campaign 123's ad 456": "set_ad_status",
        "resume ad set 123's ad 456": "set_ad_status",
        "increase campaign 123's ad set 456 budget": "update_adset_budget",
        "decrease campaign 123's adset budget": "update_adset_budget",
        "create campaign 123's ad set": "create_ad_set",
        "create campaign 123's ad": "create_ad",
        "change ad 456's status": "set_ad_status",
        "change ad ID 456 status": "set_ad_status",
        "update adset 456's status": "set_adset_status",
        "change ad #456 status": "set_ad_status",
        "update ad 456 delivery status": "set_ad_status",
        "change adset 456 current status": "set_adset_status",
        "change ad ID number 456 status": "set_ad_status",
        "change ad 456 to paused": "set_ad_status",
        "set ad 456 active": "set_ad_status",
        "pause ad 456 using campaign 123's ad set settings": "set_ad_status",
        "pause ad 456 according to campaign 123's ad set 789": "set_ad_status",
        "pause ad 456 under campaign 123's ad set rules": "set_ad_status",
        "pause ad 456 following campaign 123's ad set rules": "set_ad_status",
        "pause ad 456 pursuant to campaign 123's ad set rules": "set_ad_status",
        "switch campaign 456 to paused": "set_campaign_status",
        "switch ad 456 to active": "set_ad_status",
        "switch adset 456 to active": "set_adset_status",
        "flip ad 456 to paused": "set_ad_status",
        "switch campaign 456 off": "set_campaign_status",
        "stop campaign 456": "set_campaign_status",
        "start adset 456": "set_adset_status",
        "transition ad 456 to active": "set_ad_status",
        "move ad 456 to paused": "set_ad_status",
        "pause ad 456 governed by campaign 123's ad set rules": "set_ad_status",
        "pause ad 456 despite campaign 123's ad set rules": "set_ad_status",
        "halt campaign 456": "set_campaign_status",
        "launch adset 456": "set_adset_status",
        "take campaign 456 offline": "set_campaign_status",
        "shut down adset 456": "set_adset_status",
        "make campaign 456 inactive": "set_campaign_status",
        "pause ad 456 constrained by campaign 123's ad set rules": "set_ad_status",
        "pause ad 456 irrespective of campaign 123's ad set policy": "set_ad_status",
        "pause ad 456 while honoring campaign 123's ad set rules": "set_ad_status",
        "pause ad 456 as dictated by campaign 123's ad set policy": "set_ad_status",
        "pause ad 456 in light of campaign 123's ad set policy": "set_ad_status",
        "suspend campaign 456": "set_campaign_status",
        "begin adset 456": "set_adset_status",
        "end ad 456": "set_ad_status",
        "bring campaign 456 back online": "set_campaign_status",
        "put adset 456 offline": "set_adset_status",
        "reactivate adset 456": "set_adset_status",
        "unpause campaign 456": "set_campaign_status",
    }
    for query, expected_tool in entity_mutations.items():
        mutation_result = asyncio.run(search(query))
        assert mutation_result.splitlines()[1].startswith(f"- `{expected_tool}`")
    safe_negations = (
        "do not delete or update campaign 123; read it",
        "never delete nor update campaign 123; show details",
        "do not under any circumstances delete campaign 123; read it",
        "don’t delete campaign 123; show details",
        "neither delete nor update campaign 123; inspect it",
        "do not ever, under any circumstances, delete campaign 123; show it",
        "refrain from deleting and updating campaign 123; read it",
        "avoid deleting as well as updating campaign 123; inspect it",
        "shouldn't delete campaign 123; show details",
        "shouldn’t remove campaign 123; inspect it",
        "wouldn't delete campaign 123; inspect it",
        "cannot delete audience 123; inspect it",
        "can't delete campaign 123; show details",
        "refrain from deleting, renaming, or updating campaign 123; read it",
        "won't delete campaign 123; inspect it",
        "avoid deleting plus updating campaign 123; inspect it",
        "refrain from deleting along with updating campaign 123; read it",
        "skip deleting campaign 123 and show details",
        "refuse to delete campaign 123; inspect it",
        "decline to delete campaign 123; inspect it",
        "keep from deleting audience 123; inspect it",
        "rather than deleting campaign 123, inspect it",
        "avoid deleting together with updating campaign 123; inspect it",
        "refrain from deleting in addition to updating campaign 123; read it",
        "forgo deleting campaign 123; inspect it",
        "abstain from deleting audience 123; show details",
        "do anything but delete campaign 123; show it",
        "do everything except delete campaign 123; show details",
        "anything other than deleting audience 123; inspect it",
    )
    for query in safe_negations:
        safe_result = asyncio.run(search(query))
        expected_read = (
            "get_audience" if "audience" in query else "get_campaign"
        )
        assert safe_result.splitlines()[1].startswith(f"- `{expected_read}`")
        assert "`delete_campaign`" not in safe_result
        assert "`delete_audience`" not in safe_result
        assert "`update_campaign`" not in safe_result
        assert "`update_custom_audience`" not in safe_result
    refrain_result = asyncio.run(
        search("refrain from deleting creative 123; fetch it")
    )
    assert refrain_result.splitlines()[1].startswith("- `get_creative`")
    assert "`delete_creative`" not in refrain_result
    unicode_negation = asyncio.run(
        search("shouldn’t remove creative 123; inspect it")
    )
    assert unicode_negation.splitlines()[1].startswith("- `get_creative`")
    assert "`delete_creative`" not in unicode_negation
    unicode_will_not = asyncio.run(
        search("won’t remove creative 123; show it")
    )
    assert unicode_will_not.splitlines()[1].startswith("- `get_creative`")
    assert "`delete_creative`" not in unicode_will_not
    prohibit_delete = asyncio.run(
        search("prohibit deleting creative 123; show it")
    )
    assert prohibit_delete.splitlines()[1].startswith("- `get_creative`")
    assert "`delete_creative`" not in prohibit_delete
    explicit_delete = asyncio.run(
        search("show campaign 123 that is not active and delete it")
    )
    assert explicit_delete.splitlines()[1].startswith("- `delete_campaign`")
    comma_delete = asyncio.run(
        search("show campaign 123 that is not active, delete it")
    )
    assert comma_delete.splitlines()[1].startswith("- `delete_campaign`")
    hierarchy_reads = {
        "list ads in campaign 123": "list_ads",
        "list ads in ad set 123": "list_ads",
        "list ads in account": "list_ads",
        "get creative for ad 123": "get_ad",
        "get ad 123 creative details": "get_ad",
        "list ads with creative details": "list_ads",
        "show ads and their creatives": "list_ads",
        "show campaign 123's ads": "list_ads",
        "fetch ad set 123's ads": "list_ads",
        "show account act_123's ads": "list_ads",
        "inspect the creative attached to ad 123": "get_ad",
        "show creative used by ad 123": "get_ad",
        "retrieve the creative linked to ad 123": "get_ad",
        "show creative belonging to ad 123": "get_ad",
        "fetch creative referenced by ad 123": "get_ad",
        "inspect creative connected to ad 123": "get_ad",
        "inspect creative served by ad 123": "get_ad",
        "show creative behind ad 123": "get_ad",
        "read creative through ad 123": "get_ad",
        "inspect creative delivered by ad 123": "get_ad",
        "show creative powering ad 123": "get_ad",
        "inspect creative tied to ad 123": "get_ad",
        "creative driving ad 123": "get_ad",
        "creative embedded in ad 123": "get_ad",
        "creative selected by ad 123": "get_ad",
        "creative paired with ad 123": "get_ad",
        "creative that ad 123 uses": "get_ad",
        "creative ad 123 references": "get_ad",
        "get creative for advertisement 123": "get_ad",
        "inspect creative attached to advertisement 123": "get_ad",
        "show creative used by advertisement 123": "get_ad",
        "fetch creative referenced by advertisement 123": "get_ad",
        "get creative for advert 123": "get_ad",
        "show advertisement 123 creative": "get_ad",
        "read advertisement 123 creative": "get_ad",
        "fetch advert 123 creative": "get_ad",
        "show advertisements with creatives": "list_ads",
        "list advertisements and creatives": "list_ads",
        "creative used by ad 123": "get_ad",
        "creative attached to ad 123": "get_ad",
        "creative for ad 123": "get_ad",
        "ad 123 creative": "get_ad",
        "show creative on ad 123": "get_ad",
        "show the ad's attached creative": "get_ad",
        "show ad 123's attached creative": "get_ad",
    }
    for query, expected_tool in hierarchy_reads.items():
        hierarchy_result = asyncio.run(search(query))
        assert hierarchy_result.splitlines()[1].startswith(f"- `{expected_tool}`")
    direct_ad_creative_reads = {
        "show ad creative 123": "get_creative",
        "get ad creative by id 123": "get_creative",
        "fetch ad creative details 123": "get_creative",
        "inspect ad creative id 123": "get_creative",
        "list ad creatives": "list_creatives",
        "show advertisement creative 123": "get_creative",
        "get advertisement creative id 123": "get_creative",
        "read advert creative 123": "get_creative",
        "fetch advertisement creative details 123": "get_creative",
        "inspect advert creative id 123": "get_creative",
        "get the advertisement creative by id 123": "get_creative",
        "list advertisement creatives": "list_creatives",
    }
    for query, expected_tool in direct_ad_creative_reads.items():
        direct_result = asyncio.run(search(query))
        assert direct_result.splitlines()[1].startswith(f"- `{expected_tool}`")
    parent_comment_queries = (
        "next page of ad comments with replies",
        "continue ad comments and include nested replies",
        "next page of post comments with replies",
        "continue comments and include nested replies",
        "more comments including replies",
        "fetch next batch of Facebook post comments including replies",
        "continue page-post comments with replies",
        "page forward through post comments and nested replies",
        "show Facebook Page comments and replies",
        "list Page comments with replies",
        "get Page post comments and replies",
        "fetch page comments including replies",
        "Facebook page comments with nested replies",
        "show page comments and their replies",
        "next page of Page comments",
        "page through Page comments",
        "second page of Page comments",
        "page 2 of Page comments",
        "not replies, next page of Page comments",
        "next page of Page comments without replies",
        "more replies for comments",
        "continue nested replies under comments",
        "show replies to comments on Page",
        "show replies to comments on post 123",
        "show replies to comments on ad 123",
    )
    for query in parent_comment_queries:
        parent_comments_result = asyncio.run(search(query))
        assert parent_comments_result.splitlines()[1].startswith(
            "- `list_ad_comments`"
        )
    reply_page_queries = (
        "show replies beneath this comment",
        "fetch this comment's replies",
        "get this comment and its replies",
        "next page of nested replies",
        "next replies page",
        "page through replies",
    )
    for query in reply_page_queries:
        reply_page_result = asyncio.run(search(query))
        assert reply_page_result.splitlines()[1].startswith(
            "- `list_comment_replies`"
        )
    negated_specialized_reads = {
        "full spec for creative 123 without performance": "get_creative",
        "get full creative details, not performance": "get_creative",
        "do not analyze creative performance; show full spec": "get_creative",
        "campaign performance not needed, get details": "get_campaign",
        "do not show campaign history; get current details": "get_campaign",
        "do not list ad comments, get ad details": "get_ad",
        "creative performance isn't needed; show full spec": "get_creative",
        "creative performance is unnecessary; get metadata": "get_creative",
        "skip creative performance and show fields": "get_creative",
        "no creative performance report, just metadata": "get_creative",
        "campaign insights are not wanted; show details": "get_campaign",
        "history is unnecessary; get campaign configuration": "get_campaign",
        "skip comments and inspect ad": "get_ad",
        "creative performance is irrelevant; show metadata": "get_creative",
        "creative performance is unwanted; show metadata": "get_creative",
        "omit creative performance and show metadata": "get_creative",
        "exclude performance reporting; show creative metadata": "get_creative",
        "rather than performance, show creative metadata": "get_creative",
        "omit comments and retrieve ad metadata": "get_ad",
        "creative performance is immaterial; show fields": "get_creative",
        "bypass creative performance and fetch metadata": "get_creative",
        "ignore creative performance and show metadata": "get_creative",
        "disregard performance and inspect creative fields": "get_creative",
        "performance does not matter; get creative details": "get_creative",
        "ignore campaign history and show configuration": "get_campaign",
        "ignore comments and inspect ad metadata": "get_ad",
        "leave out performance and show creative details": "get_creative",
        "leave out comments and inspect ad metadata": "get_ad",
    }
    for query, expected_tool in negated_specialized_reads.items():
        result = asyncio.run(search(query))
        assert result.splitlines()[1].startswith(f"- `{expected_tool}`")
    creative_mutation_phrases = {
        "remove ad creative 123": "delete_creative",
        "remove the ad creative 123": "delete_creative",
        "do not get ad creative 123, delete it": "delete_creative",
        "do not show ad creative 123; remove it": "delete_creative",
        "do not get ad creative 123; remove it": "delete_creative",
    }
    for query, expected_tool in creative_mutation_phrases.items():
        result = asyncio.run(search(query))
        assert result.splitlines()[1].startswith(f"- `{expected_tool}`")


def test_compare_performance_responds_through_tool_layer(monkeypatch) -> None:
    class FakeInsightsClient:
        async def get_insights(self, object_id: str, *, fields, params):
            assert object_id == "act_123"
            assert params["level"] == "campaign"
            return {
                "data": [
                    {
                        "campaign_id": "cmp_1",
                        "campaign_name": "Campaign One",
                        "spend": "100",
                        "impressions": "1000",
                        "clicks": "50",
                    }
                ]
            }

    monkeypatch.setattr(insights, "get_graph_api_client", lambda: FakeInsightsClient())

    result = asyncio.run(
        mcp_server.call_tool(
            "compare_performance",
            {
                "level": "campaign",
                "object_ids": ["act_123"],
                "date_preset": "last_30d",
                "metrics": ["spend", "clicks"],
            },
        )
    )

    summary = result.structured_content["summary"]
    assert summary["successful"] == 1
    assert summary["failed"] == 0


def test_compact_search_serializer_returns_minimal_markdown() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    tools = [
        components["tool:get_entity_insights@"],
        components["tool:compare_performance@"],
    ]
    result = serialize_search_results_compact(tools)
    assert "Matches:" in result
    assert "`get_entity_insights` | req: level, object_id" in result
    assert "`compare_performance` | req: level, object_ids" in result
    assert "properties" not in result
    assert "additionalProperties" not in result
    assert "Next: use `call_tool`" in result


def test_compact_search_serializer_surfaces_required_archive_params() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    result = serialize_search_results_compact([components["tool:search_ads_archive@"]])
    assert "`search_ads_archive` | req: search_terms, ad_reached_countries" in result
    assert "opt: ad_type, limit, fields" in result


def test_compact_search_serializer_surfaces_required_targeting_category_params() -> None:
    components = mcp_server.local_provider.__dict__["_components"]
    result = serialize_search_results_compact([components["tool:get_targeting_categories@"]])
    assert "`get_targeting_categories` | req: category_class" in result
    assert "opt: query, account_id, limit" in result
