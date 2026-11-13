"""Creative builders that assemble Meta's raw creative spec from named inputs.

Every format here follows a request shape shown in Meta's public Marketing API
documentation (ad creative reference, carousel guide, asset feed spec and
placement asset customization guides, lead ads guide, click-to-Messenger and
click-to-WhatsApp guides, Instagram post and Partnership Ads guides). Formats
Meta does not document are not built.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from meta_ads_mcp.coordinator import mcp_server
from meta_ads_mcp.errors import MetaAdsError, MetaApiError, RateLimitError, ValidationError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.normalize import blank_to_none
from meta_ads_mcp.schemas import creation_response, mutation_response
from meta_ads_mcp.tool_types import AccountId, GraphId, StringList, coerce_csv_string_list
from meta_ads_mcp.url_secrets import refuse_url_credentials

# AdCreativeLinkDataCallToAction.type values from Meta's reference page, plus
# INSTAGRAM_MESSAGE, which Meta's click-to-Instagram examples use.
CALL_TO_ACTION_TYPES = frozenset({
    "OPEN_LINK", "LIKE_PAGE", "SHOP_NOW", "PLAY_GAME", "INSTALL_APP", "USE_APP", "CALL", "CALL_ME",
    "VIDEO_CALL", "INSTALL_MOBILE_APP", "USE_MOBILE_APP", "MOBILE_DOWNLOAD", "BOOK_TRAVEL", "LISTEN_MUSIC",
    "WATCH_VIDEO", "LEARN_MORE", "SIGN_UP", "DOWNLOAD", "WATCH_MORE", "NO_BUTTON", "VISIT_PAGES_FEED",
    "CALL_NOW", "APPLY_NOW", "CONTACT", "BUY_NOW", "GET_OFFER", "GET_OFFER_VIEW", "BUY_TICKETS",
    "UPDATE_APP", "GET_DIRECTIONS", "BUY", "SEND_UPDATES", "MESSAGE_PAGE", "DONATE", "SUBSCRIBE",
    "SAY_THANKS", "SELL_NOW", "SHARE", "DONATE_NOW", "GET_QUOTE", "CONTACT_US", "ORDER_NOW",
    "START_ORDER", "ADD_TO_CART", "VIEW_CART", "VIEW_IN_CART", "VIDEO_ANNOTATION", "RECORD_NOW",
    "INQUIRE_NOW", "CONFIRM", "REFER_FRIENDS", "REQUEST_TIME", "GET_SHOWTIMES", "LISTEN_NOW",
    "TRY_DEMO", "WOODHENGE_SUPPORT", "SOTTO_SUBSCRIBE", "FOLLOW_USER", "RAISE_MONEY", "SEE_SHOP",
    "GET_DETAILS", "FIND_OUT_MORE", "VISIT_WEBSITE", "BROWSE_SHOP", "EVENT_RSVP", "WHATSAPP_MESSAGE",
    "FOLLOW_NEWS_STORYLINE", "SEE_MORE", "BOOK_NOW", "FIND_A_GROUP", "FIND_YOUR_GROUPS",
    "PAY_TO_ACCESS", "PURCHASE_GIFT_CARDS", "FOLLOW_PAGE", "SEND_A_GIFT", "SWIPE_UP_SHOP",
    "SWIPE_UP_PRODUCT", "SEND_GIFT_MONEY", "PLAY_GAME_ON_FACEBOOK", "GET_STARTED", "OPEN_INSTANT_APP",
    "AUDIO_CALL", "GET_PROMOTIONS", "JOIN_CHANNEL", "MAKE_AN_APPOINTMENT", "ASK_ABOUT_SERVICES",
    "BOOK_A_CONSULTATION", "GET_A_QUOTE", "BUY_VIA_MESSAGE", "ASK_FOR_MORE_INFO", "CHAT_WITH_US",
    "VIEW_PRODUCT", "VIEW_CHANNEL", "GET_IN_TOUCH", "ASK_A_QUESTION", "START_A_CHAT", "CHAT_NOW",
    "ASK_US", "WATCH_LIVE_VIDEO", "JOIN_LIVE_VIDEO", "SHOP_WITH_AI", "TRY_ON_WITH_AI", "WATCH_NOW",
    "STREAM_NOW", "BUY_ACTIVITY_TICKETS", "INSTAGRAM_MESSAGE",
})

# Click-to-message CTAs and the app_destination Meta's guides pair with each.
MESSAGING_DESTINATIONS = {
    "WHATSAPP_MESSAGE": "WHATSAPP",
    "MESSAGE_PAGE": "MESSENGER",
    "INSTAGRAM_MESSAGE": "INSTAGRAM_DIRECT",
}
WHATSAPP_LINK = "https://api.whatsapp.com/send"

# Lead ads guide: the CTAs an instant form accepts, and the only link allowed.
LEAD_FORM_CALL_TO_ACTIONS = frozenset({"APPLY_NOW", "DOWNLOAD", "GET_QUOTE", "LEARN_MORE", "SIGN_UP", "SUBSCRIBE"})
LEAD_FORM_LINK = "https://fb.me/"

DEFAULT_CALL_TO_ACTION = "LEARN_MORE"
MAX_TEXT_VARIANTS = 5
MAX_BODY_CHARS = 1024
MAX_TITLE_CHARS = 255
MIN_CARDS = 2
MAX_CARDS = 10
MIN_PLACEMENT_ASSETS = 2
MAX_PLACEMENT_ASSETS = 10

# Placement asset customization guide: platforms and their positions field.
PLACEMENT_POSITION_FIELDS = {
    "facebook": "facebook_positions",
    "instagram": "instagram_positions",
    "messenger": "messenger_positions",
    "audience_network": "audience_network_positions",
    "threads": "threads_positions",
}
PARTNERSHIP_AD_FORMATS = {1: "both identities", 2: "first identity only", 3: "automatic"}

_OBJECT_STORY_ID = re.compile(r"^[0-9]+_[0-9]+$")


def _text(value: Any) -> str | None:
    return blank_to_none(value) if isinstance(value, str) else None


def _refuse_logins(value: Any, name: str) -> None:
    """Refuse a URL with an embedded login anywhere in an input, before any request is built."""
    if isinstance(value, str):
        refuse_url_credentials(value, name)
    elif isinstance(value, dict):
        for key, item in value.items():
            _refuse_logins(item, f"{name}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value, start=1):
            _refuse_logins(item, f"{name}[{index}]")


def _require_url(value: str, name: str) -> str:
    refuse_url_credentials(value, name)
    if not re.match(r"^https?://\S+$", value):
        raise ValidationError(f"{name} must be a full http:// or https:// URL.")
    return value


def _call_to_action_type(value: str | None, name: str = "call_to_action_type") -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    normalized = normalized.upper()
    if normalized not in CALL_TO_ACTION_TYPES:
        raise ValidationError(f"{name} {normalized} is not one of Meta's call-to-action types.")
    return normalized


def _variants(values: list[str] | None, name: str, max_chars: int) -> list[str] | None:
    if values is None:
        return None
    values = coerce_csv_string_list(values)
    if not isinstance(values, list) or not values:
        raise ValidationError(f"{name} must list 1 to {MAX_TEXT_VARIANTS} texts.")
    cleaned = [value.strip() if isinstance(value, str) else "" for value in values]
    if any(not value for value in cleaned):
        raise ValidationError(f"{name} must not contain blank texts.")
    if len(cleaned) > MAX_TEXT_VARIANTS:
        raise ValidationError(f"{name} accepts at most {MAX_TEXT_VARIANTS} texts; Meta's asset feed limit is {MAX_TEXT_VARIANTS}.")
    if len(set(cleaned)) != len(cleaned):
        raise ValidationError(f"{name} must not repeat a text.")
    if any(len(value) > max_chars for value in cleaned):
        raise ValidationError(f"Each text in {name} must be at most {max_chars} characters.")
    return cleaned


def _single(value: str | None, plural: list[str] | None, singular_name: str, plural_name: str) -> None:
    if value is not None and plural is not None:
        raise ValidationError(f"Use {singular_name} or {plural_name}, not both.")


def _media_choice(image_hash: str | None, image_url: str | None, where: str) -> dict[str, str]:
    if image_hash and image_url:
        raise ValidationError(f"{where}: give image_hash or image_url, not both.")
    if image_url:
        return {"image_url": _require_url(image_url, f"{where} image_url")}
    if image_hash:
        return {"image_hash": image_hash}
    return {}


def _link_data_image(media: dict[str, str]) -> dict[str, str]:
    """link_data takes image_hash, or picture for an image URL."""
    if "image_url" in media:
        return {"picture": media["image_url"]}
    return dict(media)


def _check_cards(cards: Any) -> list[dict[str, Any]]:
    if not isinstance(cards, list) or not MIN_CARDS <= len(cards) <= MAX_CARDS:
        raise ValidationError(f"cards must list {MIN_CARDS} to {MAX_CARDS} cards.")
    allowed = {"image_hash", "image_url", "video_id", "link", "headline", "description", "call_to_action_type"}
    for index, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            raise ValidationError(f"cards[{index}] must be an object.")
        unknown = sorted(set(card) - allowed)
        if unknown:
            raise ValidationError(f"cards[{index}] has unknown keys: {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}.")
    return cards


class _Inputs:
    """Normalized inputs shared by every format builder."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


