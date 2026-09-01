"""Sweep jobs for loss signals no webhook reliably sends.

checkout.abandoned is never received at all -- see schema.sql's comment
on checkout_sessions.abandoned_at ("DERIVED by the sweep job, never
received"). Invoice overdue detection is sweep-owned too, on purpose: the
sweep matches both ISSUED-and-past-due invoices (no webhook ever came)
and already-OVERDUE ones (invoice.expired already flipped the status
during normalization), so it is the single place that opens invoice risk
regardless of which path noticed the state first. Both sweeps transition
the entity's own status and open a revenue_at_risk row through the same
open_at_risk() event-driven detection uses -- its ON CONFLICT DO NOTHING
makes re-running a sweep, or overlapping with a webhook, safe.
"""

import argparse
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonical.vocabulary import FaultAttribution, LossCategory
from app.db import SessionLocal
from app.models import CheckoutSession, Invoice
from app.risk.detect import open_at_risk

ABANDONMENT_THRESHOLD = timedelta(minutes=30)


def sweep_abandoned_checkouts(session: Session, *, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - ABANDONMENT_THRESHOLD

    stale = (
        session.execute(select(CheckoutSession).where(CheckoutSession.status == "STARTED", CheckoutSession.started_at < cutoff))
        .scalars()
        .all()
    )

    opened = 0
    for checkout in stale:
        checkout.status = "ABANDONED"
        checkout.abandoned_at = now
        at_risk_id = open_at_risk(
            session,
            business_id=checkout.business_id,
            customer_id=checkout.customer_id,
            entity_type="CHECKOUT",
            entity_id=checkout.checkout_id,
            at_risk_minor=checkout.cart_value_minor,
            currency=checkout.currency,
            loss_category=LossCategory.B1.value,
            fault_attribution=FaultAttribution.CUSTOMER_SENTIMENT.value,
            claimable=True,
            failure_reason=None,
            detection_rule="checkout_abandonment_sweep",
            source_event_id=None,
            raw_event_id=checkout.raw_event_id,
        )
        if at_risk_id is not None:
            opened += 1
    return opened


def sweep_overdue_invoices(session: Session, *, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)

    overdue = (
        session.execute(select(Invoice).where(Invoice.status.in_(["ISSUED", "OVERDUE"]), Invoice.due_at < now))
        .scalars()
        .all()
    )

    opened = 0
    for invoice in overdue:
        if invoice.status != "OVERDUE":
            invoice.status = "OVERDUE"
        outstanding = invoice.amount_minor - invoice.amount_paid_minor
        at_risk_id = open_at_risk(
            session,
            business_id=invoice.business_id,
            customer_id=invoice.customer_id,
            entity_type="INVOICE",
            entity_id=invoice.invoice_id,
            at_risk_minor=outstanding,
            currency=invoice.currency,
            loss_category=LossCategory.B4.value,
            fault_attribution=FaultAttribution.CUSTOMER_SENTIMENT.value,
            claimable=True,
            failure_reason=None,
            detection_rule="invoice_overdue_sweep",
            source_event_id=None,
            raw_event_id=invoice.raw_event_id,
        )
        if at_risk_id is not None:
            opened += 1
    return opened


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the checkout-abandonment and invoice-overdue sweeps.")
    parser.parse_args()

    session = SessionLocal()
    try:
        n_checkouts = sweep_abandoned_checkouts(session)
        n_invoices = sweep_overdue_invoices(session)
        session.commit()
    finally:
        session.close()

    print(f"abandoned checkouts: {n_checkouts} newly at risk")
    print(f"overdue invoices: {n_invoices} newly at risk")


if __name__ == "__main__":
    main()
