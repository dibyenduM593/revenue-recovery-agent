"""score@v1 -- hand-set logistic recovery-propensity scorer.

Authored, not trained: coefficients are chosen the same way policy.py's
action table is authored rather than learned. This is the guaranteed
fallback promise made concrete -- app/scoring/port.py routes here on
RISK_MODEL=baseline (the default) and also falls back here if
RISK_MODEL=xgboost fails to load or predict, so this must never itself
depend on a model file or the DB.
"""

from __future__ import annotations

import math

SCORE_VERSION = "score@v1"

# Segments diverge mainly on tenure/loyalty (B2C) or size/discipline (B2B).
# SME is deliberately the worst B2B segment -- same story as
# seed/generator.py's SEGMENT_MULT.
SEGMENT_WEIGHT: dict[str, float] = {
    "new": -0.30, "casual": 0.00, "regular": 0.30, "loyal": 0.60,
    "micro": 0.00, "sme": -0.50, "mid": 0.30, "enterprise": 0.60,
}

FAILURE_WEIGHT: dict[str, float] = {
    "INSUFFICIENT_FUNDS": 0.20, "ISSUER_UNAVAILABLE": 0.50, "TECHNICAL_ERROR": 0.40,
    "EXPIRED_CARD": -0.10, "DO_NOT_HONOR": -0.30, "INVALID_DETAILS": -0.10,
    "ATTENTION_SLIP": -0.60, "MANDATE_REVOKED": -0.20, "STOLEN_CARD": -1.50,
}


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _terms(features: dict) -> dict[str, float]:
    """Every additive term score() sums into z, named. The single source

    both score() and explain_score() read from, so the two can never
    silently disagree about what the model actually computed -- the same
    discipline app/scoring/features.py's prepare_frame() holds for
    train/serve, applied to score/explain instead.
    """
    terms = {"intercept": -0.30}
    terms["segment"] = SEGMENT_WEIGHT.get(features.get("segment"), 0.0)
    terms["prior_recovery_rate"] = 0.80 * (
        features.get("prior_recovery_rate") if features.get("prior_recovery_rate") is not None else 0.5
    ) - 0.40
    terms["n_prior_transactions"] = min((features.get("n_prior_transactions") or 0) * 0.05, 0.50)
    terms["contacts_last_7d"] = max((features.get("contacts_last_7d") or 0) * -0.15, -0.45)
    terms["tenure_days"] = min((features.get("tenure_days") or 0) / 365 * 0.30, 0.40)

    if features.get("customer_type") == "B2C":
        terms["failure_code_canonical"] = FAILURE_WEIGHT.get(features.get("failure_code_canonical"), 0.0)
        terms["attempt_number"] = max(0.40 - 0.10 * ((features.get("attempt_number") or 1) - 1), -0.20)
    else:
        days_overdue = features.get("days_overdue") or 0.0
        terms["days_overdue"] = -min(days_overdue / 45.0 * 0.50, 1.00)
        terms["prior_late_rate"] = -(features.get("prior_late_rate") or 0.0) * 0.60
        if features.get("crosses_msmed_45d"):
            terms["crosses_msmed_45d"] = -0.30

    return terms


def score(features: dict) -> float:
    z = sum(_terms(features).values())
    return round(_sigmoid(z), 4)


def explain_score(features: dict) -> dict:
    """Same shape as app/scoring/model.py's explain_score() -- one

    contribution per named term, an explicit base_value, summing exactly
    to the same z score() computes -- except every number here is a
    literal, human-typed coefficient (SEGMENT_WEIGHT, FAILURE_WEIGHT,
    the 0.05/0.15/0.30 slopes...), not a value TreeSHAP derived from
    training data. Fully transparent by construction; nothing to explain
    that isn't already sitting in this file's own module-level tables.
    """
    terms = dict(_terms(features))
    base_value = terms.pop("intercept")
    z = base_value + sum(terms.values())
    return {
        "predicted_score": round(_sigmoid(z), 4),
        "base_value": base_value,
        "contributions": terms,
    }
