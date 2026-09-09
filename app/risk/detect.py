"""Gate A: writes revenue_at_risk -- the loss ledger v1 never had.

Two entry points:

- Event-driven (on_payment_failed, on_mandate_revoked): called from the
  normalize pipeline right after a payment.failed or subscription.halted
  event is upserted, since those ARE direct loss signals.
- Sweep-driven (app/risk/sweeps.py): checkout abandonment and invoice
  overdue have no webhook to react to, so they are detected by scanning
  current state instead.

Both paths funnel through open_at_risk(), which relies entirely on the
revenue_at_risk_open_uq partial unique index (business_id, entity_type,
entity_id WHERE status='OPEN') for idempotency -- ON CONFLICT DO NOTHING,
no pre-check query. That index is also what prevents a retried payment
attempt from being double-counted as a second loss: every attempt for
the same payment_intent_id resolves to the SAME entity_id (attempt #1's
payment row, via _intent_entity_id below), so a second, third, fourth
failed attempt against one order just collides with the first attempt's
still-open row and is silently absorbed rather than opening a new one.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.canonical.vocabulary import FAILURE_TAXONOMY, UNRESOLVED_STATUSES, FailureReason, NormalizationStage
from app.models import Payment, RecoveryAttempt, RevenueAtRisk
from app.normalize.structural import NormalizationError

DETECTION_VERSION = "risk@v1"


def _intent_entity_id(session: Session, business_id: uuid.UUID, payment_intent_id: str) -> uuid.UUID:
    """The payment row for attempt #1 of this intent -- the stable entity_id

    every attempt against the same order resolves to, so retries collapse
    onto one at-risk record instead of one each.
    """
    payment_id = session.execute(
        select(Payment.payment_id).where(
            Payment.business_id == business_id,
            Payment.payment_intent_id == payment_intent_id,
            Payment.attempt_number == 1,
        )
    ).scalar_one_or_none()
    if payment_id is None:
        raise NormalizationError(
            NormalizationStage.PERSIST,
            f"no attempt #1 payment found for intent {payment_intent_id!r}; will retry",
        )
    return payment_id


def open_at_risk(
    session: Session,
    *,
    business_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    entity_type: str,
    entity_id: uuid.UUID,
    at_risk_minor: int,
    currency: str,
    loss_category: str,
    fault_attribution: str,
    claimable: bool,
    failure_reason: Optional[str],
    detection_rule: str,
    source_event_id: Optional[uuid.UUID],
    raw_event_id: uuid.UUID,
) -> Optional[uuid.UUID]:
    """Returns the new at_risk_id, or None if one was already open for this entity."""
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(RevenueAtRisk)
        .values(
            at_risk_id=uuid.uuid4(),
            business_id=business_id,
            customer_id=customer_id,
            entity_type=entity_type,
            entity_id=entity_id,
            at_risk_minor=at_risk_minor,
            currency=currency,
            loss_category=loss_category,
            fault_attribution=fault_attribution,
            claimable=claimable,
            failure_reason=failure_reason,
            detected_at=now,
            detection_rule=detection_rule,
            detection_version=DETECTION_VERSION,
            status="OPEN",
            source_event_id=source_event_id,
            raw_event_id=raw_event_id,
            attributes={},
        )
        .on_conflict_do_nothing(
            index_elements=["business_id", "entity_type", "entity_id"],
            index_where=text("status = 'OPEN'"),
        )
        .returning(RevenueAtRisk.at_risk_id)
    )
    result = session.execute(stmt).first()
    return result[0] if result is not None else None


def on_payment_failed(
    session: Session,
    *,
    business_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    payment_intent_id: str,
    failure_reason: FailureReason,
    at_risk_minor: int,
    currency: str,
    source_event_id: Optional[uuid.UUID],
    raw_event_id: uuid.UUID,
) -> Optional[uuid.UUID]:
    entity_id = _intent_entity_id(session, business_id, payment_intent_id)
    entry = FAILURE_TAXONOMY[failure_reason]
    return open_at_risk(
        session,
        business_id=business_id,
        customer_id=customer_id,
        entity_type="PAYMENT",
        entity_id=entity_id,
        at_risk_minor=at_risk_minor,
        currency=currency,
        loss_category=entry.loss_category.value,
        fault_attribution=entry.fault_attribution.value,
        claimable=entry.claimable,
        failure_reason=failure_reason.value,
        detection_rule="payment_failed_taxonomy",
        source_event_id=source_event_id,
        raw_event_id=raw_event_id,
    )


def on_payment_succeeded(
    session: Session,
    *,
    business_id: uuid.UUID,
    payment_intent_id: str,
    occurred_at,
) -> bool:
    """Closes a stale OPEN/IN_RECOVERY at-risk record when the SAME intent's

    payment later succeeds through a path the recovery system never
    touched -- a later attempt in the raw event stream itself, already
    true in a historical backlog before any decision ever ran on it.
    Without this, that kind of intent stays open forever and the bounds
    layer wastes a decision on a loss that isn't one.

    Deliberately does NOT close a record that already has a
    recovery_attempts row: once bounds has decided something and the
    channel layer may have sent it, whether that success counts as a
    recovery -- and with what confidence -- is attribution's job
    (app/recovery/attribution.py), not this blunt a check. Closing it
    here first would set status to RECOVERED with no recovery_outcomes
    row at all, silently skipping attribution entirely -- exactly the bug
    this guard exists to prevent (caught during a full-pipeline
    rehearsal: recovered_at outcomes were being generated by the world
    simulator but attribution kept reporting zero STRONG or WEAK matches,
    because on_payment_succeeded had already closed the record out from
    under it moments earlier in the same worker drain).

    Returns True if a record was closed.
    """
    open_risk_id = session.execute(
        select(RevenueAtRisk.at_risk_id)
        .join(Payment, Payment.payment_id == RevenueAtRisk.entity_id)
        .where(
            RevenueAtRisk.business_id == business_id,
            RevenueAtRisk.entity_type == "PAYMENT",
            RevenueAtRisk.status.in_(UNRESOLVED_STATUSES),
            Payment.payment_intent_id == payment_intent_id,
            ~select(RecoveryAttempt.attempt_id)
            .where(RecoveryAttempt.at_risk_id == RevenueAtRisk.at_risk_id)
            .exists(),
        )
    ).scalar_one_or_none()
    if open_risk_id is None:
        return False

    session.execute(
        update(RevenueAtRisk)
        .where(RevenueAtRisk.at_risk_id == open_risk_id)
        .values(status="RECOVERED", resolved_at=occurred_at)
    )
    return True


def on_mandate_revoked(
    session: Session,
    *,
    business_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    subscription_id: uuid.UUID,
    at_risk_minor: int,
    currency: str,
    source_event_id: Optional[uuid.UUID],
    raw_event_id: uuid.UUID,
) -> Optional[uuid.UUID]:
    entry = FAILURE_TAXONOMY[FailureReason.MANDATE_REVOKED]
    return open_at_risk(
        session,
        business_id=business_id,
        customer_id=customer_id,
        entity_type="SUBSCRIPTION",
        entity_id=subscription_id,
        at_risk_minor=at_risk_minor,
        currency=currency,
        loss_category=entry.loss_category.value,
        fault_attribution=entry.fault_attribution.value,
        claimable=entry.claimable,
        failure_reason=FailureReason.MANDATE_REVOKED.value,
        detection_rule="mandate_revoked_taxonomy",
        source_event_id=source_event_id,
        raw_event_id=raw_event_id,
    )
