# Parity with Meta's official Ads MCP server

Date: 2026-11-12. Branch: `feature/official-mcp-parity`.

## Intent

Meta publishes a hosted Ads MCP server (`https://mcp.facebook.com/ads`) with 88
documented tools in seven areas. This server should be able to do everything
that one can, and do it with this project's existing strengths: ranked,
evidence-backed outputs, explicit pagination and coverage, before/after on every
write, and a read-only mode.

Decided by Jeff on 2026-11-12:

- Scope is everything, including the 31 catalog tools and the pixel setup tools.
- This reverses the earlier non-goal "replicate the Marketing API" for the
  specific surface Meta's server exposes. `SPEC.md` and `CLAUDE.md` are updated
  to say so.
- Implementation is done by Claude Opus 5.5 agents at high effort. Review is
  done by Codex `gpt-6.1-sol` at max effort. The orchestrator specs, plans,
  integrates, verifies and adjudicates review findings.

## Where we start

Measured on `master` at `7fc48d1` (102 tools, 1740 tests passing, 32 skipped):

| Area | Meta tools | Built for it | Reachable but hidden | Partial or untested | Not possible |
|---|---|---|---|---|---|
| Reporting | 7 | 2 | 1 | 2 | 2 |
| Ad creation and management | 27 | 16 | 5 | 4 | 2 |
| Catalogs | 31 | 0 | 0 | 0 | 31 |
| Signals and datasets | 13 | 0 | 3 | 0 | 10 |
| Help and troubleshooting | 2 | 1 | 0 | 0 | 1 |
| A/B tests and lift studies | 7 | 1 | 3 | 1 | 2 |
| Activity logs | 1 | 1 | 0 | 0 | 0 |
| Total | 88 | 21 | 12 | 7 | 48 |

"Reachable but hidden" means a free-form `fields` option on an existing tool
returns the data (verified read-only against a live account), but no tool
description mentions it, results are raw, and only the first chunk of a list
comes back.

Meta's documentation lists tool names and one-line descriptions only. It does
not publish parameters. The full list of the 88 names is in Appendix A.

## Goals

1. Every one of Meta's 88 capabilities is either served by a purpose-built tool
   here, or recorded as unavailable with the reason, because Meta exposes no
   public API for it.
2. A machine-checked parity ledger lists all 88 Meta tool names and what serves
   each one here. A test fails if any name is missing or points at a tool that
   is not registered.
3. New tools follow the existing conventions exactly, so the catalog-wide
   contract, routing, and read-only tests pass without special cases.
4. No new write can run in read-only mode.

## Non-goals

- Matching Meta's parameter names or response shapes. They are unpublished.
- Hosting, OAuth, or any change to how this server authenticates.
- Capabilities Meta's server does not have (video upload, commerce orders).
- Changing the behaviour of any existing tool, except where this document says
  so.

## Anti-goals

- A tool that exists only to say "not supported". If Meta has no public API for
  a capability, no tool is registered for it. The parity ledger records it as
  unavailable and names the nearest real tool, if there is one.
- Invented endpoints, fields or parameters. Every Graph API endpoint, field and
  parameter used by a new tool must be confirmed in Meta's reference
  documentation or in the `facebook_business` SDK source at 26.0.x. Anything
  that cannot be confirmed is reported back, not guessed.
- Live writes to a real ad account during development or testing.
- Customer personal data appearing in any tool response, log line, error
  message or overflow artifact.

## Conventions every new tool follows

These are existing rules of the codebase. They are restated because every work
stream depends on them.

- Tools are `async def` functions decorated with `@mcp_server.tool()` in a
  module under `meta_ads_mcp/tools/`, and the module is listed in
  `meta_ads_mcp/tools/__init__.py`.
- All Graph API traffic goes through `GraphAPIClient` in
  `meta_ads_mcp/graph_api.py`. Prefer the generic methods (`get_object`,
  `list_objects`, `create_edge_object`, `update_object`, `delete_object`). Add a
  named client method only when a call does not fit them.
- Return dicts and lists, never serialized JSON strings. Keep ids as strings.
- Use the builders in `meta_ads_mcp/schemas.py`: `collection_response` shapes via
  `normalize_collection` for lists, `mutation_response` for updates and deletes
  (with `previous` and `current`), `creation_response` for creates,
  `analysis_response` for derived reports.
