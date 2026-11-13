"""Remove secrets a caller supplied from text, nested values and raised errors.

Every tool that must keep a caller's secret (a feed password, a catalog integration token, a
Partnership Ad code, a local file's path) out of what it hands back uses this one module.

How a text is searched
----------------------
Meta can repeat a secret as written or encoded. A text is searched in up to three readings.
Each reading is made in one left-to-right pass that keeps, for every character it produces,
the span of the original text the character came from, so a match in a reading replaces
exactly that span of the original text:

1. As written.
2. Fully decoded. JSON string escapes are decoded first (backslash followed by one of
   ``" \\ / b f n r t``, or ``u`` and four hex digits in either case; a high and a low
   surrogate escape in a row are one character). Then percent escapes, in either hex case,
   through every layer of encoding (``%252F`` and ``%25252F`` are ``/``). Bytes from escapes
   in a row are read as UTF-8.
3. Decoded once: JSON escapes, then a single layer of percent escapes. This reading is made
   only when a secret itself holds a percent escape (a password typed as ``50%2Boff``), which
   the full decoding would turn into something else when a text encodes it once.

In readings 2 and 3, ``+`` and a space count as the same character (form encoding writes a
space as ``+``); a secret is compared the same way. Nothing else is folded: the secret's
letters and digits must match exactly, in their own case. Only an escape's hex digits are
read in either case.

Where a run of text can be read more than one way, these fixed rules decide:

- JSON: escapes are read left to right, a backslash with the character after it, so four
  backslashes are two. A backslash that does not start a valid escape is an ordinary
  character. A lone surrogate escape is one character that no secret normally holds.
- Percent: ``%``, then any number of ``25``, then two hex digits is one escape, decoded
  through all its layers. When the two characters after the ``25``s are not hex digits, the
  last ``25`` is the escape's own digits (``%2525zz`` is ``%zz``). This is what decoding again
  and again until nothing changes gives for any text an encoder writes; the two differ only
  for a text whose hex digits were themselves percent-encoded, which no encoder writes. A
  ``%`` not followed by an escape is an ordinary character.
- UTF-8: a byte that does not begin a complete, valid UTF-8 sequence of escapes stands alone
  as a character no secret holds, and reading continues with the next escape.

A secret is found by plain substring search (``str.find``), never by a regular expression
built from it, and every match in every reading is replaced; overlapping matches become one
replacement. Each reading costs one pass over the text, and each secret one search per
reading, so the work grows with the text's length times the number of secrets, whatever the
text holds.

How much is removed
-------------------
A secret is trimmed of surrounding white space; a blank one is ignored. By its length:

- ``RESULT`` (a successful result): MIN_SECRET_SUBSTRING_LENGTH characters or more wherever
  it occurs; MIN_SECRET_TOKEN_LENGTH up to that only as a whole token; shorter not by value at
  all, since a password such as ``csv`` equals ordinary words a result may hold.
- ``ERROR`` (a raised error): MIN_SECRET_SUBSTRING_LENGTH characters or more wherever it
  occurs; any shorter one as a whole token.
- ``ANYWHERE``: every secret wherever it occurs, whatever its length (a Partnership Ad code, a
  local file's path or name).

A whole token is one whose neighbouring characters are not letters or digits; the start and
the end of the text count as such neighbours. In a decoded reading the neighbours are judged
in that reading, so ``%20`` or a JSON ``\\n`` before a secret is a separator.
"""

from __future__ import annotations

import dataclasses
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable
from typing import Any

MIN_SECRET_SUBSTRING_LENGTH = 8
MIN_SECRET_TOKEN_LENGTH = 4
REDACTED = "[redacted]"

RESULT = "result"
ERROR = "error"
ANYWHERE = "anywhere"
_POLICIES = (RESULT, ERROR, ANYWHERE)

