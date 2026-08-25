"""Seed field_mappings rows that drive the structural normalization stage.

Each row maps one JSON path in a provider's raw payload to one canonical
field name, with a named transform (app/normalize/transforms.py) for
provider-specific value encodings, such as Razorpay's unix-second
timestamps versus the storefront source's unix-millisecond ones.

Several rows across entity kinds map to the same canonical_field (payment,
invoice, subscription, and refund all have their own path to
provider_entity_id, for instance). That is by design: only one path
resolves for any given event, since Razorpay nests each entity kind under
a different top-level key, so structural.apply_mapping simply skips the
paths that do not exist for that event.
"""

import argparse
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import FieldMapping
from seed.generator import GeneratorConfig, business_id_for

VERSION = "v1"

RAZORPAY_MAPPINGS = [
    ("event", "event_type_raw", "identity"),
    ("created_at", "occurred_at", "unix_seconds_to_datetime"),
    ("payload.payment.entity.id", "provider_entity_id", "identity"),
    ("payload.payment.entity.order_id", "provider_intent_id", "identity"),
    ("payload.payment.entity.amount", "amount_minor", "identity"),
    ("payload.payment.entity.currency", "currency", "identity"),
    ("payload.payment.entity.status", "status_raw", "identity"),
    ("payload.payment.entity.email", "customer_email", "identity"),
    ("payload.payment.entity.contact", "customer_phone", "identity"),
    ("payload.payment.entity.error_reason", "failure_code_raw", "identity"),
    ("payload.payment.entity.subscription_id", "provider_subscription_id", "identity"),
    ("payload.invoice.entity.id", "provider_entity_id", "identity"),
    ("payload.invoice.entity.amount", "amount_minor", "identity"),
    ("payload.invoice.entity.currency", "currency", "identity"),
    ("payload.invoice.entity.status", "status_raw", "identity"),
    ("payload.invoice.entity.customer_details.email", "customer_email", "identity"),
    ("payload.invoice.entity.customer_details.contact", "customer_phone", "identity"),
    ("payload.subscription.entity.id", "provider_entity_id", "identity"),
    ("payload.subscription.entity.status", "status_raw", "identity"),
    ("payload.subscription.entity.customer_id", "customer_email", "identity"),
    ("payload.refund.entity.id", "provider_entity_id", "identity"),
    ("payload.refund.entity.payment_id", "provider_intent_id", "identity"),
    ("payload.refund.entity.amount", "amount_minor", "identity"),
    ("payload.refund.entity.currency", "currency", "identity"),
    ("payload.refund.entity.status", "status_raw", "identity"),
]

STOREFRONT_MAPPINGS = [
    ("event", "event_type_raw", "identity"),
    ("data.ts", "occurred_at", "unix_millis_to_datetime"),
    ("data.sessionId", "provider_entity_id", "identity"),
    ("data.cartValue", "amount_minor", "identity"),
    ("data.curr", "currency", "identity"),
    ("data.shopperEmail", "customer_email", "identity"),
]


def _rows(business_id, source_provider: str, mappings: list[tuple[str, str, str]]) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        dict(
            business_id=business_id,
            source_provider=source_provider,
            source_field=source_field,
            canonical_field=canonical_field,
            transform=transform,
            approved=True,
            version=VERSION,
            created_at=now,
        )
        for source_field, canonical_field, transform in mappings
    ]


def seed_field_mappings(session: Session, business_id) -> int:
    rows = _rows(business_id, "razorpay", RAZORPAY_MAPPINGS) + _rows(business_id, "storefront", STOREFRONT_MAPPINGS)
    stmt = pg_insert(FieldMapping).values(rows).on_conflict_do_nothing()
    session.execute(stmt)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed field_mappings for the demo business.")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    cfg = GeneratorConfig(seed=args.seed)
    business_id = business_id_for(cfg)

    session = SessionLocal()
    try:
        n = seed_field_mappings(session, business_id)
        session.commit()
    finally:
        session.close()

    print(f"seeded up to {n} field mappings for business {business_id}")


if __name__ == "__main__":
    main()
