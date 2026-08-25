import json
import uuid
from datetime import datetime

from fastapi import APIRouter, UploadFile

from app.db import SessionLocal
from app.ingest.idempotency import insert_raw_event

router = APIRouter()


@router.post("/v1/imports")
async def import_events(file: UploadFile):
    """Upload a JSONL file of raw provider events into raw_events.

    Each line is one event: business_id, source_provider, source_type,
    external_event_id, occurred_at, payload -- the same shape seed/generator.py
    writes, and the same shape a webhook handler will build once it exists.
    """
    body = (await file.read()).decode("utf-8")

    received = inserted = duplicates = 0
    session = SessionLocal()
    try:
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            received += 1

            was_new = insert_raw_event(
                session,
                business_id=uuid.UUID(record["business_id"]),
                source_provider=record["source_provider"],
                source_type=record["source_type"],
                external_event_id=record.get("external_event_id"),
                payload=record["payload"],
                occurred_at=datetime.fromisoformat(record["occurred_at"]),
                headers=record.get("headers"),
                ingestion_method="JSON",
            )
            if was_new:
                inserted += 1
            else:
                duplicates += 1

        session.commit()
    finally:
        session.close()

    return {"received": received, "inserted": inserted, "duplicates": duplicates}
