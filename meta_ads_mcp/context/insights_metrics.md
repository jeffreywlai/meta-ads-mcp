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

Useful breakdowns:

- `age`
- `gender`
- `country`
- `region`
- `publisher_platform`
- `platform_position`
- `device_platform`
