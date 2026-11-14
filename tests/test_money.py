"""Currency conversion and account-currency resolution tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.money import (
    from_minor_units,
    resolve_account_currency,
    to_minor_units,
)


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        (19.99, "USD", 1999),
        (1.15, "USD", 115),
        (1250, "JPY", 1250),
        (1250, "ISK", 1250),
    ],
)
def test_to_minor_units_is_exact(value, currency, expected) -> None:
    assert to_minor_units(value, currency) == expected


@pytest.mark.parametrize(
    ("value", "currency"),
    [
        (1.001, "USD"),
        (1.5, "JPY"),
        (0, "USD"),
        (-1, "USD"),
        (float("nan"), "USD"),
        (float("inf"), "USD"),
    ],
)
def test_to_minor_units_rejects_unrepresentable_values(value, currency) -> None:
    with pytest.raises(ValidationError):
        to_minor_units(value, currency, field_name="bid_amount")


def test_money_conversion_requires_currency() -> None:
    with pytest.raises(ValidationError, match="currency"):
        to_minor_units(10, None)
    with pytest.raises(ValidationError, match="currency"):
        from_minor_units("1000", None)


def test_money_conversion_rejects_unknown_currency() -> None:
    with pytest.raises(ValidationError, match="Meta-supported"):
        to_minor_units(1.23, "ZZZ")
    with pytest.raises(ValidationError, match="Meta-supported"):
        from_minor_units("123", "ZZZ")


def test_from_minor_units_respects_currency_precision() -> None:
    assert from_minor_units("1999", "USD") == 19.99
    assert from_minor_units("1250", "JPY") == 1250.0
    assert from_minor_units("1250", "ISK") == 1250.0


def test_resolve_account_currency_uses_ad_account_field() -> None:
    class FakeClient:
        async def get_object(self, object_id: str, *, fields=None, params=None):
            assert object_id == "act_123"
            assert fields == ["currency"]
            return {"currency": "jpy"}

    assert asyncio.run(resolve_account_currency(FakeClient(), "123")) == "JPY"


def test_resolve_account_currency_fails_when_meta_omits_it() -> None:
    class FakeClient:
        async def get_object(self, object_id: str, *, fields=None, params=None):
            return {"id": object_id}

    with pytest.raises(ValidationError, match="Could not resolve currency"):
        asyncio.run(resolve_account_currency(FakeClient(), "123"))


# Meta's own table, "Currencies" page of the Marketing API documentation, read 2026-11-13:
# an offset of 100 means amounts are sent in hundredths of the unit, an offset of 1 means
# they are sent in whole units.
META_CURRENCY_OFFSETS = {
    "AED": 100, "ARS": 100, "AUD": 100, "BDT": 100, "BGN": 100, "BHD": 100, "BOB": 100, "BRL": 100,
    "CAD": 100, "CHF": 100, "CLP": 1, "CNY": 100, "COP": 1, "CRC": 1, "CZK": 100, "DKK": 100,
    "DZD": 100, "EGP": 100, "EUR": 100, "FBZ": 100, "GBP": 100, "GTQ": 100, "HKD": 100, "HNL": 100,
    "HRK": 100, "HUF": 1, "IDR": 1, "ILS": 100, "INR": 100, "ISK": 1, "JOD": 100, "JPY": 1,
    "KES": 100, "KRW": 1, "LTL": 100, "LVL": 100, "MOP": 100, "MXN": 100, "MYR": 100, "NGN": 100,
    "NIO": 100, "NOK": 100, "NZD": 100, "PEN": 100, "PHP": 100, "PKR": 100, "PLN": 100, "PYG": 1,
    "QAR": 100, "RON": 100, "RSD": 100, "RUB": 100, "SAR": 100, "SEK": 100, "SGD": 100, "SKK": 100,
    "THB": 100, "TRY": 100, "TWD": 1, "UAH": 100, "USD": 100, "UYU": 100, "VEF": 100, "VES": 100,
    "VND": 1, "ZAR": 100,
}


def test_every_currency_meta_documents_converts_by_metas_offset() -> None:
    """Round twenty-five: COP, CRC, HUF, IDR and TWD were sent 100 times too large."""
    from meta_ads_mcp.money import SUPPORTED_CURRENCY_DECIMAL_PLACES

    assert len(META_CURRENCY_OFFSETS) == 66
    for currency, offset in META_CURRENCY_OFFSETS.items():
        assert to_minor_units(1250, currency) == 1250 * offset, currency
        assert from_minor_units(str(1250 * offset), currency) == 1250.0, currency
    # Nothing in the server's table disagrees with Meta's, and only LKR is beyond it.
    assert set(SUPPORTED_CURRENCY_DECIMAL_PLACES) - set(META_CURRENCY_OFFSETS) == {"LKR"}


@pytest.mark.parametrize("currency", ["COP", "CRC", "HUF", "IDR", "TWD"])
def test_whole_unit_currencies_are_sent_as_whole_units_and_take_no_fraction(currency) -> None:
    assert to_minor_units(500, currency) == 500
    assert from_minor_units("500", currency) == 500.0
    with pytest.raises(ValidationError, match="too many decimal places"):
        to_minor_units(500.5, currency)


@pytest.mark.parametrize("currency", ["BHD", "JOD", "BGN", "RSD", "RUB", "VES"])
def test_currencies_meta_documents_at_offset_100_are_accepted(currency) -> None:
    # BHD and JOD have three decimal places in ISO 4217; Meta's offset for both is 100.
    assert to_minor_units(12.34, currency) == 1234
    with pytest.raises(ValidationError, match="too many decimal places"):
        to_minor_units(12.345, currency)
