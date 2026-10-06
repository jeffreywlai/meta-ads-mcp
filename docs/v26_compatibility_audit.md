# Marketing API v26 compatibility audit

Audit date: 2026-10-05 (original v26 gate: 2026-08-13)

## Gate status

- Static SDK schema audit: **PASS — all 21 default projections against SDK 26.0.2**
- Required read-only v26 live smoke tests: **PASS — 4 core/filter/native tests**
- Optional SDK field probes: **2 accepted; 2 unavailable to the tested account/token**
- Combined current live run: **6 passed, 2 explicitly skipped on 2026-10-05**
- Native optimization signals: **IMPLEMENTED — version-gated to v26+**

The repository now defaults to `v26.0`. An explicit `META_API_VERSION` override
still wins. Native optimization reads and the opt-in Instagram profile-follow
metric fail early when an override selects v25. No account configuration,
campaign, ad set, ad, creative, budget, or optimization enrollment was changed.

## Authoritative baseline

The audit compares generated field schemas in Meta's official Python Business
SDK tags `25.0.3` and `26.0.2`. The target configuration declares Graph API
`v26.0` and SDK `v26.0.2`; a package patch version is not an API version.

- [Meta Business SDK 26.0.2 release](https://github.com/facebook/facebook-python-business-sdk/releases/tag/26.0.2)
- [v26 generated API configuration](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.2/facebook_business/apiconfig.py)

## Static findings

All 21 audited local projections are present in the v26 generated
schemas. These cover activity, account/campaign/ad-set/ad discovery, Page and
Instagram identity discovery, audiences, creatives, ad images, default and
quality insights, the opt-in Instagram follow metric, learning-context
diagnostics, and native optimization fields at campaign, ad-set, and ad level.

One field remains explicitly SDK-unverified: `tasks` on the `/me/accounts`
edge. The generated `Page.Field` class does not include it, although Graph edge
requests accept explicit field projections without SDK type-checking. The live
core-read gate covers this edge.

The v25.0.3 → v26.0.0 generated Marketing API changes that overlap this repo's
surfaces are:

- Ad set added `anchor_event_attribution_window_days`.
- Ad added `dataset_split_specs`.
- Insights added `instagram_profile_follow`,
  `playable_average_game_length`, and `playable_game_start_rate`.
- Insights removed four `marketing_messages_website_*` metrics. For v26+, the
  MCP now rejects those metrics before synchronous or asynchronous Insights
  calls instead of forwarding a request that Meta will reject.
- v26 removes Instagram Explore Feed placement and deprecates Commerce Order
  Management. The MCP does not expose Commerce APIs. Its raw targeting surfaces
  now reject `instagram_positions=explore` for v26+ while preserving the
  separate `explore_home` placement.
- Messenger Stories is silently removed from `messenger_positions` in v26. The
  MCP rejects `messenger_positions=story` for v26+ so requested delivery is not
  changed without notice.

### SDK 26.0.2 additions

- Native recommendation `recommendation_names` and `recommendation_stages`
  filters are implemented through the existing broad and typed opportunity
  tools, preserving pagination and category heuristics. Cache keys include API
  version and both filters. Native stage codes remain opaque `MFR/PCR/PFR`.
- New Insights metrics and six breakdowns use existing generic inputs; vendor
  values are retained without invented metric semantics. The complete list is
  available through `get_v26_notes` and its packaged resource.
- Campaign reads now accept optional `fields` for SDK-advertised fields.
  Defaults and currency handling remain unchanged.
- Existing ad `fields`/create `params` carry dataset splits and audience
  persona specs. Creative `fields`/create `params` carry media optimization,
  and nested feature specs can carry video voiceover. No automatic enrollment
  or broad new mutation tools were added.
- SDK 26.0.2 removed 15 generated AdSet read-field entries, none present in
  current default projections. Several similarly named write parameters remain
  valid in generated mutation definitions; read-schema removal is not a reason
  to reject all generic mutation params.

### Live limits: schema presence is not API availability

The configured account/token accepted `dataset_split_specs` on ads and
`media_optimization_spec` on creatives. Two optional probes were unavailable:

- Campaign `bid_constraints`: `(#100) Tried accessing nonexisting field (bid_constraints)`.
- Ad `creative_audience_pairing_persona`: `(#100) Missing Permission`.

Those fields stay opt-in and out of default reads. The optional probes skip
only an explicit missing-field/permission response; other API errors fail.
The passing core/filter/native gate is separate from these limitations.
Custom-field fixture tests prove input/output preservation, not live entitlement,
valid metric/breakdown combinations, or acceptance of new writes.

The audit also found and corrected two stale local defaults that predated v26:

- Instagram account discovery now requests the generated IGUser field
  `profile_picture_url`, not `profile_pic`. Tool responses temporarily expose
  both names with the same value so existing `profile_pic` consumers continue
  to work during the migration.
- Default creative projections no longer request the non-generated
  `effective_instagram_story_id`; `effective_instagram_media_id` remains the
  supported Instagram feedback path.

## Reproduce the schema audit

Clone or fetch the official SDK tags, then run:

```bash
uv run audit-meta-sdk-schema \
  --sdk-repo /path/to/facebook-python-business-sdk \
  --base-ref 25.0.3 \
  --target-ref 26.0.2
```

The command exits nonzero if the requested `26.0.patch` release does not match
the SDK configuration, the target API is not v26.0, or a local default field
is absent. Explicit 26.0.0/26.0.1 releases remain supported. The static command
does not execute or claim a current live validation gate.

## Live gate

The live probes are read-only and force `META_API_VERSION=v26.0`. They cover:

1. Account, assigned-Page, campaign, ad-set, and ad default projections.
2. The default synchronous Insights projection.
3. The exposed campaign, ad-set, and ad native optimization tool contracts.
4. Native recommendation name/stage filters on the eligible account.
5. Four independently probed optional SDK fields (availability-dependent).

Run them with the live read token and active account configured:

```bash
META_RUN_LIVE_TESTS=1 \
META_API_VERSION=v26.0 \
uv run --extra dev pytest -q -m v26_live tests/test_live_integration.py
```

Required environment variables:

- `META_LIVE_ACCESS_TOKEN_READ`
- `META_LIVE_ACTIVE_ACCOUNT_ID`

The original 2026-08-13 gate completed with `3 passed, 0 skipped`. The current
upgrade re-ran core/Insights/native reads and verified the native recommendation
filters; optional field limitations are listed above. Keep this command as a
release-regression gate and report skips separately from passing validation.

## Native signal surface

`get_native_optimization_signals(level, object_id)` supports `campaign`,
`adset`, and `ad`. It reports which requested native fields Meta returned and
which were absent for that entity. Campaign optimization snapshots can include
the same payload with `include_native_signals=true`.

`get_entity_insights(..., include_instagram_profile_follow=true)` adds the v26
`instagram_profile_follow` metric without changing the default Insights
projection.
