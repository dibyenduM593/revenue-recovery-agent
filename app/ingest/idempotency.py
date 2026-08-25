import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import RawEvent


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def insert_raw_event(
    session: Session,
    *,
    business_id: uuid.UUID,
    source_provider: str,
    source_type: str,
    external_event_id: str | None,
    payload: dict[str, Any],
    occurred_at: datetime | None,
    ingestion_method: str,
    headers: dict[str, Any] | None = None,
) -> bool:
    """Insert a raw event, skipping it if already seen.

    Dedup is enforced by the raw_events unique constraints (external_event_id
    per business+provider, and payload_hash per business+provider as a
    fallback), not by an application-side lookup, so it holds under
    concurrent webhook deliveries. Returns True if a new row was inserted,
    False if this was a duplicate.
    """
    stmt = (
        pg_insert(RawEvent)
        .values(
            raw_event_id=uuid.uuid4(),
            business_id=business_id,
            source_provider=source_provider,
            source_type=source_type,
            external_event_id=external_event_id,
            payload_hash=payload_hash(payload),
            payload=payload,
            headers=headers,
            occurred_at=occurred_at,
            received_at=datetime.now(timezone.utc),
            ingestion_method=ingestion_method,
        )
        .on_conflict_do_nothing()
        .returning(RawEvent.raw_event_id)
    )
    # rowcount is unreliable for ON CONFLICT DO NOTHING with psycopg3; RETURNING
    # only yields a row when the insert actually happened, so that's the signal.
    return session.execute(stmt).first() is not None
