# Write safety and confirmed faults: design

Branch: `feature/write-safety`, from `master` at `47b5370`.

## Why

The parity PR matched Meta's official Ads MCP server tool for tool. Three habits
of that server were approved for this project and held back for this PR:

1. Every tool is marked read-only or destructive with MCP annotations.
2. Deletes and anything that starts spending show a preview and wait for a
   confirmation.
3. Inputs have strict formats, such as ids that must be digits.

This PR also fixes four faults in existing tools that live reads confirmed on
2026-11-13, and brings in one audience check that was held back until a live
upload showed it was safe.

## What the live official server does (read 2026-11-12, 106 tools)

- Annotations on all 106 tools, four hints each: `readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`. 62 are read-only. Of the
  44 writes, 21 are destructive (every delete, most updates) and 23 are not
  (creates, uploads, activate). `openWorldHint` is false everywhere.
- Confirmation on two tools only. The Instagram boost takes `confirmed`
  (false, the default, returns a plan and creates nothing). Discarding a draft
  returns a preview and a `confirmation_token`; the second call must send the
  token and is refused if the draft changed in between. Deletes have no confirm
  step; they rely on the destructive annotation.
- 58 parameters carry the pattern `^\d+$`.

## Rules for every part

- Graph API calls stay in `meta_ads_mcp/graph_api.py`. Tools return dicts and
  lists. Ids are strings.
- Additive changes are the default. A change to what an existing tool accepts or
  returns is allowed only as a real improvement and must be declared in
  `tests/fixtures/intentional_contract_changes.json` with `what_changed`,
  `why_better` and `migration`, in plain words. `tests/test_backward_compat.py`
  must pass.
- No code in this PR calls Meta during tests. No test reads a token.
- The repository is public: no account ids, business ids, names or spend figures
  in code, tests, fixtures or documents.
- `uv run --extra dev pytest -q` passes in full before a part is reported done
  (8,250 passed, 32 skipped at the start of this branch).

## Part A: four confirmed faults

**F1. Three targeting tools return nothing.** `search_behaviors`,
`search_demographics` and `get_targeting_categories` read the account edge
`broadtargetingcategories`, which lists only custom categories, so they return
an empty list for every input with no error. On 2026-11-13 all three returned 0
rows while the documented route `search?type=adTargetingCategory&class=behaviors`
returned 285.

Fix: read `search` with `type=adTargetingCategory` and the matching `class`
(`behaviors`, `demographics`, and the other documented classes for
`get_targeting_categories`), and match the caller's query against names locally.
Existing parameters and the shape of each returned row stay as they are where
the new route supplies the same fields; a field the new route does not supply is
left out, not invented. When the route returns rows but none match the query,
say so in the response (rows read, rows matched) so "nothing matches" can be
told apart from "nothing was read". The live test that only checks `count >= 0`
is tightened to expect at least one behaviour row for an empty query.

**F2. `list_audiences(subtype=...)` does not filter.** Meta ignores the
`subtype` parameter; the same mixed page comes back for any value.

Fix: stop sending `subtype`. Always request the `subtype` field (add it when the
caller's `fields` leaves it out, and remove it from the rows again if the caller
did not ask for it). Keep only rows whose subtype equals the requested one,
compared without regard to case. Because a page can hold few or no matches, keep
reading pages until `limit` matching rows are found or Meta reports no more,
with a ceiling of 10 pages per call. The summary reports rows read, rows
matched, and pages read; `summary.complete` is true only when Meta reported no
further page; `summary.next_after` continues from the last page read. Calls
without `subtype` behave exactly as before.

**F3. `preview_ad(account_id, creative_id)` always fails** with "(#100) ... The
parameter creative is required": the account preview edge takes no
`creative_id`.

Fix: for a `creative_id`, call `GET /{creative_id}/previews`. `account_id` is no
longer required with `creative_id` (it is still accepted, and still required
with a `creative` payload). `ad_formats` works for this route the same way it
does for the others.

**F4. Spend caps and minimum-spend targets come back 100 times too large.**
`get_ad_account`, `get_campaign` and `get_adset` convert budgets to major units
but return these fields in minor units under a description that says major
units.

Fix: convert `spend_cap`, `daily_spend_cap`, `lifetime_spend_cap`,
`daily_min_spend_target` and `lifetime_min_spend_target` the same way the budget
fields are converted, including currencies with no minor unit. These five are
the ones the live read confirmed. Keep them in a named set of their own next to
`MONETARY_FIELDS`, so more fields can be added once a live read confirms each.
This changes returned values, so it is declared as a behaviour change.

## Part B: MCP annotations on every tool

