"""Gate C: the measured batch report, computed end to end from the ledger

-- never a re-derivation from raw events, and only STRONG-attribution
recoveries ever enter the claimable headline. Weak-attribution recoveries
are real money, just not proven enough to claim; reported separately,
never folded in.

NET is not given a literal formula anywhere in the plan (the example
batch report's own numbers do not appear to be internally arithmetically
consistent -- it reads as illustrative, not a worked example) -- this
implementation defines it as: what treatment recovered, minus what the
same money would have recovered organically at the holdout's own rate.
Flagged for review like everything else without a literal spec.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RecoveryAttempt, RecoveryOutcome, RevenueAtRisk

NON_CLAIMABLE_SEGMENT_CATEGORY = "X_INSUFFICIENT_FUNDS"
EXCLUDED_CATEGORIES = ("X_FRAUD", "X_BANK_BLOCK")

_COMPLIANCE_PREFIXES = ("h1_", "h2_", "h4_", "h5_", "h6_", "h9_")
_RULE_REASONS_PREFIXES = ("h3_", "h7_", "h8_", "h10_")


@dataclass
class CohortStats:
    total: int = 0
    recovered_strong: int = 0
    recovered_any: int = 0
    recovered_minor: int = 0
    at_risk_minor: int = 0

    @property
    def rate(self) -> float:
        return (self.recovered_strong / self.total * 100) if self.total else 0.0


@dataclass
class BatchReport:
    total_items: int
    total_at_risk_minor: int
    claimable: dict[str, CohortStats]
    claimable_weak_excluded_minor: int
    segment: dict[str, CohortStats]
    suppressed_compliance: int = 0
    stopped_by_rules: int = 0
    held_for_approval: int = 0


def _cohort_for(session: Session, business_id: uuid.UUID, category_filter) -> dict[str, CohortStats]:
    stats = {"TREATMENT": CohortStats(), "HOLDOUT": CohortStats()}

    at_risk_rows = session.execute(
        select(RevenueAtRisk).where(RevenueAtRisk.business_id == business_id, category_filter)
    ).scalars().all()

    for at_risk in at_risk_rows:
        attempt = session.execute(
            select(RecoveryAttempt)
            .where(RecoveryAttempt.at_risk_id == at_risk.at_risk_id)
            .order_by(RecoveryAttempt.attempt_number.desc())
            .limit(1)
        ).scalars().first()
        if attempt is None:
            continue
        cohort = stats[attempt.cohort]
        cohort.total += 1
        cohort.at_risk_minor += at_risk.at_risk_minor

        outcome = session.execute(
            select(RecoveryOutcome).where(RecoveryOutcome.at_risk_id == at_risk.at_risk_id)
        ).scalars().first()
        if outcome is not None and outcome.outcome == "RECOVERED":
            cohort.recovered_any += 1
            cohort.recovered_minor += outcome.recovered_minor
            if outcome.attribution_confidence == "STRONG":
                cohort.recovered_strong += 1

    return stats


def build_report(session: Session, business_id: uuid.UUID) -> BatchReport:
    all_at_risk = session.execute(select(RevenueAtRisk).where(RevenueAtRisk.business_id == business_id)).scalars().all()
    total_items = len(all_at_risk)
    total_at_risk_minor = sum(r.at_risk_minor for r in all_at_risk)

    claimable_filter = RevenueAtRisk.claimable.is_(True)
    claimable = _cohort_for(session, business_id, claimable_filter)

    segment_filter = RevenueAtRisk.loss_category == NON_CLAIMABLE_SEGMENT_CATEGORY
    segment = _cohort_for(session, business_id, segment_filter)

    # Weak-attribution recoveries within the claimable population, excluded from the headline.
    weak_minor = 0
    for at_risk in all_at_risk:
        if not at_risk.claimable:
            continue
        outcome = session.execute(
            select(RecoveryOutcome).where(RecoveryOutcome.at_risk_id == at_risk.at_risk_id)
        ).scalars().first()
        if outcome is not None and outcome.outcome == "RECOVERED" and outcome.attribution_confidence == "WEAK":
            weak_minor += outcome.recovered_minor

    suppressed_compliance = 0
    stopped_by_rules = 0
    held_for_approval = 0
    latest_attempts = session.execute(
        select(RecoveryAttempt.suppressed_reason, RecoveryAttempt.at_risk_id).where(RecoveryAttempt.business_id == business_id)
    ).all()
    seen_at_risk = set()
    for suppressed_reason, at_risk_id in latest_attempts:
        if at_risk_id in seen_at_risk:
            continue
        seen_at_risk.add(at_risk_id)
        if not suppressed_reason:
            continue
        if suppressed_reason.startswith(_COMPLIANCE_PREFIXES):
            suppressed_compliance += 1
        elif suppressed_reason.startswith(_RULE_REASONS_PREFIXES) or suppressed_reason in ("recovery_disabled_for_business", "emergency_stop", "dry_run"):
            stopped_by_rules += 1
        elif suppressed_reason == "held_for_approval":
            held_for_approval += 1

    return BatchReport(
        total_items=total_items, total_at_risk_minor=total_at_risk_minor,
        claimable=claimable, claimable_weak_excluded_minor=weak_minor, segment=segment,
        suppressed_compliance=suppressed_compliance, stopped_by_rules=stopped_by_rules, held_for_approval=held_for_approval,
    )


def _inr(minor: int) -> str:
    rupees = minor // 100
    s = str(abs(rupees))
    if len(s) <= 3:
        grouped = s
    else:
        last3, rest = s[-3:], s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    sign = "-" if rupees < 0 else ""
    return f"{sign}₹{grouped}"


def render(report: BatchReport) -> str:
    t, h = report.claimable["TREATMENT"], report.claimable["HOLDOUT"]
    lift = t.rate - h.rate
    baseline_expected_minor = int(t.at_risk_minor * (h.rate / 100)) if t.at_risk_minor else 0
    net_minor = t.recovered_minor - baseline_expected_minor

    st, sh = report.segment["TREATMENT"], report.segment["HOLDOUT"]
    segment_lift = st.rate - sh.rate

    lines = [
        f"Batch {report.total_items} items · {_inr(report.total_at_risk_minor)} at risk",
        "CLAIMABLE (business fault + customer sentiment)",
        f"  Treatment {t.total} · {t.recovered_strong} recovered · {_inr(t.recovered_minor)}",
        f"  Holdout   {h.total} · {h.recovered_strong} recovered · {_inr(h.recovered_minor)}",
        f"  {h.rate:.1f}% → {t.rate:.1f}%   lift {lift:+.1f}pp   NET {_inr(net_minor)}   [STRONG attribution]",
        f"  Weak-attribution recoveries (excluded): {_inr(report.claimable_weak_excluded_minor)}",
        "SEGMENT — insufficient funds (reported, not claimed)",
        f"  Treatment {st.total} · {st.recovered_strong} recovered   Holdout {sh.total} · {sh.recovered_strong} recovered",
        f"  {sh.rate:.1f}% → {st.rate:.1f}%   lift {segment_lift:+.1f}pp",
        "NOT ACTIONED",
        f"  Suppressed by compliance {report.suppressed_compliance} · Stopped by rules {report.stopped_by_rules} "
        f"· Held for approval {report.held_for_approval}",
    ]
    return "\n".join(lines)


def main() -> None:
    import argparse
    import sys

    from app.db import SessionLocal
    from seed.generator import GeneratorConfig, business_id_for

    # Windows consoles default to cp1252, which can't encode the report's
    # rupee sign -- reconfigure rather than let a demo run crash on output.
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Render the Gate C batch report from the current ledger.")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    business_id = business_id_for(GeneratorConfig(seed=args.seed))
    session = SessionLocal()
    try:
        report = build_report(session, business_id)
    finally:
        session.close()

    print(render(report))


if __name__ == "__main__":
    main()
