"""Trains score_ml@v1 for one track (B2C or B2B) from its exported parquet.

GroupKFold by customer_id, never a row-level split -- a customer's own
transactions must not leak across train/holdout, or the reported AUC is
fiction. Checkpoints at 5k/10k/20k rows record holdout AUC at each size
(the learning-curve chart); a checkpoint larger than the corpus is
simply skipped.

Two things worth actually looking at in the printed report, not just
filing away: (1) do feature importances match the causal story --
segment/behavioural features and failure_code_canonical should matter,
not hour_of_day; (2) is holdout AUC suspiciously high (>0.90 means the
model recovered the response-model multiplier table, not a behavioural
pattern -- the target band is 0.70-0.80, which is what the hidden
component in seed/generator.py's latent_propensity() is there to buy).

Output: models/b2c.json / models/b2b.json, XGBoost's native JSON format,
committed to the repo so `make demo` needs no training step.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

from app.scoring.features import prepare_frame

MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "models"
CHECKPOINTS = [5_000, 10_000, 20_000]
XGB_PARAMS = {"objective": "binary:logistic", "eval_metric": "auc", "max_depth": 4}
NUM_BOOST_ROUND = 100


def _prepare(frame: pd.DataFrame, customer_type: str) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    # Shared with app/scoring/model.py -- see prepare_frame's docstring for
    # what inferring these dtypes separately silently did to serving.
    x = prepare_frame(frame, customer_type)
    y = frame["label"].astype(int)
    groups = frame["customer_id"]
    return x, y, groups


def _holdout_auc(x: pd.DataFrame, y: pd.Series, groups: pd.Series) -> float:
    n_splits = min(5, groups.nunique())
    if n_splits < 2 or y.nunique() < 2:
        return float("nan")
    gkf = GroupKFold(n_splits=n_splits)
    train_idx, test_idx = next(gkf.split(x, y, groups))
    if y.iloc[test_idx].nunique() < 2:
        return float("nan")
    dtrain = xgb.DMatrix(x.iloc[train_idx], label=y.iloc[train_idx], enable_categorical=True)
    dtest = xgb.DMatrix(x.iloc[test_idx], label=y.iloc[test_idx], enable_categorical=True)
    booster = xgb.train(XGB_PARAMS, dtrain, num_boost_round=NUM_BOOST_ROUND)
    preds = booster.predict(dtest)
    return roc_auc_score(y.iloc[test_idx], preds)


def train_track(customer_type: str, parquet_path: Path) -> dict:
    frame = pd.read_parquet(parquet_path)
    x, y, groups = _prepare(frame, customer_type)

    learning_curve = {}
    for checkpoint in CHECKPOINTS:
        if len(frame) < checkpoint:
            continue
        sub = frame.sample(n=checkpoint, random_state=42)
        sx, sy, sg = _prepare(sub, customer_type)
        learning_curve[checkpoint] = _holdout_auc(sx, sy, sg)

    full_auc = _holdout_auc(x, y, groups)

    dfull = xgb.DMatrix(x, label=y, enable_categorical=True)
    booster = xgb.train(XGB_PARAMS, dfull, num_boost_round=NUM_BOOST_ROUND)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    key = "b2c" if customer_type == "B2C" else "b2b"
    model_path = MODELS_DIR / f"{key}.json"
    booster.save_model(str(model_path))

    importances = booster.get_score(importance_type="gain")

    return {
        "customer_type": customer_type,
        "n_rows": len(frame),
        "n_customers": int(groups.nunique()),
        "positive_rate": float(y.mean()) if len(y) else float("nan"),
        "holdout_auc": full_auc,
        "learning_curve": learning_curve,
        "feature_importances": importances,
        "model_path": str(model_path),
    }


def _fmt_auc(auc: float) -> str:
    return f"{auc:.4f}" if auc == auc else "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description="Train score_ml@v1 for B2C and/or B2B.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--track", choices=["b2c", "b2b", "both"], default="both")
    args = parser.parse_args()

    reports = []
    if args.track in ("b2c", "both"):
        reports.append(train_track("B2C", args.data_dir / "b2c.parquet"))
    if args.track in ("b2b", "both"):
        reports.append(train_track("B2B", args.data_dir / "b2b.parquet"))

    for report in reports:
        print(f"--- {report['customer_type']} ---")
        print(f"  rows: {report['n_rows']}  customers: {report['n_customers']}  positive_rate: {report['positive_rate']:.3f}")
        print(f"  holdout AUC: {_fmt_auc(report['holdout_auc'])}")
        for checkpoint, auc in report["learning_curve"].items():
            print(f"    @{checkpoint}: AUC={_fmt_auc(auc)}")
        top = sorted(report["feature_importances"].items(), key=lambda kv: -kv[1])[:8]
        print("  top features (gain): " + ", ".join(f"{k}={v:.1f}" for k, v in top))
        print(f"  saved -> {report['model_path']}")


if __name__ == "__main__":
    main()
