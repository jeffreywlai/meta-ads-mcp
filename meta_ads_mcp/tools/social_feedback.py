"""Read-only social feedback tools for ads, Page posts, and Pages."""

from __future__ import annotations

from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaApiError, NotFoundError, UnsupportedFeatureError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client
from meta_ads_mcp.normalize import blank_to_none, normalize_collection

SOCIAL_CREATIVE_FIELDS = [
    "id",
    "name",
    "object_story_id",
    "effective_object_story_id",
    "effective_instagram_media_id",
    "effective_instagram_story_id",
    "instagram_permalink_url",
    "object_story_spec",
]

AD_SOCIAL_FIELDS = [
    "id",
    "name",
    "effective_status",
    f"creative{{{','.join(SOCIAL_CREATIVE_FIELDS)}}}",
]

FACEBOOK_COMMENT_FILTERS = {"stream", "toplevel"}
FACEBOOK_COMMENT_ORDERS = {"chronological", "reverse_chronological"}
COMMENT_SURFACES = {"auto", "facebook", "instagram", "all"}

SOCIAL_PERMISSION_NOTES = [
    "Facebook comments require access to the parent Page/post, usually a Page token with Page tasks and pages_read_user_content or pages_manage_engagement.",
    "Instagram comments require an Instagram business/creator media id and Instagram comment permissions on the connected account.",
]
PAGE_RECOMMENDATION_PERMISSION_NOTES = [
    "Page recommendations require a Page access token from a person who can perform Page tasks and pages_read_user_content.",
]

SOCIAL_MISSING_SIGNALS = [
    "Customer feedback score is not exposed here as a stable public Marketing API field.",
    "Negative-feedback counts such as hides, reports, or hides-all are not exposed here as stable Ads Insights fields.",
    "Commerce/catalog product review feeds are not exposed here; use list_page_recommendations for Page-level recommendations.",
]


def _validate_limit(name: str, value: int, *, minimum: int = 1, maximum: int = 100) -> None:
    """Validate bounded Graph edge limits."""
    if value < minimum or value > maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}.")


def _truncate_text(value: Any, max_chars: int) -> tuple[str | None, bool]:
    """Return text bounded for LLM-friendly output."""
    if value is None:
        return None, False
    text = str(value)
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip() + "...", True
    return text, False


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first non-empty value from a mapping."""
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return None


def _story_page_id(story_id: str | None, object_story_spec: dict[str, Any] | None) -> str | None:
    """Infer the Page id attached to a story when Meta exposes it."""
    if object_story_spec and object_story_spec.get("page_id"):
        return str(object_story_spec["page_id"])
    if story_id and "_" in story_id:
        return story_id.split("_", 1)[0]
    return None


def _social_paths(creative: dict[str, Any]) -> list[dict[str, Any]]:
    """Build compact next-call hints from creative social ids."""
    story_id = _first_present(creative, "effective_object_story_id", "object_story_id")
    instagram_media_id = creative.get("effective_instagram_media_id")
    paths: list[dict[str, Any]] = []
    if story_id:
        paths.append(
            {
                "surface": "facebook",
                "object_story_id": story_id,
                "tool": "list_ad_comments",
                "arguments": {"object_story_id": story_id, "surface": "facebook"},
            }
        )
    if instagram_media_id:
        paths.append(
            {
                "surface": "instagram",
                "instagram_media_id": instagram_media_id,
                "tool": "list_ad_comments",
                "arguments": {"instagram_media_id": instagram_media_id, "surface": "instagram"},
            }
        )
    return paths


def _compact_creative(creative: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields useful for choosing comment surfaces."""
    story_id = _first_present(creative, "effective_object_story_id", "object_story_id")
    object_story_spec = creative.get("object_story_spec") if isinstance(creative.get("object_story_spec"), dict) else {}
    return {
        "id": creative.get("id"),
        "name": creative.get("name"),
        "object_story_id": creative.get("object_story_id"),
        "effective_object_story_id": creative.get("effective_object_story_id"),
        "effective_instagram_media_id": creative.get("effective_instagram_media_id"),
        "effective_instagram_story_id": creative.get("effective_instagram_story_id"),
        "instagram_permalink_url": creative.get("instagram_permalink_url"),
        "page_id": _story_page_id(story_id, object_story_spec),
    }


