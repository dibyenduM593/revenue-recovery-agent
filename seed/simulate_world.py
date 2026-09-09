"""Phase 2: outcomes as a function of decisions.

Never pre-generated, never randomized at attribution time -- either
would make measured lift meaningless (identical treatment/holdout
outcomes, or pure noise). Instead:

    p_recover = clamp(base_propensity * action_fit * timing_fit * channel_fit, 0, 0.95)
    HOLDOUT and suppressed items: all multipliers = 1.0 -> base_propensity only

This module does NOT write recovery_outcomes -- it emits provider-shaped
success events (payment.captured / checkout.completed / invoice.paid)
back through the REAL ingestion endpoints, carrying the recovery_token
where one exists. Attribution (app/recovery/attribution.py) then has to
genuinely find them, the same discipline the cardinal rule applies to
loss events: nothing is written directly to a canonical or ledger table.
"""

import argparse
import io
import json
import random
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import app
from app.canonical.attribution_keys import provider_id_from
from app.canonical.vocabulary import UNRESOLVED_STATUSES
from app.db import SessionLocal
from app.models import Customer, Payment, RecoveryAttempt, RevenueAtRisk, SourceMapping
from seed.generator import GeneratorConfig, business_id_for, latent_propensity

# (loss_category, action) -> action_fit. A handful of pairings are fixed
# by design (EXPIRED_CARD/REQUEST_NEW_INSTRUMENT 2.1x,
# EXPIRED_CARD/RETRY_SCHEDULED 1.0x, etc.) -- EXPIRED_CARD and
# INVALID_DETAILS both resolve to loss_category B3 in this build's
# taxonomy, so both are keyed by B3 here. The rest are this build's own
# extrapolation, keyed by loss_category since most worked examples
# are given per failure_reason but B1/B4/B5 have none. Flagged for
# review, same as the taxonomy and the H-bounds were.
ACTION_FIT: dict[tuple[str, str], float] = {
    ("B3", "REQUEST_NEW_INSTRUMENT"): 2.1,
    ("B3", "RETRY_SCHEDULED"): 1.0,
    ("B3", "RETRY_NOW"): 1.0,
    ("X_INSUFFICIENT_FUNDS", "RETRY_SCHEDULED"): 1.4,
    ("A1", "RETRY_SCHEDULED"): 1.9,
    ("A6", "RETRY_SCHEDULED"): 1.7,
    ("A5", "RETRY_SCHEDULED"): 1.3,
    ("A5", "ESCALATE_HUMAN"): 1.2,
    ("B1", "NUDGE"): 1.8,
    ("B4", "NUDGE"): 1.5,
    ("B4", "ESCALATE_HUMAN"): 1.2,
    ("B2", "RECOLLECT_MANDATE"): 1.6,
    ("B2", "RETRY_SCHEDULED"): 1.0,
    ("B5", "NUDGE"): 0.9,  # worse than nothing -- only reachable via a deliberately bad policy variant
}
DEFAULT_ACTION_FIT = 1.0

CHANNEL_FIT = {"SMS": 1.15, "WHATSAPP": 1.2, "EMAIL": 1.0, "VOICE": 0.95}
DEFAULT_CHANNEL_FIT = 1.0

MAX_P_RECOVER = 0.95

# latent_propensity() is uniform[0,1) with mean 0.5 -- it exists to give each
# customer a STABLE, re-derivable latent tendency, not to itself be a
# realistic organic recovery rate. Scaled down so the holdout's organic rate
# lands in a believable ~15% range and boosted (treatment) rates land in a
# believable ~25-45% range, rather than every boosted case saturating at
# the 0.95 cap.
BASE_PROPENSITY_SCALE = 0.30


def _timing_fit(attempt_number: int) -> float:
    return max(0.85, 1.1 - 0.1 * (attempt_number - 1))


def compute_p_recover(*, base_propensity: float, loss_category: str, action: str,
                       channel: str | None, attempt_number: int, boosted: bool) -> float:
    base_propensity = base_propensity * BASE_PROPENSITY_SCALE
    if not boosted:
        return min(base_propensity, MAX_P_RECOVER)
    action_fit = ACTION_FIT.get((loss_category, action), DEFAULT_ACTION_FIT)
    channel_fit = CHANNEL_FIT.get(channel, DEFAULT_CHANNEL_FIT) if channel else DEFAULT_CHANNEL_FIT
    timing_fit = _timing_fit(attempt_number)
    return min(base_propensity * action_fit * timing_fit * channel_fit, MAX_P_RECOVER)


