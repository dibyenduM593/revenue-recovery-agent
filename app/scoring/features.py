"""at_risk row + customer -> feature dict.

This is the ONLY place features are defined. Both the training export
(app/scoring/export.py) and runtime scoring (app/scoring/baseline.py,
app/scoring/model.py) go through build_features() -- if they defined
features separately they would drift, and the model would end up scoring
production data shaped differently from what it trained on.

customer_id is used only for the train/test group split in
app/scoring/train.py; it is never itself a feature. amount is never a
feature either -- it enters only at prioritisation time as
predicted_score * at_risk_minor (see app/recovery/batch.py), per the
design note that amount is heavily repeated per customer and what
actually varies is business_model, which IS a feature.

Every query here is bounded by `at_risk.detected_at` (aliased `before`)
so a feature can never see data from after the point it would have been
computed at -- the same no-leakage discipline recovery_attempts already
applies via decided_at/executed_at.
"""

from __future__ import annotations

import statistics
import uuid
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Customer, Invoice, Payment, RecoveryAttempt, RevenueAtRisk
from seed.generator import customer_profile_for
from seed.reference import DEMO_SEED

SHARED_FEATURES = [
    "customer_type", "segment", "sector", "business_model", "tenure_days",
    "n_prior_transactions", "prior_recovery_rate", "days_since_last_success",
    "contacts_last_7d", "amount_volatility",
]
B2C_FEATURES = [
    "failure_code_canonical", "attempt_number", "payment_method",
    "issuer_bank", "hour_of_day", "is_checkout_abandon",
]
B2B_FEATURES = [
    "days_overdue", "prior_late_rate", "mean_days_late_prior",
    "invoice_cycle_index", "crosses_msmed_45d",
]

RESOLVED_STATUSES = ("RECOVERED", "LOST", "EXPIRED")

# XGBoost's categorical dtype needs at least one observed category -- a
# column that's null for every row in a slice (payment_method currently is,
# a pre-existing normalize-pipeline gap: nothing maps the raw `method`
# field to it yet) crashes DMatrix construction with "n_categories > 0 (0
# vs 0)" otherwise. Both train.py and model.py fillna() with this before
# casting to category, so a missing value is a real, if uninformative,
# category rather than a crash -- at train time and at single-row predict
# time alike.
CATEGORICAL_FILL = "__missing__"

# Which features are categorical is DECLARED, never inferred from the data.
# Inferring it (pandas dtype sniffing) silently disagrees between training
# and serving: on a full training frame `days_since_last_success` is
# float64 with NaNs, but on a single prediction row whose value is None
# pandas infers `object` -- so serving cast a numeric column to `category`
# and every real prediction died with "The data type doesn't match the one
# used in the training dataset", falling back to baseline without anyone
# noticing. Same reason build_features() is the only place features are
# defined: the train/serve pair has to agree by construction, not by two
# heuristics happening to land on the same answer.
CATEGORICAL_FEATURES = frozenset({
    "customer_type", "segment", "sector", "business_model",
    "failure_code_canonical", "payment_method", "issuer_bank",
})


def prepare_frame(frame, customer_type: str, known_categories: dict[str, list[str]] | None = None):
    """Coerce a feature frame into exactly the dtypes the booster expects.

    Shared verbatim by app/scoring/train.py and app/scoring/model.py --
    one implementation, so training and serving cannot drift.

    known_categories pins each categorical column to the vocabulary the
    booster was actually trained on (serving passes it; training doesn't,
    because training is what defines that vocabulary). A value outside it
    becomes NaN, which XGBoost reads as missing -- without this, the first
    unseen category at serving time raises "Found a category not in the
    training set" and the whole row silently falls back to baseline.
    """
    import pandas as pd

    x = frame[feature_order(customer_type)].copy()
    for col in x.columns:
        if col in CATEGORICAL_FEATURES:
            values = x[col].astype("object").fillna(CATEGORICAL_FILL)
            if known_categories is not None:
                x[col] = pd.Categorical(values, categories=known_categories.get(col, []))
            else:
                x[col] = values.astype("category")
        elif pd.api.types.is_bool_dtype(x[col]):
            continue
        else:
            # to_numeric, not a dtype check: a single-row frame holding None
            # arrives as object and must still land as float64 with NaN.
            x[col] = pd.to_numeric(x[col], errors="coerce")
    return x


