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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.canonical.vocabulary import FAILURE_TAXONOMY, FailureReason

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "revenue-recovery.internal")

ID_ALPHABET = string.ascii_letters + string.digits


def _hash01(key: str) -> float:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _weighted_pick(items: list[str], weights: list[float], u: float) -> str:
    """Turn a uniform [0,1) draw into a categorical choice, deterministically."""
    total = sum(weights)
    acc = 0.0
    threshold = u * total
    for item, weight in zip(items, weights):
        acc += weight
        if threshold < acc:
            return item
    return items[-1]


# 7 archetypes: structured shape of a customer's behavioural history, not a
# per-customer coin flip. Weights sum to 1.0.
ARCHETYPES = ["reliable", "stable_late", "seasonal", "gradual_decliner",
              "sudden_shock", "volatile", "recovering"]
ARCHETYPE_WEIGHTS = [0.30, 0.15, 0.15, 0.12, 0.10, 0.10, 0.08]
ARCHETYPE_BASE: dict[str, float] = {
    "reliable": 0.75,
    "stable_late": 0.55,
    "seasonal": 0.50,
    "gradual_decliner": 0.35,
    "sudden_shock": 0.30,
    "volatile": 0.40,
    "recovering": 0.60,
}

B2C_SEGMENTS = ["new", "casual", "regular", "loyal"]
B2C_SEGMENT_WEIGHTS = [0.30, 0.30, 0.25, 0.15]
B2B_SEGMENTS = ["micro", "sme", "mid", "enterprise"]
B2B_SEGMENT_WEIGHTS = [0.35, 0.35, 0.20, 0.10]
# SME is evidenced far worse than the other B2B segments -- everything else
# clusters close to 1.0.
SEGMENT_MULT: dict[str, float] = {
    "new": 0.85, "casual": 0.95, "regular": 1.05, "loyal": 1.20,
    "micro": 0.80, "sme": 0.55, "mid": 1.00, "enterprise": 1.20,
}

SECTORS = ["retail", "saas", "services", "logistics", "healthcare", "education"]
SECTOR_WEIGHTS = [0.30, 0.15, 0.20, 0.15, 0.10, 0.10]
SECTOR_MULT: dict[str, float] = {
    "retail": 1.00, "saas": 1.10, "services": 0.95,
    "logistics": 0.90, "healthcare": 1.05, "education": 1.00,
}

BUSINESS_MODELS = ["product", "service"]
BUSINESS_MODEL_WEIGHTS = [0.60, 0.40]
BUSINESS_MODEL_MULT: dict[str, float] = {"product": 1.00, "service": 0.95}

CUSTOMER_TYPE_B2C_RATE = 0.70


@dataclass(frozen=True)
class CustomerProfile:
    customer_type: str
    segment: str
    sector: str
    business_model: str
    archetype: str


def customer_profile_for(email: str, seed: int) -> CustomerProfile:
    """Deterministic function of (email, seed) -- recomputable anywhere
    (training export, runtime scoring) without ever having been written
    into a raw provider payload, same discipline as latent_propensity().
    """
    customer_type = "B2C" if _hash01(f"{seed}:{email}:type") < CUSTOMER_TYPE_B2C_RATE else "B2B"
    if customer_type == "B2C":
        segment = _weighted_pick(B2C_SEGMENTS, B2C_SEGMENT_WEIGHTS, _hash01(f"{seed}:{email}:segment"))
    else:
        segment = _weighted_pick(B2B_SEGMENTS, B2B_SEGMENT_WEIGHTS, _hash01(f"{seed}:{email}:segment"))
    sector = _weighted_pick(SECTORS, SECTOR_WEIGHTS, _hash01(f"{seed}:{email}:sector"))
    business_model = _weighted_pick(BUSINESS_MODELS, BUSINESS_MODEL_WEIGHTS, _hash01(f"{seed}:{email}:model"))
    archetype = _weighted_pick(ARCHETYPES, ARCHETYPE_WEIGHTS, _hash01(f"{seed}:{email}:archetype"))
    return CustomerProfile(customer_type, segment, sector, business_model, archetype)


