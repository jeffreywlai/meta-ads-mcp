# Meta Ads FastMCP Spec

## Summary

Build a new Meta Ads MCP server from scratch on FastMCP for Claude Code,
Claude Desktop, Gemini CLI, and similar MCP clients.

This server is not a general-purpose clone of upstream Meta Ads MCP projects.
It is an optimization-first tool: it should prioritize API calls and derived
analysis that help an LLM understand what is happening in an ad account,
diagnose performance problems, and recommend changes with evidence.

As of March 7, 2026, the implementation baseline is:

- Meta Marketing API `v25.0`
- FastMCP latest stable `3.1.0`
- Python `3.12+`

FastMCP `3.1.0` is the stable target and should be used with tool-search
transforms enabled to reduce upfront tool-context usage in compatible clients.

## Product Goals

- Work cleanly over `stdio` for Claude Code and similar local LLM CLIs.
- Expose information-rich tools that support analysis, diagnosis, and
  optimization of ad accounts, campaigns, ad sets, ads, creatives, audiences,
  and budgets.
- Return structured JSON-native objects, not pre-serialized JSON strings.
- Keep the architecture small, modular, and testable.
- Use Marketing API `v25.0` and leave room for v25-era recommendation and
  optimization surfaces.

## Non-Goals

- Replicate the entire Meta Marketing API in v1.
- Rebuild browser-first auth or hosted SaaS auth flows in v1.
- Optimize first for HTTP clients instead of local MCP clients.
- Support every mutation on day one.
- Build a vendor-specific remote auth product.

## Product Principles

### 1. Optimization First

The first-class tools are not CRUD tools. The first-class tools are:

- performance summaries
- breakdown analysis
- attribution-aware reporting
- pacing and budget diagnostics
- creative fatigue detection
- audience saturation signals
- recommendation and opportunity surfaces

### 2. LLM-Friendly Outputs

The server should not merely proxy raw Graph API payloads. It should return:

- normalized metrics
- computed KPIs
- ranked entities
- deltas vs comparison windows
- diagnostic flags
- concise evidence objects

### 3. Thin Execution Layer

Mutation tools are useful, but narrow:

- pause / enable
- small budget updates
- targeted bid or status changes only when safe

The server should help the LLM decide what to change before it changes anything.

### 4. CLI-First

`stdio` is the primary transport and primary documented path.

### 5. Stable Schemas

Every tool should return predictable shapes so the LLM can chain calls without
guessing.

The MCP boundary owns compatibility normalization. Common envelope aliases,
stringified argument objects, and string-list coercion must be implemented once
and covered by catalog-wide contract tests rather than repeated inside tools.

## Primary Users

- Developers using Claude Code or Gemini CLI
- Performance marketers using an LLM to inspect and optimize accounts
- Internal operators who want natural-language analysis with evidence

## Success Criteria

- A user can connect the server in Claude Code over `stdio` and retrieve:
  account summaries, campaign rankings, attribution-aware insights,
  creative-performance summaries, and audience/budget diagnostics.
- Tool outputs are consistent enough that an LLM can form optimization plans
  without custom prompt scaffolding.
- Core tools are unit-tested with mocked Graph API responses.
- The server can be run locally from source and from an installed package.

## Technical Baseline

- Runtime: Python `3.12+`
- Framework: FastMCP stable `3.1.0` at project start
- Protocol: MCP over `stdio` first, HTTP second
- Upstream API: Meta Marketing API `v25.0`
- HTTP client: `httpx.AsyncClient`
- Validation/models: `pydantic>=2`
- Testing: `pytest`, `pytest-asyncio`

## Supported Transports

- Primary: `stdio`
- Secondary: streamable HTTP

HTTP mode is supported for interoperability, but the architecture and docs
should assume a local CLI client first.

## Authentication

### V1 Auth Model

V1 uses env-based bearer-token auth only.

Required env vars:

- `META_ACCESS_TOKEN`

Optional env vars:

- `META_API_VERSION` default `v25.0`
- `META_DEFAULT_ACCOUNT_ID`
- `META_APP_ID`
- `META_APP_SECRET`
- `LOG_LEVEL`
- `FASTMCP_HOST`
- `FASTMCP_PORT`

### Auth Rules

