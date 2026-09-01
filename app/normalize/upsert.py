"""Stage 6: upsert into the canonical entity tables + the unified log.

Identity resolution goes through source_mappings (schema.sql section 1):
a provider's own object id is looked up there to find (or mint) our
internal UUID, via INSERT ... ON CONFLICT DO NOTHING so concurrent
workers processing different raw_events for the same underlying object
race safely instead of creating two rows for it. Every attempt against a
payment intent gets its OWN provider payment id in this data source, so
payments practically always take the insert path; checkout/invoice take
the update path on their second event (started -> completed,
issued -> paid). Every successful upsert also writes one revenue_events
row -- the unified log Day 5's risk detection and Day 10's attribution
both read from, never derived by re-scanning entity tables.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.canonical.events import CanonicalEvent
from app.canonical.vocabulary import EventType, MandateStatus, NormalizationStage, SubscriptionStatus
from app.models import CheckoutSession, Customer, Dispute, Invoice, Order, Payment, RevenueEvent, SourceMapping, Subscription
from app.normalize.semantic import SemanticResult
from app.normalize.structural import NormalizationError

# uuid4() (crypto-random) for every internal id was the actual root cause
# behind Day 12's determinism check failing: two runs from an identical
# seed produced identical raw_events and revenue_at_risk totals, but
# app/recovery/batch.py's `ORDER BY detected_at LIMIT 500` ties whenever
# many records share one sweep's single now() call, and with random
# internal ids there was no stable way to break that tie the same way
# twice. uuid5, keyed on the provider's own (deterministically generated)
# id, makes every internal id itself reproducible run over run -- the
# same fix business_id_for() already uses for the business itself,
# applied consistently rather than as a one-off.
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "revenue-recovery.internal.entities")


def _deterministic_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, ":".join(parts))


def _get_or_create_customer(session: Session, business_id: uuid.UUID, email: str | None, phone: str | None) -> uuid.UUID | None:
    if not email and not phone:
        return None
    email_normalized = email.strip().lower() if email else None
    now = datetime.now(timezone.utc)
    candidate_id = _deterministic_id(str(business_id), "customer", email_normalized or phone or "")
    insert_stmt = pg_insert(Customer).values(
        customer_id=candidate_id,
        business_id=business_id,
        email=email,
        email_normalized=email_normalized,
        phone_e164=phone,
        created_at=now,
        updated_at=now,
    )
    stmt = insert_stmt.on_conflict_do_nothing(index_elements=["business_id", "email_normalized"]).returning(
        Customer.customer_id
    )
    inserted = session.execute(stmt).first()
    if inserted is not None:
        return inserted[0]
    # NULL email_normalized never conflicts, so reaching here means email_normalized is set
    # and really did collide with an existing customer.
    return session.execute(
        select(Customer.customer_id).where(
            Customer.business_id == business_id, Customer.email_normalized == email_normalized
        )
    ).scalar_one()


def _resolve_internal_id(
    session: Session, business_id: uuid.UUID, provider: str, entity_kind: str, provider_entity_id: str
) -> tuple[uuid.UUID, bool]:
    """Returns (internal_id, is_new)."""
    candidate_id = _deterministic_id(str(business_id), provider, entity_kind, provider_entity_id)
    insert_stmt = pg_insert(SourceMapping).values(
        business_id=business_id,
        internal_id=candidate_id,
        internal_type=entity_kind,
        source_provider=provider,
        source_object_type=entity_kind,
        source_object_id=provider_entity_id,
        created_at=datetime.now(timezone.utc),
    )
    stmt = insert_stmt.on_conflict_do_nothing(
        index_elements=["business_id", "source_provider", "source_object_type", "source_object_id"]
    ).returning(SourceMapping.internal_id)
    inserted = session.execute(stmt).first()
    if inserted is not None:
        return inserted[0], True
    existing = session.execute(
        select(SourceMapping.internal_id).where(
            SourceMapping.business_id == business_id,
            SourceMapping.source_provider == provider,
            SourceMapping.source_object_type == entity_kind,
            SourceMapping.source_object_id == provider_entity_id,
        )
    ).scalar_one()
    return existing, False


def _upsert_payment(
    session: Session,
    business_id: uuid.UUID,
    provider: str,
    customer_id: uuid.UUID | None,
    event: CanonicalEvent,
    semantic: SemanticResult,
    raw_event_id: uuid.UUID,
    mapping_version: str,
) -> uuid.UUID:
    if event.money is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "payment event has no amount/currency")
    if semantic.status is None:
        raise NormalizationError(NormalizationStage.SEMANTIC, "payment event has no resolved status")

    payment_id, is_new = _resolve_internal_id(session, business_id, provider, "payment", event.provider_entity_id)
    if not is_new:
        # Every attempt gets its own provider payment id in this data source; this path only
        # matters if a provider ever redelivers the same payment id (a correction), in which
        # case update the existing row rather than erroring.
        payment = session.get(Payment, payment_id)
        payment.payment_status = semantic.status
        payment.raw_event_id = raw_event_id
        payment.mapping_version = mapping_version
        return payment_id

    intent_id = event.provider_intent_id or event.provider_entity_id
    # Orders arrive from a different provider (shopify) than the payment referencing them
    # (razorpay) -- a real cross-system join, not a bug. Best-effort: a payment with no
    # matching order (Razorpay-only merchant, or the order simply wasn't generated) still
    # upserts fine with order_id left NULL.
    order_id = None
    if event.provider_intent_id:
        order_id = session.execute(
            select(SourceMapping.internal_id).where(
                SourceMapping.business_id == business_id,
                SourceMapping.source_provider == "shopify",
                SourceMapping.source_object_type == "order",
                SourceMapping.source_object_id == event.provider_intent_id,
            )
        ).scalar_one_or_none()

    attempt_number = (
        session.execute(
            select(func.count())
            .select_from(Payment)
            .where(Payment.business_id == business_id, Payment.payment_intent_id == intent_id)
        ).scalar_one()
        + 1
    )
    parent_payment_id = session.execute(
        select(Payment.payment_id)
        .where(Payment.business_id == business_id, Payment.payment_intent_id == intent_id)
        .order_by(Payment.initiated_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    is_terminal = semantic.status in ("CAPTURED", "FAILED", "REFUNDED")
    session.add(
        Payment(
            payment_id=payment_id,
            business_id=business_id,
            customer_id=customer_id,
            order_id=order_id,
            payment_intent_id=intent_id,
            attempt_number=attempt_number,
            parent_payment_id=parent_payment_id,
            amount_minor=event.money.amount_minor,
            currency=event.money.currency,
            payment_status=semantic.status,
            failure_code_raw=event.failure_code_raw,
            failure_code_canonical=event.failure_reason.value if event.failure_reason else None,
            error_source=event.error_source,
            error_step=event.error_step,
            error_description=event.error_description,
            issuer_bank=event.issuer_bank,
            card_network=event.card_network,
            card_last4=event.card_last4,
            card_expiry_month=event.card_expiry_month,
            card_expiry_year=event.card_expiry_year,
            vpa=event.vpa,
            wallet=event.wallet,
            initiated_at=event.occurred_at,
            completed_at=event.occurred_at if is_terminal else None,
            raw_event_id=raw_event_id,
            mapping_version=mapping_version,
        )
    )
    return payment_id


def _upsert_checkout(
    session: Session,
    business_id: uuid.UUID,
    provider: str,
    customer_id: uuid.UUID | None,
    event: CanonicalEvent,
    semantic: SemanticResult,
    raw_event_id: uuid.UUID,
    mapping_version: str,
) -> uuid.UUID:
    if event.money is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "checkout event has no cart value")

    checkout_id, is_new = _resolve_internal_id(session, business_id, provider, "checkout", event.provider_entity_id)
    status = "COMPLETED" if semantic.event_type == EventType.CHECKOUT_COMPLETED else "STARTED"

    if is_new:
        session.add(
            CheckoutSession(
                checkout_id=checkout_id,
                business_id=business_id,
                customer_id=customer_id,
                cart_value_minor=event.money.amount_minor,
                currency=event.money.currency,
                status=status,
                started_at=event.occurred_at,
                completed_at=event.occurred_at if status == "COMPLETED" else None,
                raw_event_id=raw_event_id,
                mapping_version=mapping_version,
            )
        )
    else:
        row = session.get(CheckoutSession, checkout_id)
        row.status = status
        if status == "COMPLETED":
            row.completed_at = event.occurred_at
        row.raw_event_id = raw_event_id
        row.mapping_version = mapping_version
    return checkout_id


def _upsert_invoice(
    session: Session,
    business_id: uuid.UUID,
    provider: str,
    customer_id: uuid.UUID | None,
    event: CanonicalEvent,
    semantic: SemanticResult,
    raw_event_id: uuid.UUID,
    mapping_version: str,
) -> uuid.UUID:
    if event.money is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "invoice event has no amount")
    if semantic.status is None:
        raise NormalizationError(NormalizationStage.SEMANTIC, "invoice event has no resolved status")
    if event.due_at is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "invoice event has no due_at")

    invoice_id, is_new = _resolve_internal_id(session, business_id, provider, "invoice", event.provider_entity_id)
    if is_new:
        session.add(
            Invoice(
                invoice_id=invoice_id,
                business_id=business_id,
                customer_id=customer_id,
                amount_minor=event.money.amount_minor,
                amount_paid_minor=event.money.amount_minor if semantic.status == "PAID" else 0,
                currency=event.money.currency,
                issued_at=event.occurred_at,
                due_at=event.due_at,
                paid_at=event.occurred_at if semantic.status == "PAID" else None,
                status=semantic.status,
                raw_event_id=raw_event_id,
                mapping_version=mapping_version,
            )
        )
    else:
        row = session.get(Invoice, invoice_id)
        row.status = semantic.status
        if semantic.status == "PAID":
            row.paid_at = event.occurred_at
            row.amount_paid_minor = event.money.amount_minor
        row.raw_event_id = raw_event_id
        row.mapping_version = mapping_version
    return invoice_id


def _upsert_subscription(
    session: Session,
    business_id: uuid.UUID,
    provider: str,
    customer_id: uuid.UUID | None,
    event: CanonicalEvent,
    raw_event_id: uuid.UUID,
    mapping_version: str,
) -> uuid.UUID:
    """Only reached for subscription.halted in this data source -- there is

    no dedicated subscription-creation event, so the first mandate-revoked
    signal is also the first time we learn the subscription exists.
    """
    if event.money is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "subscription event has no billing amount")

    subscription_id, is_new = _resolve_internal_id(
        session, business_id, provider, "subscription", event.provider_entity_id
    )
    if is_new:
        session.add(
            Subscription(
                subscription_id=subscription_id,
                business_id=business_id,
                customer_id=customer_id,
                billing_amount_minor=event.money.amount_minor,
                currency=event.money.currency,
                status=SubscriptionStatus.PAST_DUE.value,
                mandate_status=MandateStatus.REVOKED.value,
                raw_event_id=raw_event_id,
                mapping_version=mapping_version,
            )
        )
    else:
        row = session.get(Subscription, subscription_id)
        row.mandate_status = MandateStatus.REVOKED.value
        row.status = SubscriptionStatus.PAST_DUE.value
        row.raw_event_id = raw_event_id
        row.mapping_version = mapping_version
    return subscription_id


def _upsert_order(
    session: Session,
    business_id: uuid.UUID,
    provider: str,
    customer_id: uuid.UUID | None,
    event: CanonicalEvent,
    semantic: SemanticResult,
    raw_event_id: uuid.UUID,
    mapping_version: str,
) -> uuid.UUID:
    if event.money is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "order event has no amount")

    order_id, is_new = _resolve_internal_id(session, business_id, provider, "order", event.provider_entity_id)
    status = (event.status_raw or "PENDING").upper()

    if is_new:
        session.add(
            Order(
                order_id=order_id,
                business_id=business_id,
                customer_id=customer_id,
                order_status=status,
                total_amount_minor=event.money.amount_minor,
                currency=event.money.currency,
                created_at=event.occurred_at,
                attributes={},
                raw_event_id=raw_event_id,
                mapping_version=mapping_version,
            )
        )
    else:
        row = session.get(Order, order_id)
        row.order_status = status
        row.raw_event_id = raw_event_id
        row.mapping_version = mapping_version
    return order_id


def _upsert_dispute(
    session: Session,
    business_id: uuid.UUID,
    provider: str,
    customer_id: uuid.UUID | None,
    event: CanonicalEvent,
    semantic: SemanticResult,
    raw_event_id: uuid.UUID,
    mapping_version: str,
) -> uuid.UUID:
    """A dispute references an already-processed payment (like a refund does):

    an open dispute is a hard stop on contacting that customer about that
    payment (Day 8), not a new loss category of its own.
    """
    if event.money is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "dispute event has no amount")
    if not event.provider_intent_id:
        raise NormalizationError(NormalizationStage.SEMANTIC, "dispute event has no payment_id reference")

    payment_id = session.execute(
        select(SourceMapping.internal_id).where(
            SourceMapping.business_id == business_id,
            SourceMapping.source_provider == provider,
            SourceMapping.source_object_type == "payment",
            SourceMapping.source_object_id == event.provider_intent_id,
        )
    ).scalar_one_or_none()
    if payment_id is None:
        raise NormalizationError(
            NormalizationStage.PERSIST,
            f"dispute references unprocessed payment {event.provider_intent_id!r}; will retry",
        )

    dispute_id, is_new = _resolve_internal_id(session, business_id, provider, "dispute", event.provider_entity_id)
    status = (event.status_raw or "OPEN").upper()
    if is_new:
        session.add(
            Dispute(
                dispute_id=dispute_id,
                business_id=business_id,
                payment_id=payment_id,
                amount_minor=event.money.amount_minor,
                currency=event.money.currency,
                status=status,
                respond_by=event.due_at,
                raw_event_id=raw_event_id,
            )
        )
    else:
        row = session.get(Dispute, dispute_id)
        row.status = status
    return dispute_id


def _apply_refund(
    session: Session, business_id: uuid.UUID, provider: str, event: CanonicalEvent
) -> uuid.UUID:
    if event.money is None:
        raise NormalizationError(NormalizationStage.VALIDATION, "refund event has no amount")
    if not event.provider_intent_id:
        raise NormalizationError(NormalizationStage.SEMANTIC, "refund event has no payment_id reference")

    payment_id = session.execute(
        select(SourceMapping.internal_id).where(
            SourceMapping.business_id == business_id,
            SourceMapping.source_provider == provider,
            SourceMapping.source_object_type == "payment",
            SourceMapping.source_object_id == event.provider_intent_id,
        )
    ).scalar_one_or_none()
    if payment_id is None:
        raise NormalizationError(
            NormalizationStage.PERSIST,
            f"refund references unprocessed payment {event.provider_intent_id!r}; will retry",
        )

    payment = session.get(Payment, payment_id)
    payment.amount_refunded_minor += event.money.amount_minor
    return payment_id


def _write_revenue_event(
    session: Session,
    business_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    event: CanonicalEvent,
    semantic: SemanticResult,
    provider: str,
    raw_event_id: uuid.UUID,
    mapping_version: str,
) -> Optional[uuid.UUID]:
    event_id = uuid.uuid4()
    stmt = (
        pg_insert(RevenueEvent)
        .values(
            event_id=event_id,
            business_id=business_id,
            customer_id=customer_id,
            event_type=semantic.event_type.value,
            entity_type=entity_type.upper(),
            entity_id=entity_id,
            occurred_at=event.occurred_at,
            amount_minor=event.money.amount_minor if event.money else None,
            currency=event.money.currency if event.money else None,
            source_provider=provider,
            attributes={"recovery_token": event.recovery_token_raw} if event.recovery_token_raw else {},
            raw_event_id=raw_event_id,
            mapping_version=mapping_version,
            created_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["raw_event_id", "entity_type", "entity_id"])
        .returning(RevenueEvent.event_id)
    )
    result = session.execute(stmt).first()
    return result[0] if result is not None else None


_ENTITY_UPSERTS = {
    "payment": _upsert_payment,
    "checkout": _upsert_checkout,
    "invoice": _upsert_invoice,
    "order": _upsert_order,
    "dispute": _upsert_dispute,
}


@dataclass
class UpsertResult:
    entity_kind: str
    entity_id: uuid.UUID
    customer_id: Optional[uuid.UUID]
    payment_intent_id: Optional[str]
    revenue_event_id: Optional[uuid.UUID]


def apply(
    session: Session,
    raw_event_id: uuid.UUID,
    business_id: uuid.UUID,
    provider: str,
    semantic: SemanticResult,
    event: CanonicalEvent,
    mapping_version: str,
) -> UpsertResult:
    customer_id = _get_or_create_customer(session, business_id, event.customer_email, event.customer_phone)

    if semantic.entity_kind == "subscription":
        entity_id = _upsert_subscription(session, business_id, provider, customer_id, event, raw_event_id, mapping_version)
        revenue_entity_type = "subscription"
    elif semantic.entity_kind == "refund":
        entity_id = _apply_refund(session, business_id, provider, event)
        revenue_entity_type = "payment"  # a refund is logged against the payment it refunds
    elif semantic.entity_kind in _ENTITY_UPSERTS:
        entity_id = _ENTITY_UPSERTS[semantic.entity_kind](
            session, business_id, provider, customer_id, event, semantic, raw_event_id, mapping_version
        )
        revenue_entity_type = semantic.entity_kind
    else:
        raise NormalizationError(NormalizationStage.SEMANTIC, f"no upsert handler for entity kind {semantic.entity_kind!r}")

    revenue_event_id = _write_revenue_event(
        session, business_id, customer_id, revenue_entity_type, entity_id, event, semantic, provider, raw_event_id, mapping_version
    )
    payment_intent_id = (event.provider_intent_id or event.provider_entity_id) if semantic.entity_kind == "payment" else None
    return UpsertResult(
        entity_kind=semantic.entity_kind,
        entity_id=entity_id,
        customer_id=customer_id,
        payment_intent_id=payment_intent_id,
        revenue_event_id=revenue_event_id,
    )
