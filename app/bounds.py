"""Day 8: the H1-H10 hard-bounds chokepoint. execute_action() is the ONLY

path to a channel dispatch -- policy.py decides what SHOULD happen; this
decides whether it's ALLOWED to happen, and is the one place that writes
recovery_attempts, so there is exactly one audit trail, not several.

The literal H1-H10 list is never spelled out verbatim anywhere in the
plan. H4 (open dispute), H5 (hard-bounced channel), and H8 (quiet hours)
are anchored by data-generation.md's sad-path table, which names those
numbers directly; the rest are constructed from the plan's own "Stopping
rules: non-retryable, max attempts, open dispute, revoked mandate, refund
pending" plus policy_bounds' own columns. Flagged for review, the same as
the loss-category taxonomy was on Day 1 -- get it wrong and suppression
counts in the eventual batch report are simply invented.

H1  consent            not opted in for this channel
H2  mandate            a revoked/expired mandate blocks an automated CHARGE
                        (checked for SUBSCRIPTION entities directly; PAYMENT
                        entities have no persisted subscription link in this
                        build's schema, so this check does not apply to them
                        -- documented gap, not a silent omission)
H3  policy stop        policy.py already decided STOP -- nothing to bound
H4  open dispute        an OPEN dispute on the payment is a hard stop
H5  hard-bounced        channel has hard-bounced before; never retry it
H6  refund recorded     a refund already exists on this payment
H7  contact cap         over policy_bounds.max_contacts_per_week
H8  quiet hours         outside the customer's allowed local hours
H9  DND                 SMS/WhatsApp/voice to a DND-registered number
H10 min gap             under policy_bounds.min_gap_hours since last contact

Policy bounds (soft, business-configurable, checked only once every H
passes): max_entities_per_batch, max_batch_spend_minor, max_sends_per_hour,
human_approval_above_minor (routes to HELD, not suppressed -- a human may
still say yes).
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import channels
from app.canonical.attribution_keys import make_key
from app.canonical.vocabulary import Action, Cohort, MandateStatus, NormalizationStage
from app.dispatch_queue import enqueue_one, priority_for
from app.models import (
    Business,
    Customer,
    CustomerContactability,
    Dispute,
    Payment,
    PolicyBounds,
    RecoveryAttempt,
    Subscription,
)
from app.normalize.structural import NormalizationError
from app.settings import EMERGENCY_STOP

# How long a queue item is worth retrying to SEND before it's stale and
# gets given up on -- distinct from the attribution window (how long we
# wait for a RESPONSE after it sent). An hour is generous for a
# single-worker demo drain and still bounded for a real deployment.
DISPATCH_STALE_AFTER = timedelta(hours=1)

IST = timezone(timedelta(hours=5, minutes=30))
CHECKOUT_ATTRIBUTION_WINDOW_SECONDS = 72 * 3600
INVOICE_ATTRIBUTION_WINDOW_SECONDS = 90 * 86400
DEFAULT_POLICY_VERSION = "policy@v1"

AWAITING_HUMAN_SUPPRESSED_REASON = "awaiting_human_review"
HELD_FOR_APPROVAL_SUPPRESSED_REASON = "held_for_approval"

@dataclass
class FanOutLeg:
    """One channel of a multi-channel decision (e.g. VOICE + WHATSAPP fired

    together for a live-demo customer). Exactly one leg per call should
    carry is_nudge=True -- that is the only leg whose recovery_attempts
    row gets the minted token, which is what makes it (and only it)
    attribution-eligible. body_override lets a caller hand in pre-built
    text (an LLM-written call script) instead of the standard rendered
    template.
    """
    channel: str
    is_nudge: bool = False
    body_override: Optional[str] = None


@dataclass
class ActionResult:
    attempt_id: uuid.UUID
    executed: bool
    suppressed_reason: Optional[str]
    enqueued: bool = False
    all_attempt_ids: list[uuid.UUID] = field(default_factory=list)


def _consent_snapshot(contact: Optional[CustomerContactability]) -> dict:
    if contact is None:
        return {"found": False}
    return {
        "found": True,
        "opted_in": contact.opted_in,
        "dnd_registered": contact.dnd_registered,
        "hard_bounced": contact.hard_bounced,
        "quiet_hours_start": contact.quiet_hours_start.isoformat(),
        "quiet_hours_end": contact.quiet_hours_end.isoformat(),
        "consecutive_failures": contact.consecutive_failures,
    }


def _bounds_snapshot(bounds: PolicyBounds) -> dict:
    return {
        "max_attempts_per_entity": bounds.max_attempts_per_entity,
        "max_contacts_per_week": bounds.max_contacts_per_week,
        "min_gap_hours": bounds.min_gap_hours,
        "max_entities_per_batch": bounds.max_entities_per_batch,
        "max_batch_spend_minor": bounds.max_batch_spend_minor,
        "max_sends_per_hour": bounds.max_sends_per_hour,
        "human_approval_above_minor": bounds.human_approval_above_minor,
        "allow_automated_charge": bounds.allow_automated_charge,
        "quiet_hours_enforced": bounds.quiet_hours_enforced,
        "holdout_percent": bounds.holdout_percent,
        "policy_version": bounds.policy_version,
    }


def _in_quiet_hours(now_ist: datetime, start: time, end: time) -> bool:
    now_t = now_ist.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= now_t < end
    return now_t >= start or now_t < end  # window wraps midnight (e.g. 21:00-09:00)


def _check_entity_bounds(
    session: Session, *, action: Action, entity_type: str, entity_id: uuid.UUID,
) -> Optional[str]:
    """H2/H3/H4/H6 -- facts about the ENTITY, independent of which channel

    (if any) is used to reach the customer. Checked once per decision,
    shared by every leg of a fan-out.
    """
    if action == Action.STOP:
        return "h3_policy_stop"

    if action == Action.ESCALATE_HUMAN:
        # No channel by construction, so without this it fell through every
        # consent/quiet-hours gate straight into dispatch -- landing in
        # channels.py's generic RETRY_NOW-shaped else-branch even though
        # nothing was ever sent to the customer. Routes to the human queue
        # instead; never executes.
        return AWAITING_HUMAN_SUPPRESSED_REASON

    if entity_type == "SUBSCRIPTION" and action in (Action.RETRY_NOW, Action.RETRY_SCHEDULED):
        subscription = session.get(Subscription, entity_id)
        if subscription is not None and subscription.mandate_status == MandateStatus.REVOKED.value:
            return "h2_revoked_mandate_blocks_charge"

    if entity_type == "PAYMENT":
        payment = session.get(Payment, entity_id)
        if payment is not None:
            if payment.amount_refunded_minor > 0:
                return "h6_refund_recorded"
            open_dispute = session.execute(
                select(Dispute.dispute_id).where(Dispute.payment_id == entity_id, Dispute.status == "OPEN").limit(1)
            ).first()
            if open_dispute is not None:
                return "h4_open_dispute"

    return None


def _check_channel_bounds(
    session: Session,
    *,
    business: Business,
    bounds: PolicyBounds,
    contact: Optional[CustomerContactability],
    channel: Optional[str],
    now: datetime,
) -> Optional[str]:
    """H1/H5/H7/H8/H9/H10 -- consent and pacing, specific to ONE channel's

    contactability row. Run once per fan-out leg at decision time, and run
    AGAIN by the dispatch worker immediately before it actually sends --
    state (consent, quiet hours, the contact cap) can change in the gap
    between a message being queued and a worker picking it up.
    """
    if channel is None:
        return None

    if contact is None:
        return "h1_no_consent_record"
    if not contact.opted_in:
        return "h1_not_opted_in"
    if contact.hard_bounced:
        return "h5_hard_bounced"
    if channel in ("SMS", "WHATSAPP", "VOICE") and contact.dnd_registered:
        return "h9_dnd_registered"
    if bounds.quiet_hours_enforced:
        now_ist = now.astimezone(IST)
        if _in_quiet_hours(now_ist, contact.quiet_hours_start, contact.quiet_hours_end):
            return "h8_quiet_hours"

    week_ago = now - timedelta(days=7)
    contacts_this_week = session.execute(
        select(func.count())
        .select_from(RecoveryAttempt)
        .where(
            RecoveryAttempt.business_id == business.business_id,
            RecoveryAttempt.customer_id == contact.customer_id,
            RecoveryAttempt.executed_at.is_not(None),
            RecoveryAttempt.executed_at >= week_ago,
        )
    ).scalar_one()
    if contacts_this_week >= bounds.max_contacts_per_week:
        return "h7_contact_cap"

    if contact.last_contacted_at is not None:
        gap_hours = (now - contact.last_contacted_at).total_seconds() / 3600
        if gap_hours < bounds.min_gap_hours:
            return "h10_min_gap"

    return None


def _check_policy_bounds(bounds: PolicyBounds, value_minor: int) -> Optional[str]:
    if value_minor >= bounds.human_approval_above_minor:
        return HELD_FOR_APPROVAL_SUPPRESSED_REASON
    return None


def _attribution(entity_type: str, action: Action, failure_window_seconds: Optional[int]) -> tuple[str, int]:
    if entity_type == "PAYMENT" and action in (Action.RETRY_NOW, Action.RETRY_SCHEDULED):
        key_type, window = "PAYMENT_INTENT", failure_window_seconds or 1800
    elif entity_type == "PAYMENT":
        key_type, window = "TOKEN", failure_window_seconds or (72 * 3600)
    elif entity_type == "CHECKOUT":
        key_type, window = "TOKEN", CHECKOUT_ATTRIBUTION_WINDOW_SECONDS
    elif entity_type == "INVOICE":
        key_type, window = "INVOICE", INVOICE_ATTRIBUTION_WINDOW_SECONDS
    elif entity_type == "SUBSCRIPTION":
        key_type, window = "SUBSCRIPTION", failure_window_seconds or (7 * 86400)
    else:
        raise NormalizationError(NormalizationStage.SEMANTIC, f"no attribution scheme for entity_type={entity_type!r}")
    return key_type, window


def execute_action(
    session: Session,
    *,
    business: Business,
    bounds: PolicyBounds,
    at_risk_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    entity_type: str,
    entity_id: uuid.UUID,
    action: Action,
    channel: Optional[str],
    template_id: Optional[uuid.UUID],
    cohort: Cohort,
    value_minor: int,
    attribution_key_value: str,
    loss_category: str = "",
    failure_window_seconds: Optional[int] = None,
    fan_out: Optional[list[FanOutLeg]] = None,
    now: Optional[datetime] = None,
    score_snapshot: Optional[dict] = None,
) -> ActionResult:
    """Authorize, and -- for a customer-facing channel -- enqueue.

    This is still the ONLY place recovery_attempts gets written and the
    ONLY place an action is authorized: every H-bound runs here, exactly
    as before. What changed is the last step. A channel-bearing action
    (NUDGE / REQUEST_NEW_INSTRUMENT / RECOLLECT_MANDATE) no longer calls a
    provider inline -- it writes a row to outbound_dispatches and returns.
    app/dispatch_worker.py sends it later, re-checking the channel bounds
    against the wall clock at that later moment before it does.

    A channel-less action (RETRY_NOW / RETRY_SCHEDULED / OPS_ALERT) was
    never a customer-facing send to begin with -- it keeps resolving
    synchronously, unchanged.

    fan_out lets one decision authorize more than one channel at once
    (a live-demo VOICE + WHATSAPP pair): each FanOutLeg becomes its own
    recovery_attempts row and its own outbound_dispatches row, but only
    the leg(s) marked is_nudge=True ever get the minted token on their
    ATTEMPT row -- see FanOutLeg's docstring for why that's what makes
    attribution isolation real instead of a promise.
    """
    now = now or datetime.now(timezone.utc)

    base_attempt_number = session.execute(
        select(func.count()).select_from(RecoveryAttempt).where(RecoveryAttempt.at_risk_id == at_risk_id)
    ).scalar_one()

    key_type, window_seconds = _attribution(entity_type, action, failure_window_seconds)

    # PAYMENT_INTENT attribution matches against the PROVIDER's intent id,
    # not ours, so the key has to carry it -- see
    # app/canonical/attribution_keys.py for what storing only the internal
    # id silently did to the recovered numbers.
    if key_type == "PAYMENT_INTENT" and entity_type == "PAYMENT":
        payment = session.get(Payment, entity_id)
        attribution_key_value = make_key(entity_id, payment.payment_intent_id if payment else None)

    entity_suppressed_reason: Optional[str] = None
    if EMERGENCY_STOP:
        entity_suppressed_reason = "emergency_stop"
    elif not business.recovery_enabled:
        entity_suppressed_reason = "recovery_disabled_for_business"
    else:
        entity_suppressed_reason = _check_entity_bounds(
            session, action=action, entity_type=entity_type, entity_id=entity_id
        )
        if entity_suppressed_reason is None and cohort == Cohort.HOLDOUT:
            entity_suppressed_reason = "holdout_cohort"
        if entity_suppressed_reason is None:
            entity_suppressed_reason = _check_policy_bounds(bounds, value_minor)
        if entity_suppressed_reason is None and business.dry_run:
            entity_suppressed_reason = "dry_run"

    legs = fan_out if fan_out is not None else (
        [FanOutLeg(channel=channel, is_nudge=True)] if channel is not None else []
    )

    if not legs:
        # RETRY_NOW / RETRY_SCHEDULED / OPS_ALERT: never a customer-facing
        # channel send, so there is nothing to queue -- resolves the same
        # instant it's decided, exactly as before this change.
        attempt_id = uuid.uuid4()
        executed = entity_suppressed_reason is None
        if executed:
            delivery_status, channel_receipt = (
                ("LOGGED", {"type": "ops_alert"}) if action.value == "OPS_ALERT"
                else ("SUBMITTED", {"simulated": True, "type": "retry"})
            )
        else:
            delivery_status, channel_receipt = None, {}
        session.add(
            RecoveryAttempt(
                attempt_id=attempt_id, business_id=business.business_id, customer_id=customer_id,
                at_risk_id=at_risk_id, correlation_id=f"{at_risk_id}:{base_attempt_number + 1}",
                attempt_number=base_attempt_number + 1, strategy=action.value, channel=None,
                template_id=template_id, cohort=cohort.value, policy_version=DEFAULT_POLICY_VERSION,
                bounds_version=bounds.policy_version, decided_at=now,
                consent_snapshot=_consent_snapshot(None),
                bounds_snapshot={**_bounds_snapshot(bounds), **(score_snapshot or {})},
                enqueued_at=now if executed else None, executed_at=now if executed else None,
                suppressed_reason=entity_suppressed_reason, channel_receipt=channel_receipt or None,
                delivery_status=delivery_status, cost_minor=0, attribution_key_type=key_type,
                attribution_key_value=attribution_key_value, attribution_window_seconds=window_seconds,
                attribution_expires_at=now + timedelta(seconds=window_seconds), recovery_token=None,
            )
        )
        return ActionResult(
            attempt_id=attempt_id, executed=executed, suppressed_reason=entity_suppressed_reason,
            all_attempt_ids=[attempt_id],
        )

    predicted_score = (score_snapshot or {}).get("predicted_score", 0.5)
    priority = priority_for(predicted_score, value_minor)
    customer = session.get(Customer, customer_id) if customer_id is not None else None

    shared_token: Optional[str] = None
    primary_attempt_id: Optional[uuid.UUID] = None
    any_enqueued = False
    first_leg_suppressed_reason = entity_suppressed_reason
    all_attempt_ids: list[uuid.UUID] = []

    for i, leg in enumerate(legs):
        attempt_id = uuid.uuid4()
        attempt_number = base_attempt_number + 1 + i
        all_attempt_ids.append(attempt_id)

        contact = None
        if customer_id is not None:
            contact = session.get(CustomerContactability, (business.business_id, customer_id, leg.channel))

        leg_suppressed = entity_suppressed_reason or _check_channel_bounds(
            session, business=business, bounds=bounds, contact=contact, channel=leg.channel, now=now,
        )
        if i == 0:
            first_leg_suppressed_reason = leg_suppressed

        leg_recovery_token = None
        payload = None
        provider = None
        if leg_suppressed is None:
            payload = channels.build_payload(
                session, business_id=business.business_id, at_risk_id=at_risk_id, attempt_id=attempt_id,
                entity_type=entity_type, entity_id=entity_id, loss_category=loss_category, channel=leg.channel,
                attribution_key_type=key_type, attribution_window_seconds=window_seconds, now=now,
                shared_recovery_token=shared_token,
            )
            if shared_token is None:
                shared_token = payload.recovery_token
            provider = channels.provider_for(leg.channel, customer.phone_e164 if customer else None)
            if leg.is_nudge:
                leg_recovery_token = payload.recovery_token
            if primary_attempt_id is None or leg.is_nudge:
                primary_attempt_id = attempt_id

        # RecoveryAttempt first, and flushed, before outbound_dispatches --
        # the dispatch row's attempt_id FK must already exist in the DB by
        # the time it's inserted, and SQLAlchemy's autoflush ordering
        # across two separately-added objects with no declared ORM
        # relationship between them is not guaranteed to respect that.
        session.add(
            RecoveryAttempt(
                attempt_id=attempt_id, business_id=business.business_id, customer_id=customer_id,
                at_risk_id=at_risk_id, correlation_id=f"{at_risk_id}:{attempt_number}",
                attempt_number=attempt_number, strategy=action.value, channel=leg.channel,
                template_id=template_id, cohort=cohort.value, policy_version=DEFAULT_POLICY_VERSION,
                bounds_version=bounds.policy_version, decided_at=now,
                consent_snapshot=_consent_snapshot(contact),
                bounds_snapshot={**_bounds_snapshot(bounds), **(score_snapshot or {})},
                enqueued_at=now if leg_suppressed is None else None,
                executed_at=None,  # set by app/dispatch_worker.py once a provider actually accepts it
                suppressed_reason=leg_suppressed,
                channel_receipt=None, delivery_status=("QUEUED" if leg_suppressed is None else None),
                cost_minor=0, attribution_key_type=key_type,
                attribution_key_value=leg_recovery_token or attribution_key_value,
                attribution_window_seconds=window_seconds,
                # Placeholder -- app/dispatch_worker.py moves this to
                # sent_at + window_seconds once the message actually goes
                # out. Left as-is here for a leg that never sends.
                attribution_expires_at=now + timedelta(seconds=window_seconds),
                recovery_token=leg_recovery_token,
            )
        )

        if leg_suppressed is None:
            session.flush()
            body = leg.body_override if leg.body_override is not None else payload.body
            session.add(
                enqueue_one(
                    business_id=business.business_id, attempt_id=attempt_id, at_risk_id=at_risk_id,
                    customer_id=customer_id, channel=leg.channel, provider=provider,
                    recovery_token=payload.recovery_token, priority=priority,
                    payload={"body": body, "target_url": payload.target_url},
                    now=now, expires_at=now + DISPATCH_STALE_AFTER,
                    cost_minor=channels.CHANNEL_COST_MINOR.get(leg.channel, 0),
                )
            )
            any_enqueued = True

    if primary_attempt_id is None:
        primary_attempt_id = all_attempt_ids[0]

    return ActionResult(
        attempt_id=primary_attempt_id, executed=any_enqueued, enqueued=any_enqueued,
        suppressed_reason=first_leg_suppressed_reason, all_attempt_ids=all_attempt_ids,
    )
