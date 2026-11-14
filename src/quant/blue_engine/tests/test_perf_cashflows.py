"""Tests for cash-flow-aware returns: XIRR (money-weighted) and TWRR (time-weighted).

These are the two numbers a PM reports side by side:

* XIRR  - what the investor's money actually earned (weights cash-flow timing).
* TWRR  - what the manager earned, independent of cash-flow timing (the fair
          benchmark-relative number).

The tests pin both against analytically-known cases, not just "does it run".
"""
from datetime import date

import pytest

from blue_engine.portfolio.attribution import perf


# --------------------------------------------------------------------------- #
# XIRR - money-weighted return
# --------------------------------------------------------------------------- #

def test_xirr_exact_10pct_over_one_year():
    """Invest 1000, receive 1100 exactly 365 days later -> 10%."""
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    r = perf.xirr(flows)
    assert r is not None
    # 365 days / 365 == 1.0 year exactly -> closed form 0.10.
    assert r == pytest.approx(0.10, abs=1e-6)


def test_xirr_half_year_doubling_is_annualised():
    """1000 -> 1100 in ~182 days must annualise well above 10%."""
    flows = [(date(2025, 1, 1), -1000.0), (date(2025, 7, 2), 1100.0)]
    r = perf.xirr(flows)
    assert r is not None
    assert r > 0.20  # a ~10% half-year return is ~21% annualised


def test_xirr_npv_is_zero_at_solution():
    """The defining property: NPV at the returned rate is ~0."""
    flows = [
        (date(2025, 1, 1), -100000.0),
        (date(2025, 4, 1), -50000.0),
        (date(2025, 10, 1), 40000.0),
        (date(2026, 1, 1), 130000.0),
    ]
    r = perf.xirr(flows)
    assert r is not None
    t0 = date(2025, 1, 1)
    npv = sum(a / (1.0 + r) ** ((d - t0).days / 365.0) for d, a in flows)
    assert npv == pytest.approx(0.0, abs=1e-3)


def test_xirr_accepts_iso_strings_and_dicts():
    flows = [
        {"date": "2025-01-01", "amount": -1000.0},
        {"date": "2026-01-01", "amount": 1100.0},
    ]
    assert perf.xirr(flows) == pytest.approx(0.10, abs=1e-6)


def test_xirr_none_when_single_sided():
    """All flows the same sign -> no root -> None, not a crash."""
    assert perf.xirr([(date(2025, 1, 1), -1000.0),
                      (date(2026, 1, 1), -500.0)]) is None


def test_xirr_none_when_too_few_flows():
    assert perf.xirr([(date(2025, 1, 1), -1000.0)]) is None
    assert perf.xirr([]) is None


def test_xirr_ignores_bad_rows():
    """One unparseable date must not sink the whole calculation."""
    flows = [
        (date(2025, 1, 1), -1000.0),
        ("not-a-date", 500.0),
        (date(2026, 1, 1), 1100.0),
    ]
    assert perf.xirr(flows) == pytest.approx(0.10, abs=1e-6)


# --------------------------------------------------------------------------- #
# TWRR - time-weighted return
# --------------------------------------------------------------------------- #

def test_twrr_ignores_large_midperiod_deposit():
    """The classic TWRR test: a big deposit must NOT change the manager return.

    Start 100,000 -> 110,000 (+10%). Then a 1,000,000 deposit arrives, and the
    value ends at 1,221,000 - i.e. the 1.11M invested grew by 10% again.
    TWRR must report ~21% (1.10 * 1.10 - 1) regardless of the deposit size.
    """
    valuations = [
        {"date": "2025-01-01", "value": 100000.0, "flow": 0.0},
        {"date": "2025-06-01", "value": 110000.0, "flow": 0.0},
        # Deposit lands ON this date and is included in `value`.
        # flow is POSITIVE = money paid IN (contribution convention).
        {"date": "2025-06-02", "value": 1110000.0, "flow": 1000000.0},
        {"date": "2025-12-31", "value": 1221000.0, "flow": 0.0},
    ]
    r = perf.twrr(valuations)
    assert r is not None
    assert r == pytest.approx(0.21, abs=1e-9)


