"""Backend logic behind the dashboard's buttons.

Distinct from scripts/demo.py (Day 12's full clean-database rehearsal,
which subprocess-chains each stage): this wipes and regenerates only the
one demo business's transactional data, leaves schema/migrations/
reference config untouched, and calls the generator's functions directly
in-process -- faster, and gives the dashboard a real return value instead
of a subprocess's stdout to parse.

Every function here is deliberately synchronous and safe to call from a
plain (non-async) FastAPI route -- FastAPI runs those in a threadpool, so
a blocking DB/HTTP call doesn't stall the event loop, consistent with the
rest of this codebase (nothing here uses async def).
"""

import random
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Business, Customer, Payment
from seed.generator import GeneratorConfig, business_id_for

BUSINESS_SEED = 42  # fixed: DEMO_BUSINESS_ID and every stage below resolve the business from this

# Everything here is per-business transactional data, safe to wipe and
# regenerate. Deliberately excludes businesses, policy_bounds,
# message_templates, field_mappings, value_mappings, failure_taxonomy,
# unmapped_fields -- authored/reference config that isn't regenerated.
_TRANSACTIONAL_TABLES = [
    "recovery_explanations", "recovery_outcomes", "recovery_tokens", "recovery_attempts", "recovery_batches",
    "revenue_at_risk", "revenue_events", "disputes", "invoices", "subscriptions", "checkout_sessions",
    "payments", "orders", "dead_letter_events", "raw_events", "source_mappings",
    "customer_contactability", "customers", "ops_alerts", "customer_merges",
]


def _business_id() -> uuid.UUID:
    return business_id_for(GeneratorConfig(seed=BUSINESS_SEED))


def wipe_transactional_data(session: Session) -> None:
    # TRUNCATE, not DELETE: this build is single-tenant by design (one
    # business throughout), so there's no WHERE clause to write -- CASCADE
    # handles the FK ordering across 18 tables instead of a hand-maintained
    # DELETE sequence, which is exactly the kind of thing that's easy to
    # get subtly wrong as the schema grows.
    session.execute(text(f"TRUNCATE {', '.join(_TRANSACTIONAL_TABLES)} CASCADE"))


def populate(
    *, customers: int = 300, payment_intents: int = 150, checkouts: int = 60, invoices: int = 30, subscriptions: int = 20
) -> dict:
    """Wipes and regenerates the demo business's data with FRESH random

    content every call (content_seed is drawn from the OS's own randomness
    source, not derived from anything fixed) -- the business identity
    (BUSINESS_SEED) never changes, so the dashboard keeps working against
    the same DEMO_BUSINESS_ID, but the transactions themselves are
    genuinely different each click: different customers hit, different
    amounts, different failure mix realizations, different instrument
    details.
    """
    from app.api import app as fastapi_app
    from app.risk.sweeps import sweep_abandoned_checkouts, sweep_overdue_invoices
    from app.worker import drain
    from fastapi.testclient import TestClient
    from seed.generate import apply_sad_paths, deliver_via_imports, seed_contactability
    from seed.generator import generate_dataset

    started = time.monotonic()
    content_seed = random.SystemRandom().randint(1, 2_000_000_000)

    session = SessionLocal()
    try:
        wipe_transactional_data(session)
        session.commit()
    finally:
        session.close()

    cfg = GeneratorConfig(
        seed=BUSINESS_SEED, content_seed=content_seed, n_customers=customers, n_payment_intents=payment_intents,
        n_checkouts=checkouts, n_invoices=invoices, n_subscriptions=subscriptions,
    )
    rng = random.Random(content_seed ^ 0xC0DE)
    events = generate_dataset(cfg)
    events = apply_sad_paths(cfg, rng, events)

    client = TestClient(fastapi_app)
    import_result = deliver_via_imports(client, events)
    drain_stats = drain()
    contactability_rows = seed_contactability(cfg)

    session = SessionLocal()
    try:
        n_abandoned = sweep_abandoned_checkouts(session)
        n_overdue = sweep_overdue_invoices(session)
        session.commit()
    finally:
        session.close()

    return {
        "content_seed": content_seed,
        "imports": import_result,
        "drain": drain_stats,
        "customer_contactability_rows": contactability_rows,
        "newly_abandoned_checkouts": n_abandoned,
        "newly_overdue_invoices": n_overdue,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }


