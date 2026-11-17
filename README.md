# Meta Ads MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP 3.4.8](https://img.shields.io/badge/FastMCP-3.4.8-green.svg)](https://github.com/PrefectHQ/fastmcp)
[![Meta Marketing API v26.0](https://img.shields.io/badge/Meta%20Marketing%20API-v26.0-blue.svg)](https://developers.facebook.com/docs/marketing-apis/)

**An optimization-first MCP server that bridges LLMs with the Meta Marketing API — tools for querying, managing, and optimizing your ad accounts through natural language.**

> Ask Claude or Gemini to "show me which creatives are fatiguing" or "give me an optimization snapshot for this account" — and it just works.

**This is not an officially supported Meta product.**

## ✨ Features

- 📊 **180 Tools** — Discovery, reporting, activity history, diagnostics, social feedback, targeting, research, product catalogs, business and catalog access, pixels and datasets, experiments, auth helpers, and controlled writes
- 🤝 **Parity with Meta's official Ads MCP server** — every one of the 106 tools it serves is mapped to a tool here, or recorded as partial, missing or unavailable with the reason; see [Parity with Meta's official server](#-parity-with-metas-official-server)
- 🔍 **Optimization-First** — Not just CRUD: pacing, fatigue, audience, and snapshot diagnostics built in
- 📖 **Built-in Docs** — Object model, metrics, optimization playbook, current v26 notes, and historical v25 notes available as tools and MCP resources
- 🎯 **Full Targeting Suite** — Interest, behavior, demographic, and geo search with audience size estimation
- 🔑 **Auth Helpers** — Generate tokens, exchange codes, refresh tokens, and validate scopes
- 🖼️ **Creative Ops** — Preview ads, upload assets, and set up A/B tests
- 🔎 **Tool Search** — FastMCP 3.4.8 tool search lets the LLM discover tools on demand instead of loading the full catalog up front
- 🖥️ **Works Everywhere** — Claude Code, Claude Desktop, Gemini CLI, or any MCP client

## 📋 Available Tools (180)

### 🔍 Discovery

| Tool | Description |
|------|-------------|
| `list_ad_accounts` | List all accessible ad accounts |
| `get_ad_account` | Get details for a specific ad account |
| `get_account_pages` | Get Pages linked to an ad account |
| `list_instagram_accounts` | List Instagram accounts linked to an ad account |
| `list_campaigns` | List campaigns in an ad account, optionally filtered by name |
| `get_campaign` | Get details for a specific campaign |
| `list_adsets` | List ad sets in a campaign or account, optionally filtered by name |
| `get_adset` | Get details for a specific ad set |
| `list_ads` | List ads in an ad set or account, optionally filtered by name |
| `get_ad` | Get details for a specific ad |
| `list_audiences` | List custom and lookalike audiences with lightweight summary fields by default |
| `get_audience` | Read one custom or lookalike audience by ID |
| `list_creatives` | List ad creatives in an account |
| `get_creative` | Read one ad creative by ID, including `url_tags` and full creative-spec fields |

### 📊 Analysis

| Tool | Description |
|------|-------------|
| `get_insights` | Backward-compatible alias for older insights calls |
| `get_entity_insights` | Get performance insights with optional scalar action columns |
| `get_performance_breakdown` | Break down performance by dimension |
| `summarize_actions` | Count selected actions without returning full action arrays |
| `compare_time_ranges` | Compare metrics across two time ranges |
| `compare_performance` | Compare metrics across multiple entities |
| `export_insights` | Export insights as CSV or structured data |
| `create_async_insights_report` | Create a large async report with lean defaults or an explicit full preset |
| `create_async_insights_report_batch` | Submit up to 10 independent breakdown reports sequentially |
| `get_async_insights_report` | Poll or bounded-wait for an async report and retrieve compact rows |
| `summarize_async_insights_report` | Sum additive metrics across report pages into compact groups |

### ⚡ Optimization

| Tool | Description |
|------|-------------|
| `get_account_optimization_snapshot` | Full account health and optimization summary |
| `get_campaign_optimization_snapshot` | Campaign-level optimization summary |
| `get_native_optimization_signals` | v26-native campaign, ad-set, or ad delivery and issue signals |
| `get_account_health_snapshot` | Account totals with previous-window and YoY comparisons |
| `detect_auction_overlap` | Directional cannibalization screen across campaigns |
| `get_ad_feedback_signals` | Feedback availability and quality-ranking signals |
| `get_budget_pacing_report` | Budget pacing and spend trajectory |
| `get_creative_performance_report` | Creative performance analysis |
| `get_creative_fatigue_report` | Detect creative fatigue signals |
| `get_audience_performance_report` | Audience segment performance analysis |
| `get_delivery_risk_report` | Delivery risks and issues |
| `get_learning_phase_report` | Learning phase status for ad sets |
| `get_recommendations` | Broad Meta-native opportunity scan |
| `get_budget_opportunities` | Budget and scaling opportunities |
| `get_creative_opportunities` | Creative asset and format opportunities |
| `get_audience_opportunities` | Audience and targeting opportunities |
| `get_delivery_opportunities` | Delivery and reach opportunities |
| `get_bidding_opportunities` | Bid strategy opportunities |
| `get_opportunity_score` | The account's 0 to 100 opportunity score, its daily history, and top recommendations by estimated score gain |
| `get_performance_trend` | CPC, CPM, cost per result, ROAS, CTR and conversion rate per day, week or month, with first-to-last change |
| `detect_performance_anomalies` | Flag days far from an entity's own recent baseline, showing the rule, baseline and threshold used |
| `get_auction_ranking_report` | Rank ads by Meta's quality, engagement and conversion rankings and say whether weak ads point at creative or at bid and budget |
| `get_advertiser_context` | Account briefing derived from settings, spend mix, funnel events and tracking setup |
| `get_delivery_errors` | Delivery-blocking problems across an account or named objects, ordered by severity and spend |

### 💬 Social Feedback

| Tool | Description |
|------|-------------|
| `get_ad_social_context` | Resolve Facebook post and Instagram media ids behind an ad |
| `list_ad_comments` | Read compact raw Facebook or Instagram comments for an ad or social object |
| `list_comment_replies` | Continue paginated replies for one Facebook or Instagram comment |
| `list_page_recommendations` | Read compact Facebook Page recommendations, reviews, or testimonials |

### 🧾 Activity History

| Tool | Description |
|------|-------------|
| `list_change_history` | Read one explicitly bounded activity page, with completeness, active filters, and an executable continuation |
| `export_change_history` | Export all API-returned changes for explicit dates, including descendants, full raw details, and coverage metadata; retrieve every saved event through `read_overflow_artifact` |

### 🎯 Planning & Targeting

| Tool | Description |
|------|-------------|
| `search_interests` | Search interest targeting options |
| `get_interest_suggestions` | Get related interest suggestions |
| `validate_interests` | Validate interest targeting IDs |
| `search_geo_locations` | Search geographic targeting options |
| `get_targeting_categories` | Get available targeting categories |
| `search_behaviors` | Search behavior targeting options |
| `search_demographics` | Search demographic targeting options |
| `estimate_audience_size` | Estimate audience size for a targeting spec |
| `get_reach_frequency_predictions` | Get reach and frequency predictions |

### ✏️ Writes

| Tool | Description |
|------|-------------|
| `create_campaign` | Create a new campaign |
| `update_campaign` | Update campaign settings |
| `delete_campaign` | Delete a campaign |
| `create_ad_set` | Create or validate an ad set, including bid strategy and constraints |
| `create_ad` | Create or validate an ad using a top-level or nested creative ID |
| `delete_adset` | Explicitly delete an ad set |
| `delete_ad` | Explicitly delete an ad |
| `set_campaign_status` | Pause or enable a campaign |
| `set_adset_status` | Pause or enable an ad set |
| `set_ad_status` | Pause or enable an ad |
| `update_campaign_budget` | Update a campaign's budget |
| `update_adset_budget` | Update an ad set's budget |
| `update_adset_bid_amount` | Update an ad set's bid amount |
| `update_campaign_bid_strategy` | Update a campaign's bidding strategy |
| `update_adset_bid_strategy` | Update an ad set's bidding strategy and constraints |
| `create_custom_audience` | Create a custom audience |
| `create_lookalike_audience` | Create a lookalike audience |
| `update_custom_audience` | Update a custom audience |
| `delete_audience` | Delete an audience |
| `create_ad_creative` | Create a new ad creative |
| `update_creative` | Update an ad creative |
| `update_ad_creative` | Attach a prepared replacement creative to an existing ad |
| `delete_creative` | Delete an ad creative |
| `update_adset` | Edit ad set name, targeting, schedule and other settings, with before and after values |
| `update_ad` | Edit ad name, creative and tracking, with before and after values |
| `add_audience_users` | Add people to a customer list audience from a local CSV; values are hashed on this machine |
| `remove_audience_users` | Remove people from a customer list audience |
| `boost_instagram_post` | Turn an existing Instagram post into a paused ad under an existing ad set |
| `create_ab_test` | Create an A/B test from cells of campaigns or ad sets |
| `create_lift_study` | Create a conversion lift study |
| `update_ad_study` | Edit an A/B test or lift study |
| `cancel_ad_study` | End or cancel an A/B test or lift study |
| `create_catalog` | Create a product catalog |
| `update_catalog` | Edit product catalog settings |
| `create_catalog_product` | Create a product (starts in staging visibility) |
| `update_catalog_product` | Edit product fields such as name, price and availability |
| `delete_catalog_product` | Delete a product |
| `create_product_set` | Create a product set from a filter |
| `update_product_set` | Edit a product set's name or filter |
| `delete_product_set` | Delete a product set; products remain |
| `create_product_feed` | Register a product feed, optionally fetched from a URL on a schedule |
| `update_product_feed` | Edit a feed's name, defaults or schedules |
| `delete_product_feed` | Delete a feed; its products are removed unless another source supplies them |
| `create_product_feed_upload` | Start an upload for a feed from a URL |
| `create_feed_rule` | Create an attribute mapping or transformation rule on a feed |
| `delete_feed_rule` | Delete a feed rule |
| `connect_catalog_event_source` | Connect a pixel or other event source to a product catalog |
| `disconnect_catalog_event_source` | Disconnect an event source from a product catalog |
| `duplicate_campaign` | Copy a campaign, optionally with its ad sets and ads; the copy starts paused |
| `duplicate_adset` | Copy an ad set, optionally into another campaign; the copy starts paused |
| `duplicate_ad` | Copy an ad, optionally into another ad set; the copy starts paused |

### 🖼️ Creative Ops

| Tool | Description |
|------|-------------|
| `get_ad_image` | Get ad image details |
| `preview_ad` | Preview an ad in various formats |
| `upload_creative_asset` | Upload an image or video asset |
| `setup_ab_test` | Set up an A/B test |
| `create_ad_creative_from_inputs` | Build a creative from named inputs: image, video, carousel, text variants, per-placement media, asset pools, catalog carousels, existing posts, click-to-message, lead forms and Partnership Ads; `dry_run` shows the request |
| `update_ad_creative_enhancements` | Turn Advantage creative enhancements on or off for a live ad by attaching a changed copy of its creative |
| `upload_ad_video` | Upload a video from a URL or from the upload folder (chunked above 25 MB, up to 4 GB) and report processing status |
| `delete_ad_image` | Remove an image from the account's image library after reading it |

### 🔬 Public Research

| Tool | Description |
|------|-------------|
| `search_ads_archive` | Search the Meta Ad Library for competitor creatives |

### 🔑 Auth Helpers

| Tool | Description |
|------|-------------|
| `generate_auth_url` | Generate an OAuth authorization URL |
| `exchange_code_for_token` | Exchange an auth code for an access token |
| `refresh_to_long_lived_token` | Refresh a short-lived token to a long-lived one |
| `generate_system_user_token` | Generate a system user access token |
| `get_token_info` | Get info about the current token |
| `validate_token` | Validate token scopes and expiry |

### 🗂️ Pages, Media & Asset Usage

| Tool | Description |
|------|-------------|
| `list_pages` | Pages the user can advertise with, Pages used on an ad account, or Pages owned by a business |
| `list_ad_images` | The ad account's image library |
| `list_ad_videos` | The ad account's video library |
| `list_creative_ads` | Ads that use one creative, with how many ads were scanned and whether the scan was complete |
| `list_instagram_media` | Recent Instagram posts, reels or stories, each marked boostable or not with Meta's reason |
| `list_audience_adsets` | Ad sets that target one custom audience |
| `get_field_reference` | Field types, updatability and allowed values for campaigns, ad sets and ads |

### 📡 Pixels & Datasets

| Tool | Description |
|------|-------------|
| `list_datasets` | Pixels and datasets on an ad account or business |
| `get_dataset` | One dataset's settings |
| `get_dataset_stats` | Event volume for up to 28 days, split by event, device or source |
| `get_dataset_quality` | Event match quality, match-key coverage and data freshness, with weak scores flagged |
| `list_custom_conversions` | Custom conversions on an ad account |

### 🧪 Experiments

| Tool | Description |
|------|-------------|
| `list_ad_studies` | A/B tests and lift studies on an account, a business, or one campaign, ad set or ad |
| `get_ad_study` | One study's settings, cells and objectives, and its winner or lift when results are ready |
| `check_ab_test_eligibility` | Local pre-check of campaigns or ad sets against Meta's documented A/B test limits |

### 🛍️ Product Catalogs

| Tool | Description |
|------|-------------|
| `list_catalogs` | Product catalogs for a business or ad account, or one catalog with counts |
| `list_dpa_eligible_catalogs` | Catalogs ranked by readiness for catalog ads, with the reasons |
| `get_catalog_diagnostics` | Catalog errors and opportunities ordered by products affected |
| `get_catalog_dynamic_ads_health` | Whether a catalog or product set can serve catalog ads, and what blocks it |
| `list_catalog_data_sources` | Feeds, uploads and API sources that supply a catalog |
| `list_catalog_products` | Products in a catalog or product set, filtered, or one product |
| `build_product_set_filter` | Build a valid product set filter from conditions and count the products that match |
| `list_product_sets` | Product sets in a catalog, one set, or the sets containing a product |
| `list_product_feeds` | Feeds on a catalog, or one feed with settings, schedule and latest upload |
| `list_product_feed_uploads` | Feed uploads with status, counts and errors ordered by rows affected |
| `list_feed_rules` | Attribute mapping and transformation rules on a feed |
| `list_catalog_event_sources` | Pixels, apps and offline datasets connected to a catalog |
| `list_event_source_catalogs` | Catalogs connected to one event source |
| `get_catalog_event_source_health` | Match rate and setup problems per connected event source, with 28-day history |
| `get_catalog_event_source_recommendations` | Event sources worth connecting to a catalog, with the evidence |
| `batch_update_catalog_products` | Create, update or delete up to 50 products by SKU in one call, with a per-item outcome and retry advice |
| `list_catalog_attribute_values` | The values that actually occur for a product attribute, with counts, for building product set filters |
| `update_feed_rule` | Change one feed rule and return before and after |

### 🏢 Business & Catalog Access

| Tool | Description |
|------|-------------|
| `list_businesses` | Businesses the token's user can access, with the user's roles |
| `list_business_users` | A business's people, system users or partner businesses |
| `list_catalog_assigned_users` | Who has direct access to a catalog, and with which tasks |
| `list_user_assigned_catalogs` | The catalogs assigned to one person, system user or partner |
| `list_catalog_partners` | Partner businesses a catalog is shared with (request shape not yet confirmed live for writes) |
| `preview_catalog_access_change` | Read-only preview of a catalog access change: before, after and what would be sent |
| `set_catalog_access` | Grant, change or remove catalog access for a person, system user or partner business, with lockout protection |

### 📖 Docs & Utility

| Tool | Description |
|------|-------------|
| `get_meta_object_model` | Meta Ads object model reference |
| `get_metrics_reference` | Insights metrics reference |
| `get_v26_notes` | Current Marketing API v26.0 capabilities and migration notes |
| `get_v25_notes` | Historical Marketing API v25.0 release notes |
| `get_optimization_playbook` | Optimization best practices playbook |
| `health_check` | Server health check |
| `get_capabilities` | List server capabilities |
| `list_mutation_tools` | List write tools and safety notes |
| `read_overflow_artifact` | Retrieve parsed item pages or complete original JSON byte chunks |
| `delete_overflow_artifact` | Delete an oversized response artifact after retrieval |

FastMCP 3.4.8 also exposes dynamic search tools at runtime:

| Tool | Description |
|------|-------------|
| `search_tools` | Search for tools by keyword |
| `call_tool` | Call a discovered tool by name |

These let the LLM discover the right tool on demand instead of loading the full catalog into context up front.
`call_tool` accepts `name` or the common `tool_name` alias, and `arguments` may
be a JSON object or an object-valued JSON string. Familiar `list_ad_sets`,
`get_ad_set`, and `delete_ad_set` spellings resolve to the canonical `adset`
tool names without duplicating search results. Public string-list parameters,
including `fields`, breakdowns, statuses, countries, scopes, and action types,
accept either JSON string lists or comma-separated strings.

Budget and bid inputs use human-readable account-currency units. The server
resolves currency from the owning Ad Account, converts values to Meta minor
units exactly, and rejects precision the currency cannot represent (for
example, fractional JPY). The conversion follows the offset Meta documents for
each currency: whole units for CLP, COP, CRC, HUF, IDR, ISK, JPY, KRW, PYG, TWD
and VND, hundredths for every other currency. Campaign bidding strategy and Ad Set bid amounts are
separate operations because Meta does not expose a campaign-level `bid_amount`.
These units are also present in tool input schemas. `bid_constraints.roas_average_floor`
is a raw integer scaled by 10,000 (`30000` means a 3.0 ROAS floor).
`list_change_history` preserves Meta's raw payment amounts in minor currency
units and labels that convention; USD cents in activity payloads are not budget inputs.

Compound `search_tools` queries can discover several requested operations at once.
Client-side deferred-tool search can load the visible `search_tools` and `call_tool`
wrappers; inner tools are then invoked through `call_tool(name=..., arguments=...)`.
Use `get_adset(fields=["id","name","effective_status","daily_budget","bid_amount"])`
for a small preflight or read-back. The other single-entity reads also accept `fields`.

## 🤝 Parity with Meta's official server

Meta publishes a hosted Ads MCP server (`https://mcp.facebook.com/ads`). On
2026-11-12 it served 106 tools: 87 of the 88 named in Meta's documentation plus
19 undocumented ones. `meta_ads_mcp/official_parity.py` holds one entry for each
served name, written from that tool's real description and parameter list and
last checked against this code by an independent comparison on 2026-11-13. A
test fails if a served name is missing or an entry points at a tool that is not
registered.

| Status | Meaning | Tools |
|--------|---------|-------|
| `covered` | A tool here does everything Meta's tool does, or more | 58 |
| `partial` | A tool here does part of it; the entry says what Meta's tool can do that ours cannot, and why that is still open | 30 |
| `missing` | Nothing here yet, although Meta's public API appears to allow it | 0 |
| `unavailable` | No working public API route is known | 13 |
| `app_internal` | Plumbing for Meta's own app (upload dialogs, telemetry); nothing to match | 6 |

The `covered` count includes `ads_update_custom_audience_users`, which Meta
documents but does not serve.

One `unavailable` entry does have a tool: `delete_feed_rule` sends the request
Meta's reference documents, but Meta's live API answers that the endpoint is
retired (tested 2026-11-13). The tool reports that plainly and says what to do
instead.

Of the 30 `partial` entries, 21 are open because no public API route is known
for what Meta's own tool does, and 9 are built differently on purpose (for
example, a stated rule in place of Meta's private detector, or rarely used ad
set fields left to `params`). None is waiting to be built.

Each `covered` or `partial` entry also carries `verified`: `live` when every
serving tool has been run against a real ad account and Meta accepted the
request, otherwise `mocked`. Mocked tests use a fake client that accepts any
endpoint name, so they cannot catch a wrong endpoint or field name. A write tool
is `live` only if it ran in the live write tests of 2026-11-13, on a throwaway ad
account and a test catalog, with everything created paused and deleted afterwards.
Some of those tools were run again there on 2026-11-14 and 2026-11-15, after later
changes to them.

To find the tool for one of Meta's names, call
`get_capabilities(official_tool="ads_get_opportunity_score")`. A Meta name
served by exactly one tool here can also be used directly as a tool name. A name
served by several tools, or by none, returns an explanation instead.

No public API route is known for:

- Benchmarks against similar advertisers (`ads_insights_industry_benchmark`,
  `ads_insights_performance_vs_peers`).
- Business Help Center article search (`ads_get_help_article`).
- Partner store integration health (`ads_catalog_list_partner_integrations`).
- Pixel event rules and parameter extractors (the eight `ads_pixel_event_*` and
  `ads_pixel_parameter_*` tools): Meta's own server limits them to first-party
  clients, the public reference marks changes as unsupported, and the live API
  rejected the documented reads.

## 📚 MCP Resources

| Resource | Description |
|----------|-------------|
| `meta://docs/object-model` | Meta Ads object hierarchy |
| `meta://docs/insights-metrics` | Available insights metrics |
| `meta://docs/v25-notes` | v25.0 API release notes |
| `meta://docs/v26-notes` | Current v26.0 API capabilities and migration notes |
| `meta://docs/optimization-playbook` | Optimization best practices |
| `meta://docs/tool-routing` | Tool routing guide |
| `get_v26_notes` | Current v26 API fields, breakdowns, recommendation filters and migration notes |
| `get_v25_notes` | Historical v25 implementation notes and deprecations |

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pipx`
- A Meta access token with the scopes needed for the tools you want to use
- Social feedback tools also need Page or Instagram permissions, such as `pages_read_user_content` for Page comments/recommendations and the relevant Instagram comment permissions for IG media comments

### 1. Get Your Credentials

Set your access token:

```bash
export META_ACCESS_TOKEN='YOUR_META_ACCESS_TOKEN'
```

Optional settings:

```bash
export META_DEFAULT_ACCOUNT_ID='act_1234567890'
export META_API_VERSION='v26.0'
export META_APP_ID='YOUR_APP_ID'
export META_APP_SECRET='YOUR_APP_SECRET'
export META_REDIRECT_URI='https://example.com/callback'
export META_EXPORT_DIR='/path/to/meta-ads-exports'
export META_EXPORT_TTL_SECONDS='86400'
export META_EXPORT_MAX_FILES='100'
export META_EXPORT_MAX_BYTES='1000000000'
export META_REQUEST_TIMEOUT='30'
```

You can also place these in a `.env` file at the repo root.

`META_REQUEST_TIMEOUT` bounds each HTTP attempt's total elapsed time as well as
network inactivity. Uncertain writes return `mutation_outcome_unknown=true` and
are never retried automatically. Read the target's current state before deciding
whether another write is necessary.

### Identify and refresh the running build

`get_capabilities` and `health_check` report installed dependency versions and a
`build.source_sha256` fingerprint captured when the server imports its source.
Save this metadata with usage reports. Restart the MCP process after an update;
an already running process retains its imported code.

With pipx's pip backend, `pipx run` can reuse its environment for 14 days.
To test a delivered commit, pin its SHA in the launch spec and rebuild the cache:

```bash
pipx run --no-cache --spec 'git+https://github.com/jeffreywlai/meta-ads-mcp.git@COMMIT_SHA' run-meta-ads-mcp
```

Replace `COMMIT_SHA` with the delivered commit before running this command.
See [pipx run caching](https://pipx.pypa.io/latest/tutorial/run-applications.html#run-cache).
This repository's changes do not update other computers or restart their clients.

When `META_APP_SECRET` is set, authenticated Graph API requests using the
configured `META_ACCESS_TOKEN` automatically include Meta's HMAC-SHA256
`appsecret_proof`. A different token supplied as a request override is left
unproved because it may belong to another app. The secret and generated proof
are never included in public error payloads.

### Optional settings

- **Analysis only.** Set `META_READ_ONLY=true` and restart. Every tool that changes something is hidden and refused. The default is writable.
- **Uploading local files.** Set `META_UPLOAD_DIR` to one folder. The upload tools read files from that folder and nowhere else. Until it is set, they refuse every local file.
- **Large responses.** A result too big to return is saved to a local file and you get an `export_id` instead. Read it with `read_overflow_artifact`. The `META_EXPORT_*` settings above control where it is kept and for how long.

### How the tools behave

The details are in [docs/reference.md](docs/reference.md):

- [Large responses](docs/reference.md#large-responses)
- [Reports](docs/reference.md#reports) and [async reports](docs/reference.md#async-reports)
- [Creative fatigue report](docs/reference.md#creative-fatigue-report)
- [Finding ads by name](docs/reference.md#finding-ads-by-name), [landing pages](docs/reference.md#landing-pages) and [image rules on an ad](docs/reference.md#image-rules-on-an-ad)
- [Audiences](docs/reference.md#audiences)
- [Change history](docs/reference.md#change-history)
- [Read-only mode](docs/reference.md#read-only-mode) and [local file uploads](docs/reference.md#local-file-uploads)
- [Tool markings](docs/reference.md#tool-markings) and [input formats](docs/reference.md#input-formats)
- [Errors and retries](docs/reference.md#errors-and-retries)
- [API version 26](docs/reference.md#api-version-26) and [checking fields against Meta's SDK](docs/reference.md#checking-fields-against-metas-sdk)

Don't have a token? Use the built-in auth helper tools (`generate_auth_url` → `exchange_code_for_token` → `refresh_to_long_lived_token`) to create one interactively.

### 2. Install & Run

#### Option A: Claude Code (recommended)

```bash
# Install from repo
claude mcp add --transport stdio MetaAds \
  --env META_ACCESS_TOKEN=YOUR_META_ACCESS_TOKEN \
  --env META_DEFAULT_ACCOUNT_ID=act_1234567890 \
  -- pipx run --spec git+https://github.com/jeffreywlai/meta-ads-mcp.git run-meta-ads-mcp
```

Or from a local clone:

```bash
claude mcp add --transport stdio MetaAds \
  --env META_ACCESS_TOKEN=YOUR_META_ACCESS_TOKEN \
  --env META_DEFAULT_ACCOUNT_ID=act_1234567890 \
  -- uv run --directory /path/to/meta-ads-mcp -m meta_ads_mcp.stdio
```

Type `/mcp` in Claude Code to verify the server is connected.

#### Option B: Gemini CLI

Add to your Gemini MCP configuration:

```json
{
  "mcpServers": {
    "MetaAds": {
      "command": "pipx",
      "args": [
        "run", "--spec",
        "git+https://github.com/jeffreywlai/meta-ads-mcp.git",
        "run-meta-ads-mcp"
      ],
      "env": {
        "META_ACCESS_TOKEN": "YOUR_META_ACCESS_TOKEN",
        "META_DEFAULT_ACCOUNT_ID": "act_1234567890"
      },
      "timeout": 30000
    }
  }
}
```

#### Option C: Direct Launch

```bash
uv sync
uv run -m meta_ads_mcp.stdio
```

For HTTP mode:

```bash
uv run -m meta_ads_mcp.server
```

## 💬 Usage Examples

Once connected, just talk naturally:

### Discovery

```
"List my accessible ad accounts"
"Show me the campaigns in act_1234567890"
"What Instagram accounts are linked to this ad account?"
"Find the Pages I can use for creative setup"
```

### Reporting & Analysis

```
"Show me account-level insights for the last 7 days"
"Compare these two campaigns on ROAS, CPA, and CTR"
"Break down campaign performance by country"
"Export ad-level insights for the last 30 days as CSV"
"Create async reports for country and device breakdowns, then wait for each result"
"Return purchase and purchase_value as flat columns without raw action arrays"
```

### Optimization

```
"Give me an optimization snapshot for this account"
"Which creatives are fatiguing in this campaign?"
"What are the biggest delivery risks right now?"
"Which audience segments are underperforming?"
```

### Planning & Research

```
"Estimate audience size for this targeting spec"
"Find interest suggestions related to running and fitness"
"Search demographic targeting categories for new parents"
"Search the Meta ads archive for competitor creatives"
```

### Writes

```
"Create a paused campaign for this account"
"Pause this campaign"
"Update this ad set budget to 75 dollars daily"
"Validate a target-ROAS ad set with a 30000 ROAS floor without creating it"
"Create an ad in this ad set using creative 123"
```

### Creatives, Boosts & Uploads

```
"Upload banner.png from my upload folder and build a link ad creative with it"
"Build a carousel creative with these three images and links, as a dry run first"
"Which of my recent Instagram posts can be boosted?"
"Boost this Instagram post for 6 days at 20 dollars a day, paused, and show me the plan first"
"Turn off image touch-ups on this ad without changing anything else"
```

### Audiences & Experiments

```
"Create a customer-list audience and add the emails in customers.csv from my upload folder"
"Set up a paused A/B test between these two campaigns, split 50/50, for two weeks"
"Show me the results of this A/B test"
```

### Product Catalogs

```
"List my product catalogs and how many products each has"
"What is wrong with this catalog? Show its diagnostics"
"Update the price and availability of these 40 products in one batch, as a dry run first"
"Create a product set of everything from brand Acme that is in stock"
"Is this catalog's pixel matching events to products?"
```

## 🏗️ Project Structure

```
meta-ads-mcp/
├── meta_ads_mcp/
│   ├── coordinator.py         # Shared FastMCP instance & tool search config
│   ├── stdio.py               # Server entry point (Claude Code / stdio)
│   ├── server.py              # Server entry point (Gemini / SSE)
│   ├── config.py              # Configuration and env loading
│   ├── auth.py                # Auth utilities
│   ├── build_info.py          # Non-secret identity of the running build
│   ├── graph_api.py           # Meta Graph API client
│   ├── graph_payload.py       # Safe construction of write payloads
│   ├── api_compat.py          # Checks that depend on the Graph API version
│   ├── pagination.py          # Graph API pagination helpers
│   ├── errors.py              # Error types
│   ├── error_middleware.py    # Turns errors into stable MCP error envelopes
│   ├── read_only.py           # Opt-in read-only mode (META_READ_ONLY)
│   ├── local_files.py         # Reads upload files only from META_UPLOAD_DIR
│   ├── overflow.py            # Bounded storage for oversized responses
│   ├── money.py               # Account-currency resolution & exact conversion
│   ├── normalize.py           # Response normalization and id comparison
│   ├── schemas.py             # Shared response builders
│   ├── diagnostics.py         # Derived metrics & optimization heuristics
│   ├── tool_types.py          # Shared input types for tool parameters
│   ├── input_compat.py        # Forgiving normalization of tool inputs
│   ├── id_formats.py          # Ids Meta defines as numbers must be digits
│   ├── activity_scope.py      # Validation of change-history scope arguments
│   ├── intent_routing.py      # Safety layer for tool search
│   ├── tool_contracts.py      # Contracts derived from the live tool catalog
│   ├── tool_annotations.py    # Read-only, destructive and open-world markings for every tool
│   ├── schema_audit.py        # Audits field contracts against Meta's SDK schemas
│   ├── field_reference.py     # Static field reference for campaigns, ad sets, ads
│   ├── official_parity.py     # Ledger mapping Meta's official tools to these tools
│   ├── argument_privacy.py    # Keeps argument values out of logs and errors
│   ├── secret_scrub.py        # Removes caller-supplied secrets from text and errors
│   ├── url_secrets.py         # Keeps usernames and passwords out of shown URLs
│   ├── tools/
│   │   ├── ads.py             # Ad creation and creative-image inspection
│   │   ├── activity.py        # Activity and change-history reads
│   │   ├── assets.py          # Pages, media libraries, asset usage, field metadata
│   │   ├── audiences.py       # Audience management and customer-list uploads
│   │   ├── auth_tools.py      # Auth helper tools
│   │   ├── business_access.py # Businesses, people, partners, catalog access
│   │   ├── campaigns.py       # Campaign & ad set management, copies, schedules
│   │   ├── catalogs.py        # Product catalogs, products and product sets
│   │   ├── catalog_feeds.py   # Catalog feeds, uploads, feed rules, event sources
│   │   ├── creatives.py       # Creatives, image and video uploads, boosts
│   │   ├── creative_formats.py # Creative builders from named inputs, enhancements
│   │   ├── datasets.py        # Pixels and datasets, event quality, custom conversions
│   │   ├── diagnostics.py     # Optimization & diagnostics
│   │   ├── discovery.py       # Account & entity discovery
│   │   ├── docs.py            # Built-in documentation tools
│   │   ├── execution.py       # Status, budget and bid changes
│   │   ├── experiments.py     # A/B tests and lift studies
│   │   ├── insights.py        # Reporting & analysis
│   │   ├── performance_signals.py # Opportunity score, trends, anomalies, delivery errors
│   │   ├── recommendations.py # Meta recommendations and opportunities
│   │   ├── research.py        # Ad Library research
│   │   ├── social_feedback.py # Comments & Page recommendations
│   │   ├── targeting.py       # Targeting & planning
│   │   └── utility.py         # Health check & capabilities
│   └── context/               # Embedded docs & playbooks
├── tests/                     # Mirrors source structure
├── CLAUDE.md
├── GEMINI.md
├── SPEC.md
├── pyproject.toml
└── README.md
```

## 🛠️ Development

```bash
uv sync --extra dev              # Install runtime and test dependencies
uv run --extra dev pytest        # Run tests
```

### Project Notes

- Keep Graph API calls isolated in `meta_ads_mcp/graph_api.py`
- Prefer optimization and diagnostics tools before mutations
- Return dicts and lists rather than serialized JSON strings
- Treat all account IDs and object IDs as strings
- Keep the implementation aligned with Marketing API v26.0

## 📄 License

Licensed under the [MIT License](LICENSE).

## 📬 Contact

Questions, suggestions, or feedback? [Open an issue](https://github.com/jeffreywlai/meta-ads-mcp/issues).

---

**Built with [FastMCP 3.4.8](https://github.com/PrefectHQ/fastmcp) and [Meta Marketing API v26.0](https://developers.facebook.com/docs/marketing-apis/)**
