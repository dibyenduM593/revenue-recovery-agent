"""Keeps detecting recovery after "Launch recovery actions" returns,

for as long as the backend process stays up. Three things a single batch
run cannot do on its own, all handled here on a repeating interval, and
all general over EVERY real Twilio-routed call/message the business has
outstanding -- not one hardcoded item. plant()/record_reply() are one
sample path through the same pipeline this sweeps in bulk; it never asks
"is this the demo's at_risk_id," only "is there a real Twilio send we
haven't confirmed yet."

1. A real WhatsApp reply has nowhere to land. app/live_demo.py's inbound
   webhook route exists, but nothing in the Twilio console is configured
   to call it -- that would need a public URL, the exact dependency this
   build has twice now deliberately avoided (see live_demo.py's own
   docstring). So instead of waiting for Twilio to push a reply to us,
   this pulls: list recent messages, find inbound ones not seen before,
   feed each through record_reply() -- the same function the webhook
   would have called, doing the same real ingestion, same attribution.

2. "SENT" in our own outbound_dispatches means Twilio's API accepted the
   request -- it is NOT proof the call ever rang or the WhatsApp message
   ever delivered. This sweeps every dispatch we believe we sent for
   real and asks Twilio directly what actually happened (ringing /
   completed / no-answer / busy / failed for a call; queued / delivered /
   read / failed for a message), writing the verified status onto the
   attempt's own channel_receipt rather than trusting our optimistic
   write at send time.

3. A nudge's attribution window closes on its own schedule, not on a
   button click. Re-running attribution and the nudge-retry pass here
   means a window closing at 3:47am gets picked up at 3:47am, not
   whenever someone next happens to click something.

Started once, from orchestrator.launch_recovery() -- "after I click
launch recovery actions" is deliberately when this begins, not server
boot, so an idle backend with no batch ever launched polls nothing.
"""

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10

# Twilio call/message lifecycle states that will never change again --
# once seen, a dispatch is dropped from future reconcile sweeps instead
# of re-fetching an already-final status from Twilio forever.
_TERMINAL_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
_TERMINAL_MESSAGE_STATUSES = {"delivered", "read", "failed", "undelivered"}

_seen_message_sids: set[str] = set()
_reconciled_terminal: set[uuid.UUID] = set()
_started_for: set[uuid.UUID] = set()
_lock = threading.Lock()


def _poll_twilio_replies(business_id: uuid.UUID) -> None:
    from app.settings import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return  # Twilio not configured -- nothing to poll, not an error

    from app.live_demo import record_reply

    try:
        # Imported here, not above: twilio is an optional dependency, and an
        # ImportError raised outside this try propagated all the way out of
        # _run_loop and killed the thread for good.
        from twilio.rest import Client

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        messages = client.messages.list(limit=20)
    except Exception:  # noqa: BLE001 -- one bad poll must not kill the loop
        logger.exception("live_poll: Twilio message list failed")
        return

    for m in messages:
        if m.direction != "inbound" or m.sid in _seen_message_sids:
            continue
        _seen_message_sids.add(m.sid)
        try:
            record_reply(m.from_, m.body, business_id=business_id, message_time=m.date_sent or m.date_created)
        except Exception:  # noqa: BLE001
            logger.exception("live_poll: record_reply failed for message %s", m.sid)