- `stdio` clients use `META_ACCESS_TOKEN` from environment.
- HTTP clients may additionally use `Authorization: Bearer <token>`.
- No browser login flow in v1.
- No Pipeboard-specific auth.
- No local token cache in v1.

## Repo Layout

```text
meta_ads_mcp/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── GEMINI.md
├── .mcp.json
├── SPEC.md
├── meta_ads_mcp/
│   ├── __init__.py
│   ├── coordinator.py
│   ├── stdio.py
│   ├── server.py
│   ├── config.py
│   ├── auth.py
│   ├── errors.py
│   ├── error_middleware.py
│   ├── graph_api.py
│   ├── graph_payload.py
│   ├── input_compat.py
│   ├── pagination.py
│   ├── normalize.py
│   ├── diagnostics.py
│   ├── schemas.py
│   ├── tool_types.py
│   ├── tools/
│   │   ├── discovery.py
│   │   ├── insights.py
│   │   ├── diagnostics.py
│   │   ├── targeting.py
│   │   ├── recommendations.py
│   │   ├── creatives.py
│   │   ├── social_feedback.py
│   │   ├── execution.py
│   │   └── docs.py
│   └── context/
│       ├── object_model.md
│       ├── insights_metrics.md
│       ├── v25_notes.md
│       └── optimization_playbook.md
└── tests/
    ├── test_stdio.py
    ├── test_server.py
    ├── test_graph_api.py
    └── tools/
```

## Architecture

### `coordinator.py`

Defines the shared FastMCP instance, intent-aware tool search, compatibility
aliases, and server-wide middleware.

### `stdio.py`

Runs the server over `stdio`. This is the default entry point for Claude Code
and similar tools.

### `server.py`

Runs the server over streamable HTTP.

### `graph_api.py`

The only module allowed to make direct Meta Graph / Marketing API requests.
Responsibilities:

- auth header injection
- versioned base URL handling
- retries and backoff
- safe-read-only retry policy; writes are never retried automatically after an
  ambiguous transport or transient server failure
- timeout handling
- paging helpers
- sync insights calls
- async insights job creation and polling
- Meta error parsing

### `input_compat.py` and `tool_types.py`

Define the LLM-facing compatibility boundary: canonical tool-name aliases,
`call_tool` envelope normalization, and reusable CSV-or-array string-list input
types.

### `graph_payload.py`

Builds mutation payloads without allowing generic extension parameters to
supply present or omitted typed fields, transport-managed authentication, or
server-managed execution controls.

### `error_middleware.py`

Converts typed Graph and rate-limit failures into compact, allowlisted,
machine-readable MCP errors after FastMCP exception wrapping.

### `normalize.py`

Normalizes raw API responses into analysis-ready structures:

- money normalization when field semantics are clear
- numeric coercion
- extracted action metrics
- extracted conversion values
- paging cursors
- lightweight entity summaries

### `diagnostics.py`

Computes derived optimization signals from raw API data:

- CTR, CPC, CPM, CPA, ROAS, CVR where inputs exist
- spend concentration
- pacing anomalies
- frequency pressure
- creative fatigue heuristics
- account / campaign / ad set ranking logic
- low-data flags

### `schemas.py`

Shared typed models and response builders.

## Server-Level Instructions

The FastMCP server instructions should tell the LLM:

- Prefer discovery and diagnostics before mutations.
- Use account and campaign summary tools before drilling into ad-level data.
- Ask for confirmation before spend-affecting mutations when the client allows.
- Use documentation tools when uncertain about fields or attribution windows.
- Treat all IDs as strings.
- Prefer comparison windows and ranked outputs when deciding what to optimize.

## Tool Design Rules

- All tools return dicts or lists, never serialized JSON strings.
- Reads return stable envelopes such as `{items, paging, summary}` or
  `{item, summary}`.
- Analysis tools return `{scope, metrics, findings, evidence, suggestions}`.
- Updates return `{ok, action, target, previous, current}`; creations return a
  stable creation/validation envelope.
- Internal errors use typed exceptions. The MCP boundary exposes safe structured
  Graph fields, retry guidance, and ambiguous-mutation warnings.
- Any derived metric must include the raw components used to compute it.
- If a metric is not available, omit it and record why in `missing_signals`.
- Every public string-list input accepts either a JSON string array or a
  comma-separated string; commas nested inside Graph field expressions are
  preserved.
