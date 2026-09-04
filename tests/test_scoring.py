import inspect

from app.policy import decide
from app.scoring import baseline, port


def test_decide_signature_has_no_score_parameter():
    """The score prioritises; it never decides. policy.decide() must stay
    structurally incapable of taking a score as input -- retrying an
    EXPIRED_CARD is wrong at predicted_score=0.9 exactly as at 0.1."""
    params = set(inspect.signature(decide).parameters)
    assert not (params & {"score", "predicted_score", "risk_score"})


def test_baseline_score_bounded():
    features = {"customer_type": "B2C", "segment": "regular", "prior_recovery_rate": 0.5}
    assert 0.0 <= baseline.score(features) <= 1.0


def test_baseline_score_rewards_loyalty_and_history():
    weak = {"customer_type": "B2C", "segment": "new", "prior_recovery_rate": 0.1, "n_prior_transactions": 0, "tenure_days": 0}
    strong = {"customer_type": "B2C", "segment": "loyal", "prior_recovery_rate": 0.9, "n_prior_transactions": 10, "tenure_days": 500}
    assert baseline.score(strong) > baseline.score(weak)


def test_baseline_score_penalizes_b2b_overdue_and_late_history():
    on_time = {"customer_type": "B2B", "segment": "mid", "days_overdue": 2, "prior_late_rate": 0.0, "crosses_msmed_45d": False}
    chronic_late = {"customer_type": "B2B", "segment": "sme", "days_overdue": 60, "prior_late_rate": 0.9, "crosses_msmed_45d": True}
    assert baseline.score(on_time) > baseline.score(chronic_late)


def test_port_default_is_baseline(monkeypatch):
    """With RISK_MODEL unset, the port must fall back to baseline.

    Asserted against a reimport with the variable cleared, not against
    whatever the ambient environment happens to say: reading os.environ
    directly made this test pass or fail based on the developer's own
    .env, which is a property of the machine, not of the code.
    """
    import importlib

    monkeypatch.delenv("RISK_MODEL", raising=False)
    reloaded = importlib.reload(port)
    try:
        assert reloaded.RISK_MODEL == "baseline"
        _score, version = reloaded.score({"customer_type": "B2C", "segment": "regular"})
        assert version == baseline.SCORE_VERSION
    finally:
        importlib.reload(port)  # restore the module to the ambient config


def test_port_off_returns_neutral_score_without_touching_baseline():
    import app.scoring.port as port_module

    original = port_module.RISK_MODEL
    try:
        port_module.RISK_MODEL = "off"
        score, version = port_module.score({})
        assert score == 0.5
        assert version == "off"
    finally:
        port_module.RISK_MODEL = original