def feature_order(customer_type: str) -> list[str]:
    return SHARED_FEATURES + (B2C_FEATURES if customer_type == "B2C" else B2B_FEATURES)


@dataclass
class FeatureContext:
    """Every customer's history, bulk-loaded once per batch.

    build_features() is called once per at-risk candidate, and a batch can
    hold tens of thousands of them; issuing this module's four history
    queries per candidate is ~5 round trips x N, which measured at ~51
    minutes for a 39,000-item corpus run. Loading each table once and
    slicing it in memory turns that into four queries total. Pass one of
    these to build_features(context=...) when scoring more than a handful
    of rows; omit it and each call falls back to querying for itself,
    which is what runtime single-item scoring wants.
    """

    payments: dict[uuid.UUID, list[Payment]] = field(default_factory=lambda: defaultdict(list))
    invoices: dict[uuid.UUID, list[Invoice]] = field(default_factory=lambda: defaultdict(list))
    at_risk: dict[uuid.UUID, list[RevenueAtRisk]] = field(default_factory=lambda: defaultdict(list))
    contacts: dict[uuid.UUID, list[datetime]] = field(default_factory=lambda: defaultdict(list))


def build_feature_context(session: Session, business_id: uuid.UUID) -> FeatureContext:
    ctx = FeatureContext()

    for payment in session.execute(
        select(Payment).where(Payment.business_id == business_id).order_by(Payment.initiated_at)
    ).scalars():
        ctx.payments[payment.customer_id].append(payment)

    for invoice in session.execute(
        select(Invoice).where(Invoice.business_id == business_id).order_by(Invoice.issued_at)
    ).scalars():
        ctx.invoices[invoice.customer_id].append(invoice)

    for row in session.execute(
        select(RevenueAtRisk).where(RevenueAtRisk.business_id == business_id).order_by(RevenueAtRisk.detected_at)
    ).scalars():
        ctx.at_risk[row.customer_id].append(row)

    for customer_id, executed_at in session.execute(
        select(RecoveryAttempt.customer_id, RecoveryAttempt.executed_at).where(
            RecoveryAttempt.business_id == business_id, RecoveryAttempt.executed_at.isnot(None)
        ).order_by(RecoveryAttempt.executed_at)
    ).all():
        ctx.contacts[customer_id].append(executed_at)

    return ctx


def _prior_payments(session: Session, customer_id: uuid.UUID, before: datetime) -> list[Payment]:
    return session.execute(
        select(Payment)
        .where(Payment.customer_id == customer_id, Payment.initiated_at < before)
        .order_by(Payment.initiated_at)
    ).scalars().all()


def _prior_invoices(session: Session, customer_id: uuid.UUID, before: datetime) -> list[Invoice]:
    return session.execute(
        select(Invoice)
        .where(Invoice.customer_id == customer_id, Invoice.issued_at < before)
        .order_by(Invoice.issued_at)
    ).scalars().all()


def _prior_at_risk(session: Session, customer_id: uuid.UUID, before: datetime, exclude_id: uuid.UUID) -> list[RevenueAtRisk]:
    return session.execute(
        select(RevenueAtRisk).where(
            RevenueAtRisk.customer_id == customer_id,
            RevenueAtRisk.detected_at < before,
            RevenueAtRisk.at_risk_id != exclude_id,
        )
    ).scalars().all()


def _contacts_last_7d(session: Session, customer_id: uuid.UUID, before: datetime) -> int:
    since = before - timedelta(days=7)
    return session.execute(
        select(func.count(RecoveryAttempt.attempt_id)).where(
            RecoveryAttempt.customer_id == customer_id,
            RecoveryAttempt.executed_at.isnot(None),
            RecoveryAttempt.executed_at >= since,
            RecoveryAttempt.executed_at < before,
        )
    ).scalar_one()


