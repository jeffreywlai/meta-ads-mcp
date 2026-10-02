# Optimization Playbook

- Look for spend concentration before making budget changes.
- Compare windows before acting on a single weak day.
- High frequency with weak CTR can indicate fatigue or audience saturation.
- Fatigue comparisons require at least 1,000 impressions in each window by
  default. `min_impressions` changes this policy; zero disables it. The floor
  is not a significance test, and fatigue confidence is uncalibrated (`null`).
- `no_pattern_detected` means usable data did not trigger a heuristic;
  `insufficient_data` means no usable comparison passed the selected policy.
- Strong spend with weak conversion volume is a signal to inspect creative,
  audience, and landing-page alignment.
- Prefer evidence-backed recommendations over generic best practices.
- On Graph API v26+, use `get_native_optimization_signals` for Meta's own
  delivery, learning, issue, automation, and recommendation payloads at the
  campaign, ad-set, or ad level.
- Use `include_instagram_profile_follow=true` only when Instagram follow
  outcomes are relevant; it is opt-in and requires Graph API v26+.
