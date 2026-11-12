# Parity with Meta's official Ads MCP server, phase two

Date: 2026-11-12. Branch: `feature/official-mcp-parity`. Follows
`2026-11-12-official-mcp-parity-design.md`, whose conventions, write rules,
evidence rule and anti-goals all still apply and are not repeated here.

## Intent

Phase one was specified from Meta's documentation, which names 88 tools with
one-line descriptions and no parameters. On 2026-11-12 Meta's live server
returned its real tool list: 106 tools with full parameter schemas. Compared
against those, this server matches 45 tools fully, matches 35 with gaps, and
lacks 8 that Meta's public API appears to allow. Phase two closes those gaps.

Jeff's goal, in his words: "we want to match any capability we dont currently
have and exceed them where we already do match them."

How that is applied here:

- **Match:** every capability in the gap list below gets a named, documented
  way to do it on this server. A capability reachable only through a free-form
  `params` pass-through does not count as matched.
- **Exceed:** each new write tool can show exactly what it would send before it
  sends anything (`dry_run`), returns before and after values, and is blocked in
  read-only mode. Each new read reports how much it examined and whether that
  was everything.

## What cannot be matched

No public API route is known for these, so nothing is built and the ledger marks
them `unavailable`:

- Benchmarks against similar advertisers (`ads_insights_industry_benchmark`,
  `ads_insights_performance_vs_peers`).
- Business Help Center search (`ads_get_help_article`).
- Partner store integration health (`ads_catalog_list_partner_integrations`).
- Pixel event rules and parameter extractors (eight tools). Meta's own server
  limits these to first-party clients.
- Ads Manager drafts: reading, staging, publishing or discarding unpublished
  changes. The SDK has no draft edge.
- Checkout URL setup and testing on a catalog.
- The AI-content disclosure field on creatives.

If an implementer finds a documented public route for any of these, they report
it; they do not build it in this phase.

## Changing tools that are already on master

Existing artifacts call this server. Jeff's rule (2026-11-12): a change that
breaks existing callers is allowed when it genuinely improves the experience of
using the tool, and artifacts and processes will be reworked around the improved
tool. A break that is not a real improvement is not allowed, and no tool changes
its contract by accident.

In practice, for the 102 tools recorded in
`tests/fixtures/master_tool_contract.json`:

- **Additive first.** Adding an optional parameter or a response field needs no
  declaration. Prefer it when it gives the same result for the user.
- **A breaking change must be declared.** Removing or renaming a tool, removing
  a parameter, making one required, changing a default, tightening validation,
  adding a step, or changing the name or meaning of a returned field: each one
  gets an entry in `tests/fixtures/intentional_contract_changes.json` with
  `why_better` (the benefit to the person using the tool, in plain words) and
  `migration` (what a caller has to change). `tests/test_backward_compat.py`
  fails on any undeclared signature change and on any stale declaration. Changes
  the signature does not show are declared by hand with kind `behaviour`.
- **When a stream may make one.** Only when this document's tables call for it,
  or when the tool's current behaviour is wrong (it does not do what its
  description says) and fixing it changes what callers get. For any other idea,
  the stream does not implement it: it describes the change, the benefit and
  what would break in its report, and the orchestrator takes it to Jeff.
- **Review.** The orchestrator checks every declared change at merge and lists
  them all for Jeff, so artifacts can be reworked from one list.

Tools added in phase one (not on `master`) may be changed freely.

Still on hold from Jeff's earlier decision, until he says otherwise: MCP
annotations on tools, and confirmation steps added to existing tools. A new tool
may offer a plan-only or dry-run mode as an option, never as a required first
call. The one exception is catalog access changes (stream P7), where a separate
read-only preview tool exists because Meta's server has that capability.

Stricter input formats: new tools and new parameters use the shared types
`GraphId` and `AccountId` from `meta_ads_mcp/tool_types.py` for ids Meta
documents as numeric. Tools on `master` keep plain string ids unless a declared
change says otherwise.

## New and extended tools

Names are final. "Extend" means adding optional parameters or response fields to
a tool that exists. Items marked (master) are on `master` and fall under the
rules above.

### Stream P1: creatives, media and assets

Files: `meta_ads_mcp/tools/creatives.py`, `meta_ads_mcp/tools/assets.py`, new
`meta_ads_mcp/tools/creative_formats.py`, and their tests.

