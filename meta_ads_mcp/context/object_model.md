# Meta Ads Object Model

- ad account -> campaigns
- campaign -> ad sets
- ad set -> ads
- ads reference creatives and assets
- ad creatives can expose social ids such as `effective_object_story_id` for
  Facebook comments and `effective_instagram_media_id` for Instagram comments

Optimization usually starts at account or campaign level, then drills into ad
sets, ads, creatives, placements, and audience segments.

Social feedback:

- Use `get_ad_social_context` to resolve comment-capable ids behind an ad.
- Use `list_ad_comments` for compact Facebook or Instagram comments.
- Use `list_page_recommendations` for Page-level recommendations or reviews.
- Customer feedback score and negative-feedback counts are not exposed here as
  stable public Marketing API fields.
