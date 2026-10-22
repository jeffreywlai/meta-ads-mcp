# Nick's 2026-10-22 usage report: disposition and verification

The implementation was checked against the current default branch,
`e3b85d85d36983af2f4f0f697ceb77858617a164`, with the existing dependency patch
`30a82de4c79a9f8fa326218c6960a904fc5824f9` retained. The unchanged baseline
passed 1,257 tests with 32 live tests skipped. The source report is Nick's local
`20261022_meta_mcp_usage_report.md`; its transcript evidence and historical build
attributions were not independently replayed on Nick's computer.

The approach is to fix shared boundaries: measure the response actually sent,
page complete records, project only requested detail, preserve typed payload
ownership, and make completeness and units explicit. Two small operations fill
concrete workflow gaps: aggregation of an existing async report and attaching an
already prepared creative to an existing ad. There is no new job service or
automatic mutation-retry system.

## Validation

The final full suite passed **1,482 tests**, with **32 live tests skipped**,
using the project's installed Python environment and this checkout's source.
The unchanged baseline passed 1,257 tests with the same 32 skips. Installed
dependencies passed `uv pip check`, and `git diff --check` was clean.

GPT-6.1-Sol agents at maximum reasoning implemented the parallel fixes and
performed cross-reviews. A separate reviewer checked the integrated changes.
Review findings were corrected and covered by regressions, including negative
and reference-only routing clauses, exact aggregation arithmetic, empty
advancing report pages, and schema-valid direct-tool overflow notices. Real
FastMCP Client tests exercise both normal overflow archival and archive failure;
the original operation's success/error status is preserved in each case.

These are repository and local transport results. They do not attest Meta's
live write behavior or the installed server on Nick's computer.

### Follow-up GitHub review

All three review findings on `327f38d` were reproduced before correction:

- Postfix exclusions such as `bid amount must not change` incorrectly exposed
  the bid write. Routing now checks negative and preservation clauses before
  accepting a field, while preserving earlier positive requests. Regression
  cases include coordinated fields, bare `unchanged`, contractions, and
  comma/colon punctuation.
- Mixed requests such as `update campaign budget and name` omitted the general
  campaign updater. Field continuations retain scope and include every required
  tool, including continuations after numeric, status, and quoted values.
  Explicit reads, literal arguments, and reference-only clauses remain constrained.
- Parsed overflow continuations decoded the entire archive for every page.
  A verified-identity and digest cache now retains at most four decoded
  artifacts within a conservative 64 MiB payload budget. Returned pages cannot
  mutate cached data. Integrity failure, replacement, deletion, and expiry
  invalidate it; restart rereads persisted artifacts and continuations.

The final independent pass found no further actionable errors: 4,030 routing
matrix checks and 250 actual local MCP `search_tools` checks passed. A second
cross-review passed 36 additional actual discovery calls. The full suite passed
1,482 tests with 32 live tests skipped, adding 95 regression cases since the
initial PR. No Meta API calls were needed for these checks.

One local before/after measurement used the same 100,000 synthetic records,
5,889,116-byte archive, `item_limit=500`, and 63,000-byte MCP result budget.
Across 100 reads, both versions recovered the same 49,262 records; byte limits
kept pages below 500 records. Full JSON parses fell from 100 to one, and elapsed
time fell from 3.1633 to 0.4524 seconds. The cold first read increased from
0.0329 to 0.1404 seconds because it accounts for decoded memory. These are local
measurements, not general latency guarantees. The conservative cache estimate
was 40,091,515 bytes; it is not process RSS. Oversized decoded artifacts bypass
the cache and retain the original repeated-parse cost.

## Findings