async def _resolve_ad_social_context(ad_id: str, *, resolve_creative: bool = True) -> dict[str, Any]:
    """Resolve social comment ids for an ad with the fewest useful calls."""
    client = get_graph_api_client()
    api_calls = 1
    try:
        ad = await client.get_object(ad_id, fields=AD_SOCIAL_FIELDS)
        creative = ad.get("creative") if isinstance(ad.get("creative"), dict) else {}
        creative_id = creative.get("id")
        has_social_ids = any(
            creative.get(key)
            for key in (
                "object_story_id",
                "effective_object_story_id",
                "effective_instagram_media_id",
                "effective_instagram_story_id",
            )
        )
        if resolve_creative and creative_id and not has_social_ids:
            api_calls += 1
            creative = await client.get_object(str(creative_id), fields=SOCIAL_CREATIVE_FIELDS)
    except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
        setattr(exc, "_meta_ads_api_calls", api_calls)
        raise

    compact_creative = _compact_creative(creative)
    paths = _social_paths(compact_creative)
    return {
        "scope": {"ad_id": ad_id},
        "ad": {
            "id": ad.get("id"),
            "name": ad.get("name"),
            "effective_status": ad.get("effective_status"),
        },
        "creative": compact_creative,
        "available_feedback_paths": paths,
        "missing_feedback_paths": [] if paths else ["No Facebook story id or Instagram media id was exposed for this ad creative."],
        "missing_signals": SOCIAL_MISSING_SIGNALS,
        "permission_notes": SOCIAL_PERMISSION_NOTES,
        "summary": {
            "api_calls": api_calls,
            "available_path_count": len(paths),
            "surfaces": [path["surface"] for path in paths],
        },
    }


def _facebook_comment_fields(include_replies: bool, reply_limit: int, include_author: bool) -> list[str]:
    """Return compact Facebook comment fields."""
    fields = ["id", "message", "created_time", "like_count", "comment_count", "parent{id}", "permalink_url"]
    if include_author:
        fields.append("from{name}")
    if include_replies and reply_limit:
        reply_fields = ["id", "message", "created_time", "like_count", "comment_count"]
        if include_author:
            reply_fields.append("from{name}")
        fields.append(f"comments.limit({reply_limit}){{{','.join(reply_fields)}}}")
    return fields


def _instagram_comment_fields(include_replies: bool, reply_limit: int, include_author: bool) -> list[str]:
    """Return compact Instagram comment fields."""
    fields = ["id", "text", "timestamp", "like_count"]
    if include_author:
        fields.append("username")
    if include_replies and reply_limit:
        reply_fields = ["id", "text", "timestamp", "like_count"]
        if include_author:
            reply_fields.append("username")
        fields.append(f"replies.limit({reply_limit}){{{','.join(reply_fields)}}}")
    return fields


def _compact_comment(
    item: dict[str, Any],
    *,
    surface: str,
    max_message_chars: int,
    include_author: bool,
) -> dict[str, Any]:
    """Normalize Facebook and Instagram comments to a shared compact shape."""
    text_key = "text" if surface == "instagram" else "message"
    created_key = "timestamp" if surface == "instagram" else "created_time"
    message, truncated = _truncate_text(item.get(text_key), max_message_chars)
    compact: dict[str, Any] = {
        "id": item.get("id"),
        "surface": surface,
        "message": message,
        "message_truncated": truncated,
        "created_time": item.get(created_key),
        "like_count": item.get("like_count"),
    }
    if surface == "facebook":
        compact["reply_count"] = item.get("comment_count")
        parent = item.get("parent") if isinstance(item.get("parent"), dict) else {}
        if parent.get("id"):
            compact["parent_id"] = parent["id"]
        if item.get("permalink_url"):
            compact["permalink_url"] = item["permalink_url"]
        replies = item.get("comments") if isinstance(item.get("comments"), dict) else {}
    else:
        replies = item.get("replies") if isinstance(item.get("replies"), dict) else {}
    if include_author:
        author = item.get("from") if surface == "facebook" else {"name": item.get("username")}
        if isinstance(author, dict) and author.get("name"):
            compact["author"] = {"name": author["name"]}
    reply_rows = replies.get("data") if isinstance(replies.get("data"), list) else []
    if reply_rows:
        compact["replies"] = [
            _compact_comment(reply, surface=surface, max_message_chars=max_message_chars, include_author=include_author)
            for reply in reply_rows
        ]
    return {key: value for key, value in compact.items() if value is not None}


