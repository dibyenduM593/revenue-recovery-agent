"""SKIP LOCKED worker that drains outbound_dispatches, highest priority

first. The outbound mirror of app/worker.py's inbound raw_events drain --
same claim-then-process shape, same reason for it: claiming holds the row
lock only long enough to flip status and set locked_until, then commits
immediately, so a worker that dies mid-send lets a later pass reclaim the
row once locked_until has passed rather than losing track of it.

The one thing this worker does that decision time (app/bounds.py) could
not: it re-checks every TIME-and-CONSENT-sensitive bound (H1/H5/H7/H8/H9/
H10, plus the business-level kill switches) against the CURRENT wall
clock immediately before calling a provider. A message authorized at
20:55 and drained at 21:05 must not go out if quiet hours started at
21:00 -- that gap cannot exist while dispatch was synchronous, and does
exist the moment a queue sits between decision and send.
"""

import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app import channels
from app.bounds import _check_channel_bounds  # the send-time re-check, deliberately reusing decision-time's own logic
from app.db import SessionLocal
from app.models import Business, Customer, CustomerContactability, OutboundDispatch, PolicyBounds, RecoveryAttempt
from app.settings import DEMO_RECIPIENT_EMAIL, EMERGENCY_STOP

logger = logging.getLogger(__name__)

BATCH_SIZE = 20
LOCK_TIMEOUT = timedelta(minutes=5)
RETRY_BACKOFF_SECONDS = [60, 300, 900, 3600]  # ~1m, 5m, 15m, 1h -- index by delivery_tries
RATE_LIMIT_DEFER_SECONDS = 300

# Everything not listed here is treated as retryable, which is what lets a
# WhatsApp message attempted before the recipient joined the sandbox
# (RECIPIENT_NOT_OPTED_IN) succeed on its own a few minutes later with no
# code path change. Not-configured is the one outcome no amount of
# retrying fixes.
_PERMANENT_STATUSES = {"SKIPPED_NOT_CONFIGURED"}


