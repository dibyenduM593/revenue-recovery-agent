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
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.canonical.vocabulary import Action, Cohort, MandateStatus, NormalizationStage
from app.models import (
    Business,
    CustomerContactability,
    Dispute,
    Payment,
    PolicyBounds,
    RecoveryAttempt,
    Subscription,
)
from app.normalize.structural import NormalizationError
from app.settings import EMERGENCY_STOP

IST = timezone(timedelta(hours=5, minutes=30))
CHECKOUT_ATTRIBUTION_WINDOW_SECONDS = 72 * 3600
INVOICE_ATTRIBUTION_WINDOW_SECONDS = 90 * 86400
DEFAULT_POLICY_VERSION = "policy@v1"

ATTRIBUTION_KEY_TYPE = {
    "CHECKOUT": "TOKEN",
    "SUBSCRIPTION": "SUBSCRIPTION",
    "INVOICE": "INVOICE",
}


@dataclass
class DispatchResult:
    channel: Optional[str]
    channel_receipt: dict
    delivery_status: Optional[str]
    cost_minor: int
    recovery_token: Optional[str]


@dataclass
class ActionContext:
    """Handed to the channel dispatcher only once bounds has cleared the

    action to proceed -- everything it needs to actually send something.
    """
    attempt_id: uuid.UUID
    business_id: uuid.UUID
    customer_id: uuid.UUID
    at_risk_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    action: Action
    channel: Optional[str]
    template_id: Optional[uuid.UUID]
    attribution_key_type: str
    attribution_window_seconds: int


DispatchFn = Callable[[ActionContext], DispatchResult]


@dataclass
class ActionResult:
    attempt_id: uuid.UUID
    executed: bool
    suppressed_reason: Optional[str]


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


def _check_hard_bounds(
    session: Session,
    *,
    business: Business,
    bounds: PolicyBounds,
    contact: Optional[CustomerContactability],
    channel: Optional[str],
    action: Action,
    entity_type: str,
    entity_id: uuid.UUID,
    now: datetime,
) -> Optional[str]:
    if action == Action.STOP:
        return "h3_policy_stop"

    if channel is not None:
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

    if contact is not None and channel is not None:
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
        return "held_for_approval"
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
    failure_window_seconds: Optional[int] = None,
    dispatch: Optional[DispatchFn] = None,
    now: Optional[datetime] = None,
) -> ActionResult:
    now = now or datetime.now(timezone.utc)

    attempt_number = (
        session.execute(
            select(func.count()).select_from(RecoveryAttempt).where(RecoveryAttempt.at_risk_id == at_risk_id)
        ).scalar_one()
        + 1
    )
    attempt_id = uuid.uuid4()
    correlation_id = f"{at_risk_id}:{attempt_number}"

    contact = None
    if customer_id is not None and channel is not None:
        contact = session.get(CustomerContactability, (business.business_id, customer_id, channel))

    suppressed_reason: Optional[str] = None
    if EMERGENCY_STOP:
        suppressed_reason = "emergency_stop"
    elif not business.recovery_enabled:
        suppressed_reason = "recovery_disabled_for_business"
    else:
        suppressed_reason = _check_hard_bounds(
            session, business=business, bounds=bounds, contact=contact, channel=channel,
            action=action, entity_type=entity_type, entity_id=entity_id, now=now,
        )
        if suppressed_reason is None and cohort == Cohort.HOLDOUT:
            suppressed_reason = "holdout_cohort"
        if suppressed_reason is None:
            suppressed_reason = _check_policy_bounds(bounds, value_minor)
        if suppressed_reason is None and business.dry_run:
            suppressed_reason = "dry_run"

    key_type, window_seconds = _attribution(entity_type, action, failure_window_seconds)

    executed = False
    channel_receipt: dict = {}
    delivery_status = None
    cost_minor = 0
    recovery_token = None

    if suppressed_reason is None and dispatch is not None:
        ctx = ActionContext(
            attempt_id=attempt_id, business_id=business.business_id, customer_id=customer_id,
            at_risk_id=at_risk_id, entity_type=entity_type, entity_id=entity_id,
            action=action, channel=channel, template_id=template_id,
            attribution_key_type=key_type, attribution_window_seconds=window_seconds,
        )
        result = dispatch(ctx)
        channel_receipt = result.channel_receipt
        delivery_status = result.delivery_status
        cost_minor = result.cost_minor
        recovery_token = result.recovery_token
        executed = True

    session.add(
        RecoveryAttempt(
            attempt_id=attempt_id,
            business_id=business.business_id,
            customer_id=customer_id,
            at_risk_id=at_risk_id,
            correlation_id=correlation_id,
            attempt_number=attempt_number,
            strategy=action.value,
            channel=channel,
            template_id=template_id,
            cohort=cohort.value,
            policy_version=DEFAULT_POLICY_VERSION,
            bounds_version=bounds.policy_version,
            decided_at=now,
            consent_snapshot=_consent_snapshot(contact),
            bounds_snapshot=_bounds_snapshot(bounds),
            executed_at=now if executed else None,
            suppressed_reason=suppressed_reason,
            channel_receipt=channel_receipt or None,
            delivery_status=delivery_status,
            cost_minor=cost_minor,
            attribution_key_type=key_type,
            attribution_key_value=recovery_token or attribution_key_value,
            attribution_window_seconds=window_seconds,
            attribution_expires_at=now + timedelta(seconds=window_seconds),
            recovery_token=recovery_token,
        )
    )

    if executed and contact is not None:
        contact.last_contacted_at = now

    return ActionResult(attempt_id=attempt_id, executed=executed, suppressed_reason=suppressed_reason)
