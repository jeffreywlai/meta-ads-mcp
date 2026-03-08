# Meta Ads FastMCP

Example Gemini MCP config:

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
        "META_ACCESS_TOKEN": "YOUR_TOKEN"
      }
    }
  }
}
```