One table, in a new module `meta_ads_mcp/tool_annotations.py`, gives each of the
180 tools its four hints. The table is applied to the registered tools at
start-up, so no tool definition file needs editing.

- `readOnlyHint` is true when the tool changes nothing at Meta and nothing on
  this machine.
- `destructiveHint` is true when the tool deletes, removes, disconnects, cancels
  or overwrites an existing value: every `delete_*`, every `update_*`,
  `remove_audience_users`, `disconnect_catalog_event_source`, `cancel_ad_study`,
  `set_catalog_access`, `batch_update_catalog_products`,
  `delete_overflow_artifact`, and the three `set_*_status` tools. The hint
  describes the worst thing a tool can do, and the status tools accept
  `ARCHIVED`, which cannot be undone from this server. (The official server's
  activate tool is not destructive because it can only activate.) It is false
  for creates, uploads, duplicates, `add_audience_users`,
  `connect_catalog_event_source` and the boost tool.
- `idempotentHint` is true for reads, deletes, status sets and updates that set
  an absolute value; false for anything that creates a new object each time it
  runs or uploads rows.
- `openWorldHint` is false, except for a tool that fetches a web address the
  caller or an ad supplies, or returns public content from outside the user's
  own assets (the public ad library search, landing-page reads).
- `call_tool` can run any tool, so its hints are worked out from the tools it
  can reach in the current mode: read-only only if every one of them is,
  destructive if any one is. With `META_READ_ONLY` on it is still not read-only
  and still destructive, because `delete_overflow_artifact` (which deletes a
  saved file on this machine) stays reachable in that mode.

Tests:
- Every registered tool has exactly one entry, and every entry names a
  registered tool. A new tool without an entry fails the suite.
- Every tool the read-only gate blocks (`read_only.read_only_tool_names()`) has
  `readOnlyHint` false.
- For every tool, `tool_contracts._tool_effect` gives the same answer with the
  annotations as it gave from the name rules alone. Annotations take precedence
  in that function, so this is what keeps routing unchanged.
- Every tool that accepts `ARCHIVED` or `DELETED` as a status is marked
  destructive.
- The annotations are present in what a client receives: the directly listed
  tools, and each tool returned by `search_tools`. Most clients see tools only
  through search, so this second place matters most.

## Part C: strict input formats

Each item is a declared contract change.

**C1. Ids that Meta defines as numbers must be digits.** Today an id is refused
only when it holds a slash, `?`, `#`, `..` or a space. A name or a placeholder
passed as an id costs a rate-limited request and returns an unclear error from
Meta.

The implementer first lists every id-bearing parameter across the 180 tools and
sorts each into a class. That list is part of the report back. The digits rule
applies only to classes that are always numeric: campaign, ad set, ad, creative,
custom audience, pixel or dataset, business, catalog, product set, product feed,
Page, Instagram account, Instagram media, study, video, system user, app.
Account ids accept digits or `act_` followed by digits.

These stay as they are, each with a test that proves it is still accepted: image
hashes, Page post ids and comment ids of the form `digits_digits`, `me`,
retailer ids and other caller-chosen keys, overflow `export_id`s, paging
cursors, async report run ids if any is non-numeric, and any id class the
implementer finds that is not provably numeric. When in doubt, leave the class
alone and list it in the report.

The refusal names the parameter and says what a valid value looks like. It does
not repeat the value. Where a parameter is always numeric, the tool's input
schema also carries the pattern, as the official server's does.

**C2. Money and count parameters refuse `true` and `false`.** `daily_budget`,
`lifetime_budget` and `bid_amount` on the create and budget tools, `ratio` on
`create_lookalike_audience`, and `retention_days` read `true` as 1 on tools that
came from `master`. They are refused with a message that names the parameter.

**C3. Web addresses with an embedded login are refused** on
`upload_creative_asset`, `create_ad_creative`, `update_creative` and
`preview_ad`, using the existing `url_secrets.refuse_url_credentials`. The tools
added by the parity PR already do this.

## Part D: preview, then confirm

Design not settled. It is written after the owner chooses between the options
put to them on 2026-11-16, and this section is replaced then. Parts A to C do
not depend on it.

## Part E: audience upload session check

Already on the branch (`fe004d5`). A batch acknowledged for a different upload
session is not counted. A live upload on 2026-11-15 showed Meta returns the
session id that was sent, as text, which was the condition for keeping it.

## Not in this PR

Everything else in the 2026-11-13 documentation comparison (faults F5 to F34,
the dated changes, missing capabilities, efficiency items), and the shared
action-list reader that stores an unreadable action value as 0.
