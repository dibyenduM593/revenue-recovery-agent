"""Stage 5: Pydantic validation.

Assembles CanonicalEvent from the typed dict (structural -> coercion ->
units) plus the semantic stage's resolved event_type/failure_reason.
Pydantic enforces the required fields and types CanonicalEvent declares;
a ValidationError here means the earlier stages produced something that
looks superficially right but violates the canonical shape (e.g. a field
of the wrong type slipping through a loosely-typed transform).
"""

from pydantic import ValidationError

from app.canonical.events import CanonicalEvent
from app.canonical.vocabulary import NormalizationStage
from app.normalize.semantic import SemanticResult
from app.normalize.structural import NormalizationError


def build_canonical_event(typed: dict, semantic: SemanticResult, money) -> CanonicalEvent:
    try:
        return CanonicalEvent(
            event_type=semantic.event_type,
            occurred_at=typed["occurred_at"],
            provider_entity_id=typed["provider_entity_id"],
            money=money,
            status_raw=typed.get("status_raw"),
            payment_method=typed.get("payment_method"),
            provider_intent_id=typed.get("provider_intent_id"),
            provider_subscription_id=typed.get("provider_subscription_id"),
            customer_email=typed.get("customer_email"),
            customer_phone=typed.get("customer_phone"),
            failure_reason=semantic.failure_reason,
            failure_code_raw=typed.get("failure_code_raw"),
            error_source=typed.get("error_source"),
            error_step=typed.get("error_step"),
            error_description=typed.get("error_description"),
            issuer_bank=typed.get("issuer_bank"),
            card_network=typed.get("card_network"),
            card_last4=typed.get("card_last4"),
            card_expiry_month=typed.get("card_expiry_month"),
            card_expiry_year=typed.get("card_expiry_year"),
            vpa=typed.get("vpa"),
            wallet=typed.get("wallet"),
            due_at=typed.get("due_at"),
            recovery_token_raw=typed.get("recovery_token_raw"),
        )
    except ValidationError as exc:
        raise NormalizationError(NormalizationStage.VALIDATION, str(exc)) from exc
