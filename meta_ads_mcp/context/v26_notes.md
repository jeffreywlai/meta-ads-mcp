# Marketing API v26 Notes

The MCP defaults to `META_API_VERSION=v26.0`. An explicit environment override
still wins; restart the MCP after changing configuration. The schema audit uses
Meta Business SDK `26.0.2`, whose Graph API version is `v26.0`, not `v26.0.2`.

## Native recommendations

`get_recommendations` and all five typed opportunity tools accept optional
`recommendation_names` and `recommendation_stages`, as string lists or CSV.
These filters are sent to Meta before the existing category heuristics run.
Stage codes are `MFR`, `PCR`, and `PFR`; treat them as opaque Meta enum values.
Use Meta's exact recommendation names, not inferred category labels.
Omitting the filters preserves a broad opportunity scan. Filtered and
unfiltered results do not share cache entries.

## Existing native signals

Use `get_native_optimization_signals` for campaign, ad-set, and ad delivery,
learning, issue, automation, and recommendation payloads. This includes ad-set
`anchor_event_attribution_window_days`. `get_entity_insights` can request
`instagram_profile_follow` with `include_instagram_profile_follow=true`.

## New fields through existing inputs

The generic inputs already support these SDK additions without new tools:

- Insights `fields`: `playable_average_game_length`, `playable_game_start_rate`,
  `shop_clicks`, `msa_seller_budget`,
  `configurable_audience_overlap_with_conv_action`,
  `configurable_audience_overlap_with_conv_converters`,
  `configurable_audience_overlap_with_conv_exposure_cost`,
  `configurable_audience_overlap_with_conv_exposure_impressions`,
  `configurable_audience_overlap_with_conv_exposure_reach`,
  `configurable_placement_ptc_conversions`,
  `configurable_placement_ptc_converters`, `configurable_placement_ptc_reach`.
- Insights `breakdowns`: `affiliate_click_region`, `affiliate_link_url`,
  `creative_fingerprint_details`, `creative_media_type_breakdown`,
  `msa_seller_name`, `placement_path`.
- `list_campaigns` and `get_campaign` custom read `fields`: `bid_constraints`. This read-field addition
  is not evidence that campaign create/update accepts that parameter. The
  configured live v26 account rejects this SDK-listed campaign field with
  `(#100) Tried accessing nonexisting field (bid_constraints)`; it is not
  included in default requests.
- Ad custom read `fields` and create `params`: `dataset_split_specs`
  and `creative_audience_pairing_persona`. General ad updates are not exposed
  by this MCP's narrow execution tools. The configured token's live persona
  read returns `(#100) Missing Permission`; this field stays opt-in.
- Creative custom read `fields` and create `params`: `media_optimization_spec`.
  Nested `degrees_of_freedom_spec.creative_features_spec` can carry Meta's
  `video_voiceover` spec. Nothing is automatically opted into optimization.
- WhatsApp Status identity, carousel, and conversion options use the existing
  nested creative and targeting payloads, subject to Meta's account eligibility.

SDK schema presence is not proof a metric, field, breakdown combination, or
mutation is available for every account. Custom fields are preserved rather
than given invented numeric semantics; let Meta validate account-specific
combinations and use raw/export outputs when exact vendor values are needed.
Read-only probes accepted ad `dataset_split_specs` and creative
`media_optimization_spec`; no write or optimization enrollment was performed.

## Removed and historical surfaces

- Reject `instagram_positions=explore` and `messenger_positions=story` in v26.
  `explore_home` is a separate placement and remains allowed.
- The four `marketing_messages_website_*` Insights fields removed in v26 are
  rejected before synchronous, async, or export requests.
- Several SDK AdSet read fields disappeared in `26.0.2`, but this does not
  imply similarly named mutation parameters were removed. Existing default
  reads remain schema-compatible; do not blanket-reject generic write params.
- Commerce Order Management and Conversions API/browser parameter building
  are outside this optimization MCP's scope. New targeting-search/browse
  parameters are not parameters of the existing broad-category endpoint.
- `get_v25_notes` and `meta://docs/v25-notes` remain historical references.

Sources: [SDK 26.0.2](https://github.com/facebook/facebook-python-business-sdk/tree/26.0.2/facebook_business/adobjects),
[v26 release](https://github.com/facebook/facebook-python-business-sdk/releases/tag/26.0.0).
