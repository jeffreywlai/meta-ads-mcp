# Marketing API v25 Notes

- Historical v25 guidance: the MCP now defaults to v26.0. Select v25 explicitly
  with `META_API_VERSION=v25.0` when these notes are needed.
- Prefer unified, current campaign flows over deprecated legacy surfaces.
- Treat recommendation and opportunity surfaces as version- and entitlement-
  sensitive.
- Async insights jobs are first-class for larger reports.
- Async report polling should surface `error_code`, `error_message`,
  `error_subcode`, `error_user_title`, and `error_user_msg` when available.
- Prefer general search surfaces for targeting lookups such as ad interests and
  geo locations.
- Creative payloads should avoid deprecated Instagram inputs such as
  `instagram_actor_id`; newer payloads should use current Instagram user/media
  identifiers instead.
- Ad preview generation should use the current preview surfaces:
  `/{ad_id}/previews` for existing ads and `/{ad_account_id}/generatepreviews`
  for creative-driven previews.