def _detect_format(inputs: _Inputs) -> str:
    selectors = {
        "object_story_id": inputs.object_story_id,
        "source_instagram_media_id": inputs.source_instagram_media_id,
        "partnership_ad_code": inputs.partnership_ad_code,
        "cards": inputs.cards,
        "placement_customizations": inputs.placement_customizations,
    }
    chosen = [name for name, value in selectors.items() if value is not None]
    if len(chosen) > 1:
        raise ValidationError(f"Use only one of {', '.join(chosen)}; each selects a different creative format.")
    has_variants = any(value is not None for value in (inputs.primary_texts, inputs.headlines, inputs.descriptions))
    if chosen:
        if has_variants:
            raise ValidationError(
                "primary_texts, headlines and descriptions apply to a single image or single video only; "
                f"they cannot be combined with {chosen[0]}."
            )
        return {
            "object_story_id": "page_post",
            "source_instagram_media_id": "instagram_post",
            "partnership_ad_code": "partnership_ad_code",
            "cards": "carousel",
            "placement_customizations": "placement_customization",
        }[chosen[0]]
    if has_variants:
        return "text_variants"
    if inputs.video_id:
        return "single_video"
    if inputs.image_hash or inputs.image_url:
        return "single_image"
    raise ValidationError(
        "No creative format selected. Give image_hash or image_url (single image), video_id (single video), "
        "cards (carousel), primary_texts/headlines/descriptions with media (text variants), "
        "placement_customizations, object_story_id (existing Page post), source_instagram_media_id "
        "(existing Instagram post) or partnership_ad_code."
    )


def _reject(inputs: _Inputs, names: list[str], format_label: str) -> None:
    given = [name for name in names if getattr(inputs, name) not in (None, [], "")]
    if given:
        raise ValidationError(f"{format_label} does not accept {', '.join(given)}.")


_CONTENT_INPUTS = [
    "image_hash", "image_url", "video_id", "message", "headline", "description", "primary_texts",
    "headlines", "descriptions", "cards", "placement_customizations", "lead_gen_form_id", "page_welcome_message",
]


def _partnership(inputs: _Inputs, *, required: bool) -> dict[str, Any]:
    """Sponsor fields from Meta's Partnership Ads guide."""
    fields: dict[str, Any] = {}
    if inputs.sponsor_page_id:
        fields["facebook_branded_content"] = {"sponsor_page_id": inputs.sponsor_page_id}
    if inputs.sponsor_instagram_account_id:
        fields["instagram_branded_content"] = {"sponsor_id": inputs.sponsor_instagram_account_id}
    if required and not fields:
        raise ValidationError(
            "A Partnership Ad needs sponsor_page_id, sponsor_instagram_account_id, or both "
            "(the partner identity shown beside the post owner)."
        )
    return fields


