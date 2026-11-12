# Official Meta Ads MCP Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give this server a purpose-built tool for every capability of Meta's official Ads MCP server that Meta's public API allows, and a tested ledger mapping all 88 of Meta's tool names to ours.

**Architecture:** Six independent work streams each add one new tool module (plus a few edits to modules that already own an object) and one new test file, in separate git worktrees branched from a shared foundation commit. Shared registries receive additive edits only. The orchestrator merges the streams in order, then adds the parity ledger, README and routing text.

**Tech Stack:** Python 3.12, FastMCP 3.4.8, `httpx`, `pydantic` 2, `pytest` with `pytest-asyncio`, Meta Marketing API v26.0.

**Spec:** `docs/superpowers/specs/2026-11-12-official-mcp-parity-design.md`. Read it in full before starting any task. Tool names, parameters and behaviours are defined there and are not repeated here.

## Global Constraints

- All Graph API traffic goes through `GraphAPIClient` in `meta_ads_mcp/graph_api.py`.
- Return dicts and lists, never serialized JSON strings. Keep ids as strings.
- Tool names in the spec are final.
- Every Graph endpoint, field and parameter is confirmed in Meta's reference documentation or the `facebook_business` SDK source at 26.0.x before it is used. Nothing is guessed. Anything unconfirmed is reported, not built.
- No tool is registered that can only answer "not supported".
- No live calls to Meta. Do not read `~/.claude.json` or any real access token. Tests use fake clients.
- No customer personal data, raw or hashed, in any response, log line, error message or overflow artifact.
- Every write tool is classified as a write by `meta_ads_mcp/tool_contracts.py` and listed in `TOOL_GROUPS["writes"]`.
- Creates default to paused or inactive where the object has such a state.
- Updates and deletes read the object first and return it as `previous`.
- Edits to shared files are additive: add lines, never reorder or reformat existing ones.
- Streams do not edit `README.md`, `meta_ads_mcp/official_parity.py`, `meta_ads_mcp/input_compat.py`, `meta_ads_mcp/coordinator.py`, `SPEC.md`, `CLAUDE.md`, or anything under `docs/`.
- `uv run --extra dev pytest` passes in full before a task is reported done. Baseline on the foundation commit: 1744 passed, 32 skipped.

## Review Focus

1. A customer row with a blank or malformed value (empty email, phone with no digits). Expected: that value is dropped and counted as invalid; it is never hashed and sent, because the hash of an empty string would be uploaded as if it were a person. Owned by Task A.
2. A read against an object the token has no permission for (for example catalogs without `catalog_management`). Expected: Meta's permission error reaches the caller unchanged in meaning; the tool does not return an empty list that reads as "you have none". Owned by every stream; pinned in Tasks D and E.
3. A local scan that stops at a page cap before reaching the end (`list_creative_ads`, `list_audience_adsets` if scanned locally, `get_delivery_errors`). Expected: the response says the scan was incomplete and how many objects were examined. Owned by Tasks A and B.
4. Anomaly detection with too little history or a flat baseline (median absolute deviation of zero). Expected: no division by zero and no flood of flags; the response says there was not enough variation or history to judge. Owned by Task B.
5. An update tool called with no changes, or a delete whose pre-read fails because the object is already gone. Expected: a `ValidationError` before any network call in the first case; in the second, the read error is raised and no delete request is sent. Owned by every stream with writes.

---

## File Structure