- Generic mutation `params` may extend typed payloads but cannot supply any
  present or omitted tool-managed field, `access_token`, or
  `execution_options`.
- The MCP transport caps inline tool responses. Oversized results are preserved
  as complete private JSON artifacts and replaced inline with an opaque
  `export_id`. Callers retrieve bounded chunks with `read_overflow_artifact`
  and can delete the artifact explicitly; automatic TTL, file-count, and
  byte-count limits bound retention.

## Response Contracts

### Collection Response

```json
{
  "items": [],
  "paging": {
    "before": null,
    "after": null,
    "next": null
  },
  "summary": {
    "count": 0
  }
}
```

### Analysis Response

```json
{
  "scope": {
    "level": "campaign",
    "object_id": "123"
  },
  "metrics": {
    "spend": 0,
    "impressions": 0,
    "clicks": 0,
    "ctr": null,
    "cpc": null,
    "cpm": null,
    "conversions": null,
    "cpa": null,
    "roas": null
  },
  "findings": [],
  "evidence": [],
  "suggestions": [],
  "missing_signals": []
}
```

### Mutation Response

```json
{
  "ok": true,
  "action": "set_campaign_status",
  "target": {
    "campaign_id": "123"
  },
  "previous": {
    "status": "ACTIVE"
  },
  "current": {
    "status": "PAUSED"
  }
}
```

## Error Model

- `ConfigError`
- `AuthError`
- `ValidationError`
- `MetaApiError`
- `NotFoundError`
- `RateLimitError`
- `AsyncJobError`
- `UnsupportedFeatureError`

`RateLimitError` carries retry timing, Graph codes, operation, and parsed Meta
usage headers when available. `MetaApiError` carries allowlisted Graph code,
subcode, user title/message, trace id, error data, retryability, operation, and
`mutation_outcome_unknown`. Safe reads may retry bounded transient failures;
writes do not retry automatically and instruct callers to verify state when the
outcome may be ambiguous. Ambiguous mutation outcomes expose `retryable=false`
even when Meta marks the underlying failure as transient.

## Optimization-First Scope

The project should prioritize tools that answer questions like:

- What is driving spend and what is driving results?
- Which campaigns or ad sets are underperforming relative to peers?
- Are there creative fatigue or frequency issues?
- Which audiences or geo segments are wasting spend?
- Is budget constrained, misallocated, or pacing poorly?
- Which recommendations or opportunity signals should the operator inspect?
- What changed between two time windows?

## Priority Tool Groups

### Group A: Discovery and Context

These tools provide the account and entity context required before analysis.

- `list_ad_accounts`
- `get_ad_account`
- `list_campaigns`
- `get_campaign`
- `list_adsets`
- `get_adset`
- `list_ads`
- `get_ad`

### Group B: Performance and Diagnostics

These are the core v1 tools.

- `get_entity_insights`
- `get_performance_breakdown`
- `compare_time_ranges`
- `get_account_optimization_snapshot`
- `get_campaign_optimization_snapshot`
- `get_budget_pacing_report`
- `get_creative_performance_report`
- `get_creative_fatigue_report`
- `get_audience_performance_report`
- `get_delivery_risk_report`
- `get_learning_phase_report`
- `create_async_insights_report`
- `create_async_insights_report_batch`
- `get_async_insights_report`

### Group C: Social Feedback

- `get_ad_social_context`
- `list_ad_comments`
- `list_comment_replies`
- `list_page_recommendations`
- `get_ad_feedback_signals`

### Group D: Activity History

- `list_change_history`

### Group E: Audience and Planning

- `search_interests`
- `search_geo_locations`
- `estimate_audience_size`
- `get_reach_frequency_predictions`

### Group F: Recommendations and Docs

- `get_recommendations`
- `get_metrics_reference`
- `get_meta_object_model`
- `get_v25_notes`
- `get_optimization_playbook`

### Group G: Controlled Execution

These are deliberately narrow and should be secondary to analysis tools.

- `set_campaign_status`
- `set_adset_status`
- `set_ad_status`
- `update_campaign_budget`
- `update_adset_budget`
- `update_campaign_bid_strategy`
- `update_adset_bid_strategy`
- `update_adset_bid_amount`
- `create_campaign`
- `create_ad_set`
- `create_ad`
- `delete_campaign`
- `delete_adset`
- `delete_ad`

