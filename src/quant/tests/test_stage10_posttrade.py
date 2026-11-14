"""Stage 10 (Post-trade): strategy health, excursions, three-way recon.

Two defects here were silent, which is the worst kind in post-trade:
- a single NaN return switched OFF the drift alarm permanently, so a degrading
  strategy would keep trading;
- `excursions(entry=0)` returned `inf`, which then dominates any average it enters.

And the documented "3-way match" was only two-way: the OpenAlgo leg was never
compared.
"""
import math

import pytest

from quant.monitor.live_monitor import excursions, max_drawdown
from quant.monitor.recon3 import reconcile, reconcile_report
from quant.monitor.strategy_health import (cusum_alarm, cusum_detail,
                                           emit_deviation_alert, rolling_sharpe)


# --------------------------------------------------------------------------- #
# CUSUM: the NaN defect
# --------------------------------------------------------------------------- #
def test_alarm_fires_on_a_degrading_series():
    assert cusum_alarm([-1.0] * 10, 0.0, 0.5, 2.0) is True


def test_alarm_does_not_fire_on_a_healthy_series():
    assert cusum_alarm([0.5] * 10, 0.0, 0.5, 2.0) is False


def test_a_nan_no_longer_switches_the_alarm_off():
    """The regression: min(0.0, s+nan) is 0.0, so the sum RESET and never fired."""
    with_nan = [-1.0, -1.0, float("nan"), -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0]
    assert cusum_alarm(with_nan, 0.0, 0.5, 2.0) is True


def test_detail_counts_only_usable_samples():
    series = [float("nan"), -1.0, -1.0, -1.0]
    detail = cusum_detail(series, 0.0, 0.5, 2.0)
    assert detail["n_used"] == 3


def test_zero_threshold_is_refused():
    """h=0 trips on any negative deviation: a config error, not an alarm."""
    with pytest.raises(ValueError, match="h must be a positive"):
        cusum_alarm([0.1], 0.0, 0.5, 0.0)


def test_negative_threshold_is_refused():
    with pytest.raises(ValueError, match="h must be a positive"):
        cusum_alarm([0.1], 0.0, 0.5, -1.0)


def test_empty_series_is_no_alarm():
    assert cusum_alarm([], 0.0, 0.5, 5.0) is False


def test_alert_carries_the_statistic():
    class _Alerts:
        def __init__(self):
            self.rows = []

        def emit(self, level, component, message):
            self.rows.append((level, component, message))

    alerts = _Alerts()
    fired = emit_deviation_alert(alerts, "strat", [-1.0] * 10, 0.0, 0.5, 2.0)
    assert fired is True
    assert alerts.rows and alerts.rows[0][0] == "ERROR"
    assert "statistic" in alerts.rows[0][2]


def test_no_alert_when_healthy():
    class _Alerts:
        def __init__(self):
            self.rows = []

        def emit(self, level, component, message):
            self.rows.append((level, component, message))

    alerts = _Alerts()
    assert emit_deviation_alert(alerts, "strat", [0.5] * 10, 0.0, 0.5, 2.0) is False
    assert alerts.rows == []


# --------------------------------------------------------------------------- #
# rolling Sharpe
# --------------------------------------------------------------------------- #
def test_sharpe_is_nan_before_a_full_window():
    out = rolling_sharpe([0.01] * 70, window=60)
    assert math.isnan(out[0])


def test_sharpe_handles_a_zero_variance_window():
    out = rolling_sharpe([0.01] * 70, window=60)
    assert math.isnan(out[-1])      # constant series has zero std


def test_sharpe_window_too_small_is_refused():
    with pytest.raises(ValueError, match="window"):
        rolling_sharpe([0.01] * 10, window=1)


def test_sharpe_survives_a_nan():
    series = [0.01] * 65 + [float("nan")] + [0.01] * 5
    out = rolling_sharpe(series, window=60)
    assert len(out) == len(series)


# --------------------------------------------------------------------------- #
# excursions: the inf defect
# --------------------------------------------------------------------------- #
def test_long_excursions_are_positive_fractions():
    mae, mfe = excursions(100.0, [90.0, 110.0], 1)
    assert mae == pytest.approx(0.10)
    assert mfe == pytest.approx(0.10)