_HEX = frozenset("0123456789abcdefABCDEF")
# Where a percent escape can start: a fixed pattern (never built from a secret), so a run of
# '%' that starts no escape is skipped in one scan.
_PERCENT_ESCAPE_START = re.compile("%[0-9A-Fa-f]{2}")
_JSON_SHORT_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class _Reading:
    """A decoded text and, for each of its characters, the span it came from in its source.

    The mapping is kept as segments: a plain run of the source copied as it is, or one
    escape decoded to one character. source is the reading this one was decoded from (None
    when it was decoded from the original text).
    """

    __slots__ = ("text", "_starts", "_from", "_to", "_plain", "_source")

    def __init__(self, source: _Reading | None) -> None:
        self._source = source
        self.text = ""
        self._starts: list[int] = []
        self._from: list[int] = []
        self._to: list[int] = []
        self._plain: list[bool] = []

    def _bounds(self, index: int) -> tuple[int, int]:
        segment = bisect_right(self._starts, index) - 1
        if self._plain[segment]:
            start = self._from[segment] + index - self._starts[segment]
            return start, start + 1
        return self._from[segment], self._to[segment]

    def span(self, start: int, end: int) -> tuple[int, int]:
        """The span of the original text that characters start to end (exclusive) came from."""
        first, last = self._bounds(start)[0], self._bounds(end - 1)[1]
        return (first, last) if self._source is None else self._source.span(first, last)


class _Builder:
    """Collects a reading's text and segments in one pass over its source text."""

    __slots__ = ("_source_text", "_parts", "_length", "reading")

    def __init__(self, source_text: str, source: _Reading | None) -> None:
        self._source_text = source_text
        self._parts: list[str] = []
        self._length = 0
        self.reading = _Reading(source)

    def _segment(self, start: int, end: int, plain: bool) -> None:
        reading = self.reading
        reading._starts.append(self._length)
        reading._from.append(start)
        reading._to.append(end)
        reading._plain.append(plain)

    def plain(self, start: int, end: int) -> None:
        if end > start:
            self._segment(start, end, True)
            self._parts.append(self._source_text[start:end])
            self._length += end - start

    def escape(self, char: str, start: int, end: int) -> None:
        self._segment(start, end, False)
        self._parts.append(char)
        self._length += 1

    def done(self) -> _Reading:
        self.reading.text = "".join(self._parts)
        return self.reading


def _hex_at(text: str, index: int, count: int) -> bool:
    return index + count <= len(text) and all(text[position] in _HEX for position in range(index, index + count))


def _json_escape_at(text: str, index: int) -> tuple[str, int] | None:
    """The character a JSON escape at text[index] (a backslash) stands for, and where it ends."""
    following = text[index + 1:index + 2]
    if following in _JSON_SHORT_ESCAPES:
        return _JSON_SHORT_ESCAPES[following], index + 2
    if following != "u" or not _hex_at(text, index + 2, 4):
        return None
    unit = int(text[index + 2:index + 6], 16)
    end = index + 6
    if 0xD800 <= unit < 0xDC00 and text.startswith("\\u", end) and _hex_at(text, end + 2, 4):
        low = int(text[end + 2:end + 6], 16)
        if 0xDC00 <= low < 0xE000:
            return chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)), end + 6
    return chr(unit), end


def _json_reading(text: str, source: _Reading | None) -> tuple[str, _Reading | None]:
    """Decode JSON string escapes; the text is returned as it is when it holds none."""
    if "\\" not in text:
        return text, source
    builder = _Builder(text, source)
    plain_from = position = 0
    while (index := text.find("\\", position)) >= 0:
        decoded = _json_escape_at(text, index)
        if decoded is None:
            position = index + 1
            continue
        char, end = decoded
        builder.plain(plain_from, index)
        builder.escape(char, index, end)
        plain_from = position = end
    builder.plain(plain_from, len(text))
    reading = builder.done()
    return reading.text, reading


def _percent_byte_at(text: str, index: int, *, layers: bool) -> tuple[int, int] | None:
    """The byte a percent escape at text[index] stands for, and where the escape ends.

    With layers, '%' then any number of '25' then two hex digits is one escape decoded
    through all its layers; when the two characters after the 25s are not hex digits, the
    last 25 is the escape's own digits.
    """
    if text[index] != "%":
        return None
    digits = index + 1
    if layers:
        while text.startswith("25", digits):
            digits += 2
        if digits > index + 1 and not _hex_at(text, digits, 2):
            return 0x25, digits
    if not _hex_at(text, digits, 2):
        return None
    return int(text[digits:digits + 2], 16), digits + 2


def _utf8_length(lead: int) -> int:
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 0