## Detailed Tool Spec

## Discovery Tools

### `list_ad_accounts`

Purpose:
List accessible ad accounts.

Inputs:

- `limit`
- `after`

Output:

- account id
- account name
- account status
- currency
- timezone
- business metadata when available

### `get_ad_account`

Purpose:
Fetch a single ad account with key metadata needed for downstream analysis.

Inputs:

- `account_id`

Output:

- account metadata
- spend summary fields when available
- attribution defaults when available

### `list_campaigns`

Purpose:
List campaigns for a given ad account.

Inputs:

- `account_id`
- `effective_status`
- `name_contains`
- `limit`
- `after`

Output:

- campaign list with ids, names, objectives, status, buying type, bid strategy,
  budgets when available

### `get_campaign`

Purpose:
Fetch campaign details relevant to optimization.

Inputs:

- `campaign_id`

Output:

- objective
- status
- buying type
- special categories
- budget fields
- optimization-relevant metadata

### `list_adsets`

Purpose:
List ad sets in account or campaign scope.

Inputs:

- `account_id`
- `campaign_id`
- `effective_status`
- `name_contains`
- `limit`
- `after`

Output:

- ad set metadata including optimization goal, billing event, bid strategy,
  bid amount, bid constraints, promoted object, targeting summary, schedule,
  and budget fields

### `get_adset`

Purpose:
Fetch an ad set with targeting and delivery-relevant metadata.

Inputs:

- `adset_id`

### `list_ads`

Purpose:
List ads by account, campaign, or ad set.

Inputs:

- `account_id`
- `campaign_id`
- `adset_id`
- `effective_status`
- `name_contains`
- `limit`
- `after`

### `get_ad`

Purpose:
Fetch ad details with optional creative summary.

Inputs:

- `ad_id`
- `include_creative_summary`

### `list_creatives`

Purpose:
List lightweight creative metadata for an ad account.

Inputs:

- `account_id`
- `limit`
- `after`
- optional `fields`

### `get_creative`

Purpose:
Read one creative by id, including full creative fields such as `url_tags`,
`asset_feed_spec`, `object_story_spec`, and `degrees_of_freedom_spec`.

Inputs:

- `creative_id`
- optional `fields`

## Activity History Tools

### `list_change_history`

Purpose:
Read Meta Ads activity logs / changelog rows for an account, campaign, ad set,
or ad.

Inputs:

- `level`: optional generic scope of `account`, `campaign`, `adset`, or `ad`
- `object_id`: optional generic object id when `level` is provided
- `account_id`: optional ad account context; object-scoped history derives it
  from the object when neither this nor `META_DEFAULT_ACCOUNT_ID` is configured
- `campaign_id`, `adset_id`, or `ad_id`: optional object scope routed through
  the ad-account activities edge with object filtering
- `since` and `until`: optional datetime filters; Meta defaults to the prior
  7 days through now when omitted
- `category`
- `business_id`
- `uid`: optional actor/user id filter
- `fields`
- `limit`
- `after`

Output:

- activity rows from the Meta `activities` edge
- parsed `extra_data_parsed` when `extra_data` is JSON encoded
- paging cursors
- scope, account parent, date-window, and filter summary

## Core Analysis Tools

### `get_insights`

Purpose:
Backward-compatible alias for older Claude calls that used `time_range`.
New clients should prefer `get_entity_insights`.

Inputs:

- same as `get_entity_insights`
- optional `time_range` object with `since` and `until`

Output:

- same envelope as `get_entity_insights`

### `get_entity_insights`

Purpose:
Return a normalized insights report for an account, campaign, ad set, or ad.

Underlying API surface:

- synchronous Meta insights edge
- use `create_async_insights_report` explicitly for queries too large for a
  synchronous request

Inputs:

- `level`: `account`, `campaign`, `adset`, `ad`
- `object_id`
- `date_preset` or `since` and `until`
- `fields`
- `action_types` to filter large Meta action arrays
- `flatten_actions` to promote requested counts or values such as `purchase`
  and `purchase_value` into scalar columns
