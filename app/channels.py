"""Day 9: channel dispatch, recovery tokens, cohort assignment.

SimulatedChannel stands in for SMS/WhatsApp/voice -- clearly labelled as
simulated, with randomized latency and a delivery-failure rate, matching
real channel behavior without a real DLT/Meta-registered sender. Email is
the one real pipe: it sends via SMTP if configured, and fails closed
(SKIPPED_NOT_CONFIGURED, never a fabricated DELIVERED) if it isn't -- this
build never claims to have sent something it didn't.

make_dispatcher() closes over a session and RNG to build the DispatchFn
bounds.execute_action() calls once an action clears every bound: it
renders the approved template, mints a recovery_token when the
attribution scheme calls for one (TOKEN-attributed categories only --
event-attributed ones like B2/B4 are proven by a later matching event,
not a click), and hands back to bounds.py with everything it needs to
write one auditable recovery_attempts row.
"""

import hashlib
import random
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bounds import ActionContext, DispatchFn, DispatchResult
from app.canonical.vocabulary import Cohort
from app.models import CheckoutSession, Customer, Invoice, MessageTemplate, Payment, RecoveryToken, Subscription
from app.settings import DEMO_RECIPIENT_EMAIL, SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USER

HOLDOUT_PERCENT = 20
SIMULATED_LATENCY_MS_RANGE = (50, 400)
SIMULATED_DELIVERY_FAILURE_RATE = 0.06

_ENTITY_MODEL = {
    "PAYMENT": Payment,
    "CHECKOUT": CheckoutSession,
    "INVOICE": Invoice,
    "SUBSCRIPTION": Subscription,
}


def assign_cohort(business_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID) -> Cohort:
    """Deterministic hash -> stable cohort. Re-deciding the same entity later

    (a retry, a re-run) must never flip its cohort -- that would
    contaminate the measurement the holdout exists to produce.
    """
    key = f"{business_id}:{entity_type}:{entity_id}"
    bucket = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 100
    return Cohort.HOLDOUT if bucket < HOLDOUT_PERCENT else Cohort.TREATMENT


def _render(template: MessageTemplate, variables: dict[str, str]) -> str:
    body = template.body
    for key, value in variables.items():
        body = body.replace(f"{{{{{key}}}}}", str(value))
    return body


def _entity_link_variables(session: Session, entity_type: str, entity_id: uuid.UUID, link: str) -> dict:
    model = _ENTITY_MODEL.get(entity_type)
    entity = session.get(model, entity_id) if model else None
    variables = {"link": link, "amount": "", "item_count": "", "invoice_number": ""}
    if entity_type == "CHECKOUT" and entity is not None:
        variables["amount"] = f"{entity.cart_value_minor / 100:.2f} {entity.currency}"
    elif entity_type == "INVOICE" and entity is not None:
        variables["amount"] = f"{entity.amount_minor / 100:.2f} {entity.currency}"
        variables["invoice_number"] = entity.invoice_number or str(entity.invoice_id)[:8]
    elif entity_type == "PAYMENT" and entity is not None:
        variables["amount"] = f"{entity.amount_minor / 100:.2f} {entity.currency}"
    elif entity_type == "SUBSCRIPTION" and entity is not None:
        variables["amount"] = f"{entity.billing_amount_minor / 100:.2f} {entity.currency}"
    return variables


def _simulated_payment_link(token: str) -> str:
    """A Razorpay Payment Link would come from their API; without live keys

    (Day 6 depends on those), this is a clearly-fake stand-in shaped like
    one. Real integration is a drop-in replacement behind this one
    function once credentials exist.
    """
    return f"https://rzp.io/l/simulated-{token[:12]}"


