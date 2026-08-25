"""Stage 2: type coercion.

Provider-agnostic: by this point every field has already been extracted
into a canonical field name by the structural stage. This stage just
enforces that each canonical field is the Python type downstream code can
rely on, regardless of which provider or transform produced it.

status_raw is deliberately not required: some entity kinds (the storefront
checkout events, in our synthetic data) only signal state through
event_type_raw and carry no separate status field at all.
"""

from datetime import datetime
from typing import Any, Callable

from app.canonical.vocabulary import NormalizationStage
from app.normalize.structural import NormalizationError

REQUIRED_FIELDS = ("event_type_raw", "occurred_at", "provider_entity_id")


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raise ValueError(f"expected a datetime, got {type(value).__name__}")


FIELD_COERCERS: dict[str, Callable[[Any], Any]] = {
    "event_type_raw": str,
    "occurred_at": _coerce_datetime,
    "amount_minor": int,
    "currency": lambda v: str(v).upper(),
    "customer_email": str,
    "customer_phone": str,
    "provider_entity_id": str,
    "provider_intent_id": str,
    "provider_subscription_id": str,
    "status_raw": str,
    "failure_code_raw": str,
}


def coerce(extracted: dict[str, Any]) -> dict[str, Any]:
    for field in REQUIRED_FIELDS:
        if field not in extracted:
            raise NormalizationError(NormalizationStage.TYPING, f"missing required field {field!r}")

    typed: dict[str, Any] = {}
    for key, value in extracted.items():
        coercer = FIELD_COERCERS.get(key)
        if coercer is None:
            continue
        try:
            typed[key] = coercer(value)
        except (TypeError, ValueError) as exc:
            raise NormalizationError(
                NormalizationStage.TYPING, f"field {key!r} failed type coercion: {exc}"
            ) from exc

    return typed
