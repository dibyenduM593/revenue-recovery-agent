"""RISK_MODEL=baseline|xgboost|off -> callable.

Mirrors the four-seam discipline already used by CHANNEL_PROVIDER,
PAYMENT_LINK_PROVIDER, and CLOCK: one environment variable switches the
implementation, nothing downstream needs to know which. Default is
baseline. RISK_MODEL=xgboost that fails to load or predict logs and
falls back to baseline rather than crashing the batch -- the fallback
promise, implemented rather than claimed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.scoring import baseline

logger = logging.getLogger(__name__)

RISK_MODEL = os.environ.get("RISK_MODEL", "baseline")


def score(features: dict) -> tuple[float, str]:
    """Returns (predicted_score, score_version)."""
    if RISK_MODEL == "off":
        return 0.5, "off"
    if RISK_MODEL == "xgboost":
        try:
            from app.scoring import model

            return model.score(features), model.SCORE_VERSION
        except Exception:
            logger.warning("RISK_MODEL=xgboost failed to score; falling back to baseline", exc_info=True)
    return baseline.score(features), baseline.SCORE_VERSION


def explain_score(features: dict) -> Optional[dict]:
    """Returns {predicted_score, base_value, contributions, score_version}

    or None if there's nothing to explain (RISK_MODEL=off has no model
    behind it at all). Same xgboost-first, baseline-fallback shape as
    score() -- a model that fails to explain itself falls back to the
    fully-transparent baseline rather than surfacing an error to the
    narrative generator that then has nothing to cite.
    """
    if RISK_MODEL == "off":
        return None
    if RISK_MODEL == "xgboost":
        try:
            from app.scoring import model

            return {**model.explain_score(features), "score_version": model.SCORE_VERSION}
        except Exception:
            logger.warning("RISK_MODEL=xgboost failed to explain; falling back to baseline", exc_info=True)
    return {**baseline.explain_score(features), "score_version": baseline.SCORE_VERSION}
