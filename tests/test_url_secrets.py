"""A URL that is stored and shown must not carry a login."""

from __future__ import annotations

import pytest

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.url_secrets import refuse_url_credentials


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret-pass@example.invalid/a.mp4",
        "https://user@example.invalid/a.mp4",
        "https://name@mail.example:secret-pass@example.invalid/a.mp4",
        "  https://user:secret-pass@example.invalid/a.mp4  ",
    ],
)
def test_a_url_with_a_login_is_refused_without_quoting_it(url) -> None:
    with pytest.raises(ValidationError) as error:
        refuse_url_credentials(url, "video_url")
    message = str(error.value)
    assert "video_url" in message
    assert "secret-pass" not in message and "example.invalid" not in message


@pytest.mark.parametrize(
    "value",
    ["https://example.invalid/a.mp4", "https://example.invalid/a.mp4?who=a@b.c", None, 5, ""],
)
def test_a_url_without_a_login_and_non_text_values_pass(value) -> None:
    refuse_url_credentials(value, "video_url")
