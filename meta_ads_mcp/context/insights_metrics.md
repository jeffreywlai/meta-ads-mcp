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

- Use `get_entity_insights` with explicit dates and `fields=["actions","action_values"]`. Set `use_unified_attribution_setting=false`, request `action_attribution_windows=["7d_click","1d_view"]`, and keep `include_raw_actions=true`.
- For each selected purchase action type, read the named `7d_click` and `1d_view` keys from `actions` (counts) and `action_values` (values). Do not substitute the generic `value`: a live v26 check returned different values even when only one window was requested. Meta's [v26 action stats schema](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.0/facebook_business/adobjects/adsactionstats.py) defines these as separate fields.
- Core metrics, action maps, and `flatten_actions` preserve Meta's generic `value`; they are not window-specific projections. Compact mode removes the raw arrays, including their named attribution fields, so do not use it for this split. No separate attribution tool is needed.
- Do not sum overlapping purchase action aliases or assume attribution windows are additive. These reads also do not reconstruct historical ad set attribution-setting changes; use activity history or retained warehouse snapshots for that evidence.

Useful breakdowns:

- `age`
- `gender`
- `country`
- `region`
- `publisher_platform`
- `platform_position`
- `device_platform`
