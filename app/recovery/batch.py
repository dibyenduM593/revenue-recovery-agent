"""Runs policy -> bounds -> channels over every OPEN at-risk record.

One call per record: decide() picks the action, assign_cohort() places it
in TREATMENT/HOLDOUT, execute_action() is the single chokepoint that
decides whether it's allowed and writes the audit row, make_dispatcher()
sends it if allowed. This is the "decision + execution layer" the
data-generation spec's coverage table attributes recovery_attempts,
recovery_tokens, and recovery_batches to.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.bounds import execute_action
from app.canonical.vocabulary import Action, EntityType, FAILURE_TAXONOMY, FailureReason, MandateStatus
from app.channels import assign_cohort
from app.models import (
    Business,
    Customer,
    CustomerContactability,
    CustomerRecoveryProfile,
    PolicyBounds,
    RecoveryAttempt,
    RecoveryBatch,
    RevenueAtRisk,
    Subscription,
)
from app.policy import HUMAN_REVIEW_THRESHOLD_MINOR, INVOICE_NUDGE_LIMIT, POLICY_VERSION, decide
from app.scoring import port
from app.scoring.features import build_feature_context, build_features

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

# 7 bound parameters per customer_recovery_profile row against Postgres's
# 65,535-parameter statement limit.
PROFILE_UPSERT_CHUNK = 5_000


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


def _escalation_reason(
    *,
    failure_reason: FailureReason | None,
    attempt_number: int,
    entity_type: EntityType,
    mandate_state: MandateStatus | None,
    value_minor: int,
) -> str | None:
    """Mirrors decide()'s own branch order so the reason a row landed on

    ESCALATE_HUMAN is captured at decision time, instead of reverse-derived
    later from a bounds_snapshot that never recorded it. Only meaningful
    when decide() actually returned ESCALATE_HUMAN for these same inputs.
    """
    if entity_type == EntityType.INVOICE:
        return "invoice_dunning_exhausted" if attempt_number > INVOICE_NUDGE_LIMIT else None

    if mandate_state != MandateStatus.REVOKED and failure_reason is not None:
        entry = FAILURE_TAXONOMY[failure_reason]
        if entry.retryable and attempt_number > entry.max_attempts and entry.escalation_action == Action.ESCALATE_HUMAN:
            return "do_not_honor_retries_exhausted"

    if value_minor >= HUMAN_REVIEW_THRESHOLD_MINOR:
        return "value_threshold"

    return None


def run_batch(session: Session, business_id: uuid.UUID, *, seed: int = 42, now: datetime | None = None) -> RecoveryBatch:
    # seed no longer drives an in-process RNG here -- dispatch simulation
    # moved to app/dispatch_worker.py, which seeds its own. Kept as a
    # parameter for call-site compatibility (orchestrator.py already
    # passes it) and because it's still the meaningful "which deterministic
    # run is this" handle for anyone calling run_batch directly.
    now = now or datetime.now(timezone.utc)

    business = session.get(Business, business_id)
    bounds = session.get(PolicyBounds, business_id)

    batch_id = uuid.uuid4()
    batch = RecoveryBatch(
        batch_id=batch_id, business_id=business_id, started_at=now,
        policy_version=POLICY_VERSION, bounds_version=bounds.policy_version, dry_run=business.dry_run,
    )
    session.add(batch)
    session.flush()

    # Candidates: every OPEN row, not yet limited -- the score decides which
    # ones survive the limit, so the limit has to apply after scoring, not
    # in SQL before it. entity_id stays as the deterministic tiebreaker
    # (see the determinism note this replaced): without one, a
    # single sweep's shared now() makes detected_at ties resolve to an
    # arbitrary subset on every run.
    candidates = session.execute(
        select(RevenueAtRisk)
        .where(
            RevenueAtRisk.business_id == business_id,
            RevenueAtRisk.status == "OPEN",
            # A live-demo walk-up row (app/live_demo.py) is decided by its
            # own launch() -- a VOICE+WHATSAPP fan-out on a forced-TREATMENT
            # cohort, sharing one nudge token. Left in the generic pool
            # here, decide()/_choose_channel() would reach it first with an
            # ordinary single-channel action (and, since the walk-up
            # customer's own phone is often the ONE number on
            # TWILIO_ALLOWLIST, that ordinary action could itself place a
            # real send) before launch() ever runs -- two decisions for one
            # planted item, one of them wrong.
            ~RevenueAtRisk.attributes.has_key("live_demo"),
        )
        .order_by(RevenueAtRisk.detected_at, RevenueAtRisk.entity_id)
    ).scalars().all()

    # One bulk load of every customer's history, rather than this module's
    # four history queries per candidate -- at corpus scale (tens of
    # thousands of open rows) the per-candidate version is ~5 round trips x N.
    feature_context = build_feature_context(session, business_id) if candidates else None

    scored: list[tuple[float, str, dict, RevenueAtRisk]] = []
    for at_risk in candidates:
        customer = session.get(Customer, at_risk.customer_id) if at_risk.customer_id else None
        if customer is not None and customer.email:
            features = build_features(session, at_risk, customer, context=feature_context)
            predicted_score, score_version = port.score(features)
        else:
            features, predicted_score, score_version = {}, 0.5, "off"
        scored.append((predicted_score, score_version, features, at_risk))

    # Score prioritises expected loss (score x at_risk_minor); it never
    # decides -- policy.decide() below still runs unscored, on
    # failure_reason/attempt/entity_type/mandate_state alone. detected_at,
    # entity_id stay as tiebreakers so the ordering is still deterministic
    # once expected loss ties (e.g. every score@v1=0.5 "off" row).
    scored.sort(key=lambda item: (-(item[0] * item[3].at_risk_minor), item[3].detected_at, item[3].entity_id))
    scored = scored[: bounds.max_entities_per_batch]

    # Upsert, not insert: a re-run rescores the same still-OPEN items (an
    # item held for approval or in the holdout stays OPEN and comes back
    # in the next batch), and the second pass should refresh that item's
    # score snapshot rather than collide with the first pass's row.
    profile_rows = [
        {
            "business_id": business_id,
            "customer_id": at_risk.customer_id,
            "at_risk_id": at_risk.at_risk_id,
            "predicted_score": predicted_score,
            "score_version": score_version,
            "features": features,
            "scored_at": now,
        }
        for predicted_score, score_version, features, at_risk in scored
        if at_risk.customer_id is not None
    ]
    # Chunked: Postgres allows at most 65,535 bound parameters per
    # statement, and this table binds 7 per row -- a corpus-scale batch of
    # ~39,000 rows would blow that in a single VALUES list.
    for start in range(0, len(profile_rows), PROFILE_UPSERT_CHUNK):
        chunk = profile_rows[start : start + PROFILE_UPSERT_CHUNK]
        stmt = pg_insert(CustomerRecoveryProfile).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                constraint="customer_recovery_profile_pkey",
                set_={
                    "customer_id": stmt.excluded.customer_id,
                    "predicted_score": stmt.excluded.predicted_score,
                    "score_version": stmt.excluded.score_version,
                    "features": stmt.excluded.features,
                    "scored_at": stmt.excluded.scored_at,
                },
            )
        )

    at_risk_rows = [item[3] for item in scored]
    score_by_at_risk_id = {item[3].at_risk_id: (item[0], item[1]) for item in scored}

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

        window = FAILURE_TAXONOMY[failure_reason].attribution_window_seconds if failure_reason else None
        predicted_score, score_version = score_by_at_risk_id.get(at_risk.at_risk_id, (0.5, "off"))
        score_snapshot = {"predicted_score": predicted_score, "score_version": score_version}
        if action == Action.ESCALATE_HUMAN:
            escalation_reason = _escalation_reason(
                failure_reason=failure_reason, attempt_number=attempt_number, entity_type=entity_type,
                mandate_state=mandate_state, value_minor=at_risk.at_risk_minor,
            )
            if escalation_reason is not None:
                score_snapshot["escalation_reason"] = escalation_reason
        result = execute_action(
            session, business=business, bounds=bounds, at_risk_id=at_risk.at_risk_id,
            customer_id=at_risk.customer_id, entity_type=at_risk.entity_type, entity_id=at_risk.entity_id,
            action=action, channel=channel, template_id=None, cohort=cohort, value_minor=at_risk.at_risk_minor,
            attribution_key_value=str(at_risk.entity_id), failure_window_seconds=window,
            loss_category=at_risk.loss_category, now=now,
            score_snapshot=score_snapshot,
        )

        batch.entities_scanned += 1
        batch.decisions_made += 1
        if result.executed:
            # "executed" now means authorized-and-queued, not confirmed-sent
            # -- see app/dispatch_worker.py. This tile is immediate batch
            # feedback, not the audit-grade number; report.py reads
            # recovery_attempts/recovery_outcomes fresh, not this counter.
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
