"""MAE/MFE are optimizer parameters (decision 0008). Numbers checked by hand.

Flow stage: Dashboard -> optimizer (research). Status: TEST.
Skipped when Blue's optimizer cannot load (for example openalgo missing).
"""
import pytest

from quant.core.blue_bridge import BlueEngineUnavailable, load

try:
    OBJ = load("optimizer.objective")
    ENG = load("optimizer.engine")
except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
    pytest.skip(f"Blue optimizer unavailable: {exc}", allow_module_level=True)


def _noop(*_a, **_k):
    return None


def test_no_trade_data_adds_nothing():
    assert OBJ.excursion_adjustment(None, None, 0.02, 0.1) == 0.0


def test_hand_example_mae_and_capture():
    # MAE 2%: -0.02 * 2 = -0.04. Capture 0.5: +0.1 * 0.5 = +0.05. Total +0.01.
    assert OBJ.excursion_adjustment(0.02, 0.5, 0.02, 0.1) == pytest.approx(0.01)


def test_missing_capture_counts_as_zero():
    # MAE 1%, penalty 0.05: -0.05. No capture data, so the bonus is 0.
    assert OBJ.excursion_adjustment(0.01, None, 0.05, 0.1) == pytest.approx(-0.05)


def test_zero_weights_switch_both_terms_off():
    assert OBJ.excursion_adjustment(0.03, 0.9, 0.0, 0.0) == 0.0


def test_objective_stores_and_reports_weights():
    o = OBJ.Objective(_noop, None, mae_penalty_per_pct=0.03, mfe_capture_weight=0.2)
    assert o.mae_penalty_per_pct == 0.03
    assert o.mfe_capture_weight == 0.2
    s = o.stats()
    assert s["mae_penalty_per_pct"] == 0.03
    assert s["mfe_capture_weight"] == 0.2


def test_objective_defaults_match_module_constants():
    o = OBJ.Objective(_noop, None)
    assert o.mae_penalty_per_pct == OBJ.MAE_PENALTY_PER_PCT
    assert o.mfe_capture_weight == OBJ.MFE_CAPTURE_WEIGHT


@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf")])
def test_negative_or_non_finite_weight_refused(bad):
    with pytest.raises(ValueError):
        OBJ.Objective(_noop, None, mae_penalty_per_pct=bad)
    with pytest.raises(ValueError):
        OBJ.Objective(_noop, None, mfe_capture_weight=bad)


def test_engine_config_carries_weights_into_to_dict():
    d = ENG.EngineConfig(mae_penalty_per_pct=0.03, mfe_capture_weight=0.2).to_dict()
    assert d["mae_penalty_per_pct"] == 0.03
    assert d["mfe_capture_weight"] == 0.2
    assert d["trial_log_path"] is None  # trial logging is off unless a path is set


def test_engine_config_refuses_negative_weight():
    with pytest.raises(ValueError):
        ENG.EngineConfig(mfe_capture_weight=-1.0)
