# Meta Ads FastMCP

## Commands

```bash
uv sync --extra dev
uv run -m meta_ads_mcp.stdio
uv run --extra dev pytest
```

## Project Notes

- Prefer optimization and diagnostics tools over broad mutation coverage.
- Keep Graph API calls isolated in `meta_ads_mcp/graph_api.py`.
- Return dicts and lists, not serialized JSON strings.
- Keep IDs as strings.
