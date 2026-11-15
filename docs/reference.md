# Operating reference

Detailed behaviour of the server's tools, moved here from the README. The tool
tables and setup steps are in the [README](../README.md).

## SDK schema audit

The repository includes an SDK-backed compatibility audit that compares local
default field projections with Meta's generated Python Business SDK schemas:

```bash
uv run audit-meta-sdk-schema \
  --sdk-repo /path/to/facebook-python-business-sdk \
  --base-ref 25.0.3 \
  --target-ref 26.0.2
```

The default API is `v26.0`; the audit target is SDK `26.0.2`. These are
different version numbers. See [the v26 compatibility audit](v26_compatibility_audit.md)
for schema findings and the read-only live validation gate.

## Version 26 options

With `META_API_VERSION=v26.0` or newer, use
`get_native_optimization_signals` for entity-native delivery, learning, issue,
automation, and recommendation payloads. `get_entity_insights` can opt into
the v26 `instagram_profile_follow` metric with
`include_instagram_profile_follow=true`. Both options fail early if an explicit
environment override selects v25. Restart the MCP to pick up a version change.

`get_recommendations` and the five typed opportunity tools accept Meta-native
`recommendation_names` and `recommendation_stages` filters, as lists or CSV.
For example, `recommendation_stages=["PCR"]` limits Meta's results before any
local category filtering. Omitting the filters keeps the existing broad scan.
Stage codes (`MFR`, `PCR`, `PFR`) are passed through without invented meanings.
Providing either filter requires API v26 or newer and fails before a request
under an explicit v25 override; unfiltered recommendation scans remain available.

New SDK fields and breakdowns use existing generic inputs: for example,
`get_entity_insights(..., fields=["shop_clicks"])` or
`get_entity_insights(..., breakdowns=["placement_path"])`. Campaign reads now
also accept custom `fields`. These examples show input paths, not guaranteed
account eligibility or valid combinations. The live v26 account rejects the
SDK-listed campaign `bid_constraints` field, so it is not a default projection.
The ad persona field also requires permissions unavailable to the tested token.
Use `get_v26_notes` for the full relevant additions and compatibility caveats.

## Oversized responses

Overflow settings are optional. Oversized MCP responses are saved as complete,
owner-only JSON artifacts and replaced inline by an opaque `export_id`. Use the
visible `call_tool` proxy with `name="read_overflow_artifact"` and the returned
`export_id`, repeating with `next_item_offset` for parsed item pages (the default
when the result has items or rows). Use `offset=0` and then each `next_offset`
when the complete original JSON bytes are needed. Guessed continuations are
rejected; retain the returned cursor. A single item too large for one response
remains available through byte mode. Retrieve the complete response
from local or remote MCP transports. Then use `call_tool` with
`name="delete_overflow_artifact"`. When `META_EXPORT_DIR` is omitted, the
server uses the operating system's temporary `meta-ads-mcp-exports` directory.
The 64,000-byte threshold measures the serialized inline MCP result: Meta's complete
result is retained in the artifact and is not truncated or discarded.
For oversized results through the schema-free `call_tool` wrapper, identical
JSON text and structured output are delivered as one compact text copy when
that fits. Direct tools with output schemas retain their structured content.
Notices distinguish the measured MCP result size from the smaller archive size.
Parsed item pages reuse verified decoded archives in a cache limited to four
artifacts and a conservative 64 MiB decoded-payload budget. File replacement,
integrity failure, deletion, and expiry invalidate cached data. Larger decoded
artifacts remain readable but bypass this cache, so their pages can take longer.
Archive schema v2 uses compact JSON and stores identical JSON compatibility
text only once, under `tool_result.structured_content`. Distinct or annotated
text remains intact. Overflow notices include archive size and retention.
Artifacts expire after 24 hours by default, and the store retains at most 100
response artifacts totaling 1 GB of JSON payload data; small private integrity
manifests are stored alongside those payloads. The three retention variables
(`META_EXPORT_TTL_SECONDS`, `META_EXPORT_MAX_FILES`, `META_EXPORT_MAX_BYTES`) override those defaults.
If the host lacks secure directory-relative file operations and process locks,
archival fails closed and the tool returns guidance to narrow the request.

## Reporting

