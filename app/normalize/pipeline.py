"""Orchestrates all six normalization stages for one raw event:

structural -> typing -> units -> semantic -> validation -> upsert

Each stage's output feeds the next; any stage can raise NormalizationError,
which the worker turns into a dead-letter row and a retry rather than a
partial write, since nothing here is persisted until the upsert stage
succeeds and the caller commits.

Immediately after upsert, a payment.failed or subscription mandate
revocation is also a direct loss signal, so those two cases go straight
into risk detection (app/risk/detect.py) in the same transaction --
checkout abandonment and invoice overdue have no such signal and are
handled by the separate sweep jobs (app/risk/sweeps.py) instead.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonical.events import CanonicalEvent
from app.canonical.vocabulary import EventType
from app.models import FieldMapping, RawEvent
from app.normalize import structural, upsert
from app.normalize import typing as typing_stage
from app.normalize import units
from app.normalize.semantic import SemanticResult, classify
from app.normalize.unmapped import track_unmapped_fields
from app.normalize.validation import build_canonical_event
from app.risk.detect import on_mandate_revoked, on_payment_failed, on_payment_succeeded


def _load_field_mappings(session: Session, provider: str) -> list[FieldMapping]:
    """field_mappings is global per provider (see schema.sql), not per-business:

    the shape of a Razorpay payload does not vary by which business receives it.
    """
    stmt = select(FieldMapping).where(
        FieldMapping.provider == provider,
        FieldMapping.approved.is_(True),
    )
    return list(session.scalars(stmt))


def _mapping_version(mappings: list[FieldMapping]) -> str:
    versions = {m.version for m in mappings}
    return versions.pop() if len(versions) == 1 else "mixed"


def normalize_raw_event(session: Session, raw_event: RawEvent) -> uuid.UUID:
    """Returns the internal id of the entity row written or updated."""
    provider = raw_event.source_provider
    mappings = _load_field_mappings(session, provider)

    extracted, matched_objects = structural.apply_mapping(raw_event.payload, mappings)
    track_unmapped_fields(session, provider, raw_event.payload, mappings)

    typed = typing_stage.coerce(extracted)
    money = units.to_money(typed)
    semantic_result: SemanticResult = classify(session, provider, matched_objects, typed)
    canonical_event: CanonicalEvent = build_canonical_event(typed, semantic_result, money)

    result = upsert.apply(
        session,
        raw_event_id=raw_event.raw_event_id,
        business_id=raw_event.business_id,
        provider=provider,
        semantic=semantic_result,
        event=canonical_event,
        mapping_version=_mapping_version(mappings),
    )
    # SessionLocal has autoflush=False, so the payment/revenue_event rows
    # upsert.apply() just added are not yet visible to risk detection's own
    # SELECT queries (notably _intent_entity_id, which looks up attempt #1 --
    # for a single-attempt failure that IS the row just added) without this.
    session.flush()

    if semantic_result.event_type == EventType.PAYMENT_FAILED and canonical_event.failure_reason is not None:
        on_payment_failed(
            session,
            business_id=raw_event.business_id,
            customer_id=result.customer_id,
            payment_intent_id=result.payment_intent_id,
            failure_reason=canonical_event.failure_reason,
            at_risk_minor=canonical_event.money.amount_minor,
            currency=canonical_event.money.currency,
            source_event_id=result.revenue_event_id,
            raw_event_id=raw_event.raw_event_id,
        )
    elif semantic_result.event_type == EventType.PAYMENT_SUCCEEDED and result.payment_intent_id:
        on_payment_succeeded(
            session, business_id=raw_event.business_id,
            payment_intent_id=result.payment_intent_id, occurred_at=canonical_event.occurred_at,
        )
    elif semantic_result.event_type == EventType.MANDATE_REVOKED:
        on_mandate_revoked(
            session,
            business_id=raw_event.business_id,
            customer_id=result.customer_id,
            subscription_id=result.entity_id,
            at_risk_minor=canonical_event.money.amount_minor,
            currency=canonical_event.money.currency,
            source_event_id=result.revenue_event_id,
            raw_event_id=raw_event.raw_event_id,
        )

    return result.entity_id
