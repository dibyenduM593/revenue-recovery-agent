"""Day 10: matches a recovery signal back to the at-risk record and

attempt that (maybe) produced it.

STRONG means high-confidence evidence: a TOKEN click (proves a specific
nudge worked) or a PAYMENT_INTENT match within its window -- for a retry
category that window is 30 minutes to 24 hours, tight enough that an
unrelated coincidental payment landing inside it is implausible. WEAK
means loose inference: ORDER/INVOICE/SUBSCRIPTION matches, whose windows
run days to 90 days, wide enough that "this payment happened to land in
the window" is real, not proof of causation. Every window check uses
each attempt's own decided_at/attribution_expires_at, never now() --
both because these events are backdated the same as everything else in
this build, and because a wall-clock check is, per data-generation.md,
"the single most likely silent bug in the whole build" in a
time-compressed run.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Payment, RecoveryAttempt, RecoveryOutcome, RevenueAtRisk, RevenueEvent


def _find_by_token(session: Session, business_id: uuid.UUID, token: str) -> Optional[RevenueEvent]:
    return (
        session.execute(
            select(RevenueEvent)
            .where(RevenueEvent.business_id == business_id, RevenueEvent.attributes["recovery_token"].astext == token)
            .order_by(RevenueEvent.occurred_at)
            .limit(1)
        )
        .scalars()
        .first()
    )


def _find_payment_intent_match(session, business_id, payment_intent_id, start, end) -> Optional[RevenueEvent]:
    return (
        session.execute(
            select(RevenueEvent)
            .join(Payment, Payment.payment_id == RevenueEvent.entity_id)
            .where(
                RevenueEvent.business_id == business_id,
                RevenueEvent.entity_type == "PAYMENT",
                RevenueEvent.event_type == "PAYMENT_SUCCEEDED",
                Payment.payment_intent_id == payment_intent_id,
                RevenueEvent.occurred_at >= start,
                RevenueEvent.occurred_at <= end,
            )
            .order_by(RevenueEvent.occurred_at)
            .limit(1)
        )
        .scalars()
        .first()
    )


def _find_checkout_match(session, business_id, checkout_id, start, end) -> Optional[RevenueEvent]:
    return (
        session.execute(
            select(RevenueEvent).where(
                RevenueEvent.business_id == business_id,
                RevenueEvent.entity_type == "CHECKOUT",
                RevenueEvent.entity_id == checkout_id,
                RevenueEvent.event_type == "CHECKOUT_COMPLETED",
                RevenueEvent.occurred_at >= start,
                RevenueEvent.occurred_at <= end,
            )
        )
        .scalars()
        .first()
    )


def _find_invoice_match(session, business_id, invoice_id, start, end) -> Optional[RevenueEvent]:
    return (
        session.execute(
            select(RevenueEvent).where(
                RevenueEvent.business_id == business_id,
                RevenueEvent.entity_type == "INVOICE",
                RevenueEvent.entity_id == invoice_id,
                RevenueEvent.event_type == "INVOICE_PAID",
                RevenueEvent.occurred_at >= start,
                RevenueEvent.occurred_at <= end,
            )
        )
        .scalars()
        .first()
    )


def _find_subscription_match(session, business_id, customer_id, start, end) -> Optional[RevenueEvent]:
    """No persisted payment<->subscription link in this build's schema

    (Payment carries no subscription_id column), so this falls back to a
    customer-level match within the window -- looser than the others by
    construction, which is exactly why it can only ever be WEAK.
    """
    if customer_id is None:
        return None
    return (
        session.execute(
            select(RevenueEvent).where(
                RevenueEvent.business_id == business_id,
                RevenueEvent.customer_id == customer_id,
                RevenueEvent.event_type == "PAYMENT_SUCCEEDED",
                RevenueEvent.occurred_at >= start,
                RevenueEvent.occurred_at <= end,
            )
        )
        .scalars()
        .first()
    )


_METHOD_FOR_ENTITY = {
    "PAYMENT": "PAYMENT_INTENT_MATCH",
    "CHECKOUT": "ORDER_MATCH",
    "INVOICE": "INVOICE_PAID",
    "SUBSCRIPTION": "SUBSCRIPTION_CHARGED",
}


def run_attribution(session: Session, business_id: uuid.UUID, *, clock: Optional[datetime] = None) -> dict:
    if clock is None:
        clock = session.execute(
            select(func.max(RevenueEvent.occurred_at)).where(RevenueEvent.business_id == business_id)
        ).scalar_one()

    stats = {"strong": 0, "weak": 0, "expired": 0, "still_open": 0}

    at_risk_rows = (
        session.execute(
            select(RevenueAtRisk).where(RevenueAtRisk.business_id == business_id, RevenueAtRisk.status.in_(["IN_RECOVERY", "OPEN"]))
        )
        .scalars()
        .all()
    )

    for at_risk in at_risk_rows:
        already = session.execute(
            select(RecoveryOutcome.outcome_id).where(RecoveryOutcome.at_risk_id == at_risk.at_risk_id)
        ).first()
        if already is not None:
            continue

        attempt = (
            session.execute(
                select(RecoveryAttempt)
                .where(RecoveryAttempt.at_risk_id == at_risk.at_risk_id)
                .order_by(RecoveryAttempt.attempt_number.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if attempt is None:
            stats["still_open"] += 1
            continue

        start, end = attempt.decided_at, attempt.attribution_expires_at
        match_event: Optional[RevenueEvent] = None
        method = _METHOD_FOR_ENTITY.get(at_risk.entity_type, "PAYMENT_INTENT_MATCH")
        confidence = "WEAK"

        if attempt.recovery_token:
            match_event = _find_by_token(session, business_id, attempt.recovery_token)
            if match_event is not None:
                method, confidence = "TOKEN_CLICK", "STRONG"

        if match_event is None:
            if at_risk.entity_type == "PAYMENT":
                payment = session.get(Payment, at_risk.entity_id)
                if payment is not None:
                    match_event = _find_payment_intent_match(session, business_id, payment.payment_intent_id, start, end)
                    if match_event is not None:
                        confidence = "STRONG"  # tight window (30min-24h): a coincidental match here is implausible
            elif at_risk.entity_type == "CHECKOUT":
                match_event = _find_checkout_match(session, business_id, at_risk.entity_id, start, end)
            elif at_risk.entity_type == "INVOICE":
                match_event = _find_invoice_match(session, business_id, at_risk.entity_id, start, end)
            elif at_risk.entity_type == "SUBSCRIPTION":
                match_event = _find_subscription_match(session, business_id, at_risk.customer_id, start, end)

        if match_event is not None:
            session.add(
                RecoveryOutcome(
                    outcome_id=uuid.uuid4(), business_id=business_id, at_risk_id=at_risk.at_risk_id,
                    attempt_id=attempt.attempt_id if attempt.executed_at else None, cohort=attempt.cohort,
                    outcome="RECOVERED", recovered_minor=at_risk.at_risk_minor, currency=at_risk.currency,
                    recovered_at=match_event.occurred_at, proof_event_id=match_event.event_id,
                    attribution_method=method, attribution_confidence=confidence,
                    latency_seconds=int((match_event.occurred_at - at_risk.detected_at).total_seconds()),
                )
            )
            at_risk.status = "RECOVERED"
            at_risk.resolved_at = match_event.occurred_at
            stats["strong" if confidence == "STRONG" else "weak"] += 1
        elif end < clock:
            session.add(
                RecoveryOutcome(
                    outcome_id=uuid.uuid4(), business_id=business_id, at_risk_id=at_risk.at_risk_id,
                    attempt_id=attempt.attempt_id if attempt.executed_at else None, cohort=attempt.cohort,
                    outcome="EXPIRED", recovered_minor=0, currency=at_risk.currency,
                    attribution_method=method, attribution_confidence="WEAK",
                )
            )
            at_risk.status = "EXPIRED"
            at_risk.resolved_at = clock
            stats["expired"] += 1
        else:
            stats["still_open"] += 1

    return stats


def main() -> None:
    import argparse

    from app.db import SessionLocal
    from seed.generator import GeneratorConfig, business_id_for

    parser = argparse.ArgumentParser(description="Match recovery signals back to at-risk records and attempts.")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    business_id = business_id_for(GeneratorConfig(seed=args.seed))
    session = SessionLocal()
    try:
        stats = run_attribution(session, business_id)
        session.commit()
    finally:
        session.close()

    print(f"business_id: {business_id}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
