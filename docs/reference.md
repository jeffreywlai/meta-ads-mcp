# How the tools behave

The details that do not fit in a tool table. Setup and the tool list are in the
[README](../README.md).

- [Large responses](#large-responses)
- [Reports](#reports)
- [Async reports](#async-reports)
- [Creative fatigue report](#creative-fatigue-report)
- [Finding ads by name](#finding-ads-by-name)
- [Landing pages](#landing-pages)
- [Replacing a placement image](#replacing-a-placement-image)
- [Image rules on an ad](#image-rules-on-an-ad)
- [Audiences](#audiences)
- [Change history](#change-history)
- [Read-only mode](#read-only-mode)
- [Tool markings](#tool-markings)
- [Local file uploads](#local-file-uploads)
- [Input formats](#input-formats)
- [Errors and retries](#errors-and-retries)
- [Default account](#default-account)
- [API version 26](#api-version-26)
- [Checking fields against Meta's SDK](#checking-fields-against-metas-sdk)

## Large responses

A result over 64,000 bytes is saved in full to a local file. The tool returns an
`export_id` in its place. Nothing is cut off.

To read it, call `call_tool` with `name="read_overflow_artifact"` and the
`export_id`:

- A result made of items or rows comes back one page of items at a time. Pass
  each `next_item_offset` to get the next page.
- If you need the original JSON byte for byte, start with `offset=0` and follow
  each `next_offset`. An item too big for one response can only be read this way.
- Use the offsets the tool gives you. Made-up offsets are refused.

When you are done, delete it: `call_tool` with `name="delete_overflow_artifact"`.

Files are kept for 24 hours. The store holds at most 100 files and 1 GB. Change
those limits with `META_EXPORT_TTL_SECONDS`, `META_EXPORT_MAX_FILES` and
`META_EXPORT_MAX_BYTES`. Files go in `META_EXPORT_DIR`, or in the system's
temporary folder under `meta-ads-mcp-exports` if that is not set. Only the user
who runs the server can read them.

If the machine cannot store the file safely, nothing is saved and the tool tells
you to narrow the request.

## Reports

### What `get_entity_insights` returns for actions

`get_insights` is the same tool under its old name.

- **Default:** action counts and values as simple maps, plus the derived
  metrics. Meta's raw action arrays are left out. Custom actions are included.
  Empty fields you did not ask for are left out.
- **`flatten_actions=["purchase"]`:** a `purchase` column on each row, and no
  maps.
- **`include_raw_actions=true`:** the raw arrays as Meta sends them.

You need `include_raw_actions=true` in two cases:

1. To read values for a named attribution window. The maps are not split by
   window.
2. To use any `action_breakdowns` other than `action_type`, such as
   `action_device`. Read those dimensions from the raw records. Without the
   flag the call is refused before anything is sent, so a dimension is never
   dropped without telling you.

`summarize_actions` and the JSON and CSV exports are unchanged.

### One ad's placements, across its copies

```
level="ad"
object_id="act_ACCOUNT_ID"            (or a campaign or ad set id)
breakdowns=["publisher_platform","platform_position"]
filtering=[{"field":"ad.name","operator":"CONTAIN","value":"NAME_PREFIX"}]
```

`level` sets what each row is. There is no separate `row_level`. The same
filters work in exports and async reports.

### Getting every row

A normal call returns one page, and its summary covers that page only. Check
`summary.complete`, and continue with `paging.after` until it is true.

Or pass `fetch_all=true` to collect up to 1,000 rows in one call. This also
works on `get_insights` and `export_insights`. Totals are worked out after the
last page. If the scan cannot finish, the call fails; it never returns part of
the answer as if it were all of it. For more than 1,000 rows, narrow the query
or use an async report.

### Click against view attribution

Call `get_entity_insights` with explicit dates and:

```
fields=["actions","action_values"]
use_unified_attribution_setting=false
action_attribution_windows=["7d_click","1d_view"]
include_raw_actions=true
```

Read the window keys from the raw purchase entries. Meta's plain `value` can
differ from them, even when you ask for one window. The core metrics, the action
maps and `flatten_actions` all use that plain `value`. Do not add window values
together, and do not add action names that overlap.

Past changes to an ad set's settings are not in this report. For those, use
change history or your own warehouse.

## Async reports

- **Fields:** a short set by default. Pass `field_preset="full"` or your own
  `fields` for more.
- **Actions as columns:** pass `flatten_actions=["purchase","purchase_value"]`
  both when you create the report and when you fetch it.
- **Raw actions:** left out when fetching unless `include_raw_actions=true`.
  Fields you asked for, such as spend, impressions and clicks, are always kept.
- **Polling:** creating a report returns `poll_arguments`. Pass that whole
  object to `get_async_insights_report` every time, so your settings carry over.
  Pass `wait=true` to let the server wait for a limited time, or watch
  `job.ready`, `job.terminal` and `job.poll_after_seconds` yourself.

### Totals from a very large report

```
summarize_async_insights_report(report_run_id="REPORT", group_by=["date_start"], metrics=["spend"])
```

This returns totals per group, not the rows.

- Spend is an exact decimal string in whole currency units. Impressions and
  clicks are whole numbers.
- Only spend, impressions and clicks can be totalled. Reach, ratios and action
  counts cannot be added up this way.
- A metric missing from the report gives a total of null, not zero.
- Each call reads a limited number of pages. If the result has
  `next_page_arguments`, call again with them and add the group totals together.
- If the report is still running, call again with the same arguments.

To work out a share of account spend, run a separate account report for the
total.

## Creative fatigue report

Call `get_creative_fatigue_report` with `account_id`, or with `level="account"`
and `object_id="act_ACCOUNT_ID"`. It compares two periods ad by ad, reads every
page, and names each ad, ad set and campaign.

**Which ads are compared.** Only ads with at least 1,000 impressions in each
period. Change that with `min_impressions`; 0 removes the requirement.

**Order of findings.** Ads with the most data first: the smaller of the two
periods' impressions, largest first. Then the biggest CTR decline, the biggest
frequency increase, the highest current spend, and ad id. This is a fixed rule
for what to look at first. It is not a statistical test, and `confidence` is
always null.

**Severity.** Medium means CTR fell at least 20% and frequency rose at least
20%. High means at least 50% for both.

**Limits.** Each period reads up to 1,000 ads. Raise it with `max_ads`, up to
10,000. If the account has more ads than the limit, or paging breaks, the call
fails. It does not diagnose from part of the data.

**Creative ids.** Each flagged ad gets its current creative id, using up to 100
extra reads. Set `max_creative_lookups` from 0 to 1,000; 0 turns this off.

- If a read fails, is rate limited or the cap is reached, the finding stays and
  the result says why the id is missing.
- After a temporary error that has used up its retries, the remaining lookups
  are skipped and marked `skipped_transient_error`.
- Groups by creative id cover flagged ads only.
- The id is the creative on the ad now. That may not be the creative it ran
  with in the earlier period. Nothing is guessed from ad names.
- `creative_identity_complete` says whether every id was read. It is separate
  from whether the report scan was complete.

## Finding ads by name

```
list_ads(name_contains_any=["Ada","Grace"], whole_term_match=true,
         effective_status=["ACTIVE"], fields=["id","name"])
```

- One call searches every name and returns each matching ad once, with
  `matched_terms`.
- The status filter applies to every name.
- Meta picks the candidates with its own matching. The server then checks them
  ignoring case.
- With `whole_term_match`, punctuation and underscores separate words, so `Ada`
  does not match `Adam`.
- There is no 1,000-ad limit. If paging breaks, the call fails.
- "Complete" means complete for the status and scope you asked for, not every
  ad the account has ever had.

## Landing pages

Ask `list_ads` for `fields=["id","name","destination_urls"]`. You get the links
set on each ad's creative: the post, carousel cards, video buttons and asset
pools. The large creative object is not returned.

These are the links as configured. Redirects, catalog substitutions and
`url_tags` are not followed or applied.

## Replacing a placement image

There is no single tool for this. Do it in steps:

1. Read the ad's creative.
2. Upload the new image.
3. Create a new creative that keeps every other field and placement rule.
4. Preview it if you want to.
5. Attach it with `update_ad_creative`.
6. Read the ad back.

The server never rewrites creatives in bulk based on an ad name or a file name.

## Image rules on an ad

`get_ad_image` shows which image an ad is set to use in which placement. Each
image comes with its crop, whether it is the default, its placement rules and
any label that could not be matched. Positions are given in the names reports
use; Instagram `stream` is shown as `feed`.

These are rules about what may serve. They do not prove which image served.
Placement reports show delivery by placement, not by image, when more than one
image is allowed.

Pass `ad_ids=["AD_1","AD_2"]` for up to 100 ads in one call. Each creative and
image is read once per call. Image hashes that cannot be found are reported as
such.

## Audiences

`list_audiences` returns `summary.complete`, `summary.truncated` and
`summary.next_after`. Follow the cursor before treating the list as complete.
With `subtype`, Meta is asked to return only audiences of that type. Any row of
another type is dropped and the page is marked incomplete. The summary reports
`rows_read` and `rows_matched`.

`create_lookalike_audience(countries=["US","CA","AU"], ratio=0.01, ...)` builds
Meta's `lookalike_spec` from those inputs. You cannot override it through
`params`. If Meta says a lookalike from that source already exists, you get that
error. The server never deletes an audience to make a create succeed.

What was tested, and what Meta's API would not allow, is in the
[2026-10-22 report](usage-report-2026-10-22-remediation.md).

## Change history

### Every change in a period

Use `export_change_history`:

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

**What is included.** An account export has every level and every event type
Meta returns. A campaign or ad set export includes everything under it. The
server finds those by scanning the account's activity and looking up each
object's parent, including deleted objects it can still read. It does not rely
on what is active today or on Meta's `add_children` flag.

An object whose parent cannot be read stays in the export with
`scope_match.status="unresolved"`, and `complete` stays false. If you know ids
that can no longer be linked to their parent, pass them in `extra_oids`.

**Dates.** `since` and `until` are required.

- A date alone for `until` covers that whole day in `time_zone` (UTC if not
  given), including days when the clocks change.
- A timestamp must carry an offset. `since` is included; `until` is not.
- Unix seconds are accepted.
- An end date in the future is cut to the time of the export.
- Every continuation uses the same dates as the first call.

**Reading the result.** The `items` in the response are a preview. To read
everything:

1. Follow `delivery.read_call`, then each `next_item_offset`, and save the
   events.
2. For a single event too big for one page, use `delivery.byte_read_call` and
   follow `next_offset`.
3. If the export has a `next_call`, run it and repeat. Combine every part.
4. Delete each file only after you have saved its contents.

`complete=true` from the file reader means you have read that file. It does not
mean every page from Meta was exported.

**Limits.** One export call stops at `max_pages` (100 by default), 45 seconds, or
about 32 MiB of events. When it stops early, or a later page fails, it keeps
what it has, says what is covered, and gives you a continuation. A missing or
repeated cursor means you need a smaller date range. Parent lookups stop at 200
per call; anything not looked up stays visible as unresolved.

The last part of a continued export is not the whole export.
`coverage.api_pages_exhausted` says the end was reached.
`coverage.starts_at_beginning` says whether this part started from the
beginning.

### A quick look

By default `list_change_history` returns 50 records from a fixed seven-day
window.

- Follow `next_call` even when the current page has no matching items.
- For a campaign or ad set it covers that object only, unless
  `include_descendants=true`.
- `export_call` is a ready-made call for the full export.
  `unfiltered_account_export_call` is the same period with every filter removed.

### Filters and fields

- Leave out `category` and `uid` when you want every change. `category="ALL"`
  means no category filter.
- A category is a filter, not a level. `category="AD_SET"` can miss a change to
  an ad set's optimization goal.
- `entity_level` uses today's names. Meta's old `object_type` values map as
  `CAMPAIGN_GROUP` → campaign, `CAMPAIGN` → ad set, `ADGROUP` → ad.
- Meta's raw fields are kept: who, which app or tool, the event type and
  `extra_data`. Parsed details are added, with a flag for whether old and new
  values are available.
- A deletion can appear as a status change.
- Payment amounts stay in Meta's minor units.
- Never fill in an old or new value that is missing.

### What "complete" means

`complete=true` means every event Meta's API returned for that query was read
from the beginning, with nothing unresolved. It does not mean every action
taken in Ads Manager is there. Permissions, how long Meta keeps events, actions
Meta does not log, and delayed events all limit what the API returns.

An empty or partial result is not proof that nothing changed. When you report
change history, always state `coverage`, the filters used, the dates and
`ai_instructions`.

## Read-only mode

Set `META_READ_ONLY=true` in the launch environment and restart.

- Tools that change anything are hidden from search and refused before they
  run: ad changes, uploads, A/B test setup and token changes. This holds
  through `call_tool` and the old tool names too.
- A tool the server does not recognise is refused.
- Report jobs and deleting saved response files still work.
- Analysis agents need `search_tools`, `call_tool` and `get_capabilities`.
  Allowing only the underlying tool names is not enough.
- Where you can, also use a token with only `ads_read`.

The default is writable. Nothing turns read-only mode on for you.

## Tool markings

Every tool carries the four MCP markings. A client uses them to decide which
calls it may run without asking you, and which it may safely retry.

- `readOnlyHint`: the tool changes nothing at Meta and nothing on this machine.
- `destructiveHint`: the worst the tool can do deletes, removes, disconnects,
  cancels, archives or overwrites something that already exists. Every delete
  and update is marked, and so are the three status tools, because `ARCHIVED`
  cannot be undone from this server.
- `idempotentHint`: running the same call twice leaves things as one call did.
  Tools that make a new object or upload rows on every run are not marked.
- `openWorldHint`: the tool reaches outside your own assets. Only
  `search_ads_archive` (the public Ad Library) and the tools that have Meta
  fetch a web address you give (image, video and product feed uploads) are
  marked.

The markings appear in the tool list and at the end of each `search_tools`
match, for example `read-only: no, destructive: yes, idempotent: yes,
open-world: no`. `search_tools` is marked read-only. `call_tool` carries the
markings of the most dangerous tool it can run. It is not marked read-only even
in read-only mode, because `delete_overflow_artifact` still runs there and
deletes a saved file on this machine.

All markings live in one table, `meta_ads_mcp/tool_annotations.py`. A new tool
without an entry fails the tests.

## Local file uploads

Set `META_UPLOAD_DIR` to one folder and put the file there. Five tools read a
local file through `file_path`: `upload_creative_asset` (images),
`upload_ad_video`, `add_audience_users`, `remove_audience_users` and
`create_product_feed_upload`.

- Until the folder is set, they refuse every local file.
- A path that leads outside the folder is refused, including through a link.

The reason: the model also reads text written by strangers, such as ad comments.
A file path it supplies must not be able to send some other file on your machine
to Meta.

## Input formats

Some inputs are checked before the tool starts, so a wrong value is refused
without a request to Meta. The message names the parameter and says what a
valid value looks like. It never repeats the value, because a value put in the
wrong field can hold private text.

- **Ids that Meta defines as numbers must be digits.** This covers campaign,
  ad set, ad, creative, custom audience, pixel or dataset, business, catalog,
  product set, product feed, Page, Instagram account and media, study, video,
  system user and app ids (including `business_app`, the second name
  `generate_system_user_token` takes for `app_id`), and each entry of a list of
  them. Send the id as a
  string: `'1234567890'`. A JSON number was refused before and still is.
- The same check covers these ids when they sit inside another argument:
  `create_ad`'s `creative.creative_id` and `creative.id`; the campaign, ad set,
  ad and ad account ids in A/B test and lift study cells; the pixel, app,
  dataset, product set and catalog ids in lift study objectives; and
  `boost_instagram_post`'s `promoted_object.pixel_id` and
  `promoted_object.application_id`. The message names the path, such as
  `creative.id`.
- An ad account id is digits or `act_` followed by digits. `object_id` on the
  reporting tools takes an ad account, campaign, ad set or ad id.
  `get_account_pages` also takes `me`.
- Spaces around an id, and a blank optional id, are treated as before.
- Ids of other forms are not checked: image hashes, Page post and comment ids
  (`123_456`), paging cursors, `export_id`, `report_run_id`, retailer and product
  ids, interest ids. The full list, with the reason for each, is in
  `meta_ads_mcp/id_formats.py`.
- Each checked id's pattern is in the tool's input schema, so
  `get_capabilities(tool_name=...)` shows it.
- **Budgets, bids, lookalike ratios and retention days refuse `true` and
  `false`.** Before, `true` was read as 1 and sent to Meta. Numbers and numeric
  text such as `'12.50'` work as before.
- **A web address with a login in it is refused** (`https://user:pass@host/...`)
  by the creative, video, boost and catalog tools, including
  `upload_creative_asset`, `create_ad_creative`, `update_creative` and
  `preview_ad`, wherever the URL sits in the creative, including inside a
  sub-object sent as JSON text. Meta stores the URL on the ad and shows it in
  responses.

## Errors and retries

A failed call to Meta returns a structured error: Meta's code and subcode, what
Meta says to do, when to retry, and usage figures when Meta gives them.

- Reads are retried on temporary failures.
- Creates, updates and deletes are never retried automatically when it is
  unclear whether they went through.
- If the error has `mutation_outcome_unknown=true`, then `retryable` is false.
  Check the object's current state before you decide to send it again.

## Default account

`META_DEFAULT_ACCOUNT_ID` is optional. Change history for a campaign, ad set or
ad does not need it: the server looks up which account owns the object, and
checks any account you gave against that.

## API version 26

The default is `v26.0`. Restart the server after changing `META_API_VERSION`.

These need v26 or newer and are refused up front if you have set v25:

- `get_native_optimization_signals`: Meta's own delivery, learning, issue,
  automation and recommendation data for a campaign, ad set or ad.
- `include_instagram_profile_follow=true` on `get_entity_insights`.
- The `recommendation_names` and `recommendation_stages` filters on
  `get_recommendations` and the five opportunity tools. Pass a list or
  comma-separated text. For example, `recommendation_stages=["PCR"]`. Meta
  applies them before the server's own category filter. The stage codes `MFR`,
  `PCR` and `PFR` are passed through as they are; the server does not attach a
  meaning to them. Without the filters you get the same broad scan as before,
  on any version.

New fields and breakdowns need no new parameter. For example,
`get_entity_insights(..., fields=["shop_clicks"])` or
`breakdowns=["placement_path"]`. Campaign reads take custom `fields` too. That
a field can be requested does not mean your account is eligible for it or that
a combination is valid.

Two fields Meta's SDK lists did not work on the tested account: campaign
`bid_constraints` is rejected, so it is not read by default, and the ad persona
field needs a permission the token did not have. `get_v26_notes` has the full
list.

## Checking fields against Meta's SDK

This compares the fields the server reads by default with the schemas in Meta's
Python Business SDK:

```bash
uv run audit-meta-sdk-schema \
  --sdk-repo /path/to/facebook-python-business-sdk \
  --base-ref 25.0.3 \
  --target-ref 26.0.2
```

The API version is `v26.0` and the SDK version is `26.0.2`. They are different
numbers. The findings, and the read-only live checks, are in
[the v26 compatibility audit](v26_compatibility_audit.md).
