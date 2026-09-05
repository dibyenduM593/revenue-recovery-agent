"""Lineage: given an at_risk_id, the full evidence chain behind it.

Every derived row in this system carries raw_event_id (see schema.sql's
design rules), so this is a straight walk: revenue_at_risk -> the
revenue_events row that triggered it (if any -- sweeps have none) -> the
entity row it points to -> the raw_events row underneath that, payload
and all. This is what "click any rupee figure, see the
provider's raw bytes" is built on; today it's a plain endpoint, not yet
wired to an explanation.
"""

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models import CheckoutSession, Invoice, Payment, RawEvent, RevenueAtRisk, RevenueEvent, Subscription

_ENTITY_MODELS: dict[str, tuple[type, str]] = {
    "PAYMENT": (Payment, "payment_id"),
    "CHECKOUT": (CheckoutSession, "checkout_id"),
    "INVOICE": (Invoice, "invoice_id"),
    "SUBSCRIPTION": (Subscription, "subscription_id"),
}


def _row_to_dict(obj) -> dict:
    return {col.name: getattr(obj, col.name) for col in obj.__table__.columns}


def build_lineage(session: Session, at_risk_id: uuid.UUID) -> Optional[dict]:
    at_risk = session.get(RevenueAtRisk, at_risk_id)
    if at_risk is None:
        return None

    entity_dict = None
    model_info = _ENTITY_MODELS.get(at_risk.entity_type)
    if model_info is not None:
        model, _ = model_info
        entity = session.get(model, at_risk.entity_id)
        if entity is not None:
            entity_dict = _row_to_dict(entity)

    source_event_dict = None
    if at_risk.source_event_id is not None:
        source_event = session.get(RevenueEvent, at_risk.source_event_id)
        if source_event is not None:
            source_event_dict = _row_to_dict(source_event)

    raw_event = session.get(RawEvent, at_risk.raw_event_id)
    raw_event_dict = _row_to_dict(raw_event) if raw_event is not None else None

    return {
        "at_risk": _row_to_dict(at_risk),
        "entity": entity_dict,
        "source_revenue_event": source_event_dict,
        "raw_event": raw_event_dict,
    }
