"""Diversity checks against the training corpus (v6 plan SS6).

Run once after export.py; writes docs/diversity-report.md.

1. Coverage heatmap and 2. per-archetype distributions are the
demo-worthy visuals (rendered as markdown tables here rather than
plots -- no charting dependency added for this). 3. entropy vs the
designed distribution and 4. Cramer's V between fields designed to be
independent are numbers for the README: entropy catches a parameter
silently collapsing, Cramer's V catches hidden coupling that shouldn't
be there.
"""

from __future__ import annotations

import argparse
import math
from collections import Counter
from pathlib import Path

import pandas as pd

DESIGNED_SEGMENT_WEIGHTS = {
    "B2C": {"new": 0.30, "casual": 0.30, "regular": 0.25, "loyal": 0.15},
    "B2B": {"micro": 0.35, "sme": 0.35, "mid": 0.20, "enterprise": 0.10},
}
DESIGNED_SECTOR_WEIGHTS = {"retail": 0.30, "saas": 0.15, "services": 0.20, "logistics": 0.15, "healthcare": 0.10, "education": 0.10}
DESIGNED_BUSINESS_MODEL_WEIGHTS = {"product": 0.60, "service": 0.40}

ARCHETYPE_NUMERIC_FIELDS = ["tenure_days", "amount_volatility", "prior_recovery_rate"]
B2B_NUMERIC_FIELDS = ["days_overdue", "prior_late_rate", "mean_days_late_prior"]


def _entropy(counts: Counter) -> float:
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts.values() if c > 0)


def _designed_entropy(weights: dict[str, float]) -> float:
    return -sum(w * math.log2(w) for w in weights.values() if w > 0)


def _cramers_v(frame: pd.DataFrame, col_a: str, col_b: str) -> float:
    table = pd.crosstab(frame[col_a], frame[col_b])
    n = table.values.sum()
    if n == 0 or table.shape[0] < 2 or table.shape[1] < 2:
        return 0.0
    row_sums = table.sum(axis=1).to_numpy()
    col_sums = table.sum(axis=0).to_numpy()
    expected = (row_sums[:, None] * col_sums[None, :]) / n
    chi2 = ((table.to_numpy() - expected) ** 2 / expected).sum()
    k = min(table.shape) - 1
    return math.sqrt(chi2 / (n * k)) if k > 0 else 0.0


def _section_for(label: str, frame: pd.DataFrame) -> list[str]:
    lines = [f"## {label} ({len(frame)} rows)", ""]

    lines.append("### 1. Coverage: segment x sector")
    lines.append("")
    lines.append(pd.crosstab(frame["segment"], frame["sector"]).to_markdown())
    lines.append("")

    lines.append("### 2. Distribution by archetype")
    lines.append("")
    numeric_fields = ARCHETYPE_NUMERIC_FIELDS + (B2B_NUMERIC_FIELDS if label == "B2B" else [])
    numeric_fields = [f for f in numeric_fields if f in frame.columns]
    if "archetype" in frame.columns and numeric_fields:
        summary = frame.groupby("archetype")[numeric_fields].mean().round(3)
        summary["n"] = frame.groupby("archetype").size()
        lines.append(summary.to_markdown())
    lines.append("")

    lines.append("### 3. Entropy vs designed distribution (bits)")
    lines.append("")
    segment_weights = DESIGNED_SEGMENT_WEIGHTS[label]
    for field, designed in (
        ("segment", segment_weights),
        ("sector", DESIGNED_SECTOR_WEIGHTS),
        ("business_model", DESIGNED_BUSINESS_MODEL_WEIGHTS),
    ):
        observed_entropy = _entropy(Counter(frame[field]))
        designed_entropy = _designed_entropy(designed)
        lines.append(f"- `{field}`: observed={observed_entropy:.3f}, designed={designed_entropy:.3f}")
    lines.append("")

    lines.append("### 4. Cramer's V -- independence check (near 0 expected)")
    lines.append("")
    frame = frame.copy()
    frame["amount_volatility_bucket"] = pd.cut(frame["amount_volatility"], bins=5, labels=False, duplicates="drop")
    v = _cramers_v(frame, "sector", "amount_volatility_bucket")
    lines.append(f"- sector x amount_volatility: {v:.3f}")
    lines.append("")

    return lines


def build_report(b2c: pd.DataFrame, b2b: pd.DataFrame) -> str:
    lines = ["# Diversity report", ""]
    if len(b2c):
        lines += _section_for("B2C", b2c)
    if len(b2b):
        lines += _section_for("B2B", b2b)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diversity checks against the training corpus.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("docs/diversity-report.md"))
    args = parser.parse_args()

    b2c = pd.read_parquet(args.data_dir / "b2c.parquet")
    b2b = pd.read_parquet(args.data_dir / "b2b.parquet")

    report = build_report(b2c, b2b)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
