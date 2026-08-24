"""Synthetic provider-shaped event generator.

Produces a deterministic, seeded stream of raw events shaped the way real
providers actually send them: Razorpay-style webhooks for payments,
invoices, and subscriptions (amounts in paise, unix timestamps, Razorpay's
own error vocabulary), plus a second, differently-shaped "storefront"
source for checkout sessions (camelCase fields, millisecond timestamps).
None of this is canonical. Mapping this mess into the canonical schema is
the normalize pipeline's job (app/normalize/), not this generator's.

Each attempt against a payment order gets its own payment_id, exactly like
real Razorpay, with no explicit attempt-number field. Recovering attempt
order from timestamps is deliberate: it is what the structural mapping
stage has to do against real data too.

Latent recovery propensity per customer is never written into the output.
It is a deterministic function of (email, seed), see latent_propensity()
below, so app/recovery/attribution.py can recompute the same ground truth
later for honest outcome simulation without it ever appearing in a raw
payload, the way it never would from a real provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.canonical.vocabulary import RETRY_POLICY, FailureReason

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "revenue-recovery.internal")

ID_ALPHABET = string.ascii_letters + string.digits


def latent_propensity(email: str, seed: int) -> float:
    """Deterministic pseudo-random recovery propensity in [0, 1) for one customer."""
    digest = hashlib.sha256(f"{seed}:{email}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _rand_id(rng: random.Random, prefix: str, length: int = 14) -> str:
    return f"{prefix}_{''.join(rng.choices(ID_ALPHABET, k=length))}"


def _unix(dt: datetime) -> int:
    return int(dt.timestamp())


def _unix_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True)
class FailureCode:
    error_code: str
    error_reason: str
    error_description: str
    error_source: str
    canonical: FailureReason


FAILURE_CODES: list[FailureCode] = [
    FailureCode("BAD_REQUEST_ERROR", "insufficient_funds",
                "Payment failed due to insufficient funds in the customer's account.",
                "bank", FailureReason.INSUFFICIENT_FUNDS),
    FailureCode("GATEWAY_ERROR", "issuer_unavailable",
                "The card issuing bank or network could not be reached.",
                "bank", FailureReason.ISSUER_UNAVAILABLE),
    FailureCode("BAD_REQUEST_ERROR", "expired_card",
                "The card has expired.",
                "customer", FailureReason.EXPIRED_CARD),
    FailureCode("BAD_REQUEST_ERROR", "payment_declined",
                "The card issuer declined the payment.",
                "bank", FailureReason.DO_NOT_HONOR),
    FailureCode("BAD_REQUEST_ERROR", "restricted_card",
                "The card has been reported lost or stolen.",
                "bank", FailureReason.STOLEN_CARD),
    FailureCode("BAD_REQUEST_ERROR", "incorrect_card_details",
                "The card number, expiry date, or CVV is invalid.",
                "customer", FailureReason.INVALID_DETAILS),
    FailureCode("SERVER_ERROR", "processing_error",
                "An internal processing error occurred while contacting the bank.",
                "gateway", FailureReason.TECHNICAL_ERROR),
]

PAYMENT_METHODS = ["card", "upi", "netbanking", "wallet"]


DEFAULT_ANCHOR = datetime(2026, 8, 25, tzinfo=timezone.utc)


@dataclass
class GeneratorConfig:
    seed: int = 42
    days: int = 30
    anchor: datetime = DEFAULT_ANCHOR
    business_name: str = "Demo Business"
    currency: str = "INR"
    min_amount_minor: int = 9_900
    max_amount_minor: int = 250_000

    n_customers: int = 250

    n_payment_intents: int = 400
    payment_failure_rate: float = 0.55

    n_checkouts: int = 150
    checkout_abandon_rate: float = 0.35

    n_invoices: int = 80
    invoice_overdue_rate: float = 0.30

    n_subscriptions: int = 60
    subscription_failure_rate: float = 0.25
    mandate_revoke_rate: float = 0.08

    refund_rate: float = 0.03

    failure_weights: dict[FailureReason, float] = field(
        default_factory=lambda: {code.canonical: 1.0 for code in FAILURE_CODES}
    )


@dataclass
class Customer:
    index: int
    email: str
    name: str
    phone: str


def business_id_for(cfg: GeneratorConfig) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{cfg.seed}:business")


def _make_customers(cfg: GeneratorConfig, rng: random.Random) -> list[Customer]:
    customers = []
    for i in range(cfg.n_customers):
        email = f"customer{i:05d}@example.com"
        customers.append(
            Customer(
                index=i,
                email=email,
                name=f"Customer {i:05d}",
                phone=f"+9198{rng.randint(10_000_000, 99_999_999)}",
            )
        )
    return customers


def _pick_failure(cfg: GeneratorConfig, rng: random.Random) -> FailureCode:
    weights = [cfg.failure_weights.get(code.canonical, 1.0) for code in FAILURE_CODES]
    return rng.choices(FAILURE_CODES, weights=weights, k=1)[0]


def _random_time(cfg: GeneratorConfig, rng: random.Random, start: datetime) -> datetime:
    return start + timedelta(seconds=rng.uniform(0, cfg.days * 86_400))


def _event(business_id: uuid.UUID, source_provider: str, source_type: str,
           occurred_at: datetime, payload: dict, external_event_id: str) -> dict:
    return {
        "business_id": str(business_id),
        "source_provider": source_provider,
        "source_type": source_type,
        "external_event_id": external_event_id,
        "occurred_at": occurred_at.isoformat(),
        "payload": payload,
    }


def _payment_entity(rng: random.Random, payment_id: str, order_id: str, amount: int,
                     currency: str, customer: Customer, status: str,
                     created_at: datetime, failure: FailureCode | None,
                     subscription_id: str | None) -> dict:
    entity = {
        "id": payment_id,
        "entity": "payment",
        "amount": amount,
        "currency": currency,
        "status": status,
        "order_id": order_id,
        "invoice_id": None,
        "international": False,
        "method": rng.choice(PAYMENT_METHODS),
        "amount_refunded": 0,
        "captured": status == "captured",
        "email": customer.email,
        "contact": customer.phone,
        "notes": [],
        "created_at": _unix(created_at),
    }
    if subscription_id:
        entity["subscription_id"] = subscription_id
    if failure is not None:
        entity.update(
            error_code=failure.error_code,
            error_description=failure.error_description,
            error_source=failure.error_source,
            error_step="payment_authorization",
            error_reason=failure.error_reason,
        )
    else:
        entity.update(error_code=None, error_description=None, error_source=None,
                       error_step=None, error_reason=None)
    return entity


def _webhook(event_name: str, entity_key: str, entity: dict) -> dict:
    return {
        "entity": "event",
        "event": event_name,
        "contains": [entity_key],
        "payload": {entity_key: {"entity": entity}},
        "created_at": entity["created_at"],
    }


def _generate_payment_intents(cfg: GeneratorConfig, rng: random.Random,
                               customers: list[Customer], business_id: uuid.UUID,
                               start: datetime) -> tuple[list[dict], list[dict]]:
    """Returns (events, successful_payment_entities) for refund sourcing."""
    events: list[dict] = []
    successes: list[dict] = []

    for i in range(cfg.n_payment_intents):
        customer = rng.choice(customers)
        order_id = _rand_id(rng, "order")
        amount = round(rng.randint(cfg.min_amount_minor, cfg.max_amount_minor), -2)
        t = _random_time(cfg, rng, start)
        will_fail = rng.random() < cfg.payment_failure_rate

        if not will_fail:
            payment_id = _rand_id(rng, "pay")
            entity = _payment_entity(rng, payment_id, order_id, amount, cfg.currency,
                                      customer, "captured", t, None, None)
            events.append(_event(business_id, "razorpay", "webhook", t,
                                  _webhook("payment.captured", "payment", entity),
                                  f"evt_{payment_id}"))
            successes.append(entity)
            continue

        failure = _pick_failure(cfg, rng)
        policy = RETRY_POLICY[failure.canonical]
        recovers = policy.retryable and rng.random() < 0.4
        max_attempts = policy.max_attempts if policy.retryable else 1
        n_attempts = rng.randint(1, max_attempts) if policy.retryable else 1

        for attempt in range(n_attempts):
            payment_id = _rand_id(rng, "pay")
            attempt_time = t + timedelta(hours=attempt * rng.uniform(4, 30))
            is_last = attempt == n_attempts - 1
            status = "captured" if (is_last and recovers) else "failed"
            entity_failure = None if status == "captured" else failure
            entity = _payment_entity(rng, payment_id, order_id, amount, cfg.currency,
                                      customer, status, attempt_time, entity_failure, None)
            event_name = "payment.captured" if status == "captured" else "payment.failed"
            events.append(_event(business_id, "razorpay", "webhook", attempt_time,
                                  _webhook(event_name, "payment", entity),
                                  f"evt_{payment_id}"))
            if status == "captured":
                successes.append(entity)

    return events, successes


def _generate_checkouts(cfg: GeneratorConfig, rng: random.Random,
                         customers: list[Customer], business_id: uuid.UUID,
                         start: datetime) -> list[dict]:
    events: list[dict] = []
    for i in range(cfg.n_checkouts):
        customer = rng.choice(customers)
        session_id = _rand_id(rng, "cs")
        amount = round(rng.randint(cfg.min_amount_minor, cfg.max_amount_minor), -2)
        t = _random_time(cfg, rng, start)

        started_payload = {
            "sessionId": session_id,
            "cartValue": amount,
            "curr": cfg.currency,
            "shopperEmail": customer.email,
            "ts": _unix_ms(t),
        }
        events.append(_event(business_id, "storefront", "checkout_event", t,
                              {"event": "checkout.started", "data": started_payload},
                              f"evt_{session_id}_started"))

        if rng.random() >= cfg.checkout_abandon_rate:
            completed_at = t + timedelta(minutes=rng.uniform(1, 20))
            completed_payload = {
                "sessionId": session_id,
                "cartValue": amount,
                "curr": cfg.currency,
                "shopperEmail": customer.email,
                "ts": _unix_ms(completed_at),
            }
            events.append(_event(business_id, "storefront", "checkout_event", completed_at,
                                  {"event": "checkout.completed", "data": completed_payload},
                                  f"evt_{session_id}_completed"))
    return events


def _generate_invoices(cfg: GeneratorConfig, rng: random.Random,
                        customers: list[Customer], business_id: uuid.UUID,
                        start: datetime) -> list[dict]:
    events: list[dict] = []
    for i in range(cfg.n_invoices):
        customer = rng.choice(customers)
        invoice_id = _rand_id(rng, "inv")
        amount = round(rng.randint(cfg.min_amount_minor, cfg.max_amount_minor), -2)
        issued_at = _random_time(cfg, rng, start)
        due_at = issued_at + timedelta(days=rng.randint(3, 14))
        overdue = rng.random() < cfg.invoice_overdue_rate

        entity = {
            "id": invoice_id,
            "entity": "invoice",
            "amount": amount,
            "currency": cfg.currency,
            "status": "issued",
            "due_by": _unix(due_at),
            "customer_details": {"email": customer.email, "contact": customer.phone},
            "created_at": _unix(issued_at),
        }
        events.append(_event(business_id, "razorpay", "webhook", issued_at,
                              _webhook("invoice.issued", "invoice", entity),
                              f"evt_{invoice_id}_issued"))

        if overdue:
            expired_entity = {**entity, "status": "expired"}
            events.append(_event(business_id, "razorpay", "webhook", due_at,
                                  _webhook("invoice.expired", "invoice", expired_entity),
                                  f"evt_{invoice_id}_expired"))
        else:
            paid_at = due_at - timedelta(days=rng.uniform(0.5, 3))
            paid_entity = {**entity, "status": "paid"}
            events.append(_event(business_id, "razorpay", "webhook", paid_at,
                                  _webhook("invoice.paid", "invoice", paid_entity),
                                  f"evt_{invoice_id}_paid"))
    return events


def _generate_subscriptions(cfg: GeneratorConfig, rng: random.Random,
                             customers: list[Customer], business_id: uuid.UUID,
                             start: datetime) -> list[dict]:
    events: list[dict] = []
    subs_customers = rng.sample(customers, k=min(cfg.n_subscriptions, len(customers)))

    for customer in subs_customers:
        subscription_id = _rand_id(rng, "sub")
        amount = round(rng.randint(cfg.min_amount_minor, cfg.max_amount_minor), -2)
        t = _random_time(cfg, rng, start)
        fails = rng.random() < cfg.subscription_failure_rate

        if not fails:
            payment_id = _rand_id(rng, "pay")
            entity = _payment_entity(rng, payment_id, _rand_id(rng, "order"), amount,
                                      cfg.currency, customer, "captured", t, None,
                                      subscription_id)
            events.append(_event(business_id, "razorpay", "webhook", t,
                                  _webhook("payment.captured", "payment", entity),
                                  f"evt_{payment_id}"))
            continue

        failure = _pick_failure(cfg, rng)
        payment_id = _rand_id(rng, "pay")
        entity = _payment_entity(rng, payment_id, _rand_id(rng, "order"), amount,
                                  cfg.currency, customer, "failed", t, failure,
                                  subscription_id)
        events.append(_event(business_id, "razorpay", "webhook", t,
                              _webhook("payment.failed", "payment", entity),
                              f"evt_{payment_id}"))

        if rng.random() < cfg.mandate_revoke_rate:
            halted_at = t + timedelta(hours=rng.uniform(1, 48))
            sub_entity = {
                "id": subscription_id,
                "entity": "subscription",
                "status": "halted",
                "customer_id": customer.email,
                "created_at": _unix(halted_at),
            }
            events.append(_event(business_id, "razorpay", "webhook", halted_at,
                                  _webhook("subscription.halted", "subscription", sub_entity),
                                  f"evt_{subscription_id}_halted"))
    return events


def _generate_refunds(cfg: GeneratorConfig, rng: random.Random,
                       successes: list[dict], business_id: uuid.UUID) -> list[dict]:
    events: list[dict] = []
    for entity in successes:
        if rng.random() >= cfg.refund_rate:
            continue
        refund_at = datetime.fromtimestamp(entity["created_at"], tz=timezone.utc) + \
            timedelta(days=rng.uniform(1, 10))
        refund_id = _rand_id(rng, "rfnd")
        refund_entity = {
            "id": refund_id,
            "entity": "refund",
            "amount": entity["amount"],
            "currency": entity["currency"],
            "payment_id": entity["id"],
            "status": "processed",
            "created_at": _unix(refund_at),
        }
        events.append(_event(business_id, "razorpay", "webhook", refund_at,
                              _webhook("refund.processed", "refund", refund_entity),
                              f"evt_{refund_id}"))
    return events


def generate_dataset(cfg: GeneratorConfig) -> list[dict]:
    rng = random.Random(cfg.seed)
    business_id = business_id_for(cfg)
    start = cfg.anchor - timedelta(days=cfg.days)
    customers = _make_customers(cfg, rng)

    payment_events, successes = _generate_payment_intents(cfg, rng, customers, business_id, start)
    checkout_events = _generate_checkouts(cfg, rng, customers, business_id, start)
    invoice_events = _generate_invoices(cfg, rng, customers, business_id, start)
    subscription_events = _generate_subscriptions(cfg, rng, customers, business_id, start)
    refund_events = _generate_refunds(cfg, rng, successes, business_id)

    events = payment_events + checkout_events + invoice_events + subscription_events + refund_events
    events.sort(key=lambda e: e["occurred_at"])
    return events


def summarize(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        key = f'{e["source_provider"]}:{e["payload"].get("event", e["payload"].get("event"))}'
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def write_jsonl(events: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic provider-shaped events.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--customers", type=int, default=250)
    parser.add_argument("--payment-intents", type=int, default=400)
    parser.add_argument("--checkouts", type=int, default=150)
    parser.add_argument("--invoices", type=int, default=80)
    parser.add_argument("--subscriptions", type=int, default=60)
    parser.add_argument("--out", type=Path, default=Path("seed/output/events.jsonl"))
    args = parser.parse_args()

    cfg = GeneratorConfig(
        seed=args.seed,
        days=args.days,
        n_customers=args.customers,
        n_payment_intents=args.payment_intents,
        n_checkouts=args.checkouts,
        n_invoices=args.invoices,
        n_subscriptions=args.subscriptions,
    )

    events = generate_dataset(cfg)
    write_jsonl(events, args.out)

    print(f"business_id: {business_id_for(cfg)}")
    print(f"wrote {len(events)} events to {args.out}")
    for key, count in summarize(events).items():
        print(f"  {key}: {count}")


if __name__ == "__main__":
    main()
