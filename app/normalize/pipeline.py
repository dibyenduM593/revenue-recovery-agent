"""Orchestrates structural -> typing -> units for one raw event.

Only stages 1-3 exist so far. Semantic mapping (provider vocab -> canonical
EventType/FailureReason) and the write into payments/revenue_events are a
separate stage that does not exist yet; a NormalizedRecord is this
pipeline's final product for now, held in memory and not persisted, since
it is cheap to recompute from raw_events.payload and there is nowhere
correct to put it until semantic classification exists.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonical.money import Money
from app.models import FieldMapping, RawEvent
from app.normalize import structural
from app.normalize import typing as typing_stage
from app.normalize import units


@dataclass
class NormalizedRecord:
    event_type_raw: str
    occurred_at: datetime
    provider_entity_id: str
    money: Money | None
    status_raw: str | None
    provider_intent_id: str | None
    provider_subscription_id: str | None
    customer_email: str | None
    customer_phone: str | None
    failure_code_raw: str | None


def _load_field_mappings(session: Session, business_id: uuid.UUID, source_provider: str) -> list[FieldMapping]:
    stmt = select(FieldMapping).where(
        FieldMapping.business_id == business_id,
        FieldMapping.source_provider == source_provider,
        FieldMapping.approved.is_(True),
    )
    return list(session.scalars(stmt))


def normalize_raw_event(session: Session, raw_event: RawEvent) -> NormalizedRecord:
    mappings = _load_field_mappings(session, raw_event.business_id, raw_event.source_provider)
    extracted = structural.apply_mapping(raw_event.payload, mappings)
    typed = typing_stage.coerce(extracted)
    money = units.to_money(typed)

    return NormalizedRecord(
        event_type_raw=typed["event_type_raw"],
        occurred_at=typed["occurred_at"],
        provider_entity_id=typed["provider_entity_id"],
        money=money,
        status_raw=typed.get("status_raw"),
        provider_intent_id=typed.get("provider_intent_id"),
        provider_subscription_id=typed.get("provider_subscription_id"),
        customer_email=typed.get("customer_email"),
        customer_phone=typed.get("customer_phone"),
        failure_code_raw=typed.get("failure_code_raw"),
    )