async def _comments_for_surface(
    *,
    parent_id: str,
    surface: str,
    limit: int,
    after: str | None,
    include_replies: bool,
    reply_limit: int,
    include_author: bool,
    max_message_chars: int,
    comment_filter: str,
    order: str | None,
) -> dict[str, Any]:
    """Fetch comments for one concrete social surface."""
    client = get_graph_api_client()
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    if surface == "facebook":
        params["filter"] = comment_filter
        if order:
            params["order"] = order
        fields = _facebook_comment_fields(include_replies, reply_limit, include_author)
    else:
        fields = _instagram_comment_fields(include_replies, reply_limit, include_author)

    payload = await client.list_objects(parent_id, "comments", fields=fields, params=params)
    normalized = normalize_collection(payload)
    return {
        "surface": surface,
        "parent_id": parent_id,
        "items": [
            _compact_comment(
                item,
                surface=surface,
                max_message_chars=max_message_chars,
                include_author=include_author,
            )
            for item in normalized["items"]
        ],
        "paging": normalized["paging"],
        "count": normalized["summary"]["count"],
    }


def _social_error_payload(
    *,
    scope: dict[str, Any],
    error: Exception,
    api_calls: int,
    permission_notes: list[str] | None = None,
) -> dict[str, Any]:
    """Return compact unavailable output for permission-gated social surfaces."""
    return {
        "items": [],
        "paging": {"before": None, "after": None, "next": None},
        "summary": {
            "count": 0,
            "api_calls": api_calls,
            "unavailable": True,
            "reason": str(error),
        },
        "scope": scope,
        "missing_signals": SOCIAL_MISSING_SIGNALS,
        "permission_notes": permission_notes or SOCIAL_PERMISSION_NOTES,
    }


@mcp_server.tool()
async def get_ad_social_context(ad_id: str, resolve_creative: bool = True) -> dict[str, Any]:
    """Use this when you need to inspect the Facebook post or Instagram media ids behind an ad."""
    return await _resolve_ad_social_context(ad_id, resolve_creative=resolve_creative)


@mcp_server.tool()
async def list_ad_comments(
    ad_id: str | None = None,
    object_story_id: str | None = None,
    instagram_media_id: str | None = None,
    surface: str = "auto",
    limit: int = 25,
    after: str | None = None,
    include_replies: bool = False,
    reply_limit: int = 3,
    include_author: bool = False,
    max_message_chars: int = 500,
    comment_filter: str = "stream",
    order: str | None = "reverse_chronological",
) -> dict[str, Any]:
    """Use this first for raw Facebook ad comments or Instagram ad comments from an ad id, Page post id, or IG media id."""
    ad_id = blank_to_none(ad_id)
    object_story_id = blank_to_none(object_story_id)
    instagram_media_id = blank_to_none(instagram_media_id)
    provided_ids = [value for value in (ad_id, object_story_id, instagram_media_id) if value]
    if len(provided_ids) != 1:
        raise ValidationError("Provide exactly one of ad_id, object_story_id, or instagram_media_id.")
    if surface not in COMMENT_SURFACES:
        raise ValidationError(f"surface must be one of {sorted(COMMENT_SURFACES)}.")
    if comment_filter not in FACEBOOK_COMMENT_FILTERS:
        raise ValidationError(f"comment_filter must be one of {sorted(FACEBOOK_COMMENT_FILTERS)}.")
    if order is not None and order not in FACEBOOK_COMMENT_ORDERS:
        raise ValidationError(f"order must be one of {sorted(FACEBOOK_COMMENT_ORDERS)} or None.")
    _validate_limit("limit", limit)
    _validate_limit("reply_limit", reply_limit, minimum=0, maximum=25)
    _validate_limit("max_message_chars", max_message_chars, minimum=0, maximum=5000)
    if after and surface not in {"facebook", "instagram"}:
        raise ValidationError("after is supported only when fetching one concrete surface.")

    api_calls = 0
    scope = {
        "ad_id": ad_id,
        "object_story_id": object_story_id,
        "instagram_media_id": instagram_media_id,
        "surface": surface,
    }
    try:
        targets: list[tuple[str, str]] = []
        if ad_id:
            context = await _resolve_ad_social_context(ad_id)
            api_calls += context["summary"]["api_calls"]
            creative = context["creative"]
            facebook_id = _first_present(creative, "effective_object_story_id", "object_story_id")
            instagram_id = creative.get("effective_instagram_media_id")
        else:
            facebook_id = object_story_id
            instagram_id = instagram_media_id

        if surface in {"auto", "facebook", "all"} and facebook_id:
            targets.append(("facebook", str(facebook_id)))
        if surface in {"auto", "instagram", "all"} and instagram_id:
            targets.append(("instagram", str(instagram_id)))

        if not targets:
            return {
                "items": [],
                "paging": {"before": None, "after": None, "next": None},
                "summary": {
                    "count": 0,
                    "api_calls": api_calls,
                    "surfaces": [],
                    "missing_surfaces": ["No matching social comment id was available for the requested surface."],
                },
                "scope": scope,
                "missing_signals": SOCIAL_MISSING_SIGNALS,
                "permission_notes": SOCIAL_PERMISSION_NOTES,
            }

        fetched = []
        unavailable_surfaces: list[dict[str, str]] = []
        for target_surface, parent_id in targets:
            api_calls += 1
            try:
                result = await _comments_for_surface(
                    parent_id=parent_id,
                    surface=target_surface,
                    limit=limit,
                    after=after,
                    include_replies=include_replies,
                    reply_limit=reply_limit,
                    include_author=include_author,
                    max_message_chars=max_message_chars,
                    comment_filter=comment_filter,
                    order=order,
                )
                fetched.append(result)
                if surface == "auto" and result["items"]:
                    break
            except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
                unavailable_surfaces.append(
                    {
                        "surface": target_surface,
                        "parent_id": parent_id,
                        "reason": str(exc),
                    }
                )
                if surface not in {"auto", "all"}:
                    raise
        if not fetched and unavailable_surfaces:
            reasons = (
                unavailable_surfaces[0]["reason"]
                if len(unavailable_surfaces) == 1
                else "; ".join(f"{item['surface']}: {item['reason']}" for item in unavailable_surfaces)
            )
            return _social_error_payload(
                scope=scope,
                error=UnsupportedFeatureError(reasons),
                api_calls=api_calls,
            )
    except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
        attempted_calls = getattr(exc, "_meta_ads_api_calls", 1)
        return _social_error_payload(scope=scope, error=exc, api_calls=max(api_calls, attempted_calls))

    items = [item for result in fetched for item in result["items"]]
    one_surface = len(fetched) == 1
    summary = {
        "count": len(items),
        "api_calls": api_calls,
        "surfaces": [result["surface"] for result in fetched],
        "parents": {result["surface"]: result["parent_id"] for result in fetched},
        "include_replies": include_replies,
        "max_message_chars": max_message_chars,
    }
    if unavailable_surfaces:
        summary["unavailable_surfaces"] = unavailable_surfaces
    return {
        "items": items,
        "paging": fetched[0]["paging"] if one_surface else {"by_surface": {result["surface"]: result["paging"] for result in fetched}},
        "summary": summary,
        "scope": scope,
        "missing_signals": SOCIAL_MISSING_SIGNALS,
        "permission_notes": SOCIAL_PERMISSION_NOTES,
    }