def launch_recovery() -> dict:
    """Decide -> bound -> execute, one batch, live (not dry_run). By the

    time this returns, every dispatch bounds.py's execute_action() allowed
    (simulated SMS/WhatsApp/voice, real email, a retry submission) has
    already happened synchronously in-process -- there's nothing left
    in flight for a caller to wait on separately, which is what makes
    "recovery launched" a true statement the instant this call resolves.
    """
    from app.recovery.batch import run_batch

    business_id = _business_id()
    session = SessionLocal()
    try:
        business = session.get(Business, business_id)
        business.recovery_enabled = True
        business.dry_run = False
        session.commit()

        batch = run_batch(session, business_id, seed=BUSINESS_SEED)
        session.commit()

        return {
            "batch_id": str(batch.batch_id),
            "entities_scanned": batch.entities_scanned,
            "decisions_made": batch.decisions_made,
            "actions_executed": batch.actions_executed,
            "actions_suppressed": batch.actions_suppressed,
            "actions_stopped": batch.actions_stopped,
            "at_risk_minor": batch.at_risk_minor,
        }
    finally:
        session.close()


def simulate_replies() -> dict:
    """The world simulator's response model, run once: reads recovery_attempts,

    decides which ones a (simulated) customer responds to, and posts
    provider-shaped success events back through real ingestion -- never
    written directly to recovery_outcomes. Drains those events, then runs
    attribution so the batch report reflects what actually got matched.
    """
    from app.recovery.attribution import run_attribution
    from app.worker import drain
    from seed.simulate_world import simulate

    business_id = _business_id()
    session = SessionLocal()
    try:
        sim_stats = simulate(session, business_id, seed=BUSINESS_SEED)
        session.commit()
    finally:
        session.close()

    drain_stats = drain()

    session = SessionLocal()
    try:
        attribution_stats = run_attribution(session, business_id)
        session.commit()
    finally:
        session.close()

    return {"simulate": sim_stats, "drain": drain_stats, "attribution": attribution_stats}


def get_kpis() -> dict:
    from app.recovery.report import build_report

    business_id = _business_id()
    session = SessionLocal()
    try:
        report = build_report(session, business_id)
    finally:
        session.close()

    t, h = report.claimable["TREATMENT"], report.claimable["HOLDOUT"]
    lift_pp = t.rate - h.rate
    baseline_expected_minor = int(t.at_risk_minor * (h.rate / 100)) if t.at_risk_minor else 0
    net_minor = t.recovered_minor - baseline_expected_minor

    return {
        "total_items": report.total_items,
        "total_at_risk_minor": report.total_at_risk_minor,
        "claimable_weak_excluded_minor": report.claimable_weak_excluded_minor,
        "treatment": {
            "total": t.total, "recovered_strong": t.recovered_strong,
            "recovered_minor": t.recovered_minor, "rate_pct": round(t.rate, 1),
        },
        "holdout": {
            "total": h.total, "recovered_strong": h.recovered_strong,
            "recovered_minor": h.recovered_minor, "rate_pct": round(h.rate, 1),
        },
        "lift_pp": round(lift_pp, 1),
        "net_minor": net_minor,
        "suppressed_compliance": report.suppressed_compliance,
        "stopped_by_rules": report.stopped_by_rules,
        "held_for_approval": report.held_for_approval,
    }


def get_transactions(limit: int = 200) -> list[dict]:
    from app.models import RevenueAtRisk

    business_id = _business_id()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Payment, Customer.email)
            .outerjoin(Customer, Customer.customer_id == Payment.customer_id)
            .where(Payment.business_id == business_id)
            .order_by(desc(Payment.initiated_at))
            .limit(limit)
        ).all()

        payment_ids = [p.payment_id for p, _ in rows]
        # A payment can accumulate more than one revenue_at_risk row across
        # its lifetime (e.g. detected, then a later attempt succeeds and a
        # fresh one opens) -- only the partial unique index guarantees
        # uniqueness among OPEN rows, so pick the most recently detected one
        # per payment in Python rather than assuming the join is 1:1.
        latest_at_risk: dict[uuid.UUID, tuple[uuid.UUID, datetime]] = {}
        if payment_ids:
            ar_rows = session.execute(
                select(RevenueAtRisk.entity_id, RevenueAtRisk.at_risk_id, RevenueAtRisk.detected_at).where(
                    RevenueAtRisk.business_id == business_id,
                    RevenueAtRisk.entity_type == "PAYMENT",
                    RevenueAtRisk.entity_id.in_(payment_ids),
                )
            ).all()
            for entity_id, at_risk_id, detected_at in ar_rows:
                current = latest_at_risk.get(entity_id)
                if current is None or detected_at > current[1]:
                    latest_at_risk[entity_id] = (at_risk_id, detected_at)

        return [
            {
                "payment_id": str(p.payment_id),
                "payment_intent_id": p.payment_intent_id,
                "attempt_number": p.attempt_number,
                "customer_email": email,
                "amount_minor": p.amount_minor,
                "currency": p.currency,
                "status": p.payment_status,
                "method": p.payment_method,
                "failure_reason": p.failure_code_canonical,
                "issuer_bank": p.issuer_bank,
                "initiated_at": p.initiated_at.isoformat() if p.initiated_at else None,
                "at_risk_id": str(latest_at_risk[p.payment_id][0]) if p.payment_id in latest_at_risk else None,
            }
            for p, email in rows
        ]
    finally:
        session.close()