| File | Responsibility | Owner |
|---|---|---|
| `meta_ads_mcp/tools/utility.py` | `TOOL_GROUPS`, `INTENT_GUIDE`, `list_mutation_tools` | Foundation creates empty groups; every stream appends |
| `meta_ads_mcp/read_only.py` | Read-only allowlist | Foundation only |
| `meta_ads_mcp/tools/__init__.py` | Module registry | Every stream adds its module |
| `meta_ads_mcp/tool_contracts.py` | Write classification (`_WRITE_NAMES`) | Streams with non-prefixed write names |
| `meta_ads_mcp/tools/assets.py` | Stream A read tools | Task A |
| `meta_ads_mcp/tools/audiences.py`, `campaigns.py`, `ads.py`, `creatives.py` | Stream A write tools, placed with the object they change | Task A |
| `meta_ads_mcp/tools/performance_signals.py` | Stream B tools | Task B |
| `meta_ads_mcp/tools/experiments.py` | Stream C tools | Task C |
| `meta_ads_mcp/tools/datasets.py` | Stream D tools | Task D |
| `meta_ads_mcp/tools/catalogs.py` | Stream E tools | Task E |
| `meta_ads_mcp/tools/catalog_feeds.py` | Stream F tools | Task F |
| `tests/test_tools_assets.py`, `test_tools_performance_signals.py`, `test_tools_experiments.py`, `test_tools_datasets.py`, `test_tools_catalogs.py`, `test_tools_catalog_feeds.py` | Per-module tests | Matching task |
| `tests/test_tool_contracts.py`, `tests/test_read_only.py` | Catalog-wide tables | Every stream appends entries its tools need |
| `meta_ads_mcp/official_parity.py`, `tests/test_official_parity.py` | Parity ledger | Task G |
| `meta_ads_mcp/input_compat.py`, `meta_ads_mcp/coordinator.py`, `README.md` | Alias resolution, routing text, tool list | Task G |

---

### Task 0: Foundation (orchestrator)

**Files:**
- Modify: `meta_ads_mcp/tools/utility.py` (`TOOL_GROUPS`)
- Modify: `meta_ads_mcp/read_only.py` (`read_only_tool_names`)
- Modify: `SPEC.md`, `CLAUDE.md`
- Test: `tests/test_read_only.py`

**Interfaces:**
- Produces: `TOOL_GROUPS` keys `assets`, `signals`, `experiments`, `catalog`, each an empty list, each treated as a read group by `read_only_tool_names()`.

- [ ] **Step 1:** Add a test `test_new_read_groups_are_read_only_groups` in `tests/test_read_only.py` asserting that all four keys exist in `utility.TOOL_GROUPS` and that a name placed in any of them is returned by `read_only_tool_names()` (monkeypatch one name into each group).
- [ ] **Step 2:** Run it; expect `KeyError: 'assets'`.
- [ ] **Step 3:** Add the four empty groups after `"research"` in `TOOL_GROUPS`, and the four names to the `groups` tuple in `read_only_tool_names()`.
- [ ] **Step 4:** Run `uv run --extra dev pytest -q`; expect 1744 passed, 32 skipped.
- [ ] **Step 5:** Update `SPEC.md` (Non-Goals, Thin Execution Layer) and `CLAUDE.md` (first project note) to state that parity with Meta's official Ads MCP server is in scope and point at the spec.
- [ ] **Step 6:** Commit spec, plan and foundation together.

---

### The loop every stream task follows

Each of Tasks A to F repeats this loop per tool, in the order the spec lists them:

- [ ] Confirm the Graph endpoint, fields and parameters in Meta's reference documentation or the SDK source. Record the page or file.
- [ ] Write failing tests in the task's test file with a fake client modeled on `FakeAudienceClient` in `tests/test_tools_audiences.py`. For each tool assert: the object id and edge requested; the exact parameters or payload sent; the response keys; each `ValidationError` the tool can raise, with no client call recorded when it is raised.
- [ ] Run the new tests and confirm they fail because the tool does not exist.
- [ ] Implement the tool. Register its name in the right `TOOL_GROUPS` list. For a write with a name not starting `create_`, `update_`, `delete_`, `set_` or `upload_`, add it to `_WRITE_NAMES`.
- [ ] Run the task's test file, then `tests/test_tool_contracts.py`, `tests/test_coordinator.py` and `tests/test_read_only.py`. Add sample arguments to `TOOL_OVERRIDES` in `tests/test_tool_contracts.py` when the smoke run needs them.
- [ ] Commit.

When the last tool is done:

- [ ] Add `INTENT_GUIDE` entries and, for writes, `common_paths` entries in `list_mutation_tools`.
- [ ] Run `uv run --extra dev pytest -q` and confirm no failures.
- [ ] Commit, then report as the spec's "Reported back by each stream" section requires.

