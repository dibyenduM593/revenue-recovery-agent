"""Keeps detecting recovery after "Launch recovery actions" returns,

for as long as the backend process stays up. Two things a single batch
run cannot do on its own, both handled here on a repeating interval:

1. A real WhatsApp reply has nowhere to land. app/live_demo.py's inbound
   webhook route exists, but nothing in the Twilio console is configured
   to call it -- that would need a public URL, the exact dependency this
   build has twice now deliberately avoided (see live_demo.py's own
   docstring). So instead of waiting for Twilio to push a reply to us,
   this pulls: list recent messages, find inbound ones not seen before,
   feed each through record_reply() -- the same function the webhook
   would have called, doing the same real ingestion, same attribution.

2. A nudge's attribution window closes on its own schedule, not on a
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

POLL_INTERVAL_SECONDS = 5

_seen_message_sids: set[str] = set()
_started_for: set[uuid.UUID] = set()
_lock = threading.Lock()


def _poll_twilio_replies(business_id: uuid.UUID) -> None:
    from app.settings import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return  # Twilio not configured -- nothing to poll, not an error

    from twilio.rest import Client

    from app.live_demo import record_reply

    try:
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
            record_reply(m.from_, m.body, business_id=business_id)
        except Exception:  # noqa: BLE001
            logger.exception("live_poll: record_reply failed for message %s", m.sid)


def _sweep_attribution(business_id: uuid.UUID) -> None:
    from app.db import SessionLocal
    from app.recovery.attribution import run_attribution
    from app.recovery.nudge_retry import process_expired_nudges

    session = SessionLocal()
    try:
        run_attribution(session, business_id)
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
    while True:
        _poll_twilio_replies(business_id)
        _sweep_attribution(business_id)
        time.sleep(POLL_INTERVAL_SECONDS)


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


def is_running(business_id: uuid.UUID) -> bool:
    return business_id in _started_for