def _provider_entity_id(session, business_id: uuid.UUID, provider: str, entity_kind: str, internal_id: uuid.UUID) -> str | None:
    return session.execute(
        select(SourceMapping.source_object_id).where(
            SourceMapping.business_id == business_id, SourceMapping.source_provider == provider,
            SourceMapping.source_object_type == entity_kind, SourceMapping.internal_id == internal_id,
        )
    ).scalar_one_or_none()


def _event(business_id: uuid.UUID, source_provider: str, source_type: str, occurred_at: datetime,
           payload: dict, external_event_id: str) -> dict:
    return {
        "business_id": str(business_id), "source_provider": source_provider, "source_type": source_type,
        "external_event_id": external_event_id, "occurred_at": occurred_at.isoformat(), "payload": payload,
    }


def _payment_recovery_event(business_id, order_id: str, amount: int, currency: str,
                             email: str, phone: str, recovered_at: datetime, recovery_token: str | None) -> dict:
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    entity = {
        "id": payment_id, "entity": "payment", "amount": amount, "currency": currency, "status": "captured",
        "order_id": order_id, "method": "card", "email": email, "contact": phone, "notes": [],
        "captured": True, "amount_refunded": 0, "international": False,
        "error_code": None, "error_description": None, "error_source": None, "error_step": None, "error_reason": None,
        "created_at": int(recovered_at.timestamp()),
    }
    if recovery_token:
        entity["recovery_token"] = recovery_token
    webhook = {"entity": "event", "event": "payment.captured", "contains": ["payment"],
               "payload": {"payment": {"entity": entity}}, "created_at": int(recovered_at.timestamp())}
    return _event(business_id, "razorpay", "webhook", recovered_at, webhook, f"evt_{payment_id}")


def _checkout_recovery_event(business_id, session_id: str, amount: int, currency: str,
                              email: str, recovered_at: datetime, recovery_token: str | None) -> dict:
    data = {"sessionId": session_id, "cartValue": amount, "curr": currency, "shopperEmail": email,
            "ts": int(recovered_at.timestamp() * 1000)}
    if recovery_token:
        data["recoveryToken"] = recovery_token
    payload = {"event": "checkout.completed", "data": data}
    return _event(business_id, "storefront", "webhook", recovered_at, payload, f"evt_{session_id}_recovered")


def _invoice_recovery_event(business_id, invoice_id: str, amount: int, currency: str,
                             email: str, phone: str, due_by: int, recovered_at: datetime) -> dict:
    entity = {
        "id": invoice_id, "entity": "invoice", "amount": amount, "currency": currency, "status": "paid",
        "due_by": due_by, "customer_details": {"email": email, "contact": phone},
        "created_at": int(recovered_at.timestamp()),
    }
    webhook = {"entity": "event", "event": "invoice.paid", "contains": ["invoice"],
               "payload": {"invoice": {"entity": entity}}, "created_at": int(recovered_at.timestamp())}
    return _event(business_id, "razorpay", "webhook", recovered_at, webhook, f"evt_{invoice_id}_recovered")


