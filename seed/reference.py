"""Authored configuration -- must be correct, not plausible.

Everything here is hand-written config, not generated data: the demo
business itself, the failure taxonomy, field/value mappings, message
templates, and policy bounds. Idempotent -- safe to run repeatedly
(everything upserts). This is deliberately separate from seed/generate.py:
config is authored once and rarely changes; loss events are generated
fresh every run.
"""

import argparse
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.canonical.vocabulary import LossCategory
from app.db import SessionLocal
from app.models import Business, MessageTemplate, PolicyBounds
from seed.failure_taxonomy import seed_failure_taxonomy
from seed.field_mappings import seed_field_mappings
from seed.generator import GeneratorConfig, business_id_for as _business_id_for
from seed.value_mappings import seed_value_mappings

DEMO_SEED = 42


def business_id_for(seed: int = DEMO_SEED) -> uuid.UUID:
    return _business_id_for(GeneratorConfig(seed=seed))


def ensure_business(session: Session, business_id: uuid.UUID, name: str) -> None:
    stmt = (
        pg_insert(Business)
        .values(
            business_id=business_id,
            name=name,
            default_currency="INR",
            default_timezone="Asia/Kolkata",
            recovery_enabled=False,
            dry_run=True,
            created_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["business_id"])
    )
    session.execute(stmt)


def ensure_policy_bounds(session: Session, business_id: uuid.UUID) -> None:
    stmt = (
        pg_insert(PolicyBounds)
        .values(
            business_id=business_id,
            max_attempts_per_entity=3,
            max_contacts_per_week=3,
            min_gap_hours=24,
            max_entities_per_batch=500,
            max_batch_spend_minor=500_000,
            max_sends_per_hour=200,
            human_approval_above_minor=5_000_000,
            allow_automated_charge=False,
            quiet_hours_enforced=True,
            holdout_percent=20,
            policy_version="bounds@v1",
            updated_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["business_id"])
    )
    session.execute(stmt)


# One EMAIL, SMS, and WHATSAPP template per token-nudge / event-attributed
# category that the channel layer can actually send. SMS/WHATSAPP carry a
# demo DLT/Meta registration id -- fake, clearly labelled -- since the
# money_fields_need_human-style compliance check (registered_before_approved)
# would otherwise refuse to mark them approved, and SimulatedChannel
# needs an approved template to send through.
_TEMPLATES = [
    # (channel, loss_category, body)
    ("EMAIL", LossCategory.B1, "Your cart is waiting -- {{amount}} for {{item_count}} item(s). Complete your order: {{link}}"),
    ("EMAIL", LossCategory.B2, "We couldn't renew your subscription -- please re-authorize: {{link}}"),
    ("EMAIL", LossCategory.B3, "Your payment didn't go through. Update your payment method: {{link}}"),
    ("EMAIL", LossCategory.B4, "Invoice {{invoice_number}} for {{amount}} is overdue. Pay now: {{link}}"),
    ("SMS", LossCategory.B1, "Your cart ({{amount}}) is waiting. Complete it: {{link}}"),
    ("SMS", LossCategory.B2, "Subscription renewal failed. Re-authorize: {{link}}"),
    ("SMS", LossCategory.B3, "Payment failed. Update card: {{link}}"),
    ("SMS", LossCategory.B4, "Invoice {{invoice_number}} overdue. Pay: {{link}}"),
    ("WHATSAPP", LossCategory.B1, "Hi! Your cart ({{amount}}) is still here. Finish checkout: {{link}}"),
    ("WHATSAPP", LossCategory.B2, "Your subscription renewal needs attention: {{link}}"),
    ("WHATSAPP", LossCategory.B3, "Your last payment didn't go through: {{link}}"),
    ("WHATSAPP", LossCategory.B4, "Invoice {{invoice_number}} is overdue: {{link}}"),
]


def seed_message_templates(session: Session, business_id: uuid.UUID) -> int:
    # message_templates has no natural-key unique constraint to ON CONFLICT
    # against (template_id is a random UUID) -- delete-then-insert is the
    # idempotent option that doesn't require touching the frozen schema.
    session.execute(
        delete(MessageTemplate).where(MessageTemplate.business_id == business_id, MessageTemplate.approved_by == "demo-seed")
    )
    rows = []
    for channel, loss_category, body in _TEMPLATES:
        # Every row carries every column explicitly (None where it doesn't apply) --
        # a bulk .values(list_of_dicts) insert takes its column set from the dicts'
        # keys, so an omitted key on some rows silently drops that column for ALL
        # rows in the same statement, not just the ones missing it.
        row = dict(
            template_id=uuid.uuid4(),
            business_id=business_id,
            channel=channel,
            loss_category=loss_category.value,
            locale="en-IN",
            body=body,
            variables=["amount", "link", "item_count", "invoice_number"],
            dlt_template_id=f"DLT-DEMO-{loss_category.value}" if channel == "SMS" else None,
            dlt_header="DEMOBZ" if channel == "SMS" else None,
            meta_template_name=f"demo_nudge_{loss_category.value.lower()}" if channel == "WHATSAPP" else None,
            approved=True,
            approved_by="demo-seed",
            generated_by="human",
            created_at=datetime.now(timezone.utc),
        )
        rows.append(row)

    stmt = pg_insert(MessageTemplate).values(rows)
    session.execute(stmt)
    return len(rows)


def run_reference_seed(session: Session, seed: int = DEMO_SEED) -> dict[str, int]:
    business_id = business_id_for(seed)
    ensure_business(session, business_id, "Demo Business")
    ensure_policy_bounds(session, business_id)
    counts = {
        "field_mappings": seed_field_mappings(session),
        "value_mappings": seed_value_mappings(session),
        "failure_taxonomy": seed_failure_taxonomy(session),
        "message_templates": seed_message_templates(session, business_id),
    }
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed all authored reference config.")
    parser.add_argument("--seed", type=int, default=DEMO_SEED)
    args = parser.parse_args()

    session = SessionLocal()
    try:
        counts = run_reference_seed(session, args.seed)
        session.commit()
    finally:
        session.close()

    print(f"business ready: {business_id_for(args.seed)}")
    for name, n in counts.items():
        print(f"  {name}: {n}")


if __name__ == "__main__":
    main()