def _reconcile_twilio_dispatches(business_id: uuid.UUID) -> None:
    """Every real (provider='twilio') dispatch we believe is SENT, checked

    directly against Twilio's own record of it -- general over the whole
    business's outstanding sends, not one at_risk_id. A call SID starts
    with 'CA', a message SID with 'SM' or 'MM'; that prefix, not the
    dispatch's channel column, decides which Twilio API to call, since
    it's the one fact that can't drift out of sync with reality.
    """
    from app.settings import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return

    from app.db import SessionLocal
    from app.models import OutboundDispatch, RecoveryAttempt
    from sqlalchemy import select

    try:
        # Both the import and the client construction: an unusable credential
        # raises here, and outside the try that killed the polling thread.
        from twilio.rest import Client

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:  # noqa: BLE001
        logger.exception("live_poll: Twilio client unavailable for reconcile")
        return

    session = SessionLocal()
    try:
        rows = session.execute(
            select(OutboundDispatch).where(
                OutboundDispatch.business_id == business_id,
                OutboundDispatch.provider == "twilio",
                OutboundDispatch.status == "SENT",
                OutboundDispatch.provider_message_id.is_not(None),
            )
        ).scalars().all()

        for row in rows:
            if row.dispatch_id in _reconciled_terminal:
                continue
            sid = row.provider_message_id
            try:
                if sid.startswith("CA"):
                    call = client.calls(sid).fetch()
                    provider_status = call.status
                    extra = {"twilio_call_status": call.status, "twilio_call_duration_seconds": call.duration}
                    terminal = provider_status in _TERMINAL_CALL_STATUSES
                else:
                    msg = client.messages(sid).fetch()
                    provider_status = msg.status
                    extra = {"twilio_message_status": msg.status, "twilio_error_code": msg.error_code}
                    terminal = provider_status in _TERMINAL_MESSAGE_STATUSES
            except Exception:  # noqa: BLE001 -- one bad fetch must not stop the sweep
                logger.exception("live_poll: Twilio fetch failed for %s (dispatch %s)", sid, row.dispatch_id)
                continue

            attempt = session.get(RecoveryAttempt, row.attempt_id)
            if attempt is not None:
                receipt = dict(attempt.channel_receipt or {})
                if receipt.get("twilio_verified_status") != provider_status:
                    receipt["twilio_verified_status"] = provider_status
                    receipt.update(extra)
                    attempt.channel_receipt = receipt

            if terminal:
                _reconciled_terminal.add(row.dispatch_id)

        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("live_poll: reconcile sweep failed")
        session.rollback()
    finally:
        session.close()


def _sweep_attribution(business_id: uuid.UUID) -> None:
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.recovery.attribution import run_attribution
    from app.recovery.nudge_retry import process_expired_nudges

    # Real wall-clock time, not run_attribution()'s own default (MAX
    # RevenueEvent.occurred_at across the whole business). That default
    # exists for the pure-synthetic batch simulator, which has no real
    # "now" -- but the bulk simulator plants B2B INVOICE_PAID events dated
    # months out as part of its normal late-invoice modeling, so reusing
    # that default here would compare a live item's real attribution
    # window against a "now" that's already months in the future, marking
    # it EXPIRED before any real reply had a chance to land.
    now = datetime.now(timezone.utc)

    session = SessionLocal()
    try:
        run_attribution(session, business_id, clock=now)
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("live_poll: run_attribution failed")
        session.rollback()
    finally:
        session.close()

    session = SessionLocal()
    try:
        process_expired_nudges(session, business_id)
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("live_poll: process_expired_nudges failed")
        session.rollback()
    finally:
        session.close()


def _run_loop(business_id: uuid.UUID) -> None:
    """One bad tick must never end the loop, and if the loop does end, say so.

    Every step below already logs and swallows its own failures, but a
    surprise raised between them (or by an optional import) used to unwind
    straight out of the thread while business_id stayed in _started_for --
    so start() kept answering "already running" for a thread that was gone,
    and no real WhatsApp reply was ever detected again. The outer guard
    keeps ticking, and the finally releases the slot so ensure_live_poll()
    can genuinely restart it.
    """
    try:
        while True:
            try:
                _poll_twilio_replies(business_id)
                _reconcile_twilio_dispatches(business_id)
                _sweep_attribution(business_id)
            except Exception:  # noqa: BLE001 -- a tick is best-effort, the loop is not
                logger.exception("live_poll: tick failed for business %s", business_id)
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        with _lock:
            _started_for.discard(business_id)


def start(business_id: uuid.UUID) -> bool:
    """Idempotent: a second call for the same business is a no-op, so

    clicking "Launch recovery actions" again doesn't stack up duplicate
    background threads. Returns whether a new thread was actually started.
    """
    with _lock:
        if business_id in _started_for:
            return False
        _started_for.add(business_id)
    thread = threading.Thread(target=_run_loop, args=(business_id,), name=f"live-poll-{business_id}", daemon=True)
    thread.start()
    return True
