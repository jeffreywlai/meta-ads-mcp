"""Ids that Meta defines as numbers must be digits, checked before a tool runs.

Graph ids for campaigns, ad sets, ads, creatives, audiences, pixels and datasets,
businesses, catalogs, product sets, product feeds, Pages, Instagram accounts and
media, studies, videos, system users and apps are digits. An ad account id is
digits, or act_ followed by digits. A name or a placeholder passed as one of these
ids used to cost a rate-limited request and came back as an unclear error from
Meta; it is now refused before the tool starts, with a message that names the
parameter and never repeats the value (a caller sometimes pastes private text into
the wrong field).

The rule is keyed by parameter name, so every tool that takes, say, a campaign_id
gets the same check. Parameters that carry an id of another form (image hashes,
Page post and comment ids, paging cursors, caller-chosen keys) are listed in
LEFT_ALONE with the reason. tests/test_id_formats.py fails when a tool gains an
id parameter that is in neither list.

Blank values and values that are not text are not judged here: each tool already
treats a blank optional id as omitted, and the framework refuses a value of the
wrong type. Spaces around an id are not judged either, so nothing that worked
before is refused for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.input_compat import canonical_tool_name
from meta_ads_mcp.tool_types import coerce_csv_string_list


@dataclass(frozen=True, slots=True)
class IdFormat:
    """What one class of id looks like: a full-match pattern and a plain description."""

    pattern: str
    description: str

    def matches(self, text: str) -> bool:
        return re.fullmatch(self.pattern, text) is not None

    def schema_pattern(self) -> str:
        """The pattern an input schema carries: this form, or a blank, with spaces allowed around it.

        The check before a tool runs strips the value and skips a blank one, so the schema
        accepts exactly that; otherwise tool search, which validates a call against the
        schema, would drop calls the tool runs.
        """
        return rf"^\s*(?:{_inner(self.pattern)})?\s*$"


def _digits(kind: str) -> IdFormat:
    return IdFormat(r"^[0-9]+$", f"{kind} id: digits only, passed as a string, for example '1234567890'")


ACCOUNT = IdFormat(
    r"^(act_)?[0-9]+$",
    "an ad account id: digits, or act_ followed by digits, for example 'act_1234567890'",
)
ACCOUNT_OR_ME = IdFormat(
    r"^(me|(act_)?[0-9]+)$",
    "an ad account id (digits, or act_ followed by digits, for example 'act_1234567890'), or 'me'",
)
AD_OBJECT = IdFormat(
    r"^(act_)?[0-9]+$",
    "an ad account, campaign, ad set or ad id: digits, or act_ followed by digits for an ad account, "
    "for example '1234567890' or 'act_1234567890'",
)

# Parameter name -> the format of the one id it carries.
SINGLE_IDS: dict[str, IdFormat] = {
    "account_id": ACCOUNT,
    "object_id": AD_OBJECT,
    "campaign_id": _digits("a campaign"),
    "adset_id": _digits("an ad set"),
    "ad_id": _digits("an ad"),
    "creative_id": _digits("a creative"),
    "audience_id": _digits("a custom audience"),
    "origin_audience_id": _digits("a custom audience"),
    "pixel_id": _digits("a pixel or dataset"),
    "dataset_id": _digits("a pixel or dataset"),
    "event_source_id": _digits("a pixel, dataset or app"),
    "business_id": _digits("a business"),
    "catalog_id": _digits("a catalog"),
    "product_set_id": _digits("a product set"),
    "product_extensions_product_set_id": _digits("a product set"),
    "feed_id": _digits("a product feed"),
    "page_id": _digits("a Facebook Page"),
    "sponsor_page_id": _digits("a Facebook Page"),
    "instagram_account_id": _digits("an Instagram account"),
    "sponsor_instagram_account_id": _digits("an Instagram account"),
    "instagram_media_id": _digits("an Instagram media"),
    "source_instagram_media_id": _digits("an Instagram media"),
    "study_id": _digits("a study"),
    "entity_id": _digits("a campaign, ad set or ad"),
    "video_id": _digits("a video"),
    "system_user_id": _digits("a system user"),
    "user_id": _digits("a business user or system user"),
    "app_id": _digits("an app"),
    "partner_id": _digits("a business"),
    "lead_gen_form_id": _digits("a lead form"),
    "rf_prediction_id": _digits("a reach and frequency prediction"),
    "value_rule_set_id": _digits("a value rule set"),
    "topline_id": _digits("a topline"),
}

# Parameter name -> the format of each id in a list, given as a list or as comma-separated text.
LIST_IDS: dict[str, IdFormat] = {
    "campaign_ids": _digits("a campaign"),
    "ad_ids": _digits("an ad"),
    "object_ids": AD_OBJECT,
    "entity_ids": _digits("a campaign, ad set or ad"),
    "video_ids": _digits("a video"),
    "primary_feed_ids": _digits("a product feed"),
    "search_page_ids": _digits("a Facebook Page"),
}

# A tool whose parameter of a shared name takes a different form.
TOOL_OVERRIDES: dict[tuple[str, str], IdFormat] = {
    # get_account_pages lists the Pages of the token's user; "me" is its default.
    ("get_account_pages", "account_id"): ACCOUNT_OR_ME,
}

# Id-carrying parameters that keep their current form, and why.
LEFT_ALONE: dict[str, str] = {
    "image_hash": "an image hash is letters and digits",
    "hashes": "image hashes are letters and digits",
    "comment_id": "a comment id is two runs of digits joined by an underscore",
    "object_story_id": "a Page post id is two runs of digits joined by an underscore",
    "export_id": "names a file this server wrote, not a Meta object",
    "after": "a paging cursor is text Meta chooses",
    "error_after": "a paging cursor is text Meta chooses",
    "targeting_after": "a paging cursor is text Meta chooses",
    "report_run_id": "an async report run id is not shown to be digits in every case",
    "retailer_id": "chosen by the catalog owner, any text",
    "product_id": "a catalog product item id; not in the classes shown to be digits",
    "upload_id": "a product feed upload id; not in the classes shown to be digits",
    "rule_id": "a product feed rule id; not in the classes shown to be digits (update_feed_rule's own type already asks for digits)",
    "interest_ids": "targeting interest ids; not in the classes shown to be digits",
    "cell_ids": "split-test cell ids; not in the classes shown to be digits",
    "owner_id": "the owner of a new study may be more than one kind of object; the forms Meta accepts are not shown",
    "extra_oids": "any object related to a change-history scope, of any kind",
    "uid": "a person's id in change history; it already accepts a number",
    "time_based_ad_rotation_id_blocks": "nested lists whose own type already asks for digits",
}


def id_format(tool_name: str, parameter: str) -> tuple[IdFormat, bool] | None:
    """The format a tool's parameter must have and whether it is a list, or None when it is not checked."""
    override = TOOL_OVERRIDES.get((tool_name, parameter))
    if override is not None:
        return override, False
    if parameter in SINGLE_IDS:
        return SINGLE_IDS[parameter], False
    if parameter in LIST_IDS:
        return LIST_IDS[parameter], True
    return None


