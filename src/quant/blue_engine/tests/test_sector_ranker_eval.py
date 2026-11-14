"""HM vs cross-sectional ranker: the comparison must be honest and safe.

These tests do not assert that the ranker wins. They assert that the comparison
CANNOT lie: it must use the same splits for both sides, exclude unlabelled rows,
and default to keeping HM when the evidence is not there.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.ml.evaluation import sector_ranker_eval as ev  # noqa: E402


def _doc(n_dates=120, n_sectors=8):
    """Synthetic labelled cross-sections: ranker-visible structure baked in."""
    rows = []
    for d in range(n_dates):
        as_of = f"2025-{1 + d // 28:02d}-{1 + d % 28:02d}"
        for s in range(n_sectors):
            # hm_score drives the label, so a model has something to learn.
            hm = 90.0 - s * 10.0
            good = 1 if hm > 60 else 0
            rows.append({
                "as_of": as_of, "sector": f"S{s}", "hm_score": hm,
                "rsi": 100.0 - hm, "ema_rsi": 50.0, "rising": 1.0,
                "below_level": 1.0 if hm > 55 else 0.0,
                "ret_5": 0.01 * good, "ret_20": 0.02 * good,
                "ret_60": 0.03 * good, "vol_20": 0.01,
                "dist_ema20": 0.0,
                "fwd_5d": 0.01 if good else -0.01,
                "fwd_20d": 0.02 if good else -0.02,
                "label_5d": good, "label_20d": good,
            })
    return {"rows": rows, "provenance": {}}


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #
def test_unlabelled_rows_are_excluded():
    """The middle tercile and the tail must never be fitted or traded."""
    doc = _doc(n_dates=3, n_sectors=3)
    doc["rows"].append({**doc["rows"][0], "label_5d": None})
    rows, X, y, fwd = ev._rows_for_horizon(doc, 5)
    assert len(rows) == len(X) == len(y) == len(fwd)
    assert all(v is not None for v in y)


def test_rows_and_features_stay_aligned():
    doc = _doc(n_dates=4, n_sectors=5)
    rows, X, y, fwd = ev._rows_for_horizon(doc, 5)
    assert len(X[0]) == len(ev.FEATURES)
    for r, x in zip(rows, X):
        assert x[ev.FEATURES.index("hm_score")] == r["hm_score"]


def test_baseline_sees_only_the_hm_score():
    rows, X, _, _ = ev._rows_for_horizon(_doc(), 5)
    sel = ev._select(X, ev.BASELINE_FEATURES)
    assert len(sel[0]) == 1
    assert sel[0][0] == rows[0]["hm_score"]


def test_select_preserves_column_order():
    rows, X, _, _ = ev._rows_for_horizon(_doc(n_dates=2, n_sectors=4), 5)
    sel = ev._select(X, ["ret_5", "hm_score"])
    assert sel[0] == [rows[0]["ret_5"], rows[0]["hm_score"]]


# --------------------------------------------------------------------------- #
# Decision logic: conservative by default
# --------------------------------------------------------------------------- #
def test_decision_defaults_to_hm_when_sharpes_are_equal():
    d = ev._decide({"sharpe_mean": 1.0}, {"sharpe_mean": 1.0, "sharpe_std": 0.2})
    assert d["winner"] == "hm"
    assert d["keep"] == "HM"


def test_decision_requires_beating_baseline_by_more_than_dispersion():
    """A ranker inside its own fold noise has not won."""
    d = ev._decide({"sharpe_mean": 1.0}, {"sharpe_mean": 1.1, "sharpe_std": 0.5})
    assert d["winner"] == "hm"
    assert d["required"] == pytest.approx(1.5)


def test_decision_credits_a_clear_win():
    d = ev._decide({"sharpe_mean": 0.5}, {"sharpe_mean": 2.0, "sharpe_std": 0.1})
    assert d["winner"] == "ranker"
    assert "paper trading" in d["keep"]


def test_decision_is_undecided_without_sharpe():
    d = ev._decide({"sharpe_mean": None}, {"sharpe_mean": 1.0})
    assert d["winner"] == "undecided"
    assert d["keep"].startswith("HM")


def test_a_negative_baseline_still_needs_a_real_win():
    d = ev._decide({"sharpe_mean": -0.5},
                   {"sharpe_mean": -0.4, "sharpe_std": 0.3})
    assert d["winner"] == "hm"


# --------------------------------------------------------------------------- #
# Guard rails on the runner
# --------------------------------------------------------------------------- #
def test_too_few_rows_reports_a_reason_instead_of_crashing():
    out = ev.evaluate(_doc(n_dates=2, n_sectors=3), 5, min_train=50_000)
    assert out["ok"] is False
    assert "labelled rows" in out["reason"]


def test_compare_returns_both_sides_and_a_decision():
    res = ev.compare(_doc(n_dates=200, n_sectors=8), horizon=5,
                     n_splits=3, min_train=200)
    assert "baseline" in res and "ranker" in res
    assert res["decision"]["winner"] in {"hm", "ranker", "undecided"}
    assert res["horizon"] == 5


# --------------------------------------------------------------------------- #
# The cost gate bug (shared module)
# --------------------------------------------------------------------------- #
def test_cost_check_uses_a_period_mean_not_a_second_division():
    """Regression: the gate divided by n twice, so EVERY strategy failed.

    `mu` is already per-period. Dividing again shrank the gross edge by ~4
    orders of magnitude and rejected runs with a Sharpe above 4.
    """
    from blue_engine.ml.evaluation.metrics import cost_adjusted_edge
    src = Path(
        __import__("blue_engine.ml.evaluation.walk_forward",
                   fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert "gross_edge_bps = mu * 10_000.0" in src, (
        "the cost gate must express the per-period mean directly in bps")
    assert "(mu / len(all_rets))" not in src, (
        "double division is the bug that made every run look cost-negative")

    # Behaviour: 45 bps gross - 10 bps cost = 35 bps net.
    ok = cost_adjusted_edge(45.0, cost_bps_per_turnover=10.0)
    assert ok["net_bps"] == pytest.approx(35.0)
    assert ok["survives"] is True
    # And the gate must still be able to reject a thin edge.
    thin = cost_adjusted_edge(5.0, cost_bps_per_turnover=10.0)
    assert thin["net_bps"] == pytest.approx(-5.0)
    assert thin["survives"] is False
