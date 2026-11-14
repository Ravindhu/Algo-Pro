"""Tail risk wrapper: VaR/CVaR summary, stress scenarios, and fail-closed limits.

The point of these tests is the *direction* of failure. A tail-risk number that
cannot be computed must read as unsafe, never as zero risk.
"""
import pytest

from quant.portfolio import tail_risk as tr


#: A small returns series in FRACTIONS, with a fat left tail.
RETURNS = [-0.04, -0.02, -0.01, 0.0, 0.005, 0.01, 0.02, 0.015, -0.005, 0.003] * 6


def test_default_stress_scenarios_are_india_relevant():
    """The five named scenarios come from the engine, not a local copy."""
    assert len(tr.DEFAULT_STRESS) >= 5
    for name in ("GFC_2008", "TAPER_2013", "DEMONET_2016", "COVID_2020"):
        assert name in tr.DEFAULT_STRESS


def test_summarise_reports_var_and_cvar_at_both_levels():
    out = tr.summarise(RETURNS)
    for key in ("var_hist_95", "cvar_95", "var_hist_99", "cvar_99", "max_drawdown"):
        assert key in out
    assert out["n"] == len(RETURNS)


def test_cvar_is_at_least_as_severe_as_var():
    """CVaR is the mean of the tail, so its magnitude must not be smaller than VaR."""
    out = tr.summarise(RETURNS)
    assert abs(out["cvar_99"]) >= abs(out["var_hist_99"])


def test_empty_series_gives_no_metrics_never_zero():
    out = tr.summarise([])
    assert out["n"] == 0
    assert "cvar_99" not in out  # absent, not 0.0: zero would read as "no risk"


def test_percentage_scale_is_refused():
    """A percent series would understate CVaR by 100x. Refuse it loudly."""
    with pytest.raises(ValueError, match="fractions"):
        tr.summarise([-1.5, 2.0, -0.8, 1.2] * 10)


def test_fraction_scale_is_accepted():
    assert tr.summarise(RETURNS)["n"] == len(RETURNS)


def test_stress_is_sorted_worst_first():
    out = tr.stress(1.0)
    pnls = [r["pnl_pct"] for r in out]
    assert pnls == sorted(pnls)
    # GFC is the deepest shock, so it must lead the list.
    assert out[0]["scenario"] == "GFC_2008"


def test_stress_without_beta_assumes_one():
    """An unknown beta must not produce an empty result; the engine defaults to 1.0."""
    assert len(tr.stress(None)) == len(tr.DEFAULT_STRESS)


# --------------------------------------------------------------------------- #
# fail-closed limits
# --------------------------------------------------------------------------- #
def test_unset_limits_fail_closed():
    out = tr.check_limits(RETURNS)
    assert out["breached"] is True
    assert any("not set" in r for r in out["reasons"])


def test_no_returns_fails_closed():
    out = tr.check_limits([], max_cvar=0.05, max_drawdown=0.20)
    assert out["breached"] is True
    assert out["measurable"] is False


def test_generous_limits_pass():
    out = tr.check_limits(RETURNS, max_cvar=0.50, max_drawdown=0.90)
    assert out["breached"] is False, out["reasons"]
    assert out["measurable"] is True


def test_tight_limit_breaches():
    out = tr.check_limits(RETURNS, max_cvar=0.0001, max_drawdown=0.90)
    assert out["breached"] is True
    assert any("cvar" in r for r in out["reasons"])


def test_drawdown_limit_breaches():
    out = tr.check_limits(RETURNS, max_cvar=0.50, max_drawdown=0.0001)
    assert out["breached"] is True
    assert any("drawdown" in r for r in out["reasons"])


def test_panel_shape():
    p = tr.panel(RETURNS, beta=1.1)
    assert set(p) == {"summary", "stress", "scenarios_available"}
    assert p["scenarios_available"] == len(tr.DEFAULT_STRESS)
    assert p["summary"]["n"] == len(RETURNS)