def simulate(session, business_id: uuid.UUID, *, seed: int) -> dict:
    # Local import: app.live_demo reaches back into seed.generator, so a
    # module-level import here closes the loop.
    from app.live_demo import live_demo_at_risk_ids

    rng = random.Random(seed ^ 0x517)
    cfg = GeneratorConfig(seed=seed)

    # A planted walk-up item belongs to the human holding the phone, and
    # this function must never answer on their behalf. It would otherwise
    # emit a success event carrying that row's REAL nudge token, which
    # attribution then credits TOKEN_CLICK/STRONG -- indistinguishable
    # from the person actually replying, except for a recovered_at landing
    # somewhere random inside the 3-day window (often days out). Because
    # the dashboard fires this a few seconds after launch, the simulator
    # beat the human essentially every time, and the real YES then found
    # the item already resolved and did nothing. Same boundary
    # app/recovery/report.py already draws around these rows.
    excluded = live_demo_at_risk_ids(session, business_id)

    rows = session.execute(
        select(RevenueAtRisk)
        .where(
            RevenueAtRisk.business_id == business_id,
            RevenueAtRisk.status.in_(UNRESOLVED_STATUSES),
        )
        # Explicit, fully deterministic order: this loop draws from `rng`
        # once per row, so an unordered SELECT (Postgres makes no promise
        # about row order without ORDER BY) would consume the same draws
        # against a different row each run -- same bug class as
        # run_batch()'s ORDER BY tie, just via RNG sequence instead of LIMIT.
        .order_by(RevenueAtRisk.entity_type, RevenueAtRisk.entity_id)
    ).scalars().all()

    events: list[dict] = []
    stats = {
        "considered": 0, "boosted": 0, "base_only": 0, "recovered": 0,
        "skipped_no_customer": 0, "skipped_live_demo": 0,
    }

    for at_risk in rows:
        if at_risk.at_risk_id in excluded:
            stats["skipped_live_demo"] += 1
            continue

        attempt = session.execute(
            select(RecoveryAttempt)
            .where(RecoveryAttempt.at_risk_id == at_risk.at_risk_id)
            .order_by(RecoveryAttempt.attempt_number.desc())
            .limit(1)
        ).scalars().first()
        if attempt is None:
            continue

        customer = session.get(Customer, at_risk.customer_id) if at_risk.customer_id else None
        if customer is None or not customer.email:
            stats["skipped_no_customer"] += 1
            continue

        boosted = attempt.executed_at is not None and attempt.cohort == "TREATMENT"
        base_propensity = latent_propensity(customer.email, cfg.seed)
        p_recover = compute_p_recover(
            base_propensity=base_propensity, loss_category=at_risk.loss_category, action=attempt.strategy,
            channel=attempt.channel, attempt_number=attempt.attempt_number, boosted=boosted,
        )
        stats["considered"] += 1
        stats["boosted" if boosted else "base_only"] += 1

        if rng.random() >= p_recover:
            continue

        # executed_at, not decided_at, when a message actually went out --
        # since the outbound queue, a customer can't respond to something
        # before it was actually sent, and decided_at can now be well
        # before that if the queue was backed up. Falls back to decided_at
        # for un-executed (holdout/suppressed) rows, where it's still the
        # only sensible anchor for the organic-recovery observation window.
        window_start = attempt.executed_at or attempt.decided_at
        window_end = attempt.attribution_expires_at
        span = max((window_end - window_start).total_seconds(), 60)
        recovered_at = window_start + timedelta(seconds=rng.uniform(span * 0.05, span * 0.85))
        recovery_token = attempt.recovery_token  # None for holdout/suppressed -- correctly forces WEAK inference only

        if at_risk.entity_type == "PAYMENT":
            # The provider half of the composite key, never the whole
            # string: this used to read attribution_key_value raw, which
            # is a UUID, and emitted success events carrying an order_id
            # no provider ever issued -- so attribution never matched them.
            payment_intent_id = (
                provider_id_from(attempt.attribution_key_value)
                if attempt.attribution_key_type == "PAYMENT_INTENT"
                else None
            )
            if payment_intent_id is None:
                # TOKEN-attributed: need the payment_intent_id, not the token, to build order_id
                payment = session.get(Payment, at_risk.entity_id)
                payment_intent_id = payment.payment_intent_id if payment else None
            if payment_intent_id is None:
                continue
            events.append(_payment_recovery_event(
                business_id, payment_intent_id, at_risk.at_risk_minor, at_risk.currency,
                customer.email, customer.phone_e164 or "", recovered_at, recovery_token,
            ))
        elif at_risk.entity_type == "CHECKOUT":
            session_id = _provider_entity_id(session, business_id, "storefront", "checkout", at_risk.entity_id)
            if session_id is None:
                continue
            events.append(_checkout_recovery_event(
                business_id, session_id, at_risk.at_risk_minor, at_risk.currency, customer.email, recovered_at, recovery_token
            ))
        elif at_risk.entity_type == "INVOICE":
            invoice_id = _provider_entity_id(session, business_id, "razorpay", "invoice", at_risk.entity_id)
            if invoice_id is None:
                continue
            events.append(_invoice_recovery_event(
                business_id, invoice_id, at_risk.at_risk_minor, at_risk.currency,
                customer.email, customer.phone_e164 or "", int(recovered_at.timestamp()), recovered_at,
            ))
        elif at_risk.entity_type == "SUBSCRIPTION":
            events.append(_payment_recovery_event(
                business_id, f"order_{uuid.uuid4().hex[:10]}", at_risk.at_risk_minor, at_risk.currency,
                customer.email, customer.phone_e164 or "", recovered_at, None,
            ))
        stats["recovered"] += 1

    if events:
        client = TestClient(app)
        body = "\n".join(json.dumps(e) for e in events).encode("utf-8")
        resp = client.post("/v1/imports", files={"file": ("outcomes.jsonl", io.BytesIO(body), "application/jsonl")})
        resp.raise_for_status()
        stats["delivered"] = resp.json()
    else:
        stats["delivered"] = {"received": 0, "inserted": 0, "duplicates": 0}

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate outcomes for decided-but-unresolved at-risk records.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-drain", dest="drain", action="store_false", default=True)
    args = parser.parse_args()

    business_id = business_id_for(GeneratorConfig(seed=args.seed))
    session = SessionLocal()
    try:
        stats = simulate(session, business_id, seed=args.seed)
        session.commit()
    finally:
        session.close()

    print(f"business_id: {business_id}")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    if args.drain:
        from app.worker import drain

        print(f"worker drain: {drain()}")


if __name__ == "__main__":
    main()