- List tools take `limit` and `after`, and report whether more pages exist the
  same way `list_audiences` does.
- Tools that accept `fields` use the `FieldList` type. Tools that accept lists of
  strings use `StringList`, so comma-separated strings are accepted.
- Money is in major units of the account currency, converted with
  `meta_ads_mcp/money.py`, as the budget tools do.
- Input mistakes raise `ValidationError` from `meta_ads_mcp/errors.py` before any
  network call.
- Docstrings are written for a model choosing a tool: what it is for, and the
  one or two facts that prevent misuse. Keep them near the length of the
  existing ones.
- Every tool name is added to exactly one group in `TOOL_GROUPS` in
  `meta_ads_mcp/tools/utility.py`. `test_capabilities_manifest_matches_registered_tools`
  requires the groups and the registered tools to match exactly.
- Each new capability gets an entry in `INTENT_GUIDE` (same file) when a user
  would plausibly ask for it in words that do not contain the tool name.

## Rules for write tools

- A write tool must be classified as a write by `meta_ads_mcp/tool_contracts.py`.
  Names starting with `create_`, `update_`, `delete_`, `set_` or `upload_` are
  classified automatically. Any other write name (for example
  `add_audience_users`, `cancel_ad_study`, `connect_catalog_event_source`) is
  added to `_WRITE_NAMES` there.
- Every write tool is listed in `TOOL_GROUPS["writes"]`. That list drives the
  read-only block and `list_mutation_tools`.
- Creates default to a paused or inactive state wherever the object has one.
- Updates read the current values first and return them as `previous`.
- Deletes read the object first and return enough of it in `previous` to
  recreate it. Their docstrings say to use them only on an explicit request.
- Tools that create or edit campaigns, ad sets or ads accept `validate_only` and
  pass Meta's validate-only execution option, as `create_ad_set` does.
- `list_mutation_tools` gains `common_paths` entries for the new write families.

## New tools

Names are final. Parameters listed are the required shape; implementers add
optional parameters where Meta's API supports something useful.

### Stream A: ad management gaps

New read group `assets`. Module `meta_ads_mcp/tools/assets.py` for the new read
tools. New writes go in the module that already owns the object.

| Tool | What it does |
|---|---|
| `list_pages(source, account_id, business_id, limit, after, fields)` | `source` is `user` (Pages the token's user can advertise with), `ad_account` (Pages used for ads on that account) or `business` (Pages owned by a business). Covers three Meta tools. `get_account_pages` is left unchanged. |
| `list_ad_images(account_id, hashes, limit, after, fields)` | The account's image library. |
| `list_ad_videos(account_id, limit, after, fields)` | The account's video library. |
| `list_creative_ads(creative_id, account_id, effective_status, limit, after)` | Ads that use one creative. If Meta has no server-side filter for this, page through the account's ads and filter locally, and report how many ads were scanned and whether the scan was complete. |
| `list_instagram_media(instagram_account_id, boost_eligible_only, limit, after, fields)` | Recent posts, reels and stories. `boost_eligible_only` defaults to true and each item says whether it can be boosted and, if not, why. |
| `list_audience_adsets(audience_id, limit, after)` | Ad sets that target one custom audience. |
| `get_field_reference(level, field_names, object_id)` | Field metadata for campaigns, ad sets and ads: type, description, and where known the allowed values and whether the field can be used as a filter. Use Meta's live field metadata (`metadata=1`) as the source for types and descriptions. |
| `update_adset(adset_id, name, targeting, start_time, end_time, optimization_goal, billing_event, promoted_object, status, validate_only, params)` | General ad set edit with before/after. Budget and bid changes stay in the existing dedicated tools; reject those fields here with a message naming the right tool. |
| `update_ad(ad_id, name, creative_id, tracking_specs, status, validate_only, params)` | General ad edit with before/after. |
| `add_audience_users(audience_id, schema, rows, pre_hashed)` | Adds people to a customer list audience. See the personal-data rules below. |
| `remove_audience_users(audience_id, schema, rows, pre_hashed)` | Removes people from a customer list audience. |
| `boost_instagram_post(account_id, adset_id, instagram_media_id, instagram_account_id, name, call_to_action, validate_only)` | Creates a creative from an existing Instagram post and a paused ad under an existing ad set. Returns both ids. |

Personal-data rules for the two audience tools:

- `pre_hashed` defaults to false. When false, the server normalizes each value by
  Meta's published rules for that key (trim, lowercase, phone digits only, and so
  on) and hashes it with SHA-256 locally. Raw values never leave the machine.
