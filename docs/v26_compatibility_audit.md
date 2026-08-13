# Marketing API v26 compatibility audit

Audit date: 2026-08-13

## Gate status

- Static SDK schema audit: **PASS**
- Read-only v26 live smoke tests: **PASS — 3 passed, 0 skipped on 2026-08-13**
- Native optimization signals: **IMPLEMENTED — version-gated to v26+**

The repository still defaults to `v25.0`. Native optimization reads and the
opt-in Instagram profile-follow metric require `META_API_VERSION=v26.0` or
newer and fail early on v25.

## Authoritative baseline

The audit compares generated field schemas in Meta's official Python Business
SDK tags `25.0.3` and `26.0.0`. The v26 SDK configuration declares Graph API
`v26.0` and SDK `v26.0.0`.

- [Meta Business SDK v26.0.0 release](https://github.com/facebook/facebook-python-business-sdk/releases/tag/26.0.0)
- [v26 generated API configuration](https://github.com/facebook/facebook-python-business-sdk/blob/26.0.0/facebook_business/apiconfig.py)

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
  --target-ref 26.0.0
```

The command exits nonzero if the v26 SDK configuration is unexpected or a
local default field is absent from the target generated schema.

## Live gate

The live probes are read-only and force `META_API_VERSION=v26.0`. They cover:

1. Account, assigned-Page, campaign, ad-set, and ad default projections.
2. The default synchronous Insights projection.
3. The exposed campaign, ad-set, and ad native optimization tool contracts.

Run them with the live read token and active account configured:

```bash
META_RUN_LIVE_TESTS=1 \
META_API_VERSION=v26.0 \
uv run --extra dev pytest -q -m v26_live tests/test_live_integration.py
```

Required environment variables:

- `META_LIVE_ACCESS_TOKEN_READ`
- `META_LIVE_ACTIVE_ACCOUNT_ID`

The 2026-08-13 gate completed with `3 passed, 0 skipped`. Keep this command as
a release-regression gate for future Marketing API upgrades.

## Native signal surface

`get_native_optimization_signals(level, object_id)` supports `campaign`,
`adset`, and `ad`. It reports which requested native fields Meta returned and
which were absent for that entity. Campaign optimization snapshots can include
the same payload with `include_native_signals=true`.

`get_entity_insights(..., include_instagram_profile_follow=true)` adds the v26
`instagram_profile_follow` metric without changing the default Insights
projection.
