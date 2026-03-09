# Meta Ads FastMCP

Optimization-first MCP server for Meta Ads, designed for Claude Code, Claude Desktop, Gemini CLI, and similar MCP clients.

Built on FastMCP `3.1.0` with direct Meta Marketing API access, pinned to `v25.0` by default.

This is not an officially supported Meta product.

## Features

- `71` MCP tools covering discovery, reporting, diagnostics, targeting, research, auth helpers, and controlled writes
- `stdio`-first local setup for Claude Code and other CLI MCP clients
- FastMCP `3.1` tool search enabled to reduce upfront tool-context usage in Claude-compatible clients
- Meta Marketing API `v25.0` by default
- Built-in docs and MCP resources for object model, metrics, optimization guidance, and v25 notes
- Optimization-first tool surface: not just CRUD, but pacing, fatigue, audience, and snapshot diagnostics
- Narrow write tools for campaign, ad set, ad, audience, and creative workflows

## Available Tools

### Discovery

- `list_ad_accounts`
- `get_ad_account`
- `get_account_pages`
- `list_instagram_accounts`
- `list_campaigns`
- `get_campaign`
- `list_adsets`
- `get_adset`
- `list_ads`
- `get_ad`
- `list_audiences`
- `list_creatives`

### Analysis

- `get_entity_insights`
- `get_performance_breakdown`
- `compare_time_ranges`
- `compare_performance`
- `export_insights`
- `create_async_insights_report`
- `get_async_insights_report`

### Optimization

- `get_account_optimization_snapshot`
- `get_campaign_optimization_snapshot`
- `get_budget_pacing_report`
- `get_creative_performance_report`
- `get_creative_fatigue_report`
- `get_audience_performance_report`
- `get_delivery_risk_report`
- `get_learning_phase_report`
- `get_recommendations`

### Planning And Targeting

- `search_interests`
- `get_interest_suggestions`
- `validate_interests`
- `search_geo_locations`
- `get_targeting_categories`
- `search_behaviors`
- `search_demographics`
- `estimate_audience_size`
- `get_reach_frequency_predictions`

### Writes

- `create_campaign`
- `update_campaign`
- `delete_campaign`
- `create_ad_set`
- `create_ad`
- `set_campaign_status`
- `set_adset_status`
- `set_ad_status`
- `update_campaign_budget`
- `update_adset_budget`
- `create_custom_audience`
- `create_lookalike_audience`
- `update_custom_audience`
- `delete_audience`
- `create_ad_creative`
- `update_creative`
- `delete_creative`

### Creative Ops

- `get_ad_image`
- `preview_ad`
- `upload_creative_asset`
- `setup_ab_test`

### Public Research

- `search_ads_archive`

### Auth Helpers

- `generate_auth_url`
- `exchange_code_for_token`
- `refresh_to_long_lived_token`
- `generate_system_user_token`
- `get_token_info`
- `validate_token`

### Docs And Utility

- `get_meta_object_model`
- `get_metrics_reference`
- `get_v25_notes`
- `get_optimization_playbook`
- `health_check`
- `get_capabilities`

FastMCP `3.1` also exposes dynamic search tools at runtime:

- `search_tools`
- `call_tool`

Those let Claude discover the right hidden tool on demand instead of loading the full tool catalog into context up front.

## MCP Resources

- `meta://docs/object-model`
- `meta://docs/insights-metrics`
- `meta://docs/v25-notes`
- `meta://docs/optimization-playbook`
- `meta://docs/tool-routing`

## Quick Start

### Prerequisites

