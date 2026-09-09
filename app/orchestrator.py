"""Backend logic behind the dashboard's buttons.

Distinct from scripts/demo.py (the full clean-database rehearsal,
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
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    Business,
    Customer,
    CustomerRecoveryProfile,
    Invoice,
    Payment,
    RecoveryAttempt,
    RecoveryBatch,
    RecoveryOutcome,
    RevenueAtRisk,
)
from seed.generator import GeneratorConfig, business_id_for

BUSINESS_SEED = 42  # fixed: DEMO_BUSINESS_ID and every stage below resolve the business from this

# Everything here is per-business transactional data, safe to wipe and
# regenerate. Deliberately excludes businesses, policy_bounds,
# message_templates, field_mappings, value_mappings, failure_taxonomy,
# unmapped_fields -- authored/reference config that isn't regenerated.
_TRANSACTIONAL_TABLES = [
    "recovery_explanations", "outbound_dispatches", "customer_recovery_profile",
    "recovery_outcomes", "recovery_tokens", "recovery_attempts", "recovery_batches",
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
    """Decide -> bound -> enqueue -> drain, one batch, live (not dry_run).

    execute_action() no longer dispatches inline -- a channel-bearing
    action is authorized and written to outbound_dispatches, then
    app.dispatch_worker.drain() sends everything queued, highest predicted-
    recovery-value first, re-checking time-sensitive bounds against the
    current clock right before each send. Draining here, synchronously,
    before this call returns, is what keeps "recovery launched" a true
    statement the instant this call resolves -- simulate_replies() below
    depends on every customer-facing attempt already having its real
    executed_at set by the time it runs, or every recovery number in the
    batch report would read as zero.

    Two things happen after the batch that the batch itself doesn't do:
    any live-demo item planted but not yet decided (excluded from
    run_batch()'s own candidates -- see batch.py) gets its real
    VOICE+WHATSAPP fan-out fired here; and app.live_poll starts, so a real
    WhatsApp reply or a nudge window closing an hour from now still gets
    detected and attributed without anyone clicking anything again.
    """
    from app.dispatch_worker import drain as drain_dispatches
    from app.live_demo import launch as launch_live_demo
    from app.live_poll import start as start_live_poll
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

        pending_live_demo = session.execute(
            select(RevenueAtRisk.at_risk_id).where(
                RevenueAtRisk.business_id == business_id,
                RevenueAtRisk.status == "OPEN",
                RevenueAtRisk.attributes.has_key("live_demo"),
            )
        ).scalars().all()
        # Read out of the ORM object before the session closes -- batch is
        # detached the instant this block exits, and accessing its
        # attributes after that raises DetachedInstanceError.
        batch_summary = {
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

    dispatch_stats = drain_dispatches(seed=BUSINESS_SEED, business_id=business_id)
    live_demo_results = [launch_live_demo(at_risk_id, business_id=business_id) for at_risk_id in pending_live_demo]
    live_poll_started = start_live_poll(business_id)

    return {
        **batch_summary,
        "dispatch": dispatch_stats,
        "live_demo": live_demo_results,
        "live_poll_started": live_poll_started,
    }


def ensure_live_poll() -> dict:
    """Restarts app.live_poll if it isn't already running for this business.

    launch_recovery() starts it, but the thread lives only in-process --
    a backend restart (code reload, crash, redeploy) or simply opening the
    dashboard fresh drops it silently, and nothing then detects a real
    WhatsApp reply until someone happens to click Launch again. The
    dashboard's own page-load script calls this every time so live
    detection resumes on its own, not only after that specific button.
    """
    from app.live_poll import start as start_live_poll

    return {"live_poll_started": start_live_poll(_business_id())}


def simulate_replies() -> dict:
    """The world simulator's response model, run once: reads recovery_attempts,

    decides which ones a (simulated) customer responds to, and posts
    provider-shaped success events back through real ingestion -- never
    written directly to recovery_outcomes. Drains those events, runs
    attribution, then processes any nudge whose window closed unconverted
    (app.recovery.nudge_retry): retries it (up to 3 tries total) or, on
    the 3rd miss, writes the terminal NOT_RECOVERED outcome. A retry that
    gets enqueued here is drained immediately so its own delivery_status
    is real by the time this call returns, same discipline as
    launch_recovery().
    """
    from app.dispatch_worker import drain as drain_dispatches
    from app.recovery.attribution import run_attribution
    from app.recovery.nudge_retry import process_expired_nudges
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

    session = SessionLocal()
    try:
        retry_stats = process_expired_nudges(session, business_id)
        session.commit()
    finally:
        session.close()
    retry_dispatch_stats = drain_dispatches(seed=BUSINESS_SEED, business_id=business_id)

    return {
        "simulate": sim_stats, "drain": drain_stats, "attribution": attribution_stats,
        "nudge_retry": retry_stats, "nudge_retry_dispatch": retry_dispatch_stats,
    }


def get_kpis() -> dict:
    from app.recovery import human_review
    from app.recovery.report import build_report

    business_id = _business_id()
    session = SessionLocal()
    try:
        report = build_report(session, business_id)

        # Across B2C+B2B combined -- a row can count toward this AND "in
        # human hands" at once (high score, but still awaiting a human);
        # that's intentional, not a double-count to reconcile.
        confidently_recoverable_minor = session.execute(
            select(func.coalesce(func.sum(RevenueAtRisk.at_risk_minor), 0))
            .select_from(RevenueAtRisk)
            .join(CustomerRecoveryProfile, CustomerRecoveryProfile.at_risk_id == RevenueAtRisk.at_risk_id)
            .where(
                RevenueAtRisk.business_id == business_id,
                RevenueAtRisk.status.in_(("OPEN", "IN_RECOVERY")),
                CustomerRecoveryProfile.predicted_score > 0.8,
            )
        ).scalar_one()

        in_human_hands_minor = human_review.in_human_hands_minor(session, business_id)

        # Unconditional grand total -- every RecoveryOutcome ever written
        # for this business, RECOVERED or PARTIALLY_RECOVERED, from ANY
        # source: the synthetic batch's treatment cohort, the holdout,
        # human-review escalations, a live-demo YES reply, all of it. No
        # claimable filter, no cohort filter, no live-demo exclusion --
        # unlike report.py's Treatment/Holdout/Lift/Net (which deliberately
        # wall a live-demo row out to keep the MEASURED number honest),
        # this tile isn't measuring anything, it's just adding up every
        # dollar that has actually come back, so it's the one number that
        # moves the instant any recovery lands, from whatever source.
        total_recovered_minor = session.execute(
            select(func.coalesce(func.sum(RecoveryOutcome.recovered_minor), 0)).where(
                RecoveryOutcome.business_id == business_id,
                RecoveryOutcome.outcome.in_(("RECOVERED", "PARTIALLY_RECOVERED")),
            )
        ).scalar_one()
    finally:
        session.close()

    t, h = report.claimable["TREATMENT"], report.claimable["HOLDOUT"]
    lift_pp = t.rate - h.rate

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
        "suppressed_compliance": report.suppressed_compliance,
        "stopped_by_rules": report.stopped_by_rules,
        "confidently_recoverable_minor": int(confidently_recoverable_minor),
        "in_human_hands_minor": in_human_hands_minor,
        "total_recovered_minor": int(total_recovered_minor),
    }


def _likelihood_label(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 0.65:
        return "high"
    if score >= 0.40:
        return "medium"
    return "low"


def _payment_transactions(business_id: uuid.UUID, session: Session, limit: int, customer_type: str | None = None) -> list[dict]:
    query = (
        select(Payment, Customer.email)
        .outerjoin(Customer, Customer.customer_id == Payment.customer_id)
        .where(Payment.business_id == business_id)
    )
    if customer_type is not None:
        query = query.where(Customer.customer_type == customer_type)
    rows = session.execute(query.order_by(desc(Payment.initiated_at)).limit(limit)).all()

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

    score_by_at_risk: dict[uuid.UUID, float] = {}
    at_risk_ids = [v[0] for v in latest_at_risk.values()]
    if at_risk_ids:
        score_rows = session.execute(
            select(CustomerRecoveryProfile.at_risk_id, CustomerRecoveryProfile.predicted_score).where(
                CustomerRecoveryProfile.business_id == business_id,
                CustomerRecoveryProfile.at_risk_id.in_(at_risk_ids),
            )
        ).all()
        score_by_at_risk = dict(score_rows)

    result = []
    for p, email in rows:
        at_risk_id = latest_at_risk[p.payment_id][0] if p.payment_id in latest_at_risk else None
        score = score_by_at_risk.get(at_risk_id) if at_risk_id else None
        result.append({
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
            "at_risk_id": str(at_risk_id) if at_risk_id else None,
            "recovery_likelihood": _likelihood_label(score),
        })
    return result


def get_transactions_b2c(limit: int = 200) -> list[dict]:
    business_id = _business_id()
    session = SessionLocal()
    try:
        return _payment_transactions(business_id, session, limit, customer_type="B2C")
    finally:
        session.close()


def get_transactions_b2b(limit: int = 200) -> list[dict]:
    """Invoice-shaped counterpart to _payment_transactions -- B2B losses

    live in the invoices table, not payments, so they never appeared in
    the dashboard at all until this.
    """
    business_id = _business_id()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Invoice, Customer.email, Customer.canonical_name)
            .outerjoin(Customer, Customer.customer_id == Invoice.customer_id)
            .where(Invoice.business_id == business_id, Customer.customer_type == "B2B")
            .order_by(desc(Invoice.issued_at))
            .limit(limit)
        ).all()

        invoice_ids = [inv.invoice_id for inv, _, _ in rows]
        latest_at_risk: dict[uuid.UUID, tuple[uuid.UUID, datetime]] = {}
        if invoice_ids:
            ar_rows = session.execute(
                select(RevenueAtRisk.entity_id, RevenueAtRisk.at_risk_id, RevenueAtRisk.detected_at).where(
                    RevenueAtRisk.business_id == business_id,
                    RevenueAtRisk.entity_type == "INVOICE",
                    RevenueAtRisk.entity_id.in_(invoice_ids),
                )
            ).all()
            for entity_id, at_risk_id, detected_at in ar_rows:
                current = latest_at_risk.get(entity_id)
                if current is None or detected_at > current[1]:
                    latest_at_risk[entity_id] = (at_risk_id, detected_at)

        score_by_at_risk: dict[uuid.UUID, float] = {}
        at_risk_ids = [v[0] for v in latest_at_risk.values()]
        if at_risk_ids:
            score_rows = session.execute(
                select(CustomerRecoveryProfile.at_risk_id, CustomerRecoveryProfile.predicted_score).where(
                    CustomerRecoveryProfile.business_id == business_id,
                    CustomerRecoveryProfile.at_risk_id.in_(at_risk_ids),
                )
            ).all()
            score_by_at_risk = dict(score_rows)

        now = datetime.now(timezone.utc)
        result = []
        for inv, email, name in rows:
            at_risk_id = latest_at_risk[inv.invoice_id][0] if inv.invoice_id in latest_at_risk else None
            score = score_by_at_risk.get(at_risk_id) if at_risk_id else None
            days_overdue = max((now - inv.due_at).total_seconds() / 86400, 0.0) if inv.status != "PAID" else 0.0
            result.append({
                "invoice_id": str(inv.invoice_id),
                "customer_email": email,
                "customer_name": name,
                "amount_minor": inv.amount_minor,
                "amount_paid_minor": inv.amount_paid_minor,
                "currency": inv.currency,
                "status": inv.status,
                "dunning_stage": inv.dunning_stage,
                "days_overdue": round(days_overdue, 1),
                "due_at": inv.due_at.isoformat() if inv.due_at else None,
                "at_risk_id": str(at_risk_id) if at_risk_id else None,
                "recovery_likelihood": _likelihood_label(score),
            })
        return result
    finally:
        session.close()


_SEGMENT_LABELS = {
    "new": "New customers", "casual": "Casual customers", "regular": "Regular customers", "loyal": "Loyal customers",
    "micro": "Micro businesses", "sme": "Small/medium businesses", "mid": "Mid-size businesses", "enterprise": "Enterprise accounts",
}


def get_predictions(customer_type: str | None = None) -> dict:
    """Plain-language summary of what the recovery-likelihood model is
    saying about the current batch of at-risk payments -- feeds the
    dashboard's Recovery Predictions panel (charts, not raw scores).
    Optionally scoped to one customer_type, reused by the B2C/B2B tabs.
    """
    business_id = _business_id()
    session = SessionLocal()
    try:
        query = select(
            CustomerRecoveryProfile.predicted_score,
            CustomerRecoveryProfile.score_version,
            CustomerRecoveryProfile.features,
        ).where(CustomerRecoveryProfile.business_id == business_id)
        if customer_type is not None:
            query = query.join(Customer, Customer.customer_id == CustomerRecoveryProfile.customer_id).where(
                Customer.customer_type == customer_type
            )
        rows = session.execute(query).all()
    finally:
        session.close()

    if not rows:
        return {"scored_count": 0}

    n = len(rows)
    avg_score = sum(r[0] for r in rows) / n
    version = rows[0][1]

    buckets = {"high": 0, "medium": 0, "low": 0}
    for score, _version, _features in rows:
        buckets[_likelihood_label(score)] += 1

    seg_scores: dict[str, list[float]] = defaultdict(list)
    for score, _version, features in rows:
        seg = (features or {}).get("segment")
        if seg:
            seg_scores[seg].append(score)
    segments = sorted(
        (
            {"label": _SEGMENT_LABELS.get(seg, seg.title()), "avg_score": round(sum(v) / len(v), 3), "count": len(v)}
            for seg, v in seg_scores.items()
        ),
        key=lambda x: -x["avg_score"],
    )[:8]

    return {
        "scored_count": n,
        "avg_score": round(avg_score, 3),
        "buckets": buckets,
        "model_label": "Trained AI model" if version == "score_ml@v1" else "Statistical model",
        "segments": segments,
    }


def _customer_type_summary(*, customer_type: str, entity_types: tuple[str, ...]) -> dict:
    business_id = _business_id()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(RevenueAtRisk.at_risk_id, RevenueAtRisk.at_risk_minor, Customer.customer_segment)
            .join(Customer, Customer.customer_id == RevenueAtRisk.customer_id)
            .where(
                RevenueAtRisk.business_id == business_id,
                RevenueAtRisk.entity_type.in_(entity_types),
                Customer.customer_type == customer_type,
            )
        ).all()

        at_risk_ids = [at_risk_id for at_risk_id, _, _ in rows]
        recovered_minor = 0
        recovered_count = 0
        if at_risk_ids:
            for _at_risk_id, outcome, recovered in session.execute(
                select(RecoveryOutcome.at_risk_id, RecoveryOutcome.outcome, RecoveryOutcome.recovered_minor).where(
                    RecoveryOutcome.at_risk_id.in_(at_risk_ids)
                )
            ).all():
                # Both outcomes, matching get_kpis()'s total_recovered_minor and
                # app/scoring/export.py's POSITIVE_OUTCOMES. Counting only
                # RECOVERED here made the B2C/B2B tabs undercount the headline
                # tile for the same money.
                if outcome in ("RECOVERED", "PARTIALLY_RECOVERED"):
                    recovered_count += 1
                    recovered_minor += recovered
    finally:
        session.close()

    at_risk_minor = sum(minor for _, minor, _ in rows)
    segment_totals: dict[str, int] = defaultdict(int)
    for _at_risk_id, minor, segment in rows:
        segment_totals[segment or "unknown"] += minor
    segments = sorted(
        (
            {"label": _SEGMENT_LABELS.get(seg, seg.title()) if seg != "unknown" else "Unknown", "at_risk_minor": total}
            for seg, total in segment_totals.items()
        ),
        key=lambda x: -x["at_risk_minor"],
    )

    return {
        "count": len(rows),
        "at_risk_minor": at_risk_minor,
        "recovered_minor": recovered_minor,
        "recovered_count": recovered_count,
        "recovery_rate_pct": round(recovered_count / len(rows) * 100, 1) if rows else 0.0,
        "segments": segments,
    }


_DAYS_OVERDUE_BUCKETS = [("0-15", 0, 15), ("16-30", 16, 30), ("31-45", 31, 45), ("46+", 46, None)]


def _days_overdue_buckets() -> list[dict]:
    business_id = _business_id()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Invoice.due_at)
            .join(Customer, Customer.customer_id == Invoice.customer_id)
            .where(Invoice.business_id == business_id, Customer.customer_type == "B2B", Invoice.status != "PAID")
        ).all()
    finally:
        session.close()

    now = datetime.now(timezone.utc)
    buckets = {label: 0 for label, _, _ in _DAYS_OVERDUE_BUCKETS}
    for (due_at,) in rows:
        days = max((now - due_at).total_seconds() / 86400, 0.0)
        for label, lo, hi in _DAYS_OVERDUE_BUCKETS:
            if days >= lo and (hi is None or days <= hi):
                buckets[label] += 1
                break
    return [{"label": label, "count": buckets[label]} for label, _, _ in _DAYS_OVERDUE_BUCKETS]


def get_b2c_summary() -> dict:
    return _customer_type_summary(customer_type="B2C", entity_types=("PAYMENT", "CHECKOUT"))


def get_b2b_summary() -> dict:
    summary = _customer_type_summary(customer_type="B2B", entity_types=("INVOICE", "SUBSCRIPTION"))
    summary["days_overdue_buckets"] = _days_overdue_buckets()
    return summary


def get_human_review_queue() -> dict:
    """Pooled B2C+B2B queue behind the Human Review tab, ranked by

    recovery likelihood alone -- see app/recovery/human_review.py for the
    one shared definition of "awaiting human" this wraps.
    """
    from app.recovery import human_review

    business_id = _business_id()
    session = SessionLocal()
    try:
        items = human_review.human_review_queue(session, business_id)
    finally:
        session.close()

    count = len(items)
    total_at_risk_minor = sum(item["value_minor"] for item in items)
    avg_score = (sum(item["predicted_score"] for item in items) / count) if count else 0.0
    by_reason: dict[str, int] = defaultdict(int)
    for item in items:
        by_reason[item["reason"]] += 1

    return {
        "count": count,
        "total_at_risk_minor": total_at_risk_minor,
        "avg_score": round(avg_score, 3),
        "by_reason": dict(by_reason),
        "items": [
            {
                "at_risk_id": str(item["at_risk_id"]),
                "customer_id": str(item["customer_id"]) if item["customer_id"] else None,
                "customer_type": item["customer_type"],
                "entity_type": item["entity_type"],
                "value_minor": item["value_minor"],
                "currency": item["currency"],
                "predicted_score": item["predicted_score"],
                "reason": item["reason"],
                "decided_at": item["decided_at"].isoformat() if item["decided_at"] else None,
            }
            for item in items
        ],
    }


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

    recovery_attempts has no batch_id column (the same schema gap
    build_batch_evidence works around in app/explain.py), so attempts are
    correlated to this batch by falling inside [started_at, completed_at]
    -- safe here because run_batch() decides every entity synchronously
    within one call and this build never runs two batches concurrently for
    one business.
    """
    from app.explain import _inr, render_narrative

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
