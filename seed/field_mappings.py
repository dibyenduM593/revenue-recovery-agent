"""Seed field_mappings rows that drive the structural normalization stage.

Each row maps one JSON path in a provider's raw payload to one canonical
field name, with a named transform (app/normalize/transforms.py) for
provider-specific value encodings, such as Razorpay's unix-second
timestamps versus the storefront source's unix-millisecond ones.

field_mappings is global per (provider, api_version, source_object), not
per-business (see schema.sql): the shape of a Razorpay payload does not
vary by which business receives it. source_object records which entity
kind a path belongs to -- payment, invoice, subscription, refund, or the
shared envelope fields ('event', 'created_at') that sit outside all of
them. Only one source_object's paths resolve for any given event, since
Razorpay nests each entity kind under a different top-level key, so
structural.apply_mapping simply skips the paths that do not exist for
that event.
"""

import argparse
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import FieldMapping

VERSION = "v1"
API_VERSION = "v1"
MAPPING_SOURCE = "official_documentation"

# (source_object, source_field, canonical_field, transform)
RAZORPAY_MAPPINGS = [
    ("envelope", "event", "event_type_raw", "identity"),
    ("envelope", "created_at", "occurred_at", "unix_seconds_to_datetime"),
    ("payment", "payload.payment.entity.id", "provider_entity_id", "identity"),
    ("payment", "payload.payment.entity.order_id", "provider_intent_id", "identity"),
    ("payment", "payload.payment.entity.amount", "amount_minor", "identity"),
    ("payment", "payload.payment.entity.currency", "currency", "identity"),
    ("payment", "payload.payment.entity.status", "status_raw", "identity"),
    ("payment", "payload.payment.entity.email", "customer_email", "identity"),
    ("payment", "payload.payment.entity.contact", "customer_phone", "identity"),
    ("payment", "payload.payment.entity.error_reason", "failure_code_raw", "identity"),
    ("payment", "payload.payment.entity.subscription_id", "provider_subscription_id", "identity"),
    ("invoice", "payload.invoice.entity.id", "provider_entity_id", "identity"),
    ("invoice", "payload.invoice.entity.amount", "amount_minor", "identity"),
    ("invoice", "payload.invoice.entity.currency", "currency", "identity"),
    ("invoice", "payload.invoice.entity.status", "status_raw", "identity"),
    ("invoice", "payload.invoice.entity.customer_details.email", "customer_email", "identity"),
    ("invoice", "payload.invoice.entity.customer_details.contact", "customer_phone", "identity"),
    ("subscription", "payload.subscription.entity.id", "provider_entity_id", "identity"),
    ("subscription", "payload.subscription.entity.status", "status_raw", "identity"),
    ("subscription", "payload.subscription.entity.customer_id", "customer_email", "identity"),
    ("refund", "payload.refund.entity.id", "provider_entity_id", "identity"),
    ("refund", "payload.refund.entity.payment_id", "provider_intent_id", "identity"),
    ("refund", "payload.refund.entity.amount", "amount_minor", "identity"),
    ("refund", "payload.refund.entity.currency", "currency", "identity"),
    ("refund", "payload.refund.entity.status", "status_raw", "identity"),
]

STOREFRONT_MAPPINGS = [
    ("checkout", "event", "event_type_raw", "identity"),
    ("checkout", "data.ts", "occurred_at", "unix_millis_to_datetime"),
    ("checkout", "data.sessionId", "provider_entity_id", "identity"),
    ("checkout", "data.cartValue", "amount_minor", "identity"),
    ("checkout", "data.curr", "currency", "identity"),
    ("checkout", "data.shopperEmail", "customer_email", "identity"),
]


def _rows(provider: str, mappings: list[tuple[str, str, str, str]]) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        dict(
            provider=provider,
            api_version=API_VERSION,
            source_object=source_object,
            source_field=source_field,
            canonical_field=canonical_field,
            transformation=transform,
            mapping_source=MAPPING_SOURCE,
            approved=True,
            version=VERSION,
            created_at=now,
        )
        for source_object, source_field, canonical_field, transform in mappings
    ]


def seed_field_mappings(session: Session) -> int:
    rows = _rows("razorpay", RAZORPAY_MAPPINGS) + _rows("storefront", STOREFRONT_MAPPINGS)
    stmt = pg_insert(FieldMapping).values(rows).on_conflict_do_nothing(
        index_elements=["provider", "api_version", "source_object", "source_field", "version"]
    )
    session.execute(stmt)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the global field_mappings rows.")
    parser.parse_args()

    session = SessionLocal()
    try:
        n = seed_field_mappings(session)
        session.commit()
    finally:
        session.close()

    print(f"seeded up to {n} field mappings")


if __name__ == "__main__":
    main()
