"""Matches a recovery signal back to the at-risk record and

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
this build, and because a wall-clock check is the single most likely
silent bug in a time-compressed run.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.canonical.attribution_keys import provider_id_from
from app.canonical.vocabulary import UNRESOLVED_STATUSES
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
        # "As of the last event we've seen" -- so replaying a historical
        # synthetic corpus doesn't instantly expire every window just
        # because the real date has moved on since the data was generated.
        clock = session.execute(
            select(func.max(RevenueEvent.occurred_at)).where(RevenueEvent.business_id == business_id)
        ).scalar_one()
        # ...but never AHEAD of real time. The generator dates some
        # INVOICE_PAID events months out (an invoice due date plus a
        # payment delay lands past the anchor window), and a single such
        # row drags this max into the future for the whole business --
        # which then reads as "now" and expires every genuinely-live
        # nudge, including a real one a human is still holding their
        # phone waiting to answer. Nothing has happened in the future,
        # so the clock is allowed to lag reality but never to lead it.
        now = datetime.now(timezone.utc)
        if clock is None or clock > now:
            clock = now

    stats = {"strong": 0, "weak": 0, "expired": 0, "still_open": 0}

    at_risk_rows = (
        session.execute(
            select(RevenueAtRisk).where(RevenueAtRisk.business_id == business_id, RevenueAtRisk.status.in_(UNRESOLVED_STATUSES))
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

        attempts = (
            session.execute(
                select(RecoveryAttempt)
                .where(RecoveryAttempt.at_risk_id == at_risk.at_risk_id)
                .order_by(RecoveryAttempt.attempt_number.desc())
            )
            .scalars()
            .all()
        )
        if not attempts:
            stats["still_open"] += 1
            continue
        attempt = attempts[0]  # latest, used for the window and for event-attributed fallback matching below

        start, end = attempt.decided_at, attempt.attribution_expires_at
        match_event: Optional[RevenueEvent] = None
        method = _METHOD_FOR_ENTITY.get(at_risk.entity_type, "PAYMENT_INTENT_MATCH")
        confidence = "WEAK"

        # Check EVERY attempt's own token, not just the latest one. A
        # fan-out (e.g. a live-demo VOICE + WHATSAPP pair) puts the nudge
        # token on only ONE of two attempts sharing an at_risk_id, and that
        # one is not always the higher attempt_number -- checking only
        # "the latest" would silently drop a real click's evidence whenever
        # a channel-less follow-up (or the other fan-out leg) attempt
        # number happens to be newer.
        for candidate in attempts:
            if not candidate.recovery_token:
                continue
            match_event = _find_by_token(session, business_id, candidate.recovery_token)
            if match_event is not None:
                method, confidence, attempt = "TOKEN_CLICK", "STRONG", candidate
                break

        if match_event is None:
            if at_risk.entity_type == "PAYMENT":
                # Provider intent id off the composite key when the attempt
                # carries one; rows written before the key became composite
                # fall back to the lookup that has always been here.
                payment_intent_id = provider_id_from(attempt.attribution_key_value)
                if payment_intent_id is None:
                    payment = session.get(Payment, at_risk.entity_id)
                    payment_intent_id = payment.payment_intent_id if payment is not None else None
                if payment_intent_id is not None:
                    match_event = _find_payment_intent_match(session, business_id, payment_intent_id, start, end)
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
    parser.add_argument(
        "--clock", type=str, default=None,
        help="ISO8601 moment to evaluate windows AS OF. Defaults to the newest event in the data, "
             "which is the right choice for a time-compressed replay but can never exceed a still-open "
             "window: simulate_world places recoveries at most 0.85 of the way through a window, so the "
             "derived clock always lands BEFORE the window closes and long-window items (90-day "
             "invoices) can never expire. A corpus run needs those negatives, so it passes the real "
             "evaluation moment here.",
    )
    args = parser.parse_args()

    clock = datetime.fromisoformat(args.clock) if args.clock else None

    business_id = business_id_for(GeneratorConfig(seed=args.seed))
    session = SessionLocal()
    try:
        stats = run_attribution(session, business_id, clock=clock)
        session.commit()
    finally:
        session.close()

    print(f"business_id: {business_id}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