def latent_propensity(email: str, seed: int) -> float:
    """Deterministic recovery propensity in [0, 1) for one customer.

    Decomposed into a structured component (archetype x segment x sector x
    business_model -- all observable) and a hidden component (the "life
    circumstance" v3 SS3 wants unlearnable). A hash of the email alone has
    zero mutual information with any observable feature; training a model
    against that would learn nothing but action_fit/timing_fit and call it
    a score. The hidden term stays, but as ~40% of the total, not all of it.
    """
    profile = customer_profile_for(email, seed)
    base = ARCHETYPE_BASE[profile.archetype]
    base *= SEGMENT_MULT[profile.segment]
    base *= SECTOR_MULT[profile.sector]
    base *= BUSINESS_MODEL_MULT[profile.business_model]
    hidden = _hash01(f"{seed}:{email}:hidden")
    return _clamp(base * (0.75 + 0.5 * hidden), 0.02, 0.95)


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
    weight: float  # fraction of all failed payments in the target failure mix


# Two entries share canonical=DO_NOT_HONOR on purpose: the SAME provider
# decline code ("payment_declined") arrives with error_source='bank' most of
# the time and error_source='business' rarely -- that distinction, not the
# failure_reason, is what A5 (false decline) actually depends on. Weights
# sum to 1.0, matching the target failure mix exactly.
FAILURE_CODES: list[FailureCode] = [
    FailureCode("BAD_REQUEST_ERROR", "insufficient_funds",
                "Payment failed due to insufficient funds in the customer's account.",
                "bank", FailureReason.INSUFFICIENT_FUNDS, 0.32),
    FailureCode("GATEWAY_ERROR", "issuer_unavailable",
                "The card issuing bank or network could not be reached.",
                "bank", FailureReason.ISSUER_UNAVAILABLE, 0.14),
    FailureCode("SERVER_ERROR", "processing_error",
                "An internal processing error occurred while contacting the bank.",
                "gateway", FailureReason.TECHNICAL_ERROR, 0.11),
    FailureCode("BAD_REQUEST_ERROR", "expired_card",
                "The card has expired.",
                "customer", FailureReason.EXPIRED_CARD, 0.10),
    FailureCode("BAD_REQUEST_ERROR", "payment_declined",
                "The card issuer declined the payment.",
                "bank", FailureReason.DO_NOT_HONOR, 0.09),
    FailureCode("BAD_REQUEST_ERROR", "incorrect_otp",
                "The OTP or PIN entered was incorrect.",
                "customer", FailureReason.ATTENTION_SLIP, 0.08),
    FailureCode("BAD_REQUEST_ERROR", "incorrect_card_details",
                "The card number, expiry date, or CVV is invalid.",
                "customer", FailureReason.INVALID_DETAILS, 0.06),
    FailureCode("BAD_REQUEST_ERROR", "mandate_revoked",
                "The payment mandate backing this charge has been revoked.",
                "customer", FailureReason.MANDATE_REVOKED, 0.05),
    FailureCode("BAD_REQUEST_ERROR", "restricted_card",
                "The card has been reported lost or stolen.",
                "bank", FailureReason.STOLEN_CARD, 0.03),
    FailureCode("BAD_REQUEST_ERROR", "payment_declined",
                "The card issuer declined the payment.",
                "business", FailureReason.DO_NOT_HONOR, 0.02),
]

PAYMENT_METHODS = ["card", "upi", "netbanking", "wallet"]

ISSUING_BANKS = ["HDFC Bank", "ICICI Bank", "State Bank of India", "Axis Bank", "Kotak Mahindra Bank", "Yes Bank"]
CARD_NETWORKS = ["Visa", "MasterCard", "RuPay", "Amex"]
UPI_PSPS = ["okhdfcbank", "oksbi", "okicici", "okaxis", "ybl", "paytm"]
WALLETS = ["paytm", "phonepe", "amazonpay", "mobikwik"]