def _require_page(inputs: _Inputs, format_label: str) -> str:
    if not inputs.page_id:
        raise ValidationError(f"page_id is required for {format_label}.")
    return inputs.page_id


def _messaging(inputs: _Inputs) -> str | None:
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS:
        return inputs.call_to_action_type
    if inputs.page_welcome_message:
        raise ValidationError("page_welcome_message is only used with a click-to-message call_to_action_type (WHATSAPP_MESSAGE, MESSAGE_PAGE, INSTAGRAM_MESSAGE).")
    return None


def _check_messaging(inputs: _Inputs, cta: str) -> None:
    if inputs.link_url:
        raise ValidationError(
            f"{cta} opens a chat, so link_url is not used; Meta's click-to-message guides set the destination themselves."
        )
    if inputs.lead_gen_form_id:
        raise ValidationError("lead_gen_form_id cannot be combined with a click-to-message call_to_action_type.")
    if cta == "INSTAGRAM_MESSAGE" and not inputs.instagram_account_id:
        raise ValidationError("INSTAGRAM_MESSAGE needs instagram_account_id: the button opens a chat with that account.")


def _check_lead_form(inputs: _Inputs) -> str:
    cta = inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION
    if cta not in LEAD_FORM_CALL_TO_ACTIONS:
        raise ValidationError(
            f"An instant form accepts call_to_action_type {', '.join(sorted(LEAD_FORM_CALL_TO_ACTIONS))}; got {cta}."
        )
    if inputs.link_url:
        raise ValidationError(f"With lead_gen_form_id Meta only accepts {LEAD_FORM_LINK} as the link, so omit link_url.")
    return cta


def _story_spec(inputs: _Inputs, page_id: str) -> dict[str, Any]:
    spec: dict[str, Any] = {"page_id": page_id}
    if inputs.instagram_account_id:
        spec["instagram_user_id"] = inputs.instagram_account_id
    return spec


