"""Phase 1: loss events, delivered for real.

The cardinal rule: this module builds provider-shaped payloads and POSTs
them to the real endpoints -- it never touches a canonical table. Two
delivery paths, deliberately different:

- The 30-day historical backlog goes through POST /v1/imports. It has to:
  the webhook endpoint's 5-minute freshness check exists to reject a
  REPLAYED live webhook, and backdated-by-design historical data would
  always fail that check with its own honest timestamps. Signing 30-day-
  old payloads with a fresh "now" would be lying about when they happened.
- A small freshly-timestamped batch goes through POST /v1/webhooks/razorpay
  with real HMAC signing, proving the actual webhook pipe -- signature
  verification, freshness check, idempotency -- at the HTTP layer, not
  just the import path.

Both use FastAPI's TestClient: a real ASGI request through routing,
signature verification, and the ORM, without needing a separately running
uvicorn process -- the same reason `make demo` can call this as one step.
"""

import argparse
import copy
import hashlib
import hmac
import io
import json
import random
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.api import app
from app.db import SessionLocal
from app.models import Customer, CustomerContactability
from app.settings import DEMO_BUSINESS_ID, RAZORPAY_WEBHOOK_SECRET
from seed.generator import GeneratorConfig, business_id_for, generate_dataset

CONTACT_CHANNELS = ["SMS", "EMAIL", "WHATSAPP", "VOICE"]
DND_RATE = 0.08          # SMS/WhatsApp/voice only -- India's DND registry is not an email concept
OPT_OUT_RATE = 0.05
HARD_BOUNCE_RATE = 0.03


def _corrupt(rng: random.Random, event: dict) -> dict:
    """1% of events, deliberately broken: the structural stage should

    find no event_type_raw at all and dead-letter it cleanly. An
    untested dead-letter path is a broken dead-letter path.

    Strips only the envelope key carrying event_type_raw ("event" for
    Razorpay/storefront, "topic" for Shopify) rather than replacing the
    whole payload -- the rest of the payload's real content stays, so
    this corrupted copy's payload_hash still differs from every other
    corrupted copy. A fixed placeholder payload would make every
    "malformed" event byte-identical, and raw_events' payload_hash
    uniqueness constraint would then silently collapse them into one
    row -- exactly what happened the first time this was written.
    """
    broken = copy.deepcopy(event)
    broken["payload"].pop("event", None)
    broken["payload"].pop("topic", None)
    broken["external_event_id"] = (broken.get("external_event_id") or "evt") + "_malformed"
    return broken


def apply_sad_paths(cfg: GeneratorConfig, rng: random.Random, events: list[dict]) -> list[dict]:
    """Inserts malformed/duplicate copies immediately next to their originals,

    preserving the chronological (occurred_at-sorted) delivery order.
    Deliberately NOT shuffled: the worker's queue claims strictly by
    received_at (insertion order), so a refund or dispute delivered ahead
    of the payment it references would exhaust its 5-attempt cap before
    that payment is ever touched -- shuffling here would break the very
    ordering assumption the rest of the pipeline correctly relies on.
    """
    out: list[dict] = []
    for event in events:
        out.append(event)
        if rng.random() < cfg.malformed_rate:
            out.append(_corrupt(rng, event))
        if rng.random() < cfg.duplicate_delivery_rate:
            out.append(event)  # exact repeat -- exercises idempotency at scale
    return out