- Python `3.12+`
- [`uv`](https://github.com/astral-sh/uv)
- A Meta access token with the scopes needed for the tools you want to use

### 1. Configure Credentials

Set your access token:

```bash
export META_ACCESS_TOKEN='YOUR_META_ACCESS_TOKEN'
```

Optional settings:

```bash
export META_DEFAULT_ACCOUNT_ID='act_1234567890'
export META_API_VERSION='v25.0'
export META_APP_ID='YOUR_APP_ID'
export META_APP_SECRET='YOUR_APP_SECRET'
export META_REDIRECT_URI='https://example.com/callback'
```

You can also place these in a local `.env` file at the repo root.

### 2. Install And Run

#### Option A: Claude Code (recommended)

From a local clone:

```bash
claude mcp add --transport stdio MetaAds \
  --env META_ACCESS_TOKEN=YOUR_META_ACCESS_TOKEN \
  --env META_DEFAULT_ACCOUNT_ID=act_1234567890 \
  -- uv run --directory /path/to/meta-ads-mcp -m meta_ads_mcp.stdio
```

Or use the project-scoped config in [.mcp.json](/Users/jefflai/Documents/GitHub/meta-ads-mcp/.mcp.json).

Type `/mcp` in Claude Code to verify the server is connected.

Because FastMCP `3.1` tool search is enabled, Claude may show `search_tools` and `call_tool` as part of the active runtime surface.

#### Option B: Gemini CLI

Add this to your Gemini MCP config:

```json
{
  "mcpServers": {
    "MetaAds": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/meta-ads-mcp",
        "-m",
        "meta_ads_mcp.stdio"
      ],
      "env": {
        "META_ACCESS_TOKEN": "YOUR_META_ACCESS_TOKEN",
        "META_DEFAULT_ACCOUNT_ID": "act_1234567890"
      }
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

## Usage Examples

Once connected, you can ask natural-language questions like:

### Discovery

```text
List my accessible ad accounts
Show me the campaigns in act_1234567890
What Instagram accounts are linked to this ad account?
Find the Pages I can use for creative setup
```

### Reporting And Analysis

```text
Show me account-level insights for the last 7 days
Compare these two campaigns on ROAS, CPA, and CTR
Break down campaign performance by country
Export ad-level insights for the last 30 days as CSV
```

### Optimization

```text
Give me an optimization snapshot for this account
Which creatives are fatiguing in this campaign?
What are the biggest delivery risks right now?
Which audience segments are underperforming?
```

### Planning And Research

```text
Estimate audience size for this targeting spec
Find interest suggestions related to running and fitness
Search demographic targeting categories for new parents
Search the Meta ads archive for competitor creatives
```

### Writes

```text
Create a paused campaign for this account
Pause this campaign
Update this ad set budget to 75 dollars daily
Create an ad in this ad set using creative 123
```

## Project Structure

```text
meta-ads-mcp/
├── meta_ads_mcp/
│   ├── coordinator.py
│   ├── stdio.py
│   ├── server.py
│   ├── config.py
│   ├── auth.py
│   ├── errors.py
│   ├── graph_api.py
│   ├── normalize.py
│   ├── schemas.py
│   ├── tools/
│   │   ├── ads.py
│   │   ├── audiences.py
│   │   ├── auth_tools.py
│   │   ├── campaigns.py
│   │   ├── creatives.py
│   │   ├── diagnostics.py
│   │   ├── discovery.py
│   │   ├── docs.py
│   │   ├── execution.py
│   │   ├── insights.py
│   │   ├── recommendations.py
│   │   ├── research.py
│   │   ├── targeting.py
│   │   └── utility.py
│   └── context/
├── tests/
├── CLAUDE.md
├── GEMINI.md
├── SPEC.md
├── pyproject.toml
└── README.md
```

## Development

```bash
uv sync
python3 -m pytest -q
```

### Project Notes

- Keep Graph API calls isolated in `meta_ads_mcp/graph_api.py`
- Prefer optimization and diagnostics tools before mutations
- Return dicts and lists rather than serialized JSON strings where possible
- Treat all account IDs and object IDs as strings
- Keep the implementation aligned with Marketing API `v25.0`

## Current Scope

This server is optimized for questions like:

- What is driving spend and results?
- What changed versus the previous period?
- Which campaigns, ad sets, creatives, or audiences look inefficient?
- What should I inspect before changing budgets or status?
- What targeting options and related categories should I evaluate next?

It is not intended to mirror the entire Marketing API surface area one-to-one.

## License

The package metadata currently declares `MIT` in [pyproject.toml](/Users/jefflai/Documents/GitHub/meta-ads-mcp/pyproject.toml).
