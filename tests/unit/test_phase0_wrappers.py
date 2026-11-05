"""Phase 0: src/quant wrappers over Blue_engine.

Each wrapper must (a) delegate to Blue_engine, and (b) keep the numbers it produced
before the wrap. Golden values below were taken from the pre-wrap implementation
(git history, commit 579c653, src/quant/research/costs.py).

These tests need the OpenAlgo SDK, because importing Blue_engine's cost module loads
blue_engine.core. Without the SDK they skip with the reason. Production requires the
SDK (requirements.txt pins openalgo==2.0.3).
"""
import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable


def _blue_or_skip(name: str):
    try:
        return blue_bridge.load(name)
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")


def test_bridge_loads_blue_cost_model():
    module = _blue_or_skip("backtest.costs")
    assert hasattr(module, "CostModel")


def test_bridge_reports_missing_module_clearly():
    _blue_or_skip("backtest.costs")  # ensures the package is loadable first
    with pytest.raises(BlueEngineUnavailable):
        blue_bridge.load("does_not_exist_module")


def _expected_inr(notional, side, product):
    """Independent arithmetic for the owner-confirmed rules (oracle for the wrapper).

    Delivery: no brokerage; STT on both legs; stamp on buy.
    Intraday: brokerage min(0.03%, Rs 20); STT on sell; stamp (small) on buy.
    All product lines: exchange txn, SEBI, 18% GST on brokerage+txn+SEBI, 5 bps impact.
    """
    intraday = product == "intraday"
    buy = side != "SELL"
    brokerage = min(0.0003 * notional, 20.0) if intraday else 0.0
    if intraday:
        stt = 0.0 if buy else 0.00025 * notional
        stamp = 0.00003 * notional if buy else 0.0
    else:
        stt = 0.001 * notional
        stamp = 0.00015 * notional if buy else 0.0
    txn = 0.0000307 * notional
    sebi = 0.000001 * notional
    gst = 0.18 * (brokerage + txn + sebi)
    impact = 0.0005 * notional
    return brokerage + stt + stamp + txn + sebi + gst + impact


@pytest.mark.parametrize("notional", [0.0, 1_000.0, 100_000.0, 10_000_000.0, 1e9])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("product", ["delivery", "intraday"])
def test_cost_matches_owner_rules(notional, side, product):
    _blue_or_skip("backtest.costs")
    from quant.research.costs import CostModel

    got = CostModel().trade_cost(notional, side, product)
    assert got == pytest.approx(_expected_inr(notional, side, product), rel=1e-9, abs=1e-6)


def test_delivery_has_no_brokerage_and_intraday_is_capped():
    _blue_or_skip("backtest.costs")
    from quant.research.costs import CostModel

    m = CostModel()
    # Delivery brokerage is zero: only STT, txn, SEBI, GST, stamp and impact remain.
    assert m.trade_cost(1e9, "SELL", "delivery") == pytest.approx(_expected_inr(1e9, "SELL", "delivery"))
    # Intraday brokerage is 0.03% for a small order (no cap yet) and stops at Rs 20 for large ones.
    assert m.trade_cost(1_000.0, "SELL", "intraday") == pytest.approx(_expected_inr(1_000.0, "SELL", "intraday"))
    big_one = _expected_inr(1e9, "SELL", "intraday")
    assert m.trade_cost(1e9, "SELL", "intraday") == pytest.approx(big_one)


def test_negative_notional_still_rejected():
    from quant.research.costs import CostModel

    with pytest.raises(ValueError):
        CostModel().trade_cost(-1.0, "BUY")


def test_walk_forward_keeps_purge_gap_and_order():
    """Each train block stops `purge` rows before its test block; test is after train."""
    from quant.research.walk_forward import purged_walk_forward as wf

    folds = list(wf(103, n_splits=3, purge=4, min_train=5))
    assert folds, "expected at least one fold"
    for train, test in folds:
        assert train.size >= 5
        assert train.max() < test.min() - 4          # purge gap respected
        assert test.min() > train.max()              # test strictly after train


# Golden Sharpe values from the pre-wrap implementation (annualised, periods=252).
def test_sharpe_numbers_unchanged_after_wrap():
    _blue_or_skip("ml.evaluation.metrics")
    from quant.research.validation import sharpe

    assert sharpe([0.01, -0.005, 0.02, 0.0, 0.007]) == pytest.approx(10.574976083972865, rel=1e-12)


def test_sharpe_matches_blue_result():
    """Stage 1 of docs/DECOUPLING_PLAN.md: validation.sharpe uses the local copy; it must still match Blue."""
    metrics = _blue_or_skip("ml.evaluation.metrics")
    from quant.research import validation

    rets = [0.01, -0.005, 0.02]
    assert validation.sharpe(rets, periods=252) == pytest.approx(
        metrics.sharpe(rets, periods_per_year=252.0))


def test_sharpe_constant_series_is_zero_not_noise():
    """Old code returned ~6.9e16 here (float noise in the std). The wrapped version returns 0.0."""
    _blue_or_skip("ml.evaluation.metrics")
    from quant.research.validation import sharpe

    assert sharpe([0.001] * 10) == 0.0