def test_twrr_flat_when_no_change():
    valuations = [
        {"date": "2025-01-01", "value": 100000.0},
        {"date": "2025-06-01", "value": 100000.0},
        {"date": "2025-12-31", "value": 100000.0},
    ]
    assert perf.twrr(valuations) == pytest.approx(0.0, abs=1e-12)


def test_twrr_chains_simple_returns():
    """+10% then -10% -> 1.10 * 0.90 - 1 = -1%."""
    valuations = [
        {"date": "2025-01-01", "value": 100.0},
        {"date": "2025-06-01", "value": 110.0},
        {"date": "2025-12-31", "value": 99.0},
    ]
    assert perf.twrr(valuations) == pytest.approx(-0.01, abs=1e-12)


def test_twrr_none_when_too_few_points():
    assert perf.twrr([{"date": "2025-01-01", "value": 100.0}]) is None
    assert perf.twrr([]) is None


def test_twrr_skips_nonpositive_prior_value():
    """A zero prior value cannot support a ratio; the leg is skipped, not NaN."""
    valuations = [
        {"date": "2025-01-01", "value": 0.0},
        {"date": "2025-06-01", "value": 100.0},
        {"date": "2025-12-31", "value": 110.0},
    ]
    r = perf.twrr(valuations)
    assert r == pytest.approx(0.10, abs=1e-12)  # only the second leg counts


# --------------------------------------------------------------------------- #
# summarise() wiring
# --------------------------------------------------------------------------- #

def test_summarise_includes_xirr_and_twrr_when_supplied():
    returns = [0.001, -0.002, 0.003, 0.0, 0.0015]
    cash_flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    valuations = [
        {"date": "2025-01-01", "value": 100.0},
        {"date": "2025-12-31", "value": 110.0},
    ]
    out = perf.summarise(returns, cash_flows=cash_flows, valuations=valuations)
    assert out["xirr"] == pytest.approx(0.10, abs=1e-6)
    assert out["twrr"] == pytest.approx(0.10, abs=1e-12)


def test_summarise_omits_them_by_default():
    out = perf.summarise([0.001, 0.002])
    assert "xirr" not in out
    assert "twrr" not in out


# --------------------------------------------------------------------------- #
# risk_report.report wiring: XIRR/TWRR surface from NAV rows
# --------------------------------------------------------------------------- #

def test_report_attaches_twrr_and_xirr_from_rows_with_flows():
    """A row carrying an external `flow` yields BOTH twrr and xirr."""
    from blue_engine.portfolio import risk_report as rr

    rows = [
        {"date": "2025-01-01", "nav": 100000.0, "flow": 100000.0},
        {"date": "2025-06-01", "nav": 110000.0},
        {"date": "2025-12-31", "nav": 121000.0},
    ]
    rep = rr.report(rows)
    s = rep["performance"]["summary"]
    assert "twrr" in s and s["twrr"] is not None
    assert "xirr" in s and s["xirr"] is not None


def test_report_attaches_twrr_but_omits_xirr_without_flows():
    """No external flows -> TWRR still computed; XIRR intentionally omitted."""
    from blue_engine.portfolio import risk_report as rr

    rows = [
        {"date": "2025-01-01", "nav": 100000.0},
        {"date": "2025-06-01", "nav": 110000.0},
        {"date": "2025-12-31", "nav": 121000.0},
    ]
    rep = rr.report(rows)
    s = rep["performance"]["summary"]
    assert "twrr" in s and s["twrr"] is not None
    assert "xirr" not in s            # no money-weighted question to answer


def test_report_twrr_equals_compounded_return_with_no_flows():
    """With no external flows, TWRR == the compounded window return."""
    from blue_engine.portfolio import risk_report as rr

    rows = [
        {"date": "2025-01-01", "nav": 100.0},
        {"date": "2025-06-01", "nav": 110.0},
        {"date": "2025-12-31", "nav": 121.0},
    ]
    s = rr.report(rows)["performance"]["summary"]
    assert s["twrr"] == pytest.approx(0.21, abs=1e-9)


def test_report_does_not_break_on_empty_rows():
    from blue_engine.portfolio import risk_report as rr

    rep = rr.report([])
    assert rep["ok"] is False
    # summary may be absent, but nothing must raise.