def _mint_token(session: Session, *, business_id: uuid.UUID, at_risk_id: uuid.UUID, attempt_id: uuid.UUID,
                 target_url: str, window_seconds: int, now: datetime) -> str:
    token = uuid.uuid4().hex
    session.add(
        RecoveryToken(
            token=token,
            business_id=business_id,
            at_risk_id=at_risk_id,
            attempt_id=attempt_id,
            target_url=target_url,
            issued_at=now,
            expires_at=now + timedelta(seconds=window_seconds),
        )
    )
    # Explicit flush: recovery_attempts.recovery_token FK-references this row,
    # and the batch runner only commits once at the very end. Without this,
    # SQLAlchemy's bulk-insert batching does not reliably order this table's
    # INSERT ahead of the (much larger) recovery_attempts batch that follows.
    session.flush()
    return token


def _pick_template(session: Session, business_id: uuid.UUID, channel: str, loss_category: str) -> Optional[MessageTemplate]:
    return session.execute(
        select(MessageTemplate).where(
            MessageTemplate.business_id == business_id,
            MessageTemplate.channel == channel,
            MessageTemplate.loss_category == loss_category,
            MessageTemplate.approved.is_(True),
        )
    ).scalars().first()


def _simulated_delivery(rng: random.Random, channel: str) -> tuple[str, dict]:
    failed = rng.random() < SIMULATED_DELIVERY_FAILURE_RATE
    latency_ms = rng.randint(*SIMULATED_LATENCY_MS_RANGE)
    status = "FAILED" if failed else "DELIVERED"
    return status, {"simulated": True, "channel": channel, "latency_ms": latency_ms}


def _send_email(to_address: str, subject: str, body: str) -> tuple[str, dict]:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM):
        return "SKIPPED_NOT_CONFIGURED", {"simulated": False, "channel": "EMAIL", "reason": "SMTP not configured"}
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_address
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_address], msg.as_string())
        return "SENT", {"simulated": False, "channel": "EMAIL", "to": to_address}
    except Exception as exc:  # noqa: BLE001 -- a channel send failing is data (delivery_status), not a crash
        return "FAILED", {"simulated": False, "channel": "EMAIL", "error": str(exc)}


def make_dispatcher(session: Session, *, business_id: uuid.UUID, loss_category: str, rng: random.Random) -> DispatchFn:
    def dispatch(ctx: ActionContext) -> DispatchResult:
        now = datetime.now(timezone.utc)
        template = _pick_template(session, business_id, ctx.channel, loss_category) if ctx.channel else None

        target_url = _simulated_payment_link(uuid.uuid4().hex)
        variables = _entity_link_variables(session, ctx.entity_type, ctx.entity_id, target_url)

        recovery_token = None
        if ctx.attribution_key_type == "TOKEN":
            recovery_token = _mint_token(
                session, business_id=business_id, at_risk_id=ctx.at_risk_id, attempt_id=ctx.attempt_id,
                target_url=target_url, window_seconds=ctx.attribution_window_seconds, now=now,
            )
            variables["link"] = f"/r/{recovery_token}"

        body = _render(template, variables) if template else f"(no approved {ctx.channel} template for {loss_category})"

        if ctx.channel == "EMAIL":
            to_address = DEMO_RECIPIENT_EMAIL or "demo-recipient@example.invalid"
            delivery_status, receipt = _send_email(to_address, "Action needed on your recent order", body)
            cost_minor = 0
        elif ctx.channel in ("SMS", "WHATSAPP", "VOICE"):
            delivery_status, receipt = _simulated_delivery(rng, ctx.channel)
            cost_minor = {"SMS": 20, "WHATSAPP": 15, "VOICE": 80}[ctx.channel]
        elif ctx.action.value == "OPS_ALERT":
            delivery_status, receipt, cost_minor = "LOGGED", {"simulated": False, "type": "ops_alert"}, 0
        else:
            # RETRY_NOW / RETRY_SCHEDULED: a system-initiated recharge attempt,
            # not a customer-facing channel. No live Razorpay charge API access
            # in this build (same gap as Day 6) -- simulated as "submitted".
            delivery_status, receipt, cost_minor = "SUBMITTED", {"simulated": True, "type": "retry"}, 0

        receipt["rendered_body"] = body
        return DispatchResult(
            channel=ctx.channel, channel_receipt=receipt, delivery_status=delivery_status,
            cost_minor=cost_minor, recovery_token=recovery_token,
        )

    return dispatch
