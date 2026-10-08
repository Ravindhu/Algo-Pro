"""Tests for external cash-flow capture (deposits/withdrawals).

Why this exists: before flows were recorded, a deposit raised NAV and every
downstream metric booked that as a RETURN. A 10 lakh top-up on a 10 lakh book
read as a +100% day, inflating vol/Sharpe/VaR. These tests pin the fix:

* ``EquityTracker.deposit`` / ``withdraw`` book an external flow, not P&L.
* ``snapshot`` writes a row-level ``flow`` (== 0 on a normal session).
* ``daily_returns`` STRIPS a flow so it is not counted as a return.
* ``risk_report._cash_flows_from_rows`` builds an XIRR ledger that includes the
  opening capital and every later change.
"""
from __future__ import annotations

from blue_engine.portfolio import tracker as tr


def _t(start=1_000_000.0):
    return tr.EquityTracker(starting_cash=start, charges_bps=0.0)


# --------------------------------------------------------------------------- #
# EquityTracker.deposit / withdraw
# --------------------------------------------------------------------------- #

def test_deposit_raises_cash_and_is_not_pnl():
    t = _t()
    t.deposit(250_000.0)
    assert t.cash == 1_250_000.0
    # Funding, not profit: no realised and no unrealised P&L.
    assert t.realized_pnl + t.unrealized_pnl({}) == 0.0
    assert t.net_deposits == 250_000.0
    assert t.contributions == 1_250_000.0


def test_withdraw_lowers_cash_and_is_not_a_loss():
    t = _t()
    t.withdraw(100_000.0)
    assert t.cash == 900_000.0
    assert t.realized_pnl + t.unrealized_pnl({}) == 0.0
    assert t.net_deposits == -100_000.0
    assert t.contributions == 900_000.0


def test_flow_journal_records_date_and_sign():
    t = _t()
    t.deposit(50_000.0, ts=1_760_000_000.0)
    t.withdraw(20_000.0, ts=1_760_000_000.0)
    rows = t.flows()
    assert len(rows) == 2
    assert rows[0]["amount"] == 50_000.0        # + = money in
    assert rows[1]["amount"] == -20_000.0       # - = money out
    assert t.flow_on(rows[0]["date"]) == 30_000.0   # net, same day


def test_invalid_flows_are_skipped_not_raised():
    t = _t()
    assert t.deposit(-5.0) == 0.0
    assert t.deposit(0.0) == 0.0
    assert t.withdraw(-1.0) == 0.0
    assert t.withdraw(float("nan")) == 0.0
    assert t.net_deposits == 0.0
    assert t.cash == 1_000_000.0


# --------------------------------------------------------------------------- #
# snapshot carries the flow
# --------------------------------------------------------------------------- #

def test_snapshot_has_zero_flow_on_a_normal_session():
    t = _t()
    snap = t.snapshot({})
    assert snap["flow"] == 0.0
    assert snap["net_deposits"] == 0.0


def test_snapshot_reports_the_days_deposit():
    t = _t()
    ts = 1_760_000_000.0
    t.deposit(100_000.0, ts=ts)
    snap = t.snapshot({}, ts=ts)
    assert snap["flow"] == 100_000.0
    assert snap["contributions"] == 1_100_000.0


def test_return_since_start_uses_contributed_capital():
    """A deposit must NOT be reported as a positive return."""
    t = _t()
    t.deposit(1_000_000.0)          # doubles the account
    snap = t.snapshot({})
    assert snap["return_since_start"] == 0.0    # NAV == contributions -> 0%


# --------------------------------------------------------------------------- #
# daily_returns strips the flow
# --------------------------------------------------------------------------- #

def test_daily_returns_strips_a_deposit():
    """+10% real then a 1,000,000 deposit: the return series must show only +10%."""
    rows = [
        {"date": "2025-01-01", "nav": 100_000.0},
        {"date": "2025-01-02", "nav": 110_000.0},                    # +10%
        {"date": "2025-01-03", "nav": 1_110_000.0, "flow": 1_000_000.0},
    ]
    rets = tr.daily_returns(rows)
    assert len(rets) == 2
    assert round(rets[0], 9) == 0.10
    # Day 3: (1,110,000 - 1,000,000) / 110,000 - 1 == 0.0  (NOT +909%)
    assert round(rets[1], 9) == 0.0


def test_daily_returns_flow_blind_mode_is_legacy():
    rows = [
        {"date": "2025-01-01", "nav": 100_000.0},
        {"date": "2025-01-02", "nav": 1_100_000.0, "flow": 1_000_000.0},
    ]
    raw = tr.daily_returns(rows, strip_flows=False)
    assert round(raw[0], 6) == 10.0        # the naive, wrong answer