def test_short_excursions_invert_the_direction():
    mae, mfe = excursions(100.0, [90.0, 110.0], -1)
    assert mae == pytest.approx(0.10)   # a short is hurt by the rise
    assert mfe == pytest.approx(0.10)


def test_zero_entry_is_refused():
    """It used to return inf, which then dominates any average it enters."""
    with pytest.raises(ValueError, match="positive"):
        excursions(0.0, [90.0], 1)


def test_empty_prices_is_refused_with_a_reason():
    with pytest.raises(ValueError, match="empty"):
        excursions(100.0, [], 1)


def test_side_zero_is_refused():
    """It used to return (0,0): a flat trade, which is a lie."""
    with pytest.raises(ValueError, match="side"):
        excursions(100.0, [90.0], 0)


def test_non_finite_prices_are_dropped():
    mae, mfe = excursions(100.0, [float("nan"), 90.0], 1)
    assert mae == pytest.approx(0.10)


def test_max_drawdown_of_an_empty_curve_is_zero():
    assert max_drawdown([]) == 0.0


# --------------------------------------------------------------------------- #
# three-way reconciliation
# --------------------------------------------------------------------------- #
def test_all_three_agree():
    assert reconcile({"A": 10}, {"A": 10}, {"A": 10})["verdict"] == "match"


def test_stale_openalgo_is_named():
    """The leg that two-way recon cannot see."""
    out = reconcile({"A": 10}, {"A": 5}, {"A": 10})
    assert out["symbols"]["A"]["cause"] == "openalgo_stale"


def test_we_hold_something_nobody_else_does():
    out = reconcile({"A": 10}, {}, {})
    assert out["symbols"]["A"]["cause"] == "we_only"


def test_we_missed_a_fill_the_others_have():
    out = reconcile({}, {"A": 10}, {"A": 10})
    assert out["symbols"]["A"]["cause"] == "ours_missing"


def test_partial_split_is_named():
    out = reconcile({"A": 10}, {"A": 5}, {"A": 8})
    assert out["symbols"]["A"]["cause"] == "partial_split"


def test_a_symbol_only_the_broker_holds_is_reported():
    """The most dangerous case: silence is not agreement."""
    out = reconcile({"B": 1}, {"B": 1}, {"B": 1, "ZZZ": 50})
    assert "ZZZ" in out["symbols"]
    assert out["symbols"]["ZZZ"]["broker"] == 50


def test_a_missing_leg_blocks_the_whole_comparison():
    """An entry must never be approved on a comparison that did not happen."""
    out = reconcile({"A": 10}, None, {"A": 10})
    assert out["verdict"] == "blocked"
    assert out["legs"]["openalgo"] == "unavailable"


def test_zero_positions_are_dropped():
    assert reconcile({"A": 0}, {}, {})["verdict"] == "match"


def test_report_is_audited_and_alerted():
    class _Audit:
        def __init__(self):
            self.rows = []

        def append(self, kind, payload):
            self.rows.append((kind, payload))

    class _Alerts:
        def __init__(self):
            self.rows = []

        def emit(self, level, component, message):
            self.rows.append((level, component, message))

    audit, alerts = _Audit(), _Alerts()
    result = reconcile({"A": 10}, {"A": 5}, {"A": 10})
    reconcile_report(result, audit=audit, alerts=alerts)
    assert audit.rows and audit.rows[0][0] == "recon3"
    assert alerts.rows and alerts.rows[0][0] == "ERROR"


def test_blocked_raises_a_warning_not_an_error():
    class _Alerts:
        def __init__(self):
            self.rows = []

        def emit(self, level, component, message):
            self.rows.append((level, component, message))

    alerts = _Alerts()
    reconcile_report(reconcile({"A": 10}, None, {"A": 10}), alerts=alerts)
    assert alerts.rows[0][0] == "WARNING"


def test_match_raises_no_alert():
    class _Alerts:
        def __init__(self):
            self.rows = []

        def emit(self, level, component, message):
            self.rows.append((level, component, message))

    alerts = _Alerts()
    reconcile_report(reconcile({"A": 1}, {"A": 1}, {"A": 1}), alerts=alerts)
    assert alerts.rows == []