- When `pre_hashed` is true, each value must look like a SHA-256 hex digest, or
  the call is rejected.
- Rows are sent in batches within Meta's per-request limit, using Meta's session
  fields so a multi-batch upload is one logical upload.
- The response contains counts and Meta's session information only. No row,
  value or hash is echoed, logged, placed in an error message, or written to an
  overflow artifact.

### Stream B: reporting and troubleshooting gaps

All in the existing `optimization` group. Module
`meta_ads_mcp/tools/performance_signals.py`.

| Tool | What it does |
|---|---|
| `get_opportunity_score(account_id, include_history, history_days, include_recommendations)` | The account's 0 to 100 score, its recent daily history, and the top recommendations with their estimated score gain. |
| `get_performance_trend(level, object_id, metrics, since, until, granularity)` | CPC, CPM, cost per result, ROAS, CTR and conversion rate per day, week or month, with first-to-last change and direction for each. |
| `detect_performance_anomalies(level, object_id, metrics, lookback_days, baseline_days, sensitivity)` | Flags days whose value is far from that entity's own recent baseline. Uses a robust rule (median and median absolute deviation, compared with the same weekdays where enough history exists). The response states the rule, the baseline values and the threshold. It must not claim statistical significance. |
| `get_auction_ranking_report(level, object_id, date_preset, since, until, top_n)` | Ranks ads by Meta's quality, engagement-rate and conversion-rate rankings alongside spend and results, and says for each weak ad whether the evidence points at creative quality or at bid and budget. |
| `get_advertiser_context(account_id, lookback_days)` | A briefing built from account data: business and account settings, spend by objective and optimization goal, funnel event volumes with step-to-step drop-off, datasets and custom conversions in use, Pages and Instagram identities, and which optimization goals have enough weekly event volume to support them. Labeled as derived here, not Meta's own summary. |
| `get_delivery_errors(account_id, level, object_ids, limit, after)` | Delivery-blocking problems across an account or for named objects: account status and disable reason, `issues_info`, failed delivery checks and ad review feedback, ordered by severity and by spend affected. |

Two Meta capabilities in this area may have no public API: industry benchmarks
from similar advertisers, and Help Center article search. The implementer checks
Meta's documentation first. If a public API exists, build
`get_industry_benchmarks` or `search_help_articles` to the conventions above. If
not, build nothing and report it; the ledger marks them unavailable, with
`get_auction_ranking_report` named as the nearest tool for benchmarks.

### Stream C: experiments

New read group `experiments`. Module `meta_ads_mcp/tools/experiments.py`.
`setup_ab_test` is left unchanged.

| Tool | What it does |
|---|---|
| `list_ad_studies(account_id, business_id, study_type, entity_id, limit, after)` | A/B tests and lift studies on an account or business, optionally only those involving one campaign, ad set or ad. |
| `get_ad_study(study_id, include_results)` | One study's settings, cells and objectives, and with `include_results` the outcome: the winning cell and confidence for A/B tests, lift and confidence for lift studies. Says plainly when results are not ready. |
| `check_ab_test_eligibility(account_id, level, entity_ids)` | Whether the given campaigns or ad sets can be put in an A/B test. If Meta has a public eligibility endpoint, use it. Otherwise apply Meta's documented requirements locally (exist, same account, compatible objectives and buying type, not already in a running study, deliverable state) and label the result as a local pre-check. |
| `create_ab_test(account_id, name, cells, start_time, end_time, confidence_level, hypothesis, params)` | Creates an A/B test from cells, each with a name, a share of the audience, and campaign or ad set ids. Validates that shares total 100 and that no entity appears in two cells. |
| `create_lift_study(account_id, business_id, name, cells, objectives, start_time, end_time, params)` | Creates a conversion lift study. |
| `update_ad_study(study_id, name, description, start_time, end_time, params)` | Edits a study, with before/after. |
| `cancel_ad_study(study_id)` | Ends or cancels a study, using whichever operation Meta documents for the study's current state. Returns the prior state. |

