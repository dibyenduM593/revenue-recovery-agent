"""The outbound work list. app/worker.py drains raw_events inbound; this

is its mirror outbound -- app/dispatch_worker.py drains outbound_dispatches
the same way, claim-then-process, SKIP LOCKED.

enqueue_one() is called only from app/bounds.py::execute_action(), only
after every H-bound has already passed -- it is a work-list write, not a
new authorization path. Nothing here re-decides whether a message is
allowed to go out; that decision was already made and audited before this
function is ever called.
"""

import uuid
from datetime import datetime
from typing import Optional

from app.models import OutboundDispatch


def priority_for(predicted_score: float, at_risk_minor: int) -> int:
    """Expected-recoverable-value ordering -- the same predicted_score *

    at_risk_minor ranking app/recovery/batch.py already sorts candidates
    by (to decide which ones survive max_entities_per_batch), reused here
    rather than invented twice. Under a provider rate limit, the queue
    drains this order first: the highest expected recovery gets the
    scarce send slots before a coin's-worth of a customer who probably
    won't come back anyway.
    """
    return int(round(predicted_score * at_risk_minor))


def idempotency_key_for(attempt_id: uuid.UUID, channel: str) -> str:
    """Deterministic, not random: a worker that dies between 'provider

    accepted it' and 'commit' must not place the same call twice on the
    next claim pass. One (attempt, channel) pair can only ever produce one
    real send.
    """
    return f"{attempt_id}:{channel}"


def enqueue_one(
    *,
    business_id: uuid.UUID,
    attempt_id: uuid.UUID,
    at_risk_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    channel: str,
    provider: str,
    recovery_token: Optional[str],
    priority: int,
    payload: dict,
    now: datetime,
    expires_at: datetime,
    cost_minor: int,
) -> OutboundDispatch:
    dispatch_id = uuid.uuid4()
    return OutboundDispatch(
        dispatch_id=dispatch_id,
        business_id=business_id,
        attempt_id=attempt_id,
        at_risk_id=at_risk_id,
        customer_id=customer_id,
        channel=channel,
        provider=provider,
        recovery_token=recovery_token,
        priority=priority,
        status="PENDING",
        scheduled_for=now,
        delivery_tries=0,
        max_delivery_tries=5,
        expires_at=expires_at,
        idempotency_key=idempotency_key_for(attempt_id, channel),
        payload=payload,
        cost_minor=cost_minor,
    )
