# Meta Ads FastMCP

Optimization-first MCP server for Meta Ads, designed for Claude Code, Claude
Desktop, Gemini CLI, and similar local MCP clients.

## Status

This repo currently implements a Phase 1 scaffold:

- FastMCP server entry points for `stdio` and HTTP
- direct Marketing API client on `httpx`
- optimization-oriented discovery, insights, diagnostics, targeting,
  recommendations, docs, and narrow execution tools
- local context docs and unit tests

## Quick Start

1. Set `META_ACCESS_TOKEN`
2. Optionally set `META_DEFAULT_ACCOUNT_ID`
3. Run:

```bash
uv run -m meta_ads_mcp.stdio
```

For Claude Code, use the config in [.mcp.json](/Users/jefflai/Documents/GitHub/meta-ads-mcp/.mcp.json).

## Primary Focus

This server is meant to help an LLM answer questions like:

- what is driving spend and results?
- what changed vs the previous period?
- which campaigns, ad sets, creatives, or audience segments are inefficient?
- where are the strongest optimization opportunities?

