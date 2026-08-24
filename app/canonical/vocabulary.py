from enum import Enum
from typing import NamedTuple, Optional


class EventType(str, Enum):
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_SUCCEEDED = "PAYMENT_SUCCEEDED"
    CHECKOUT_ABANDONED = "CHECKOUT_ABANDONED"
    INVOICE_OVERDUE = "INVOICE_OVERDUE"
    SUBSCRIPTION_PAYMENT_FAILED = "SUBSCRIPTION_PAYMENT_FAILED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    REFUND_CREATED = "REFUND_CREATED"


class FailureReason(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    ISSUER_UNAVAILABLE = "ISSUER_UNAVAILABLE"
    EXPIRED_CARD = "EXPIRED_CARD"
    DO_NOT_HONOR = "DO_NOT_HONOR"
    STOLEN_CARD = "STOLEN_CARD"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    INVALID_DETAILS = "INVALID_DETAILS"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"


class Backoff(str, Enum):
    PAYDAY_ALIGNED = "payday_aligned"
    EXPONENTIAL_30M = "exponential_30m"
    EXPONENTIAL_15M = "exponential_15m"
    FIXED_24H = "fixed_24h"


class Escalation(str, Enum):
    NONE = "none"
    SMS_THEN_WHATSAPP = "sms_then_whatsapp"
    REQUEST_NEW_INSTRUMENT = "request_new_instrument"
    RECOLLECT_MANDATE = "recollect_mandate"
    HUMAN_REVIEW = "human_review"
    FLAG_NO_CONTACT = "flag_no_contact"


class RetryPolicy(NamedTuple):
    retryable: bool
    max_attempts: int
    backoff: Optional[Backoff]
    escalation: Escalation


RETRY_POLICY: dict[FailureReason, RetryPolicy] = {
    FailureReason.INSUFFICIENT_FUNDS: RetryPolicy(True, 3, Backoff.PAYDAY_ALIGNED, Escalation.SMS_THEN_WHATSAPP),
    FailureReason.ISSUER_UNAVAILABLE: RetryPolicy(True, 4, Backoff.EXPONENTIAL_30M, Escalation.NONE),
    FailureReason.TECHNICAL_ERROR: RetryPolicy(True, 3, Backoff.EXPONENTIAL_15M, Escalation.NONE),
    FailureReason.EXPIRED_CARD: RetryPolicy(False, 0, None, Escalation.REQUEST_NEW_INSTRUMENT),
    FailureReason.MANDATE_REVOKED: RetryPolicy(False, 0, None, Escalation.RECOLLECT_MANDATE),
    FailureReason.DO_NOT_HONOR: RetryPolicy(True, 1, Backoff.FIXED_24H, Escalation.HUMAN_REVIEW),
    FailureReason.INVALID_DETAILS: RetryPolicy(False, 0, None, Escalation.REQUEST_NEW_INSTRUMENT),
    FailureReason.STOLEN_CARD: RetryPolicy(False, 0, None, Escalation.FLAG_NO_CONTACT),
}
