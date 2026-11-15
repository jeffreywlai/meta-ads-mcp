# Official Meta Ads MCP Parity, Phase Two Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every capability gap between this server and the 106 tools Meta's official Ads MCP server actually serves, changing the call contract of a tool already on `master` only as a declared, genuine improvement.

**Architecture:** Seven independent work streams, each in its own git worktree branched from the phase-two foundation commit, each owning a fixed set of files. Extensions to existing tools are additive optional parameters. The orchestrator merges the streams, then updates the parity ledger, README and search router.

**Tech Stack:** Python 3.12, FastMCP 3.4.8, `httpx`, `pydantic` 2, `pytest` with `pytest-asyncio`, Meta Marketing API v26.0.

**Spec:** `docs/superpowers/specs/2026-11-12-official-mcp-parity-phase2-design.md`, which builds on `docs/superpowers/specs/2026-11-12-official-mcp-parity-design.md`. Read both in full. Tool names, parameters and behaviours are defined there and are not repeated here.

## Global Constraints

- Tools on `master` (the 102 in `tests/fixtures/master_tool_contract.json`) do not change their contract by accident. Additive changes (an optional parameter, a new response field) need no declaration and are preferred when they give the user the same result. A breaking change (removed or renamed tool, removed parameter, new required parameter, changed default, tighter validation, an added step, a changed returned field) is allowed only as a genuine improvement to the experience of using the tool, and only when the spec's tables call for it or the tool's current behaviour is wrong. It must be declared in `tests/fixtures/intentional_contract_changes.json` with `why_better` and `migration`. Any other breaking idea is proposed in the stream's report, not implemented. `tests/test_backward_compat.py` must pass; no stream edits `master_tool_contract.json`.
- For every extended `master` tool that is meant to stay compatible, a test proves that a call made the old way sends exactly the same request as before the change.
- No tool gets MCP annotations. No existing tool gets a confirmation step.
- New tools and new parameters use `GraphId` and `AccountId` from `meta_ads_mcp/tool_types.py` for ids Meta documents as numeric. Tools on `master` keep plain `str` ids.
- Evidence rule: an endpoint (object type or edge) is built on only if Meta's public reference documentation describes it; SDK presence alone is not enough. On a documented endpoint, a field or parameter may come from the documentation or the SDK source at tag `26.0.2`. Anything unconfirmed is reported, not guessed and not built.
- No live calls to Meta. Do not read `~/.claude.json`, `~/.meta_employee_token`, any `.env`, or any real access token. Tests use fake clients.
- All Graph API traffic goes through `GraphAPIClient`. Object ids and edge names passed to its helpers must be single path segments; the client rejects anything else.
- Every write tool is classified as a write by `meta_ads_mcp/tool_contracts.py`, listed in `TOOL_GROUPS["writes"]` and in no other group, and added to the hard-coded set in `test_live_catalog_classifies_every_current_mutation` in `tests/test_tool_contracts_routing.py`.
- Every new write tool supports `dry_run=true`, which returns exactly what would be sent and sends nothing. Creates default to paused or inactive where the object has such a state. Updates and deletes read the object first and return it as `previous`; if that read fails, nothing is written.
- A list is complete only when Meta's response has no next page. A next page with no usable cursor is incomplete, with a stated reason. Repeated cursors stop the read. The number of requests is capped separately from the number of rows.
- A response never claims completeness, health, readiness or success that the data read does not support.
- No personal data in logs or error messages. A password is never returned or logged.
- Tool docstrings must not contain the `+` character. Keep default responses small; a response above about 63 KB is moved to an overflow artifact.
- Edits to shared files are additive: add lines, never reorder or reformat existing ones. Streams do not edit `README.md`, `meta_ads_mcp/official_parity.py`, `meta_ads_mcp/input_compat.py`, `meta_ads_mcp/intent_routing.py`, `meta_ads_mcp/coordinator.py`, `meta_ads_mcp/read_only.py`, `tests/fixtures/master_tool_contract.json`, `SPEC.md`, `CLAUDE.md`, or anything under `docs/`.
- Each new tool module must be added to the import list and to the `_patch_clients` module list in `tests/test_tool_contracts.py`, or the catalog-wide smoke test runs it with a real client. Add `TOOL_OVERRIDES` entries where a tool needs sample arguments, and any new client method to `UniversalFakeClient`.
- `uv run --extra dev pytest` passes in full before a task is reported done.

