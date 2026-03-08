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
- FastMCP latest stable `2.14.5`
- Python `3.12+`

FastMCP `3.0.0rc1` exists, but v1 should avoid release candidates. Re-check the
latest stable FastMCP version immediately before scaffolding the project.

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
- Framework: FastMCP stable `2.14.5` at project start
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
│   ├── graph_api.py
│   ├── pagination.py
│   ├── normalize.py
│   ├── diagnostics.py
│   ├── schemas.py
│   ├── tools/
│   │   ├── discovery.py
│   │   ├── insights.py
│   │   ├── diagnostics.py
│   │   ├── targeting.py
│   │   ├── recommendations.py
│   │   ├── creatives.py
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

Defines the shared FastMCP instance and server instructions.

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
- timeout handling
- paging helpers
- sync insights calls
- async insights job creation and polling
- Meta error parsing

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
- Writes return `{ok, action, target, previous, current}`.
- Errors raise typed exceptions, not free-form error text.
- Any derived metric must include the raw components used to compute it.
- If a metric is not available, omit it and record why in `missing_signals`.

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
- `get_async_insights_report`

### Group C: Audience and Planning

- `search_interests`
- `search_geo_locations`
- `estimate_audience_size`
- `get_reach_frequency_predictions`

### Group D: Recommendations and Docs

- `get_recommendations`
- `get_metrics_reference`
- `get_meta_object_model`
- `get_v25_notes`
- `get_optimization_playbook`

### Group E: Controlled Execution

These are deliberately narrow and should be secondary to analysis tools.

- `set_campaign_status`
- `set_adset_status`
- `set_ad_status`
- `update_campaign_budget`
- `update_adset_budget`

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
- `limit`
- `after`

Output:

- ad set metadata including optimization goal, billing event, bid strategy,
  targeting summary, schedule, budget fields

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
- `limit`
- `after`

### `get_ad`

Purpose:
Fetch ad details with optional creative summary.

Inputs:

- `ad_id`
- `include_creative_summary`

## Core Analysis Tools

### `get_entity_insights`

Purpose:
Return a normalized insights report for an account, campaign, ad set, or ad.

Underlying API surface:

- Meta insights edge
- sync or async depending on payload size

Inputs:

- `level`: `account`, `campaign`, `adset`, `ad`
- `object_id`
- `date_preset` or `since` and `until`
- `fields`
- `breakdowns`
- `action_breakdowns`
- `time_increment`
- `use_unified_attribution_setting`
- `action_attribution_windows`
- `limit`

Output:

- normalized rows
- summary totals
- extracted actions and conversion values
- derived KPIs when possible

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

Output:

- ranked segments
- summary totals
- derived KPIs by segment
- top and bottom performers

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

- same core filters as `get_entity_insights`

Output:

- report run id
- status
- polling hint

### `get_async_insights_report`

Purpose:
Poll an async insights job and fetch results when ready.

Inputs:

- `report_run_id`
- `limit`
- `after`

Output:

- job status
- progress
- rows if complete

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

- `account_id`
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

- `fastmcp==2.14.5`
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
- FastMCP latest stable is `2.14.5` as of March 7, 2026

## Source Notes

This spec is based on current official or primary-source references available on
March 7, 2026, including:

- Meta Marketing API documentation and changelog
- Meta official Postman Marketing API workspace
- FastMCP official documentation and PyPI release metadata
