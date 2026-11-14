"""Currency-aware conversion helpers for Meta monetary fields."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from meta_ads_mcp.errors import ValidationError
from meta_ads_mcp.graph_api import normalize_account_id


# How many decimal places an amount in each currency has when it is sent to Meta, from the
# offset column of Meta's "Currencies" page (Marketing API documentation, read 2026-11-13):
# offset 100 is two places (amounts are sent in hundredths), offset 1 is none (amounts are
# sent in whole units). This follows Meta's offset, not ISO 4217: BHD and JOD have three
# places in ISO and an offset of 100 here; COP, CRC, HUF, IDR and TWD have an offset of 1.
# LKR is an ad account currency missing from that page and is kept at two places.
SUPPORTED_CURRENCY_DECIMAL_PLACES = {
    "AED": 2,
    "ARS": 2,
    "AUD": 2,
    "BDT": 2,
    "BGN": 2,
    "BHD": 2,
    "BOB": 2,
    "BRL": 2,
    "CAD": 2,
    "CHF": 2,
    "CLP": 0,
    "CNY": 2,
    "COP": 0,
    "CRC": 0,
    "CZK": 2,
    "DKK": 2,
    "DZD": 2,
    "EGP": 2,
    "EUR": 2,
    "FBZ": 2,
    "GBP": 2,
    "GTQ": 2,
    "HKD": 2,
    "HNL": 2,
    "HRK": 2,
    "HUF": 0,
    "IDR": 0,
    "ILS": 2,
    "INR": 2,
    "ISK": 0,
    "JOD": 2,
    "JPY": 0,
    "KES": 2,
    "KRW": 0,
    "LKR": 2,
    "LTL": 2,
    "LVL": 2,
    "MOP": 2,
    "MXN": 2,
    "MYR": 2,
    "NGN": 2,
    "NIO": 2,
    "NOK": 2,
    "NZD": 2,
    "PEN": 2,
    "PHP": 2,
    "PKR": 2,
    "PLN": 2,
    "PYG": 0,
    "QAR": 2,
    "RON": 2,
    "RSD": 2,
    "RUB": 2,
    "SAR": 2,
    "SEK": 2,
    "SGD": 2,
    "SKK": 2,
    "THB": 2,
    "TRY": 2,
    "TWD": 0,
    "UAH": 2,
    "USD": 2,
    "UYU": 2,
    "VEF": 2,
    "VES": 2,
    "VND": 0,
    "ZAR": 2,
}

ZERO_DECIMAL_CURRENCIES = {
    currency
    for currency, decimal_places in SUPPORTED_CURRENCY_DECIMAL_PLACES.items()
    if decimal_places == 0
}


class ObjectReader(Protocol):
    """Small Graph client surface needed to resolve account currency."""

    async def get_object(
        self,
        object_id: str,
        *,
        fields: list[str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def normalize_currency(currency: str | None) -> str:
    """Return a validated Meta-supported account currency code."""
    normalized = currency.strip().upper() if isinstance(currency, str) else ""
    if normalized not in SUPPORTED_CURRENCY_DECIMAL_PLACES:
        raise ValidationError(
            "A Meta-supported ad account currency is required for monetary conversion."
        )
    return normalized


def currency_decimal_places(currency: str | None) -> int:
    """Return the currency's number of minor-unit decimal places."""
    normalized = normalize_currency(currency)
    return SUPPORTED_CURRENCY_DECIMAL_PLACES[normalized]


def _decimal_value(value: Any, *, field_name: str) -> Decimal:
    """Convert user or Graph numeric input to a finite Decimal."""
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a finite numeric value.") from exc
    if not numeric.is_finite():
        raise ValidationError(f"{field_name} must be a finite numeric value.")
    return numeric


def to_minor_units(
    value: Any,
    currency: str | None,
    *,
    field_name: str = "amount",
    allow_zero: bool = False,
) -> int:
    """Encode a human currency value exactly, rejecting unsupported precision.

    allow_zero is for the few fields where Meta documents 0 as a value of its own,
    such as an ad set's minimum spend targets, where 0 removes the target.
    """
    decimal_places = currency_decimal_places(currency)
    numeric = _decimal_value(value, field_name=field_name)
    _check_sign(numeric, field_name=field_name, allow_zero=allow_zero)
    scaled = numeric * (Decimal(10) ** decimal_places)
    if scaled != scaled.to_integral_value():
        raise ValidationError(
            f"{field_name} has too many decimal places for "
            f"{normalize_currency(currency)} (maximum {decimal_places})."
        )
    return int(scaled)


def _check_sign(numeric: Decimal, *, field_name: str, allow_zero: bool) -> None:
    if allow_zero and numeric < 0:
        raise ValidationError(f"{field_name} must be 0 or greater.")
    if not allow_zero and numeric <= 0:
        raise ValidationError(f"{field_name} must be greater than 0.")


def validate_positive_amount(value: Any, *, field_name: str, allow_zero: bool = False) -> None:
    """Fail fast before any API lookup when a write amount is not positive (or, with allow_zero, negative)."""
    numeric = _decimal_value(value, field_name=field_name)
    _check_sign(numeric, field_name=field_name, allow_zero=allow_zero)


def from_minor_units(
    value: Any,
    currency: str | None,
    *,
    field_name: str = "amount",
) -> float | None:
    """Decode a Graph minor-unit value into a human-readable amount."""
    if value in (None, "", "null"):
        return None
    decimal_places = currency_decimal_places(currency)
    numeric = _decimal_value(value, field_name=field_name)
    return float(numeric / (Decimal(10) ** decimal_places))


async def resolve_account_currency(client: ObjectReader, account_id: str | None) -> str:
    """Fetch and validate currency from the owning Ad Account."""
    if not account_id or not str(account_id).strip():
        raise ValidationError(
            "Could not derive the owning ad account needed for monetary conversion."
        )
    # Imported here: normalize imports this module, so a module-level import would be circular.
    from meta_ads_mcp.normalize import names_other_object

    normalized_account_id = normalize_account_id(str(account_id).strip())
    account = await client.get_object(normalized_account_id, fields=["currency"])
    # An answer that names a different account is not this account's currency, and every
    # amount converted with it would be sent at the wrong scale. act_123 and 123 are one
    # account; an answer without a readable id is read as before.
    if names_other_object(normalized_account_id, account):
        raise ValidationError(
            f"Could not resolve currency for ad account {normalized_account_id}: Meta's answer was about a "
            "different ad account, so its currency was not used."
        )
    try:
        return normalize_currency(account.get("currency"))
    except ValidationError as exc:
        raise ValidationError(
            f"Could not resolve currency for ad account {normalized_account_id}."
        ) from exc