### Stream D: datasets and pixels

New read group `signals`. Module `meta_ads_mcp/tools/datasets.py`.

| Tool | What it does |
|---|---|
| `list_datasets(account_id, business_id, limit, after, fields)` | Pixels and app datasets on an account or business. |
| `get_dataset(dataset_id, fields)` | One dataset's settings, including Conversions API setup. |
| `get_dataset_stats(dataset_id, since, until, aggregation)` | Event volume received, up to 28 days, optionally split by event, device or source. |
| `get_dataset_quality(dataset_id)` | Event match quality scores, coverage per match key, and data freshness per channel, with plain flags for scores Meta's guidance treats as weak. |
| `list_custom_conversions(account_id, limit, after, fields)` | Custom conversions on an account. |
| `list_pixel_event_rules(pixel_id, rule_id, limit, after)` | Event rules on a pixel, or one rule. |
| `create_pixel_event_rule(pixel_id, ...)` | Creates a rule, inactive. |
| `update_pixel_event_rule(rule_id, active)` | Activates or deactivates a rule. |
| `delete_pixel_event_rule(rule_id)` | Deletes a rule. |
| `list_pixel_parameters(pixel_id, parameter_id, limit, after)` | Parameter extractors on a pixel, or one. |
| `create_pixel_parameter(pixel_id, ...)` | Creates a parameter extractor. |
| `update_pixel_parameter(parameter_id, ...)` | Updates one. |
| `delete_pixel_parameter(parameter_id)` | Deletes one. |

The orchestrator has not confirmed that Meta exposes event rules and parameter
extractors through the public Graph API. The implementer researches this before
writing any of the eight rule and parameter tools. If no public endpoint is
documented, those tools are not built and the finding is reported.

### Stream E: catalogs, products and product sets

New read group `catalog`. Module `meta_ads_mcp/tools/catalogs.py`.

| Tool | What it does |
|---|---|
| `list_catalogs(business_id, account_id, catalog_id, limit, after, fields)` | Catalogs for a business or ad account, or one catalog with its product and feed counts. |
| `list_dpa_eligible_catalogs(business_id, account_id)` | Catalogs able to run Advantage+ catalog ads, ranked, with the reason each one ranks where it does and a recommended choice when one is clear. |
| `get_catalog_diagnostics(catalog_id, severity, limit, after)` | Errors, warnings and opportunities, ordered by how many products each affects. |
| `get_catalog_dynamic_ads_health(catalog_id, product_set_id)` | Whether the catalog or one product set can serve catalog ads, and what is blocking it if not. |
| `list_catalog_data_sources(catalog_id)` | Feeds, uploads and API sources that supply the catalog. |
| `create_catalog(business_id, name, vertical, params)` | Creates a catalog. Loading products is done with the feed and product tools. |
| `update_catalog(catalog_id, name, params)` | Edits catalog settings, with before/after. |
| `list_catalog_products(catalog_id, product_set_id, product_id, filter, limit, after, fields)` | Products in a catalog or set, filtered, or one product. |
| `create_catalog_product(catalog_id, retailer_id, name, price, currency, availability, url, image_url, ...)` | Creates a product. |
| `update_catalog_product(product_id, ...)` | Edits product fields, with before/after. |
| `delete_catalog_product(product_id)` | Deletes a product. |
| `build_product_set_filter(catalog_id, conditions, match)` | Turns structured conditions (field, operator, value) into a valid Meta filter, then counts how many products in the catalog match and returns a small sample. It only builds and tests the filter. The calling model translates plain English into conditions. |
| `list_product_sets(catalog_id, product_set_id, product_id, limit, after)` | Product sets in a catalog, one set, or the sets containing a product. |
| `create_product_set(catalog_id, name, filter, params)` | Creates a product set from a filter. |
| `update_product_set(product_set_id, name, filter, retailer_id)` | Edits a set, with before/after. |
| `delete_product_set(product_set_id)` | Deletes a set. Products remain. |

### Stream F: catalog feeds and event sources

Same `catalog` read group. Module `meta_ads_mcp/tools/catalog_feeds.py`.