## Review Focus

1. An existing artifact calls an extended tool exactly as it does today. Expected: the same request reaches Meta and the same response fields come back, unless a declared change in `intentional_contract_changes.json` says otherwise. Owned by P1, P2, P3 and P4.
2. A caller gives a new named parameter and the same key inside `params`. Expected: a `ValidationError` before any network call, naming both. Owned by P2.
3. A batch write partly fails or times out. Expected: per-item results that say what was applied and what was not, and a plain statement of whether retrying is safe. Owned by P5.
4. A one-step tool creates a campaign, then fails creating the ad set. Expected: the error names what was created so it can be found and removed, and nothing is left active. Owned by P1.
5. A report covers several entities and one cannot be read. Expected: results for the others, with the unreadable one named as missing; never a silent omission. Owned by P3.

---

## File Structure

| Stream | Owns |
|---|---|
| P1 | `meta_ads_mcp/tools/creatives.py`, `meta_ads_mcp/tools/assets.py`, new `meta_ads_mcp/tools/creative_formats.py`, `tests/test_tools_creatives.py`, `tests/test_tools_assets.py`, new `tests/test_tools_creative_formats.py` |
| P2 | `meta_ads_mcp/tools/campaigns.py`, `meta_ads_mcp/tools/ads.py`, `meta_ads_mcp/tools/execution.py`, `tests/test_tools_campaigns.py`, `tests/test_tools_ads.py`, `tests/test_tools_execution.py` |
| P3 | `meta_ads_mcp/tools/insights.py`, `meta_ads_mcp/tools/performance_signals.py`, `meta_ads_mcp/tools/discovery.py`, `meta_ads_mcp/tools/research.py`, and their test files |
| P4 | `meta_ads_mcp/tools/experiments.py`, `meta_ads_mcp/tools/audiences.py`, `tests/test_tools_experiments.py`, `tests/test_tools_audiences.py` |
| P5 | `meta_ads_mcp/tools/catalogs.py`, `tests/test_tools_catalogs.py` |
| P6 | `meta_ads_mcp/tools/catalog_feeds.py`, `tests/test_tools_catalog_feeds.py` |
| P7 | new `meta_ads_mcp/tools/business_access.py`, new `tests/test_tools_business_access.py` |

Shared registries (`meta_ads_mcp/tools/__init__.py`, `TOOL_GROUPS`, `INTENT_GUIDE` and `list_mutation_tools` in `meta_ads_mcp/tools/utility.py`, `_WRITE_NAMES` in `meta_ads_mcp/tool_contracts.py`, `tests/test_tool_contracts.py`, `tests/test_tool_contracts_routing.py`, the end of the `GraphAPIClient` class) take additive edits from any stream.

---

### The loop every stream task follows

Per tool or extension, in the order the spec lists them:

- [ ] Confirm the Graph endpoint, fields and parameters in Meta's reference documentation or the SDK source. Record the page or file.
- [ ] For an extension to a tool on `master`: first write a test that captures the request an old-style call sends today, and confirm it passes before any change.
- [ ] Write failing tests with a fake client: the object id and edge requested, the exact parameters or payload sent, the response keys, each `ValidationError` with no client call recorded, and for writes the `dry_run` case.
- [ ] Implement. Register new tool names in the right `TOOL_GROUPS` list and, for writes, in the routing test's set and in `_WRITE_NAMES` when the name does not start with `create_`, `update_`, `delete_`, `set_` or `upload_`.
- [ ] Run the stream's test files, then `tests/test_tool_contracts.py`, `tests/test_coordinator.py`, `tests/test_read_only.py` and `tests/test_backward_compat.py`.
- [ ] Commit.

When the stream is done:

