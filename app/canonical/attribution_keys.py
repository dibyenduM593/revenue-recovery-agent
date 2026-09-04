"""The composite attribution key: internal id + provider id, together.

recovery_attempts.attribution_key_value has to serve two different
readers, and they need different halves of the same fact:

- attribution.py matches a recovery against provider-shaped events, so it
  needs the PROVIDER id (`order_esDHleaMWUreZr`).
- everything that navigates back to our own rows needs the INTERNAL id
  (the payments.payment_id UUID).

Storing only the internal id -- which is what this column did until now --
silently broke every PAYMENT_INTENT-attributed recovery: seed/simulate_world
read the column expecting a provider id, emitted a success event whose
order_id was a UUID no provider had ever issued, and attribution then failed
to match it. 11,609 payments were recorded NOT_RECOVERED on that basis, some
of which genuinely had been recovered. Nothing errored; the number was just
wrong, which is the worst way for a money number to be wrong.

So the key carries both halves, separated by a character that appears in
neither a UUID nor a provider id:

    00103cd1-3c65-502c-b795-844ba69a6956|order_esDHleaMWUreZr
    <-------------- internal ---------->|<---- provider ---->

split_key() tolerates a bare internal id with no separator, so rows written
before this change still parse -- they simply return provider=None and the
caller falls back to a database lookup, exactly as it did before.
"""

from __future__ import annotations

import uuid

SEPARATOR = "|"


def make_key(internal_id: uuid.UUID | str, provider_id: str | None) -> str:
    """internal_id alone when there is no provider id to pair it with."""
    internal = str(internal_id)
    if not provider_id:
        return internal
    return f"{internal}{SEPARATOR}{provider_id}"


def split_key(value: str | None) -> tuple[str | None, str | None]:
    """-> (internal_id, provider_id). Either half may be None."""
    if not value:
        return None, None
    internal, separator, provider = value.partition(SEPARATOR)
    if not separator:
        return internal or None, None
    return internal or None, provider or None


def provider_id_from(value: str | None) -> str | None:
    return split_key(value)[1]


def internal_id_from(value: str | None) -> str | None:
    return split_key(value)[0]
