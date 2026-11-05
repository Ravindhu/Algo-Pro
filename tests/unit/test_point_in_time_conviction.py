"""X-4: conviction inputs read only past values (no look-ahead).

Flow stage: Feature store, Calibrated conviction
Status: TEST
"""
import inspect
import random

import pandas as pd

from quant.data.feature_store import FeatureStore
from quant.models.conviction import CONDITIONS, score_conviction

T0 = pd.Timestamp("2026-03-02 09:15")
T1 = pd.Timestamp("2026-03-03 09:15")
T2 = pd.Timestamp("2026-03-04 09:15")


def test_later_write_does_not_change_earlier_answer():
    fs = FeatureStore()
    fs.put("mom", "A", T0, 0.4)
    before = fs.get_asof("mom", "A", T0 + pd.Timedelta(hours=1))
    fs.put("mom", "A", T2, 0.99)
    assert fs.get_asof("mom", "A", T0 + pd.Timedelta(hours=1)) == before == 0.4


def test_release_time_is_inclusive_and_not_earlier():
    fs = FeatureStore()
    release = pd.Timestamp("2026-03-03 18:40")  # e.g. a results release after the close
    fs.put("catalyst", "A", release, 0.8)
    assert fs.get_asof("catalyst", "A", pd.Timestamp("2026-03-03 15:30")) is None
    assert fs.get_asof("catalyst", "A", release) == 0.8


def test_out_of_order_writes_match_in_order_writes():
    rows = [(T0, 0.1), (T1, 0.5), (T2, 0.9)]
    ordered, shuffled = FeatureStore(), FeatureStore()
    for ts, v in rows:
        ordered.put("liq", "A", ts, v)
    random.Random(7).shuffle(rows)
    for ts, v in rows:
        shuffled.put("liq", "A", ts, v)
    for probe in [T0 - pd.Timedelta(days=1), T0, T1, T1 + pd.Timedelta(hours=3), T2, T2 + pd.Timedelta(days=5)]:
        assert shuffled.get_asof("liq", "A", probe) == ordered.get_asof("liq", "A", probe)


def test_store_has_no_forward_lookup():
    params = list(inspect.signature(FeatureStore.get_asof).parameters)
    assert params == ["self", "feature", "symbol", "ts"]
    assert not hasattr(FeatureStore, "get_forward")
    assert not hasattr(FeatureStore, "next_value")


def test_conviction_from_store_is_unchanged_by_future_data():
    fs = FeatureStore()
    past = {"momentum": 0.7, "trend_structure": 0.8, "catalyst": 0.6,
            "liquidity": 0.9, "execution": 0.75, "strategy_history": 0.5}
    for name, v in past.items():
        fs.put(name, "A", T0, v)

    def conviction_at(t):
        conds = {name: fs.get_asof(name, "A", t) for name in CONDITIONS}
        return score_conviction(conds).score

    t = T0 + pd.Timedelta(hours=2)
    score_before = conviction_at(t)
    assert score_before > 0.0
    for name in CONDITIONS:
        fs.put(name, "A", T2, 1.0)  # future data, should be invisible at t
    assert conviction_at(t) == score_before


def test_no_past_data_means_zero_conviction():
    fs = FeatureStore()
    conds = {name: fs.get_asof(name, "A", T0) for name in CONDITIONS}
    assert score_conviction(conds).score == 0.0
