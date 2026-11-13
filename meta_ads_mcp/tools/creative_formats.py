"""Creative builders that assemble Meta's raw creative spec from named inputs.

Every format here follows a request shape shown in Meta's public Marketing API
documentation (ad creative reference, carousel guide, asset feed spec and
placement asset customization guides, lead ads guide, click-to-Messenger,
click-to-WhatsApp and click-to-Instagram guides, Instagram post and Partnership
Ads guides, the multi-media ads guide, the catalog ads get-started guide and the
product extensions guide). Formats Meta does not document are not built.
"""

from __future__ import annotations

import dataclasses
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

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
# Meta's click-to-Instagram guide: an existing Instagram post's INSTAGRAM_MESSAGE button carries this link.
INSTAGRAM_LINK = "https://www.instagram.com"

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
# A display link must be a URL or a bare domain (Meta: "The caption must be an actual URL").
_DISPLAY_LINK = re.compile(
    r"^(?:https?://)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}(?::[0-9]{1,5})?(?:/\S*)?$"
)

# Keys accepted inside cards, placement_customizations and multi_media entries.
CARD_KEYS = frozenset({"image_hash", "image_url", "video_id", "link", "headline", "description", "call_to_action_type"})
_MULTI_MEDIA_KEYS = frozenset({"image_hash", "image_url", "video_id", "thumbnail_url", "thumbnail_hash"})
_PLACEMENT_KEYS = frozenset({
    "image_hash", "image_url", "video_id", "publisher_platforms", "facebook_positions", "instagram_positions",
    "messenger_positions", "audience_network_positions", "threads_positions",
})
_KNOWN_ENTRY_KEYS = CARD_KEYS | _MULTI_MEDIA_KEYS | _PLACEMENT_KEYS


def _text(value: Any) -> str | None:
    return blank_to_none(value) if isinstance(value, str) else None


def _unknown_keys(entry: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    """Refuse unknown keys by count; a misplaced value can arrive as a key, so keys are never quoted."""
    unknown = [key for key in entry if key not in allowed]
    if unknown:
        count = len(unknown)
        raise ValidationError(
            f"{where} has {count} unknown {'key' if count == 1 else 'keys'} (not repeated here, since a "
            f"misplaced value can arrive as a key). Accepted keys: {', '.join(sorted(allowed))}."
        )


def _refuse_logins(value: Any, name: str) -> None:
    """Refuse a URL with an embedded login anywhere in an input, before any request is built.

    A key is named in the error only when it is one this tool accepts; any other key may be a
    misplaced value, so the position is given instead.
    """
    if isinstance(value, str):
        refuse_url_credentials(value, name)
    elif isinstance(value, dict):
        for index, (key, item) in enumerate(value.items(), start=1):
            _refuse_logins(item, f"{name}.{key}" if key in _KNOWN_ENTRY_KEYS else f"{name} field {index}")
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
        raise ValidationError(
            f"{name} is not one of Meta's call-to-action types; use an UPPER_CASE value such as LEARN_MORE, "
            "SHOP_NOW, SIGN_UP, WHATSAPP_MESSAGE or MESSAGE_PAGE."
        )
    return normalized


def _display_link(value: Any) -> str | None:
    """display_link, sent as Meta's caption: the URL or domain shown on the ad, not the click destination."""
    text = _text(value)
    if text is None:
        return None
    refuse_url_credentials(text, "display_link")
    if not _DISPLAY_LINK.match(text):
        raise ValidationError(
            "display_link must be a URL or a domain such as example.com: Meta requires the caption to be an "
            "actual URL that matches where the ad leads."
        )
    return text


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
    for index, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            raise ValidationError(f"cards[{index}] must be an object.")
        _unknown_keys(card, CARD_KEYS, f"cards[{index}]")
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
        "facebook_partnership_ad_code": inputs.facebook_partnership_ad_code,
        "cards": inputs.cards,
        "placement_customizations": inputs.placement_customizations,
        "multi_media": inputs.multi_media,
        "product_set_id": inputs.product_set_id,
    }
    chosen = [name for name, value in selectors.items() if value is not None]
    if len(chosen) > 1:
        raise ValidationError(f"Use only one of {', '.join(chosen)}; each selects a different creative format.")
    has_variants = any(value is not None for value in (inputs.primary_texts, inputs.headlines, inputs.descriptions))
    if chosen:
        if has_variants and chosen[0] != "multi_media":
            raise ValidationError(
                "primary_texts, headlines and descriptions apply to a single image, a single video or multi_media only; "
                f"they cannot be combined with {chosen[0]}."
            )
        if chosen[0] == "object_story_id" and _has_partnership(inputs):
            return "facebook_partnership_post"
        return {
            "object_story_id": "page_post",
            "source_instagram_media_id": "instagram_post",
            "partnership_ad_code": "partnership_ad_code",
            "facebook_partnership_ad_code": "facebook_partnership_ad_code",
            "cards": "carousel",
            "placement_customizations": "placement_customization",
            "multi_media": "multi_media",
            "product_set_id": "catalog_carousel",
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
        "placement_customizations, multi_media (asset pool), product_set_id (catalog carousel), object_story_id "
        "(existing Page post), source_instagram_media_id (existing Instagram post), partnership_ad_code or "
        "facebook_partnership_ad_code."
    )