def _instrument_fields(rng: random.Random, method: str, customer: Customer) -> dict:
    """Payment-method-specific fields, mirroring what Razorpay's real
    payment.entity actually carries per method: a nested `card` object for
    card payments, a bare `bank` code for netbanking, `vpa` for UPI,
    `wallet` for wallet. issuer_bank (A1 bank-degradation detection) only
    has a meaningful value for card and netbanking -- UPI and wallet
    payments route through a PSP/wallet provider, not a specific bank.
    """
    if method == "card":
        return {
            "card": {
                "issuer": rng.choice(ISSUING_BANKS),
                "network": rng.choice(CARD_NETWORKS),
                "last4": f"{rng.randint(0, 9999):04d}",
                "expiry_month": rng.randint(1, 12),
                "expiry_year": rng.randint(2027, 2032),
            }
        }
    if method == "netbanking":
        return {"bank": rng.choice(ISSUING_BANKS)}
    if method == "upi":
        local_part = customer.email.split("@")[0]
        return {"vpa": f"{local_part}@{rng.choice(UPI_PSPS)}"}
    if method == "wallet":
        return {"wallet": rng.choice(WALLETS)}
    return {}


DEFAULT_ANCHOR = datetime(2026, 8, 25, tzinfo=timezone.utc)


@dataclass
class GeneratorConfig:
    seed: int = 42
    # business_id_for() keys ONLY off `seed` -- DEMO_BUSINESS_ID (settings.py)
    # and every downstream stage (sweeps, recovery.run, simulate_world) all
    # resolve the business from a fixed --seed, so it must stay stable.
    # content_seed drives everything else (which customer gets which
    # failure, which amount, which instrument): leave it None for the
    # historical "one seed controls everything" behavior the demo's
    # determinism check relies on, or set it independently so the
    # dashboard's orchestrator button can generate genuinely different
    # transaction data on every click without ever changing which business
    # the data belongs to.
    content_seed: int | None = None
    days: int = 30
    anchor: datetime = DEFAULT_ANCHOR
    business_name: str = "Demo Business"
    currency: str = "INR"
    min_amount_minor: int = 9_900
    max_amount_minor: int = 250_000
    # ~4% of amounts land here instead -- the tail that makes
    # human_approval_above_minor (Rs 50,000 default) actually exercisable.
    high_value_rate: float = 0.04
    high_value_min_minor: int = 5_000_000
    high_value_max_minor: int = 20_000_000

    n_customers: int = 800

    n_payment_intents: int = 1_300
    payment_failure_rate: float = 0.55
    dispute_rate: float = 0.02  # share of captured payments that also get disputed

    n_checkouts: int = 480
    checkout_abandon_rate: float = 0.35

    n_invoices: int = 240
    invoice_overdue_rate: float = 0.30

    n_subscriptions: int = 160
    subscription_failure_rate: float = 0.25
    mandate_revoke_rate: float = 0.08

    refund_rate: float = 0.03

    # Sad paths, deliberate: an untested dead-letter path is a broken one.
    malformed_rate: float = 0.01
    duplicate_delivery_rate: float = 0.03


# n_transactions distribution (both B2C and B2B): a long head of one-shot
# customers, a long tail where behavioural features (rolling_late_rate,
# tenure) become computable at all.
TRANSACTION_COUNT_BUCKETS = [(1, 1), (2, 4), (5, 9), (10, 15)]
TRANSACTION_COUNT_WEIGHTS = [0.55, 0.30, 0.11, 0.04]


@dataclass
class Customer:
    index: int
    email: str
    name: str
    phone: str
    customer_type: str       # 'B2C' | 'B2B'
    segment: str
    sector: str
    business_model: str      # 'product' | 'service'
    archetype: str
    fixed_amount_minor: int  # drawn once per customer, reused across transactions
    amount_volatility: float # 0.0 for product, jittered for service
    n_transactions: int


def business_id_for(cfg: GeneratorConfig) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{cfg.seed}:business")


def _n_transactions(rng: random.Random) -> int:
    lo, hi = rng.choices(TRANSACTION_COUNT_BUCKETS, weights=TRANSACTION_COUNT_WEIGHTS, k=1)[0]
    return rng.randint(lo, hi)


