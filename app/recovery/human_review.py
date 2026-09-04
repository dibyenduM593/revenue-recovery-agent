"""One definition of "awaiting human," reused by the KPI tile, the report's

cohort exclusion, and the Human Review tab's queue query -- so they can
never disagree about which rows count. Two independent mechanisms land a
row here: policy.decide() choosing Action.ESCALATE_HUMAN (never executed,
per app/bounds.py's h-check), and bounds.py's own value-threshold
held_for_approval suppression. Both are "temporary, not a permanent fact"
-- the at_risk row stays OPEN either way -- so both belong in the same
queue rather than being scattered across the generic stopped_held bucket.
"""

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.bounds import HELD_FOR_APPROVAL_SUPPRESSED_REASON
from app.canonical.vocabulary import Action
from app.models import Customer, CustomerRecoveryProfile, RecoveryAttempt, RevenueAtRisk


def is_awaiting_human(attempt: RecoveryAttempt) -> bool:
    return (
        (attempt.strategy == Action.ESCALATE_HUMAN.value and attempt.executed_at is None)
        or attempt.suppressed_reason == HELD_FOR_APPROVAL_SUPPRESSED_REASON
    )


def latest_attempt_by_at_risk(session: Session, business_id: uuid.UUID) -> dict[uuid.UUID, RecoveryAttempt]:
    """One query, not the repeated N+1

    .order_by(attempt_number.desc()).limit(1) pattern this replaces --
    Postgres's DISTINCT ON picks each at_risk_id's latest attempt in a
    single index scan instead of one round trip per row.
    """
    rows = session.execute(
        select(RecoveryAttempt)
        .distinct(RecoveryAttempt.at_risk_id)
        .where(RecoveryAttempt.business_id == business_id)
        .order_by(RecoveryAttempt.at_risk_id, RecoveryAttempt.attempt_number.desc())
    ).scalars().all()
    return {attempt.at_risk_id: attempt for attempt in rows}


def awaiting_human_at_risk_ids(session: Session, business_id: uuid.UUID) -> set[uuid.UUID]:
    latest = latest_attempt_by_at_risk(session, business_id)
    return {at_risk_id for at_risk_id, attempt in latest.items() if is_awaiting_human(attempt)}


def _latest_attempt_subquery(business_id: uuid.UUID):
    return (
        select(
            RecoveryAttempt.at_risk_id.label("at_risk_id"),
            RecoveryAttempt.strategy.label("strategy"),
            RecoveryAttempt.executed_at.label("executed_at"),
            RecoveryAttempt.suppressed_reason.label("suppressed_reason"),
            RecoveryAttempt.bounds_snapshot.label("bounds_snapshot"),
            RecoveryAttempt.decided_at.label("decided_at"),
        )
        .distinct(RecoveryAttempt.at_risk_id)
        .where(RecoveryAttempt.business_id == business_id)
        .order_by(RecoveryAttempt.at_risk_id, RecoveryAttempt.attempt_number.desc())
        .subquery()
    )


def _awaiting_filter(latest_attempt_subq):
    return or_(
        and_(
            latest_attempt_subq.c.strategy == Action.ESCALATE_HUMAN.value,
            latest_attempt_subq.c.executed_at.is_(None),
        ),
        latest_attempt_subq.c.suppressed_reason == HELD_FOR_APPROVAL_SUPPRESSED_REASON,
    )


def in_human_hands_minor(session: Session, business_id: uuid.UUID) -> int:
    """Direct SQL sum for the KPI tile, not a Python-side loop over loaded rows."""
    latest_attempt_subq = _latest_attempt_subquery(business_id)
    total = session.execute(
        select(func.coalesce(func.sum(RevenueAtRisk.at_risk_minor), 0))
        .select_from(RevenueAtRisk)
        .join(latest_attempt_subq, latest_attempt_subq.c.at_risk_id == RevenueAtRisk.at_risk_id)
        .where(RevenueAtRisk.business_id == business_id, _awaiting_filter(latest_attempt_subq))
    ).scalar_one()
    return int(total)


def human_review_queue(session: Session, business_id: uuid.UUID, limit: int = 500) -> list[dict]:
    """Row data for the Human Review tab, ranked by recovery likelihood alone

    -- independent of which action nearly fired, so a high-score ticket
    surfaces first whether it got there via ESCALATE_HUMAN or the value
    threshold.
    """
    latest_attempt_subq = _latest_attempt_subquery(business_id)

    rows = session.execute(
        select(
            RevenueAtRisk.at_risk_id,
            RevenueAtRisk.customer_id,
            RevenueAtRisk.entity_type,
            RevenueAtRisk.at_risk_minor,
            RevenueAtRisk.currency,
            Customer.customer_type,
            CustomerRecoveryProfile.predicted_score,
            latest_attempt_subq.c.strategy,
            latest_attempt_subq.c.suppressed_reason,
            latest_attempt_subq.c.bounds_snapshot,
            latest_attempt_subq.c.decided_at,
        )
        .select_from(RevenueAtRisk)
        .join(latest_attempt_subq, latest_attempt_subq.c.at_risk_id == RevenueAtRisk.at_risk_id)
        .outerjoin(Customer, Customer.customer_id == RevenueAtRisk.customer_id)
        .outerjoin(
            CustomerRecoveryProfile,
            and_(
                CustomerRecoveryProfile.business_id == RevenueAtRisk.business_id,
                CustomerRecoveryProfile.at_risk_id == RevenueAtRisk.at_risk_id,
            ),
        )
        .where(RevenueAtRisk.business_id == business_id, _awaiting_filter(latest_attempt_subq))
        .order_by(func.coalesce(CustomerRecoveryProfile.predicted_score, 0).desc())
        .limit(limit)
    ).all()

    items = []
    for row in rows:
        if row.strategy == Action.ESCALATE_HUMAN.value:
            reason = (row.bounds_snapshot or {}).get("escalation_reason") or "escalate_human"
        else:
            reason = "value_threshold"
        items.append(
            {
                "at_risk_id": row.at_risk_id,
                "customer_id": row.customer_id,
                "customer_type": row.customer_type,
                "entity_type": row.entity_type,
                "value_minor": row.at_risk_minor,
                "currency": row.currency,
                "predicted_score": float(row.predicted_score) if row.predicted_score is not None else 0.5,
                "reason": reason,
                "decided_at": row.decided_at,
            }
        )
    return items