def deliver_via_imports(client: TestClient, events: list[dict]) -> dict:
    body = "\n".join(json.dumps(e) for e in events).encode("utf-8")
    resp = client.post(
        "/v1/imports",
        files={"file": ("events.jsonl", io.BytesIO(body), "application/jsonl")},
    )
    resp.raise_for_status()
    return resp.json()


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver_webhook_smoke_test(client: TestClient, business_id, n: int = 5) -> dict:
    """A handful of FRESHLY-timestamped Razorpay payments through the real

    webhook endpoint -- HMAC signed, current timestamps, including one
    payload posted 3x to prove idempotency holds at that layer too.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        return {"skipped": "RAZORPAY_WEBHOOK_SECRET not set"}
    # The webhook route attributes every delivery to settings.DEMO_BUSINESS_ID
    # (single-tenant by design, see app/settings.py). This function took a
    # business_id and never used it, so `--seed 7` posted seed-7 events at
    # the seed-42 business id -- a raw_events foreign-key violation, not a
    # clean error. Skip rather than post events that cannot land.
    if business_id != DEMO_BUSINESS_ID:
        return {
            "skipped": f"webhook route posts to DEMO_BUSINESS_ID ({DEMO_BUSINESS_ID}), not this run's "
                       f"business ({business_id}); set DEMO_BUSINESS_ID to smoke-test this seed"
        }

    now = datetime.now(timezone.utc)
    results = {"posted": 0, "inserted": 0, "duplicates": 0}
    for i in range(n):
        payment_id = f"pay_smoketest{i:04d}"
        entity = {
            "id": payment_id, "entity": "payment", "amount": 50000 + i * 100, "currency": "INR",
            "status": "failed", "order_id": f"order_smoketest{i:04d}", "method": "card",
            "email": f"smoketest{i}@example.com", "contact": "+919800000000", "notes": [],
            "error_code": "BAD_REQUEST_ERROR", "error_reason": "issuer_unavailable",
            "error_description": "The card issuing bank or network could not be reached.",
            "error_source": "bank", "error_step": "payment_authorization",
            "card": {"issuer": "HDFC Bank", "network": "Visa", "last4": "4242", "expiry_month": 12, "expiry_year": 2030},
            "created_at": int(now.timestamp()),
        }
        payload = {"entity": "event", "event": "payment.failed", "contains": ["payment"],
                   "payload": {"payment": {"entity": entity}}, "created_at": int(now.timestamp())}
        body = json.dumps(payload).encode()
        headers = {"X-Razorpay-Signature": _sign(RAZORPAY_WEBHOOK_SECRET, body),
                   "X-Razorpay-Event-Id": f"evt_{payment_id}", "Content-Type": "application/json"}

        posts = 3 if i == 0 else 1  # first one proves "same payload 3x -> one row"
        for _ in range(posts):
            resp = client.post("/v1/webhooks/razorpay", content=body, headers=headers)
            resp.raise_for_status()
            results["posted"] += 1
            if resp.json()["inserted"]:
                results["inserted"] += 1
            else:
                results["duplicates"] += 1
    return results


def seed_contactability(cfg: GeneratorConfig) -> int:
    """customer_contactability has no raw_event_id/mapping_version column in

    schema.sql -- it cannot be pipeline-derived with lineage the way every
    other canonical table is, so this seeds it directly against whichever
    customers already exist after the backlog is drained (it can only run
    after that, since customer rows are minted reactively during
    normalization). Delete-then-insert per business: idempotent, no
    natural key to ON CONFLICT against.
    """
    business_id = business_id_for(cfg)
    rng = random.Random((cfg.content_seed if cfg.content_seed is not None else cfg.seed) ^ 0x5EED)
    session = SessionLocal()
    try:
        from sqlalchemy import delete, select

        session.execute(
            delete(CustomerContactability).where(CustomerContactability.business_id == business_id)
        )
        customer_ids = session.execute(
            select(Customer.customer_id).where(Customer.business_id == business_id)
        ).scalars().all()

        rows = []
        now = datetime.now(timezone.utc)
        for customer_id in customer_ids:
            for channel in CONTACT_CHANNELS:
                dnd = channel in ("SMS", "WHATSAPP", "VOICE") and rng.random() < DND_RATE
                opted_out = rng.random() < OPT_OUT_RATE
                hard_bounced = rng.random() < HARD_BOUNCE_RATE
                rows.append(
                    dict(
                        business_id=business_id,
                        customer_id=customer_id,
                        channel=channel,
                        opted_in=not opted_out,
                        opted_out_at=now if opted_out else None,
                        dnd_registered=dnd,
                        max_contacts_per_week=3,
                        min_gap_hours=24,
                        consecutive_failures=3 if hard_bounced else 0,
                        hard_bounced=hard_bounced,
                    )
                )
        if rows:
            session.execute(CustomerContactability.__table__.insert(), rows)
        session.commit()
        return len(rows)
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and deliver loss events through real ingestion.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--content-seed", type=int, default=None,
        help="Seeds the random transaction content independently of --seed (which controls the "
        "business identity and must stay fixed for the rest of the pipeline to find it). Omit to "
        "reuse --seed for content too, the historical make-demo/Gate-D behavior.",
    )
    parser.add_argument("--customers", type=int, default=800)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--anchor", type=str, default=None,
        help="ISO8601 date the generated window ENDS at (default 2026-08-25). A corpus run moves this "
             "back so that a batch decided just after the window has had its full 90-day invoice "
             "attribution window elapse by real today -- otherwise unrecovered invoices never expire "
             "and only the recovered ones reach the training corpus.",
    )
    # Event VOLUME is driven by these, not --customers -- GeneratorConfig's
    # defaults are fixed counts regardless of customer count. Exposed so a
    # caller wanting a fast interactive run (the dashboard's orchestrator)
    # can ask for a smaller batch without touching make demo's defaults.
    parser.add_argument("--payment-intents", type=int, default=400)
    parser.add_argument("--checkouts", type=int, default=150)
    parser.add_argument("--invoices", type=int, default=80)
    parser.add_argument("--invoice-overdue-rate", type=float, default=None,
                         help="Overrides GeneratorConfig.invoice_overdue_rate (default 0.30) -- "
                              "the training profile wants it raised to ~0.35 for B2B label balance.")
    parser.add_argument("--subscriptions", type=int, default=60)
    parser.add_argument("--drain", action="store_true", default=True)
    parser.add_argument("--no-drain", dest="drain", action="store_false")
    parser.add_argument("--contactability", action="store_true", default=True)
    parser.add_argument("--no-contactability", dest="contactability", action="store_false")
    args = parser.parse_args()

    cfg_kwargs = dict(
        seed=args.seed, content_seed=args.content_seed, n_customers=args.customers, days=args.days,
        n_payment_intents=args.payment_intents, n_checkouts=args.checkouts,
        n_invoices=args.invoices, n_subscriptions=args.subscriptions,
    )
    if args.invoice_overdue_rate is not None:
        cfg_kwargs["invoice_overdue_rate"] = args.invoice_overdue_rate
    if args.anchor is not None:
        cfg_kwargs["anchor"] = datetime.fromisoformat(args.anchor)
    cfg = GeneratorConfig(**cfg_kwargs)
    rng = random.Random((cfg.content_seed if cfg.content_seed is not None else cfg.seed) ^ 0xC0DE)

    events = generate_dataset(cfg)
    events = apply_sad_paths(cfg, rng, events)

    client = TestClient(app)
    import_result = deliver_via_imports(client, events)
    smoke_result = deliver_webhook_smoke_test(client, business_id_for(cfg))

    print(f"business_id: {business_id_for(cfg)}")
    print(f"imports: {import_result}")
    print(f"webhook smoke test: {smoke_result}")

    if args.drain:
        from app.worker import drain

        stats = drain()
        print(f"worker drain: {stats}")

    if args.contactability:
        n = seed_contactability(cfg)
        print(f"customer_contactability: {n} rows")


if __name__ == "__main__":
    main()
