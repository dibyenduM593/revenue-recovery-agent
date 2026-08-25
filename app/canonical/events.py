"""The canonical event: what any provider payload normalizes into.

Field descriptions below are not decoration -- they are read by the LLM
mapping proposer (Day 4+) as the prompt that tells it what a given
source_field in an unfamiliar payload probably maps to. Write them as if
explaining the field to someone who has never seen this payload, because
that is exactly the reader.

This is the semantic-stage output: structural -> typing -> units produce
the raw typed values (app/normalize/{structural,typing,units}.py), and
this model is where they get validated into something a real recovery
decision can be made from. Money and instrument fields are optional
because not every event type carries them (a bare mandate-revocation
signal has no amount; a checkout-started event has no failure code).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.canonical.money import Money
from app.canonical.vocabulary import EventType, FailureReason


class CanonicalEvent(BaseModel):
    event_type: EventType = Field(
        description="What happened: a payment failed or succeeded, a checkout was abandoned, "
        "an invoice went overdue, a subscription charge failed, a mandate was revoked, or a "
        "refund was created."
    )
    occurred_at: datetime = Field(
        description="When this happened according to the provider's own clock, not when we "
        "received the webhook or import row."
    )
    provider_entity_id: str = Field(
        description="The provider's own ID for the primary entity this event is about -- a "
        "payment ID, invoice ID, checkout/session ID, or subscription ID, depending on "
        "event_type."
    )
    money: Optional[Money] = Field(
        default=None,
        description="The amount and currency this event concerns, if any. Absent for events "
        "with no inherent amount, such as a bare mandate revocation.",
    )
    status_raw: Optional[str] = Field(
        default=None,
        description="The provider's own status string for the entity (e.g. Razorpay's "
        "payment.entity.status), kept verbatim alongside event_type rather than replacing it, "
        "since the same status string can mean different things across event types.",
    )
    provider_intent_id: Optional[str] = Field(
        default=None,
        description="The provider's ID for the parent intent this entity belongs to -- an "
        "order ID for a payment, a payment ID for a refund. Used to group retry attempts "
        "under one intent so they are not double-counted as separate losses.",
    )
    provider_subscription_id: Optional[str] = Field(
        default=None,
        description="The provider's subscription ID, when this event is tied to a recurring "
        "billing relationship rather than a one-off payment or checkout.",
    )
    customer_email: Optional[str] = Field(default=None, description="The customer's email, as given by the provider, unnormalized.")
    customer_phone: Optional[str] = Field(default=None, description="The customer's phone number, as given by the provider, unnormalized.")
    failure_reason: Optional[FailureReason] = Field(
        default=None,
        description="The canonical failure reason, after value_mappings translates the "
        "provider's own failure code. Present only when event_type is a failure.",
    )
    failure_code_raw: Optional[str] = Field(
        default=None,
        description="The provider's own failure/error code, verbatim, before translation to "
        "a canonical FailureReason. Kept for audit even after semantic mapping succeeds.",
    )
    error_source: Optional[str] = Field(
        default=None,
        description="Who or what the provider blames for the failure: customer, business, "
        "bank, or gateway. This is the signal that separates a genuine customer-side decline "
        "from a false decline (loss category A5) -- confirm on Day 6 whether Razorpay's "
        "payload actually populates this reliably.",
    )
    error_step: Optional[str] = Field(
        default=None,
        description="Which stage of the payment flow the failure occurred at, e.g. "
        "payment_authentication vs payment_authorization.",
    )
    error_description: Optional[str] = Field(
        default=None, description="The provider's human-readable explanation of the failure, verbatim."
    )
    issuer_bank: Optional[str] = Field(
        default=None,
        description="The customer's card-issuing bank, when the payment method is a card. "
        "Used to detect bank-side degradation (loss category A1): a spike in failures at one "
        "issuer, across many customers, points at the bank rather than any single customer.",
    )
    card_network: Optional[str] = Field(default=None, description="The card network, e.g. visa, mastercard, rupay.")
    card_last4: Optional[str] = Field(default=None, description="The last four digits of the card, for display and instrument matching only.")
    card_expiry_month: Optional[int] = Field(default=None, description="The card's expiry month (1-12), when known.")
    card_expiry_year: Optional[int] = Field(default=None, description="The card's expiry year (4-digit), when known.")
    vpa: Optional[str] = Field(default=None, description="The UPI virtual payment address used, when the payment method is UPI.")
    wallet: Optional[str] = Field(default=None, description="The wallet provider used, when the payment method is a wallet.")
