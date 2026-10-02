# Insights Metrics Reference

Core metrics:

- `spend`
- `impressions`
- `reach`
- `clicks`
- `ctr`
- `cpc`
- `cpm`
- `frequency`
- `actions`
- `action_values`
- `quality_ranking`
- `engagement_rate_ranking`
- `conversion_rate_ranking`

Date presets:

- Prefer explicit `since` and `until` for audited period comparisons.
- Use `maximum` for Meta's all-available-window preset. The MCP also accepts common aliases such as `lifetime`, `all_time`, `ytd`, and `last_30_days`.

Action counts:

- Use `summarize_actions` for appointments, purchases, leads, and custom action types when the full `actions` array would be noisy.
- Meta action counts are attribution-platform numbers; reconcile purchases against Snowplow/Snowflake when purchase truth matters.

Explicit click/view attribution reads:

- Use `get_entity_insights` with the same scope, dates, and fields for each query. Set `use_unified_attribution_setting=false` and request one `action_attribution_windows` value per call, such as `["7d_click"]` or `["1d_view"]`, rather than mixing windows into one total.
- Request `flatten_actions=["purchase","purchase_value"]` and `include_raw_actions=false` for compact scalar results. These controls already exist; no separate attribution tool is needed.
- These reads compare Meta-attributed outcomes under the requested settings. They do not reconstruct historical ad set attribution-setting changes; use activity history or retained warehouse snapshots for that evidence.

Useful breakdowns:

- `age`
- `gender`
- `country`
- `region`
- `publisher_platform`
- `platform_position`
- `device_platform`