---

### Task A: Ad management gaps

**Files:**
- Create: `meta_ads_mcp/tools/assets.py`, `tests/test_tools_assets.py`
- Modify: `meta_ads_mcp/tools/audiences.py` (`add_audience_users`, `remove_audience_users`), `meta_ads_mcp/tools/campaigns.py` (`update_adset`), `meta_ads_mcp/tools/ads.py` (`update_ad`), `meta_ads_mcp/tools/creatives.py` (`boost_instagram_post`), the shared registries
- Test: `tests/test_tools_assets.py`, plus additions to `tests/test_tools_audiences.py`, `tests/test_tools_campaigns.py`, `tests/test_tools_ads.py`, `tests/test_tools_creatives.py`

**Interfaces:**
- Consumes: `assets` group from Task 0.
- Produces: the twelve Stream A tools with the signatures in the spec. Read tools go in `assets`; the five writes go in `writes`. `add_audience_users`, `remove_audience_users` and `boost_instagram_post` are added to `_WRITE_NAMES`.

Tests this task must include beyond the loop:

- `test_audience_users_blank_and_malformed_values_are_dropped_not_hashed`: rows `[["a@example.com"], [""], ["   "]]` with schema `["EMAIL"]` send exactly one hashed value, and the response reports two invalid values.
- `test_audience_users_normalizes_before_hashing`: `" A@Example.COM "` produces the SHA-256 of `a@example.com`.
- `test_audience_users_pre_hashed_rejects_non_digests`: `pre_hashed=True` with `"a@example.com"` raises `ValidationError` and records no client call.
- `test_audience_users_never_echo_rows`: neither the response, nor the text of an error raised by the fake client mid-upload, nor `caplog.text` contains the input value or its hash.
- `test_audience_users_batches_share_one_session`: more rows than one request allows produce several requests whose session fields carry the same session id, consecutive batch numbers, and a final-batch marker on the last only.
- `test_list_creative_ads_reports_incomplete_scan`: when the fake client keeps returning a next page past the scan cap, the summary says the scan was incomplete and gives the number of ads examined. (Skip only if a confirmed server-side filter makes local scanning unnecessary.)
- `test_update_adset_rejects_budget_and_bid_fields`: passing `daily_budget` or `bid_amount` through `params` raises `ValidationError` naming `update_adset_budget` or `update_adset_bid_amount`.
- `test_update_adset_requires_a_change` and `test_update_ad_requires_a_change`: no fields raises `ValidationError` with no client call.
- `test_boost_instagram_post_creates_paused_ad`: the ad payload has `status` `PAUSED` and references the creative id returned by the creative call.

---

### Task B: Reporting and troubleshooting gaps

**Files:**
- Create: `meta_ads_mcp/tools/performance_signals.py`, `tests/test_tools_performance_signals.py`
- Modify: the shared registries

**Interfaces:**
- Consumes: existing `optimization` group; existing insights helpers in `meta_ads_mcp/tools/insights.py` and `meta_ads_mcp/normalize.py` for metric and action parsing. Reuse them; do not duplicate parsing.
- Produces: `get_opportunity_score`, `get_performance_trend`, `detect_performance_anomalies`, `get_auction_ranking_report`, `get_advertiser_context`, `get_delivery_errors`, all in `optimization`. `get_industry_benchmarks` and `search_help_articles` only if a public Meta API is confirmed.

Tests this task must include beyond the loop:

- `test_anomalies_flat_baseline_does_not_flag_or_divide_by_zero`: fourteen identical daily values followed by the same value return no anomalies and a note that the baseline had no variation.
- `test_anomalies_short_history_says_so`: fewer daily rows than `baseline_days` return no anomalies and a `missing_signals` entry.
- `test_anomalies_flags_a_clear_spike_with_evidence`: a tenfold one-day spend spike is flagged, and the finding carries the day, the value, the baseline median and the threshold used.
- `test_anomalies_never_claims_significance`: the serialized response does not contain the word "significant".
- `test_trend_zero_denominators_yield_null_not_error`: a day with zero impressions or zero clicks gives `None` for CTR, CPC and conversion rate.
- `test_delivery_errors_reports_incomplete_scan`: as for Task A's scan test.
- `test_delivery_errors_orders_by_severity_then_spend`.
- `test_opportunity_score_missing_field_is_reported`: when Meta omits `opportunity_score`, the tool returns the recommendations it has and lists the score in `missing_signals`.
- `test_advertiser_context_labels_itself_as_derived`.