- [ ] Add `INTENT_GUIDE` entries and, for writes, `common_paths` entries in `list_mutation_tools`.
- [ ] Run `uv run --extra dev pytest -q` and confirm no failures.
- [ ] Commit, then report as the phase-one spec's "Reported back by each stream" section requires, adding for each extended `master` tool the name of the test that proves old-style calls are unchanged.

---

### Tasks P1 to P7

Each task implements its stream's table in the phase-two spec, on the files listed above, following the loop. Tests each task must include beyond the loop:

**P1**
- `test_create_ad_creative_from_inputs_dry_run_sends_nothing_and_returns_the_spec`
- One test per format asserting the exact assembled spec, and one per invalid combination asserting a `ValidationError` with no client call.
- `test_boost_one_step_failure_after_campaign_creation_names_what_was_created`
- `test_boost_one_step_creates_everything_paused` and `test_boost_plan_only_sends_nothing`
- `test_preview_ad_single_format_call_is_unchanged` and `test_update_creative_without_labels_is_unchanged`
- `test_audience_adsets_targeting_scan_reports_incomplete_scan`

**P2**
- `test_duplicate_defaults_to_paused` for each of the three copy tools, and `test_duplicate_dry_run_sends_nothing`
- `test_named_parameter_and_same_params_key_together_raise` for each extended create tool
- `test_create_campaign_params_only_call_is_unchanged`, `test_create_ad_set_params_only_call_is_unchanged`, `test_create_ad_params_only_call_is_unchanged`
- `test_set_status_active_and_paused_calls_are_unchanged` and `test_set_status_accepts_archived`
- `test_update_adset_spend_caps_use_major_units`

**P3**
- `test_get_entity_insights_without_sort_is_unchanged` and `test_get_entity_insights_sort_is_passed_to_meta`
- `test_trend_cost_per_result_follows_metas_result_definition`
- `test_multi_entity_report_names_an_unreadable_entity_and_returns_the_rest`
- `test_search_ads_archive_with_terms_is_unchanged`
- `test_list_ad_accounts_keeps_every_field_it_returned_before`

**P4**
- `test_create_ab_test_campaign_and_adset_cells_are_unchanged`
- `test_update_custom_audience_without_rule_is_unchanged`

**P5**
- `test_batch_products_partial_failure_reports_each_item`
- `test_batch_products_states_whether_a_retry_is_safe`
- `test_batch_products_dry_run_sends_nothing`
- `test_attribute_values_scan_reports_coverage`

**P6**
- `test_update_feed_rule_reads_then_updates_and_returns_previous`
- `test_feed_credentials_are_never_returned_or_logged`

**P7**
- `test_preview_catalog_access_change_is_read_only_allowed`
- `test_set_catalog_access_is_blocked_in_read_only`
- `test_set_catalog_access_reads_current_access_first_and_returns_before_and_after`
- `test_business_user_listing_returns_only_identifying_fields`

---

### Task Q: Integration and docs (orchestrator)

- [ ] Merge P1 to P7 one at a time, resolving additive overlaps, running the full suite after each.
- [ ] Update `meta_ads_mcp/official_parity.py`: move an entry from `partial` or `missing` to `covered` only where its gap is closed, and rewrite the note of any entry that stays `partial` to list what remains.
- [ ] Extend `_SUBJECT_SCOPED_WRITE_VERBS` in `meta_ads_mcp/intent_routing.py` for any new write tool whose name starts with an unknown verb (`duplicate`, `batch`), with routing tests.
- [ ] Update `README.md` and the routing text in `meta_ads_mcp/coordinator.py`.
- [ ] Full suite. Commit.

### Task R: Verification and review (orchestrator)

- [ ] Run each new or extended read tool read-only against the real account, paced to stay under the app's rate limit. Update `LIVE_VERIFIED_TOOLS` from the results.
- [ ] Launch the second Codex `gpt-6.1-sol` max-effort review, covering phase two and the phase-one fixes.
- [ ] Check every finding against the code or a test before acting on it. Fix confirmed ones with a failing test first.
- [ ] Full suite, then report.