| Tool | Change |
|---|---|
| `create_ad_creative_from_inputs` (new) | Builds a creative from named inputs instead of Meta's raw spec, and rejects combinations Meta does not allow before sending. Formats: single image, single video, carousel (`cards`), text variants (up to five each of `primary_texts`, `headlines`, `descriptions`), per-placement media (`placement_customizations`), an existing Page post (`object_story_id`), an existing Instagram post (`source_instagram_media_id`), click-to-message (Messenger, WhatsApp, Instagram Direct), lead form (`lead_gen_form_id`), and Partnership Ads. Common inputs: `page_id`, `instagram_account_id`, `link_url`, `call_to_action_type`, `url_tags`, `image_hash` or `image_url`, `video_id`. `dry_run=true` returns the assembled spec and sends nothing. Build only the formats Meta's public documentation describes; report any that are not documented. `create_ad_creative` (master) is unchanged. |
| `upload_ad_video` (new) | Uploads a video from a URL, or from a local file where Meta's documented upload allows it. Returns the video id and its processing status. `upload_creative_asset` (master) is unchanged. |
| `delete_ad_image` (new) | Deletes an image from the account library by hash, reading it first. |
| `update_ad_creative_enhancements` (new) | Turns Advantage creative enhancements on or off for a live ad: reads the ad's current creative, creates a copy with the enhancement settings changed, attaches it, and says the ad returns to review. `dry_run` supported. |
| `update_creative` (master), extend | Optional `adlabels` and `adlabels_operation` (`set`, `add`, `remove`). |
| `preview_ad` (master), extend | Optional `ad_formats` list to preview several placements in one call. `ad_format` keeps its meaning and default. |
| `list_ad_videos`, extend | `video_ids` lookup, `title_contains`, and processing status in each item. |
| `list_instagram_media`, extend | Filters: date range, media type, and feed, story or reel. |
| `list_audience_adsets`, extend | Also find ad sets that target the audience but have no ads, by scanning ad set targeting for the audience in `custom_audiences` and `excluded_custom_audiences`, with scan coverage reported. |
| `get_field_reference`, extend | Cover metrics as well as settings: name, aliases, whether it can be used to sort or filter and with which operators. Source: the SDK's `AdsInsights` field list and this server's metrics reference. |
| `boost_instagram_post`, extend | Optional one-step mode: when `adset_id` is omitted, create a campaign and ad set from `daily_budget`, `duration_days`, `targeting`, `objective`, with every default stated in the response. `plan_only=true` returns the plan and sends nothing. Partnership Ad inputs where documented. Everything is created paused. |

### Stream P2: campaign structure

Files: `meta_ads_mcp/tools/campaigns.py`, `meta_ads_mcp/tools/ads.py`,
`meta_ads_mcp/tools/execution.py`, and their tests.

| Tool | Change |
|---|---|
| `duplicate_campaign`, `duplicate_adset`, `duplicate_ad` (new) | Copy an object with Meta's copy endpoint. Options: deep copy of children, new name or rename rule, target campaign or ad set, start and end times. Copies are created paused unless the caller says otherwise. Returns the new ids. |
| `update_adset`, extend | `daily_spend_cap`, `lifetime_spend_cap`, `daily_min_spend_target`, `lifetime_min_spend_target` in major currency units, and `status="ARCHIVED"`. These are currently rejected. |
| `update_ad`, extend | `status="ARCHIVED"`. |
| `set_campaign_status`, `set_adset_status`, `set_ad_status` (master), extend | Accept `ARCHIVED` in addition to `ACTIVE` and `PAUSED`. The docstring says archiving cannot be undone from this server. |
| `create_campaign` (master), extend | Optional named parameters for what is reachable only through `params` today: `spend_cap`, `start_time`, `stop_time`, `promoted_object`, `special_ad_category_country`, `adlabels`. |
| `create_ad_set` (master), extend | Optional named parameters: `frequency_control_specs`, `attribution_spec`, `adset_schedule`, the four spend cap and minimum fields, `dsa_beneficiary`, `dsa_payor`, `adlabels`. |
| `create_ad` (master), extend | Optional named parameters: `conversion_domain`, `adlabels`, and an inline creative spec as an alternative to a creative id. |

For the three extended create tools, a named parameter and the same key inside
`params` must not both be given; that raises a `ValidationError`. A call that
uses only `params`, as callers do today, behaves exactly as before.

### Stream P3: reporting

Files: `meta_ads_mcp/tools/insights.py`,
`meta_ads_mcp/tools/performance_signals.py`,
`meta_ads_mcp/tools/discovery.py`, `meta_ads_mcp/tools/research.py`, and their
tests.

| Tool | Change |
|---|---|
| `get_performance_trend`, `detect_performance_anomalies`, extend | Cost per result, conversion rate and results follow each entity's own result as Meta reports it (the Insights `results` and `cost_per_result` fields), not only purchases and leads. State which definition was used. |
| `get_performance_trend`, `detect_performance_anomalies`, `get_auction_ranking_report`, `get_advertiser_context`, extend | Optional `object_ids` list, or a child-level split, so one call can cover several entities. Bounded, with coverage reported. |
| `get_entity_insights` (master), extend | Optional `sort` (a metric and direction), passed to Meta's Insights `sort` parameter. |
| `search_ads_archive` (master), extend | Optional `search_page_ids` and `ad_active_status`. `search_terms` is required today; a new optional path must not change calls that pass it. |
| `list_ad_accounts` (master), extend | Additional fields per account: owning business, whether a funding source exists, disable reason, and minimum daily budget, where Meta's account fields provide them. |

