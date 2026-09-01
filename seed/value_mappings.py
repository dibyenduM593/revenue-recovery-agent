"""Seed value_mappings rows that drive the semantic normalization stage.

Each row translates one provider-specific raw string into a canonical
value for one canonical_field. Sourced from the same shapes
seed/generator.py produces (Razorpay's event names and error_reason
codes, the storefront source's event names) -- there is no
feature-spec.md in this repo to seed from instead (see project notes);
these are read directly off what the providers we actually integrate
against send.
"""

import argparse

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.canonical.vocabulary import EventType, FailureReason, InvoiceStatus, PaymentStatus
from app.db import SessionLocal
from app.models import ValueMapping

# (canonical_field, source_value, canonical_value)
RAZORPAY_VALUE_MAPPINGS: list[tuple[str, str, str]] = [
    ("event_type", "payment.captured", EventType.PAYMENT_SUCCEEDED.value),
    ("event_type", "payment.failed", EventType.PAYMENT_FAILED.value),
    ("event_type", "invoice.issued", EventType.INVOICE_ISSUED.value),
    ("event_type", "invoice.paid", EventType.INVOICE_PAID.value),
    ("event_type", "invoice.expired", EventType.INVOICE_OVERDUE.value),
    ("event_type", "subscription.halted", EventType.MANDATE_REVOKED.value),
    ("event_type", "refund.processed", EventType.REFUND_CREATED.value),
    ("event_type", "payment.dispute.created", EventType.DISPUTE_CREATED.value),
    ("failure_reason", "insufficient_funds", FailureReason.INSUFFICIENT_FUNDS.value),
    ("failure_reason", "issuer_unavailable", FailureReason.ISSUER_UNAVAILABLE.value),
    ("failure_reason", "expired_card", FailureReason.EXPIRED_CARD.value),
    ("failure_reason", "payment_declined", FailureReason.DO_NOT_HONOR.value),
    ("failure_reason", "restricted_card", FailureReason.STOLEN_CARD.value),
    ("failure_reason", "incorrect_card_details", FailureReason.INVALID_DETAILS.value),
    ("failure_reason", "processing_error", FailureReason.TECHNICAL_ERROR.value),
    ("failure_reason", "incorrect_otp", FailureReason.ATTENTION_SLIP.value),
    ("failure_reason", "mandate_revoked", FailureReason.MANDATE_REVOKED.value),
    ("payment_status", "created", PaymentStatus.CREATED.value),
    ("payment_status", "authorized", PaymentStatus.AUTHORIZED.value),
    ("payment_status", "captured", PaymentStatus.CAPTURED.value),
    ("payment_status", "failed", PaymentStatus.FAILED.value),
    ("payment_status", "refunded", PaymentStatus.REFUNDED.value),
    ("invoice_status", "issued", InvoiceStatus.ISSUED.value),
    ("invoice_status", "paid", InvoiceStatus.PAID.value),
    ("invoice_status", "expired", InvoiceStatus.OVERDUE.value),
]

STOREFRONT_VALUE_MAPPINGS: list[tuple[str, str, str]] = [
    ("event_type", "checkout.started", EventType.CHECKOUT_STARTED.value),
    ("event_type", "checkout.completed", EventType.CHECKOUT_COMPLETED.value),
]

SHOPIFY_VALUE_MAPPINGS: list[tuple[str, str, str]] = [
    ("event_type", "orders/create", EventType.ORDER_CREATED.value),
]


def _rows(provider: str, mappings: list[tuple[str, str, str]]) -> list[dict]:
    return [
        dict(provider=provider, canonical_field=field, source_value=source, canonical_value=canonical, approved=True)
        for field, source, canonical in mappings
    ]


def seed_value_mappings(session: Session) -> int:
    rows = (
        _rows("razorpay", RAZORPAY_VALUE_MAPPINGS)
        + _rows("storefront", STOREFRONT_VALUE_MAPPINGS)
        + _rows("shopify", SHOPIFY_VALUE_MAPPINGS)
    )
    stmt = pg_insert(ValueMapping).values(rows).on_conflict_do_nothing(
        index_elements=["provider", "canonical_field", "source_value"]
    )
    session.execute(stmt)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the global value_mappings rows.")
    parser.parse_args()

    session = SessionLocal()
    try:
        n = seed_value_mappings(session)
        session.commit()
    finally:
        session.close()

    print(f"seeded up to {n} value mappings")


if __name__ == "__main__":
    main()