def test_daily_returns_unchanged_without_flows():
    rows = [
        {"date": "2025-01-01", "nav": 100.0},
        {"date": "2025-01-02", "nav": 110.0},
        {"date": "2025-01-03", "nav": 121.0},
    ]
    rets = tr.daily_returns(rows)
    assert [round(r, 9) for r in rets] == [0.10, 0.10]


# --------------------------------------------------------------------------- #
# risk_report builds an XIRR ledger from rows
# --------------------------------------------------------------------------- #

def test_cash_flows_include_opening_capital_and_deltas():
    from blue_engine.portfolio import risk_report as rr

    rows = [
        {"date": "2025-01-01", "nav": 1_000_000.0, "contributions": 1_000_000.0},
        {"date": "2025-06-01", "nav": 1_100_000.0, "contributions": 1_000_000.0},
        {"date": "2025-07-01", "nav": 1_250_000.0, "contributions": 1_200_000.0},
        {"date": "2025-12-31", "nav": 1_320_000.0, "contributions": 1_200_000.0},
    ]
    flows = rr._cash_flows_from_rows(rows)
    # Opening: investor pays 1,000,000 OUT (negative).
    assert flows[0] == ("2025-01-01", -1_000_000.0)
    # Top-up: another 200,000 OUT.
    assert ("2025-07-01", -200_000.0) in flows
    # Terminal NAV: the value they could take out.
    assert flows[-1] == ("2025-12-31", 1_320_000.0)


def test_cash_flows_empty_without_flow_or_contributions():
    from blue_engine.portfolio import risk_report as rr

    rows = [{"date": "2025-01-01", "nav": 100.0},
            {"date": "2025-12-31", "nav": 110.0}]
    assert rr._cash_flows_from_rows(rows) == []


def test_report_xirr_matches_known_return():
    """A single contribution that grows 10% in a year gives XIRR ~= 10%."""
    from blue_engine.portfolio import risk_report as rr

    rows = [
        {"date": "2025-01-01", "nav": 1_000_000.0, "contributions": 1_000_000.0},
        {"date": "2025-06-01", "nav": 1_050_000.0, "contributions": 1_000_000.0},
        {"date": "2026-01-01", "nav": 1_100_000.0, "contributions": 1_000_000.0},
    ]
    s = rr.report(rows)["performance"]["summary"]
    assert s.get("xirr") is not None
    assert abs(s["xirr"] - 0.10) < 1e-4


# --------------------------------------------------------------------------- #
# daily_portfolio flow detection (helpers)
# --------------------------------------------------------------------------- #

def test_previous_contributions_reads_last_recorded(tmp_path):
    from blue_engine.jobs import daily_portfolio as dp
    from blue_engine.portfolio import tracker as tr

    assert dp._previous_contributions(str(tmp_path)) is None   # empty store
    tr.store_nav({"date": "2025-01-01", "nav": 1_000_000.0,
                  "contributions": 1_000_000.0}, directory=tmp_path)
    tr.store_nav({"date": "2025-02-01", "nav": 1_100_000.0,
                  "contributions": 1_100_000.0}, directory=tmp_path)
    assert dp._previous_contributions(str(tmp_path)) == 1_100_000.0


def test_previous_contributions_none_before_field_exists(tmp_path):
    """Rows written before `contributions` existed must not fake a baseline."""
    from blue_engine.jobs import daily_portfolio as dp
    from blue_engine.portfolio import tracker as tr

    tr.store_nav({"date": "2025-01-01", "nav": 1_000_000.0},
                 directory=tmp_path)
    assert dp._previous_contributions(str(tmp_path)) is None


def test_detected_flow_is_the_capital_delta(tmp_path):
    from blue_engine.jobs import daily_portfolio as dp
    from blue_engine.portfolio import tracker as tr

    tr.store_nav({"date": "2025-01-01", "nav": 1_000_000.0,
                  "contributions": 1_000_000.0}, directory=tmp_path)
    prev = dp._previous_contributions(str(tmp_path))
    assert 1_200_000.0 - prev == 200_000.0     # a 2 lakh top-up


def test_first_contributions_returns_opening_capital(tmp_path):
    from blue_engine.jobs import daily_portfolio as dp
    from blue_engine.portfolio import tracker as tr

    tr.store_nav({"date": "2025-01-01", "nav": 1_000_000.0,
                  "contributions": 1_000_000.0}, directory=tmp_path)
    assert dp._first_contributions(str(tmp_path), 9_999_999.0) == 1_000_000.0
    # No history -> falls back to the current capital (net_deposits would be 0).
    assert dp._first_contributions(str(tmp_path / "nope"), 500.0) == 500.0