| Tool | What it does |
|---|---|
| `list_product_feeds(catalog_id, feed_id, limit, after)` | Feeds on a catalog, or one feed with settings, schedule and latest upload status. |
| `create_product_feed(catalog_id, name, schedule, ...)` | Registers a feed, optionally fetched from a URL on a schedule. |
| `update_product_feed(feed_id, ...)` | Edits name, defaults, parsing options or schedules, with before/after. |
| `delete_product_feed(feed_id)` | Deletes a feed and its schedule. The docstring says its products are removed afterwards unless another source supplies them. |
| `create_product_feed_upload(feed_id, url, update_only)` | Starts an upload for a feed. |
| `list_product_feed_uploads(feed_id, upload_id, limit, after)` | Uploads for a feed with status, counts, and errors ordered by rows affected. |
| `list_feed_rules(feed_id)` | Attribute mapping and transformation rules on a feed. |
| `create_feed_rule(feed_id, attribute, rule_type, params)` | Creates a rule. |
| `delete_feed_rule(rule_id)` | Deletes a rule. The feed and products are unaffected. |
| `list_catalog_event_sources(catalog_id)` | Pixels, apps and offline datasets connected to a catalog. |
| `list_event_source_catalogs(event_source_id)` | Catalogs connected to one event source. |
| `get_catalog_event_source_health(catalog_id)` | Match rate and setup problems per connected event source, including 28-day match-rate history. |
| `get_catalog_event_source_recommendations(catalog_id, business_id)` | Event sources worth connecting because signal coverage is weak or missing, with the evidence. |
| `connect_catalog_event_source(catalog_id, event_source_id)` | Connects an event source. |
| `disconnect_catalog_event_source(catalog_id, event_source_id)` | Disconnects one. |

## The parity ledger

A new module `meta_ads_mcp/official_parity.py` holds one entry for each of
Meta's 88 tool names (Appendix A):

- `status`: `covered` or `unavailable`.
- `tools`: the tool names here that serve it, most direct first.
- `note`: one plain sentence, for example which argument selects the equivalent
  behaviour, or why it is unavailable.

How it is used:

- `get_capabilities` accepts an `official_tool` argument and returns that entry,
  so a model that knows Meta's name can find ours.
- When a Meta name is passed to `call_tool` or used as a tool name and its entry
  has exactly one tool, it resolves to that tool through the existing alias path
  in `meta_ads_mcp/input_compat.py`. When the entry has several tools or is
  unavailable, the error names the options from the ledger.
- A test asserts that all 88 names are present, that every `covered` entry names
  only registered tools, and that every `unavailable` entry has a reason.

The orchestrator builds the ledger after the streams are merged. Streams do not
edit it.

## How the parallel streams avoid colliding

Before the streams start, the orchestrator commits a foundation change:

- empty groups `assets`, `signals`, `experiments` and `catalog` in `TOOL_GROUPS`;
- those four group names added to the read-only allowlist in
  `meta_ads_mcp/read_only.py`;
- this document, and the `SPEC.md` and `CLAUDE.md` updates.

Each stream then:

- works in its own git worktree branched from the foundation commit;
- owns its new module and its new test file outright;
- makes only additive edits to shared files (`tools/__init__.py`,
  `TOOL_GROUPS`, `INTENT_GUIDE`, `_WRITE_NAMES`, `list_mutation_tools`,
  `tests/test_tool_contracts.py`, `tests/test_read_only.py`), adding its lines
  without reordering or reformatting anything already there;
- does not edit `README.md`, `official_parity.py`, `input_compat.py`, or this
  document.

The orchestrator merges the streams one at a time, resolves the additive
overlaps, then writes the ledger, the README tool list, and the routing text in
`meta_ads_mcp/coordinator.py`.

## Testing and verification

Per stream:

- A test file per new module, using a fake Graph client as
  `tests/test_tools_audiences.py` does. For every tool: the endpoint and edge
  called, the parameters sent, the response shape, and each validation error.
- Every new tool passes the catalog-wide tests unchanged in spirit:
  `tests/test_tool_contracts.py` (including the smoke run of every tool),
  `tests/test_coordinator.py`, and `tests/test_read_only.py`. Add entries to the
  existing override tables where a tool needs sample arguments.
- Every new write tool appears in the read-only blocked set and is refused both
  directly and through `call_tool`.
- The audience user tools have tests proving that no row value or hash appears
  in the response, in raised error text, or in captured logs.
