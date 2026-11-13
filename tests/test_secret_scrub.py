"""The shared scrubber: what it removes, what it leaves, and that no input makes it slow."""

from __future__ import annotations

import json
import random
import re
import signal
import string
import time
from contextlib import contextmanager
from urllib.parse import quote, quote_plus

import pytest

from meta_ads_mcp import secret_scrub
from meta_ads_mcp.errors import MetaApiError
from meta_ads_mcp.secret_scrub import (
    ANYWHERE,
    ERROR,
    RESULT,
    rewrite_exception,
    scrub_exception,
    scrub_text,
    scrub_value,
)

# Built at run time so no secret is a literal on a line that raises.
SECRET = "".join(("R7 pass/w", "örd+\"x\\<&>~!*'()=", "\U0001F511"))
SHORT = "".join(("Cr3d", "!9"))


def _lowercase_escapes(text: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), text)


def _forms(secret: str) -> dict[str, str]:
    escaped = json.dumps(secret)[1:-1]
    return {
        "plain": secret,
        "url_encoded": quote(secret, safe=""),
        "url_encoded_lowercase": _lowercase_escapes(quote(secret, safe="")),
        "slash_preserved": quote(secret),
        "form_encoded": quote_plus(secret),
        "double_encoded": quote(quote(secret, safe=""), safe=""),
        "triple_encoded_lowercase": _lowercase_escapes(quote(quote(quote(secret, safe=""), safe=""), safe="")),
        "form_then_url_encoded": quote(quote_plus(secret), safe=""),
        "json": escaped,
        "json_unicode": json.dumps(secret, ensure_ascii=False)[1:-1],
        "json_uppercase_unicode": re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m[1].upper(), escaped),
        "json_escaped_slash": escaped.replace("/", "\\/"),
        "json_of_url_encoded": json.dumps(quote(secret))[1:-1].replace("/", "\\/"),
    }


@pytest.mark.parametrize("form", list(_forms(SECRET)))
@pytest.mark.parametrize("policy", [RESULT, ERROR, ANYWHERE])
def test_every_encoding_of_a_secret_is_removed(form, policy) -> None:
    text = _forms(SECRET)[form]
    assert scrub_text(f"before {text} after", [SECRET], policy=policy) == "before [redacted] after"


def test_letters_and_digits_stay_case_sensitive_but_escape_hex_digits_do_not() -> None:
    code = "".join(("Ab+cD", "/eF=9"))
    for repeated in (code, quote(code), _lowercase_escapes(quote(code, safe="")), f" {code} "):
        assert scrub_text(f"x {repeated} y", [code], policy=ANYWHERE, shown="<c>").split() == ["x", "<c>", "y"]
    for different in (code.swapcase(), quote(code.swapcase()), quote(code.lower(), safe="")):
        assert scrub_text(f"x {different} y", [code], policy=ANYWHERE) == f"x {different} y"


def test_a_percent_typed_in_a_secret_is_a_real_character() -> None:
    code = "".join(("code%2F", "Tail"))
    assert scrub_text(f"x {code} y", [code], policy=ANYWHERE, shown="<c>") == "x <c> y"
    # Encoded once, the typed % is %25.
    assert scrub_text(f"x {quote(code, safe='')} y", [code], policy=ANYWHERE, shown="<c>") == "x <c> y"
    # A different letter case after the typed % is a different code.
    assert scrub_text("x code%2fTail y", [code], policy=ANYWHERE) == "x code%2fTail y"


def test_a_backslash_typed_in_a_secret_is_found_written_plainly_and_json_escaped() -> None:
    secret = "".join(("ab\\n", "cd\\efgh"))
    assert scrub_text(f"x {secret} y", [secret], policy=ERROR) == "x [redacted] y"
    assert scrub_text(f"x {json.dumps(secret)[1:-1]} y", [secret], policy=ERROR) == "x [redacted] y"


