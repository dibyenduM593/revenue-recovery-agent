"""Walk-up live demo: plant one real phone number's failed payment into

the real pipeline, then let the real queue drive a real call and a real
WhatsApp nudge to it.

Three rules this module exists to hold:

1. NOTHING BYPASSES THE PIPELINE. plant() does not write to payments or
   revenue_at_risk directly -- it synthesizes one provider-shaped
   payment.failed payload and POSTs it through /v1/imports, exactly as
   seed/generate.py does, so normalization, risk detection and scoring all
   run for it the same way they run for the synthetic corpus. Same
   cardinal rule the README already claims; this is the module most
   tempted to break it.

2. THE CALL CANNOT BE CREDITED WITH A RECOVERY. The VOICE and WHATSAPP
   legs are decided together (bounds.FanOutLeg) and share ONE minted
   nudge token, but only the WhatsApp leg carries that token on its
   recovery_attempts row. attribution.py credits recoveries by an
   attempt's own recovery_token, so a call is structurally ineligible --
   enforced by a foreign key, not by a convention someone has to remember.
   The WhatsApp leg is listed second so it also owns the higher
   attempt_number, keeping it the "latest attempt" that report.py and
   human_review.py resolve per at-risk row.

3. AN UNVERIFIED NUMBER NEVER REACHES TWILIO. channels.provider_for()
   routes to the real API only for numbers in TWILIO_ALLOWLIST; every
   other number plants, queues and drains identically but sends through
   the simulated provider. On a Twilio trial account an unverified number
   is rejected at the API anyway -- this makes that a routing decision we
   make on purpose rather than an error we discover.

DEMO STAND-IN, stated plainly: there is no real money movement here. The
WhatsApp message asks the recipient to reply YES, and a YES is ingested
as a payment-success event carrying the nudge token -- which attribution
then matches as a STRONG token-attributed recovery. A real deployment
would swap that one function (_yes_reply_event) for a Razorpay Payment
Link webhook and change nothing else. This is documented in README.md
under "What's simulated vs. real" so the batch report's recovered number
is never mistaken for collected cash in the demo.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bounds import FanOutLeg, execute_action
from app.canonical.vocabulary import (
    FAILURE_TAXONOMY,
    UNRESOLVED_STATUSES,
    Action,
    Cohort,
    EntityType,
    FailureReason,
)
from app.channels import voice_script_template
from app.db import SessionLocal
from app.models import (
    Business,
    Customer,
    CustomerContactability,
    Payment,
    PolicyBounds,
    RecoveryAttempt,
    RevenueAtRisk,
)
from app.policy import decide
from app.settings import ANTHROPIC_API_KEY, EXPLAIN_MODEL, TWILIO_ALLOWLIST
from seed.generator import Customer as GenCustomer
from seed.generator import _event, _payment_entity, _webhook
from seed.reference import DEMO_SEED

# Same discipline as app/explain.py's narrative generation: the model is
# handed exactly one number it's allowed to say (the amount) and cannot
# invent, round, or combine anything else. Written for text-to-speech
# (Twilio's <Say>), not for reading -- no markdown, no bullet points, no
# sign-off, and told explicitly this is spoken aloud.
_VOICE_SYSTEM_PROMPT = (
    "You write short scripts for an automated phone call, read aloud by text-to-speech, telling "
    "a customer their payment failed and guiding them to WhatsApp for next steps. Spoken tone, "
    "not written: plain sentences, no markdown, no bullet points, no sign-off, 3-4 sentences. "
    "Briefly and naturally explain why the payment failed given the reason provided. End by "
    "telling them a WhatsApp message has been sent and they can resolve this by replying yes or no. "
    "CRITICAL: the only number you may say is the amount given to you -- never compute, round, or "
    "introduce any other number, and never invent details not given to you."
)


def _llm_voice_script(*, amount_text: str, reason_text: str) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic

        # Local import: app.explain -> app.recovery.report -> this module
        # (for live_demo_at_risk_ids), so a top-level import here is circular.
        from app.explain import verify_numbers

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=EXPLAIN_MODEL,
            max_tokens=300,
            system=_VOICE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Amount: {amount_text}\nFailure reason: {reason_text}"}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            return None
        amount_number = amount_text.split()[0]  # "2499 rupees" -> "2499"
        verified, _unverified = verify_numbers(text, {"verifiable_numbers": [amount_number]})
        return text if verified else None
    except Exception:  # noqa: BLE001 -- an LLM call failing is a fallback trigger, not a crash
        return None


def _voice_script(*, amount_text: str, reason_text: str) -> str:
    return _llm_voice_script(amount_text=amount_text, reason_text=reason_text) or voice_script_template(
        amount_text=amount_text, reason_text=reason_text
    )

# Deliberately under policy.HUMAN_REVIEW_THRESHOLD_MINOR (Rs 50,000): at or
# above it decide() returns ESCALATE_HUMAN, which has no channel, so
# nothing would ever be queued and the demo would look broken for a
# reason that is actually correct behavior.
DEMO_AMOUNT_MINOR = 249_900


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    error_code: str
    error_reason: str
    error_description: str
    error_source: str
    canonical: FailureReason


# Only failure reasons whose policy outcome is a CUSTOMER-FACING action
# belong in a dropdown that promises a phone call -- plus one that
# deliberately isn't, because a system that knows when NOT to contact
# someone is worth showing too (see ATTENTION_SLIP's taxonomy note:
# a wrong OTP is synchronous and mid-session, an async nudge just pesters).
SCENARIOS: dict[str, Scenario] = {
    "expired_card": Scenario(
        "expired_card", "Card expired", "BAD_REQUEST_ERROR", "expired_card",
        "The card has expired.", "customer", FailureReason.EXPIRED_CARD,
    ),
    "invalid_details": Scenario(
        "invalid_details", "Wrong card details", "BAD_REQUEST_ERROR", "incorrect_card_details",
        "The card number, expiry date, or CVV is invalid.", "customer", FailureReason.INVALID_DETAILS,
    ),
    "mandate_revoked": Scenario(
        "mandate_revoked", "Mandate revoked", "BAD_REQUEST_ERROR", "mandate_revoked",
        "The payment mandate backing this charge has been revoked.", "customer", FailureReason.MANDATE_REVOKED,
    ),
    "wrong_otp": Scenario(
        "wrong_otp", "Wrong OTP (policy declines to act)", "BAD_REQUEST_ERROR", "incorrect_otp",
        "The OTP or PIN entered was incorrect.", "customer", FailureReason.ATTENTION_SLIP,
    ),
}


def live_demo_at_risk_ids(session: Session, business_id: uuid.UUID) -> set[uuid.UUID]:
    """Rows plant() stamped -- report.py excludes these from cohort totals

    the same way it already excludes awaiting-human rows (see
    human_review.py): a walk-up tester's forced-TREATMENT, forced-consent
    row is not part of the statistical population the holdout measures,
    so it must never be allowed to move the claimed lift number. It still
    shows up in the dashboard's own tables/KPIs, per the "show but don't
    count" call this module was built to.
    """
    rows = session.execute(
        select(RevenueAtRisk.at_risk_id).where(
            RevenueAtRisk.business_id == business_id, RevenueAtRisk.attributes.has_key("live_demo")
        )
    ).all()
    return {r[0] for r in rows}


def _demo_email_for(phone_e164: str) -> str:
    """A deterministic email that hashes to B2C.

    customer_type comes from customer_profile_for(email, DEMO_SEED), and a
    planted row that lands B2B would sit in the B2B tab looking for an
    invoice it doesn't have. Walking a counter until the hash lands B2C
    takes one or two tries and stays deterministic per phone number.
    """
    from seed.generator import customer_profile_for

    digits = "".join(ch for ch in phone_e164 if ch.isdigit())
    for n in range(50):
        candidate = f"live-demo-{digits}-{n}@demo.local"
        if customer_profile_for(candidate, DEMO_SEED).customer_type == "B2C":
            return candidate
    return f"live-demo-{digits}@demo.local"


def _force_contactability(session: Session, business_id: uuid.UUID, customer_id: uuid.UUID, now: datetime) -> None:
    """A walk-up number has no seeded consent row, and the seeded ones are

    randomized. Every bound stays enforced -- this just sets the state a
    real opted-in customer would have, explicitly, instead of leaving it
    to chance. min_gap_hours=0 matters specifically: the VOICE and
    WHATSAPP legs are sent seconds apart, and the default 24h gap (H10)
    would suppress the second one at send time.
    """
    for channel in ("VOICE", "WHATSAPP", "SMS"):
        row = session.get(CustomerContactability, (business_id, customer_id, channel))
        if row is None:
            row = CustomerContactability(
                business_id=business_id, customer_id=customer_id, channel=channel,
            )
            session.add(row)
        row.opted_in = True
        row.dnd_registered = False
        row.hard_bounced = False
        # _in_quiet_hours(now, start, end) treats start==end as the empty
        # range, i.e. NEVER in quiet hours -- 00:00-23:59 was tried first
        # and does the opposite: it's quiet all day except one minute.
        # Caught by actually running launch() end to end, not by inspection.
        row.quiet_hours_start = datetime.min.time().replace(hour=0, minute=0)
        row.quiet_hours_end = datetime.min.time().replace(hour=0, minute=0)
        row.min_gap_hours = 0
        row.max_contacts_per_week = 50
        row.consecutive_failures = 0
    session.flush()


def plant(phone_e164: str, scenario_key: str, *, business_id: Optional[uuid.UUID] = None) -> dict:
    """Ingest one real failed payment for this phone number, through the

    real endpoints. Returns the at_risk_id the pipeline opened for it.
    """
    import random

    from fastapi.testclient import TestClient

    from app.api import app as fastapi_app
    from app.worker import drain
    from seed.generate import deliver_via_imports

    scenario = SCENARIOS.get(scenario_key)
    if scenario is None:
        raise ValueError(f"unknown scenario {scenario_key!r}; expected one of {sorted(SCENARIOS)}")

    if business_id is None:
        from app.orchestrator import _business_id

        business_id = _business_id()

    now = datetime.now(timezone.utc)
    email = _demo_email_for(phone_e164)
    rng = random.Random(uuid.uuid4().int % (2**32))

    gen_customer = GenCustomer(
        index=0, email=email, name="Live Demo", phone=phone_e164, customer_type="B2C",
        segment="regular", sector="retail", business_model="product", archetype="reliable",
        fixed_amount_minor=DEMO_AMOUNT_MINOR, amount_volatility=0.0, n_transactions=1,
    )

    class _F:  # shape _payment_entity expects for a failure
        error_code = scenario.error_code
        error_reason = scenario.error_reason
        error_description = scenario.error_description
        error_source = scenario.error_source

    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    order_id = f"order_{uuid.uuid4().hex[:10]}"
    entity = _payment_entity(
        rng, payment_id, order_id, DEMO_AMOUNT_MINOR, "INR", gen_customer, "failed", now, _F, None
    )
    event = _event(
        business_id, "razorpay", "webhook", now,
        _webhook("payment.failed", "payment", entity), f"evt_{payment_id}",
    )

    client = TestClient(fastapi_app)
    import_result = deliver_via_imports(client, [event])
    drain_stats = drain()

    session = SessionLocal()
    try:
        payment = session.execute(
            select(Payment).where(Payment.business_id == business_id, Payment.payment_intent_id == order_id)
        ).scalars().first()
        if payment is None:
            return {
                "ok": False, "reason": "payment did not normalize", "imports": import_result, "drain": drain_stats,
            }

        at_risk = session.execute(
            select(RevenueAtRisk).where(
                RevenueAtRisk.business_id == business_id,
                RevenueAtRisk.entity_type == "PAYMENT",
                RevenueAtRisk.entity_id == payment.payment_id,
            )
        ).scalars().first()

        if payment.customer_id is not None:
            _force_contactability(session, business_id, payment.customer_id, now)

        if at_risk is not None:
            # The one place this module marks its own rows. Not a shortcut
            # around detection -- detection already ran and opened this row;
            # this stamps it so the queue, the report and the dashboard can
            # all tell a walk-up demo item from corpus data.
            at_risk.attributes = {
                **(at_risk.attributes or {}),
                "live_demo": {"phone": phone_e164, "scenario": scenario_key, "planted_at": now.isoformat()},
            }
        session.commit()

        return {
            "ok": True,
            "at_risk_id": str(at_risk.at_risk_id) if at_risk else None,
            "payment_id": str(payment.payment_id),
            "customer_id": str(payment.customer_id) if payment.customer_id else None,
            "email": email,
            "amount_minor": DEMO_AMOUNT_MINOR,
            "scenario": scenario.label,
            "will_call_for_real": phone_e164 in TWILIO_ALLOWLIST,
            "imports": import_result,
            "drain": drain_stats,
        }
    finally:
        session.close()


def launch(at_risk_id: uuid.UUID, *, business_id: Optional[uuid.UUID] = None) -> dict:
    """Decide, fan out to VOICE + WHATSAPP on one shared nudge, enqueue, drain.

    Goes through policy.decide() and every H-bound exactly like the batch
    does -- if the scenario's policy outcome is STOP (wrong OTP), nothing
    is queued and that is the correct, demonstrable answer, not a failure.
    """
    from app.dispatch_worker import drain as drain_dispatches

    if business_id is None:
        from app.orchestrator import _business_id

        business_id = _business_id()

    now = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        at_risk = session.get(RevenueAtRisk, at_risk_id)
        if at_risk is None:
            return {"ok": False, "reason": "unknown at_risk_id"}

        business = session.get(Business, business_id)
        bounds = session.get(PolicyBounds, business_id)
        customer = session.get(Customer, at_risk.customer_id) if at_risk.customer_id else None

        failure_reason = FailureReason(at_risk.failure_reason) if at_risk.failure_reason else None
        attempt_number = session.execute(
            select(RecoveryAttempt).where(RecoveryAttempt.at_risk_id == at_risk_id)
        ).scalars().all()
        action = decide(
            failure_reason=failure_reason, attempt_number=len(attempt_number) + 1,
            entity_type=EntityType(at_risk.entity_type), mandate_state=None,
            value_minor=at_risk.at_risk_minor,
        )
        if action in (Action.STOP, Action.OPS_ALERT):
            return {
                "ok": True, "action": action.value, "queued": False,
                "explanation": FAILURE_TAXONOMY[failure_reason].notes if failure_reason else "",
            }

        amount_text = f"{at_risk.at_risk_minor / 100:.0f} rupees"
        reason_text = (at_risk.failure_reason or "a payment failure").replace("_", " ").lower()
        script = _voice_script(amount_text=amount_text, reason_text=reason_text)
        whatsapp_body = (
            f"About your {amount_text} payment that failed ({reason_text}). "
            "This is a demo, so there's no real payment link: reply YES and we'll record this as recovered, "
            "or NO to leave it open."
        )

        result = execute_action(
            session, business=business, bounds=bounds, at_risk_id=at_risk_id,
            customer_id=at_risk.customer_id, entity_type=at_risk.entity_type, entity_id=at_risk.entity_id,
            action=action, channel=None, template_id=None,
            # Forced TREATMENT, not assign_cohort()'s hash: a walk-up demo
            # tester isn't part of the statistical population the holdout
            # exists to measure, and a coin flip that silently does nothing
            # ~20% of the time is a bad live demo, not a correct one. The
            # real batch's assign_cohort() is untouched by this -- and
            # report.py excludes live_demo-tagged rows from cohort totals
            # either way, so this can't skew the measured lift number.
            cohort=Cohort.TREATMENT,
            value_minor=at_risk.at_risk_minor, attribution_key_value=str(at_risk.entity_id),
            loss_category=at_risk.loss_category, now=now,
            # VOICE first, WHATSAPP second: the nudge leg must own the higher
            # attempt_number so it stays the "latest attempt" everything
            # else resolves per at-risk row.
            fan_out=[
                FanOutLeg(channel="VOICE", is_nudge=False, body_override=script),
                FanOutLeg(channel="WHATSAPP", is_nudge=True, body_override=whatsapp_body),
            ],
            score_snapshot={"live_demo": True},
        )
        if result.enqueued:
            at_risk.status = "IN_RECOVERY"
        session.commit()
        enqueued = result.enqueued
        suppressed = result.suppressed_reason
        attempt_ids = [str(a) for a in result.all_attempt_ids]
        for_real = bool(customer and customer.phone_e164 in TWILIO_ALLOWLIST)
    finally:
        session.close()

    dispatch_stats = drain_dispatches(business_id=business_id)
    return {
        "ok": True, "action": action.value, "queued": enqueued, "suppressed_reason": suppressed,
        "attempt_ids": attempt_ids, "dispatch": dispatch_stats, "for_real": for_real,
    }


def _yes_reply_event(business_id: uuid.UUID, payment_intent_id: str, amount_minor: int, currency: str,
                     email: str, phone: str, recovery_token: Optional[str], now: datetime) -> dict:
    """THE demo stand-in, isolated to one function on purpose.

    A real deployment replaces this with a Razorpay Payment Link
    webhook -- the shape it produces (a captured payment carrying the
    nudge token) is identical either way, so attribution, the report and
    every number downstream are unchanged by the swap.
    """
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    entity = {
        "id": payment_id, "entity": "payment", "amount": amount_minor, "currency": currency,
        "status": "captured", "order_id": payment_intent_id, "method": "card", "email": email,
        "contact": phone, "notes": [], "captured": True, "amount_refunded": 0, "international": False,
        "error_code": None, "error_description": None, "error_source": None, "error_step": None,
        "error_reason": None, "created_at": int(now.timestamp()),
    }
    if recovery_token:
        entity["recovery_token"] = recovery_token
    return _event(
        business_id, "razorpay", "webhook", now,
        _webhook("payment.captured", "payment", entity), f"evt_{payment_id}",
    )


def record_reply(
    from_number: str, body_text: str, *, business_id: Optional[uuid.UUID] = None,
    message_time: Optional[datetime] = None,
) -> dict:
    """Twilio inbound-WhatsApp handler. A YES becomes a real ingested

    payment-success event carrying the nudge token; attribution then
    matches it as TOKEN_CLICK/STRONG on its next run, the same path a real
    payment link click would take. Never writes recovery_outcomes directly.

    message_time is the reply's OWN timestamp (Twilio's date_sent when
    polled -- see app/live_poll.py), not the moment this function happens
    to run. A trial account only ever has one allowlisted number, so
    re-polling Twilio's recent-messages list after any restart re-offers
    old, already-answered replies as if new (see app/live_poll.py's
    _seen_message_sids, which is in-memory and does not survive one).
    Without this guard an old reply could redeem a token minted AFTER it
    was sent -- closing out a fresh nudge nobody actually answered yet.
    Requiring the matched attempt to have existed (been executed) at or
    before the reply's own timestamp, and to still be open, is what
    actually links a YES to the transaction it was replying to, rather
    than to "whatever nudge is newest right now."
    """
    from fastapi.testclient import TestClient

    from app.api import app as fastapi_app
    from app.recovery.attribution import run_attribution
    from app.worker import drain
    from seed.generate import deliver_via_imports

    if business_id is None:
        from app.orchestrator import _business_id

        business_id = _business_id()

    normalized = (body_text or "").strip().lower()
    said_yes = normalized.startswith("y")
    phone = from_number.replace("whatsapp:", "").strip()
    now = datetime.now(timezone.utc)
    reply_time = message_time or now

    session = SessionLocal()
    try:
        customer = session.execute(
            select(Customer).where(Customer.business_id == business_id, Customer.phone_e164 == phone)
        ).scalars().first()
        if customer is None:
            return {"ok": False, "reason": f"no customer for {phone}"}

        # The most recent nudge-bearing attempt for this customer that (a)
        # already existed when this reply was sent and (b) is still
        # unresolved -- the one whose token this specific YES should
        # redeem, not just "whatever's newest in the table right now."
        # UNRESOLVED_STATUSES, not just "OPEN": a nudge that actually went
        # out moves its row to IN_RECOVERY (see launch()), so matching only
        # OPEN would exclude precisely the rows that are waiting on a reply.
        attempt = session.execute(
            select(RecoveryAttempt)
            .join(RevenueAtRisk, RevenueAtRisk.at_risk_id == RecoveryAttempt.at_risk_id)
            .where(
                RecoveryAttempt.business_id == business_id,
                RecoveryAttempt.customer_id == customer.customer_id,
                RecoveryAttempt.recovery_token.is_not(None),
                RecoveryAttempt.executed_at.is_not(None),
                RecoveryAttempt.executed_at <= reply_time,
                RevenueAtRisk.status.in_(UNRESOLVED_STATUSES),
            )
            .order_by(RecoveryAttempt.executed_at.desc())
            .limit(1)
        ).scalars().first()
        if attempt is None:
            return {"ok": False, "reason": "no open nudge awaiting a reply for this number as of this message's time"}

        at_risk = session.get(RevenueAtRisk, attempt.at_risk_id)
        payment = session.get(Payment, at_risk.entity_id) if at_risk.entity_type == "PAYMENT" else None
        payment_intent_id = payment.payment_intent_id if payment else None
        token = attempt.recovery_token
        amount_minor = at_risk.at_risk_minor
        currency = at_risk.currency
        email = customer.email or ""
    finally:
        session.close()

    if not said_yes:
        return {"ok": True, "reply": "no", "recovered": False, "at_risk_id": str(at_risk.at_risk_id)}

    if payment_intent_id is None:
        return {"ok": False, "reason": "no payment_intent_id to attribute against"}

    event = _yes_reply_event(business_id, payment_intent_id, amount_minor, currency, email, phone, token, now)
    client = TestClient(fastapi_app)
    import_result = deliver_via_imports(client, [event])
    drain_stats = drain()

    session = SessionLocal()
    try:
        # Real wall-clock time, not run_attribution()'s own default (MAX
        # RevenueEvent.occurred_at across the business) -- see
        # app/live_poll.py's _sweep_attribution() for why that default is
        # wrong for anything evaluating a real reply against a real window.
        attribution_stats = run_attribution(session, business_id, clock=now)
        session.commit()
    finally:
        session.close()

    return {
        "ok": True, "reply": "yes", "recovered": True, "at_risk_id": str(at_risk.at_risk_id),
        "imports": import_result, "drain": drain_stats, "attribution": attribution_stats,
    }


# ---------------------------------------------------------------------
# HTTP surface. The dashboard calls plant/launch directly; the reply
# endpoint is the one Twilio itself calls -- point the WhatsApp sandbox's
# "when a message comes in" webhook at POST /live-demo/whatsapp-reply.
# ---------------------------------------------------------------------

router = APIRouter()


class PlantRequest(BaseModel):
    phone_e164: str
    scenario: str


class LaunchRequest(BaseModel):
    at_risk_id: str


@router.get("/dashboard/api/live-demo/scenarios")
def api_scenarios():
    return {"scenarios": [{"key": s.key, "label": s.label} for s in SCENARIOS.values()]}


@router.post("/dashboard/api/live-demo/plant")
def api_plant(body: PlantRequest):
    return plant(body.phone_e164, body.scenario)


@router.post("/dashboard/api/live-demo/launch")
def api_launch(body: LaunchRequest):
    return launch(uuid.UUID(body.at_risk_id))


@router.post("/live-demo/whatsapp-reply")
def api_whatsapp_reply(From: str = Form(...), Body: str = Form(...)):
    """Twilio POSTs application/x-www-form-urlencoded with these exact

    field names (capitalized) for an inbound WhatsApp message. TwiML back
    is optional for a webhook that only needs to record the reply, not
    respond in-band -- an empty <Response/> tells Twilio not to send
    anything further.
    """
    record_reply(From, Body)
    return PlainTextResponse('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")