Synchronous `get_entity_insights` (and its `get_insights` alias) omits duplicate
raw action arrays by default, keeping numeric action maps and derived metrics.
With `flatten_actions=["purchase"]`, it returns a named `purchase` column and
omits the full action maps. Unrequested null padding is removed; scalar
projections also omit metrics that duplicate top-level values. Custom action counts and values remain
available without an extra opt-in. Set `include_raw_actions=true` to retain raw
arrays, including named attribution-window values needed for click/view
comparisons; generic maps are not window-specific totals. `summarize_actions`
and JSON/CSV exports retain their existing action output. Existing callers that
read raw action arrays must now opt in. Dimensional `action_breakdowns` (anything
beyond `action_type`, such as `action_device`) require `include_raw_actions=true`;
read the raw records for those dimensions, not the scalar maps or derived totals.
Compact calls fail before making a request instead of discarding the dimensions.

For placement reporting across copies of an ad, use `level="ad"` with the
parent `object_id="act_ACCOUNT_ID"` (or a campaign/ad set ID),
`breakdowns=["publisher_platform","platform_position"]`, and native
`filtering=[{"field":"ad.name","operator":"CONTAIN","value":"NAME_PREFIX"}]`.
`level` already selects row granularity; no separate `row_level` is needed.
Filters also work with exports and async report creation. Synchronous summaries
cover the returned page only: check `summary.complete` and continue with
`paging.after` before treating the result as exhaustive.
Alternatively, set `fetch_all=true` to collect up to 1,000 rows from the first
page onward in one call (also supported by `get_insights` and `export_insights`).
The server combines metrics after pagination, or fails explicitly if the scan
cannot finish. Narrow the query or use async reports for larger results.

Async report creation returns ready-to-use `poll_arguments`. Pass the whole
object to `get_async_insights_report` on every fetch so `flatten_actions` and
raw dimensional action settings survive polling. These projection options are
explicit, not hidden process state. To summarize a large catalog report, call
`summarize_async_insights_report(report_run_id="REPORT", group_by=["date_start"],
metrics=["spend"])`. It sums pages with decimal arithmetic and returns grouped
rows, not the whole catalog. Spend totals use exact decimal strings in major
currency units; impression/click totals use integers. Only additive spend/impression/click metrics are
supported; reach, ratios, and action counts cannot safely be summed this way.
The call is bounded by time and page count. When `next_page_arguments` is present,
use it to continue the scan; each result covers only its returned pages. Combine
the group totals across those ranges. If the report is still running, poll again
with the original arguments. A missing metric yields a
null total instead of silently contributing zero. An independent account report
is still needed as the denominator for catalog share of total account spend.

## Creative fatigue, ad search and asset reads

For an account-wide fatigue sweep, call `get_creative_fatigue_report` with
`account_id` or `level="account", object_id="act_ACCOUNT_ID"`. It reads both
windows at ad granularity, follows pagination, and includes ad/campaign/ad set
names. Findings use a transparent ordering: minimum impressions across both
windows, CTR decline, frequency increase, current spend, then ad ID. This is a
rule-based priority order, not statistical significance. Each window
defaults to a 1,000-ad bound; use `max_ads=5000` for a larger account (maximum
10,000). Scans beyond the selected bound or unusable pagination fail explicitly;
no partial diagnosis is returned. Flagged ads receive current creative IDs via
up to 100 extra metadata reads (`max_creative_lookups`, 0–1,000; zero disables
enrichment). Missing IDs, failed reads, rate limits, and the lookup cap remain
explicit without discarding findings. An exhausted transient error stops further
optional reads for that call; unread identities are marked `skipped_transient_error`.
Exact creative-ID groups cover flagged ads only; current IDs do not prove
historical creative identity and are not
inferred from names. `creative_identity_complete` is separate from the complete
Insights scan. Confidence remains null/uncalibrated. Medium severity requires
at least 20% CTR decline and 20% frequency rise; high requires at least 50% of
both. These thresholds are a prioritization policy, not probabilities.

