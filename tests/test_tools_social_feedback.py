"""Social feedback tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.errors import MetaApiError
from meta_ads_mcp.tools import social_feedback


class FakeSocialClient:
    """Fake API client for social feedback tests."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, list[str] | None]] = []
        self.list_calls: list[tuple[str, str, list[str] | None, dict[str, object] | None]] = []

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.get_calls.append((object_id, fields))
        if object_id == "ad_full":
            return {
                "id": "ad_full",
                "name": "Social Ad",
                "effective_status": "ACTIVE",
                "creative": {
                    "id": "crt_full",
                    "name": "Creative",
                    "effective_object_story_id": "page_1_post_1",
                    "effective_instagram_media_id": "ig_media_1",
                    "object_story_spec": {"page_id": "page_1"},
                },
            }
        if object_id == "ad_needs_creative":
            return {"id": "ad_needs_creative", "name": "Ad", "creative": {"id": "crt_1"}}
        if object_id == "crt_1":
            return {
                "id": "crt_1",
                "name": "Fetched Creative",
                "object_story_id": "page_2_post_2",
            }
        return {"id": object_id, "creative": {}}

    async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
        self.list_calls.append((parent_id, edge, fields, params))
        if edge == "comments" and parent_id == "page_1_post_1":
            return {
                "data": [
                    {
                        "id": "comment_1",
                        "message": "This is a long comment about the ad",
                        "created_time": "2026-04-01T00:00:00+0000",
                        "like_count": 2,
                        "comment_count": 1,
                    }
                ],
                "paging": {"cursors": {"after": "after_fb"}},
            }
        if edge == "comments" and parent_id == "ig_media_1":
            return {
                "data": [
                    {
                        "id": "ig_comment_1",
                        "text": "IG comment",
                        "timestamp": "2026-04-01T00:00:00+0000",
                        "like_count": 3,
                    }
                ],
                "paging": {"cursors": {"after": "after_ig"}},
            }
        if edge == "ratings":
            return {
                "data": [
                    {
                        "created_time": "2026-04-02T00:00:00+0000",
                        "review_text": "Great store and great service",
                        "recommendation_type": "positive",
                        "reviewer": {"name": "Customer"},
                    }
                ],
                "paging": {"cursors": {"after": "after_review"}},
            }
        raise AssertionError(f"Unexpected edge: {parent_id}/{edge}")


def test_get_ad_social_context_uses_expanded_creative_without_extra_call(monkeypatch) -> None:
    client = FakeSocialClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.get_ad_social_context("ad_full"))

    assert result["summary"]["api_calls"] == 1
    assert result["creative"]["page_id"] == "page_1"
    assert result["available_feedback_paths"][0]["surface"] == "facebook"
    assert result["available_feedback_paths"][0]["arguments"] == {
        "object_story_id": "page_1_post_1",
        "surface": "facebook",
    }
    assert result["available_feedback_paths"][1]["surface"] == "instagram"
    assert result["available_feedback_paths"][1]["arguments"] == {
        "instagram_media_id": "ig_media_1",
        "surface": "instagram",
    }
    assert [call[0] for call in client.get_calls] == ["ad_full"]


def test_get_ad_social_context_fetches_creative_only_when_social_ids_missing(monkeypatch) -> None:
    client = FakeSocialClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.get_ad_social_context("ad_needs_creative"))

    assert result["summary"]["api_calls"] == 2
    assert result["creative"]["object_story_id"] == "page_2_post_2"
    assert [call[0] for call in client.get_calls] == ["ad_needs_creative", "crt_1"]


def test_list_ad_comments_counts_failed_creative_resolution(monkeypatch) -> None:
    class FailingCreativeClient(FakeSocialClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "crt_1":
                self.get_calls.append((object_id, fields))
                raise MetaApiError("Creative unavailable", code=190)
            return await super().get_object(object_id, fields=fields, params=params)

    client = FailingCreativeClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.list_ad_comments(ad_id="ad_needs_creative"))

    assert result["summary"]["api_calls"] == 2
    assert result["summary"]["unavailable"] is True
    assert [call[0] for call in client.get_calls] == ["ad_needs_creative", "crt_1"]


def test_list_ad_comments_direct_story_id_compacts_and_truncates(monkeypatch) -> None:
    client = FakeSocialClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(
        social_feedback.list_ad_comments(
            object_story_id="page_1_post_1",
            limit=5,
            max_message_chars=10,
        )
    )

    assert result["summary"]["api_calls"] == 1
    assert result["summary"]["surfaces"] == ["facebook"]
    assert result["items"][0]["message"] == "This is a..."
    assert result["items"][0]["message_truncated"] is True
    assert "author" not in result["items"][0]
    assert client.list_calls[0][3] == {
        "limit": 5,
        "filter": "stream",
        "order": "reverse_chronological",
    }


