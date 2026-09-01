"""Day 11: every rupee explainable.

Four scopes -- ATTEMPT, AT_RISK, BATCH, SUPPRESSION -- each backed by a
deterministic evidence bundle built entirely from the ledger (never
re-derived from raw events, never containing anything the LLM invented).
The narrative is generated FROM that bundle and stored once; it is never
regenerated at view time, so what a viewer sees today matches what got
verified.

The number verifier is the actual safety mechanism, not a decoration:
every evidence bundle carries an explicit `verifiable_numbers` allowlist
-- the exact figures it is safe to state, pre-formatted -- and the
narrative prompt is instructed to use only those. Anything the model
writes that isn't in that allowlist is flagged in `unverified_spans` and
`numbers_verified` is set False, which the demo page treats as "do not
trust this narrative, fall back to the template" per the plan's own cut
order ("LLM explanations hallucinate figures -> Verifier already blocks
display; fall back to templated narration").

Fails closed exactly like EmailChannel: no ANTHROPIC_API_KEY configured,
or the call errors, or verification fails -> the deterministic template
path runs instead. This module never blocks the demo on an LLM being
reachable.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CheckoutSession,
    Customer,
    Invoice,
    Payment,
    RecoveryAttempt,
    RecoveryBatch,
    RecoveryExplanation,
    RecoveryOutcome,
    RevenueAtRisk,
    Subscription,
)
from app.recovery.report import build_report, render
from app.settings import ANTHROPIC_API_KEY, EXPLAIN_MODEL

PROMPT_VERSION = "explain@v1"

_ENTITY_MODEL = {"PAYMENT": Payment, "CHECKOUT": CheckoutSession, "INVOICE": Invoice, "SUBSCRIPTION": Subscription}

_SUPPRESSION_MEANINGS = {
    "h1_no_consent_record": "No consent record exists for this customer on this channel.",
    "h1_not_opted_in": "The customer has not opted in to this channel.",
    "h2_revoked_mandate_blocks_charge": "The subscription's payment mandate has been revoked, so an automated charge cannot be attempted.",
    "h3_policy_stop": "The policy engine itself decided no action should be taken.",
    "h4_open_dispute": "There is an open dispute on this payment -- contacting the customer about it is a hard stop.",
    "h5_hard_bounced": "This channel has hard-bounced for this customer before; it is never retried.",
    "h6_refund_recorded": "A refund has already been recorded against this payment.",
    "h7_contact_cap": "The customer has already been contacted the maximum number of times allowed this week.",
    "h8_quiet_hours": "It is currently outside the customer's allowed contact hours in their local timezone.",
    "h9_dnd_registered": "The customer's number is registered Do Not Disturb for SMS/WhatsApp/voice.",
    "h10_min_gap": "Not enough time has passed since the customer was last contacted.",
    "held_for_approval": "The amount at risk exceeds the threshold requiring human approval before acting.",
    "dry_run": "The business is running in dry-run mode: decisions are recorded but nothing is sent.",
    "holdout_cohort": "This record was assigned to the holdout group, which is deliberately never contacted so its outcome measures what would have happened without any action.",
    "recovery_disabled_for_business": "Recovery is not yet enabled for this business.",
    "emergency_stop": "The global emergency stop is active, blocking all automated actions.",
}

_TERMINAL_PREFIXES = ("h1_", "h2_", "h4_", "h5_", "h6_", "h9_")


def _inr(minor: Optional[int]) -> str:
    if minor is None:
        return "0"
    rupees = minor // 100
    s = str(abs(rupees))
    if len(s) <= 3:
        grouped = s
    else:
        last3, rest = s[-3:], s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    return ("-" if rupees < 0 else "") + grouped


def _entity_label(session: Session, entity_type: str, entity_id: uuid.UUID) -> str:
    model = _ENTITY_MODEL.get(entity_type)
    entity = session.get(model, entity_id) if model else None
    if entity is None:
        return f"{entity_type.title()} {str(entity_id)[:8]}"
    if entity_type == "INVOICE":
        return f"Invoice {entity.invoice_number or str(entity_id)[:8]}"
    return f"{entity_type.title()} {str(entity_id)[:8]}"


def _forensic_context(session: Session, entity_type: str, entity_id: uuid.UUID) -> dict:
    """Qualitative detail (bank, method, who) beyond what the ledger tables

    already carry as loss_category/fault_attribution codes -- this is what
    actually lets the LLM write "declined by HDFC Bank for insufficient
    funds" instead of paraphrasing an enum. Never contributes to
    verifiable_numbers: none of these fields are numeric claims the
    narrative could get wrong, so there is nothing here for the verifier
    to check.
    """
    if entity_type != "PAYMENT":
        return {}
    payment = session.get(Payment, entity_id)
    if payment is None:
        return {}
    customer = session.get(Customer, payment.customer_id) if payment.customer_id else None
    return {
        "customer_email": customer.email if customer else None,
        "payment_method": payment.payment_method,
        "issuer_bank": payment.issuer_bank,
        "failure_code": payment.failure_code_canonical,
    }


# ---------------------------------------------------------------------
# Evidence bundle builders -- one per scope, deterministic, DB-only.
# ---------------------------------------------------------------------
def build_attempt_evidence(session: Session, attempt_id: uuid.UUID) -> dict:
    attempt = session.get(RecoveryAttempt, attempt_id)
    if attempt is None:
        raise ValueError(f"no recovery_attempts row {attempt_id}")
    at_risk = session.get(RevenueAtRisk, attempt.at_risk_id)
    outcome = session.execute(
        select(RecoveryOutcome).where(RecoveryOutcome.attempt_id == attempt_id)
    ).scalars().first()

    numbers = [str(attempt.attempt_number), _inr(at_risk.at_risk_minor)]
    if outcome and outcome.recovered_minor:
        numbers.append(_inr(outcome.recovered_minor))

    return {
        "scope": "ATTEMPT",
        "entity": _entity_label(session, at_risk.entity_type, at_risk.entity_id),
        "entity_type": at_risk.entity_type,
        "loss_category": at_risk.loss_category,
        "fault_attribution": at_risk.fault_attribution,
        "claimable": at_risk.claimable,
        "failure_reason": at_risk.failure_reason,
        "at_risk_rupees": _inr(at_risk.at_risk_minor),
        "attempt_number": attempt.attempt_number,
        "strategy": attempt.strategy,
        "channel": attempt.channel,
        "cohort": attempt.cohort,
        "decided_at": attempt.decided_at.isoformat(),
        "executed_at": attempt.executed_at.isoformat() if attempt.executed_at else None,
        "suppressed_reason": attempt.suppressed_reason,
        "delivery_status": attempt.delivery_status,
        "outcome": outcome.outcome if outcome else "PENDING",
        "recovered_rupees": _inr(outcome.recovered_minor) if outcome else None,
        "attribution_confidence": outcome.attribution_confidence if outcome else None,
        **_forensic_context(session, at_risk.entity_type, at_risk.entity_id),
        "verifiable_numbers": numbers,
    }


def build_at_risk_evidence(session: Session, at_risk_id: uuid.UUID) -> dict:
    at_risk = session.get(RevenueAtRisk, at_risk_id)
    if at_risk is None:
        raise ValueError(f"no revenue_at_risk row {at_risk_id}")
    attempts = session.execute(
        select(RecoveryAttempt).where(RecoveryAttempt.at_risk_id == at_risk_id).order_by(RecoveryAttempt.attempt_number)
    ).scalars().all()
    outcome = session.execute(
        select(RecoveryOutcome).where(RecoveryOutcome.at_risk_id == at_risk_id)
    ).scalars().first()

    numbers = [_inr(at_risk.at_risk_minor), str(len(attempts))]
    attempt_summaries = []
    for a in attempts:
        numbers.append(str(a.attempt_number))
        attempt_summaries.append(
            {
                "attempt_number": a.attempt_number,
                "strategy": a.strategy,
                "channel": a.channel,
                "executed": a.executed_at is not None,
                "suppressed_reason": a.suppressed_reason,
            }
        )
    if outcome and outcome.recovered_minor:
        numbers.append(_inr(outcome.recovered_minor))

    return {
        "scope": "AT_RISK",
        "entity": _entity_label(session, at_risk.entity_type, at_risk.entity_id),
        "entity_type": at_risk.entity_type,
        "loss_category": at_risk.loss_category,
        "fault_attribution": at_risk.fault_attribution,
        "claimable": at_risk.claimable,
        "failure_reason": at_risk.failure_reason,
        "at_risk_rupees": _inr(at_risk.at_risk_minor),
        "status": at_risk.status,
        "detected_at": at_risk.detected_at.isoformat(),
        "detection_rule": at_risk.detection_rule,
        "attempt_count": len(attempts),
        "attempts": attempt_summaries,
        "outcome": outcome.outcome if outcome else "PENDING",
        "recovered_rupees": _inr(outcome.recovered_minor) if outcome else None,
        "attribution_method": outcome.attribution_method if outcome else None,
        "attribution_confidence": outcome.attribution_confidence if outcome else None,
        **_forensic_context(session, at_risk.entity_type, at_risk.entity_id),
        "verifiable_numbers": numbers,
    }


def build_batch_evidence(session: Session, batch_id: uuid.UUID) -> dict:
    batch = session.get(RecoveryBatch, batch_id)
    if batch is None:
        raise ValueError(f"no recovery_batches row {batch_id}")

    # recovery_attempts has no batch_id column (Day 8's schema), so there is
    # no way to scope the report strictly to this one batch's own attempts --
    # it reflects the business's current ledger as a whole. Honest about the
    # gap rather than pretending a scoping that doesn't exist.
    report = build_report(session, batch.business_id)
    t, h = report.claimable["TREATMENT"], report.claimable["HOLDOUT"]
    lift = t.rate - h.rate

    numbers = [
        str(batch.entities_scanned), str(batch.decisions_made), str(batch.actions_executed),
        str(batch.actions_suppressed), str(batch.actions_stopped), _inr(batch.at_risk_minor),
        str(t.total), str(t.recovered_strong), str(h.total), str(h.recovered_strong),
        f"{t.rate:.1f}", f"{h.rate:.1f}", f"{lift:+.1f}",
        _inr(t.recovered_minor), _inr(h.recovered_minor),
    ]

    return {
        "scope": "BATCH",
        "batch_id": str(batch_id),
        "dry_run": batch.dry_run,
        "entities_scanned": batch.entities_scanned,
        "decisions_made": batch.decisions_made,
        "actions_executed": batch.actions_executed,
        "actions_suppressed": batch.actions_suppressed,
        "actions_stopped": batch.actions_stopped,
        "at_risk_rupees": _inr(batch.at_risk_minor),
        "current_business_report_text": render(report),
        "treatment_total": t.total,
        "treatment_recovered": t.recovered_strong,
        "treatment_rate_pct": f"{t.rate:.1f}",
        "holdout_total": h.total,
        "holdout_recovered": h.recovered_strong,
        "holdout_rate_pct": f"{h.rate:.1f}",
        "lift_pp": f"{lift:+.1f}",
        "verifiable_numbers": numbers,
    }


def build_suppression_evidence(session: Session, attempt_id: uuid.UUID) -> dict:
    attempt = session.get(RecoveryAttempt, attempt_id)
    if attempt is None:
        raise ValueError(f"no recovery_attempts row {attempt_id}")
    if not attempt.suppressed_reason:
        raise ValueError(f"attempt {attempt_id} was not suppressed -- nothing to explain here")
    at_risk = session.get(RevenueAtRisk, attempt.at_risk_id)

    reason = attempt.suppressed_reason
    meaning = _SUPPRESSION_MEANINGS.get(reason, reason)
    permanent = reason.startswith(_TERMINAL_PREFIXES) or reason == "h3_policy_stop"

    numbers = [_inr(at_risk.at_risk_minor), str(attempt.attempt_number)]
    for snapshot in (attempt.consent_snapshot, attempt.bounds_snapshot):
        for value in (snapshot or {}).values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numbers.append(str(value))

    return {
        "scope": "SUPPRESSION",
        "entity": _entity_label(session, at_risk.entity_type, at_risk.entity_id),
        "loss_category": at_risk.loss_category,
        "at_risk_rupees": _inr(at_risk.at_risk_minor),
        "attempt_number": attempt.attempt_number,
        "strategy_that_was_suppressed": attempt.strategy,
        "channel": attempt.channel,
        "suppressed_reason": reason,
        "suppressed_reason_meaning": meaning,
        "permanent": permanent,
        "consent_snapshot": attempt.consent_snapshot,
        "bounds_snapshot": attempt.bounds_snapshot,
        **_forensic_context(session, at_risk.entity_type, at_risk.entity_id),
        "verifiable_numbers": numbers,
    }


_BUILDERS = {
    "ATTEMPT": build_attempt_evidence,
    "AT_RISK": build_at_risk_evidence,
    "BATCH": build_batch_evidence,
    "SUPPRESSION": build_suppression_evidence,
}


# ---------------------------------------------------------------------
# Narrative rendering: LLM first, deterministic template as the fallback
# every failure mode (no key, API error, failed verification) lands on.
# ---------------------------------------------------------------------
def _template_narrative(bundle: dict) -> str:
    scope = bundle["scope"]
    if scope == "ATTEMPT":
        base = (
            f"{bundle['entity']} has {bundle['at_risk_rupees']} at risk, category "
            f"{bundle['loss_category']} ({bundle['fault_attribution']}). Attempt "
            f"#{bundle['attempt_number']} decided on strategy {bundle['strategy']}"
            + (f" via {bundle['channel']}" if bundle["channel"] else "") + "."
        )
        if bundle["suppressed_reason"]:
            base += f" Suppressed: {bundle['suppressed_reason']}."
        elif bundle["executed_at"]:
            base += f" Executed, delivery status {bundle['delivery_status']}."
        if bundle["outcome"] == "RECOVERED":
            base += f" Recovered {bundle['recovered_rupees']} ({bundle['attribution_confidence']} attribution)."
        return base
    if scope == "AT_RISK":
        base = (
            f"{bundle['entity']}: {bundle['at_risk_rupees']} at risk, category {bundle['loss_category']}, "
            f"detected via {bundle['detection_rule']}. {bundle['attempt_count']} attempt(s) made, "
            f"current status {bundle['status']}."
        )
        if bundle["outcome"] == "RECOVERED":
            base += f" Recovered {bundle['recovered_rupees']} via {bundle['attribution_method']}."
        return base
    if scope == "BATCH":
        return (
            f"Batch scanned {bundle['entities_scanned']} at-risk records, executed "
            f"{bundle['actions_executed']}, suppressed {bundle['actions_suppressed']}, "
            f"stopped {bundle['actions_stopped']} ({bundle['at_risk_rupees']} at risk total). "
            f"Treatment recovered {bundle['treatment_recovered']}/{bundle['treatment_total']} "
            f"({bundle['treatment_rate_pct']}%) vs. holdout {bundle['holdout_recovered']}/{bundle['holdout_total']} "
            f"({bundle['holdout_rate_pct']}%) -- lift {bundle['lift_pp']}pp."
        )
    if scope == "SUPPRESSION":
        base = (
            f"{bundle['entity']} ({bundle['at_risk_rupees']} at risk) was not actioned on attempt "
            f"#{bundle['attempt_number']}: {bundle['suppressed_reason_meaning']}"
        )
        base += " This is permanent for this loss." if bundle["permanent"] else " This may clear on a later batch."
        return base
    if scope == "GROUP":
        plural = "s" if bundle["count"] != 1 else ""
        base = (
            f"{bundle['group_name']}: {bundle['count']} item{plural}, {bundle['total_at_risk_rupees']} at risk. "
            f"{bundle['group_definition']}"
        )
        if bundle.get("total_recovered_rupees"):
            base += f" {bundle['total_recovered_rupees']} recovered so far."
        if bundle.get("by_channel"):
            base += " Channels: " + ", ".join(f"{k} ({v})" for k, v in bundle["by_channel"].items()) + "."
        if bundle.get("by_suppressed_reason"):
            base += " Reasons: " + ", ".join(f"{k} ({v})" for k, v in bundle["by_suppressed_reason"].items()) + "."
        return base
    return "No narrative template for this scope."


_WORD_RE = re.compile(r"\S+")
_PURE_NUMBER_RE = re.compile(r"^-?\+?\d[\d,]*(?:\.\d+)?%?$")
_STRIP_CHARS = ".,;:()[]{}\"'"


def _number_tokens(text: str) -> list[str]:
    """Whole whitespace-delimited words that are purely numeric, punctuation

    stripped from the edges only. Deliberately NOT a scanning regex: an
    entity id like "Invoice 6f52816a" can contain a run of digits ("52816")
    that a bare digit-matching regex would misflag as an unverified
    financial figure -- an alphanumeric token never matches here, whole-word,
    regardless of where its letters fall.
    """
    tokens = []
    for word in _WORD_RE.findall(text):
        stripped = word.strip(_STRIP_CHARS)
        if _PURE_NUMBER_RE.match(stripped):
            tokens.append(stripped)
    return tokens


def verify_numbers(narrative: str, bundle: dict) -> tuple[bool, list[str]]:
    allowed = set(bundle.get("verifiable_numbers", []))
    unverified = []
    for token in _number_tokens(narrative):
        bare = token.lstrip("+").replace(",", "")
        if token in allowed or bare in allowed or bare.rstrip("%") in allowed:
            continue
        unverified.append(token)
    return (len(unverified) == 0), unverified


_SYSTEM_PROMPT = (
    "You write short, factual explanations of payment-recovery decisions for a business "
    "owner reviewing an audit trail. You are given a JSON evidence bundle. Write 2-4 plain "
    "sentences explaining what happened and why, in the tone of an analyst, not marketing copy. "
    "If the bundle's scope is GROUP, it summarizes many transactions bucketed together (e.g. "
    "everything recovered, or everything suppressed this run) rather than one single payment -- "
    "write about the group in aggregate (what it is, how many, why they ended up here, and any "
    "channel/reason breakdown given), not as if it were one transaction. "
    "CRITICAL: the bundle's `verifiable_numbers` list is the ONLY numbers you may write -- do "
    "not compute, round, combine, or introduce any number not in that exact list. If you need "
    "to state something numeric that isn't in the list, describe it in words instead. Never "
    "invent facts not present in the bundle."
)


def _llm_narrative(bundle: dict) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=EXPLAIN_MODEL,
            max_tokens=512,
            # No output_config/effort here: that's an Opus/Sonnet-5-tier
            # parameter and errors outright on Haiku 4.5, the current
            # EXPLAIN_MODEL default. Omitting it is fine for either tier --
            # this is a short, low-stakes narrative, not a task worth
            # tuning thinking depth for.
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Evidence bundle:\n{bundle}"}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        return text or None
    except Exception:  # noqa: BLE001 -- an LLM call failing is a fallback trigger, not a crash
        return None


@dataclass
class ExplanationResult:
    narrative: str
    numbers_verified: bool
    unverified_spans: list[str]
    model: str


def render_narrative(bundle: dict) -> ExplanationResult:
    llm_text = _llm_narrative(bundle)
    if llm_text is not None:
        verified, unverified = verify_numbers(llm_text, bundle)
        if verified:
            return ExplanationResult(llm_text, True, [], EXPLAIN_MODEL)
        # LLM produced an unverifiable number -- per the plan, fall back rather than display it.

    template_text = _template_narrative(bundle)
    verified, unverified = verify_numbers(template_text, bundle)
    return ExplanationResult(template_text, verified, unverified, "template")


# ---------------------------------------------------------------------
# Cache-through entry point.
# ---------------------------------------------------------------------
def explain(
    session: Session,
    scope: str,
    *,
    business_id: uuid.UUID,
    at_risk_id: Optional[uuid.UUID] = None,
    attempt_id: Optional[uuid.UUID] = None,
    batch_id: Optional[uuid.UUID] = None,
    force: bool = False,
) -> RecoveryExplanation:
    if not force:
        id_filter = {"ATTEMPT": attempt_id, "SUPPRESSION": attempt_id, "AT_RISK": at_risk_id, "BATCH": batch_id}[scope]
        id_column = {
            "ATTEMPT": RecoveryExplanation.attempt_id,
            "SUPPRESSION": RecoveryExplanation.attempt_id,
            "AT_RISK": RecoveryExplanation.at_risk_id,
            "BATCH": RecoveryExplanation.batch_id,
        }[scope]
        cached = session.execute(
            select(RecoveryExplanation)
            .where(RecoveryExplanation.scope == scope, id_column == id_filter)
            .order_by(RecoveryExplanation.generated_at.desc())
        ).scalars().first()
        if cached is not None:
            return cached

    builder = _BUILDERS[scope]
    key_id = {"ATTEMPT": attempt_id, "SUPPRESSION": attempt_id, "AT_RISK": at_risk_id, "BATCH": batch_id}[scope]
    bundle = builder(session, key_id)
    result = render_narrative(bundle)

    explanation = RecoveryExplanation(
        explanation_id=uuid.uuid4(),
        business_id=business_id,
        scope=scope,
        at_risk_id=at_risk_id if scope == "AT_RISK" else None,
        attempt_id=attempt_id if scope in ("ATTEMPT", "SUPPRESSION") else None,
        batch_id=batch_id if scope == "BATCH" else None,
        evidence_bundle=bundle,
        narrative=result.narrative,
        numbers_verified=result.numbers_verified,
        unverified_spans=result.unverified_spans or None,
        model=result.model,
        prompt_version=PROMPT_VERSION,
        generated_at=datetime.now(timezone.utc),
    )
    session.add(explanation)
    session.flush()
    return explanation
