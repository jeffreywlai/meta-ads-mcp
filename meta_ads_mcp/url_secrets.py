"""Keep usernames and passwords out of URLs that are stored or shown.

A media or link URL given to Meta is stored on the ad object and returned in
responses, dry runs and errors. A login embedded in it (https://user:pass@host/)
would be shown to everyone who can read the object, so such a URL is refused.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from meta_ads_mcp.errors import ValidationError


def refuse_url_credentials(value: object, name: str) -> None:
    """Raise if the URL carries a username or password; the message never quotes the URL."""
    if not isinstance(value, str):
        return
    try:
        parts = urlsplit(value.strip())
        has_login = parts.username is not None or parts.password is not None
    except ValueError:
        has_login = "@" in value.split("/", 3)[2] if value.count("/") >= 2 else False
    if has_login:
        raise ValidationError(
            f"{name} must not contain a username or password, because the URL is stored on the "
            "ad object and shown in responses. Host the file at an address that needs no login."
        )