- `breakdowns`
- `action_breakdowns`
- `time_increment`
- `use_unified_attribution_setting`
- `action_attribution_windows`
- `limit`
- `after`

Output:

- normalized rows
- summary totals
- extracted actions and conversion values
- derived KPIs when possible

### `summarize_actions`

Purpose:
Count appointments, purchases, leads, or custom Meta action types without
returning the full `actions` arrays.

Inputs:

- `level`
- `object_id`
- `action_types`
- `date_preset` or `since` and `until`
- optional `breakdowns`
- optional `include_rows`

Output:

- compact action totals
- matched action types and whether the action filter is `all` or `filtered`
- summary metrics
- paging, completeness, and a continuation hint when totals cover only one page
- Meta attribution notice for conversion-source caveats

### `get_performance_breakdown`

Purpose:
Return a focused breakdown report for optimization analysis.

Examples:

- age
- gender
- country / region
- placement / publisher platform
- device platform
- product id when applicable
- action destination when applicable

Inputs:

- `level`
- `object_id`
- `breakdown`
- `date_preset` or `since` and `until`
- `fields`
- `sort_by`
- `after`

Output:

- ranked segments
- summary totals
- derived KPIs by segment
- top and bottom performers
- paging and completeness

### `export_insights`

Purpose:
Return export-style JSON rows or CSV text while keeping large results
retrievable through the server overflow-artifact flow.

Inputs:

- the core `get_entity_insights` filters
- `format`: `json` or `csv`
- `limit` and optional `after`
- `inline_limit`
- `allow_large_output`

Output:

- structured rows or CSV text
- the effective reporting window
- Meta paging and the next cursor
- truncation guidance when `inline_limit` is reached
- for responses above the MCP inline cap, an overflow `export_id` that can be
  read in bounded chunks and deleted after retrieval

### `compare_time_ranges`

Purpose:
Compare two windows for the same entity and report material changes.

Inputs:

- `level`
- `object_id`
- `current_since`
- `current_until`
- `previous_since`
- `previous_until`
- `fields`

Output:

- current metrics
- previous metrics
- absolute deltas
- percentage deltas
- material changes
- evidence objects

### `get_account_optimization_snapshot`

Purpose:
Give the LLM a concise account-level optimization briefing.

This is a composite tool built from multiple API calls and derived analysis.

Inputs:

- `account_id`
- `date_preset` or `since` and `until`
- `compare_to_previous`
- `top_n`

Output:

- account summary
- top spend drivers
- top result drivers
- biggest inefficiencies
- budget concentration
- flags such as high frequency, weak CTR, high CPC, weak ROAS where measurable

### `get_campaign_optimization_snapshot`

Purpose:
Produce a campaign-level briefing that tells the LLM what to investigate next.

Inputs:

- `campaign_id`
- `date_preset` or `since` and `until`
- `top_n_adsets`
- `top_n_ads`

Output:

- campaign summary
- ad set ranking
- ad ranking
- creative concentration
- delivery risks
- suggested next tools

### `get_account_health_snapshot`

Purpose:
Collapse current, previous-window, and year-over-year account totals into one
call when explicit dates are provided.

Inputs:

- `account_id`
- `date_preset` or `since` and `until`
- `include_previous`
- `include_year_over_year`

Output:

- current account metrics
- previous-window comparison when explicit dates are present
- year-over-year comparison when explicit dates are present
- compact findings and evidence

### `detect_auction_overlap`

Purpose:
Provide a directional cannibalization screen by comparing campaign spend across
shared publisher-platform breakdowns.

Inputs:

- `account_id`
- optional `campaign_ids`
- `date_preset` or `since` and `until`
- `max_campaigns`
- `min_platform_spend`

Output:

- campaigns checked
- shared publisher-platform spend
- potential overlap findings
- missing-signal notes clarifying this is not person-level auction overlap

### `get_ad_feedback_signals`

Purpose:
Handle asks for ad comments, reviews, testimonials, customer feedback, negative
feedback, or quality rankings.

Inputs:

- optional `level` and `object_id`
- optional `account_id`, `campaign_id`, `adset_id`, or `ad_id`
- `date_preset` or `since` and `until`

Output:

- available quality ranking fields when scoped
- direct users toward `list_ad_comments` and `list_page_recommendations` for raw social feedback
- unavailable customer feedback score, negative-feedback counts, and commerce/catalog review-feed signals
- weak-quality findings when Meta ranking fields are below average

### `get_ad_social_context`

Purpose:
Resolve the Facebook Page post id or Instagram media id behind an ad creative
before reading comments. This avoids blind calls to multiple social edges.

Inputs:

- `ad_id`
- `resolve_creative`

Output:

- compact ad and creative identity
- available Facebook/Instagram feedback paths
- missing-path explanations
- permission notes and stable unavailable signals

### `list_ad_comments`

Purpose:
Read compact raw Facebook or Instagram comments for one ad, Page post id, or
Instagram media id.

Inputs:

- exactly one of `ad_id`, `object_story_id`, or `instagram_media_id`
- `surface` as `auto`, `facebook`, `instagram`, or `all`
- pagination and token-control options: `limit`, `after`, `include_replies`,
  `reply_limit`, `include_author`, and `max_message_chars`
- Facebook comment options: `comment_filter` and `order`

Output:

- compact normalized comments with message, time, likes, surface, and optional replies
- paging cursor for one surface, or per-surface paging when `surface="all"`
- API-call count and parent ids used
- structured unavailable output for permission-gated Page/Instagram surfaces

### `list_comment_replies`

Purpose:
Continue the paginated replies for one Facebook or Instagram comment.

Inputs:

- `comment_id`
- `surface`: `facebook` or `instagram`
- `limit` and optional `after`
- `include_author`
- `max_message_chars`

Output:

- compact normalized replies
- paging cursor for the next reply page
- API-call and edge metadata
- structured unavailable output for permission-gated surfaces

### `list_page_recommendations`

Purpose:
Read compact Facebook Page recommendations, reviews, or testimonials for an
owned Page.

Inputs:

- `page_id`
- `limit` and optional `after`
- `include_reviewer`
- `max_message_chars`

Output:

- compact recommendation rows
- paging cursor
- permission notes
- stable unavailable signals for customer feedback score and catalog review feeds

### `get_budget_pacing_report`

Purpose:
Tell the LLM whether an entity looks budget-constrained, underdelivering, or
misallocated.

Inputs:

- `level`
- `object_id`
- `date_preset` or `since` and `until`

Output:

- spend trend
- spend share by child entities
- budget fields
- pacing flags
- evidence for overspend / underspend / concentration

### `get_creative_performance_report`

Purpose:
Summarize creative performance across ads.

Inputs:

- `account_id` or `campaign_id` or `adset_id`
- `date_preset` or `since` and `until`
- `top_n`

Output:

- creative-level ranking
- delivery and engagement metrics
- video watch metrics when available
- outbound click metrics when available
- conversion metrics when available

### `get_creative_fatigue_report`

Purpose:
Identify likely fatigue or saturation signals.

Derived signals may include:

- rising frequency with falling CTR
- rising CPC with flat or falling conversion rate
- high spend concentrated in a small creative set

Inputs:

- `campaign_id` or `adset_id`
- `date_preset` or `since` and `until`
- `lookback_windows`

Output:

- fatigue flags
- impacted creatives
- evidence across windows
- confidence notes

### `get_audience_performance_report`

Purpose:
Explain how different audience segments are performing.

Underlying API surfaces:

- insights with breakdowns
- targeting search helpers

Inputs:

- `level`
- `object_id`
- `segment_by`
- `date_preset` or `since` and `until`

Output:

- ranked audience segments
- wasted-spend candidates
- strong segments
- concentration and skew notes

### `get_delivery_risk_report`

Purpose:
Highlight delivery issues and efficiency risks.

Inputs:

- `campaign_id` or `adset_id`
- `date_preset` or `since` and `until`

Output:

- delivery flags
- budget / bid / audience / creative hypotheses
- evidence
- missing signals if diagnosis is weak

### `get_learning_phase_report`

Purpose:
Expose learning status and nearby metadata that helps the LLM interpret unstable
performance.

Inputs:

- `campaign_id` or `adset_id`

Output:

- learning-related status where exposed
- optimization goal
- bid strategy
- recent delivery context

## Async Reporting Tools

### `create_async_insights_report`

Purpose:
Start an async insights job for larger queries.

Underlying API surface:

- Meta async insights jobs

Inputs:

- core date, breakdown, increment, field, and pagination filters
- `field_preset`: lean by default or explicit full compatibility fields
- `flatten_actions`: adds only the action fields required by requested scalar
  projections

Output:

- report run id
- status
- effective field preset and requested fields
- polling hint

### `create_async_insights_report_batch`

Purpose:
Submit up to ten independent breakdown reports in one bounded, sequential call.
The sequence stops early on rate limits or transient failures and returns both
created jobs and the unsubmitted count so callers never guess which jobs exist.

### `get_async_insights_report`

Purpose:
Poll an async insights job and fetch results when ready.

Inputs:

- `report_run_id`
- optional fetch-time `fields`, `action_types`, and `flatten_actions`
- `include_raw_actions`, false by default
- `limit`
- `after`
- `wait`, `wait_timeout_seconds`, and `poll_interval_seconds`

Output:

- job status
- normalized ready/terminal/progress/poll-after state
- compact rows if complete, with action arrays and maps omitted by default
- explicit wait-timeout state when bounded polling expires

## Audience and Planning Tools

### `search_interests`

Purpose:
Search targeting interests for audience planning.

Underlying API surface:

- Meta targeting search

Inputs:

- `query`
- `limit`

Output:

- interest ids
- names
- audience hints when available

### `search_geo_locations`

Purpose:
Search geo targeting options.

Inputs:

- `query`
- `location_types`
- `limit`

### `estimate_audience_size`

Purpose:
Estimate audience size for a proposed targeting specification.

Underlying API surface:

- Meta reach estimate

Inputs:

- `account_id`
- `targeting_spec`
- `optimization_goal`

Output:

- estimated audience size bounds
- supporting metadata

### `get_reach_frequency_predictions`

Purpose:
Expose planning signals for reach / frequency style scenarios when available.

Underlying API surface:

- Meta reach frequency prediction surface

Inputs:

- `account_id`
- optional filtering fields

Output:

- prediction objects
- reach and impression bounds where available

## Recommendations and Documentation Tools

### `get_recommendations`

Purpose:
Return recommendation and opportunity signals exposed by current Meta surfaces.

Important:

This tool must be implemented defensively. Recommendation and opportunity
surfaces may change by version, account type, or entitlement. Unsupported
surfaces should return a structured `UnsupportedFeatureError`, not a vague
failure.

Inputs:

- `account_id` or compatibility alias `object_id`
- optional `campaign_id`

Output:

- recommendations
- recommendation category
- severity or score if available
- evidence or linked entity ids

### `get_metrics_reference`

Purpose:
Provide compact documentation for commonly used insights metrics and breakdowns.

### `get_meta_object_model`

Purpose:
Provide compact docs for account, campaign, ad set, ad, and creative
relationships.

### `get_v25_notes`

Purpose:
Provide curated notes on supported v25-specific behavior, deprecations, and
implementation caveats.

### `get_optimization_playbook`

Purpose:
Provide compact guidance the LLM can use when interpreting diagnostics.

Examples:

- what high frequency may imply
- when CTR changes matter
- when budget concentration may be a problem
- what signals are too weak to act on

### `list_mutation_tools`

Purpose:
Expose the write catalog and common mutation paths without requiring a full
manifest dump.

Output:

- write tool list
- common pause, budget, bid, creation, and audience paths
- safety notes

## Controlled Execution Tools

These tools exist, but they are not the center of the product.

### `set_campaign_status`

Inputs:

- `campaign_id`
- `status`: `ACTIVE` or `PAUSED`

### `set_adset_status`

Inputs:

- `adset_id`
- `status`: `ACTIVE` or `PAUSED`

### `set_ad_status`

Inputs:

- `ad_id`
- `status`: `ACTIVE` or `PAUSED`

Deletion is intentionally separate from status updates:

- `delete_campaign(campaign_id)`
- `delete_adset(adset_id)`
- `delete_ad(ad_id)`

### `update_campaign_budget`

Inputs:

- `campaign_id`
- `daily_budget` or `lifetime_budget`

Rules:

- require exactly one budget field
- include previous and current values in the response
- do not silently coerce unsupported combinations

### `update_adset_budget`

Inputs:

- `adset_id`
- `daily_budget` or `lifetime_budget`