@pytest.mark.parametrize(("policy", "text", "shown"), [
    # Errors: a short secret as a whole token only.
    (ERROR, "Bad password abc for the feed; abcdef is fine; (abc) too",
     "Bad password [redacted] for the feed; abcdef is fine; ([redacted]) too"),
    # Results: under four characters, not by value at all.
    (RESULT, "Bad password abc for the feed", "Bad password abc for the feed"),
    # Anywhere: inside words too.
    (ANYWHERE, "xabcx", "x[redacted]x"),
])
def test_length_policy(policy, text, shown) -> None:
    assert scrub_text(text, ["abc"], policy=policy) == shown


@pytest.mark.parametrize("policy", [RESULT, ERROR])
def test_whole_token_band_and_long_secrets(policy) -> None:
    seven = "".join(("S3", "cur3!"))
    assert scrub_text(f"Login {seven} failed ({seven}).", [seven], policy=policy) == "Login [redacted] failed ([redacted])."
    assert scrub_text(f"x{seven} and {seven}9", [seven], policy=policy) == f"x{seven} and {seven}9"
    eight = "".join(("pass", "word8"))
    assert scrub_text(f"xx{eight}yy", [eight], policy=policy) == "xx[redacted]yy"


@pytest.mark.parametrize("surroundings", ["url", "message", "json_newlines", "colon"])
def test_short_secret_boundary_is_judged_on_the_decoded_text(surroundings) -> None:
    text = {
        "url": quote("https://feed-user:" + SHORT + "@example.invalid/f.csv", safe=""),
        "message": quote("Invalid credential " + SHORT + " on import", safe=""),
        "json_newlines": json.dumps("Invalid credential\n" + SHORT + "\nretry")[1:-1],
        "colon": quote("user:" + SHORT, safe=""),
    }[surroundings]
    for policy in (RESULT, ERROR):
        cleaned = scrub_text(text, [SHORT], policy=policy)
        assert SHORT not in cleaned and quote(SHORT, safe="") not in cleaned and "[redacted]" in cleaned
    # Inside a longer word, decoded or not, a short secret is left alone.
    inside = quote("word" + SHORT + "x", safe="")
    assert scrub_text(inside, [SHORT], policy=ERROR) == inside


def test_only_the_secret_span_of_the_original_text_is_replaced() -> None:
    text = "a%20" + quote(SECRET, safe="") + "%0Ab"
    assert scrub_text(text, [SECRET], policy=ERROR) == "a%20[redacted]%0Ab"


def test_overlapping_and_adjacent_matches() -> None:
    long_secret = "".join(("abcdefgh", "ij"))
    assert scrub_text("xabcdefghijabcdefghijx", [long_secret], policy=ERROR) == "x[redacted][redacted]x"
    # Two secrets that overlap in the text become one replacement.
    assert scrub_text("x 12345678abcd y", ["12345678", "5678abcd"], policy=ERROR) == "x [redacted] y"


def test_values_keys_and_collisions() -> None:
    secret = "".join(("long-", "secret-1"))
    value = {"a " + secret: [secret, ("t", secret)], "a [redacted]": 1, "n": 5}
    cleaned = scrub_value(value, [secret], policy=RESULT)
    assert cleaned == {"a [redacted]": ["[redacted]", ("t", "[redacted]")], "a [redacted] (2)": 1, "n": 5}


def test_blank_and_non_text_secrets_are_ignored() -> None:
    assert scrub_text("a b", ["  ", None, 5], policy=ANYWHERE) == "a b"
    assert scrub_value({"k": "v"}, [], policy=ERROR) == {"k": "v"}


def test_error_chain_groups_notes_and_fields_are_scrubbed() -> None:
    secret = "".join(("chain-", "secret-77"))
    error = MetaApiError("top " + quote(secret, safe=""), code=100, user_message="u " + secret,
                         error_data={"blame": secret})
    cause = RuntimeError("cause " + secret)
    cause.add_note("note " + secret)
    error.__cause__ = cause
    error.__context__ = ExceptionGroup("group", [ValueError("member " + secret)])
    cause.__context__ = error  # a cycle ends
    scrub_exception(error, [secret], policy=ERROR, shown="<s>")
    texts = [error.message, error.user_message, repr(error.error_data), str(cause), cause.__notes__[0],
             str(error.__context__.exceptions[0])]
    assert all(secret not in text and "<s>" in text for text in texts), texts


