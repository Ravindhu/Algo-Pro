"""Phase 2: factors, regime, signal, events (advice only), calibration, meta-labeler."""
import numpy as np
import pandas as pd

from quant.models.calibration import platt_fit, predict_proba, reliability
from quant.models.events import EventAdvice, score_filing
from quant.models.factors import composite, momentum_12_1
from quant.models.meta_label import MetaLabeler
from quant.models.regime import regime_states
from quant.models.signal import make_signal


def _prices(n=400, k=5, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, (n, k)), axis=0)),
                        index=idx, columns=[f"S{i}" for i in range(k)])


def test_momentum_uses_only_past_prices():
    close = _prices()
    base = momentum_12_1(close)
    changed = close.copy()
    changed.iloc[300:] *= 3.0  # change the future
    assert np.allclose(base.iloc[:300].to_numpy(), momentum_12_1(changed).iloc[:300].to_numpy(), equal_nan=True)


def test_composite_is_cross_sectionally_centred():
    c = composite(_prices())
    assert abs(c.iloc[-1].mean()) < 1e-9


def test_regime_warmup_is_normal_and_labels_valid():
    close = _prices(300, 1)["S0"]
    st = regime_states(close)
    assert len(st) == len(close)
    valid = st.dropna()
    assert set(valid.unique()) <= {"low_vol", "normal", "high_vol"}


def test_signal_threshold_and_stop():
    s = make_signal("A", score=1.5, daily_vol=0.01)
    assert s.direction == 1 and s.stop_pct == 0.02
    assert make_signal("A", score=0.2, daily_vol=0.01).direction == 0


def test_event_scoring_is_advice_only():
    adv = score_filing("A", "Company bags a major order win and announces buyback")
    assert isinstance(adv, EventAdvice)
    assert adv.score > 0 and adv.sets_size is False
    bad = score_filing("A", "SEBI order issued; fraud alleged")
    assert bad.score < 0 and -1.0 <= bad.score <= 1.0


def test_platt_scaling_separates_classes():
    rng = np.random.default_rng(4)
    x = rng.normal(0, 1, 600)
    y = (x + rng.normal(0, 0.5, 600) > 0).astype(float)
    w, b = platt_fit(x, y)
    p = predict_proba((x - x.mean()) / x.std(), w, b)
    assert ((p > 0.5) == (y == 1)).mean() > 0.8
    rows = reliability(p, y, bins=5)
    assert sum(r[2] for r in rows) == len(y)


def test_meta_labeler_skips_low_confidence_and_caps_bet():
    rng = np.random.default_rng(5)
    X = rng.normal(0, 1, (400, 2))
    y = (X[:, 0] > 0).astype(float)
    ml = MetaLabeler.fit(X, y, take_threshold=0.6, max_bet=0.05)
    take_hi, bet_hi = ml.decide([3.0, 0.0])
    take_lo, bet_lo = ml.decide([-3.0, 0.0])
    assert take_hi and bet_hi <= 0.05
    assert not take_lo and bet_lo == 0.0
