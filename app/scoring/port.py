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
