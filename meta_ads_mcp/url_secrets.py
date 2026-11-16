"""Keep usernames and passwords out of URLs that are stored or shown.

A media or link URL given to Meta is stored on the ad object and returned in
responses, dry runs and errors. A login embedded in it (https://user:pass@host/)
would be shown to everyone who can read the object, so such a URL is refused.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from meta_ads_mcp.errors import ValidationError

# Where the login part of a URL ends at the latest: white space, or the start of the path,
# query or fragment (see url_logins).
_LOGIN_STOP = re.compile(r"[\s/?#]")
_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_SCHEME_FIRST = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


@dataclasses.dataclass(frozen=True, slots=True)
class UrlLogin:
    """The login part of one scheme://user:password@ in a text: [start, end) ends after the @."""

    start: int
    end: int
    scheme: str
    login: str


def url_logins(text: str) -> Iterator[UrlLogin]:
    """Every scheme://login@ in the text, left to right, in one pass.

    The scheme is a letter followed by letters, digits, '+', '.' or '-', directly before
    '://'. The login runs to the last @ before white space or the path, query or fragment,
    which is where a URL parser ends it: a user name may itself contain @ (an email
    address), and a password may contain @ or :, so the split into user and password is
    made at the first colon afterwards. An @ with no scheme:// before it (an email
    address, a handle), or one after the host's first /, ? or # (in a path or query), is
    not a login. A scan, not a regular expression, so a long run of letters cannot make it
    slow: each character is looked at a fixed number of times.
    """
    if "@" not in text or "://" not in text:
        return
    allowed_from = 0
    search = 0
    while (separator := text.find("://", search)) >= 0:
        search = separator + 1
        start = separator
        while start > allowed_from and text[start - 1] in _SCHEME_CHARS:
            start -= 1
        while start < separator and text[start] not in _SCHEME_FIRST:
            start += 1
        if start == separator:
            continue
        login_from = separator + 3
        stop = _LOGIN_STOP.search(text, login_from)
        at = text.rfind("@", login_from, stop.start() if stop else len(text))
        if at < 0:
            continue
        yield UrlLogin(start, at + 1, text[start:login_from], text[login_from:at])
        allowed_from = search = at + 1


def refuse_url_login_in_text(text: str, name: str) -> None:
    """Raise if the text is, or holds anywhere inside it, a URL with a username or password.

    The message names the parameter and never quotes the text.
    """
    refuse_url_credentials(text, name)
    if (login := next(url_logins(text), None)) is not None:
        refuse_url_credentials(text[login.start:].split()[0], name)


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