def _make_customers(cfg: GeneratorConfig, rng: random.Random) -> list[Customer]:
    customers = []
    for i in range(cfg.n_customers):
        email = f"customer{i:05d}@example.com"
        # profile is keyed off cfg.seed (the stable business-identity seed),
        # matching latent_propensity()'s own key, not content_seed -- so a
        # customer's demographics don't shuffle across dashboard reruns.
        profile = customer_profile_for(email, cfg.seed)
        fixed_amount_minor = _amount(cfg, rng)
        amount_volatility = 0.0 if profile.business_model == "product" else rng.uniform(0.10, 0.35)
        customers.append(
            Customer(
                index=i,
                email=email,
                name=f"Customer {i:05d}",
                phone=f"+9198{rng.randint(10_000_000, 99_999_999)}",
                customer_type=profile.customer_type,
                segment=profile.segment,
                sector=profile.sector,
                business_model=profile.business_model,
                archetype=profile.archetype,
                fixed_amount_minor=fixed_amount_minor,
                amount_volatility=amount_volatility,
                n_transactions=_n_transactions(rng),
            )
        )
    return customers


def _customer_amount(cfg: GeneratorConfig, rng: random.Random, customer: Customer) -> int:
    if customer.amount_volatility == 0.0:
        return customer.fixed_amount_minor
    jittered = customer.fixed_amount_minor * rng.gauss(1.0, customer.amount_volatility)
    return max(round(jittered, -2), cfg.min_amount_minor)


def _pick_failure(cfg: GeneratorConfig, rng: random.Random) -> FailureCode:
    weights = [code.weight for code in FAILURE_CODES]
    return rng.choices(FAILURE_CODES, weights=weights, k=1)[0]


def _random_time(cfg: GeneratorConfig, rng: random.Random, start: datetime) -> datetime:
    return start + timedelta(seconds=rng.uniform(0, cfg.days * 86_400))


def _amount(cfg: GeneratorConfig, rng: random.Random) -> int:
    if rng.random() < cfg.high_value_rate:
        return round(rng.randint(cfg.high_value_min_minor, cfg.high_value_max_minor), -2)
    return round(rng.randint(cfg.min_amount_minor, cfg.max_amount_minor), -2)


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
    entity.update(_instrument_fields(rng, entity["method"], customer))
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


def _order_entity(order_id: str, amount: int, currency: str, customer: Customer, created_at: datetime) -> dict:
    """Shopify-shaped, deliberately messy relative to Razorpay's own shape:

    a decimal-string major-unit amount instead of an integer minor unit,
    a lowercase currency code, ISO8601 timestamps instead of unix epoch.
    """
    return {
        "id": order_id,
        "financial_status": "pending",
        "total_price": f"{amount / 100:.2f}",
        "currency": currency.lower(),
        "customer": {"email": customer.email, "phone": customer.phone},
        "created_at": created_at.isoformat(),
    }


def _shopify_webhook(order_entity: dict) -> dict:
    return {"topic": "orders/create", "order": order_entity}


def _dispute_entity(dispute_id: str, payment_id: str, amount: int, currency: str,
                     created_at: datetime, respond_by: datetime) -> dict:
    return {
        "id": dispute_id,
        "entity": "dispute",
        "payment_id": payment_id,
        "amount": amount,
        "currency": currency,
        "reason_code": "goods_or_services_not_provided",
        "status": "open",
        "respond_by": _unix(respond_by),
        "created_at": _unix(created_at),
    }


