"""Named, whitelisted value transforms for field_mappings.transform.

A transform name is looked up in TRANSFORMS and called. Nothing here ever
evaluates a string as code: an unrecognized name is a structural-stage
error, not a fallback to eval.
"""

from datetime import datetime, timezone
from typing import Any, Callable


def _identity(value: Any) -> Any:
    return value


def _unix_seconds_to_datetime(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _unix_millis_to_datetime(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _iso_to_datetime(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


def _upper(value: Any) -> str:
    return str(value).upper()


def _decimal_string_to_minor(value: Any) -> int:
    """"1495.00" -> 149500. Shopify sends decimal-string major units, unlike

    Razorpay's integer minor units -- exactly the kind of provider-specific
    unit mismatch this whitelist exists to make explicit and testable.
    """
    from decimal import Decimal

    return int((Decimal(str(value)) * 100).to_integral_value())


TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "identity": _identity,
    "unix_seconds_to_datetime": _unix_seconds_to_datetime,
    "unix_millis_to_datetime": _unix_millis_to_datetime,
    "iso_to_datetime": _iso_to_datetime,
    "upper": _upper,
    "decimal_string_to_minor": _decimal_string_to_minor,
}