def test_rewrite_exception_applies_any_transform() -> None:
    error = ValueError("abc")
    rewrite_exception(error, lambda value: value.upper() if isinstance(value, str) else value)
    assert error.args == ("ABC",)


def test_the_length_thresholds_are_the_agreed_ones() -> None:
    assert secret_scrub.MIN_SECRET_SUBSTRING_LENGTH == 8
    assert secret_scrub.MIN_SECRET_TOKEN_LENGTH == 4


def test_unknown_policy_is_refused() -> None:
    with pytest.raises(ValueError):
        scrub_text("x", ["x"], policy="sometimes")


# Bounded work. A matcher built from per-character alternatives took 0.33 s for 20 backslashes
# and did not finish for 24. Every case below runs in this process under a deadline that
# interrupts it, so a regression fails the test rather than hanging it.


@contextmanager
def _deadline(seconds: float):
    def expired(_signum, _frame):
        raise TimeoutError(f"scrubbing took longer than {seconds} s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    began = time.perf_counter()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert time.perf_counter() - began < seconds


def _megabyte_with_secrets(secrets: list[str]) -> str:
    rnd = random.Random(7)
    pieces: list[str] = []
    size = 0
    while size < 1_000_000:
        piece = rnd.choice([
            " ".join(rnd.choices(string.ascii_lowercase, k=8)),
            quote(rnd.choice(secrets), safe=""),
            json.dumps(rnd.choice(secrets))[1:-1].replace("/", "\\/"),
            rnd.choice(secrets),
            "%2F%3A%25%",
            "\\n\\u00e9\\",
        ])
        pieces.append(piece)
        size += len(piece)
    return "".join(pieces)


_TEN_SECRETS = ["".join(random.Random(index).choices(string.ascii_letters + string.digits + "+/=", k=4 + 4 * index))
                for index in range(10)]


@pytest.mark.parametrize(("case", "text", "secrets"), [
    ("reviewer backslashes, scaled", "\\" * 8000 + "Y", ["\\" * 4000 + "Z"]),
    ("backslashes before a letter", "\\" * 200_000 + "n", ["\\" * 99 + "nZ", "\\\\\\n"]),
    ("percent signs", "%" * 1_000_000, ["%%%%Z", "%25%25Z9", "abcdefghij"]),
    ("nested percent escapes", "%25" * 300_000 + "2", ["%%Z1", "%2525Z9xx"]),
    ("unfinished UTF-8 escapes", "%C3" * 300_000, ["%C3%C3Z", "Ã©Z"]),
    ("unicode escape starts", "\\u" * 500_000, ["\\u\\uZ"]),
    ("lone surrogate escapes", "\\ud83d" * 150_000, ["x\\ud83dZ"]),
    ("a dense short secret", "abcd" * 250_000, ["abcd", "bcda", "".join(("abcd", "abcd"))]),
    ("a megabyte with ten secrets", _megabyte_with_secrets(_TEN_SECRETS), _TEN_SECRETS),
], ids=lambda value: value if isinstance(value, str) and len(value) < 40 else "")
@pytest.mark.parametrize("policy", [RESULT, ERROR])
def test_scrubbing_finishes_in_bounded_time(case, text, secrets, policy) -> None:
    with _deadline(5.0):
        cleaned = scrub_text(text, secrets, policy=policy)
    if case == "a megabyte with ten secrets":
        for secret in _TEN_SECRETS[2:]:  # eight characters or more: removed wherever they occur
            assert secret not in cleaned


def test_a_url_login_scan_of_a_long_text_finishes_in_bounded_time() -> None:
    from meta_ads_mcp.tools.catalog_feeds import _caller_secrets, _mask_url_passwords

    text = "a" * 1_000_000 + " https://user:" + SHORT + "@example.invalid/ " + "a://" * 100_000 + "@"
    with _deadline(5.0):
        masked = _mask_url_passwords(text)
        found = _caller_secrets({"username": text})
    assert SHORT not in masked and found == [SHORT]


# Round nine: every cell of the module docstring's table of what is found.

_MATRIX_SECRETS = {
    "plain": "".join(("Plain-", "secret-77")),
    "backslash": "".join(("Back\\", "slash-77")),
    "quote": "".join(("Quote\"", "secret-77")),
    "plus": "".join(("Plus+", "secret-77")),
    "percent": "".join(("Pct%2F", "secret-77")),
    "percent+backslash": "".join(("Pct%2F\\", "slash-77")),
    "percent+quote": "".join(("Pct%2F\"", "quote-77")),
    "percent+plus": "".join(("Pct%2F+", "plus-77")),
    "backslash+plus": "".join(("Back\\+", "plus-77")),
}


def _percent_layers(text: str, layers: int) -> str:
    for _ in range(layers):
        text = quote(text, safe="")
    return text


def _json(text: str) -> str:
    return json.dumps(text)[1:-1]


def _echo(secret: str, json_place: str, layers: int) -> str:
    if json_place == "none":
        return _percent_layers(secret, layers)
    if json_place == "outside":
        return _json(_percent_layers(secret, layers))
    return _percent_layers(_json(secret), layers)


_MAX_LAYERS = secret_scrub.MAX_SINGLE_PERCENT_LAYERS


@pytest.mark.parametrize("held", list(_MATRIX_SECRETS))
@pytest.mark.parametrize("json_place", ["none", "outside", "inside"])
@pytest.mark.parametrize("layers", [0, 1, 2, _MAX_LAYERS, _MAX_LAYERS + 1])
def test_encoding_matrix(held, json_place, layers) -> None:
    secret = _MATRIX_SECRETS[held]
    echo = _echo(secret, json_place, layers)
    cleaned = scrub_text(f"x {echo} y", [secret], policy=ERROR)
    expected_found = not ("percent" in held and layers > _MAX_LAYERS)
    if expected_found:
        assert cleaned == "x [redacted] y", (echo, cleaned)
    else:
        # The documented limit: a literal %HH encoded more layers than are read one at a time.
        assert cleaned == f"x {echo} y"


@pytest.mark.parametrize("held", ["backslash", "quote"])
def test_json_escaping_applied_twice_is_a_documented_miss(held) -> None:
    secret = _MATRIX_SECRETS[held]
    echo = _json(_json(secret))
    assert scrub_text(f"x {echo} y", [secret], policy=ERROR) == f"x {echo} y"


@pytest.mark.parametrize("json_character", [chr(34), chr(92)])
@pytest.mark.parametrize("outcome", ["error", "success"])
def test_json_escaped_password_with_its_own_percent_sequence_is_private(monkeypatch, json_character, outcome) -> None:
    """R7 M1: JSON escaping only, of a password holding a literal %2F and a quote or backslash."""
    import asyncio

    from meta_ads_mcp.tools import catalog_feeds

    secret = "".join(("R7", json_character, "percent", "%2FTail"))
    encoded = json.dumps(secret)[1:-1]

    class Echo:
        async def create_edge_object(self, parent_id, edge, *, data):
            if outcome == "error":
                raise MetaApiError("Rejected credential " + encoded, code=100)
            return {"id": "20", "detail": encoded}

    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", Echo)
    call = catalog_feeds.create_product_feed_upload(
        "10", url="https://example.invalid/feed.csv", username="feed-user", password=secret,
    )
    if outcome == "error":
        with pytest.raises(MetaApiError) as raised:
            asyncio.run(call)
        shown = str(raised.value)
    else:
        shown = asyncio.run(call)["created"]["detail"]
    assert encoded not in shown and "[redacted]" in shown


_PERCENT_SECRETS = ["".join(("Pct%2F", "\\slash-77")), "".join(("50%2B", "off\"x9"))]


@pytest.mark.parametrize(("case", "text"), [
    ("escaped backslashes and percent escapes", "%5C\\\\%25" * 100_000),
    ("deeply nested percent escapes", "%" + "25" * 400_000 + "2F"),
    ("many nested escapes", "%25252F\\u0025" * 80_000),
], ids=lambda value: value if isinstance(value, str) and len(value) < 60 else "")
@pytest.mark.parametrize("policy", [RESULT, ERROR])
def test_every_reading_finishes_in_bounded_time(case, text, policy) -> None:
    # Secrets holding a percent escape and a backslash turn on every reading.
    with _deadline(5.0):
        scrub_text(text, _PERCENT_SECRETS, policy=policy)
