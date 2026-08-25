"""SKIP LOCKED worker that drains raw_events through the normalize pipeline.

Claiming and processing are separate transactions on purpose. Claiming
holds the row lock only long enough to flip processing_status and set
locked_until, then commits immediately. Processing happens afterwards,
without holding a lock, and locked_until is what protects a claimed row
from being picked up again while it is still in flight, so a worker that
dies mid-event lets a later pass reclaim it once locked_until has passed.
That separation does not pay for itself today, when a stage is fast
in-memory work, but it is the shape this loop needs once later stages call
out to real channels (see the plan's Day 10 recovery channels).
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update

from app.canonical.vocabulary import ProcessingStatus
from app.db import SessionLocal
from app.models import DeadLetterEvent, RawEvent
from app.normalize.pipeline import normalize_raw_event
from app.normalize.structural import NormalizationError

BATCH_SIZE = 20
LOCK_TIMEOUT = timedelta(minutes=5)
MAX_ATTEMPTS = 5


def _claim_batch() -> list[uuid.UUID]:
    now = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        stmt = (
            select(RawEvent.raw_event_id)
            .where(
                or_(
                    RawEvent.processing_status == ProcessingStatus.PENDING.value,
                    and_(
                        RawEvent.processing_status == ProcessingStatus.PROCESSING.value,
                        RawEvent.locked_until < now,
                    ),
                )
            )
            .order_by(RawEvent.received_at)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        ids = list(session.scalars(stmt))
        if ids:
            session.execute(
                update(RawEvent)
                .where(RawEvent.raw_event_id.in_(ids))
                .values(processing_status=ProcessingStatus.PROCESSING.value, locked_until=now + LOCK_TIMEOUT)
            )
        session.commit()
        return ids
    finally:
        session.close()


def _process_one(event_id: uuid.UUID) -> str:
    """Returns 'processed', 'requeued', or 'dead_letter'."""
    session = SessionLocal()
    try:
        event = session.get(RawEvent, event_id)
        if event is None:
            return "processed"

        try:
            normalize_raw_event(session, event)
        except NormalizationError as exc:
            event.attempt_count += 1
            event.last_error = exc.message
            session.add(
                DeadLetterEvent(
                    raw_event_id=event.raw_event_id,
                    business_id=event.business_id,
                    stage=exc.stage.value,
                    error_message=exc.message,
                    created_at=datetime.now(timezone.utc),
                )
            )
            if event.attempt_count >= MAX_ATTEMPTS:
                event.processing_status = ProcessingStatus.DEAD_LETTER.value
                outcome = "dead_letter"
            else:
                event.processing_status = ProcessingStatus.PENDING.value
                outcome = "requeued"
            event.locked_until = None
            session.commit()
            return outcome

        event.processing_status = ProcessingStatus.PROCESSED.value
        event.locked_until = None
        session.commit()
        return "processed"
    finally:
        session.close()


def drain(max_batches: int | None = None) -> dict[str, int]:
    """Claim and process batches until none remain. Returns outcome counts."""
    counts = {"processed": 0, "requeued": 0, "dead_letter": 0, "batches": 0}
    while max_batches is None or counts["batches"] < max_batches:
        ids = _claim_batch()
        if not ids:
            break
        for event_id in ids:
            counts[_process_one(event_id)] += 1
        counts["batches"] += 1
    return counts


def run_forever(poll_interval: float = 2.0) -> None:
    while True:
        stats = drain()
        if stats["batches"] == 0:
            time.sleep(poll_interval)


if __name__ == "__main__":
    run_forever()
