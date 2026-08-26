"""POST /v1/webhooks/{provider} -- HMAC-verified webhook intake.

The signature is checked over the RAW request bytes before any JSON
parsing happens. That ordering matters: verifying against a re-serialized
`json.dumps(json.loads(body))` would accept a payload whose byte-level
encoding differs from what the provider actually signed (key order,
whitespace, unicode escaping), which is exactly the gap a forged-but-
reformatted payload would exploit.

Beyond the signature, a payload's own `created_at` is checked against a
freshness window: a valid signature only proves the bytes were not
tampered with, not that they are being delivered for the first time. Both
checks happen before the row is written, and after that the handler does
one thing -- insert into raw_events, relying on its DB-level uniqueness
constraints for idempotency -- so it can return 200 fast and leave all
normalization work to the worker.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.db import SessionLocal
from app.ingest.idempotency import insert_raw_event
from app.settings import DEMO_BUSINESS_ID, RAZORPAY_WEBHOOK_SECRET

router = APIRouter()

TIMESTAMP_WINDOW = timedelta(minutes=5)
SOURCE_TYPE = "webhook"


@dataclass(frozen=True)
class ProviderWebhookConfig:
    secret: str
    signature_header: str
    event_id_header: str | None = None


WEBHOOK_CONFIG: dict[str, ProviderWebhookConfig] = {}
if RAZORPAY_WEBHOOK_SECRET:
    WEBHOOK_CONFIG["razorpay"] = ProviderWebhookConfig(
        secret=RAZORPAY_WEBHOOK_SECRET,
        signature_header="X-Razorpay-Signature",
        event_id_header="X-Razorpay-Event-Id",
    )


class WebhookRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> None:
    if not signature:
        raise WebhookRejected(401, "missing signature header")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookRejected(401, "signature verification failed")


def check_timestamp_window(payload: dict[str, Any], *, now: datetime | None = None) -> None:
    """Reject a payload whose own created_at is outside TIMESTAMP_WINDOW of now.

    A correct signature only proves the payload was not altered in transit,
    not that it is fresh -- without this, a captured payload replayed hours
    or days later would sail through signature verification and be ingested
    as if it just happened.
    """
    created_at = payload.get("created_at")
    if created_at is None:
        return
    occurred_at = datetime.fromtimestamp(int(created_at), tz=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if abs(now - occurred_at) > TIMESTAMP_WINDOW:
        raise WebhookRejected(400, "timestamp outside acceptable window")


@router.post("/v1/webhooks/{provider}")
async def receive_webhook(provider: str, request: Request):
    config = WEBHOOK_CONFIG.get(provider)
    if config is None:
        raise HTTPException(status_code=404, detail=f"no webhook configured for provider {provider!r}")

    raw_body = await request.body()
    signature = request.headers.get(config.signature_header)

    try:
        verify_signature(raw_body, signature, config.secret)
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise WebhookRejected(400, "payload must be a JSON object")
        check_timestamp_window(payload)
    except WebhookRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    occurred_at = None
    if payload.get("created_at") is not None:
        occurred_at = datetime.fromtimestamp(int(payload["created_at"]), tz=timezone.utc)

    external_event_id = request.headers.get(config.event_id_header) if config.event_id_header else None

    session = SessionLocal()
    try:
        was_new = insert_raw_event(
            session,
            business_id=DEMO_BUSINESS_ID,
            source_provider=provider,
            source_type=SOURCE_TYPE,
            external_event_id=external_event_id,
            payload=payload,
            occurred_at=occurred_at,
            headers=dict(request.headers),
            ingestion_method="WEBHOOK",
        )
        session.commit()
    finally:
        session.close()

    return {"received": True, "inserted": was_new}