def _claim_batch(business_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    now = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        conditions = [
            or_(
                OutboundDispatch.status == "PENDING",
                and_(OutboundDispatch.status == "IN_FLIGHT", OutboundDispatch.locked_until < now),
            ),
            OutboundDispatch.scheduled_for <= now,
        ]
        if business_id is not None:
            conditions.append(OutboundDispatch.business_id == business_id)

        stmt = (
            select(OutboundDispatch.dispatch_id)
            .where(*conditions)
            .order_by(OutboundDispatch.priority.desc(), OutboundDispatch.scheduled_for)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        ids = list(session.scalars(stmt))
        if ids:
            session.execute(
                update(OutboundDispatch)
                .where(OutboundDispatch.dispatch_id.in_(ids))
                .values(status="IN_FLIGHT", locked_until=now + LOCK_TIMEOUT)
            )
        session.commit()
        return ids
    finally:
        session.close()


def _finalize_attempt(
    session: Session, attempt: RecoveryAttempt, *, executed: bool, delivery_status: str | None,
    channel_receipt: dict | None, cost_minor: int, suppressed_reason: str | None, now: datetime,
) -> None:
    if executed:
        attempt.executed_at = now
        attempt.delivery_status = delivery_status
        attempt.channel_receipt = channel_receipt
        attempt.cost_minor = cost_minor
        # The attribution clock starts when the customer could actually have
        # SEEN the message, not when we decided to send it -- see the
        # DISPATCH_STALE_AFTER / attribution_expires_at split in bounds.py.
        attempt.attribution_expires_at = now + timedelta(seconds=attempt.attribution_window_seconds)
    else:
        attempt.suppressed_reason = suppressed_reason
        attempt.delivery_status = delivery_status


def _process_one(dispatch_id: uuid.UUID, rng: random.Random) -> str:
    """Returns 'sent', 'suppressed', 'deferred', 'retrying', or 'failed'."""
    now = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        row = session.get(OutboundDispatch, dispatch_id)
        if row is None or row.status not in ("PENDING", "IN_FLIGHT"):
            return "sent"  # already handled by another pass

        attempt = session.get(RecoveryAttempt, row.attempt_id)

        if row.provider_message_id is not None:
            # A provider already accepted this row and handed back an id; the
            # only way it is being claimed again is that the worker died
            # between that accept and the commit, and the lease expired.
            # Re-sending here would place a SECOND real call to a customer
            # for one decision -- the one duplicate the idempotency_key
            # cannot prevent, because this is the same row retrying rather
            # than a second row. Close the books on what the provider
            # already did instead of doing it twice.
            row.status = "SENT"
            row.sent_at = row.sent_at or now
            if attempt is not None and attempt.executed_at is None:
                _finalize_attempt(
                    session, attempt, executed=True, delivery_status="SENT",
                    channel_receipt={"recovered_after_worker_restart": True,
                                     "provider_message_id": row.provider_message_id},
                    cost_minor=row.cost_minor, suppressed_reason=None, now=now,
                )
            session.commit()
            return "sent"

        if row.expires_at < now:
            row.status = "EXPIRED"
            row.last_error = "stale: not sent before expires_at"
            if attempt is not None:
                _finalize_attempt(
                    session, attempt, executed=False, delivery_status=None, channel_receipt=None,
                    cost_minor=0, suppressed_reason="dispatch_expired_unsent", now=now,
                )
            session.commit()
            return "failed"

        business = session.get(Business, row.business_id)
        bounds = session.get(PolicyBounds, row.business_id)

        # Business-level kill switches can flip between decision and send
        # too -- re-check them here, not just the per-channel H-bounds.
        # contact is bound before the branch on purpose: it is read again
        # further down (last_contacted_at), and binding it only inside the
        # else would make that read depend on an early return staying
        # exactly where it is.
        contact = None
        send_time_reason = None
        if EMERGENCY_STOP:
            send_time_reason = "emergency_stop"
        elif business is None or bounds is None:
            send_time_reason = "business_config_missing"
        elif not business.recovery_enabled:
            send_time_reason = "recovery_disabled_for_business"
        else:
            if row.customer_id is not None:
                contact = session.get(CustomerContactability, (row.business_id, row.customer_id, row.channel))
            send_time_reason = _check_channel_bounds(
                session, business=business, bounds=bounds, contact=contact, channel=row.channel, now=now,
            )

        if send_time_reason is not None:
            row.status = "SUPPRESSED"
            row.last_error = send_time_reason
            if attempt is not None:
                _finalize_attempt(
                    session, attempt, executed=False, delivery_status=None, channel_receipt=None,
                    cost_minor=0, suppressed_reason=f"{send_time_reason}_at_send", now=now,
                )
            session.commit()
            return "suppressed"

        # max_sends_per_hour: declared in policy_bounds, never enforced
        # before this worker existed. Not a failure -- just wait for the
        # window to ease, same row, no retry counted against it.
        hour_ago = now - timedelta(hours=1)
        sent_this_hour = session.execute(
            select(func.count()).select_from(OutboundDispatch).where(
                OutboundDispatch.business_id == row.business_id,
                OutboundDispatch.status == "SENT",
                OutboundDispatch.sent_at >= hour_ago,
            )
        ).scalar_one()
        if sent_this_hour >= bounds.max_sends_per_hour:
            row.status = "PENDING"
            row.scheduled_for = now + timedelta(seconds=RATE_LIMIT_DEFER_SECONDS)
            row.locked_until = None
            session.commit()
            return "deferred"

        customer = session.get(Customer, row.customer_id) if row.customer_id else None
        body = (row.payload or {}).get("body", "")

        if row.provider == "twilio" and row.channel == "VOICE":
            to_number = customer.phone_e164 if customer else None
            delivery_status, receipt, cost_minor, provider_message_id = (
                channels.send_twilio_voice(to_number, body) if to_number
                else ("FAILED", {"error": "no phone on file"}, 0, None)
            )
        elif row.provider == "twilio" and row.channel == "WHATSAPP":
            to_number = customer.phone_e164 if customer else None
            delivery_status, receipt, cost_minor, provider_message_id = (
                channels.send_twilio_whatsapp(to_number, body) if to_number
                else ("FAILED", {"error": "no phone on file"}, 0, None)
            )
        elif row.channel == "EMAIL":
            to_address = DEMO_RECIPIENT_EMAIL or "demo-recipient@example.invalid"
            delivery_status, receipt, cost_minor, provider_message_id = channels.send_email(
                to_address, "Action needed on your recent order", body
            )
        else:
            delivery_status, receipt, cost_minor, provider_message_id = channels.send_simulated(rng, row.channel)

        receipt["rendered_body"] = body

        if provider_message_id is not None:
            # Persist the provider's id in its OWN commit, immediately, before
            # any further work can fail: that id is the only evidence a real
            # message already went out, and the guard at the top of this
            # function relies on it surviving a crash. Simulated sends return
            # no id and skip this -- there's no real-world side effect to
            # protect against.
            row.provider_message_id = provider_message_id
            session.commit()

        if delivery_status in ("SENT", "DELIVERED", "LOGGED", "SUBMITTED"):
            row.status = "SENT"
            row.sent_at = now
            row.cost_minor = cost_minor
            if attempt is not None:
                _finalize_attempt(
                    session, attempt, executed=True, delivery_status=delivery_status, channel_receipt=receipt,
                    cost_minor=cost_minor, suppressed_reason=None, now=now,
                )
                if contact is not None:
                    contact.last_contacted_at = now
            session.commit()
            return "sent"

        # Anything else is a send failure -- retryable or permanent.
        row.delivery_tries += 1
        row.last_error = str(receipt.get("error") or delivery_status)

        permanent = delivery_status in _PERMANENT_STATUSES or row.delivery_tries >= row.max_delivery_tries
        if permanent:
            row.status = "FAILED"
            if attempt is not None:
                _finalize_attempt(
                    session, attempt, executed=False, delivery_status=delivery_status, channel_receipt=None,
                    cost_minor=0, suppressed_reason=f"dispatch_failed_{delivery_status.lower()}", now=now,
                )
            session.commit()
            return "failed"

        backoff = RETRY_BACKOFF_SECONDS[min(row.delivery_tries - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        row.status = "PENDING"
        row.scheduled_for = now + timedelta(seconds=backoff)
        row.locked_until = None
        session.commit()
        return "retrying"
    finally:
        session.close()


def drain(max_batches: int | None = None, *, seed: int = 42, business_id: uuid.UUID | None = None) -> dict[str, int]:
    """Claim and process batches until none remain. Returns outcome counts."""
    rng = random.Random(seed)
    counts = {"sent": 0, "suppressed": 0, "deferred": 0, "retrying": 0, "failed": 0, "errored": 0, "batches": 0}
    while max_batches is None or counts["batches"] < max_batches:
        ids = _claim_batch(business_id)
        if not ids:
            break
        for dispatch_id in ids:
            try:
                counts[_process_one(dispatch_id, rng)] += 1
            except Exception:  # noqa: BLE001 -- one poisoned row must not abort the whole drain
                # The row stays IN_FLIGHT and its locked_until lease expires
                # on its own, so a later pass reclaims it; that is the same
                # recovery app/worker.py relies on inbound. What must not
                # happen is the rest of this batch (and the caller's
                # launch_recovery request) dying with it.
                logger.exception("dispatch %s failed to process", dispatch_id)
                counts["errored"] += 1
        counts["batches"] += 1
    return counts


def run_forever(poll_interval: float = 2.0) -> None:
    while True:
        stats = drain()
        if stats["batches"] == 0:
            time.sleep(poll_interval)


if __name__ == "__main__":
    run_forever()