### Creation and bidding

- `create_campaign`, `create_ad_set`, and `create_ad` accept `validate_only`.
- `create_ad_set` accepts `bid_strategy` and structured `bid_constraints`.
- `create_ad` accepts either top-level `creative_id` or the familiar nested
  `creative.creative_id`/`creative.id` form, with conflicts rejected.
- `update_adset_bid_strategy` accepts both bid amount and bid constraints.

## Derived Metrics and Heuristics

When source fields are present, compute:

- `ctr`
- `link_ctr`
- `cpc`
- `cpm`
- `cvr`
- `cpa`
- `roas`
- `spend_share`
- `result_share`
- `frequency_change`

Every derived metric must include its source fields in the returned evidence.

Example:

```json
{
  "metric": "ctr",
  "value": 0.0182,
  "formula": "clicks / impressions",
  "inputs": {
    "clicks": 182,
    "impressions": 10000
  }
}
```

## Optimization Findings Model

Analysis tools should emit machine-readable findings with:

- `type`
- `severity`
- `confidence`
- `summary`
- `evidence`
- `affected_entities`
- `next_actions`

Example finding types:

- `high_frequency_declining_ctr`
- `high_spend_low_conversion`
- `budget_concentration`
- `creative_fatigue_risk`
- `segment_underperformance`
- `delivery_instability`
- `insufficient_data`

## API Usage Priorities

The implementation should spend most of its effort on these Meta surfaces:

- insights edge with breakdowns
- async insights jobs
- targeting search
- reach estimate
- reach frequency predictions
- recommendation / opportunity surfaces where exposed in v25-era accounts

The implementation should spend less effort on:

- broad entity mutation coverage
- image downloading and asset management
- browser auth flows
- hosted auth middleware

## Resources

The server should expose read-only resources for LLM grounding:

- `meta://docs/object-model`
- `meta://docs/insights-metrics`
- `meta://docs/v25-notes`
- `meta://docs/optimization-playbook`

## Packaging

Proposed package name:

- `meta-ads-fastmcp`

Entrypoints:

- `meta_ads_mcp.stdio:main`
- `meta_ads_mcp.server:main`

Suggested console script:

- `run-meta-ads-mcp`

## Suggested Dependencies

- `fastmcp==3.4.5`
- `mcp>=1.26.0`
- `httpx[http2]>=0.28.1`
- `pydantic>=2`
- `python-dotenv>=1`
- `pytest>=9`
- `pytest-asyncio>=1`

## Testing Requirements

- Unit-test all tools with mocked Graph API responses.
- Add contract tests for response envelopes.
- Test pagination helpers.
- Test async insights job flows.
- Test failure modes:
  missing auth, invalid fields, unsupported breakdowns, rate limits, partial
  data, empty data.
- Enforce a catalog-wide schema invariant that every public array-of-string
  input also advertises and accepts a comma-separated string.
- Verify safe reads retry bounded transient failures while create, update, and
  delete calls do not retry after ambiguous outcomes.
- Keep live integration tests opt-in behind env vars.

## Phasing

### Phase 1

- project scaffold
- stdio entrypoint
- auth and config
- Graph API client
- discovery tools
- core insights tool
- time-range comparison
- account and campaign optimization snapshots
- breakdown analysis
- basic docs resources
- unit tests

### Phase 2

- async insights jobs
- creative performance and fatigue analysis
- audience performance reports
- budget pacing report
- targeting and reach estimate tools
- reach frequency predictions

### Phase 3

- recommendation / opportunity tools
- narrow execution tools
- optional HTTP hardening
- broader v25 support where justified

## Open Decisions

- Final package name
- Whether to include appsecret proof support in v1 or defer it
- Exact scope of recommendation / opportunity tools, since those surfaces may
  vary by entitlement and version
- Whether prompts should ship in v1 or later

## External Version Notes

These assumptions should be re-verified immediately before implementation:

- Meta Marketing API target version is `v25.0`
- FastMCP latest stable is `3.1.0` as of March 9, 2026

## Source Notes

This spec is based on current official or primary-source references available on
March 7, 2026, including:

- Meta Marketing API documentation and changelog
- Meta official Postman Marketing API workspace
- FastMCP official documentation and PyPI release metadata