- `uv run --extra dev pytest` passes in full before the stream reports done.
- No live Meta calls. Tests are mocked.

By the orchestrator after integration:

- Full test suite.
- Each new read tool is run read-only against the real account where the current
  token allows. Catalog tools wait for a token with `catalog_management`.
- No write tool is run against a real account without Jeff's explicit go-ahead
  for that specific action.
- A Codex `gpt-6.1-sol` max-effort review of the full branch. Every finding is
  checked before it is acted on or relayed.

## Reported back by each stream

- The tools built, and any tool in this document not built, with the reason.
- For each tool, the Graph endpoint used and the Meta documentation page or SDK
  file that confirms it.
- Anything that could not be confirmed.
- The final test count.

## Appendix A: Meta's 88 tool names

Reporting (7): `ads_get_ad_entities`, `ads_get_opportunity_score`,
`ads_insights_advertiser_context`, `ads_insights_anomaly_signal`,
`ads_insights_auction_ranking_benchmarks`, `ads_insights_industry_benchmark`,
`ads_insights_performance_trend`.

Ad creation and management (27): `ads_get_ad_accounts`,
`ads_get_ad_account_pages`, `ads_get_pages_for_business`, `ads_get_user_pages`,
`ads_get_field_context`, `ads_create_campaign`, `ads_create_ad_set`,
`ads_create_ad`, `ads_update_entity`, `ads_activate_entity`,
`ads_create_creative`, `ads_get_creatives`, `ads_get_creative_ads`,
`ads_get_ad_images`, `ads_get_ad_videos`, `ads_get_ad_preview`,
`ads_library_search`, `ads_get_ig_accounts`, `ads_get_ig_media`,
`ads_boost_ig_post`, `ads_get_ad_account_custom_audiences`,
`ads_get_custom_audience`, `ads_get_custom_audience_adsets`,
`ads_create_custom_audience`, `ads_update_custom_audience`,
`ads_update_custom_audience_users`, `ads_delete_custom_audience`.

Catalogs (31): `ads_catalog_create`, `ads_catalog_update_catalog`,
`ads_catalog_list_catalogs`, `ads_catalog_list_dpa_eligible_catalogs`,
`ads_catalog_get_diagnostics`, `ads_catalog_get_dynamic_ads_health`,
`ads_catalog_get_data_sources`, `ads_catalog_list_products`,
`ads_catalog_product_create`, `ads_catalog_update_product`,
`ads_catalog_delete_product`, `ads_catalog_get_suggested_filter`,
`ads_catalog_create_product_set`, `ads_catalog_update_product_set`,
`ads_catalog_list_product_sets`, `ads_catalog_product_set_delete`,
`ads_catalog_create_product_feed`, `ads_catalog_update_product_feed`,
`ads_catalog_list_product_feeds`,
`ads_catalog_create_product_feed_upload_session`,
`ads_catalog_get_product_feed_upload_sessions`,
`ads_catalog_product_feed_delete`, `ads_catalog_create_feed_rule`,
`ads_catalog_get_feed_rules`, `ads_catalog_product_feed_delete_rule`,
`ads_catalog_event_source_get`, `ads_catalog_event_source_get_catalogs`,
`ads_catalog_event_source_get_health`,
`ads_catalog_event_source_get_recommendations`,
`ads_catalog_event_source_connect`, `ads_catalog_event_source_disconnect`.

Signals and datasets (13): `ads_get_datasets`, `ads_get_dataset_details`,
`ads_get_dataset_stats`, `ads_get_dataset_quality`, `ads_pixel_event_read`,
`ads_pixel_event_create`, `ads_pixel_event_update`, `ads_pixel_event_delete`,
`ads_pixel_parameter_read`, `ads_pixel_parameter_create`,
`ads_pixel_parameter_update`, `ads_pixel_parameter_delete`,
`ads_get_customconversions`.

Help and troubleshooting (2): `ads_get_errors`, `ads_get_help_article`.

A/B tests and lift studies (7): `ads_experiment_list_tests`,
`ads_experiment_check_eligibility`, `ads_experiment_abtest_create_test`,
`ads_experiment_abtest_get_test`, `ads_experiment_abtest_update_test`,
`ads_experiment_lift_create_test`, `ads_experiment_lift_get_test`.

Activity logs (1): `ads_account_get_activity_logs`.
