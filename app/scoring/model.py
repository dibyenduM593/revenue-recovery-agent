"""score_ml@v1 -- trained XGBoost scorer, one booster per track.

Boosters are lazy-loaded from models/b2c.json / models/b2b.json (native
XGBoost JSON, committed to the repo so `make demo` needs no training
step) and cached in-process. Never call this directly from batch.py --
always go through app/scoring/port.py, which catches load/predict
failures here and falls back to app/scoring/baseline.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import xgboost as xgb

from app.scoring.features import feature_order, prepare_frame

SCORE_VERSION = "score_ml@v1"
MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "models"

_boosters: dict[str, xgb.Booster] = {}
_categories: dict[str, dict[str, list[str]]] = {}


def _track_key(customer_type: str) -> str:
    return "b2c" if customer_type == "B2C" else "b2b"


def _decode_categories(path: Path) -> dict[str, list[str]]:
    """Read back the exact categorical vocabulary the booster was trained on.

    XGBoost's native JSON stores it as a flat character-code array per
    feature plus the offsets that delimit each string
    (`gradient_booster.model.cats.enc`). Decoding it here means serving
    never has to guess -- and means an unseen category degrades to
    missing instead of raising, which is what it did before this existed.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    learner = raw.get("learner", {})
    names = learner.get("feature_names", [])
    types = learner.get("feature_types", [])
    encs = learner.get("gradient_booster", {}).get("model", {}).get("cats", {}).get("enc", [])

    # enc is indexed by the feature's own position, NOT by a running count
    # of categorical features -- numeric features occupy their slot with an
    # empty entry. Counting only the categoricals reads the wrong slots and
    # yields empty vocabularies for everything after the first numeric
    # column, which then fails as "must have at least one category".
    out: dict[str, list[str]] = {}
    for index, (name, ftype) in enumerate(zip(names, types)):
        if ftype != "c" or index >= len(encs):
            continue
        enc = encs[index]
        offsets, values = enc.get("offsets") or [], enc.get("values") or []
        out[name] = [
            "".join(chr(c) for c in values[offsets[i]:offsets[i + 1]])
            for i in range(len(offsets) - 1)
        ]
    return out


def _load(customer_type: str) -> tuple[xgb.Booster, dict[str, list[str]]]:
    key = _track_key(customer_type)
    if key not in _boosters:
        path = MODELS_DIR / f"{key}.json"
        booster = xgb.Booster()
        booster.load_model(str(path))  # raises if the file is missing/corrupt
        _boosters[key] = booster
        _categories[key] = _decode_categories(path)
    return _boosters[key], _categories[key]


def score(features: dict) -> float:
    customer_type = features.get("customer_type", "B2C")
    booster, known_categories = _load(customer_type)
    columns = feature_order(customer_type)
    row = {c: features.get(c) for c in columns}
    frame = prepare_frame(pd.DataFrame([row]), customer_type, known_categories=known_categories)
    dmatrix = xgb.DMatrix(frame, enable_categorical=True)
    return float(booster.predict(dmatrix)[0])


def explain_score(features: dict) -> dict:
    """Real per-feature attribution for THIS row's prediction, not an LLM

    guessing at a black box. XGBoost's pred_contribs (TreeSHAP under the
    hood) returns one contribution per input feature, in the exact order
    columns were given, plus one final "bias" column -- and they sum
    EXACTLY to the row's own margin (verified: -2.786017 both ways on a
    real row), so this is an identity, not an approximation. Positive
    means "pushed the score up," negative "pushed it down," and the
    magnitudes are directly comparable to each other for this one row.
    """
    customer_type = features.get("customer_type", "B2C")
    booster, known_categories = _load(customer_type)
    columns = feature_order(customer_type)
    row = {c: features.get(c) for c in columns}
    frame = prepare_frame(pd.DataFrame([row]), customer_type, known_categories=known_categories)
    dmatrix = xgb.DMatrix(frame, enable_categorical=True)
    contribs = booster.predict(dmatrix, pred_contribs=True)[0]

    return {
        "predicted_score": float(booster.predict(dmatrix)[0]),
        "base_value": float(contribs[-1]),
        "contributions": {col: float(val) for col, val in zip(columns, contribs[:-1])},
    }