def _generate_payment_intents(cfg: GeneratorConfig, rng: random.Random,
                               customers: list[Customer], business_id: uuid.UUID,
                               start: datetime) -> tuple[list[dict], list[dict]]:
    """Returns (events, successful_payment_entities) for refund sourcing."""
    events: list[dict] = []
    successes: list[dict] = []

    def _maybe_dispute(entity: dict, captured_at: datetime) -> None:
        if rng.random() >= cfg.dispute_rate:
            return
        dispute_time = captured_at + timedelta(days=rng.uniform(1, 6))
        respond_by = dispute_time + timedelta(days=7)
        dispute_id = _rand_id(rng, "disp")
        events.append(_event(business_id, "razorpay", "webhook", dispute_time,
                              _webhook("payment.dispute.created", "dispute",
                                       _dispute_entity(dispute_id, entity["id"], entity["amount"],
                                                        entity["currency"], dispute_time, respond_by)),
                              f"evt_{dispute_id}"))

    # Inverted from the old rng.choice(customers)-per-intent loop: iterate
    # customers, emit each one's full n_transactions. That is the only way
    # repeats are real rather than accidental Poisson noise, and it means
    # cfg.n_payment_intents no longer sizes this loop directly -- volume is
    # controlled via cfg.n_customers and the repeat distribution instead.
    intents = [(customer, txn) for customer in customers for txn in range(customer.n_transactions)]
    rng.shuffle(intents)

    for customer, _txn in intents:
        order_id = _rand_id(rng, "order")
        amount = _customer_amount(cfg, rng, customer)
        t = _random_time(cfg, rng, start)

        order_created_at = t - timedelta(minutes=rng.uniform(2, 45))
        events.append(_event(business_id, "shopify", "webhook", order_created_at,
                              _shopify_webhook(_order_entity(order_id, amount, cfg.currency, customer, order_created_at)),
                              f"evt_{order_id}_created"))

        will_fail = rng.random() < cfg.payment_failure_rate

        if not will_fail:
            payment_id = _rand_id(rng, "pay")
            entity = _payment_entity(rng, payment_id, order_id, amount, cfg.currency,
                                      customer, "captured", t, None, None)
            events.append(_event(business_id, "razorpay", "webhook", t,
                                  _webhook("payment.captured", "payment", entity),
                                  f"evt_{payment_id}"))
            successes.append(entity)
            _maybe_dispute(entity, t)
            continue

        failure = _pick_failure(cfg, rng)
        policy = FAILURE_TAXONOMY[failure.canonical]
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
                _maybe_dispute(entity, attempt_time)

    return events, successes


def _generate_checkouts(cfg: GeneratorConfig, rng: random.Random,
                         customers: list[Customer], business_id: uuid.UUID,
                         start: datetime) -> list[dict]:
    if not customers:
        return []
    events: list[dict] = []
    for i in range(cfg.n_checkouts):
        customer = rng.choice(customers)
        session_id = _rand_id(rng, "cs")
        amount = _amount(cfg, rng)
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
    if not customers:
        return []
    events: list[dict] = []
    for i in range(cfg.n_invoices):
        customer = rng.choice(customers)
        invoice_id = _rand_id(rng, "inv")
        amount = _amount(cfg, rng)
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
        amount = _amount(cfg, rng)
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
                "amount": amount,
                "currency": cfg.currency,
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
    rng = random.Random(cfg.content_seed if cfg.content_seed is not None else cfg.seed)
    business_id = business_id_for(cfg)
    start = cfg.anchor - timedelta(days=cfg.days)
    customers = _make_customers(cfg, rng)

    # PAYMENT/CHECKOUT are Razorpay's B2C-facing products (Checkout, Payment
    # Gateway); INVOICE/SUBSCRIPTION are its B2B recurring-billing products.
    # Partitioning here is what makes customer_type an enforced fact instead
    # of an independent coin-flip unrelated to which events a customer gets
    # -- see build_features()'s own B2C_FEATURES/B2B_FEATURES split, which
    # this now actually matches.
    b2c_customers = [c for c in customers if c.customer_type == "B2C"]
    b2b_customers = [c for c in customers if c.customer_type == "B2B"]

    payment_events, successes = _generate_payment_intents(cfg, rng, b2c_customers, business_id, start)
    checkout_events = _generate_checkouts(cfg, rng, b2c_customers, business_id, start)
    invoice_events = _generate_invoices(cfg, rng, b2b_customers, business_id, start)
    subscription_events = _generate_subscriptions(cfg, rng, b2b_customers, business_id, start)
    refund_events = _generate_refunds(cfg, rng, successes, business_id)

    events = payment_events + checkout_events + invoice_events + subscription_events + refund_events
    events.sort(key=lambda e: e["occurred_at"])
    return events


def summarize(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        name = e["payload"].get("event") or e["payload"].get("topic")
        key = f'{e["source_provider"]}:{name}'
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
    parser.add_argument("--customers", type=int, default=800)
    parser.add_argument("--payment-intents", type=int, default=1_300)
    parser.add_argument("--checkouts", type=int, default=480)
    parser.add_argument("--invoices", type=int, default=240)
    parser.add_argument("--subscriptions", type=int, default=160)
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
