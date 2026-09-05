"""The recovery decision engine: (failure_reason, attempt_number, entity_type,
mandate_state, value_minor) -> Action.

Pure and side-effect-free by design -- no DB, no I/O -- so it is trivially
unit-testable, and so bounds.py can call it inside a single
execute_action() chokepoint without worrying what else it might do.
Versioned (POLICY_VERSION) because every recovery_attempts row snapshots
which version decided it; changing this function's behavior without
bumping the version would silently rewrite history for attempts already
decided under the old one.

decide() answers "what would we naturally do here" -- it is not the last
word. The bounds chokepoint enforces hard limits (consent, quiet
hours, spend caps, EMERGENCY_STOP) on top of whatever this returns, and
can downgrade any Action to STOP. The one bound this module preempts on
its own is human_approval_above_minor's spirit: a large loss defaults to
a human's eyes before an automated customer-facing action goes out,
because "safe by default" for money above that line is a policy
preference, not something that should depend on bounds.py running first.
"""

from typing import Optional

from app.canonical.vocabulary import FAILURE_TAXONOMY, Action, EntityType, FailureReason, MandateStatus

POLICY_VERSION = "policy@v1"

# Matches policy_bounds.human_approval_above_minor's schema default (Rs 50,000).
# Bounds.py enforces the hard, per-business-configurable version of this;
# this constant is just this module's own default-safe threshold.
HUMAN_REVIEW_THRESHOLD_MINOR = 5_000_000

# Actions that are already "don't touch the customer" -- a large value
# doesn't make a fraud stop or an internal alert MORE escalated, so these
# are exempt from the value-based ESCALATE_HUMAN override below.
_VALUE_OVERRIDE_EXEMPT = {Action.STOP, Action.OPS_ALERT, Action.ESCALATE_HUMAN}

# INVOICE dunning has no FailureReason at all (B4 is a sweep-detected
# state, not a failure code) -- escalate to a human once automated nudges
# have had this many attempts without the invoice being paid.
INVOICE_NUDGE_LIMIT = 3


def decide(
    *,
    failure_reason: Optional[FailureReason],
    attempt_number: int,
    entity_type: EntityType,
    mandate_state: Optional[MandateStatus],
    value_minor: int,
) -> Action:
    if entity_type == EntityType.CHECKOUT:
        # B1: no failure_reason, no mandate concept -- always a nudge to finish the cart.
        return Action.NUDGE

    if entity_type == EntityType.INVOICE:
        # B4: no failure_reason either -- dunning attempts, then a human chases it.
        return Action.ESCALATE_HUMAN if attempt_number > INVOICE_NUDGE_LIMIT else Action.NUDGE

    # PAYMENT and SUBSCRIPTION share failure_reason-driven logic below.
    if mandate_state == MandateStatus.REVOKED:
        # A charge can't even be attempted without a live mandate, regardless of
        # what the failure_reason's own default_action would otherwise say.
        action = Action.RECOLLECT_MANDATE
    else:
        if failure_reason is None:
            raise ValueError(f"failure_reason is required for entity_type={entity_type}")
        entry = FAILURE_TAXONOMY[failure_reason]
        if entry.retryable and attempt_number <= entry.max_attempts:
            action = entry.default_action
        elif entry.retryable:
            # retries exhausted
            action = entry.escalation_action or Action.STOP
        else:
            # nothing to retry -- default_action IS the (only) response
            action = entry.default_action

    if value_minor >= HUMAN_REVIEW_THRESHOLD_MINOR and action not in _VALUE_OVERRIDE_EXEMPT:
        return Action.ESCALATE_HUMAN

    return action
