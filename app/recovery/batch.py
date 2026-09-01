"""Runs policy -> bounds -> channels over every OPEN at-risk record.

One call per record: decide() picks the action, assign_cohort() places it
in TREATMENT/HOLDOUT, execute_action() is the single chokepoint that
decides whether it's allowed and writes the audit row, make_dispatcher()
sends it if allowed. This is the "decision + execution layer" the
data-generation spec's coverage table attributes recovery_attempts,
recovery_tokens, and recovery_batches to.
"""

import random
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bounds import execute_action
from app.canonical.vocabulary import Action, EntityType, FAILURE_TAXONOMY, FailureReason, MandateStatus
from app.channels import assign_cohort, make_dispatcher
from app.models import Business, CustomerContactability, PolicyBounds, RecoveryAttempt, RecoveryBatch, RevenueAtRisk, Subscription
from app.policy import POLICY_VERSION, decide

# Only PERMANENT, per-entity compliance facts close the loss out
# (SUPPRESSED): opted out, DND-registered, hard-bounced, an open dispute,
# a refund already recorded, a revoked mandate blocking a charge, or
# policy itself deciding STOP. Contact-cap/quiet-hours/min-gap (h7/h8/h10)
# are temporary and self-clear; emergency_stop, recovery_disabled, dry_run,
# holdout, and held-for-approval are global or experimental conditions, not
# facts about this specific loss. All of those stay OPEN so a later batch
# reconsiders them once the condition clears -- marking them SUPPRESSED
# would permanently lock the loss out the moment the business re-enables
# recovery or quiet hours pass.
_TERMINAL_SUPPRESSIONS_PREFIX = ("h1_", "h2_", "h4_", "h5_", "h6_", "h9_")
_TERMINAL_SUPPRESSIONS = {"h3_policy_stop"}

CHANNEL_PREFERENCE = ["EMAIL", "SMS", "WHATSAPP"]


def _needs_channel(action: Action) -> bool:
    return action in (Action.NUDGE, Action.REQUEST_NEW_INSTRUMENT, Action.RECOLLECT_MANDATE)


def _choose_channel(session: Session, business_id: uuid.UUID, customer_id: uuid.UUID | None) -> str | None:
    if customer_id is None:
        return "EMAIL"
    for channel in CHANNEL_PREFERENCE:
        contact = session.get(CustomerContactability, (business_id, customer_id, channel))
        if contact is not None and contact.opted_in and not contact.hard_bounced:
            if channel in ("SMS", "WHATSAPP") and contact.dnd_registered:
                continue
            return channel
    return "EMAIL"  # no viable channel -- pass one through anyway so bounds.py's consent check fires with a real reason


def _mandate_state(session: Session, at_risk: RevenueAtRisk) -> MandateStatus | None:
    if at_risk.entity_type != "SUBSCRIPTION":
        return None
    subscription = session.get(Subscription, at_risk.entity_id)
    if subscription is None or subscription.mandate_status is None:
        return None
    return MandateStatus(subscription.mandate_status)


def run_batch(session: Session, business_id: uuid.UUID, *, seed: int = 42, now: datetime | None = None) -> RecoveryBatch:
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)

    business = session.get(Business, business_id)
    bounds = session.get(PolicyBounds, business_id)

    batch_id = uuid.uuid4()
    batch = RecoveryBatch(
        batch_id=batch_id, business_id=business_id, started_at=now,
        policy_version=POLICY_VERSION, bounds_version=bounds.policy_version, dry_run=business.dry_run,
    )
    session.add(batch)
    session.flush()

    at_risk_rows = session.execute(
        select(RevenueAtRisk)
        .where(RevenueAtRisk.business_id == business_id, RevenueAtRisk.status == "OPEN")
        # entity_id as a tiebreaker, not just detected_at: every record a
        # single sweep call opens shares that call's one now(), so without
        # a second sort key the LIMIT below picks an arbitrary subset among
        # ties -- a different one on every run, which is exactly what broke
        # the Day 12 determinism check. entity_id is deterministic (see
        # upsert.py's _deterministic_id), so this ordering is now stable.
        .order_by(RevenueAtRisk.detected_at, RevenueAtRisk.entity_id)
        .limit(bounds.max_entities_per_batch)
    ).scalars().all()

    for at_risk in at_risk_rows:
        entity_type = EntityType(at_risk.entity_type)
        failure_reason = FailureReason(at_risk.failure_reason) if at_risk.failure_reason else None
        mandate_state = _mandate_state(session, at_risk)

        attempt_number = (
            session.execute(
                select(RecoveryAttempt.attempt_id).where(RecoveryAttempt.at_risk_id == at_risk.at_risk_id)
            ).all()
        )
        attempt_number = len(attempt_number) + 1

        action = decide(
            failure_reason=failure_reason, attempt_number=attempt_number, entity_type=entity_type,
            mandate_state=mandate_state, value_minor=at_risk.at_risk_minor,
        )
        cohort = assign_cohort(business_id, at_risk.entity_type, at_risk.entity_id)

        channel = _choose_channel(session, business_id, at_risk.customer_id) if _needs_channel(action) else None
        dispatcher = make_dispatcher(session, business_id=business_id, loss_category=at_risk.loss_category, rng=rng)

        window = FAILURE_TAXONOMY[failure_reason].attribution_window_seconds if failure_reason else None
        result = execute_action(
            session, business=business, bounds=bounds, at_risk_id=at_risk.at_risk_id,
            customer_id=at_risk.customer_id, entity_type=at_risk.entity_type, entity_id=at_risk.entity_id,
            action=action, channel=channel, template_id=None, cohort=cohort, value_minor=at_risk.at_risk_minor,
            attribution_key_value=str(at_risk.entity_id), failure_window_seconds=window,
            dispatch=dispatcher, now=now,
        )

        batch.entities_scanned += 1
        batch.decisions_made += 1
        if result.executed:
            batch.actions_executed += 1
            at_risk.status = "IN_RECOVERY"
        elif result.suppressed_reason and result.suppressed_reason.startswith(_TERMINAL_SUPPRESSIONS_PREFIX) or result.suppressed_reason in _TERMINAL_SUPPRESSIONS:
            batch.actions_suppressed += 1
            at_risk.status = "SUPPRESSED"
        else:
            batch.actions_stopped += 1  # dry_run / holdout_cohort / held_for_approval -- not a block, stays OPEN

    batch.at_risk_minor = sum(r.at_risk_minor for r in at_risk_rows)
    batch.completed_at = datetime.now(timezone.utc)
    return batch
