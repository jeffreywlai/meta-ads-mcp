# Meta Ads MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP 3.1](https://img.shields.io/badge/FastMCP-3.1-green.svg)](https://github.com/jlowin/fastmcp)
[![Meta Marketing API v25.0](https://img.shields.io/badge/Meta%20Marketing%20API-v25.0-blue.svg)](https://developers.facebook.com/docs/marketing-apis/)

**An optimization-first MCP server that bridges LLMs with the Meta Marketing API — 79 tools for querying, managing, and optimizing your ad accounts through natural language.**

> Ask Claude or Gemini to "show me which creatives are fatiguing" or "give me an optimization snapshot for this account" — and it just works.

**This is not an officially supported Meta product.**

## ✨ Features

- 📊 **79 Tools** — Discovery, reporting, diagnostics, targeting, research, auth helpers, and controlled writes
- 🔍 **Optimization-First** — Not just CRUD: pacing, fatigue, audience, and snapshot diagnostics built in
- 📖 **Built-in Docs** — Object model, metrics, optimization playbook, and v25 notes available as tools and MCP resources
- 🎯 **Full Targeting Suite** — Interest, behavior, demographic, and geo search with audience size estimation
- 🔑 **Auth Helpers** — Generate tokens, exchange codes, refresh tokens, and validate scopes
- 🖼️ **Creative Ops** — Preview ads, upload assets, and set up A/B tests
- 🔎 **Tool Search** — FastMCP 3.1 tool search lets the LLM discover tools on demand instead of loading all 79 up front
- 🖥️ **Works Everywhere** — Claude Code, Claude Desktop, Gemini CLI, or any MCP client

## 📋 Available Tools (79)

### 🔍 Discovery

| Tool | Description |
|------|-------------|
| `list_ad_accounts` | List all accessible ad accounts |
| `get_ad_account` | Get details for a specific ad account |
| `get_account_pages` | Get Pages linked to an ad account |
| `list_instagram_accounts` | List Instagram accounts linked to an ad account |
| `list_campaigns` | List campaigns in an ad account |
| `get_campaign` | Get details for a specific campaign |
| `list_adsets` | List ad sets in a campaign or account |
| `get_adset` | Get details for a specific ad set |
| `list_ads` | List ads in an ad set or account |
| `get_ad` | Get details for a specific ad |
| `list_audiences` | List custom and lookalike audiences |
| `list_creatives` | List ad creatives in an account |

### 📊 Analysis

| Tool | Description |
|------|-------------|
| `get_entity_insights` | Get performance insights for any entity |
| `get_performance_breakdown` | Break down performance by dimension |
| `compare_time_ranges` | Compare metrics across two time ranges |
| `compare_performance` | Compare metrics across multiple entities |
| `export_insights` | Export insights as CSV or structured data |
| `create_async_insights_report` | Create a large async insights report |
| `get_async_insights_report` | Poll and retrieve an async report |

### ⚡ Optimization

| Tool | Description |
|------|-------------|
| `get_account_optimization_snapshot` | Full account health and optimization summary |
| `get_campaign_optimization_snapshot` | Campaign-level optimization summary |
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
| `create_ad_set` | Create a new ad set |
| `create_ad` | Create a new ad |
| `set_campaign_status` | Pause or enable a campaign |
| `set_adset_status` | Pause or enable an ad set |
| `set_ad_status` | Pause or enable an ad |
| `update_campaign_budget` | Update a campaign's budget |
| `update_adset_budget` | Update an ad set's budget |
| `update_adset_bid_amount` | Update an ad set's bid amount |
| `update_campaign_bid_strategy` | Update a campaign's bidding strategy |
| `update_adset_bid_strategy` | Update an ad set's bidding strategy |
| `create_custom_audience` | Create a custom audience |
| `create_lookalike_audience` | Create a lookalike audience |
| `update_custom_audience` | Update a custom audience |
| `delete_audience` | Delete an audience |
| `create_ad_creative` | Create a new ad creative |
| `update_creative` | Update an ad creative |
| `delete_creative` | Delete an ad creative |

### 🖼️ Creative Ops

| Tool | Description |
|------|-------------|
| `get_ad_image` | Get ad image details |
| `preview_ad` | Preview an ad in various formats |
| `upload_creative_asset` | Upload an image or video asset |
| `setup_ab_test` | Set up an A/B test |

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

### 📖 Docs & Utility

| Tool | Description |
|------|-------------|
| `get_meta_object_model` | Meta Ads object model reference |
| `get_metrics_reference` | Insights metrics reference |
| `get_v25_notes` | Marketing API v25.0 release notes |
| `get_optimization_playbook` | Optimization best practices playbook |
| `health_check` | Server health check |
| `get_capabilities` | List server capabilities |

FastMCP 3.1 also exposes dynamic search tools at runtime:

| Tool | Description |
|------|-------------|
| `search_tools` | Search for tools by keyword |
| `call_tool` | Call a discovered tool by name |

These let the LLM discover the right tool on demand instead of loading the full catalog into context up front.

## 📚 MCP Resources

| Resource | Description |
|----------|-------------|
| `meta://docs/object-model` | Meta Ads object hierarchy |
| `meta://docs/insights-metrics` | Available insights metrics |
| `meta://docs/v25-notes` | v25.0 API release notes |
| `meta://docs/optimization-playbook` | Optimization best practices |
| `meta://docs/tool-routing` | Tool routing guide |

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or `pipx`
- A Meta access token with the scopes needed for the tools you want to use

### 1. Get Your Credentials

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

You can also place these in a `.env` file at the repo root.

Don't have a token? Use the built-in auth helper tools (`generate_auth_url` → `exchange_code_for_token` → `refresh_to_long_lived_token`) to create one interactively.

### 2. Install & Run

#### Option A: Claude Code (recommended)

```bash
claude mcp add --transport stdio MetaAds \
  --env META_ACCESS_TOKEN=YOUR_META_ACCESS_TOKEN \
  --env META_DEFAULT_ACCOUNT_ID=act_1234567890 \
  -- uv run --directory /path/to/meta-ads-mcp -m meta_ads_mcp.stdio
```

Or use the project-scoped config in `.mcp.json`.

Type `/mcp` in Claude Code to verify the server is connected.

#### Option B: Gemini CLI

Add to your Gemini MCP configuration:

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
"Create an ad in this ad set using creative 123"
```

## 🏗️ Project Structure

```
meta-ads-mcp/
├── meta_ads_mcp/
│   ├── coordinator.py         # Shared FastMCP instance
│   ├── stdio.py               # Server entry point (Claude Code / stdio)
│   ├── server.py              # Server entry point (Gemini / SSE)
│   ├── config.py              # Configuration and env loading
│   ├── auth.py                # Auth utilities
│   ├── errors.py              # Error handling
│   ├── graph_api.py           # Meta Graph API client
│   ├── normalize.py           # Response normalization
│   ├── schemas.py             # Pydantic schemas
│   ├── tools/
│   │   ├── ads.py             # Ad management
│   │   ├── audiences.py       # Audience management
│   │   ├── auth_tools.py      # Auth helper tools
│   │   ├── campaigns.py       # Campaign & ad set management
│   │   ├── creatives.py       # Creative management
│   │   ├── diagnostics.py     # Optimization & diagnostics
│   │   ├── discovery.py       # Account & entity discovery
│   │   ├── docs.py            # Built-in documentation tools
│   │   ├── execution.py       # Write operations
│   │   ├── insights.py        # Reporting & analysis
│   │   ├── recommendations.py # AI recommendations
│   │   ├── research.py        # Ad Library research
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
uv sync                          # Install dependencies
uv run pytest                    # Run tests
```

### Project Notes

- Keep Graph API calls isolated in `meta_ads_mcp/graph_api.py`
- Prefer optimization and diagnostics tools before mutations
- Return dicts and lists rather than serialized JSON strings
- Treat all account IDs and object IDs as strings
- Keep the implementation aligned with Marketing API v25.0

## 📄 License

Licensed under the [MIT License](LICENSE).

## 📬 Contact

Questions, suggestions, or feedback? [Open an issue](https://github.com/jeffreywlai/meta-ads-mcp/issues).

---

**Built with [FastMCP 3.1](https://github.com/jlowin/fastmcp) and [Meta Marketing API v25.0](https://developers.facebook.com/docs/marketing-apis/)**