def build_features(
    session: Session, at_risk: RevenueAtRisk, customer: Customer, context: FeatureContext | None = None
) -> dict:
    profile = customer_profile_for(customer.email or "", DEMO_SEED)
    before = at_risk.detected_at

    if context is None:
        prior_payments = _prior_payments(session, customer.customer_id, before)
        prior_invoices = _prior_invoices(session, customer.customer_id, before)
        prior_at_risk = _prior_at_risk(session, customer.customer_id, before, at_risk.at_risk_id)
        contacts_last_7d = _contacts_last_7d(session, customer.customer_id, before)
    else:
        # Same `< before` cutoff as the queries above, applied to the
        # pre-sorted in-memory lists -- bisect over the sort key rather
        # than a linear scan, since one customer can carry hundreds of rows.
        cid = customer.customer_id
        cached_payments = context.payments.get(cid, [])
        cut = bisect_left([p.initiated_at for p in cached_payments], before)
        prior_payments = cached_payments[:cut]

        cached_invoices = context.invoices.get(cid, [])
        cut = bisect_left([i.issued_at for i in cached_invoices], before)
        prior_invoices = cached_invoices[:cut]

        cached_at_risk = context.at_risk.get(cid, [])
        cut = bisect_left([r.detected_at for r in cached_at_risk], before)
        prior_at_risk = [r for r in cached_at_risk[:cut] if r.at_risk_id != at_risk.at_risk_id]

        cached_contacts = context.contacts.get(cid, [])
        since = before - timedelta(days=7)
        contacts_last_7d = bisect_left(cached_contacts, before) - bisect_left(cached_contacts, since)

    resolved_prior = [r for r in prior_at_risk if r.status in RESOLVED_STATUSES]
    recovered_prior = [r for r in resolved_prior if r.status == "RECOVERED"]
    # No history -> neutral prior, not zero: an unscored new customer
    # shouldn't look like a guaranteed loss.
    prior_recovery_rate = (len(recovered_prior) / len(resolved_prior)) if resolved_prior else 0.5

    successful_payments = [p for p in prior_payments if p.payment_status == "CAPTURED" and p.completed_at]
    paid_invoices = [i for i in prior_invoices if i.paid_at is not None]
    last_success_candidates = [p.completed_at for p in successful_payments] + [i.paid_at for i in paid_invoices]
    last_success_at = max(last_success_candidates) if last_success_candidates else None
    days_since_last_success = (
        (before - last_success_at).total_seconds() / 86400 if last_success_at else None
    )

    amounts = [p.amount_minor for p in prior_payments]
    mean_amount = statistics.mean(amounts) if amounts else 0
    amount_volatility = (statistics.pstdev(amounts) / mean_amount) if len(amounts) >= 2 and mean_amount > 0 else 0.0

    tenure_days = max((before - customer.created_at).total_seconds() / 86400, 0.0) if customer.created_at else 0.0

    features: dict = {
        "customer_type": profile.customer_type,
        "segment": profile.segment,
        "sector": profile.sector,
        "business_model": profile.business_model,
        "tenure_days": tenure_days,
        "n_prior_transactions": len(prior_payments) + len(prior_invoices),
        "prior_recovery_rate": prior_recovery_rate,
        "days_since_last_success": days_since_last_success,
        "contacts_last_7d": contacts_last_7d,
        "amount_volatility": amount_volatility,
    }

    if profile.customer_type == "B2C":
        payment = session.get(Payment, at_risk.entity_id) if at_risk.entity_type == "PAYMENT" else None
        features.update({
            "failure_code_canonical": at_risk.failure_reason,
            "attempt_number": payment.attempt_number if payment else 1,
            "payment_method": payment.payment_method if payment else None,
            "issuer_bank": payment.issuer_bank if payment else None,
            "hour_of_day": before.hour,
            "is_checkout_abandon": at_risk.entity_type == "CHECKOUT",
        })
    else:
        invoice = session.get(Invoice, at_risk.entity_id) if at_risk.entity_type == "INVOICE" else None
        days_overdue = max((before - invoice.due_at).total_seconds() / 86400, 0.0) if invoice else 0.0
        late_invoices = [i for i in prior_invoices if i.paid_at and i.paid_at > i.due_at]
        late_days = [(i.paid_at - i.due_at).total_seconds() / 86400 for i in late_invoices]
        features.update({
            "days_overdue": days_overdue,
            "prior_late_rate": (len(late_invoices) / len(prior_invoices)) if prior_invoices else 0.0,
            "mean_days_late_prior": statistics.mean(late_days) if late_days else 0.0,
            "invoice_cycle_index": len(prior_invoices) + 1,
            # India's MSMED Act: payment to a registered MSME becomes
            # overdue-with-penalty past 45 days -- a real B2B risk signal,
            # not an arbitrary threshold.
            "crosses_msmed_45d": days_overdue > 45,
        })

    return features