@mcp_server.tool()
async def list_page_recommendations(
    page_id: str,
    limit: int = 25,
    after: str | None = None,
    include_reviewer: bool = False,
    max_message_chars: int = 500,
) -> dict[str, Any]:
    """Use this to read compact Facebook Page recommendations, reviews, or testimonials for an owned Page."""
    _validate_limit("limit", limit)
    _validate_limit("max_message_chars", max_message_chars, minimum=0, maximum=5000)
    fields = ["created_time", "review_text", "rating", "recommendation_type", "open_graph_story{id}"]
    if include_reviewer:
        fields.append("reviewer{name}")
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    try:
        payload = await get_graph_api_client().list_objects(page_id, "ratings", fields=fields, params=params)
    except (MetaApiError, NotFoundError, UnsupportedFeatureError) as exc:
        return _social_error_payload(
            scope={"page_id": page_id},
            error=exc,
            api_calls=1,
            permission_notes=PAGE_RECOMMENDATION_PERMISSION_NOTES,
        )

    normalized = normalize_collection(payload)
    items: list[dict[str, Any]] = []
    for item in normalized["items"]:
        message, truncated = _truncate_text(item.get("review_text"), max_message_chars)
        compact = {
            "created_time": item.get("created_time"),
            "message": message,
            "message_truncated": truncated,
            "rating": item.get("rating"),
            "recommendation_type": item.get("recommendation_type"),
        }
        story = item.get("open_graph_story") if isinstance(item.get("open_graph_story"), dict) else {}
        if story.get("id"):
            compact["story_id"] = story["id"]
        reviewer = item.get("reviewer") if isinstance(item.get("reviewer"), dict) else {}
        if include_reviewer and reviewer.get("name"):
            compact["reviewer"] = {"name": reviewer["name"]}
        items.append({key: value for key, value in compact.items() if value is not None})

    return {
        "items": items,
        "paging": normalized["paging"],
        "scope": {"page_id": page_id},
        "summary": {
            "count": len(items),
            "api_calls": 1,
            "page_id": page_id,
            "max_message_chars": max_message_chars,
        },
        "missing_signals": SOCIAL_MISSING_SIGNALS,
        "permission_notes": PAGE_RECOMMENDATION_PERMISSION_NOTES,
    }
