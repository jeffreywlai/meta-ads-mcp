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


# Characters after which // may start a scheme-relative address inside text (see
# scheme_relative_logins): white space is tested separately.
_RELATIVE_BOUNDARY = frozenset("=&?'\"`()[]{}<>,;")


# URL parsers (Python's urlsplit, browsers) delete these before reading an address, so
# //user:pa<TAB>ss@host is read as a login; a scan that stops at them would see none.
_PARSER_REMOVED = frozenset("\t\r\n")


def _as_parsers_read(text: str) -> tuple[str, list[bool]]:
    """The text without tab, CR and LF, and for each kept character whether one was deleted just before it."""
    kept: list[str] = []
    after_removed: list[bool] = []
    removed = False
    for character in text:
        if character in _PARSER_REMOVED:
            removed = True
            continue
        kept.append(character)
        after_removed.append(removed)
        removed = False
    return "".join(kept), after_removed


def scheme_relative_logins(text: str) -> Iterator[int]:
    """The position (in the text as parsers read it) of every //user:password@ inside it, in one pass.

    Tab, CR and LF are deleted first, as URL parsers do; where one was deleted counts as a
    boundary. The // is at the start of the text or right after white space, a deleted
    character, or one of _RELATIVE_BOUNDARY; the user part runs to the last @ before a
    space or other white space, /, ? or #, and must hold a ':' (a password). Without a
    scheme an @ is more often ordinary text, so a user name alone (write to
    //contact@example.com) is not counted; the password is what must not reach Meta.
    """
    if "@" not in text or "/" not in text:
        return
    text, after_removed = _as_parsers_read(text)
    search = 0
    while (start := text.find("//", search)) >= 0:
        search = start + 2
        at_boundary = (
            start == 0 or after_removed[start] or text[start - 1].isspace() or text[start - 1] in _RELATIVE_BOUNDARY
        )
        if not at_boundary:
            continue
        stop = _LOGIN_STOP.search(text, search)
        at = text.rfind("@", search, stop.start() if stop else len(text))
        if at >= 0 and ":" in text[search:at]:
            yield start
            search = at + 1


def _parser_read_password_logins(text: str) -> bool:
    """True when the text, read without tab, CR and LF as parsers read it, holds scheme://user:password@."""
    if not _PARSER_REMOVED.intersection(text):
        return False  # the scan of the text as given already judged it
    cleaned, _ = _as_parsers_read(text)
    return any(":" in login.login for login in url_logins(cleaned))


def refuse_url_login_in_text(text: str, name: str, *, scheme_relative: bool = False) -> None:
    """Raise if the text is, or holds anywhere inside it, a URL with a username or password.

    The whole text is checked as a URL, and every scheme://login@ inside it, as the catalog
    tools always have. With scheme_relative (the creative and ad tools) two more are
    refused: a //user:password@host inside the text at a boundary, and a scheme://
    login with a password that a parser would read once tab, CR and LF are deleted
    (https://user:pa<TAB>ss@host). Without it (the catalog tools) the check is exactly as
    before. The message names the parameter and never quotes the text.
    """
    refuse_url_credentials(text, name)
    if (login := next(url_logins(text), None)) is not None:
        refuse_url_credentials(text[login.start:].split()[0], name)
    if scheme_relative and (
        next(scheme_relative_logins(text), None) is not None or _parser_read_password_logins(text)
    ):
        raise _login_refusal(name)


def _login_refusal(name: str) -> ValidationError:
    return ValidationError(
        f"{name} must not contain a username or password, because the URL is stored on the "
        "ad object and shown in responses. Host the file at an address that needs no login."
    )


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
        raise _login_refusal(name)
