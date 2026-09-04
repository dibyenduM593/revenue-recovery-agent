"""Day 9 + outbound queue: cohort assignment, message payload construction,

and the provider implementations that actually send.

Split in two, matching the enqueue/drain split in app/bounds.py and
app/dispatch_worker.py:

- build_payload() runs at DECISION time (inside execute_action()): looks
  up the approved template, mints a recovery_token when the attribution
  scheme calls for one (TOKEN-attributed categories only -- event-attributed
  ones like B2/B4 are proven by a later matching event, not a click), and
  renders the body. Pure DB read/write, no provider I/O, so a queue backlog
  never blocks it.
- send_*() functions run at SEND time (inside the dispatch worker, per
  outbound_dispatches row): the actual provider call. SimulatedProvider
  stands in for SMS/WhatsApp/voice when the recipient isn't on
  TWILIO_ALLOWLIST -- clearly labelled, randomized latency/failure,
  matching real channel behavior without a real DLT/Meta-registered
  sender. EmailChannel and the Twilio providers are the real pipes: each
  fails closed (a typed *_NOT_CONFIGURED status, never a fabricated
  DELIVERED) if not configured, same discipline throughout.
"""

import hashlib
import random
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonical.vocabulary import Cohort
from app.models import CheckoutSession, Invoice, MessageTemplate, Payment, RecoveryToken, Subscription
from app.settings import (
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    TWILIO_ACCOUNT_SID,
    TWILIO_ALLOWLIST,
    TWILIO_AUTH_TOKEN,
    TWILIO_VOICE_NUMBER,
    TWILIO_WHATSAPP_NUMBER,
)

HOLDOUT_PERCENT = 20
SIMULATED_LATENCY_MS_RANGE = (50, 400)
SIMULATED_DELIVERY_FAILURE_RATE = 0.06
CHANNEL_COST_MINOR = {"SMS": 20, "WHATSAPP": 15, "VOICE": 80}

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


def provider_for(channel: str, phone_e164: Optional[str]) -> str:
    """The allowlist gate. A number reaches Twilio's real API ONLY if it is

    both on TWILIO_ALLOWLIST and Twilio is configured -- everything else,
    regardless of who's asking, routes to the simulated sender. This is
    the one function that decides "real vs simulated"; nothing downstream
    re-decides it.
    """
    if channel == "EMAIL":
        return "smtp"
    if channel in ("VOICE", "WHATSAPP", "SMS") and phone_e164 and phone_e164 in TWILIO_ALLOWLIST:
        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            return "twilio"
    return "simulated"


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

    this is a clearly-fake stand-in shaped like one. Real integration is a
    drop-in replacement behind this one function once credentials exist.
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
    # Explicit flush: recovery_attempts.recovery_token FK-references this row.
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


@dataclass
class PayloadResult:
    body: str
    recovery_token: Optional[str]
    target_url: Optional[str]


def build_payload(
    session: Session,
    *,
    business_id: uuid.UUID,
    at_risk_id: uuid.UUID,
    attempt_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    loss_category: str,
    channel: str,
    attribution_key_type: str,
    attribution_window_seconds: int,
    now: datetime,
    shared_recovery_token: Optional[str] = None,
) -> PayloadResult:
    """Decision-time payload build: template lookup, token mint, render.

    shared_recovery_token lets a fan-out (VOICE + WHATSAPP from one
    decision) put the SAME link in both messages while only the
    WhatsApp-side recovery_attempts row is ever allowed to own the token
    as its attribution key -- see app/bounds.py's fan-out handling.
    """
    template = _pick_template(session, business_id, channel, loss_category)

    target_url = _simulated_payment_link(uuid.uuid4().hex)
    variables = _entity_link_variables(session, entity_type, entity_id, target_url)

    recovery_token = shared_recovery_token
    if recovery_token is None and attribution_key_type == "TOKEN":
        recovery_token = _mint_token(
            session, business_id=business_id, at_risk_id=at_risk_id, attempt_id=attempt_id,
            target_url=target_url, window_seconds=attribution_window_seconds, now=now,
        )
    if recovery_token:
        variables["link"] = f"/r/{recovery_token}"

    body = _render(template, variables) if template else f"(no approved {channel} template for {loss_category})"
    return PayloadResult(body=body, recovery_token=recovery_token, target_url=target_url)


# ---------------------------------------------------------------------
# Provider sends -- called only by the dispatch worker, never at decision time.
# ---------------------------------------------------------------------