def explain_batch(batch_id: str) -> dict:
    """The batch-level narrative: LLM first (using the same evidence bundle

    build_batch_evidence() always built), the deterministic template only
    when there's no API key, the call fails, or the model states a number
    outside the bundle's verifiable_numbers allowlist. `is_fallback` tells
    the caller which one actually happened, so the dashboard can label it
    honestly instead of presenting a template sentence as if it were AI
    output.
    """
    from app.explain import explain as explain_fn

    session = SessionLocal()
    try:
        result = explain_fn(session, "BATCH", business_id=_business_id(), batch_id=uuid.UUID(batch_id))
        session.commit()
        return {
            "narrative": result.narrative,
            "numbers_verified": result.numbers_verified,
            "model": result.model,
            "is_fallback": result.model == "template",
        }
    finally:
        session.close()


def explain_transaction(at_risk_id: str) -> dict:
    """Same LLM-first/template-fallback narrative, scoped to one loss record

    -- what the dashboard's per-row "Explain" button calls.
    """
    from app.explain import explain as explain_fn

    session = SessionLocal()
    try:
        result = explain_fn(session, "AT_RISK", business_id=_business_id(), at_risk_id=uuid.UUID(at_risk_id))
        session.commit()
        return {
            "narrative": result.narrative,
            "numbers_verified": result.numbers_verified,
            "model": result.model,
            "is_fallback": result.model == "template",
        }
    finally:
        session.close()


_GROUP_META = {
    "recovered": (
        "Recovered",
        "Customers who were contacted and completed the payment.",
    ),
    "executed_pending": (
        "Actioned, awaiting response",
        "A nudge or retry was sent this run; no customer response yet.",
    ),
    "suppressed": (
        "Suppressed (compliance)",
        "Permanently blocked from contact this run -- no consent on file, an open dispute, "
        "a hard-bounced channel, a revoked mandate, or a policy stop.",
    ),
    "stopped_held": (
        "Stopped or held",
        "Not actioned this run for a temporary reason -- the holdout cohort (deliberately "
        "untouched so its outcome measures what would happen without any action), a contact "
        "cap, quiet hours, the minimum gap between contacts, or held for human approval.",
    ),
}
_TERMINAL_SUPPRESSION_PREFIXES = ("h1_", "h2_", "h4_", "h5_", "h6_", "h9_")


