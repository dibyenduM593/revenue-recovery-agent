import pytest

from app.canonical.vocabulary import Action, EntityType, FailureReason, MandateStatus
from app.policy import HUMAN_REVIEW_THRESHOLD_MINOR, decide

SMALL_VALUE = 10_000  # well under the human-review threshold


def test_retryable_within_max_attempts_uses_default_action():
    # ISSUER_UNAVAILABLE: retryable, max_attempts=4, default_action=RETRY_SCHEDULED
    assert (
        decide(
            failure_reason=FailureReason.ISSUER_UNAVAILABLE,
            attempt_number=1,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.RETRY_SCHEDULED
    )
    assert (
        decide(
            failure_reason=FailureReason.ISSUER_UNAVAILABLE,
            attempt_number=4,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.RETRY_SCHEDULED
    )


def test_retries_exhausted_with_escalation_uses_escalation_action():
    # DO_NOT_HONOR: retryable, max_attempts=1, escalation_action=ESCALATE_HUMAN
    assert (
        decide(
            failure_reason=FailureReason.DO_NOT_HONOR,
            attempt_number=2,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.ESCALATE_HUMAN
    )


def test_retries_exhausted_without_escalation_falls_back_to_stop():
    # ISSUER_UNAVAILABLE: retryable, max_attempts=4, escalation_action=None
    assert (
        decide(
            failure_reason=FailureReason.ISSUER_UNAVAILABLE,
            attempt_number=5,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.STOP
    )


def test_non_retryable_always_uses_default_action_regardless_of_attempt_number():
    for attempt in (1, 2, 99):
        assert (
            decide(
                failure_reason=FailureReason.EXPIRED_CARD,
                attempt_number=attempt,
                entity_type=EntityType.PAYMENT,
                mandate_state=None,
                value_minor=SMALL_VALUE,
            )
            == Action.REQUEST_NEW_INSTRUMENT
        )


def test_fraud_signal_never_contacts_the_customer():
    assert (
        decide(
            failure_reason=FailureReason.STOLEN_CARD,
            attempt_number=1,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.OPS_ALERT
    )


def test_revoked_mandate_overrides_failure_reason_entirely():
    # DO_NOT_HONOR would normally retry, but a revoked mandate can't be charged at all.
    assert (
        decide(
            failure_reason=FailureReason.DO_NOT_HONOR,
            attempt_number=1,
            entity_type=EntityType.SUBSCRIPTION,
            mandate_state=MandateStatus.REVOKED,
            value_minor=SMALL_VALUE,
        )
        == Action.RECOLLECT_MANDATE
    )


def test_checkout_is_always_a_nudge():
    for attempt in (1, 5, 20):
        assert (
            decide(
                failure_reason=None,
                attempt_number=attempt,
                entity_type=EntityType.CHECKOUT,
                mandate_state=None,
                value_minor=SMALL_VALUE,
            )
            == Action.NUDGE
        )


def test_invoice_escalates_after_the_nudge_limit():
    assert (
        decide(
            failure_reason=None,
            attempt_number=1,
            entity_type=EntityType.INVOICE,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.NUDGE
    )
    assert (
        decide(
            failure_reason=None,
            attempt_number=4,
            entity_type=EntityType.INVOICE,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.ESCALATE_HUMAN
    )


def test_high_value_escalates_to_a_human_even_when_policy_would_auto_retry():
    assert (
        decide(
            failure_reason=FailureReason.ISSUER_UNAVAILABLE,
            attempt_number=1,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=HUMAN_REVIEW_THRESHOLD_MINOR,
        )
        == Action.ESCALATE_HUMAN
    )


def test_high_value_does_not_override_an_already_suppressed_action():
    # Fraud stays OPS_ALERT regardless of amount -- value doesn't make a
    # "don't contact the customer" decision MORE escalated.
    assert (
        decide(
            failure_reason=FailureReason.STOLEN_CARD,
            attempt_number=1,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=HUMAN_REVIEW_THRESHOLD_MINOR * 10,
        )
        == Action.OPS_ALERT
    )


def test_insufficient_funds_still_gets_a_normal_retry_action():
    # X_INSUFFICIENT_FUNDS is non-claimable (segment-reported), but that's a
    # reporting distinction -- policy.py still decides a normal action for it.
    assert (
        decide(
            failure_reason=FailureReason.INSUFFICIENT_FUNDS,
            attempt_number=1,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
        == Action.RETRY_SCHEDULED
    )


def test_missing_failure_reason_for_payment_raises():
    with pytest.raises(ValueError):
        decide(
            failure_reason=None,
            attempt_number=1,
            entity_type=EntityType.PAYMENT,
            mandate_state=None,
            value_minor=SMALL_VALUE,
        )