def send_simulated(rng: random.Random, channel: str) -> tuple[str, dict, int, Optional[str]]:
    failed = rng.random() < SIMULATED_DELIVERY_FAILURE_RATE
    latency_ms = rng.randint(*SIMULATED_LATENCY_MS_RANGE)
    status = "FAILED" if failed else "DELIVERED"
    cost_minor = CHANNEL_COST_MINOR.get(channel, 0)
    receipt = {"simulated": True, "channel": channel, "latency_ms": latency_ms}
    return status, receipt, cost_minor, None


def send_email(to_address: str, subject: str, body: str) -> tuple[str, dict, int, Optional[str]]:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM):
        return "SKIPPED_NOT_CONFIGURED", {"simulated": False, "channel": "EMAIL", "reason": "SMTP not configured"}, 0, None
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_address
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_address], msg.as_string())
        return "SENT", {"simulated": False, "channel": "EMAIL", "to": to_address}, 0, None
    except Exception as exc:  # noqa: BLE001 -- a channel send failing is data (delivery_status), not a crash
        return "FAILED", {"simulated": False, "channel": "EMAIL", "error": str(exc)}, 0, None


def send_twilio_voice(to_number: str, script_text: str) -> tuple[str, dict, int, Optional[str]]:
    """Inline TwiML on the call-create request -- no public webhook needed

    for the voice leg, deliberately: a callback URL would need a public
    tunnel to be up for the whole call, the exact dependency that killed
    the Day 6 webhook attempt. <Say> only, no <Gather>; the WhatsApp
    opt-in has to happen via the recipient's own text to Twilio's sandbox
    regardless of anything a keypress could do.
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_VOICE_NUMBER):
        return "SKIPPED_NOT_CONFIGURED", {"simulated": False, "channel": "VOICE", "reason": "Twilio voice not configured"}, 0, None
    try:
        from twilio.rest import Client  # lazy import: app boots fine without the SDK installed
        from xml.sax.saxutils import escape as xml_escape

        twiml = f"<Response><Say voice=\"Polly.Aditi\">{xml_escape(script_text)}</Say></Response>"
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(to=to_number, from_=TWILIO_VOICE_NUMBER, twiml=twiml)
        return "SENT", {"simulated": False, "channel": "VOICE", "call_sid": call.sid, "script": script_text}, CHANNEL_COST_MINOR["VOICE"], call.sid
    except Exception as exc:  # noqa: BLE001 -- provider failure is data, not a crash
        return "FAILED", {"simulated": False, "channel": "VOICE", "error": str(exc)}, 0, None


def send_twilio_whatsapp(to_number: str, body: str) -> tuple[str, dict, int, Optional[str]]:
    """Sandbox constraint, not a bug: Twilio/Meta require the RECIPIENT to

    text the join phrase to the sandbox number before this number can send
    them anything at all. A message attempted before that join lands back
    as a provider error, which the worker treats as a typed, retryable
    failure (RECIPIENT_NOT_OPTED_IN) rather than a permanent one -- it
    resolves itself the moment the person joins, no code change needed.
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER):
        return "SKIPPED_NOT_CONFIGURED", {"simulated": False, "channel": "WHATSAPP", "reason": "Twilio WhatsApp not configured"}, 0, None
    try:
        from twilio.rest import Client  # lazy import

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        to = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
        msg = client.messages.create(to=to, from_=TWILIO_WHATSAPP_NUMBER, body=body)
        return "SENT", {"simulated": False, "channel": "WHATSAPP", "message_sid": msg.sid}, CHANNEL_COST_MINOR["WHATSAPP"], msg.sid
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)
        # Twilio's own error code 63016 / a "not a valid whatsapp" message is
        # the sandbox-opt-in rejection -- distinguish it so the worker backs
        # off and retries instead of dead-lettering a message that will
        # succeed the instant the recipient joins.
        if "63016" in error_text or "not currently opted in" in error_text.lower():
            return "RECIPIENT_NOT_OPTED_IN", {"simulated": False, "channel": "WHATSAPP", "error": error_text}, 0, None
        return "FAILED", {"simulated": False, "channel": "WHATSAPP", "error": error_text}, 0, None


def voice_script_template(*, amount_text: str, reason_text: str) -> str:
    """Deterministic fallback script -- app/live_demo.py's LLM-personalized

    version tries first and falls back to this on no key, an API error, or
    an unverified number, same discipline as app/explain.py's narrative
    generation. No sandbox join-phrase instruction: WhatsApp opt-in is a
    one-time setup step, not part of the recovery flow itself, and this
    script has no way to know whether the specific recipient has already
    done it -- it just tells them to check WhatsApp, which is always true.
    """
    return (
        f"Hello, this is an automated call about a recent payment of {amount_text} that didn't go through, "
        f"because of {reason_text}. We've sent you a WhatsApp message with a simple way to sort this out -- "
        f"just reply yes or no. Thank you."
    )
