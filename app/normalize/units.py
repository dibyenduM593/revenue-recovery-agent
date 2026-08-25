"""Stage 3: unit handling.

Assembles the typed amount_minor and currency fields into a canonical
Money. Not every event carries money (a bare mandate-revocation signal
does not), so both fields absent is valid; only one being present is not.
"""

from typing import Any

from pydantic import ValidationError

from app.canonical.money import Money
from app.canonical.vocabulary import NormalizationStage
from app.normalize.structural import NormalizationError


def to_money(typed: dict[str, Any]) -> Money | None:
    amount = typed.get("amount_minor")
    currency = typed.get("currency")

    if amount is None and currency is None:
        return None
    if amount is None or currency is None:
        raise NormalizationError(
            NormalizationStage.UNITS, "amount_minor and currency must both be present or both absent"
        )

    try:
        return Money(amount_minor=amount, currency=currency)
    except ValidationError as exc:
        raise NormalizationError(NormalizationStage.UNITS, str(exc)) from exc
