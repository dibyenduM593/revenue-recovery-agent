from enum import Enum
from typing import NamedTuple, Optional


class EventType(str, Enum):
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_SUCCEEDED = "PAYMENT_SUCCEEDED"
    CHECKOUT_STARTED = "CHECKOUT_STARTED"
    CHECKOUT_COMPLETED = "CHECKOUT_COMPLETED"
    CHECKOUT_ABANDONED = "CHECKOUT_ABANDONED"  # derived by the sweep job; never a raw event_type_raw value
    INVOICE_ISSUED = "INVOICE_ISSUED"
    INVOICE_PAID = "INVOICE_PAID"
    INVOICE_OVERDUE = "INVOICE_OVERDUE"
    SUBSCRIPTION_PAYMENT_FAILED = "SUBSCRIPTION_PAYMENT_FAILED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    REFUND_CREATED = "REFUND_CREATED"
    ORDER_CREATED = "ORDER_CREATED"
    DISPUTE_CREATED = "DISPUTE_CREATED"


class FailureReason(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    ISSUER_UNAVAILABLE = "ISSUER_UNAVAILABLE"
    EXPIRED_CARD = "EXPIRED_CARD"
    DO_NOT_HONOR = "DO_NOT_HONOR"
    STOLEN_CARD = "STOLEN_CARD"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    INVALID_DETAILS = "INVALID_DETAILS"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"
    ATTENTION_SLIP = "ATTENTION_SLIP"  # wrong PIN/OTP entry -- synchronous, mid-checkout


class Backoff(str, Enum):
    PAYDAY_ALIGNED = "payday_aligned"
    EXPONENTIAL_30M = "exponential_30m"
    EXPONENTIAL_15M = "exponential_15m"
    FIXED_24H = "fixed_24h"


class Action(str, Enum):
    """Matches recovery_attempts.strategy's CHECK constraint in schema.sql."""

    RETRY_NOW = "RETRY_NOW"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    NUDGE = "NUDGE"
    REQUEST_NEW_INSTRUMENT = "REQUEST_NEW_INSTRUMENT"
    RECOLLECT_MANDATE = "RECOLLECT_MANDATE"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    OPS_ALERT = "OPS_ALERT"
    STOP = "STOP"


class LossCategory(str, Enum):
    """WHAT kind of revenue loss this is (research A1..B4, X_*).

    A2 (settlement-freeze), A3 (reconciliation leakage), and A4 (checkout-
    friction cause analysis) are stated non-goals -- Razorpay exposes no
    webhook or API for any of them -- so they are deliberately absent here
    even though schema.sql's header comment lists the full A1..A7 range.
    """

    A1 = "A1"  # issuer/bank-side technical degradation
    A5 = "A5"  # false decline: gateway/bank declined a payment that should have gone through
    A6 = "A6"  # gateway/platform-side technical failure, not tied to a specific issuer
    A7 = "A7"  # reserved: no FailureReason maps here yet, pending real-payload findings
    B1 = "B1"  # checkout abandoned -- nudge to complete, token-attributed
    B2 = "B2"  # subscription mandate lapsed -- nudge to re-authorize, event-attributed
    B3 = "B3"  # payment instrument issue -- nudge to update, token-attributed
    B4 = "B4"  # invoice/receivable overdue -- nudge to pay, event-attributed (B2B, long window)
    B5 = "B5"  # attention-slip (wrong PIN/OTP) -- proves the decision NOT to nudge: async contact hurts, not helps
    X_INSUFFICIENT_FUNDS = "X_INSUFFICIENT_FUNDS"
    X_FRAUD = "X_FRAUD"
    X_BANK_BLOCK = "X_BANK_BLOCK"  # reserved: no FailureReason maps here yet


class FaultAttribution(str, Enum):
    BUSINESS_FAULT = "BUSINESS_FAULT"
    CUSTOMER_SENTIMENT = "CUSTOMER_SENTIMENT"
    CUSTOMER_CIRCUMSTANCE = "CUSTOMER_CIRCUMSTANCE"
    EXTERNAL = "EXTERNAL"


class FailureTaxonomyEntry(NamedTuple):
    loss_category: LossCategory
    fault_attribution: FaultAttribution
    claimable: bool
    retryable: bool
    max_attempts: int
    backoff: Optional[Backoff]
    default_action: Action
    escalation_action: Optional[Action]
    attribution_window_seconds: int
    notes: str


# The taxonomy IS the product: reason -> recoverability -> action -> bound.
# Mirrors schema.sql's failure_taxonomy table 1:1 -- this dict is that
# table's seed data. claimable/loss_category assignments below are a
# best-effort reading of ambiguous cases (A5, A6, and the B1-B4 split are
# not clean-cut); confirm before Gate A, since "if money is wrong, stop
# everything."
FAILURE_TAXONOMY: dict[FailureReason, FailureTaxonomyEntry] = {
    FailureReason.INSUFFICIENT_FUNDS: FailureTaxonomyEntry(
        loss_category=LossCategory.X_INSUFFICIENT_FUNDS,
        fault_attribution=FaultAttribution.CUSTOMER_CIRCUMSTANCE,
        claimable=False,
        retryable=True,
        max_attempts=3,
        backoff=Backoff.PAYDAY_ALIGNED,
        default_action=Action.RETRY_SCHEDULED,
        escalation_action=Action.NUDGE,
        attribution_window_seconds=7 * 86400,
        notes="Segment-reported only -- never enters the claimable headline number.",
    ),
    FailureReason.ISSUER_UNAVAILABLE: FailureTaxonomyEntry(
        loss_category=LossCategory.A1,
        fault_attribution=FaultAttribution.BUSINESS_FAULT,
        claimable=True,
        retryable=True,
        max_attempts=4,
        backoff=Backoff.EXPONENTIAL_30M,
        default_action=Action.RETRY_SCHEDULED,
        escalation_action=None,
        attribution_window_seconds=30 * 60,
        notes="Bank/issuer-side technical degradation; a retry lands within minutes or not at all.",
    ),
    FailureReason.TECHNICAL_ERROR: FailureTaxonomyEntry(
        loss_category=LossCategory.A6,
        fault_attribution=FaultAttribution.BUSINESS_FAULT,
        claimable=True,
        retryable=True,
        max_attempts=3,
        backoff=Backoff.EXPONENTIAL_15M,
        default_action=Action.RETRY_SCHEDULED,
        escalation_action=None,
        attribution_window_seconds=30 * 60,
        notes="Gateway/platform-side technical failure, not tied to a specific issuing bank (that's A1).",
    ),
    FailureReason.EXPIRED_CARD: FailureTaxonomyEntry(
        loss_category=LossCategory.B3,
        fault_attribution=FaultAttribution.CUSTOMER_SENTIMENT,
        claimable=True,
        retryable=False,
        max_attempts=0,
        backoff=None,
        default_action=Action.REQUEST_NEW_INSTRUMENT,
        escalation_action=None,  # non-retryable: default_action IS the only response, nothing to escalate to
        attribution_window_seconds=72 * 3600,
        notes="Token-based nudge to a payment link for updating the instrument.",
    ),
    FailureReason.MANDATE_REVOKED: FailureTaxonomyEntry(
        loss_category=LossCategory.B2,
        fault_attribution=FaultAttribution.CUSTOMER_SENTIMENT,
        claimable=True,
        retryable=False,
        max_attempts=0,
        backoff=None,
        default_action=Action.RECOLLECT_MANDATE,
        escalation_action=None,  # non-retryable: default_action IS the only response, nothing to escalate to
        attribution_window_seconds=7 * 86400,
        notes="Subscription re-authorization; attributed via the next SUBSCRIPTION_CHARGED event, not a token click.",
    ),
    FailureReason.DO_NOT_HONOR: FailureTaxonomyEntry(
        loss_category=LossCategory.A5,
        fault_attribution=FaultAttribution.BUSINESS_FAULT,
        claimable=True,
        retryable=True,
        max_attempts=1,
        backoff=Backoff.FIXED_24H,
        default_action=Action.RETRY_SCHEDULED,
        escalation_action=Action.ESCALATE_HUMAN,
        attribution_window_seconds=24 * 3600,
        notes="Generic issuer decline -- may be a false decline (A5) rather than genuine customer "
        "fault. Whether error_source actually separates the two is an open question.",
    ),
    FailureReason.INVALID_DETAILS: FailureTaxonomyEntry(
        loss_category=LossCategory.B3,
        fault_attribution=FaultAttribution.CUSTOMER_SENTIMENT,
        claimable=True,
        retryable=False,
        max_attempts=0,
        backoff=None,
        default_action=Action.REQUEST_NEW_INSTRUMENT,
        escalation_action=None,
        attribution_window_seconds=72 * 3600,
        notes="Token-based nudge to correct payment instrument details.",
    ),
    FailureReason.ATTENTION_SLIP: FailureTaxonomyEntry(
        loss_category=LossCategory.B5,
        fault_attribution=FaultAttribution.CUSTOMER_SENTIMENT,
        claimable=True,
        retryable=False,
        max_attempts=0,
        backoff=None,
        default_action=Action.STOP,
        escalation_action=None,
        attribution_window_seconds=0,
        notes="A wrong PIN/OTP is synchronous and mid-session -- the customer is already retrying it "
        "themselves in the checkout flow. An async nudge later doesn't help and reads as pestering; "
        "the correct policy response is to record the loss and do nothing.",
    ),
    FailureReason.STOLEN_CARD: FailureTaxonomyEntry(
        loss_category=LossCategory.X_FRAUD,
        fault_attribution=FaultAttribution.EXTERNAL,
        claimable=False,
        retryable=False,
        max_attempts=0,
        backoff=None,
        default_action=Action.OPS_ALERT,  # the response IS "alert ops", not a customer-facing action at all
        escalation_action=None,
        attribution_window_seconds=0,
        notes="Fraud signal -- never contact the customer. Excluded from every recovery number.",
    ),
}


class PaymentStatus(str, Enum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class SubscriptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELLED = "CANCELLED"


class MandateStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class InvoiceStatus(str, Enum):
    ISSUED = "ISSUED"
    OVERDUE = "OVERDUE"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    WRITTEN_OFF = "WRITTEN_OFF"


class ProcessingStatus(str, Enum):
    """Matches raw_events.processing_status's CHECK constraint in schema.sql."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    DEAD = "DEAD"


class NormalizationStage(str, Enum):
    STRUCTURAL = "STRUCTURAL"
    TYPING = "TYPING"
    UNITS = "UNITS"
    SEMANTIC = "SEMANTIC"
    VALIDATION = "VALIDATION"
    PERSIST = "PERSIST"


class EntityType(str, Enum):
    PAYMENT = "PAYMENT"
    CHECKOUT = "CHECKOUT"
    INVOICE = "INVOICE"
    SUBSCRIPTION = "SUBSCRIPTION"


class Cohort(str, Enum):
    TREATMENT = "TREATMENT"
    HOLDOUT = "HOLDOUT"


# revenue_at_risk.status values that mean "not finished with this yet" --
# detected but undecided (OPEN), or nudged and awaiting a response
# (IN_RECOVERY). Named once because the pair was previously spelled out
# as a literal at every call site, and a copy that said only "OPEN"
# silently stopped real WhatsApp replies from ever matching their nudge.
UNRESOLVED_STATUSES = ("OPEN", "IN_RECOVERY")