Use `list_ads(name_contains_any=["Ada","Grace"], whole_term_match=true,
effective_status=["ACTIVE"], fields=["id","name"])` to search several names in
one call that pages separate Graph `CONTAIN` queries and deduplicates matching ad IDs.
The status filter is sent with each query. Meta determines candidate case/Unicode
matching; local matching uses casefold. Whole-term mode treats
punctuation and underscores as separators and avoids matching `Ada` in `Adam`.
Each ad includes `matched_terms`. There is no 1,000-ad account inventory cap.
Missing or repeated cursors fail explicitly; completeness still reflects the selected
scope and status filters, not all historical ads.

For a landing-page inventory, request `fields=["id","name","destination_urls"]`.
The server extracts configured story, carousel, video CTA, and asset-feed links
without returning the large creative object. These are configured destinations;
redirects, dynamic catalog substitutions, and `url_tags` are not resolved.

For a placement-image replacement, compose existing operations: inspect the ad's
creative, upload the new image, create a replacement creative preserving its
other fields and asset customization rules, attach it with `update_ad_creative`,
and read back the ad. Preview the replacement before attaching it when appropriate.
No bulk creative rewrite is inferred from a name or image filename.

Audience lists expose `summary.complete`, `summary.truncated`, and
`summary.next_after`; follow the cursor before treating the inventory as complete.
`create_lookalike_audience(countries=["US","CA","AU"], ratio=0.01, ...)`
uses `custom_ratio` and nested geographic targeting. `lookalike_spec` is managed
by the typed inputs and cannot be overridden through `params`. Duplicate-source
errors remain explicit; the server does not delete existing audiences to make a create succeed.

See the [2026-10-22 report dispositions](usage-report-2026-10-22-remediation.md)
for validation evidence and live API limitations.

`get_ad_image` joins asset labels to `configured_image_rules`, retaining crop
coordinates, explicit default flags, rule constraints, and unresolved labels.
It includes reporting-position hints (Instagram `stream` becomes `feed`;
unknown values pass through). These are configured eligibility rules, not proof
that a specific image served. Placement Insights show delivery totals, but do
not establish image-level delivery when several assets are eligible.
Pass `ad_ids=["AD_1","AD_2"]` instead of `ad_id` for a collection of up to 100
distinct ads. Creative reads and image-hash lookups are reused within that call,
with image caching scoped to the owning account. Unresolved image hashes remain
explicit. This uses the existing per-object Graph reads, not a new batch job or
persistent cache.

## Read-only mode

For an analysis-only server, set `META_READ_ONLY=true` in its launch environment
and restart it. This opt-in mode hides mutation tools from search and rejects
advertising mutations, asset uploads, A/B-test setup, and token mutations before
execution—even through `call_tool` or compatibility aliases. Unknown tools fail
closed. Reporting jobs and local overflow-artifact cleanup remain available.
Use an `ads_read` token as an additional permission boundary where possible.
Analysis agents using tool search need access to `search_tools`, `call_tool`, and
`get_capabilities`; allowing only underlying tool names is not sufficient.
The default remains writable; no existing server or dbt agent configuration is
changed automatically.

## Local file uploads

Tools that upload a local file (`add_audience_users`, `remove_audience_users`,
`create_product_feed_upload`, `upload_ad_video` and `upload_creative_asset` for images, each with
`file_path`) read only from one folder you
choose. Set `META_UPLOAD_DIR` in the launch environment to that folder and put
the file there. Until it is set, these tools refuse to read any local file. A
path that leads outside the folder, including through a link, is refused. This
matters because the model also reads text written by other people, such as ad
comments, and a file path it supplies should not be able to send some other file
on your machine to Meta.

## Attribution windows and async reports

For click/view attribution comparisons, use `get_entity_insights` with explicit
dates, `fields=["actions","action_values"]`,
`use_unified_attribution_setting=false`,
`action_attribution_windows=["7d_click","1d_view"]`, and
`include_raw_actions=true`. Read the named window keys from the raw purchase
action entries. Meta's generic `value` can differ from them even for a single
requested window; core metrics, action maps, and `flatten_actions` use that
generic value, not window-specific totals. Do not assume the window values or
overlapping action aliases are additive. Historical ad set setting changes
still require retained activity or warehouse history.

Async insights use a lean scalar field set by default; pass
`field_preset="full"` or explicit `fields` when the wider Meta response is
required. Pass `flatten_actions=["purchase","purchase_value"]` when creating
and retrieving a report to request only the required action dependencies and
promote those values into scalar row columns. Async retrieval omits verbose
action arrays and maps unless `include_raw_actions=true`, while preserving
requested scalar fields such as spend, impressions, and clicks. Use `wait=true`
for a bounded server-side poll, or inspect the returned `job.ready`,
`job.terminal`, and `job.poll_after_seconds` fields yourself.