| Finding | Disposition | Change and evidence |
| --- | --- | --- |
| M01: premature overflow | Fixed in repository | Nested calls defer size handling to the outer `call_tool` result. The wrapper can emit one compact JSON text copy when redundant text plus structured data would overflow. Direct tools retain required output-schema content. Notices distinguish actual serialized MCP bytes from archived bytes. Wrapper and direct transport tests cover both contracts. |
| M02: difficult and unsafe overflow reads | Fixed in repository | Parsed item/row pages are the default for collections, including nested async rows. Byte mode remains available for the full artifact and huge individual items. Only zero or recently issued continuations are accepted, persisted in the existing integrity manifest; a guessed offset such as 62000 is rejected. Old evicted continuations explicitly require restarting at zero. Retention, ownership, integrity, and process-lock protections remain. |
| M03: inflated insight rows and lost flattening | Fixed in repository, with an explicit polling contract | Named action projections remove full action maps unless raw actions are requested. Unrequested null padding is removed; flattened projections omit duplicate metrics. Creation now returns `poll_arguments` containing fields, `flatten_actions`, and dimensional-action raw mode. Every fetch must pass those arguments; projection options are deliberately not hidden in process state. A synthetic 21-row placement fixture with 30 action types per row stays below 16,000 bytes and retains a named `purchase` column. |
| M04: lookalike spec | Payload fixed; live creation/read-back unverified | A supplied ratio defaults to `custom_ratio`; multi-country output uses `location_spec.geo_locations.countries` without `country:null`. The single-country path and explicit caller-selected types are preserved for Meta to validate. Invalid ratio bounds fail locally, and `params` documents that it cannot override the managed spec. Tests cover one, three, and six countries. No audiences were created or deleted for this work. |
| M05: `get_adset(fields=...)` | Fixed in repository | `get_adset`, `get_ad`, and `get_ad_account` accept field projections like sibling reads. Money is still decoded using the owning account's currency; internally added context is removed from an explicit projection. |
| M06: 1,000-ad inventory cap | Fixed in repository; Meta matching behavior remains upstream | Each OR term becomes a paged Graph `CONTAIN` query with the requested status filter. IDs are deduplicated and local whole-term matching refines candidates. Account size no longer triggers a 1,000-ad scan failure. Original term spelling is sent to Meta; exact Unicode/case candidate behavior requires live verification. |
| M07: compound discovery | Fixed in repository | Compound requests union compatible operations instead of stopping at the first route. Campaign-budget intent favors `update_campaign_budget`. Negative/read-only intent and literal argument compatibility continue to constrain results. Report queries have regression coverage. |
| M08: historical uncertain write | Existing uncertainty semantics preserved; request bound strengthened | HTTPX inactivity timeouts do not bound total elapsed time. Every request attempt now also has a wall-clock deadline. Mutation timeout remains unknown, nonretryable, and instructs a read-back. A delayed synthetic POST is attempted exactly once. This does not identify the cause of Nick's historical 221-second hang or prove the state of his write. |
| M09: historical client result limit | Covered by M01/M02 and existing archival | Oversized results are retained and recovered through bounded responses. A client-specific token cap can still be lower than the server's byte limit; use a smaller page or scalar projection. |
| M10: hidden tools and client ToolSearch | Mitigated in instructions and discovery | Load visible server `search_tools`/`call_tool` wrappers, discover the inner tool, then invoke it through `call_tool`. The server cannot change another client's deferred-tool search ranking. |
| M11: units | Fixed in schemas and response metadata | Budget/bid inputs describe major account-currency units, not universally dollars. ROAS floors remain raw integers scaled by 10,000. Activity payment amounts remain unchanged and carry minor-unit metadata; USD raw amounts are cents. Existing currency conversion behavior is retained. |
| M12: audience completeness | Fixed in repository | Audience pages expose `summary.complete`, `truncated`, and `next_after`. The Graph paging envelope is preserved for compatibility. Tests cover pages with and without continuation. |

## Workflow gaps