### Stream P4: experiments and audiences

Files: `meta_ads_mcp/tools/experiments.py`, `meta_ads_mcp/tools/audiences.py`,
and their tests.

| Tool | Change |
|---|---|
| `create_ab_test`, extend | Ad-level cells (creative tests), a `primary_metric` that decides the winner, and optional secondary metrics, where Meta's public study endpoint supports them. Report anything it does not support. |
| `check_ab_test_eligibility`, extend | Lift study eligibility, only if a documented public route exists. |
| `update_custom_audience` (master), extend | Optional named `rule` for website audiences. |

### Stream P5: catalog products

Files: `meta_ads_mcp/tools/catalogs.py` and its tests.

| Tool | Change |
|---|---|
| `batch_update_catalog_products` (new) | Create, update or delete many products in one call by retailer id (SKU), using Meta's batch endpoint. Per-item results. Warns when a product's feed will overwrite the change. States how a timed-out batch can be retried safely. `dry_run` supported. |
| `list_catalog_products`, extend | Filter to products hit by a diagnostic (`error_type`, `error_priority`), and a product-group view. |
| `list_catalog_attribute_values` (new) | The values that actually occur in a catalog for an attribute (brands, categories, custom labels), with counts, so the calling model can build a filter that matches. |
| `list_dpa_eligible_catalogs`, extend | Optional `ad_url` and `pixel_id`: prefer catalogs whose product URLs match the domain or that are connected to that pixel, and say why. |

### Stream P6: catalog feeds and event sources

Files: `meta_ads_mcp/tools/catalog_feeds.py` and its tests.

| Tool | Change |
|---|---|
| `update_feed_rule` (new) | Edits a feed rule's parameters, with before and after values. |
| `update_product_feed`, extend | `clear_replace_schedule` and `clear_update_schedule`, to stop a scheduled fetch without deleting the feed, if Meta documents how. |
| `create_product_feed`, `create_product_feed_upload`, extend | Feed credentials on schedules, and uploading a local file's contents, where documented. A password is never returned or logged. |
| `list_event_source_catalogs`, extend | Optional per-catalog match rates for the source. |
| `get_catalog_event_source_health`, extend | Scope to one source, and that source's match rate across all its catalogs beside this catalog's rate. |

### Stream P7: businesses and catalog access

New module `meta_ads_mcp/tools/business_access.py` and tests. New read group
`access`.

| Tool | What it does |
|---|---|
| `list_businesses` | Businesses the token can access, with name search. |
| `list_business_users` | A business's people, system users or partner businesses, with their roles. |
| `list_catalog_assigned_users` | Who can access a product catalog and with which permissions. |
| `list_user_assigned_catalogs` | The catalogs assigned to one person, system user or partner business. |
| `preview_catalog_access_change` | Read-only. Shows the current and proposed permissions for one person or partner on one catalog, and any warnings. |
| `set_catalog_access` | Grants, changes or removes a person's or partner's access to a catalog. Reads the current access first and returns before and after. |

Personal data: these tools return names and, where Meta provides them, email
addresses of people in the business. Return only the fields needed to identify
a person and their role; do not log them.

## How the parallel streams avoid colliding

As in phase one. Each stream works in its own git worktree from the foundation
commit, owns the files listed for it, and makes additive edits to the shared
registries. `assets.py` belongs to P1 alone. `performance_signals.py` belongs to
P3 alone. No stream edits `README.md`, `official_parity.py`, `input_compat.py`,
`intent_routing.py`, `coordinator.py`, `read_only.py`, the test fixture
`master_tool_contract.json`, or anything under `docs/`.

The foundation commit, by the orchestrator, adds the `access` read group, the
`GraphId` and `AccountId` types, and merges the phase-one review fixes.

After the merge the orchestrator updates the ledger from `partial` or `missing`
to `covered` only for entries whose gap is closed, updates the README, and
extends the search router if any new write tool starts with an unknown verb.

## Testing and verification

As in phase one, plus:

- `tests/test_backward_compat.py` passes. A stream edits
  `intentional_contract_changes.json` only to declare a change it made under
  the rules above, and never edits `master_tool_contract.json`.
- For every extended (master) tool that is meant to stay compatible, a test
  proves that a call made the old way sends exactly the same request as before.
- Every new write tool has a `dry_run` test proving nothing is sent.
- Live checks by the orchestrator are read-only and paced, because the app's
  rate limit is shared with production use. Write tools stay `mocked` in the
  ledger.
- A second Codex `gpt-6.1-sol` max-effort review covers phase two and the
  phase-one fixes.