## Errors and retries

Graph API failures are returned as allowlisted structured error JSON with Meta
codes, subcodes, user guidance, retry timing, and usage diagnostics when
available. Reads may retry transient failures; create, update, and delete calls
are not automatically retried after ambiguous failures. If
`mutation_outcome_unknown=true`, `retryable` is false and callers must verify
current object state before deciding whether to retry.

## Default account

`META_DEFAULT_ACCOUNT_ID` is an optional convenience, not a requirement for
object-scoped activity history. When a campaign, ad set, or ad ID is supplied,
the server looks up its owning account and validates any explicit or default
account against that ownership.

## Complete change history

Use `export_change_history` when asking for every change in a period:

```json
{
  "name": "export_change_history",
  "arguments": {
    "account_id": "act_123",
    "since": "2026-06-01",
    "until": "2026-06-30",
    "time_zone": "America/New_York"
  }
}
```

Account scope includes activity at every level and every event type Meta returns.
Campaign and ad set exports include descendants by default: the server scans
account activity and resolves parent IDs for observed objects, including readable
deleted objects. It does not depend on the current active-object inventory or
Meta's `add_children` flag. Unreadable parents and unknown entity types remain in
the export as `scope_match.status="unresolved"` candidates; `complete` stays false.
Use `extra_oids` for known related IDs that can no longer be linked to their parent.

`since` and `until` are required for exports. Date-only `until` includes the whole
specified day in `time_zone` (UTC by default), including daylight-saving changes.
Timestamp bounds require an explicit offset, with inclusive `since` and exclusive
`until`. Unix seconds are also accepted. Future end dates are capped at collection
time. Every continuation uses the same frozen effective bounds.

The response's `items` are a preview. Follow `delivery.read_call`, then each
`next_item_offset`, to read and save every artifact event. For oversized single
events, use `delivery.byte_read_call` and follow `next_offset` to recover the full
JSON. An artifact reader's `complete=true` means that artifact has been read; it
does **not** establish that all Meta pages were exported. After saving the current
artifact, follow the export's `next_call` if present and combine every export part.
Delete artifacts only after their contents are saved or consumed.

Exports automatically paginate, bounded by `max_pages` (default 100), a 45-second
retrieval budget, and approximately 32 MiB of collected event JSON. Reaching a
budget or failing a later API page preserves fetched events and returns explicit
partial coverage and a continuation. Missing/repeated cursors require a smaller
date window. Parent lookups are bounded at 200 per call; unresolved candidates
remain visible when that budget is reached. The final continuation part alone
is not a complete export: `coverage.api_pages_exhausted` records reaching the end,
while `coverage.starts_at_beginning` distinguishes a full retrieval from its tail.

For a quick page, `list_change_history` defaults to 50 records and a frozen
seven-day window. Follow `next_call` even if the current page has no matching
items. Its campaign/ad set scope is object-only unless
`include_descendants=true`. `export_call` suggests a full-detail hierarchy export;
`unfiltered_account_export_call` removes entity/category/actor/source restrictions
for the same effective period.

Omit `category` and `uid` when asking for every change. A category is a filter,
not an entity level: `category="AD_SET"` can omit optimization-goal changes on an
ad set. `category="ALL"` means no category filter. The output normalizes legacy
`object_type` names into `entity_level`: `CAMPAIGN_GROUP` → campaign, `CAMPAIGN` →
ad set, and `ADGROUP` → ad. Raw fields, actors, application/tool attribution,
event types, and `extra_data` are retained; parsed details and old/new availability
are added. Deletion can be a status-change event. Missing old/new details must not
be inferred, and payment amounts retain their raw minor currency units.

Always report `coverage`, active filters, effective dates, and `ai_instructions`.
`complete=true` means all API-returned events for that query were retrieved from
the beginning, with no unresolved scope candidates. It does not establish parity
with every Ads Manager action or unlimited retention. Permissions, Meta retention,
unlogged actions, and delayed events can still limit what the API exposes. An
empty or partial response is not evidence that no changes occurred.