def _refusal(label: str, id_format_: IdFormat) -> ValidationError:
    return ValidationError(
        f"{label} must be {id_format_.description}. The value sent is not repeated, because a value "
        "put in the wrong field can hold private text."
    )


def check_id_arguments(tool_name: str, arguments: dict[str, Any] | None) -> None:
    """Refuse an id that is not of its parameter's form; the message names the parameter, never the value."""
    for parameter, value in (arguments or {}).items():
        rule = id_format(tool_name, parameter) if isinstance(parameter, str) else None
        if rule is None:
            continue
        expected, is_list = rule
        if not is_list:
            if isinstance(value, str) and value.strip() and not expected.matches(value.strip()):
                raise _refusal(parameter, expected)
            continue
        items = coerce_csv_string_list(value) if isinstance(value, str) else value
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, str) and item.strip() and not expected.matches(item.strip()):
                raise _refusal(f"{parameter}[{index}]", expected)


def _inner(pattern: str) -> str:
    return pattern.removeprefix("^").removesuffix("$")


def _csv_pattern(pattern: str) -> str:
    """The pattern for comma-separated text of ids of one form, as the check reads it.

    Each piece between commas is an id or blank, with spaces allowed around it: the
    check drops blank pieces and strips the others.
    """
    piece = rf"\s*(?:{_inner(pattern)})?\s*"
    return rf"^{piece}(?:,{piece})*$"


def _add_pattern(schema: dict[str, Any], expected: IdFormat, is_list: bool) -> None:
    branches = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else [schema]
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        if branch.get("type") == "string":
            branch.setdefault("pattern", _csv_pattern(expected.pattern) if is_list else expected.schema_pattern())
        elif branch.get("type") == "array" and isinstance(branch.get("items"), dict):
            if branch["items"].get("type") == "string":
                branch["items"].setdefault("pattern", expected.schema_pattern())
    if is_list:
        schema.setdefault("description", f"A list of ids, each {expected.description}.")
    else:
        schema.setdefault("description", f"{expected.description[0].upper()}{expected.description[1:]}.")


def apply_id_formats(server: Any) -> None:
    """Put each checked id's pattern into the registered tools' input schemas, as clients see them."""
    for tool in server.local_provider._components.values():
        name = getattr(tool, "name", None)
        schema = getattr(tool, "parameters", None)
        if not isinstance(name, str) or not isinstance(schema, dict):
            continue
        for parameter, property_schema in (schema.get("properties") or {}).items():
            rule = id_format(name, parameter)
            if rule is not None and isinstance(property_schema, dict):
                _add_pattern(property_schema, *rule)


class IdFormatMiddleware(Middleware):
    """Refuse a malformed id before the tool starts, for direct and routed calls alike."""

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        arguments = context.message.arguments
        failure: ToolError | None = None
        if isinstance(name, str) and name.strip() and isinstance(arguments, dict):
            try:
                check_id_arguments(canonical_tool_name(name), arguments)
            except ValidationError as exc:
                failure = ToolError(str(exc))
        if failure is not None:
            raise failure
        return await call_next(context)