def _percent_reading(text: str, source: _Reading | None, *, layers: bool) -> tuple[str, _Reading | None]:
    """Decode percent escapes (all layers, or one), reading bytes in a row as UTF-8."""
    if "%" not in text:
        return text, source
    builder = _Builder(text, source)
    plain_from = position = 0
    while (found := _PERCENT_ESCAPE_START.search(text, position)) is not None:
        index = found.start()
        decoded = _percent_byte_at(text, index, layers=layers)
        if decoded is None:  # cannot happen after the pattern matched; kept as plain text if it did
            position = index + 1
            continue
        byte, end = decoded
        if byte < 0x80:
            char = chr(byte)
        else:
            char = chr(0xDC00 + byte)
            needed = _utf8_length(byte)
            collected = [byte]
            sequence_end = end
            while 0 < len(collected) < needed:
                following = _percent_byte_at(text, sequence_end, layers=layers) if sequence_end < len(text) else None
                if following is None or not 0x80 <= following[0] <= 0xBF:
                    break
                collected.append(following[0])
                sequence_end = following[1]
            if needed and len(collected) == needed:
                try:
                    char = bytes(collected).decode("utf-8")
                    end = sequence_end
                except UnicodeDecodeError:
                    pass
        builder.plain(plain_from, index)
        builder.escape(char, index, end)
        plain_from = position = end
    builder.plain(plain_from, len(text))
    reading = builder.done()
    return reading.text, reading


def _decoded(text: str, *, layers: bool) -> tuple[str, _Reading | None]:
    """Reading 2 (layers) or 3: JSON escapes, then percent escapes, with '+' read as a space."""
    unescaped, reading = _json_reading(text, None)
    decoded, reading = _percent_reading(unescaped, reading, layers=layers)
    return decoded.replace("+", " "), reading


def _holds_percent_escape(text: str) -> bool:
    return _PERCENT_ESCAPE_START.search(text) is not None


@dataclasses.dataclass(frozen=True, slots=True)
class _Needle:
    exact: str
    folded: str
    whole_token: bool


def _needles(secrets: Iterable[Any], policy: str) -> list[_Needle]:
    if policy not in _POLICIES:
        raise ValueError(f"policy must be one of {', '.join(_POLICIES)}")
    needles: dict[str, _Needle] = {}
    for secret in secrets:
        if not isinstance(secret, str):
            continue
        text = secret.strip()
        if not text or text in needles:
            continue
        length = len(text)
        if policy == RESULT and length < MIN_SECRET_TOKEN_LENGTH:
            continue
        whole_token = policy != ANYWHERE and length < MIN_SECRET_SUBSTRING_LENGTH
        needles[text] = _Needle(text, text.replace("+", " "), whole_token)
    return list(needles.values())


def _find_spans(
    haystack: str, needle: str, whole_token: bool, reading: _Reading | None, spans: list[tuple[int, int]],
) -> None:
    """Add the original span of every occurrence of needle in haystack (a reading of the text)."""
    size = len(needle)
    total = len(haystack)
    position = 0
    while (start := haystack.find(needle, position)) >= 0:
        end = start + size
        if whole_token and (
            (start > 0 and haystack[start - 1].isalnum()) or (end < total and haystack[end].isalnum())
        ):
            position = start + 1
            continue
        spans.append((start, end) if reading is None else reading.span(start, end))
        position = end