| Gap | Disposition and supported workflow |
| --- | --- |
| A: three-year catalog spend | Added `summarize_async_insights_report`. It reads the existing report page by page and sums only additive metrics with decimal arithmetic, grouping by fields such as `date_start`. A 99,164-row synthetic report reduces to three correct totals. Calls are bounded by page count, group count, and time; partial ranges expose continuations and never claim whole-report completeness. Total-account spend still requires its own denominator report. |
| B: placement-image replacement | Existing `get_creative`, `upload_creative_asset`, and `create_ad_creative` preserve editable creative specs. Added the missing typed `update_ad_creative(ad_id, creative_id)` to attach the prepared replacement. Search surfaces the multi-step workflow. The caller must inspect and preserve placement rules and read back the ad; image filenames are not assumed to be ad names. No live creative changes were performed. |
| C: lookalike recreation | The create payload and discoverability are addressed by M04. Duplicate deletion is not inferred from a create failure. Ad-set duplication was never attempted in the corpus and is not evidence of a missing server capability. |
| D: landing-page inventory | `list_ads` and `get_ad` accept `destination_urls` in `fields`. The projection extracts configured story, carousel, video CTA, and asset-feed destinations without the large creative payload. It does not resolve redirects, dynamic catalog substitutions, URL tags, or product stock; those are separate operations. |
| E: placement comparisons | Existing `get_account_health_snapshot`, `compare_time_ranges`, and `compare_performance` cover their documented comparison scopes. For placement mix, query both explicit windows with identical dimensions, use compact scalar actions, and compare spend shares only after confirming completeness. The new async reducer can sum additive placement totals. Causal judgments remain analysis, not a deterministic server claim. The corpus did not try the existing comparison tools, so no missing comparison endpoint was inferred. |
| F: daily live reads outside MCP | No demonstrated server defect. M05 enables compact preflight/read-back through MCP; existing typed mutations and uncertainty guidance remain. The external skill's choice to use direct scripts is not changed by this repository. |

The report's other coverage notes are retained as limits: no envelope failures
were observed; creative-fatigue thresholds, CSV string inputs, ad-set creation,
export caps, and rate limits were not exercised in the retained corpus. The
existing `get_adset` already returns ROAS constraints; no duplicate field fix was
needed. These notes do not establish deployment behavior on another computer.

## Reproducible workflows

For daily changes, discover several tools in one call:

```json
{"query":"update ad set budget bid amount status campaign budget bid strategy"}
```

Then inspect each resolved entity with selected fields, apply the specific typed
operation, and read it back. Schemas state monetary units. An unknown write
outcome requires reading the current state before retrying.

For a compact URL inventory:

```json
{"name":"list_ads","arguments":{"account_id":"act_ACCOUNT","effective_status":["ACTIVE"],"name_contains_any":["PRODUCT_A","PRODUCT_B"],"fields":["id","name","destination_urls"]}}
```

For purchases by placement, use `get_entity_insights` with explicit dates,
`breakdowns=["publisher_platform","platform_position"]`, a small scalar field
list, and `flatten_actions=["purchase"]`. With async reports, pass the returned
`poll_arguments` to every `get_async_insights_report` call; do not drop them and
pass just the ID.

For catalog spend, create the report with the required product/date breakdown,
then call:

```json
{"name":"summarize_async_insights_report","arguments":{"report_run_id":"REPORT","group_by":["date_start"],"metrics":["spend"]}}
```

Spend totals are exact decimal strings in account-currency major units; counts
are integers, including values beyond floating-point precision. Require
`summary.complete=true` for a whole-report result. If bounded execution
returns `next_page_arguments`, continue and combine the totals for each group;
the continuation response explicitly represents a page range, not the full
report. A missing metric makes the group total unknown rather than zero.

## Deployment and live verification limits

`get_capabilities` and `health_check` expose a source fingerprint and actual
installed package versions. Pin the delivered commit in Nick's pipx spec,
rebuild its cached environment, restart his MCP process, then record those
values. A local test run cannot update or attest his computer.

Before calling M04 live-verified, create a disposable lookalike with an
authorized test seed and read its ratio/countries back; include the single,
three-, and six-country cases and distinguish duplicate subcode 1713007 from
invalid-spec subcode 1713084. Such a write was not required or performed for
repository remediation. Before asserting Meta search equivalence, compare
returned IDs for representative case/Unicode terms against Graph requests in
the intended account.

Primary references checked during implementation:

- [HTTPX timeout phases](https://www.python-httpx.org/advanced/timeouts/) distinguish inactivity from an overall deadline.
- [Python timeout context manager](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout) supplies cancellation-based elapsed-time bounds.
- [pipx run caching](https://pipx.pypa.io/latest/tutorial/run-applications.html#run-cache) explains stale cached installs and refresh behavior.
- [Meta's generated AdAccount source](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adaccount.py) exposes top-level `origin_audience_id` and the generic serialized lookalike-spec input.
- [Meta's generated Ad source](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/ad.py) exposes the creative update parameter.

Meta's detailed lookalike reference was unavailable/rate-limited during this
work. The nested write payload is supported by the successful request recorded
in Nick's report, while the generated SDK confirms the available parameters.
Neither establishes successful new audience creation under today's account
eligibility rules.