def explain_batch_groups(batch_id: str) -> dict:
    """Buckets one batch's attempts into the same four categories the KPI

    tiles already summarize as executed/suppressed/stopped counters, and
    generates one LLM-first/template-fallback narrative per bucket instead
    of per transaction -- "why did this whole group of 63 end up executed"
    rather than 63 separate one-line explanations.

    recovery_attempts has no batch_id column (the same Day 8 schema gap
    build_batch_evidence works around in app/explain.py), so attempts are
    correlated to this batch by falling inside [started_at, completed_at]
    -- safe here because run_batch() decides every entity synchronously
    within one call and this build never runs two batches concurrently for
    one business.
    """
    from app.explain import _inr, render_narrative
    from app.models import RecoveryAttempt, RecoveryBatch, RecoveryOutcome, RevenueAtRisk

    business_id = _business_id()
    session = SessionLocal()
    try:
        batch = session.get(RecoveryBatch, uuid.UUID(batch_id))
        if batch is None:
            raise ValueError(f"no recovery_batches row {batch_id}")

        window_end = batch.completed_at or datetime.now(timezone.utc)
        attempts = session.execute(
            select(RecoveryAttempt).where(
                RecoveryAttempt.business_id == business_id,
                RecoveryAttempt.decided_at >= batch.started_at,
                RecoveryAttempt.decided_at <= window_end,
            )
        ).scalars().all()

        at_risk_by_id: dict[uuid.UUID, RevenueAtRisk] = {}
        outcome_by_at_risk: dict[uuid.UUID, RecoveryOutcome] = {}
        payment_by_entity: dict[uuid.UUID, tuple] = {}
        at_risk_ids = [a.at_risk_id for a in attempts]
        if at_risk_ids:
            at_risk_by_id = {
                ar.at_risk_id: ar
                for ar in session.execute(
                    select(RevenueAtRisk).where(RevenueAtRisk.at_risk_id.in_(at_risk_ids))
                ).scalars()
            }
            outcome_by_at_risk = {
                o.at_risk_id: o
                for o in session.execute(
                    select(RecoveryOutcome).where(RecoveryOutcome.at_risk_id.in_(at_risk_ids))
                ).scalars()
            }
            payment_ids = [ar.entity_id for ar in at_risk_by_id.values() if ar.entity_type == "PAYMENT"]
            if payment_ids:
                payment_by_entity = {
                    p.payment_id: (p, email)
                    for p, email in session.execute(
                        select(Payment, Customer.email)
                        .outerjoin(Customer, Customer.customer_id == Payment.customer_id)
                        .where(Payment.payment_id.in_(payment_ids))
                    ).all()
                }

        buckets: dict[str, list] = {key: [] for key in _GROUP_META}
        for a in attempts:
            outcome = outcome_by_at_risk.get(a.at_risk_id)
            if a.executed_at is not None:
                key = "recovered" if outcome and outcome.outcome == "RECOVERED" else "executed_pending"
            elif a.suppressed_reason and (
                a.suppressed_reason.startswith(_TERMINAL_SUPPRESSION_PREFIXES) or a.suppressed_reason == "h3_policy_stop"
            ):
                key = "suppressed"
            else:
                key = "stopped_held"
            buckets[key].append((a, outcome))

        results = {}
        for key, (label, definition) in _GROUP_META.items():
            items = buckets[key]
            # "recovered so far" is only coherent for buckets whose defining
            # attempt actually executed -- an at-risk item bucketed into
            # suppressed/stopped_held was NOT executed by the attempt found
            # in this batch, so any RecoveryOutcome on that at_risk_id (joined
            # by at_risk_id, not attempt_id) necessarily belongs to a
            # DIFFERENT attempt from a different batch. Showing it here would
            # read as "this was blocked, but recovered anyway" -- true but
            # attributed to the wrong decision, so it's omitted for those two.
            show_recovered = key in ("recovered", "executed_pending")
            results[key] = _explain_group(
                label, definition, items, at_risk_by_id, payment_by_entity, _inr, render_narrative, show_recovered
            )
        return results
    finally:
        session.close()


def _explain_group(label, definition, items, at_risk_by_id, payment_by_entity, inr, render_narrative, show_recovered) -> dict:
    from collections import Counter

    count = len(items)
    if count == 0:
        return {
            "group_name": label, "count": 0, "total_at_risk_rupees": "0",
            "narrative": f'Nothing fell into "{label}" in this run.',
            "is_fallback": True, "model": "template", "items": [],
        }

    total_minor = sum(at_risk_by_id[a.at_risk_id].at_risk_minor for a, _ in items if a.at_risk_id in at_risk_by_id)
    recovered_minor = sum(o.recovered_minor for _, o in items if o and o.recovered_minor) if show_recovered else 0
    reason_counts = Counter(a.suppressed_reason for a, _ in items if a.suppressed_reason)
    channel_counts = Counter(a.channel for a, _ in items if a.channel)
    category_counts = Counter(
        at_risk_by_id[a.at_risk_id].loss_category for a, _ in items if a.at_risk_id in at_risk_by_id
    )

    numbers = [str(count), inr(total_minor)]
    if recovered_minor:
        numbers.append(inr(recovered_minor))
    numbers += [str(v) for v in reason_counts.values()]
    numbers += [str(v) for v in channel_counts.values()]
    numbers += [str(v) for v in category_counts.values()]

    bundle = {
        "scope": "GROUP",
        "group_name": label,
        "group_definition": definition,
        "count": count,
        "total_at_risk_rupees": inr(total_minor),
        "total_recovered_rupees": inr(recovered_minor) if recovered_minor else None,
        "by_suppressed_reason": dict(reason_counts),
        "by_channel": dict(channel_counts),
        "by_loss_category": dict(category_counts),
        "verifiable_numbers": numbers,
    }
    result = render_narrative(bundle)

    item_rows = []
    for a, outcome in items[:50]:
        ar = at_risk_by_id.get(a.at_risk_id)
        payment, email = payment_by_entity.get(ar.entity_id, (None, None)) if ar else (None, None)
        item_rows.append({
            "payment_id": str(ar.entity_id)[:8] if ar else None,
            "customer_email": email,
            "amount_minor": ar.at_risk_minor if ar else None,
            "channel": a.channel,
            "suppressed_reason": a.suppressed_reason,
            "outcome": outcome.outcome if outcome else None,
        })

    return {
        "group_name": label,
        "count": count,
        "total_at_risk_rupees": bundle["total_at_risk_rupees"],
        "narrative": result.narrative,
        "is_fallback": result.model == "template",
        "model": result.model,
        "items": item_rows,
    }