def _build_single_image(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    page_id = _require_page(inputs, "a single image creative")
    media = _media_choice(inputs.image_hash, inputs.image_url, "single image")
    link_data: dict[str, Any] = {}
    notes: list[str] = []
    messaging = _messaging(inputs)
    if messaging:
        _check_messaging(inputs, messaging)
        if messaging == "WHATSAPP_MESSAGE":
            link_data["link"] = WHATSAPP_LINK
        else:
            notes.append(
                "Meta's click-to-Messenger and click-to-Instagram image examples show no fixed link value; "
                "link_data.link is left out."
            )
        link_data["call_to_action"] = {"type": messaging, "value": {"app_destination": MESSAGING_DESTINATIONS[messaging]}}
        if inputs.page_welcome_message:
            link_data["page_welcome_message"] = inputs.page_welcome_message
    elif inputs.lead_gen_form_id:
        cta = _check_lead_form(inputs)
        link_data["link"] = LEAD_FORM_LINK
        link_data["call_to_action"] = {"type": cta, "value": {"lead_gen_form_id": inputs.lead_gen_form_id}}
    else:
        if not inputs.link_url:
            raise ValidationError("link_url is required for a single image creative (unless it uses a click-to-message CTA or lead_gen_form_id).")
        link_data["link"] = inputs.link_url
        link_data["call_to_action"] = {
            "type": inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION,
            "value": {"link": inputs.link_url},
        }
    link_data.update(_link_data_image(media))
    if inputs.message:
        link_data["message"] = inputs.message
    if inputs.headline:
        link_data["name"] = inputs.headline
    if inputs.description:
        link_data["description"] = inputs.description
    spec = _story_spec(inputs, page_id)
    spec["link_data"] = link_data
    return {"object_story_spec": spec}, notes


def _build_single_video(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    page_id = _require_page(inputs, "a single video creative")
    thumbnail = _media_choice(inputs.image_hash, inputs.image_url, "single video thumbnail")
    if not thumbnail:
        raise ValidationError("A single video creative needs a thumbnail: give image_hash or image_url with video_id.")
    video_data: dict[str, Any] = {"video_id": inputs.video_id, **thumbnail}
    call_to_action: dict[str, Any] | None = None
    messaging = _messaging(inputs)
    if messaging:
        _check_messaging(inputs, messaging)
        if messaging != "MESSAGE_PAGE":
            raise ValidationError(
                f"{messaging} on a video is not built: Meta's guides show a click-to-message video example for "
                "Messenger (MESSAGE_PAGE) only. Use a single image for WhatsApp or Instagram Direct."
            )
        call_to_action = {"type": messaging, "value": {"app_destination": MESSAGING_DESTINATIONS[messaging]}}
        if inputs.page_welcome_message:
            video_data["page_welcome_message"] = inputs.page_welcome_message
    elif inputs.lead_gen_form_id:
        cta = _check_lead_form(inputs)
        call_to_action = {"type": cta, "value": {"link": LEAD_FORM_LINK, "lead_gen_form_id": inputs.lead_gen_form_id}}
    elif inputs.link_url:
        call_to_action = {
            "type": inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION,
            "value": {"link": inputs.link_url},
        }
    elif inputs.call_to_action_type:
        raise ValidationError("call_to_action_type on a video needs link_url, a click-to-message type, or lead_gen_form_id.")
    if inputs.description and call_to_action is None:
        raise ValidationError("description on a video becomes link_description, which Meta accepts only with a call to action; add link_url.")
    if call_to_action is not None:
        video_data["call_to_action"] = call_to_action
    if inputs.message:
        video_data["message"] = inputs.message
    if inputs.headline:
        video_data["title"] = inputs.headline
    if inputs.description:
        video_data["link_description"] = inputs.description
    spec = _story_spec(inputs, page_id)
    spec["video_data"] = video_data
    return {"object_story_spec": spec}, []


def _build_carousel(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    page_id = _require_page(inputs, "a carousel")
    _reject(inputs, ["image_hash", "image_url", "video_id", "headline", "description", "lead_gen_form_id"], "A carousel (use per-card fields)")
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS or inputs.page_welcome_message:
        raise ValidationError("Click-to-message carousels are not built; use a single image.")
    if not inputs.link_url:
        raise ValidationError("link_url is required for a carousel: Meta requires the top-level 'See more' link.")
    cards = _check_cards(inputs.cards)
    attachments: list[dict[str, Any]] = []
    for index, card in enumerate(cards, start=1):
        where = f"cards[{index}]"
        media = _media_choice(_text(card.get("image_hash")), _text(card.get("image_url")), where)
        if not media:
            raise ValidationError(f"{where} needs image_hash or image_url; Meta requires an image on every card, including video cards.")
        link = _text(card.get("link")) or inputs.link_url
        _require_url(link, f"{where} link")
        cta = _call_to_action_type(card.get("call_to_action_type"), f"{where} call_to_action_type") or inputs.call_to_action_type
        if cta in MESSAGING_DESTINATIONS:
            raise ValidationError(f"{where}: click-to-message carousels are not built.")
        attachment: dict[str, Any] = {"link": link, **_link_data_image(media)}
        video_id = _text(card.get("video_id"))
        if video_id is not None:
            if not video_id.isdigit():
                raise ValidationError(f"{where} video_id must be a numeric Meta video id.")
            attachment["video_id"] = video_id
        if _text(card.get("headline")):
            attachment["name"] = _text(card.get("headline"))
        if _text(card.get("description")):
            attachment["description"] = _text(card.get("description"))
        attachment["call_to_action"] = {"type": cta or DEFAULT_CALL_TO_ACTION, "value": {"link": link}}
        attachments.append(attachment)
    link_data: dict[str, Any] = {"link": inputs.link_url, "child_attachments": attachments}
    if inputs.message:
        link_data["message"] = inputs.message
    spec = _story_spec(inputs, page_id)
    spec["link_data"] = link_data
    return {"object_story_spec": spec}, []


def _asset_feed_texts(inputs: _Inputs) -> dict[str, Any]:
    feed: dict[str, Any] = {}
    bodies = inputs.primary_texts or ([inputs.message] if inputs.message else None)
    titles = inputs.headlines or ([inputs.headline] if inputs.headline else None)
    descriptions = inputs.descriptions or ([inputs.description] if inputs.description else None)
    if bodies:
        feed["bodies"] = [{"text": text} for text in bodies]
    if titles:
        feed["titles"] = [{"text": text} for text in titles]
    if descriptions:
        feed["descriptions"] = [{"text": text} for text in descriptions]
    return feed


def _asset_feed_link(inputs: _Inputs, format_label: str) -> dict[str, Any]:
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS or inputs.page_welcome_message:
        raise ValidationError(f"Click-to-message is not built for {format_label}; use a single image or video.")
    if inputs.lead_gen_form_id:
        raise ValidationError(f"lead_gen_form_id is not built for {format_label}; use a single image or video.")
    if not inputs.link_url:
        raise ValidationError(f"link_url is required for {format_label}.")
    return {
        "link_urls": [{"website_url": inputs.link_url}],
        "call_to_action_types": [inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION],
    }


def _build_text_variants(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    page_id = _require_page(inputs, "text variants")
    feed: dict[str, Any] = {}
    if inputs.video_id:
        thumbnail: dict[str, str] = {"video_id": inputs.video_id}
        if inputs.image_hash and inputs.image_url:
            raise ValidationError("text variants: give image_hash or image_url as the video thumbnail, not both.")
        if inputs.image_url:
            thumbnail["thumbnail_url"] = _require_url(inputs.image_url, "image_url")
        elif inputs.image_hash:
            thumbnail["thumbnail_hash"] = inputs.image_hash
        feed["videos"] = [thumbnail]
        ad_format = "SINGLE_VIDEO"
    elif inputs.image_hash:
        if inputs.image_url:
            raise ValidationError("text variants: give image_hash, not image_url, for the image.")
        feed["images"] = [{"hash": inputs.image_hash}]
        ad_format = "SINGLE_IMAGE"
    else:
        raise ValidationError(
            "Text variants need one image (image_hash; upload an image URL first with upload_creative_asset) or one video (video_id)."
        )
    feed.update(_asset_feed_texts(inputs))
    feed.update(_asset_feed_link(inputs, "text variants"))
    feed["ad_formats"] = [ad_format]
    notes = [
        "Built as an asset_feed_spec, as Meta's asset feed options guide shows. Meta chooses among the texts at "
        "delivery. Meta's documentation does not say which ad set settings this needs, so check the ad with "
        "preview_ad and create_ad validate_only before relying on it."
    ]
    return {"object_story_spec": _story_spec(inputs, page_id), "asset_feed_spec": feed}, notes


def _string_list(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{where} must be a nonempty list of names.")
    return [item.strip().lower() for item in value]


def _build_placement_customization(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    page_id = _require_page(inputs, "placement_customizations")
    _reject(inputs, ["image_hash", "image_url", "video_id"], "placement_customizations (put media in each entry)")
    entries = inputs.placement_customizations
    if not isinstance(entries, list) or not MIN_PLACEMENT_ASSETS <= len(entries) <= MAX_PLACEMENT_ASSETS:
        raise ValidationError(f"placement_customizations must list {MIN_PLACEMENT_ASSETS} to {MAX_PLACEMENT_ASSETS} entries.")
    allowed_keys = {"image_hash", "video_id", "publisher_platforms", *PLACEMENT_POSITION_FIELDS.values()}
    media_kind: str | None = None
    assets: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    seen_media: set[str] = set()
    claimed: dict[tuple[str, str], int] = {}
    fallback_index: int | None = None
    for index, entry in enumerate(entries, start=1):
        where = f"placement_customizations[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{where} must be an object.")
        unknown = sorted(set(entry) - allowed_keys)
        if unknown:
            raise ValidationError(f"{where} has unknown keys: {', '.join(unknown)}. image_url is not accepted here; upload it first with upload_creative_asset.")
        image_hash, video_id = _text(entry.get("image_hash")), _text(entry.get("video_id"))
        if bool(image_hash) == bool(video_id):
            raise ValidationError(f"{where} needs exactly one of image_hash or video_id.")
        kind = "image" if image_hash else "video"
        if media_kind is None:
            media_kind = kind
        elif media_kind != kind:
            raise ValidationError("placement_customizations must be all images or all videos; Meta allows one ad format per asset feed.")
        media = image_hash or video_id
        if media in seen_media:
            raise ValidationError(f"{where} repeats media {media}; give each placement group a different asset.")
        seen_media.add(media)
        label = f"placement_{index}"
        if kind == "image":
            assets.append({"hash": image_hash, "adlabels": [{"name": label}]})
        else:
            if not video_id.isdigit():
                raise ValidationError(f"{where} video_id must be a numeric Meta video id.")
            assets.append({"video_id": video_id, "adlabels": [{"name": label}]})
        platforms_raw = entry.get("publisher_platforms")
        position_keys = [key for key in PLACEMENT_POSITION_FIELDS.values() if entry.get(key) is not None]
        if platforms_raw is None:
            if position_keys:
                raise ValidationError(f"{where} sets {', '.join(position_keys)} without publisher_platforms.")
            if fallback_index is not None:
                raise ValidationError(
                    f"Only one entry may omit publisher_platforms (the fallback); entries {fallback_index} and {index} both do."
                )
            fallback_index = index
            customization_spec: dict[str, Any] = {}
        else:
            platforms = _string_list(platforms_raw, f"{where} publisher_platforms")
            unknown_platforms = sorted(set(platforms) - set(PLACEMENT_POSITION_FIELDS))
            if unknown_platforms:
                raise ValidationError(
                    f"{where} publisher_platforms {', '.join(unknown_platforms)} not supported; use {', '.join(PLACEMENT_POSITION_FIELDS)}."
                )
            customization_spec = {"publisher_platforms": platforms}
            for key in position_keys:
                platform = next(name for name, field in PLACEMENT_POSITION_FIELDS.items() if field == key)
                if platform not in platforms:
                    raise ValidationError(f"{where} sets {key} but publisher_platforms does not include {platform}.")
            for platform in platforms:
                key = PLACEMENT_POSITION_FIELDS[platform]
                if entry.get(key) is None:
                    raise ValidationError(f"{where} needs {key}: Meta requires the positions for each platform in publisher_platforms.")
                positions = _string_list(entry[key], f"{where} {key}")
                customization_spec[key] = positions
                for position in positions:
                    owner = claimed.get((platform, position))
                    if owner is not None:
                        raise ValidationError(
                            f"{platform} {position} is claimed by entries {owner} and {index}; placements must not overlap."
                        )
                    claimed[(platform, position)] = index
        label_key = "image_label" if kind == "image" else "video_label"
        rules.append({"customization_spec": customization_spec, label_key: {"name": label}})
    feed: dict[str, Any] = {("images" if media_kind == "image" else "videos"): assets}
    feed.update(_asset_feed_texts(inputs))
    feed.update(_asset_feed_link(inputs, "placement_customizations"))
    feed["ad_formats"] = ["SINGLE_IMAGE" if media_kind == "image" else "SINGLE_VIDEO"]
    feed["asset_customization_rules"] = rules
    feed["optimization_type"] = "PLACEMENT"
    notes = [
        "Built as asset_customization_rules with optimization_type PLACEMENT, as Meta's placement asset "
        "customization guide shows. Placements not covered by any rule are not described by Meta's guide."
    ]
    if fallback_index is not None:
        notes.append(
            f"Entry {fallback_index} has no publisher_platforms and is sent with an empty customization_spec, as in "
            "Meta's Threads example; Meta's guide does not state how such a rule is used."
        )
    return {"object_story_spec": _story_spec(inputs, page_id), "asset_feed_spec": feed}, notes


def _build_page_post(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    _reject(inputs, [*_CONTENT_INPUTS, "link_url", "call_to_action_type", "sponsor_page_id", "sponsor_instagram_account_id", "partnership_ad_format"], "An existing Page post (object_story_id) uses the post as it is and")
    story_id = inputs.object_story_id
    if not _OBJECT_STORY_ID.match(story_id):
        raise ValidationError("object_story_id must be {page_id}_{post_id}, digits on both sides.")
    if inputs.page_id and story_id.split("_", 1)[0] != inputs.page_id:
        raise ValidationError("object_story_id belongs to a different Page than page_id.")
    spec: dict[str, Any] = {"object_story_id": story_id}
    if inputs.instagram_account_id:
        spec["instagram_user_id"] = inputs.instagram_account_id
    return spec, []


def _build_instagram_post(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    _reject(inputs, _CONTENT_INPUTS, "An existing Instagram post (source_instagram_media_id) uses the post as it is and")
    page_id = _require_page(inputs, "an existing Instagram post (Meta's object_id)")
    partnership = _partnership(inputs, required=inputs.partnership_ad_format is not None)
    spec: dict[str, Any] = {"object_id": page_id, "source_instagram_media_id": inputs.source_instagram_media_id}
    if partnership:
        spec.update(partnership)
        if inputs.partnership_ad_format is not None:
            spec["branded_content"] = {"ad_format": inputs.partnership_ad_format}
        if inputs.instagram_account_id:
            spec["instagram_user_id"] = inputs.instagram_account_id
    else:
        if not inputs.instagram_account_id:
            raise ValidationError("instagram_account_id is required with source_instagram_media_id: it names the account that owns the post.")
        spec["instagram_user_id"] = inputs.instagram_account_id
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS:
        raise ValidationError("Click-to-message buttons on an existing Instagram post are not built.")
    if inputs.link_url:
        spec["call_to_action"] = {
            "type": inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION,
            "value": {"link": inputs.link_url},
        }
    elif inputs.call_to_action_type:
        raise ValidationError("call_to_action_type on an existing Instagram post needs link_url.")
    return spec, []


def _build_partnership_ad_code(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    _reject(inputs, [*_CONTENT_INPUTS, "link_url", "call_to_action_type"], "A Partnership Ad from an ad code uses the creator's post as it is and")
    page_id = _require_page(inputs, "a Partnership Ad from an ad code (the brand's Page, Meta's object_id)")
    partnership = _partnership(inputs, required=True)
    branded: dict[str, Any] = {"instagram_boost_post_access_token": inputs.partnership_ad_code}
    if inputs.partnership_ad_format is not None:
        branded["ad_format"] = inputs.partnership_ad_format
    spec: dict[str, Any] = {"object_id": page_id, "branded_content": branded, **partnership}
    return spec, ["The ad code is sent to Meta and is not echoed in this response."]


_BUILDERS = {
    "single_image": _build_single_image,
    "single_video": _build_single_video,
    "carousel": _build_carousel,
    "text_variants": _build_text_variants,
    "placement_customization": _build_placement_customization,
    "page_post": _build_page_post,
    "instagram_post": _build_instagram_post,
    "partnership_ad_code": _build_partnership_ad_code,
}


def build_creative_spec(inputs: _Inputs) -> tuple[str, dict[str, Any], list[str]]:
    """Return (format, Graph payload without name, notes) or raise ValidationError."""
    creative_format = _detect_format(inputs)
    if creative_format not in {"instagram_post", "partnership_ad_code"}:
        if inputs.sponsor_page_id or inputs.sponsor_instagram_account_id or inputs.partnership_ad_format is not None:
            raise ValidationError(
                "Partnership Ad inputs are built only with source_instagram_media_id or partnership_ad_code, as Meta's "
                "Partnership Ads guide documents. Partnership Ads from new media are not built."
            )
    if inputs.lead_gen_form_id and creative_format not in {"single_image", "single_video"}:
        raise ValidationError("lead_gen_form_id is built for a single image or single video creative only.")
    payload, notes = _BUILDERS[creative_format](inputs)
    if inputs.url_tags:
        payload["url_tags"] = inputs.url_tags
    return creative_format, payload, notes


def _redacted(payload: dict[str, Any]) -> dict[str, Any]:
    """Hide the partnership ad code in anything returned to the caller."""
    shown = deepcopy(payload)
    branded = shown.get("branded_content")
    if isinstance(branded, dict) and "instagram_boost_post_access_token" in branded:
        branded["instagram_boost_post_access_token"] = "<partnership_ad_code>"
    return shown


@mcp_server.tool()
async def create_ad_creative_from_inputs(
    account_id: AccountId,
    name: str,
    page_id: GraphId | None = None,
    instagram_account_id: GraphId | None = None,
    link_url: str | None = None,
    call_to_action_type: str | None = None,
    url_tags: str | None = None,
    image_hash: str | None = None,
    image_url: str | None = None,
    video_id: GraphId | None = None,
    message: str | None = None,
    headline: str | None = None,
    description: str | None = None,
    primary_texts: StringList | None = None,
    headlines: StringList | None = None,
    descriptions: StringList | None = None,
    cards: list[dict[str, Any]] | None = None,
    placement_customizations: list[dict[str, Any]] | None = None,
    object_story_id: str | None = None,
    source_instagram_media_id: GraphId | None = None,
    lead_gen_form_id: GraphId | None = None,
    page_welcome_message: str | None = None,
    sponsor_page_id: GraphId | None = None,
    sponsor_instagram_account_id: GraphId | None = None,
    partnership_ad_code: str | None = None,
    partnership_ad_format: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build a creative from named inputs instead of Meta's raw spec, and reject combinations Meta does not document before sending. Formats: single image (image_hash or image_url), single video (video_id plus a thumbnail), carousel (cards, 2 to 10, each with image and optional video_id, link, headline, description, call_to_action_type), text variants (up to 5 each of primary_texts, headlines, descriptions with one image_hash or video_id; pass them as JSON arrays, since a plain string is split at commas), placement_customizations (2 to 10 entries, each image_hash or video_id with publisher_platforms and positions; at most one entry without platforms), existing Page post (object_story_id), existing Instagram post (source_instagram_media_id), click-to-message (call_to_action_type WHATSAPP_MESSAGE, MESSAGE_PAGE or INSTAGRAM_MESSAGE), instant form (lead_gen_form_id) and Partnership Ads (sponsor ids on an Instagram post, or partnership_ad_code). dry_run returns the assembled spec and sends nothing. Attach the creative with create_ad or update_ad_creative."""
    for label, value in (
        ("link_url", link_url), ("image_url", image_url), ("cards", cards),
        ("placement_customizations", placement_customizations),
    ):
        _refuse_logins(value, label)
    account = blank_to_none(account_id)
    creative_name = blank_to_none(name)
    if account is None:
        raise ValidationError("account_id is required.")
    if creative_name is None:
        raise ValidationError("name is required.")
    if len(creative_name) > 100:
        raise ValidationError("name must be at most 100 characters.")
    if partnership_ad_format is not None and partnership_ad_format not in PARTNERSHIP_AD_FORMATS:
        raise ValidationError("partnership_ad_format must be 1 (both identities), 2 (first identity only) or 3 (automatic).")
    _single(_text(message), primary_texts, "message", "primary_texts")
    _single(_text(headline), headlines, "headline", "headlines")
    _single(_text(description), descriptions, "description", "descriptions")
    resolved_link = _text(link_url)
    if resolved_link:
        _require_url(resolved_link, "link_url")
    for label, value in (("message", message), ("headline", headline), ("description", description)):
        limit = MAX_BODY_CHARS if label == "message" else MAX_TITLE_CHARS
        if _text(value) and len(_text(value)) > limit:
            raise ValidationError(f"{label} must be at most {limit} characters.")
    inputs = _Inputs(
        page_id=_text(page_id),
        instagram_account_id=_text(instagram_account_id),
        link_url=resolved_link,
        call_to_action_type=_call_to_action_type(call_to_action_type),
        url_tags=_text(url_tags),
        image_hash=_text(image_hash),
        image_url=_text(image_url),
        video_id=_text(video_id),
        message=_text(message),
        headline=_text(headline),
        description=_text(description),
        primary_texts=_variants(primary_texts, "primary_texts", MAX_BODY_CHARS),
        headlines=_variants(headlines, "headlines", MAX_TITLE_CHARS),
        descriptions=_variants(descriptions, "descriptions", MAX_TITLE_CHARS),
        cards=cards,
        placement_customizations=placement_customizations,
        object_story_id=_text(object_story_id),
        source_instagram_media_id=_text(source_instagram_media_id),
        lead_gen_form_id=_text(lead_gen_form_id),
        page_welcome_message=_text(page_welcome_message),
        sponsor_page_id=_text(sponsor_page_id),
        sponsor_instagram_account_id=_text(sponsor_instagram_account_id),
        partnership_ad_code=_text(partnership_ad_code),
        partnership_ad_format=partnership_ad_format,
    )
    creative_format, spec, notes = build_creative_spec(inputs)
    payload = {"name": creative_name, **spec}
    resolved_account = normalize_account_id(account)
    target = {"account_id": resolved_account}
    shown = _redacted(payload)
    if dry_run:
        return {
            "ok": True,
            "action": "create_ad_creative_from_inputs",
            "dry_run": True,
            "target": target,
            "format": creative_format,
            "would_send": {"method": "POST", "path": f"{resolved_account}/adcreatives", "data": shown},
            "notes": notes,
            "sent": False,
        }
    created = await get_graph_api_client().create_edge_object(resolved_account, "adcreatives", data=payload)
    response = creation_response(action="create_ad_creative_from_inputs", target=target, result=created)
    response.update({
        "creative_id": str(created.get("id") or "") or None,
        "format": creative_format,
        "sent_spec": shown,
        "notes": notes,
    })
    return response


# update_ad_creative_enhancements

# Advantage+ creative feature keys from Meta's "Get Started with Advantage+ Creative" page.
ADVANTAGE_CREATIVE_FEATURES = frozenset({
    "adapt_to_placement", "add_text_overlay", "creative_stickers", "description_automation", "enhance_cta",
    "image_animation", "image_background_gen", "image_brightness_and_contrast", "image_templates",
    "image_text_translation", "image_touchups", "image_uncrop", "inline_comment", "media_type_automation",
    "pac_relaxation", "product_extensions", "reveal_details_over_time", "text_optimizations",
    "text_translation", "translate_voiceover", "video_auto_crop", "video_filtering", "video_uncrop",
})
ENROLL_STATUSES = frozenset({"OPT_IN", "OPT_OUT"})

# Creative fields read and copied into the replacement creative.
COPYABLE_CREATIVE_FIELDS = [
    "object_story_spec",
    "asset_feed_spec",
    "object_story_id",
    "source_instagram_media_id",
    "instagram_user_id",
    "object_id",
    "url_tags",
    "call_to_action",
    "facebook_branded_content",
    "instagram_branded_content",
    "product_set_id",
]
ENHANCEMENT_READ_FIELDS = ["id", "name", "account_id", *COPYABLE_CREATIVE_FIELDS, "degrees_of_freedom_spec"]


def _raise_step_failure(exc: Exception, *, definite: str, unknown: str) -> None:
    """Raise the error for a failed write step, stating what is known about its outcome.

    Must be called inside the ``except`` block that caught ``exc``.
    """
    error = _step_failure(exc, definite=definite, unknown=unknown)
    if error is exc:
        raise exc
    raise error from exc


def _step_failure(exc: Exception, *, definite: str, unknown: str) -> Exception:
    """Return the error to raise when a write step fails, stating what is known about its outcome.

    A Meta error response or a rate limit means the request was refused. A lost
    response (mutation_outcome_unknown) or any error that is not one of this
    server's own errors (a transport failure, for example) leaves the outcome
    unknown. Meta error types keep their type; anything else becomes a
    MetaApiError so the message reaches the caller.
    """
    outcome_unknown = bool(getattr(exc, "mutation_outcome_unknown", False)) or not isinstance(exc, MetaAdsError)
    reason = getattr(exc, "message", None) if isinstance(exc, (MetaApiError, RateLimitError)) else None
    reason = reason or str(exc) or type(exc).__name__
    message = f"{unknown if outcome_unknown else definite}. Error: {reason}"
    if isinstance(exc, (MetaApiError, RateLimitError)):
        exc.message = message
        if isinstance(exc, MetaApiError) and outcome_unknown:
            exc.mutation_outcome_unknown = True
        return exc
    return MetaApiError(message=message, mutation_outcome_unknown=outcome_unknown)


def _features_of(spec: Any) -> dict[str, str]:
    """Read enroll_status per feature from a degrees_of_freedom_spec."""
    features: dict[str, str] = {}
    if isinstance(spec, dict) and isinstance(spec.get("creative_features_spec"), dict):
        for key, value in spec["creative_features_spec"].items():
            if isinstance(value, dict) and isinstance(value.get("enroll_status"), str):
                features[key] = value["enroll_status"]
    return features


@mcp_server.tool()
async def update_ad_creative_enhancements(
    ad_id: GraphId,
    features: dict[str, str],
    name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Turn Advantage creative enhancements on or off for a live ad. Meta does not edit a published creative, so this reads the ad's creative, creates a copy with features changed (for example {"image_touchups": "OPT_OUT", "text_optimizations": "OPT_IN"}), and attaches the copy. The ad is expected to go back to review because its creative changed. dry_run reads the ad and creative and returns the new creative and ad update without sending them."""
    ad_id = blank_to_none(ad_id)
    if ad_id is None:
        raise ValidationError("ad_id is required.")
    if not isinstance(features, dict) or not features:
        raise ValidationError("features must map at least one enhancement name to OPT_IN or OPT_OUT.")
    requested: dict[str, str] = {}
    for key, value in features.items():
        feature = str(key).strip().lower()
        status = str(value).strip().upper() if isinstance(value, str) else ""
        if feature not in ADVANTAGE_CREATIVE_FEATURES:
            raise ValidationError(f"{feature} is not one of Meta's Advantage creative features: {', '.join(sorted(ADVANTAGE_CREATIVE_FEATURES))}.")
        if status not in ENROLL_STATUSES:
            raise ValidationError(f"{feature} must be OPT_IN or OPT_OUT.")
        requested[feature] = status
    new_name = blank_to_none(name)
    if new_name is not None and len(new_name) > 100:
        raise ValidationError("name must be at most 100 characters.")
    client = get_graph_api_client()
    ad = await client.get_object(ad_id, fields=["id", "name", "account_id", "effective_status", "creative{id}"])
    creative_ref = ad.get("creative")
    old_creative_id = str(creative_ref.get("id") or "") if isinstance(creative_ref, dict) else str(creative_ref or "")
    if not old_creative_id:
        raise ValidationError("Meta returned no creative for this ad, so nothing was changed.")
    account_id = blank_to_none(str(ad.get("account_id") or ""))
    creative = await client.get_object(old_creative_id, fields=ENHANCEMENT_READ_FIELDS)
    account_id = account_id or blank_to_none(str(creative.get("account_id") or ""))
    if account_id is None:
        raise ValidationError("Meta did not return the ad's account_id, so nothing was changed.")
    copied = {field: creative[field] for field in COPYABLE_CREATIVE_FIELDS if creative.get(field) not in (None, "", [], {})}
    if "object_story_spec" in copied:
        copied.pop("object_story_id", None)
    if not any(key in copied for key in ("object_story_spec", "object_story_id", "source_instagram_media_id", "asset_feed_spec")):
        raise ValidationError(
            "This creative has no object_story_spec, object_story_id, source_instagram_media_id or asset_feed_spec "
            "that can be copied, so a replacement cannot be built. Nothing was changed."
        )
    old_spec = creative.get("degrees_of_freedom_spec") if isinstance(creative.get("degrees_of_freedom_spec"), dict) else {}
    new_spec = deepcopy(old_spec)
    feature_spec = new_spec.get("creative_features_spec")
    if not isinstance(feature_spec, dict):
        feature_spec = {}
    for feature, status in requested.items():
        entry = dict(feature_spec.get(feature) or {}) if isinstance(feature_spec.get(feature), dict) else {}
        entry["enroll_status"] = status
        feature_spec[feature] = entry
    new_spec["creative_features_spec"] = feature_spec
    base_name = str(creative.get("name") or f"Creative {old_creative_id}")
    payload = {
        "name": new_name or f"{base_name} (enhancements updated)"[:100],
        **copied,
        "degrees_of_freedom_spec": new_spec,
    }
    resolved_account = normalize_account_id(account_id)
    target = {"ad_id": ad_id, "account_id": resolved_account}
    previous = {"creative_id": old_creative_id, "creative_features": _features_of(old_spec)}
    not_copied = sorted(
        key for key, value in creative.items()
        if key not in {"id", "name", "account_id", "degrees_of_freedom_spec", *copied} and value not in (None, "", [], {})
    )
    review_note = (
        "The ad now uses a new creative, so expect it to go back to review before it delivers with the change. "
        "The old creative is left in the library."
    )
    copy_note = (
        f"Copied from the old creative: {', '.join(sorted(copied))}. Meta returns some read-only values inside these "
        "specs; if Meta rejects the copy, nothing is attached."
    )
    if dry_run:
        return {
            "ok": True,
            "action": "update_ad_creative_enhancements",
            "dry_run": True,
            "sent": False,
            "target": target,
            "previous": previous,
            "would_send": [
                {"method": "POST", "path": f"{resolved_account}/adcreatives", "data": payload},
                {"method": "POST", "path": ad_id, "data": {"creative": {"creative_id": "<new creative id>"}}},
            ],
            "proposed_features": _features_of(new_spec),
            "fields_not_copied": not_copied,
            "notes": [copy_note, review_note],
        }
    try:
        created = await client.create_edge_object(resolved_account, "adcreatives", data=payload)
    except Exception as exc:
        _raise_step_failure(
            exc,
            definite=(
                f"No replacement creative was created and ad {ad_id} still uses creative {old_creative_id}"
            ),
            unknown=(
                f"Meta may have created a replacement creative named {payload['name']!r} before the connection "
                f"failed. Nothing was attached, so ad {ad_id} still uses creative {old_creative_id}. Look for that "
                "creative in the account before retrying, so a second copy is not made"
            ),
        )
    new_creative_id = str(created.get("id") or "")
    if not new_creative_id:
        raise ValidationError("Meta did not return a creative id, so the ad was not changed.")
    try:
        await client.update_object(ad_id, data={"creative": {"creative_id": new_creative_id}})
    except Exception as exc:
        _raise_step_failure(
            exc,
            definite=(
                f"Creative {new_creative_id} was created but ad {ad_id} still uses creative {old_creative_id}, "
                "because Meta refused the attach. Delete the new creative with delete_creative or attach it with "
                "update_ad_creative"
            ),
            unknown=(
                f"Creative {new_creative_id} was created, but whether ad {ad_id} now uses it is unknown: the attach "
                f"request got no clear answer. The ad's current creative is unknown (it was {old_creative_id}). Read "
                "the ad with get_ad before retrying, and before deleting either creative, since the ad may be "
                "using either one"
            ),
        )
    response = mutation_response(
        action="update_ad_creative_enhancements",
        target=target,
        previous=previous,
        current={"creative_id": new_creative_id, "creative_features": _features_of(new_spec)},
    )
    response.update({"fields_not_copied": not_copied, "notes": [copy_note, review_note]})
    return response
