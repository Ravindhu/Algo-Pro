"""Stage 3 (Data and features): corp actions, feature matrix, leak wiring.

Three things were wrong in this stage audit:
- corp_actions.py produced ``inf`` prices on a zero factor, crashed on dividends,
  and claimed a wrap it never performed;
- feature_store_pit and leakage_gate were built and called by NOTHING;
- no single place assembled a feature matrix, so the look-ahead rules were a
  checklist nobody ran.
"""
import numpy as np
import pandas as pd
import pytest

from quant.data.corp_actions import back_adjust, validate_actions
from quant.models.feature_matrix import build_feature_matrix, price_features


# --------------------------------------------------------------------------- #
# corp actions - the three fixed defects
# --------------------------------------------------------------------------- #
def _bars(dates, close, symbol="X"):
    return pd.DataFrame({
        "symbol": [symbol] * len(dates),
        "date": pd.to_datetime(dates),
        "open": close, "high": close, "low": close, "close": close,
        "volume": [1000] * len(dates),
    })


def test_split_adjusts_bars_before_the_ex_date_only():
    out = back_adjust(_bars(["2026-01-01", "2026-01-05"], [200.0, 100.0]),
                      pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                    "factor": [2.0]}))
    assert out["close"].tolist() == [100.0, 100.0]


def test_volume_moves_the_opposite_way():
    out = back_adjust(_bars(["2026-01-01", "2026-01-05"], [200.0, 100.0]),
                      pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                    "factor": [2.0]}))
    assert out["volume"].tolist() == [2000, 1000]


def test_zero_factor_is_refused_not_inf():
    """A zero factor used to divide prices into inf, which silently poisons everything."""
    with pytest.raises(ValueError, match="factor must be > 0"):
        back_adjust(_bars(["2026-01-01", "2026-01-05"], [200.0, 100.0]),
                    pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                  "factor": [0.0]}))


def test_negative_factor_is_refused():
    with pytest.raises(ValueError, match="factor must be > 0"):
        validate_actions(pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                       "factor": [-2.0]}))


def test_dividend_adjusts_by_ratio_not_division():
    """A 5 rupee dividend on a 100 ex-date price implies a 105 pre-price."""
    out = back_adjust(_bars(["2026-01-01", "2026-01-05"], [105.0, 100.0]),
                      pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                    "amount": [5.0]}))
    assert out["close"].iloc[0] == pytest.approx(100.0)


def test_dividend_no_longer_raises_keyerror():
    """Dividends were the most common action and the old code crashed on them."""
    out = back_adjust(_bars(["2026-01-01"], [105.0]),
                      pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                    "amount": [5.0]}))
    assert len(out) == 1


def test_two_splits_compound():
    bars = _bars(["2026-01-01", "2026-01-07", "2026-01-12"], [400.0, 200.0, 100.0])
    actions = pd.DataFrame({"symbol": ["X", "X"],
                            "date": ["2026-01-05", "2026-01-10"],
                            "factor": [2.0, 2.0]})
    assert back_adjust(bars, actions)["close"].tolist() == [100.0, 100.0, 100.0]


def test_missing_symbol_column_gets_a_clear_error():
    with pytest.raises(ValueError, match="symbol"):
        back_adjust(_bars(["2026-01-01"], [100.0]).drop(columns=["symbol"]),
                    pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                  "factor": [2.0]}))


def test_actions_with_neither_factor_nor_amount_are_refused():
    with pytest.raises(ValueError, match="factor.*amount"):
        validate_actions(pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"]}))


def test_empty_actions_is_a_no_op():
    bars = _bars(["2026-01-01"], [100.0])
    assert back_adjust(bars, pd.DataFrame(columns=["symbol", "date", "factor"])).equals(bars)


def test_factor_of_one_is_a_no_op():
    bars = _bars(["2026-01-01"], [100.0])
    out = back_adjust(bars, pd.DataFrame({"symbol": ["X"], "date": ["2026-01-05"],
                                          "factor": [1.0]}))
    assert out["close"].tolist() == [100.0]


# --------------------------------------------------------------------------- #
# feature matrix
# --------------------------------------------------------------------------- #
def _prices(n=400, seed=7, symbols=("A",)):
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {s: 100 * np.cumprod(1 + rng.normal(0, 0.01, n)) for s in symbols}, index=idx)


def test_price_features_are_never_filled():
    """A filled value invents data and the leak audit cannot see an invention."""
    feats = price_features(_prices(n=80))
    assert "mom_12_1" in feats
    # .isna().any() returns a Series (one entry per symbol), so collapse it first.
    assert bool(feats["mom_12_1"].isna().any().any())   # warm-up must stay NaN


def test_matrix_builds_and_passes_the_leak_gate():
    out = build_feature_matrix(_prices())
    assert out["verdict"] == "clean"
    assert out["features"]
    assert not out["matrix"].empty


def test_matrix_is_refused_when_the_gate_is_not_clean(monkeypatch):
    """Guard the enforcement path: a non-clean verdict must raise."""
    import quant.models.feature_matrix as fm

    def bad(*a, **k):
        return {"verdict": "leaked", "detail": "injected"}

    monkeypatch.setattr(fm, "audit_leakage", bad)
    with pytest.raises(ValueError, match="feature matrix refused"):
        build_feature_matrix(_prices())


def test_matrix_returns_the_audit_alongside_the_data():
    """A caller cannot get features without also being told whether they leak."""
    out = build_feature_matrix(_prices(), require_clean=False)
    assert "leakage" in out and "checks_run" in out["leakage"]


def test_empty_prices_gives_an_incomplete_verdict_not_a_crash():
    empty = pd.DataFrame(index=pd.DatetimeIndex([]), columns=["A"], dtype="float64")
    out = build_feature_matrix(empty, require_clean=False)
    assert out["verdict"] in ("incomplete", "failed", "leaked")


def test_matrix_multi_symbol():
    out = build_feature_matrix(_prices(symbols=("A", "B")))
    assert out["verdict"] == "clean"
    assert len(out["features"]) == 2