def test_reply_author_fields_are_requested_when_author_enabled() -> None:
    facebook_fields = social_feedback._facebook_comment_fields(
        include_replies=True,
        reply_limit=3,
        include_author=True,
    )
    instagram_fields = social_feedback._instagram_comment_fields(
        include_replies=True,
        reply_limit=3,
        include_author=True,
    )
    assert "comments.limit(3){id,message,created_time,like_count,comment_count,from{name}}" in facebook_fields
    assert "replies.limit(3){id,text,timestamp,like_count,username}" in instagram_fields


def test_list_ad_comments_can_fetch_all_available_ad_surfaces(monkeypatch) -> None:
    client = FakeSocialClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.list_ad_comments(ad_id="ad_full", surface="all"))

    assert result["summary"]["api_calls"] == 3
    assert result["summary"]["surfaces"] == ["facebook", "instagram"]
    assert {item["surface"] for item in result["items"]} == {"facebook", "instagram"}
    assert result["paging"]["by_surface"]["facebook"]["after"] == "after_fb"
    assert result["paging"]["by_surface"]["instagram"]["after"] == "after_ig"


def test_list_ad_comments_auto_falls_back_to_instagram_when_facebook_errors(monkeypatch) -> None:
    class FallbackClient(FakeSocialClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if parent_id == "page_1_post_1" and edge == "comments":
                raise MetaApiError("Missing Page permission", code=200)
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    client = FallbackClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.list_ad_comments(ad_id="ad_full"))

    assert result["summary"]["api_calls"] == 3
    assert result["summary"]["surfaces"] == ["instagram"]
    assert result["summary"]["unavailable_surfaces"][0]["surface"] == "facebook"
    assert result["items"][0]["surface"] == "instagram"


def test_list_ad_comments_auto_keeps_fetching_after_empty_surface(monkeypatch) -> None:
    class EmptyFacebookClient(FakeSocialClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            if parent_id == "page_1_post_1" and edge == "comments":
                return {"data": [], "paging": {"cursors": {"after": "after_empty_fb"}}}
            return await super().list_objects(parent_id, edge, fields=fields, params=params)

    client = EmptyFacebookClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.list_ad_comments(ad_id="ad_full"))

    assert result["summary"]["api_calls"] == 3
    assert result["summary"]["surfaces"] == ["facebook", "instagram"]
    assert result["summary"]["count"] == 1
    assert result["items"][0]["surface"] == "instagram"


def test_list_ad_comments_rejects_auto_pagination_cursor() -> None:
    with pytest.raises(social_feedback.ValidationError, match="one concrete surface"):
        asyncio.run(social_feedback.list_ad_comments(ad_id="ad_full", after="after_fb"))


def test_list_ad_comments_ignores_blank_optional_ids(monkeypatch) -> None:
    client = FakeSocialClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.list_ad_comments(ad_id="ad_full", object_story_id=" "))

    assert result["summary"]["surfaces"] == ["facebook"]
    assert result["items"][0]["id"] == "comment_1"


def test_list_page_recommendations_compacts_reviews(monkeypatch) -> None:
    client = FakeSocialClient()
    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: client)

    result = asyncio.run(social_feedback.list_page_recommendations("page_1", include_reviewer=True))

    assert result["summary"]["api_calls"] == 1
    assert result["scope"] == {"page_id": "page_1"}
    assert result["items"][0]["message"] == "Great store and great service"
    assert result["items"][0]["reviewer"] == {"name": "Customer"}
    assert client.list_calls[0][0:2] == ("page_1", "ratings")


def test_social_permission_errors_return_structured_unavailable(monkeypatch) -> None:
    class PermissionClient(FakeSocialClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            raise MetaApiError("Missing Page permission", code=200)

    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: PermissionClient())

    result = asyncio.run(social_feedback.list_ad_comments(object_story_id="page_1_post_1"))

    assert result["summary"]["unavailable"] is True
    assert result["summary"]["reason"] == "Missing Page permission"
    assert result["permission_notes"]


def test_page_recommendation_permission_errors_use_page_specific_notes(monkeypatch) -> None:
    class PermissionClient(FakeSocialClient):
        async def list_objects(self, parent_id: str, edge: str, *, fields=None, params=None):
            raise MetaApiError("Missing Page ratings permission", code=200)

    monkeypatch.setattr(social_feedback, "get_graph_api_client", lambda: PermissionClient())

    result = asyncio.run(social_feedback.list_page_recommendations("page_1"))

    assert result["summary"]["unavailable"] is True
    assert result["permission_notes"] == social_feedback.PAGE_RECOMMENDATION_PERMISSION_NOTES
    assert "Instagram" not in " ".join(result["permission_notes"])
