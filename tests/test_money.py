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
