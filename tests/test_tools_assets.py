"""Asset and lookup read tool tests (Stream A)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from meta_ads_mcp.errors import MetaApiError, ValidationError
from meta_ads_mcp.tools import assets


class FakeAssetsClient:
    """Fake client that records calls and serves queued edge pages."""

    def __init__(
        self,
        pages: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        objects: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.pages = {key: list(value) for key, value in (pages or {}).items()}
        self.objects = objects or {}
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.list_calls.append(
            {"parent_id": parent_id, "edge": edge, "fields": fields, "params": dict(params or {})}
        )
        queue = self.pages.get((parent_id, edge))
        if not queue:
            return {"data": []}
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.get_calls.append({"object_id": object_id, "fields": fields, "params": params})
        return dict(self.objects.get(object_id, {"id": object_id}))


def _patch(monkeypatch, client) -> None:
    monkeypatch.setattr(assets, "get_graph_api_client", lambda: client)


def _no_client(monkeypatch) -> None:
    monkeypatch.setattr(
        assets, "get_graph_api_client", lambda: pytest.fail("No client should be created")
    )


def _paged(data: list[dict[str, Any]], after: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"data": data}
    if after:
        payload["paging"] = {
            "cursors": {"after": after},
            "next": f"https://graph.facebook.com/next?after={after}",
        }
    return payload


# list_pages


@pytest.mark.parametrize(
    ("arguments", "parent", "edge", "extra_params"),
    [
        ({"source": "user"}, "me", "accounts", {"is_promotable": True}),
        ({"source": "ad_account", "account_id": "123"}, "act_123", "promote_pages", {}),
        ({"source": "business", "business_id": "biz_1"}, "biz_1", "owned_pages", {}),
    ],
)
def test_list_pages_routes_each_source_to_its_documented_edge(
    monkeypatch, arguments, parent, edge, extra_params
) -> None:
    client = FakeAssetsClient(pages={(parent, edge): [_paged([{"id": "page_1", "name": "P"}])]})
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_pages(**arguments, limit=10, after="cur"))

    call = client.list_calls[0]
    assert (call["parent_id"], call["edge"]) == (parent, edge)
    assert call["params"] == {"limit": 10, "after": "cur", **extra_params}
    assert result["items"] == [{"id": "page_1", "name": "P"}]
    assert result["summary"]["source"] == arguments["source"]
    assert result["summary"]["edge"] == f"{parent}/{edge}"
    assert result["summary"]["complete"] is True
    assert result["summary"]["next_after"] is None


def test_list_pages_reports_next_cursor(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("me", "accounts"): [_paged([{"id": "p"}], after="nxt")]})
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_pages(source="user"))
    assert result["summary"]["complete"] is False
    assert result["summary"]["truncated"] is True
    assert result["summary"]["next_after"] == "nxt"


def test_list_pages_uses_requested_fields(monkeypatch) -> None:
    client = FakeAssetsClient()
    _patch(monkeypatch, client)
    asyncio.run(assets.list_pages(source="business", business_id="b1", fields="id,name"))
    assert client.list_calls[0]["fields"] == ["id", "name"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"source": "everything"}, "source must be one of"),
        ({"source": "ad_account"}, "account_id is required"),
        ({"source": "business"}, "business_id is required"),
        ({"source": "user", "account_id": "1"}, "source=user"),
        ({"source": "ad_account", "account_id": "1", "business_id": "2"}, "business_id"),
        ({"source": "business", "business_id": "2", "account_id": "1"}, "account_id"),
        ({"source": "user", "limit": 0}, "limit"),
    ],
)
def test_list_pages_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(assets.list_pages(**arguments))


# list_ad_images / list_ad_videos


def test_list_ad_images_sends_hashes_and_default_fields(monkeypatch) -> None:
    client = FakeAssetsClient(
        pages={("act_123", "adimages"): [_paged([{"hash": "h1", "url": "https://x/1.png"}])]}
    )
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_ad_images(account_id="123", hashes="h1, h2", limit=5))

    call = client.list_calls[0]
    assert (call["parent_id"], call["edge"]) == ("act_123", "adimages")
    assert call["params"] == {"limit": 5, "hashes": ["h1", "h2"]}
    assert call["fields"] == ["id", "hash", "name", "url", "permalink_url", "width", "height", "original_width", "original_height", "status", "created_time", "updated_time"]
    assert result["items"][0]["hash"] == "h1"
    assert result["summary"]["complete"] is True


def test_list_ad_images_rejects_blank_hashes_without_call(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="hashes"):
        asyncio.run(assets.list_ad_images(account_id="123", hashes=["h1", " "]))
    with pytest.raises(ValidationError, match="account_id"):
        asyncio.run(assets.list_ad_images(account_id=" "))


def test_list_ad_videos_reads_advideos_edge(monkeypatch) -> None:
    client = FakeAssetsClient(
        pages={("act_123", "advideos"): [_paged([{"id": "v1", "title": "Clip"}], after="n2")]}
    )
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_ad_videos(account_id="act_123", after="n1"))

    call = client.list_calls[0]
    assert (call["parent_id"], call["edge"]) == ("act_123", "advideos")
    assert call["params"] == {"limit": 50, "after": "n1"}
    assert call["fields"] == ["id", "title", "length", "picture", "permalink_url", "status", "created_time", "updated_time"]
    assert result["summary"] == {"count": 1, "complete": False, "truncated": True, "next_after": "n2"}


def test_list_ad_videos_validates_limit_without_call(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="limit"):
        asyncio.run(assets.list_ad_videos(account_id="123", limit=0))


# list_creative_ads


def _ad(ad_id: str, creative_id: str) -> dict[str, Any]:
    return {"id": ad_id, "name": f"Ad {ad_id}", "creative": {"id": creative_id}}


def test_list_creative_ads_scans_account_ads_and_filters_by_creative(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("act_123", "ads"): [
        _paged([_ad("a1", "crt_1"), _ad("a2", "crt_2")], after="c1"),
        _paged([_ad("a3", "crt_1")]),
    ]})
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_creative_ads(
        creative_id="crt_1", account_id="123", effective_status=["ACTIVE", "PAUSED"], limit=2,
    ))

    assert [item["id"] for item in result["items"]] == ["a1", "a3"]
    assert all(item["creative_id"] == "crt_1" for item in result["items"])
    assert len(client.list_calls) == 2
    assert client.list_calls[0]["params"] == {"limit": 2, "effective_status": ["ACTIVE", "PAUSED"]}
    assert client.list_calls[1]["params"]["after"] == "c1"
    summary = result["summary"]
    assert summary["count"] == 2
    assert summary["scanned_count"] == 3
    assert summary["complete"] is True
    assert summary["scan_capped"] is False
    assert summary["next_after"] is None
    assert summary["matching"] == "local"


def test_list_creative_ads_resolves_account_from_creative(monkeypatch) -> None:
    client = FakeAssetsClient(
        objects={"crt_1": {"id": "crt_1", "account_id": "999"}},
        pages={("act_999", "ads"): [_paged([_ad("a1", "crt_1")])]},
    )
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_creative_ads(creative_id="crt_1"))
    assert client.get_calls[0] == {"object_id": "crt_1", "fields": ["id", "account_id"], "params": None}
    assert client.list_calls[0]["parent_id"] == "act_999"
    assert result["summary"]["account_id"] == "act_999"


def test_list_creative_ads_reports_incomplete_scan(monkeypatch) -> None:
    class EndlessClient(FakeAssetsClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.list_calls.append({"parent_id": parent_id, "edge": edge, "params": dict(params or {})})
            page = len(self.list_calls)
            return _paged([_ad(f"a{page}", "crt_other")], after=f"cursor{page}")

    client = EndlessClient()
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_creative_ads(creative_id="crt_1", account_id="123", limit=1))

    summary = result["summary"]
    assert len(client.list_calls) == assets.MAX_SCAN_PAGES
    assert summary["complete"] is False
    assert summary["scan_capped"] is True
    assert summary["scanned_count"] == assets.MAX_SCAN_PAGES
    assert summary["pages_read"] == assets.MAX_SCAN_PAGES
    assert summary["next_after"] == f"cursor{assets.MAX_SCAN_PAGES}"
    assert "incomplete" in summary["scan_note"]
    assert str(assets.MAX_SCAN_PAGES) in summary["scan_note"]


def test_list_creative_ads_repeated_cursor_stops_as_incomplete(monkeypatch) -> None:
    class LoopingClient(FakeAssetsClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.list_calls.append({"params": dict(params or {})})
            return _paged([_ad("a1", "crt_1")], after="same")

    client = LoopingClient()
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_creative_ads(creative_id="crt_1", account_id="123"))
    assert len(client.list_calls) == 2
    assert result["summary"]["complete"] is False
    assert result["summary"]["next_after"] is None


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"creative_id": " "}, "creative_id is required"),
        ({"creative_id": "c", "account_id": "1", "limit": 0}, "limit"),
        ({"creative_id": "c", "account_id": "1", "limit": 5000}, "limit"),
    ],
)
def test_list_creative_ads_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(assets.list_creative_ads(**arguments))


def test_list_creative_ads_without_resolvable_account_raises(monkeypatch) -> None:
    client = FakeAssetsClient(objects={"crt_1": {"id": "crt_1"}})
    _patch(monkeypatch, client)
    with pytest.raises(ValidationError, match="account_id"):
        asyncio.run(assets.list_creative_ads(creative_id="crt_1"))
    assert client.list_calls == []


# list_instagram_media


def _media(media_id: str, eligible: bool | None, reason: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"id": media_id, "media_type": "IMAGE", "timestamp": "2026-11-01T00:00:00+0000"}
    if eligible is not None:
        info: dict[str, Any] = {"eligible_to_boost": eligible}
        if reason:
            info["boost_ineligible_reason"] = reason
        item["boost_eligibility_info"] = info
    return item


def test_list_instagram_media_filters_to_boost_eligible_by_default(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("ig_1", "media"): [_paged([
        _media("m1", True), _media("m2", False, "COPYRIGHTED_MUSIC"), _media("m3", None),
    ], after="n1")]})
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_instagram_media(instagram_account_id="ig_1", limit=3))

    call = client.list_calls[0]
    assert (call["parent_id"], call["edge"]) == ("ig_1", "media")
    assert call["params"] == {"limit": 3}
    assert "boost_eligibility_info{eligible_to_boost,boost_ineligible_reason}" in call["fields"]
    assert [item["id"] for item in result["items"]] == ["m1"]
    assert result["items"][0]["boost_eligible"] is True
    summary = result["summary"]
    assert summary["scanned_count"] == 3
    assert summary["excluded_ineligible"] == 1
    assert summary["eligibility_unknown"] == 1
    assert summary["boost_eligible_only"] is True
    assert summary["next_after"] == "n1"
    assert summary["complete"] is False


def test_list_instagram_media_all_items_explain_ineligibility(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("ig_1", "media"): [_paged([
        _media("m1", True), _media("m2", False, "COPYRIGHTED_MUSIC"), _media("m3", None),
    ])]})
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_instagram_media(
        instagram_account_id="ig_1", boost_eligible_only=False, fields="id,caption",
    ))

    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["m2"]["boost_eligible"] is False
    assert by_id["m2"]["boost_ineligible_reason"] == "COPYRIGHTED_MUSIC"
    assert by_id["m3"]["boost_eligible"] is None
    assert by_id["m3"]["boost_ineligible_reason"] is None
    assert client.list_calls[0]["fields"] == ["id", "caption", "boost_eligibility_info{eligible_to_boost,boost_ineligible_reason}"]


def test_list_instagram_media_reads_stories_edge(monkeypatch) -> None:
    client = FakeAssetsClient()
    _patch(monkeypatch, client)
    asyncio.run(assets.list_instagram_media(instagram_account_id="ig_1", source="stories"))
    assert client.list_calls[0]["edge"] == "stories"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"instagram_account_id": ""}, "instagram_account_id is required"),
        ({"instagram_account_id": "ig", "source": "reels"}, "source must be"),
        ({"instagram_account_id": "ig", "limit": 0}, "limit"),
    ],
)
def test_list_instagram_media_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(assets.list_instagram_media(**arguments))


# list_audience_adsets


def test_list_audience_adsets_groups_ads_from_the_audience_ads_edge(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("aud_1", "ads"): [
        _paged([
            {"id": "a1", "adset_id": "s1", "campaign_id": "c1", "adset": {"id": "s1", "name": "Set 1", "effective_status": "ACTIVE"}},
            {"id": "a2", "adset_id": "s2", "campaign_id": "c1", "adset": {"id": "s2", "name": "Set 2"}},
        ], after="p2"),
        _paged([{"id": "a3", "adset_id": "s1", "campaign_id": "c1"}]),
    ]})
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_audience_adsets(audience_id="aud_1", limit=2, source="ads"))

    assert [call["edge"] for call in client.list_calls] == ["ads", "ads"]
    assert client.list_calls[0]["parent_id"] == "aud_1"
    assert client.list_calls[0]["params"] == {"limit": 2}
    assert client.list_calls[1]["params"] == {"limit": 2, "after": "p2"}
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["s1"]["ad_ids"] == ["a1", "a3"]
    assert by_id["s1"]["ad_count"] == 2
    assert by_id["s1"]["name"] == "Set 1"
    assert by_id["s2"]["campaign_id"] == "c1"
    summary = result["summary"]
    assert summary["count"] == 2
    assert summary["scanned_ads"] == 3
    assert summary["complete"] is True
    assert summary["source_edge"] == "aud_1/ads"


def test_list_audience_adsets_reports_incomplete_scan(monkeypatch) -> None:
    class EndlessClient(FakeAssetsClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.list_calls.append({"params": dict(params or {})})
            page = len(self.list_calls)
            return _paged([{"id": f"a{page}", "adset_id": "s1"}], after=f"cur{page}")

    client = EndlessClient()
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_audience_adsets(audience_id="aud_1", limit=1, source="ads"))
    assert result["summary"]["complete"] is False
    assert result["summary"]["scanned_ads"] == assets.MAX_SCAN_PAGES
    assert result["summary"]["next_after"] == f"cur{assets.MAX_SCAN_PAGES}"
    assert "incomplete" in result["summary"]["scan_note"]


def test_list_audience_adsets_validation_errors_make_no_call(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="audience_id is required"):
        asyncio.run(assets.list_audience_adsets(audience_id=" "))
    with pytest.raises(ValidationError, match="limit"):
        asyncio.run(assets.list_audience_adsets(audience_id="aud_1", limit=0))


def test_permission_error_is_not_swallowed(monkeypatch) -> None:
    class DeniedClient(FakeAssetsClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            raise MetaApiError(message="(#200) Permissions error", status_code=403, code=200)

    _patch(monkeypatch, DeniedClient())
    with pytest.raises(MetaApiError, match="Permissions error"):
        asyncio.run(assets.list_audience_adsets(audience_id="aud_1"))
    with pytest.raises(MetaApiError, match="Permissions error"):
        asyncio.run(assets.list_pages(source="business", business_id="b1"))


# get_field_reference


def test_get_field_reference_returns_types_updatability_and_allowed_values(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(assets.get_field_reference(
        level="adset", field_names="optimization_goal, name, not_a_field",
    ))
    by_name = {item["name"]: item for item in result["items"]}
    assert by_name["optimization_goal"]["updatable"] is True
    assert "LINK_CLICKS" in by_name["optimization_goal"]["allowed_values"]
    assert by_name["name"]["type"] == "string"
    assert by_name["name"]["filter_support"]
    assert by_name["name"]["description"] is None
    assert result["summary"]["unknown_fields"] == ["not_a_field"]
    assert result["summary"]["source"].startswith("facebook-python-business-sdk 26.0.2")
    assert "metadata=1" in result["summary"]["source_note"]
    assert result["summary"]["reference_url"].endswith("/ad-campaign")


def test_get_field_reference_lists_every_field_for_a_level(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(assets.get_field_reference(level="campaign"))
    names = [item["name"] for item in result["items"]]
    assert {"objective", "status", "daily_budget", "special_ad_categories"} <= set(names)
    assert result["summary"]["count"] == len(names)


def test_get_field_reference_reads_current_values_for_known_fields(monkeypatch) -> None:
    client = FakeAssetsClient(objects={"ad_1": {"id": "ad_1", "status": "PAUSED", "name": "Ad"}})
    _patch(monkeypatch, client)
    result = asyncio.run(assets.get_field_reference(
        level="ad", field_names=["status", "name", "bogus"], object_id="ad_1",
    ))
    assert client.get_calls == [{"object_id": "ad_1", "fields": ["status", "name"], "params": None}]
    by_name = {item["name"]: item for item in result["items"]}
    assert by_name["status"]["current_value"] == "PAUSED"
    assert by_name["name"]["current_value"] == "Ad"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"level": "account"}, "level must be one of"),
        ({"level": "ad", "object_id": "ad_1"}, "field_names is required"),
        ({"level": "ad", "field_names": ["bogus"], "object_id": "ad_1"}, "No known fields"),
    ],
)
def test_get_field_reference_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(assets.get_field_reference(**arguments))


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("boostable instagram posts", "find_pages_media_and_asset_usage"),
        ("which ad sets use this custom audience", "find_pages_media_and_asset_usage"),
        ("which ads use this creative", "find_pages_media_and_asset_usage"),
        ("field types allowed values", "find_pages_media_and_asset_usage"),
        ("boost instagram post", "edit_ad_sets_ads_and_customer_lists"),
    ],
)
def test_stream_a_intents_route_plain_requests(query, intent) -> None:
    from meta_ads_mcp.tools import utility

    result = asyncio.run(utility.get_capabilities(intent=query))
    assert result["closest_intents"][0]["intent"] == intent


def test_stream_a_writes_appear_in_mutation_common_paths(monkeypatch) -> None:
    from meta_ads_mcp.config import reload_settings
    from meta_ads_mcp.tools import utility

    monkeypatch.delenv("META_READ_ONLY", raising=False)
    reload_settings()
    result = asyncio.run(utility.list_mutation_tools())
    assert result["common_paths"]["general_edits"] == ["update_adset", "update_ad"]
    assert result["common_paths"]["customer_list_users"] == ["add_audience_users", "remove_audience_users"]
    assert result["common_paths"]["instagram_boost"] == ["boost_instagram_post"]
    exact = asyncio.run(utility.get_capabilities(intent="find_pages_media_and_asset_usage"))
    assert exact["selected_intent"]["recommended_order"][0] == "list_pages"


def test_get_field_reference_accepts_ad_set_alias(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(assets.get_field_reference(level="ad_set", field_names=["billing_event"]))
    assert result["summary"]["level"] == "adset"


# Phase two: list_ad_videos lookups and processing status


def test_list_ad_videos_adds_processing_status(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("act_123", "advideos"): [_paged([
        {"id": "v1", "status": {"video_status": "ready"}}, {"id": "v2"},
    ])]})
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_ad_videos(account_id="123"))
    assert [item["processing_status"] for item in result["items"]] == ["ready", None]
    assert result["summary"] == {"count": 2, "complete": True, "truncated": False, "next_after": None}


def test_list_ad_videos_by_id_names_missing_videos(monkeypatch) -> None:
    class LookupClient(FakeAssetsClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            self.get_calls.append({"object_id": object_id, "fields": fields})
            if object_id == "2":
                raise MetaApiError(message="Unsupported get request", status_code=400, code=100)
            return {"id": object_id, "title": "Clip", "status": {"video_status": "processing"}}

    client = LookupClient()
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_ad_videos(account_id="123", video_ids="1, 2, 1"))
    assert [call["object_id"] for call in client.get_calls] == ["1", "2"]
    assert client.get_calls[0]["fields"] == ["id", "title", "length", "picture", "permalink_url", "status", "created_time", "updated_time"]
    assert result["items"] == [{"id": "1", "title": "Clip", "status": {"video_status": "processing"}, "processing_status": "processing"}]
    assert result["missing"] == [{"id": "2", "reason": "Unsupported get request", "code": 100}]
    assert result["summary"]["complete"] is False
    assert client.list_calls == []


def test_list_ad_videos_title_contains_scans_and_matches_locally(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("act_123", "advideos"): [
        _paged([{"id": "v1", "title": "Spring Sale"}, {"id": "v2", "title": "Winter"}], after="c1"),
        _paged([{"id": "v3", "title": "spring teaser"}]),
    ]})
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_ad_videos(account_id="123", title_contains="SPRING", limit=2))
    assert [item["id"] for item in result["items"]] == ["v1", "v3"]
    assert len(client.list_calls) == 2
    assert result["summary"]["scanned_count"] == 3
    assert result["summary"]["complete"] is True


class ProjectingVideoClient(FakeAssetsClient):
    """Returns only the fields asked for, as Meta does."""

    def __init__(self, videos) -> None:
        super().__init__()
        self.videos = videos

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        self.list_calls.append({"parent_id": parent_id, "edge": edge, "fields": fields, "params": dict(params or {})})
        return {"data": [{key: value for key, value in video.items() if key in (fields or [])} for video in self.videos]}


def test_g11_video_title_filter_reads_title_even_with_custom_fields(monkeypatch) -> None:
    client = ProjectingVideoClient([{"id": "20", "title": "Spring launch", "length": 12}])
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_ad_videos("10", title_contains="Spring", fields=["id"]))
    assert [row["id"] for row in result["items"]] == ["20"], result
    assert client.list_calls[0]["fields"] == ["id", "title"]
    # The title was read only to match on; the caller asked for ids.
    assert result["items"] == [{"id": "20", "processing_status": None}]


@pytest.mark.parametrize("fields", [["id", "title"], ["title", "id"]])
def test_g11_title_field_given_by_the_caller_is_not_added_twice(monkeypatch, fields) -> None:
    client = ProjectingVideoClient([{"id": "20", "title": "Spring launch"}])
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_ad_videos("10", title_contains="Spring", fields=fields))
    assert client.list_calls[0]["fields"] == fields
    assert result["items"][0]["title"] == "Spring launch"


def test_g11_title_filter_with_default_fields_is_unchanged(monkeypatch) -> None:
    client = ProjectingVideoClient([{"id": "20", "title": "Spring launch", "length": 12}])
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_ad_videos("10", title_contains="spring"))
    assert client.list_calls[0]["fields"] == ["id", "title", "length", "picture", "permalink_url", "status", "created_time", "updated_time"]
    assert result["items"] == [{"id": "20", "title": "Spring launch", "length": 12, "processing_status": None}]


def test_g13_image_projection_contract_test_detects_invalid_field(monkeypatch) -> None:
    """The request-contract test must not share its expectation with the production constant."""
    monkeypatch.setattr(assets, "AD_IMAGE_LIST_FIELDS", ["this_field_does_not_exist"])
    with pytest.raises(AssertionError):
        test_list_ad_images_sends_hashes_and_default_fields(monkeypatch)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"video_ids": ["abc"]}, "numeric"),
        ({"video_ids": [str(index) for index in range(26)]}, "at most 25"),
        ({"video_ids": ["1"], "title_contains": "x"}, "do not combine"),
        ({"video_ids": ["1"], "after": "c"}, "do not combine"),
        ({"title_contains": "x", "limit": 1000}, "limit"),
    ],
)
def test_list_ad_videos_lookup_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(assets.list_ad_videos(account_id="123", **arguments))


# Phase two: list_instagram_media filters


def test_list_instagram_media_filters_by_type_product_and_dates(monkeypatch) -> None:
    def media(media_id, media_type, product, timestamp):
        return {
            "id": media_id, "media_type": media_type, "media_product_type": product, "timestamp": timestamp,
            "boost_eligibility_info": {"eligible_to_boost": True},
        }

    client = FakeAssetsClient(pages={("ig_1", "media"): [_paged([
        media("m1", "VIDEO", "REELS", "2026-11-05T10:00:00+0000"),
        media("m2", "IMAGE", "FEED", "2026-11-05T10:00:00+0000"),
        media("m3", "VIDEO", "REELS", "2026-10-30T23:59:59+0000"),
        media("m4", "VIDEO", "REELS", "2026-11-10T00:00:00+0000"),
    ], after="n1")]})
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_instagram_media(
        instagram_account_id="ig_1", since="2026-11-01", until="2026-11-09",
        media_types="video", media_product_types=["reel"], fields="id,caption",
    ))

    call = client.list_calls[0]
    assert call["params"] == {"limit": 25, "since": 1793491200, "until": 1794268800}
    assert call["fields"] == ["id", "caption", "boost_eligibility_info{eligible_to_boost,boost_ineligible_reason}", "timestamp", "media_type", "media_product_type"]
    assert [item["id"] for item in result["items"]] == ["m1"]
    summary = result["summary"]
    assert summary["excluded_by_filters"] == 3
    assert summary["filters"]["media_product_types"] == ["REELS"]
    assert summary["filters"]["date_range_sent_to_meta"] is True
    assert summary["complete"] is False
    assert summary["next_after"] == "n1"


def test_list_instagram_media_stories_filter_dates_locally(monkeypatch) -> None:
    client = FakeAssetsClient()
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_instagram_media(instagram_account_id="ig_1", source="stories", since="2026-11-01"))
    assert "since" not in client.list_calls[0]["params"]
    assert result["summary"]["filters"]["date_range_sent_to_meta"] is False


def test_list_instagram_media_without_filters_is_unchanged(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("ig_1", "media"): [_paged([_media("m1", True)])]})
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_instagram_media(instagram_account_id="ig_1"))
    assert client.list_calls[0]["params"] == {"limit": 25}
    assert client.list_calls[0]["fields"] == ["id", "caption", "media_type", "media_product_type", "timestamp", "permalink", "media_url", "thumbnail_url", "boost_eligibility_info{eligible_to_boost,boost_ineligible_reason}"]
    assert "filters" not in result["summary"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"since": "11/01/2026"}, "YYYY-MM-DD"),
        ({"since": "2026-11-05", "until": "2026-11-01"}, "on or after"),
        ({"media_types": ["GIF"]}, "unknown values"),
        ({"media_product_types": ["story"]}, "source=stories"),
        ({"media_product_types": ["FEED"], "source": "stories"}, "stories only"),
    ],
)
def test_list_instagram_media_filter_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(assets.list_instagram_media(instagram_account_id="ig_1", **arguments))


# Phase two: list_audience_adsets targeting scan


def _adset(adset_id: str, included=(), excluded=()) -> dict[str, Any]:
    targeting: dict[str, Any] = {"geo_locations": {"countries": ["US"]}}
    if included:
        targeting["custom_audiences"] = [{"id": value, "name": "A"} for value in included]
    if excluded:
        targeting["excluded_custom_audiences"] = [{"id": value, "name": "A"} for value in excluded]
    return {"id": adset_id, "name": f"Set {adset_id}", "effective_status": "PAUSED", "campaign_id": "c9", "targeting": targeting}


def test_audience_adsets_targeting_scan_finds_ad_sets_without_ads(monkeypatch) -> None:
    client = FakeAssetsClient(
        objects={"aud_1": {"id": "aud_1", "account_id": "123"}},
        pages={
            ("aud_1", "ads"): [_paged([{"id": "a1", "adset_id": "s1", "campaign_id": "c1"}])],
            ("act_123", "adsets"): [_paged([
                _adset("s1", included=["aud_1"]), _adset("s2", excluded=["aud_1"]), _adset("s3", included=["other"]),
            ])],
        },
    )
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_audience_adsets(audience_id="aud_1"))

    assert client.get_calls == [{"object_id": "aud_1", "fields": ["id", "account_id"], "params": None}]
    adset_call = next(call for call in client.list_calls if call["edge"] == "adsets")
    assert adset_call["parent_id"] == "act_123"
    assert adset_call["fields"] == ["id", "name", "effective_status", "campaign_id", "targeting"]
    by_id = {item["id"]: item for item in result["items"]}
    assert set(by_id) == {"s1", "s2"}
    assert by_id["s1"]["found_by"] == ["ads", "targeting"]
    assert by_id["s1"]["targeting_use"] == ["included"]
    assert by_id["s2"]["found_by"] == ["targeting"]
    assert by_id["s2"]["targeting_use"] == ["excluded"]
    assert by_id["s2"]["ad_count"] == 0
    summary = result["summary"]
    assert summary["complete"] is True
    assert summary["targeting_scan"]["scanned_adsets"] == 3
    assert summary["targeting_scan"]["matched_adsets"] == 2


def test_audience_adsets_targeting_scan_reports_incomplete_scan(monkeypatch) -> None:
    class EndlessAdsets(FakeAssetsClient):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            self.list_calls.append({"parent_id": parent_id, "edge": edge, "params": dict(params or {})})
            page = len(self.list_calls)
            return _paged([_adset(f"s{page}", included=["aud_1"])], after=f"t{page}")

    client = EndlessAdsets()
    _patch(monkeypatch, client)

    result = asyncio.run(assets.list_audience_adsets(audience_id="aud_1", source="targeting", account_id="123", limit=1))

    summary = result["summary"]
    assert len(client.list_calls) == assets.MAX_SCAN_PAGES
    assert summary["complete"] is False
    assert summary["targeting_scan"]["complete"] is False
    assert summary["targeting_scan"]["scan_capped"] is True
    assert summary["targeting_scan"]["scanned_adsets"] == assets.MAX_SCAN_PAGES
    assert "incomplete" in summary["targeting_scan"]["scan_note"]
    assert summary["targeting_next_after"] == f"t{assets.MAX_SCAN_PAGES}"
    assert summary["ads_scan"]["ran"] is False
    assert client.get_calls == []


def test_audience_adsets_without_account_reports_skipped_targeting_scan(monkeypatch) -> None:
    client = FakeAssetsClient(pages={("aud_1", "ads"): [_paged([])]})
    _patch(monkeypatch, client)
    result = asyncio.run(assets.list_audience_adsets(audience_id="aud_1"))
    assert result["summary"]["complete"] is False
    assert result["summary"]["targeting_scan"]["ran"] is False
    assert "account_id" in result["summary"]["targeting_scan"]["skipped_reason"]


def test_audience_adsets_targeting_continuation_uses_targeting_after(monkeypatch) -> None:
    client = FakeAssetsClient()
    _patch(monkeypatch, client)
    asyncio.run(assets.list_audience_adsets(audience_id="aud_1", source="targeting", account_id="act_9", targeting_after="t5"))
    assert client.list_calls[0]["params"] == {"limit": 200, "after": "t5"}
    assert client.list_calls[0]["parent_id"] == "act_9"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"source": "everything"}, "source must be one of"),
        ({"source": "ads", "targeting_after": "t"}, "targeting_after"),
        ({"source": "targeting", "after": "a"}, "targeting_after"),
    ],
)
def test_audience_adsets_source_validation_makes_no_call(monkeypatch, arguments, message) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match=message):
        asyncio.run(assets.list_audience_adsets(audience_id="aud_1", **arguments))


# Phase two: get_field_reference metrics


def test_get_field_reference_metrics_default_lists_core_and_derived(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(assets.get_field_reference(level="metric"))
    by_name = {item["name"]: item for item in result["items"]}
    assert set(assets.SERVER_CORE_METRICS) <= set(by_name)
    assert by_name["spend"]["meta_sort_values"] == ["spend_ascending", "spend_descending"]
    assert by_name["actions"]["value_shape"] == "action_list"
    assert by_name["actions"]["meta_sort_values"][0] == "actions:<action_type>_ascending"
    assert by_name["roas"]["kind"] == "server_derived"
    assert by_name["roas"]["formula"] == "conversion_value / spend"
    assert by_name["roas"]["meta_filterable"] is False
    summary = result["summary"]
    assert "GREATER_THAN" in summary["filter_operators"]
    assert "cost_per_result" in summary["all_insights_fields"]
    assert len(summary["all_insights_fields"]) == 229
    assert len(str(result)) < 20000


def test_get_field_reference_metrics_named_fields(monkeypatch) -> None:
    _no_client(monkeypatch)
    result = asyncio.run(assets.get_field_reference(level="insights", field_names="cost_per_result, inline_link_clicks, bogus"))
    by_name = {item["name"]: item for item in result["items"]}
    assert by_name["cost_per_result"]["value_shape"] == "list"
    assert by_name["cost_per_result"]["meta_sort_values"] is None
    assert by_name["inline_link_clicks"]["in_server_metrics_reference"] is False
    assert result["summary"]["unknown_fields"] == ["bogus"]
    assert "all_insights_fields" not in result["summary"]


def test_get_field_reference_metrics_reject_object_id(monkeypatch) -> None:
    _no_client(monkeypatch)
    with pytest.raises(ValidationError, match="object_id"):
        asyncio.run(assets.get_field_reference(level="metrics", field_names=["spend"], object_id="1"))