class SecretScrubber:
    """Removes a fixed set of secrets, under one length policy, from text, values and errors."""

    __slots__ = ("_needles", "_shown", "_single_layer")

    def __init__(self, secrets: Iterable[Any], *, policy: str, shown: str = REDACTED) -> None:
        self._needles = _needles(secrets, policy)
        self._shown = shown
        self._single_layer = any(_holds_percent_escape(needle.folded) for needle in self._needles)

    def __bool__(self) -> bool:
        return bool(self._needles)

    def text(self, text: str) -> str:
        """text with every occurrence of a secret, in any reading, replaced."""
        if not self._needles or not text:
            return text
        spans: list[tuple[int, int]] = []
        for needle in self._needles:
            _find_spans(text, needle.exact, needle.whole_token, None, spans)
        readings = [_decoded(text, layers=True)]
        if self._single_layer:
            readings.append(_decoded(text, layers=False))
        for haystack, reading in readings:
            unchanged = reading is None and haystack == text
            for needle in self._needles:
                if unchanged and needle.folded == needle.exact:
                    continue  # the same search as in the text as written
                _find_spans(haystack, needle.folded, needle.whole_token, reading, spans)
        if not spans:
            return text
        spans.sort()
        parts: list[str] = []
        kept_from = 0
        current_start, current_end = spans[0]
        for start, end in spans[1:]:
            if start < current_end:
                current_end = max(current_end, end)
                continue
            parts.extend((text[kept_from:current_start], self._shown))
            kept_from = current_end
            current_start, current_end = start, end
        parts.extend((text[kept_from:current_start], self._shown, text[current_end:]))
        return "".join(parts)

    def value(self, value: Any) -> Any:
        """A copy of value with secrets removed from every string, mapping key and value, list and tuple."""
        if not self._needles:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            shown: dict[Any, Any] = {}
            for key, item in value.items():
                shown[distinct_key(shown, self.value(key))] = self.value(item)
            return shown
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.value(item) for item in value)
        return value

    def exception(self, exc: BaseException) -> None:
        """Remove the secrets from a raised error and every error behind it, in place."""
        if self._needles:
            rewrite_exception(exc, self.value)


def scrub_text(text: str, secrets: Iterable[Any], *, policy: str, shown: str = REDACTED) -> str:
    """text with every given secret removed, by the policy's length rules."""
    return SecretScrubber(secrets, policy=policy, shown=shown).text(text)


def scrub_value(value: Any, secrets: Iterable[Any], *, policy: str, shown: str = REDACTED) -> Any:
    """A copy of a nested value (dicts with their keys, lists, tuples, strings) with every given secret removed."""
    return SecretScrubber(secrets, policy=policy, shown=shown).value(value)


def scrub_exception(exc: BaseException, secrets: Iterable[Any], *, policy: str = ERROR, shown: str = REDACTED) -> None:
    """Remove every given secret from a raised error and every error behind it, in place."""
    SecretScrubber(secrets, policy=policy, shown=shown).exception(exc)


def distinct_key(shown: dict[Any, Any], key: Any) -> Any:
    """key, or key with a number added when masking made it equal to a key already shown.

    Two keys that differed only in a secret become the same once it is removed; both
    entries are kept, the later one as "<key> (2)", "<key> (3)" and so on.
    """
    if key not in shown or not isinstance(key, str):
        return key
    number = 2
    while f"{key} ({number})" in shown:
        number += 1
    return f"{key} ({number})"


def _error_attribute_names(exc: BaseException) -> list[str]:
    """The data attributes an error carries: dataclass fields, slots and instance attributes."""
    names: list[str] = [field.name for field in dataclasses.fields(exc)] if dataclasses.is_dataclass(exc) else []
    for cls in type(exc).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        names.extend([slots] if isinstance(slots, str) else list(slots))
    names.extend(getattr(exc, "__dict__", {}))
    return [name for name in dict.fromkeys(names) if not name.startswith("__")]


def rewrite_exception(exc: BaseException, transform: Callable[[Any], Any]) -> None:
    """Apply transform to a raised error's data and to every error behind it, in place.

    A traceback log prints the whole __cause__ and __context__ chain and the MCP error shows
    the top error's public fields, so every error reachable that way, and every error inside
    an exception group, has its data attributes (dataclass fields, slots, instance
    attributes), its args (which hold the message) and its notes rewritten. Each error is
    visited once, so a cycle ends.
    """
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for name in _error_attribute_names(current):
            try:
                value = getattr(current, name)
            except AttributeError:
                continue
            if value is None or isinstance(value, BaseException):
                continue
            cleaned = transform(value)
            if cleaned != value:
                try:
                    setattr(current, name, cleaned)
                except (AttributeError, TypeError):
                    pass
        if current.args:
            current.args = tuple(transform(arg) for arg in current.args)
        notes = getattr(current, "__notes__", None)
        if isinstance(notes, list):
            current.__notes__ = [transform(note) for note in notes]
        pending.extend([current.__cause__, current.__context__])
        pending.extend(getattr(current, "exceptions", None) or ())
