"""Nudge retry cadence: up to NUDGE_RETRY_MAX_TRIES nudges per at-risk item,

one after another as each one's own attribution window closes unconverted
(each window is already ~24-72h per FAILURE_TAXONOMY, which is what makes
"about a day apart" happen without any extra artificial delay coded in).
If the last try's window closes with still no match, the loss is finalized
NOT_RECOVERED -- a real, previously-unused outcome value the schema
already declares (recovery_outcomes.outcome CHECK constraint) but nothing
wrote before this module existed. If ANY retry converts, run_attribution()
already writes RECOVERED the normal way; this module only ever runs for
items attribution has NOT already resolved.

Only a "nudge" (a channel-bearing attempt whose recovery_attempts row
carries a recovery_token) counts toward the try count or gets retried.
This is deliberate, not incidental: it's the same recovery_token FK that
makes a live-demo VOICE call structurally unable to be credited with a
recovery (app/bounds.py's FanOutLeg.is_nudge) -- a call is never a "try"
here either, only the nudge that call points to is.

Call process_expired_nudges() any time after run_attribution() has run --
it only ever acts on at-risk rows attribution left "still_open" or that
never got a recovery_outcomes row at all.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bounds import execute_action
from app.canonical.vocabulary import UNRESOLVED_STATUSES, Action, Cohort
from app.models import Business, PolicyBounds, RecoveryAttempt, RecoveryOutcome, RevenueAtRisk
# Imported rather than re-declared: a second copy of this mapping would
# drift from attribution.py's, and these two modules must label an
# unconverted loss the same way to stay comparable in the same table.
from app.recovery.attribution import _METHOD_FOR_ENTITY

NUDGE_RETRY_MAX_TRIES = 3


def _nudge_attempts(session: Session, at_risk_id: uuid.UUID) -> list[RecoveryAttempt]:
    return session.execute(
        select(RecoveryAttempt)
        .where(RecoveryAttempt.at_risk_id == at_risk_id, RecoveryAttempt.recovery_token.is_not(None))
        .order_by(RecoveryAttempt.attempt_number)
    ).scalars().all()


def process_expired_nudges(session: Session, business_id: uuid.UUID, *, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    business = session.get(Business, business_id)
    bounds = session.get(PolicyBounds, business_id)
    stats = {"retried": 0, "not_recovered": 0, "still_waiting": 0}

    candidates = session.execute(
        select(RevenueAtRisk).where(
            RevenueAtRisk.business_id == business_id, RevenueAtRisk.status.in_(UNRESOLVED_STATUSES)
        )
    ).scalars().all()

    for at_risk in candidates:
        already = session.execute(
            select(RecoveryOutcome.outcome_id).where(RecoveryOutcome.at_risk_id == at_risk.at_risk_id)
        ).first()
        if already is not None:
            continue

        nudges = _nudge_attempts(session, at_risk.at_risk_id)
        executed_nudges = [a for a in nudges if a.executed_at is not None]
        if not executed_nudges:
            continue  # nothing sent yet for this row -- not this module's concern

        latest_nudge = executed_nudges[-1]
        if latest_nudge.attribution_expires_at >= now:
            stats["still_waiting"] += 1
            continue  # window still open; run_attribution() may yet match it

        if len(executed_nudges) >= NUDGE_RETRY_MAX_TRIES:
            session.add(
                RecoveryOutcome(
                    outcome_id=uuid.uuid4(), business_id=business_id, at_risk_id=at_risk.at_risk_id,
                    attempt_id=latest_nudge.attempt_id, cohort=latest_nudge.cohort,
                    outcome="NOT_RECOVERED", recovered_minor=0, currency=at_risk.currency,
                    # Nothing was matched -- the entity's own scheme, at WEAK,
                    # matching how attribution.py labels its EXPIRED rows.
                    # TOKEN_CLICK/STRONG here would claim strong evidence of a
                    # click for a loss that explicitly never converted, and
                    # would poison any query grouping by method/confidence.
                    attribution_method=_METHOD_FOR_ENTITY.get(at_risk.entity_type, "PAYMENT_INTENT_MATCH"),
                    attribution_confidence="WEAK",
                )
            )
            at_risk.status = "LOST"
            at_risk.resolved_at = now
            stats["not_recovered"] += 1
            continue

        # Retry: same channel and entity, decided fresh right now -- goes
        # through execute_action() and every H-bound exactly like the
        # original nudge did, including a brand new token (the fixed
        # attribution.py checks EVERY attempt's own token, not just the
        # latest, so a click on try #1's token still counts even after
        # try #2 has been decided).
        cohort = Cohort(latest_nudge.cohort)
        result = execute_action(
            session, business=business, bounds=bounds, at_risk_id=at_risk.at_risk_id,
            customer_id=latest_nudge.customer_id, entity_type=at_risk.entity_type, entity_id=at_risk.entity_id,
            action=Action(latest_nudge.strategy), channel=latest_nudge.channel, template_id=None,
            cohort=cohort, value_minor=at_risk.at_risk_minor, attribution_key_value=str(at_risk.entity_id),
            loss_category=at_risk.loss_category, now=now,
            score_snapshot={"nudge_retry_of": str(latest_nudge.attempt_id), "nudge_try_number": len(executed_nudges) + 1},
        )
        if result.enqueued:
            stats["retried"] += 1
        else:
            stats["still_waiting"] += 1  # suppressed this pass (e.g. quiet hours) -- next pass tries again

    return stats