def _has_partnership(inputs: _Inputs) -> bool:
    return bool(inputs.sponsor_page_id or inputs.sponsor_instagram_account_id or inputs.partnership_ad_format is not None)


def _reject(inputs: _Inputs, names: list[str], format_label: str) -> None:
    given = [name for name in names if getattr(inputs, name) not in (None, [], "")]
    if given:
        raise ValidationError(f"{format_label} does not accept {', '.join(given)}.")


_CONTENT_INPUTS = [
    "image_hash", "image_url", "video_id", "message", "headline", "description", "primary_texts",
    "headlines", "descriptions", "cards", "placement_customizations", "lead_gen_form_id", "page_welcome_message",
    "multi_media", "product_set_id", "display_link", "product_extensions_product_set_id",
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
            f"An instant form accepts call_to_action_type {', '.join(sorted(LEAD_FORM_CALL_TO_ACTIONS))} only."
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
    if inputs.display_link:
        link_data["caption"] = inputs.display_link
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
        if inputs.display_link:
            # Meta's call-to-action value reference: link_caption is the caption shown in the attachment.
            call_to_action["value"]["link_caption"] = inputs.display_link
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
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS:
        return _build_messaging_carousel(inputs, page_id)
    if inputs.page_welcome_message:
        raise ValidationError(
            "page_welcome_message on a carousel needs call_to_action_type INSTAGRAM_MESSAGE, the click-to-message "
            "carousel Meta's guides show."
        )
    if not inputs.link_url:
        raise ValidationError("link_url is required for a carousel: Meta requires the top-level 'See more' link.")
    cards = _check_cards(inputs.cards)
    attachments: list[dict[str, Any]] = []
    for index, card in enumerate(cards, start=1):
        where = f"cards[{index}]"
        media = _card_media(card, where)
        link = _text(card.get("link")) or inputs.link_url
        _require_url(link, f"{where} link")
        cta = _call_to_action_type(card.get("call_to_action_type"), f"{where} call_to_action_type") or inputs.call_to_action_type
        if cta in MESSAGING_DESTINATIONS:
            raise ValidationError(
                f"{where} has a click-to-message call_to_action_type but the carousel does not: a click-to-message "
                "carousel sets the same messaging call_to_action_type at the top level and on every card."
            )
        attachment: dict[str, Any] = {"link": link, **media}
        _card_texts(card, attachment)
        attachment["call_to_action"] = {"type": cta or DEFAULT_CALL_TO_ACTION, "value": {"link": link}}
        attachments.append(attachment)
    link_data: dict[str, Any] = {"link": inputs.link_url, "child_attachments": attachments}
    if inputs.message:
        link_data["message"] = inputs.message
    if inputs.display_link:
        link_data["caption"] = inputs.display_link
    spec = _story_spec(inputs, page_id)
    spec["link_data"] = link_data
    return {"object_story_spec": spec}, []


def _card_media(card: dict[str, Any], where: str) -> dict[str, str]:
    """A card's image (image_hash, or picture for a URL) and optional video_id."""
    media = _media_choice(_text(card.get("image_hash")), _text(card.get("image_url")), where)
    if not media:
        raise ValidationError(f"{where} needs image_hash or image_url; Meta requires an image on every card, including video cards.")
    shown = _link_data_image(media)
    video_id = _text(card.get("video_id"))
    if video_id is not None:
        if not video_id.isdigit():
            raise ValidationError(f"{where} video_id must be a numeric Meta video id.")
        shown["video_id"] = video_id
    return shown


def _card_texts(card: dict[str, Any], attachment: dict[str, Any]) -> None:
    if _text(card.get("headline")):
        attachment["name"] = _text(card.get("headline"))
    if _text(card.get("description")):
        attachment["description"] = _text(card.get("description"))


def _build_messaging_carousel(inputs: _Inputs, page_id: str) -> tuple[dict[str, Any], list[str]]:
    """A click-to-Instagram carousel, as Meta's click-to-Instagram guide shows: every card opens the same chat."""
    cta = inputs.call_to_action_type
    if cta != "INSTAGRAM_MESSAGE":
        raise ValidationError(
            f"A {cta} carousel is not built: Meta's click-to-message guides show a carousel for Instagram Direct "
            "(INSTAGRAM_MESSAGE) only. Use a single image or video for WhatsApp or Messenger."
        )
    _check_messaging(inputs, cta)
    if inputs.display_link:
        raise ValidationError("display_link is not used on a click-to-message carousel: the cards open a chat, not a website.")
    cards = _check_cards(inputs.cards)
    button = {"type": cta, "value": {"app_destination": MESSAGING_DESTINATIONS[cta]}}
    attachments: list[dict[str, Any]] = []
    for index, card in enumerate(cards, start=1):
        where = f"cards[{index}]"
        if card.get("link") is not None:
            raise ValidationError(f"{where} link is not used: on a click-to-Instagram carousel every card opens the chat.")
        card_cta = _call_to_action_type(card.get("call_to_action_type"), f"{where} call_to_action_type")
        if card_cta not in (None, cta):
            raise ValidationError(
                f"{where} call_to_action_type must be {cta} or left out: on a click-to-message carousel every card "
                "opens the same messaging surface."
            )
        attachment: dict[str, Any] = _card_media(card, where)
        _card_texts(card, attachment)
        attachment["call_to_action"] = deepcopy(button)
        attachments.append(attachment)
    link_data: dict[str, Any] = {"call_to_action": deepcopy(button), "child_attachments": attachments}
    if inputs.message:
        link_data["message"] = inputs.message
    if inputs.page_welcome_message:
        link_data["page_welcome_message"] = inputs.page_welcome_message
    spec = _story_spec(inputs, page_id)
    spec["link_data"] = link_data
    notes = [
        "Built as Meta's click-to-Instagram carousel example shows: no link at the top or on the cards, and every "
        "card's button opens a chat with instagram_account_id. Pair it with an ad set whose destination_type is "
        "INSTAGRAM_DIRECT."
    ]
    return {"object_story_spec": spec}, notes


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
    link: dict[str, str] = {"website_url": inputs.link_url}
    if inputs.display_link:
        # Meta's asset feed link URL reference: display_url is the rendered URL shown on the ad.
        link["display_url"] = inputs.display_link
    return {
        "link_urls": [link],
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
    elif inputs.image_hash or inputs.image_url:
        # Meta's AdAssetFeedSpecImage reference documents both hash and url.
        media = _media_choice(inputs.image_hash, inputs.image_url, "text variants")
        feed["images"] = [{"url": media["image_url"]} if "image_url" in media else {"hash": media["image_hash"]}]
        ad_format = "SINGLE_IMAGE"
    else:
        raise ValidationError("Text variants need one image (image_hash or image_url) or one video (video_id).")
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
    allowed_keys = _PLACEMENT_KEYS
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
        _unknown_keys(entry, allowed_keys, where)
        image_hash, image_url, video_id = (_text(entry.get(key)) for key in ("image_hash", "image_url", "video_id"))
        if sum(1 for value in (image_hash, image_url, video_id) if value) != 1:
            raise ValidationError(f"{where} needs exactly one of image_hash, image_url or video_id.")
        if image_url:
            _require_url(image_url, f"{where} image_url")
        kind = "video" if video_id else "image"
        if media_kind is None:
            media_kind = kind
        elif media_kind != kind:
            raise ValidationError(
                "placement_customizations must be all images or all videos: Meta's placement customization guide "
                "documents one ad format per asset feed. For a pool that mixes images and videos, use multi_media."
            )
        media = image_hash or image_url or video_id
        if media in seen_media:
            raise ValidationError(f"{where} repeats the media of an earlier entry; give each placement group a different asset.")
        seen_media.add(media)
        label = f"placement_{index}"
        if kind == "image":
            image_ref = {"url": image_url} if image_url else {"hash": image_hash}
            assets.append({**image_ref, "adlabels": [{"name": label}]})
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
            unknown_platforms = [platform for platform in platforms if platform not in PLACEMENT_POSITION_FIELDS]
            if unknown_platforms:
                count = len(unknown_platforms)
                raise ValidationError(
                    f"{where} publisher_platforms has {count} unsupported {'value' if count == 1 else 'values'}; "
                    f"use {', '.join(PLACEMENT_POSITION_FIELDS)}."
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
                            f"A {platform} position is claimed by entries {owner} and {index}; placements must not overlap."
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
        return spec, _instagram_post_messaging(inputs, spec, partnership)
    if inputs.link_url:
        spec["call_to_action"] = {
            "type": inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION,
            "value": {"link": inputs.link_url},
        }
    elif inputs.call_to_action_type:
        raise ValidationError("call_to_action_type on an existing Instagram post needs link_url.")
    return spec, []


def _instagram_post_messaging(inputs: _Inputs, spec: dict[str, Any], partnership: dict[str, Any]) -> list[str]:
    """A click-to-message button on an existing Instagram post, as Meta's click-to-WhatsApp and
    click-to-Instagram guides show (a top-level call_to_action beside source_instagram_media_id)."""
    cta = inputs.call_to_action_type
    if partnership:
        raise ValidationError(
            "Click-to-message buttons on a Partnership Ad from an Instagram post are not built: Meta's guides show "
            "them on the advertiser's own post only."
        )
    if cta == "MESSAGE_PAGE":
        raise ValidationError(
            "MESSAGE_PAGE on an existing Instagram post is not built: Meta's guides show WHATSAPP_MESSAGE and "
            "INSTAGRAM_MESSAGE buttons on an Instagram post, not Messenger."
        )
    _check_messaging(inputs, cta)
    if cta == "WHATSAPP_MESSAGE":
        spec["call_to_action"] = {"type": cta, "value": {"link": WHATSAPP_LINK, "app_destination": "WHATSAPP"}}
    else:
        spec["call_to_action"] = {"type": cta, "value": {"link": INSTAGRAM_LINK}}
    return [
        f"Built as Meta's {'click-to-WhatsApp' if cta == 'WHATSAPP_MESSAGE' else 'click-to-Instagram'} guide shows "
        f"for an existing Instagram post. Pair it with an ad set whose destination_type is {MESSAGING_DESTINATIONS[cta]}."
    ]


def _build_partnership_ad_code(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    _reject(inputs, [*_CONTENT_INPUTS, "link_url", "call_to_action_type"], "A Partnership Ad from an ad code uses the creator's post as it is and")
    page_id = _require_page(inputs, "a Partnership Ad from an ad code (the brand's Page, Meta's object_id)")
    partnership = _partnership(inputs, required=True)
    branded: dict[str, Any] = {"instagram_boost_post_access_token": inputs.partnership_ad_code}
    if inputs.partnership_ad_format is not None:
        branded["ad_format"] = inputs.partnership_ad_format
    spec: dict[str, Any] = {"object_id": page_id, "branded_content": branded, **partnership}
    return spec, ["The ad code is sent to Meta and is not echoed in this response."]


def _facebook_sponsor(inputs: _Inputs, format_label: str) -> dict[str, Any]:
    """Sponsor fields for a Facebook Partnership Ad, as Meta's boost-an-existing-Facebook-post guide shows."""
    if not inputs.sponsor_page_id:
        raise ValidationError(
            f"{format_label} needs sponsor_page_id: Meta's Facebook Partnership Ads request names the partner Page in "
            "facebook_branded_content.sponsor_page_id. sponsor_instagram_account_id may be added beside it."
        )
    return _partnership(inputs, required=True)


def _identity_notes(inputs: _Inputs, format_label: str) -> list[str]:
    """page_id and instagram_account_id are not part of Meta's Facebook Partnership Ads request; say so."""
    unused = [name for name in ("page_id", "instagram_account_id") if getattr(inputs, name)]
    if not unused:
        return []
    return [
        f"{' and '.join(unused)} {'was' if len(unused) == 1 else 'were'} not sent: Meta's documented {format_label} "
        "request has no such field. The post owner and sponsor_page_id are the two identities."
    ]


def _build_facebook_partnership_post(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    label = "Facebook Partnership Ad from an existing post"
    _reject(inputs, [*_CONTENT_INPUTS, "link_url", "call_to_action_type"], f"A {label} (object_story_id with sponsor ids) uses the post as it is and")
    story_id = inputs.object_story_id
    if not _OBJECT_STORY_ID.match(story_id):
        raise ValidationError("object_story_id must be {post_owner_page_id}_{post_id}, digits on both sides.")
    spec: dict[str, Any] = {"object_story_id": story_id, **_facebook_sponsor(inputs, f"A {label}")}
    if inputs.partnership_ad_format is not None:
        spec["branded_content"] = {"ad_format": inputs.partnership_ad_format}
    return spec, _identity_notes(inputs, label)


def _build_facebook_partnership_ad_code(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    label = "Facebook Partnership Ad from an ad code"
    _reject(inputs, _CONTENT_INPUTS, f"A {label} uses the creator's post as it is and")
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS:
        raise ValidationError(f"Click-to-message buttons on a {label} are not built.")
    branded: dict[str, Any] = {"facebook_boost_post_access_token": inputs.facebook_partnership_ad_code}
    if inputs.partnership_ad_format is not None:
        branded["ad_format"] = inputs.partnership_ad_format
    spec: dict[str, Any] = {"branded_content": branded, **_facebook_sponsor(inputs, f"A {label}")}
    if inputs.link_url:
        spec["call_to_action"] = {"type": inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION, "value": {"link": inputs.link_url}}
    elif inputs.call_to_action_type:
        raise ValidationError(f"call_to_action_type on a {label} needs link_url.")
    return spec, ["The ad code is sent to Meta and is not echoed in this response.", *_identity_notes(inputs, label)]


_FACEBOOK_HOSTS = ("facebook.com", "fb.com", "fb.me", "facebook.net")


def _build_catalog_carousel(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    """A carousel template filled from a product set, as Meta's catalog ads get-started guide shows."""
    page_id = _require_page(inputs, "a catalog carousel")
    _reject(inputs, ["image_hash", "image_url", "video_id", "lead_gen_form_id"], "A catalog carousel takes its media from the catalog and")
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS or inputs.page_welcome_message:
        raise ValidationError("Click-to-message catalog carousels are not built.")
    if not inputs.link_url:
        raise ValidationError("link_url is required for a catalog carousel: it is the default landing page and the end card link.")
    host = (urlsplit(inputs.link_url).hostname or "").lower()
    if any(host == name or host.endswith(f".{name}") for name in _FACEBOOK_HOSTS):
        raise ValidationError("A catalog carousel link_url cannot be a Facebook URL; Meta's catalog ads guide says so.")
    # Meta's guide: call_to_action carries the type only, and link and call_to_action take no template tags.
    template: dict[str, Any] = {
        "link": inputs.link_url,
        "call_to_action": {"type": inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION},
    }
    if inputs.message:
        template["message"] = inputs.message
    if inputs.headline:
        template["name"] = inputs.headline
    if inputs.description:
        template["description"] = inputs.description
    if inputs.display_link:
        template["caption"] = inputs.display_link
    spec = _story_spec(inputs, page_id)
    spec["template_data"] = template
    notes = [
        "Built as a template creative with product_set_id, as Meta's catalog ads guide shows. message, headline and "
        "description may use catalog tags such as {{product.name}} and {{product.price}}. Meta's guide says the ad set "
        "needs promoted_object.product_set_id for the same product set."
    ]
    return {"product_set_id": inputs.product_set_id, "object_story_spec": spec}, notes


MIN_POOL_ASSETS = 2
MAX_POOL_ASSETS = 10


def _build_multi_media(inputs: _Inputs) -> tuple[dict[str, Any], list[str]]:
    """An asset pool Meta mixes at delivery, as Meta's multi-media ads guide shows (media_sourcing_spec)."""
    page_id = _require_page(inputs, "multi_media")
    _reject(inputs, ["image_hash", "image_url", "video_id"], "multi_media (put media in each entry)")
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS or inputs.page_welcome_message:
        raise ValidationError("Click-to-message is not built for multi_media; use a single image or video.")
    if not inputs.link_url:
        raise ValidationError("link_url is required for multi_media.")
    entries = inputs.multi_media
    if not isinstance(entries, list) or not MIN_POOL_ASSETS <= len(entries) <= MAX_POOL_ASSETS:
        raise ValidationError(
            f"multi_media must list {MIN_POOL_ASSETS} to {MAX_POOL_ASSETS} images or videos; Meta's limit is "
            f"{MAX_POOL_ASSETS} media per ad."
        )
    images: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    primary: dict[str, Any] = {}
    for index, entry in enumerate(entries, start=1):
        where = f"multi_media[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{where} must be an object.")
        _unknown_keys(entry, _MULTI_MEDIA_KEYS, where)
        image_hash, image_url, video_id, thumb_url, thumb_hash = (
            _text(entry.get(key)) for key in ("image_hash", "image_url", "video_id", "thumbnail_url", "thumbnail_hash")
        )
        if sum(1 for value in (image_hash, image_url, video_id) if value) != 1:
            raise ValidationError(f"{where} needs exactly one of image_hash, image_url or video_id.")
        media = image_hash or image_url or video_id
        media_key = ("video" if video_id else "image", media)
        if media_key in seen:
            raise ValidationError(f"{where} repeats the media of an earlier entry; Meta rejects duplicate images or videos in one pool.")
        seen.add(media_key)
        if video_id is None:
            if thumb_url or thumb_hash:
                raise ValidationError(f"{where}: thumbnail_url and thumbnail_hash apply to a video entry only.")
            if image_url:
                _require_url(image_url, f"{where} image_url")
            images.append({**({"url": image_url} if image_url else {"hash": image_hash}), "source": "multi_media", "opt_in_status": "opt_in"})
            if index == 1:
                primary = {"image_url": image_url} if image_url else {"image_hash": image_hash}
            continue
        if not video_id.isdigit():
            raise ValidationError(f"{where} video_id must be a numeric Meta video id.")
        if thumb_url and thumb_hash:
            raise ValidationError(f"{where}: give thumbnail_url or thumbnail_hash, not both.")
        if thumb_hash and index != 1:
            raise ValidationError(
                f"{where}: Meta's multi-media guide shows thumbnail_url for pool videos; thumbnail_hash is accepted only "
                "on the first entry, which becomes the main video."
            )
        video: dict[str, Any] = {"video_id": video_id, "original_video_id": video_id, "source": "multi_media", "opt_in_status": "opt_in"}
        if thumb_url:
            video.update({"thumbnail_url": _require_url(thumb_url, f"{where} thumbnail_url"), "thumbnail_source": "custom"})
        videos.append(video)
        if index == 1:
            if not (thumb_url or thumb_hash):
                raise ValidationError(
                    f"{where} is the main video, so it needs thumbnail_url or thumbnail_hash, as a single video does."
                )
            primary = {"video_id": video_id, **({"image_url": thumb_url} if thumb_url else {"image_hash": thumb_hash})}
    call_to_action = {"type": inputs.call_to_action_type or DEFAULT_CALL_TO_ACTION, "value": {"link": inputs.link_url}}
    if inputs.display_link and "video_id" in primary:
        call_to_action["value"]["link_caption"] = inputs.display_link
    main_message = (inputs.primary_texts or [inputs.message])[0]
    main_headline = (inputs.headlines or [inputs.headline])[0]
    main_description = (inputs.descriptions or [inputs.description])[0]
    spec = _story_spec(inputs, page_id)
    if "video_id" in primary:
        video_data: dict[str, Any] = {**primary, "call_to_action": call_to_action}
        for key, value in (("message", main_message), ("title", main_headline), ("link_description", main_description)):
            if value:
                video_data[key] = value
        spec["video_data"] = video_data
    else:
        link_data: dict[str, Any] = {"link": inputs.link_url, "call_to_action": call_to_action, **_link_data_image(primary)}
        for key, value in (("message", main_message), ("name", main_headline), ("description", main_description)):
            if value:
                link_data[key] = value
        if inputs.display_link:
            link_data["caption"] = inputs.display_link
        spec["link_data"] = link_data
    sourcing: dict[str, Any] = {}
    if images:
        sourcing["images"] = images
    if videos:
        sourcing["videos"] = videos
    for key, values in (("bodies", inputs.primary_texts), ("titles", inputs.headlines), ("descriptions", inputs.descriptions)):
        if values:
            sourcing[key] = [{"text": text} for text in values]
    notes = [
        "Built as media_sourcing_spec, as Meta's multi-media ads guide shows: the first entry is the main media in "
        "object_story_spec and also appears in the pool, and Meta chooses among the media and texts at delivery.",
    ]
    if "image_url" in primary:
        notes.append(
            "The main image is given by URL (link_data.picture and the pool's url). Meta's guide shows a hash for the "
            "main image; if Meta rejects the pairing, upload the image and use image_hash."
        )
    return {"object_story_spec": spec, "media_sourcing_spec": sourcing}, notes


_BUILDERS = {
    "single_image": _build_single_image,
    "single_video": _build_single_video,
    "carousel": _build_carousel,
    "text_variants": _build_text_variants,
    "placement_customization": _build_placement_customization,
    "page_post": _build_page_post,
    "instagram_post": _build_instagram_post,
    "partnership_ad_code": _build_partnership_ad_code,
    "facebook_partnership_post": _build_facebook_partnership_post,
    "facebook_partnership_ad_code": _build_facebook_partnership_ad_code,
    "catalog_carousel": _build_catalog_carousel,
    "multi_media": _build_multi_media,
}

# Formats that carry their own Partnership Ad inputs, and new-media formats Meta's
# "Instagram Partnership Ads with a New Ad Creative" guide shows (an object_story_spec
# with sponsor ids beside it).
_PARTNERSHIP_POST_FORMATS = {"instagram_post", "partnership_ad_code", "facebook_partnership_post", "facebook_partnership_ad_code"}
_PARTNERSHIP_NEW_MEDIA_FORMATS = {"single_image", "single_video"}


def _check_product_extensions(inputs: _Inputs, creative_format: str, partnership: bool) -> None:
    """Meta's product extensions guide: a single image or video with a website link."""
    if creative_format not in {"single_image", "single_video"}:
        detail = (
            " product_set_id builds a catalog carousel; product_extensions_product_set_id adds products below a "
            "single image or video." if creative_format == "catalog_carousel" else ""
        )
        raise ValidationError(
            "product_extensions_product_set_id applies to a single image or single video only, the formats Meta's "
            f"product extensions guide lists.{detail}"
        )
    if inputs.call_to_action_type in MESSAGING_DESTINATIONS or inputs.lead_gen_form_id or not inputs.link_url:
        raise ValidationError(
            "product_extensions_product_set_id needs link_url and a website call to action: Meta's product "
            "extensions guide shows a link ad, and click-to-message and instant form creatives are not built with it."
        )
    if partnership:
        raise ValidationError(
            "product_extensions_product_set_id is not built for a Partnership Ad: Meta's product extensions guide "
            "does not show the two together."
        )


def build_creative_spec(inputs: _Inputs) -> tuple[str, dict[str, Any], list[str]]:
    """Return (format, Graph payload without name, notes) or raise ValidationError."""
    creative_format = _detect_format(inputs)
    new_media_partnership = creative_format not in _PARTNERSHIP_POST_FORMATS and _has_partnership(inputs)
    if new_media_partnership:
        if creative_format not in _PARTNERSHIP_NEW_MEDIA_FORMATS:
            raise ValidationError(
                "Partnership Ads from new media are built for a single image or single video only, the shape Meta's "
                "partnership guide shows; other formats with sponsor ids are not documented."
            )
        if inputs.partnership_ad_format is not None:
            raise ValidationError(
                "partnership_ad_format is documented for boosting an existing post or ad code, not for a Partnership "
                "Ad from new media."
            )
    if inputs.lead_gen_form_id and creative_format not in {"single_image", "single_video"}:
        raise ValidationError("lead_gen_form_id is built for a single image or single video creative only.")
    if inputs.display_link and not inputs.link_url and creative_format not in _PARTNERSHIP_POST_FORMATS | {"page_post"}:
        raise ValidationError(
            "display_link needs link_url: it is the address shown on the ad, and Meta requires it to match where "
            "the ad leads. Click-to-message and instant form creatives take no display_link."
        )
    if inputs.product_extensions_product_set_id:
        _check_product_extensions(inputs, creative_format, new_media_partnership)
    payload, notes = _BUILDERS[creative_format](inputs)
    if inputs.product_extensions_product_set_id:
        payload["creative_sourcing_spec"] = {"associated_product_set_id": inputs.product_extensions_product_set_id}
        payload["degrees_of_freedom_spec"] = {
            "creative_features_spec": {
                "product_extensions": {"enroll_status": "OPT_IN", "action_metadata": {"type": "MANUAL"}},
            },
        }
        notes = [
            *notes,
            "Product extensions requested as Meta's product extensions guide shows: products from this product set "
            "appear below the image or video. Meta's guide says the campaign needs the Sales or Traffic objective.",
        ]
    if new_media_partnership:
        payload.update(_partnership(inputs, required=True))
        notes = [
            *notes,
            "Partnership Ad from new media: page_id is the primary identity and the sponsor ids are the partner, as "
            "Meta's guide shows. That guide's example also sends a standard_enhancements opt-in, which Meta retired in "
            "Marketing API v22.0, so it is not sent.",
        ]
    if inputs.url_tags:
        payload["url_tags"] = inputs.url_tags
    return creative_format, payload, notes


_AD_CODE_FIELDS = ("instagram_boost_post_access_token", "facebook_boost_post_access_token")


def _redacted(payload: dict[str, Any]) -> dict[str, Any]:
    """Hide the partnership ad code in anything returned to the caller."""
    shown = deepcopy(payload)
    branded = shown.get("branded_content")
    if isinstance(branded, dict):
        for key in _AD_CODE_FIELDS:
            if key in branded:
                branded[key] = _CODE_SHOWN
    return shown


_CODE_SHOWN = "<partnership_ad_code>"


def _without_codes(value: Any, codes: list[str]) -> Any:
    """Copy a value with every Partnership Ad code in its text replaced."""
    if isinstance(value, str):
        for code in codes:
            value = value.replace(code, _CODE_SHOWN)
        return value
    if isinstance(value, dict):
        return {_without_codes(key, codes): _without_codes(item, codes) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_codes(item, codes) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_codes(item, codes) for item in value)
    return value


def _error_attribute_names(exc: BaseException) -> list[str]:
    """The data attributes an error carries: dataclass fields, slots and instance attributes."""
    names: list[str] = [field.name for field in dataclasses.fields(exc)] if dataclasses.is_dataclass(exc) else []
    for cls in type(exc).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        names.extend([slots] if isinstance(slots, str) else list(slots))
    names.extend(getattr(exc, "__dict__", {}))
    return [name for name in dict.fromkeys(names) if not name.startswith("__")]


def _mask_codes_in_error(exc: BaseException, codes: list[str | None]) -> None:
    """Remove Partnership Ad codes from a raised error and from every error behind it, in place.

    Meta's error text may repeat a code it was sent. The error is returned to the caller and a
    traceback log prints it with its __cause__ and __context__ chain, so every error reachable
    that way (and inside an exception group) has its message, arguments, notes and public
    fields cleaned. Each error is visited once, so a cycle ends.
    """
    present = sorted({code for code in codes if isinstance(code, str) and code}, key=len, reverse=True)
    if not present:
        return
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for name in _error_attribute_names(current):
            try:
                value = getattr(current, name)
            except AttributeError:
                continue
            if value is None or isinstance(value, BaseException):
                continue
            cleaned = _without_codes(value, present)
            if cleaned != value:
                try:
                    setattr(current, name, cleaned)
                except (AttributeError, TypeError):
                    pass
        if current.args:
            current.args = tuple(_without_codes(arg, present) for arg in current.args)
        notes = getattr(current, "__notes__", None)
        if isinstance(notes, list):
            current.__notes__ = [_without_codes(note, present) for note in notes]
        pending.extend([current.__cause__, current.__context__])
        pending.extend(getattr(current, "exceptions", None) or ())


def _codes_given(*values: Any) -> list[str]:
    """Each ad code as given and as sent (stripped), so either form is found in an error."""
    codes: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            codes.extend([value, value.strip()])
    return codes


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
    multi_media: list[dict[str, Any]] | None = None,
    product_set_id: GraphId | None = None,
    facebook_partnership_ad_code: str | None = None,
    display_link: str | None = None,
    product_extensions_product_set_id: GraphId | None = None,
) -> dict[str, Any]:
    """Build a creative from named inputs instead of Meta's raw spec, and reject combinations Meta does not document before sending. Formats: single image (image_hash or image_url), single video (video_id plus a thumbnail), carousel (cards, 2 to 10, each with image and optional video_id, link, headline, description, call_to_action_type), text variants (up to 5 each of primary_texts, headlines, descriptions with one image or video; pass them as JSON arrays, since a plain string is split at commas), placement_customizations (2 to 10 entries, each image_hash, image_url or video_id with publisher_platforms and positions; all images or all videos; at most one entry without platforms), multi_media asset pool (2 to 10 entries, images and videos may mix, each image_hash, image_url or video_id with optional thumbnail_url; the first is the main media; Meta mixes them with the text variants), catalog carousel (product_set_id with link_url; texts may use tags like {{product.name}}), existing Page post (object_story_id), existing Instagram post (source_instagram_media_id), click-to-message (call_to_action_type WHATSAPP_MESSAGE, MESSAGE_PAGE or INSTAGRAM_MESSAGE on a single image; MESSAGE_PAGE on a video; INSTAGRAM_MESSAGE on a carousel, every card opening the same chat; WHATSAPP_MESSAGE or INSTAGRAM_MESSAGE on an existing Instagram post), instant form (lead_gen_form_id) and Partnership Ads: sponsor ids on an Instagram post, on a Facebook post (object_story_id, sponsor_page_id required) or on a new single image or video; or an ad code (partnership_ad_code for Instagram, facebook_partnership_ad_code for Facebook). display_link is the address shown on the ad (Meta's caption) and needs link_url. product_extensions_product_set_id shows products from that product set below a single image or video link ad. dry_run returns the assembled spec and sends nothing. Attach the creative with create_ad or update_ad_creative."""
    arguments = dict(locals())
    try:
        return await _create_from_inputs(**arguments)
    except BaseException as exc:
        # The ad code is masked in results; an error can repeat it, so it is removed there too.
        _mask_codes_in_error(exc, _codes_given(partnership_ad_code, facebook_partnership_ad_code))
        raise


async def _create_from_inputs(
    *,
    account_id: str,
    name: str,
    dry_run: bool,
    page_id: str | None,
    instagram_account_id: str | None,
    link_url: str | None,
    call_to_action_type: str | None,
    url_tags: str | None,
    image_hash: str | None,
    image_url: str | None,
    video_id: str | None,
    message: str | None,
    headline: str | None,
    description: str | None,
    primary_texts: list[str] | None,
    headlines: list[str] | None,
    descriptions: list[str] | None,
    cards: list[dict[str, Any]] | None,
    placement_customizations: list[dict[str, Any]] | None,
    object_story_id: str | None,
    source_instagram_media_id: str | None,
    lead_gen_form_id: str | None,
    page_welcome_message: str | None,
    sponsor_page_id: str | None,
    sponsor_instagram_account_id: str | None,
    partnership_ad_code: str | None,
    partnership_ad_format: int | None,
    multi_media: list[dict[str, Any]] | None,
    product_set_id: str | None,
    facebook_partnership_ad_code: str | None,
    display_link: str | None,
    product_extensions_product_set_id: str | None,
) -> dict[str, Any]:
    for label, value in (
        ("link_url", link_url), ("image_url", image_url), ("cards", cards),
        ("placement_customizations", placement_customizations), ("multi_media", multi_media),
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
        multi_media=multi_media,
        product_set_id=_text(product_set_id),
        facebook_partnership_ad_code=_text(facebook_partnership_ad_code),
        display_link=_display_link(display_link),
        product_extensions_product_set_id=_text(product_extensions_product_set_id),
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
    try:
        created = await get_graph_api_client().create_edge_object(resolved_account, "adcreatives", data=payload)
    except Exception as exc:
        _raise_step_failure(
            exc,
            definite="Meta refused the request, so no creative was created",
            unknown=(
                f"Meta may have created the creative named {creative_name!r} before the connection failed. Look for "
                "it in the account's creatives before retrying, so a second copy is not made"
            ),
        )
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

# Creative fields read and copied into the replacement creative: every top-level field
# create_ad_creative_from_inputs can write (all are AdCreative fields in Meta's SDK 26.0.2),
# so a copy of a creative this server built keeps every part of it. media_sourcing_spec holds
# an asset pool, branded_content a Partnership Ad's format and ad code, and
# creative_sourcing_spec a product extensions product set.
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
    "media_sourcing_spec",
    "branded_content",
    "creative_sourcing_spec",
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
    codes: list[str] = []
    try:
        return await _update_enhancements(ad_id, features, name, dry_run, codes)
    except BaseException as exc:
        # A Partnership Ad code read from the old creative is never shown, even when an error repeats it.
        _mask_codes_in_error(exc, codes)
        raise


async def _update_enhancements(
    ad_id: str, features: dict[str, str], name: str | None, dry_run: bool, codes: list[str]
) -> dict[str, Any]:
    ad_id = blank_to_none(ad_id)
    if ad_id is None:
        raise ValidationError("ad_id is required.")
    if not isinstance(features, dict) or not features:
        raise ValidationError("features must map at least one enhancement name to OPT_IN or OPT_OUT.")
    requested: dict[str, str] = {}
    unknown = [key for key in features if str(key).strip().lower() not in ADVANTAGE_CREATIVE_FEATURES]
    if unknown:
        count = len(unknown)
        raise ValidationError(
            f"features has {count} {'name' if count == 1 else 'names'} that {'is' if count == 1 else 'are'} not one of "
            f"Meta's Advantage creative features (not repeated here): {', '.join(sorted(ADVANTAGE_CREATIVE_FEATURES))}."
        )
    for key, value in features.items():
        feature = str(key).strip().lower()
        status = str(value).strip().upper() if isinstance(value, str) else ""
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
    branded = creative.get("branded_content") if isinstance(creative.get("branded_content"), dict) else {}
    codes.extend(_codes_given(*(branded.get(key) for key in _AD_CODE_FIELDS)))
    account_id = account_id or blank_to_none(str(creative.get("account_id") or ""))
    if account_id is None:
        raise ValidationError("Meta did not return the ad's account_id, so nothing was changed.")
    copied = {field: creative[field] for field in COPYABLE_CREATIVE_FIELDS if creative.get(field) not in (None, "", [], {})}
    if "object_story_spec" in copied:
        copied.pop("object_story_id", None)
    has_ad_code = any(isinstance(branded.get(key), str) and branded[key] for key in _AD_CODE_FIELDS)
    if not has_ad_code and not any(
        key in copied for key in ("object_story_spec", "object_story_id", "source_instagram_media_id", "asset_feed_spec")
    ):
        raise ValidationError(
            "This creative has no object_story_spec, object_story_id, source_instagram_media_id, asset_feed_spec "
            "or Partnership Ad code that can be copied, so a replacement cannot be built. Nothing was changed."
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
                {"method": "POST", "path": f"{resolved_account}/adcreatives", "data": _redacted(payload)},
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
