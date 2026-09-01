"""Stage 4: semantic mapping -- provider vocabulary -> canonical values.

Looks up event_type_raw, status_raw, and failure_code_raw against
value_mappings. An unrecognized raw value is a NormalizationError, not a
silent pass-through or a guess -- the same never-fall-back-to-identity
discipline transforms.py's whitelist already enforces at the structural
stage. Unlike field_mappings (which SELECT already filters to approved
rows), value_mappings is looked up per-value here since the value space
is open-ended (a new failure code can show up in any payload), so an
unapproved or missing row has to fail loudly rather than being cached
away at pipeline start.
"""

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonical.vocabulary import EventType, FailureReason, NormalizationStage
from app.models import ValueMapping
from app.normalize.structural import NormalizationError

# Which canonical_field in value_mappings carries an entity kind's status,
# for entity kinds where the provider signals status via a distinct field
# rather than only through event_type_raw.
STATUS_CANONICAL_FIELD: dict[str, str] = {
    "payment": "payment_status",
    "invoice": "invoice_status",
}


def resolve_entity_kind(matched_source_objects: set[str]) -> str:
    candidates = matched_source_objects - {"envelope"}
    if len(candidates) != 1:
        raise NormalizationError(
            NormalizationStage.SEMANTIC,
            f"expected exactly one entity kind to match, got {sorted(candidates)}",
        )
    return candidates.pop()


def _lookup(session: Session, provider: str, canonical_field: str, source_value: str) -> str:
    stmt = select(ValueMapping.canonical_value).where(
        ValueMapping.provider == provider,
        ValueMapping.canonical_field == canonical_field,
        ValueMapping.source_value == source_value,
        ValueMapping.approved.is_(True),
    )
    canonical_value = session.scalars(stmt).first()
    if canonical_value is None:
        raise NormalizationError(
            NormalizationStage.SEMANTIC,
            f"no approved value_mapping for {canonical_field}={source_value!r} (provider={provider!r})",
        )
    return canonical_value


@dataclass
class SemanticResult:
    entity_kind: str
    event_type: EventType
    failure_reason: Optional[FailureReason]
    status: Optional[str]  # canonical status string for entity kinds that carry one; None for checkout


def classify(session: Session, provider: str, matched_source_objects: set[str], typed: dict[str, Any]) -> SemanticResult:
    entity_kind = resolve_entity_kind(matched_source_objects)

    raw_event_type = typed["event_type_raw"]
    event_type = EventType(_lookup(session, provider, "event_type", raw_event_type))

    failure_reason = None
    if typed.get("failure_code_raw"):
        failure_reason = FailureReason(_lookup(session, provider, "failure_reason", typed["failure_code_raw"]))

    status = None
    status_field = STATUS_CANONICAL_FIELD.get(entity_kind)
    if status_field and typed.get("status_raw"):
        status = _lookup(session, provider, status_field, typed["status_raw"])

    return SemanticResult(entity_kind=entity_kind, event_type=event_type, failure_reason=failure_reason, status=status)
