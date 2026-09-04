"""Training corpus export.

Labels only exist once the full pipeline has actually run: generate ->
normalize -> detect -> batch -> simulate_world -> attribution. There is
no shortcut that preserves the cardinal rule (nothing written directly
to a canonical or ledger table) -- this reads back a scaled run of the
real pipeline, it is not a separate synthetic data path.

Reads canonical tables + recovery_outcomes for one business, builds one
row per resolved at-risk item via app/scoring/features.py, and writes
b2c.parquet / b2b.parquet. `archetype` is included on each row for the
diversity report (app/scoring/diversity.py) only -- it is never a model
feature (see features.py's docstring on why).
"""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Customer, RecoveryOutcome, RevenueAtRisk
from app.scoring.features import build_feature_context, build_features
from seed.generator import GeneratorConfig, business_id_for, customer_profile_for
from seed.reference import DEMO_SEED

# PARTIALLY_RECOVERED counts as a positive label too -- some money moved,
# which is what the score is meant to predict the likelihood of.
POSITIVE_OUTCOMES = {"RECOVERED", "PARTIALLY_RECOVERED"}


def export_rows(session: Session, business_id: uuid.UUID, seed: int) -> tuple[list[dict], list[dict]]:
    rows = session.execute(
        select(RevenueAtRisk, RecoveryOutcome)
        .join(RecoveryOutcome, RecoveryOutcome.at_risk_id == RevenueAtRisk.at_risk_id)
        .where(RevenueAtRisk.business_id == business_id)
        .order_by(RevenueAtRisk.entity_type, RevenueAtRisk.entity_id)
    ).all()

    b2c_rows: list[dict] = []
    b2b_rows: list[dict] = []

    # Same bulk-load-once fix as app/recovery/batch.py: one context built
    # here instead of build_features()'s four history queries per row --
    # at a training-corpus row count that's the difference between minutes
    # and the better part of an hour.
    context = build_feature_context(session, business_id) if rows else None

    for at_risk, outcome in rows:
        if at_risk.customer_id is None:
            continue
        customer = session.get(Customer, at_risk.customer_id)
        if customer is None or not customer.email:
            continue

        features = build_features(session, at_risk, customer, context=context)
        profile = customer_profile_for(customer.email, seed)

        record = dict(features)
        record["customer_id"] = str(customer.customer_id)
        record["at_risk_id"] = str(at_risk.at_risk_id)
        record["archetype"] = profile.archetype
        record["label"] = 1 if outcome.outcome in POSITIVE_OUTCOMES else 0

        (b2c_rows if features["customer_type"] == "B2C" else b2b_rows).append(record)

    return b2c_rows, b2b_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the training corpus from the DB.")
    parser.add_argument("--seed", type=int, default=DEMO_SEED)
    parser.add_argument("--out", type=Path, default=Path("data"))
    args = parser.parse_args()

    business_id = business_id_for(GeneratorConfig(seed=args.seed))
    session = SessionLocal()
    try:
        b2c_rows, b2b_rows = export_rows(session, business_id, args.seed)
    finally:
        session.close()

    args.out.mkdir(parents=True, exist_ok=True)
    b2c_path = args.out / "b2c.parquet"
    b2b_path = args.out / "b2b.parquet"
    pd.DataFrame(b2c_rows).to_parquet(b2c_path, index=False)
    pd.DataFrame(b2b_rows).to_parquet(b2b_path, index=False)

    print(f"business_id: {business_id}")
    print(f"b2c rows: {len(b2c_rows)} -> {b2c_path}")
    print(f"b2b rows: {len(b2b_rows)} -> {b2b_path}")


if __name__ == "__main__":
    main()