---

### Task C: Experiments

**Files:**
- Create: `meta_ads_mcp/tools/experiments.py`, `tests/test_tools_experiments.py`
- Modify: the shared registries

**Interfaces:**
- Consumes: `experiments` group from Task 0.
- Produces: `list_ad_studies`, `get_ad_study`, `check_ab_test_eligibility` in `experiments`; `create_ab_test`, `create_lift_study`, `update_ad_study`, `cancel_ad_study` in `writes`. `cancel_ad_study` is added to `_WRITE_NAMES`. `setup_ab_test` in `creatives.py` is not touched.

Tests this task must include beyond the loop:

- `test_create_ab_test_shares_must_total_100`: `[50, 40]` raises; `[33.34, 33.33, 33.33]` is accepted.
- `test_create_ab_test_rejects_entity_in_two_cells`.
- `test_create_ab_test_requires_two_cells`.
- `test_get_ad_study_results_not_ready_is_stated`: a study with no results returns a plain not-ready status, not an empty winner.
- `test_eligibility_is_labeled_local_precheck` when no Meta endpoint is used.
- `test_update_ad_study_requires_a_change` and `test_cancel_ad_study_returns_prior_state`.

---

### Task D: Datasets and pixels

**Files:**
- Create: `meta_ads_mcp/tools/datasets.py`, `tests/test_tools_datasets.py`
- Modify: the shared registries

**Interfaces:**
- Consumes: `signals` group from Task 0.
- Produces: `list_datasets`, `get_dataset`, `get_dataset_stats`, `get_dataset_quality`, `list_custom_conversions` in `signals`. The eight event-rule and parameter tools only if a public endpoint is confirmed; their reads in `signals`, their writes in `writes`.

Tests this task must include beyond the loop:

- `test_dataset_stats_rejects_windows_over_28_days`.
- `test_dataset_stats_rejects_until_before_since`.
- `test_dataset_quality_flags_weak_scores_with_the_threshold_used`.
- `test_list_datasets_requires_exactly_one_of_account_or_business`.
- `test_permission_error_is_not_swallowed`: when the fake client raises the project's Meta API error for a permission failure, `list_datasets` raises it; it does not return an empty collection.
- If rule tools are built: `test_create_pixel_event_rule_starts_inactive`.

---

### Task E: Catalogs, products and product sets

**Files:**
- Create: `meta_ads_mcp/tools/catalogs.py`, `tests/test_tools_catalogs.py`
- Modify: the shared registries

**Interfaces:**
- Consumes: `catalog` group from Task 0.
- Produces: the sixteen Stream E tools. Reads (`list_catalogs`, `list_dpa_eligible_catalogs`, `get_catalog_diagnostics`, `get_catalog_dynamic_ads_health`, `list_catalog_data_sources`, `list_catalog_products`, `build_product_set_filter`, `list_product_sets`) in `catalog`; the rest in `writes`.
- Produces for Task F: nothing shared. The two catalog modules do not import from each other.

`build_product_set_filter` conditions are a list of objects with `field`, `operator` and `value`. `match` is `all` or `any`. Operators accepted are the ones Meta's product set filter documentation lists; reject any other with a message listing the accepted ones. The tool builds the filter, then calls the catalog's products edge with it and `summary` to return the match count and up to five sample products. Tool-contract classification: it is a read despite the name; confirm `tool_contracts.py` classifies it so, and if the `build_` name is ambiguous there, tag it read-only as `get_native_optimization_signals` is tagged.

Tests this task must include beyond the loop:

- `test_filter_builder_rejects_unknown_operator_listing_accepted_ones`.
- `test_filter_builder_all_and_any_produce_and_or_shapes`.
- `test_filter_builder_returns_match_count_and_sample`.
- `test_filter_builder_is_read_only_allowed`: it is in `read_only_tool_names()`.
- `test_product_price_uses_major_units`: `price=12.5, currency="USD"` is sent in the form Meta's product API documents; assert the exact sent value.
- `test_list_catalogs_requires_one_scope`.
- `test_delete_product_reads_then_deletes_and_returns_previous`; `test_delete_product_missing_object_sends_no_delete`.
- `test_permission_error_is_not_swallowed` for `list_catalogs`.
- `test_diagnostics_ordered_by_products_affected`.

---

### Task F: Catalog feeds and event sources

**Files:**
- Create: `meta_ads_mcp/tools/catalog_feeds.py`, `tests/test_tools_catalog_feeds.py`
- Modify: the shared registries

**Interfaces:**
- Consumes: `catalog` group from Task 0.
- Produces: the fifteen Stream F tools. Reads (`list_product_feeds`, `list_product_feed_uploads`, `list_feed_rules`, `list_catalog_event_sources`, `list_event_source_catalogs`, `get_catalog_event_source_health`, `get_catalog_event_source_recommendations`) in `catalog`; the rest in `writes`. `connect_catalog_event_source` and `disconnect_catalog_event_source` are added to `_WRITE_NAMES`.

Tests this task must include beyond the loop:

- `test_create_feed_schedule_validates_interval_and_url`.
- `test_feed_upload_requires_http_url`.
- `test_feed_uploads_errors_ordered_by_rows_affected`.
- `test_delete_feed_docstring_warns_products_are_removed`.
- `test_connect_and_disconnect_are_blocked_in_read_only`.
- `test_event_source_health_includes_28_day_history_when_present_and_says_when_absent`.
- `test_delete_feed_rule_reads_then_deletes_and_returns_previous`.

---

### Task G: Integration, ledger and docs (orchestrator)

**Files:**
- Create: `meta_ads_mcp/official_parity.py`, `tests/test_official_parity.py`
- Modify: `meta_ads_mcp/input_compat.py`, `meta_ads_mcp/tools/utility.py` (`get_capabilities`), `meta_ads_mcp/coordinator.py`, `README.md`

**Interfaces:**
- Consumes: every tool name produced by Tasks A to F as actually built.
- Produces: `OFFICIAL_TOOLS: dict[str, OfficialToolEntry]` with fields `status` (`"covered"` or `"unavailable"`), `tools: tuple[str, ...]`, `note: str`; `resolve_official_tool(name: str) -> str | None` returning the single target when an entry has exactly one tool.

- [ ] **Step 1:** Merge the six stream branches into `feature/official-mcp-parity` in the order A, B, C, D, E, F. Resolve shared-file overlaps by keeping both sides. Run the full suite after each merge.
- [ ] **Step 2:** Write `tests/test_official_parity.py`: `test_all_88_official_names_present` (the names from the spec's Appendix A, as a literal list in the test), `test_covered_entries_name_registered_tools`, `test_unavailable_entries_have_a_reason`, `test_single_target_names_resolve_through_call_tool`, `test_multi_target_name_error_lists_options`, `test_get_capabilities_official_tool_lookup`.
- [ ] **Step 3:** Run; expect import failure.
- [ ] **Step 4:** Implement the ledger, wire `resolve_official_tool` into `canonical_tool_name`, add the `official_tool` argument to `get_capabilities`.
- [ ] **Step 5:** Update the routing instructions in `coordinator.py` and the tool list and count in `README.md`.
- [ ] **Step 6:** Run the full suite. Commit.

### Task H: Verification and review (orchestrator)

- [ ] **Step 1:** Run each new read tool read-only against the real account with the working token. Record which returned data, which failed and why.
- [ ] **Step 2:** Launch the Codex `gpt-6.1-sol` max-effort review of the branch against the spec.
- [ ] **Step 3:** Check every finding against the code or a test before acting on it. Fix confirmed ones with a failing test first.
- [ ] **Step 4:** Full suite, then report.
