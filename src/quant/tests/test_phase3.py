"""Phase 3: session guards, exits, router, protection, session, throttle, monitors, alerts, dashboard, accounting."""
from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from quant.data.ingest import normalize_fill_event
from quant.execution.protection import protective_stop_intent
from quant.execution.router import pov_child_qty, vwap_schedule
from quant.monitor.live_monitor import excursions, max_drawdown
from quant.monitor.session import BrokerSession
from quant.monitor.strategy_health import cusum_alarm, rolling_sharpe
from quant.monitor.throttle import TokenBucket
from quant.monitor.tca import mean_slippage_bps, slippage_bps
from quant.notify.alerts import Alerts
from quant.portfolio.portfolio_risk import historical_cvar
from quant.reporting.accounting import fifo_realised, require_role
from quant.reporting.dashboard import build_payload
from quant.risk.controls import HaltHandler, carry_allowed, gap_exit_required, in_event_blackout
from quant.risk.exits import ExitState, check_exit
from quant.risk.squareoff import roll_due, should_square_off


def test_halt_freeze_and_resume():
    h = HaltHandler()
    h.freeze("NIFTY")
    assert h.is_frozen("NIFTY")
    h.resume("NIFTY")
    assert not h.is_frozen("NIFTY")


def test_blackout_carry_and_gap_rules():
    assert in_event_blackout(date(2026, 2, 1), [date(2026, 2, 1)])
    assert not in_event_blackout(date(2026, 2, 2), [date(2026, 2, 1)])
    assert not carry_allowed("MIS", adv_ok=True)
    assert carry_allowed("CNC", adv_ok=True) and not carry_allowed("CNC", adv_ok=False)
    assert gap_exit_required(100, 108) is None
    assert gap_exit_required(100, 85) is not None


def test_square_off_and_rollover():
    assert should_square_off(time(15, 20), "MIS")
    assert not should_square_off(time(15, 20), "CNC")
    assert roll_due(date(2026, 11, 26), date(2026, 11, 24))
    assert not roll_due(date(2026, 11, 26), date(2026, 11, 1))


def test_stop_and_trailing_and_time_exits():
    st = ExitState(entry=100, side=1, stop_pct=0.05, trail_pct=0.03, time_limit_bars=5)
    reason, st = check_exit(st, 95.0)
    assert reason == "stop"
    st = ExitState(entry=100, side=1, stop_pct=0.10, trail_pct=0.03, time_limit_bars=50)
    _, st = check_exit(st, 110.0)
    reason, _ = check_exit(st, 106.0)
    assert reason == "trailing"
    st = ExitState(entry=100, side=1, stop_pct=0.1, trail_pct=0.1, time_limit_bars=2)
    _, st = check_exit(st, 100.5)
    reason, _ = check_exit(st, 100.6)
    assert reason == "time"


def test_pov_and_vwap():
    assert pov_child_qty(remaining=50, bar_volume=10_000, pov=0.1) == 50
    assert pov_child_qty(remaining=500, bar_volume=10_000, pov=0.1) == 1000 - 500
    assert sum(vwap_schedule(1000, [1, 2, 3])) == 1000
    assert vwap_schedule(10, [1, 1]) == [5, 5]


def test_protection_intent_is_exit_and_opposite_side():
    s = protective_stop_intent("I1", "A", "S1", position_qty=100, stop_price=95.0)
    assert s.side == "SELL" and s.kind == "protection" and s.qty == 100
    short = protective_stop_intent("I2", "A", "S1", position_qty=-50, stop_price=105.0)
    assert short.side == "BUY"
    with pytest.raises(ValueError):
        protective_stop_intent("I3", "A", "S1", 0, 1.0)


def test_broker_session_and_throttle():
    s = BrokerSession(max_age=timedelta(hours=20))
    now = datetime(2026, 11, 3, 9, 0)
    assert s.needs_login(now)
    s.record_login(now)
    assert not s.needs_login(now + timedelta(hours=5))
    assert s.needs_login(now + timedelta(hours=21))
    tb = TokenBucket(rate_per_sec=2, capacity=2)
    assert tb.allow(0.0) and tb.allow(0.0) and not tb.allow(0.0)
    assert tb.allow(1.0)


def test_normalize_fill_and_reject_bad():
    f = normalize_fill_event({"order_id": 7, "symbol": "A", "side": "SELL", "filled_qty": 5, "avg_price": 10.5})
    assert f["signed_qty"] == -5 and f["order_id"] == "7"
    with pytest.raises(ValueError):
        normalize_fill_event({"order_id": 1, "symbol": "A"})


def test_monitors_drawdown_excursion_slippage():
    assert max_drawdown([100, 120, 90, 110]) == pytest.approx(0.25)
    mae, mfe = excursions(100, [98, 104, 101], side=1)
    assert mae == pytest.approx(0.02) and mfe == pytest.approx(0.04)
    assert slippage_bps("BUY", 100.1, 100.0) == pytest.approx(10.0)
    assert slippage_bps("SELL", 99.9, 100.0) == pytest.approx(10.0)
    assert mean_slippage_bps([{"side": "BUY", "fill": 100.1, "arrival": 100}]) > 0


def test_strategy_health_detects_decay():
    rng = np.random.default_rng(8)
    good = rng.normal(0.001, 0.01, 300)
    bad = rng.normal(-0.004, 0.01, 300)
    assert not cusum_alarm(good, mu0=0.0, k=0.001, h=0.15)
    assert cusum_alarm(bad, mu0=0.0, k=0.001, h=0.15)
    rs = rolling_sharpe(good, window=60)
    assert np.isnan(rs[10]) and not np.isnan(rs[-1])


def test_alerts_critical_calls_handler():
    seen = []
    a = Alerts(on_critical=seen.append)
    a.emit("WARNING", "x", "minor")
    assert seen == []
    a.emit("CRITICAL", "ledger", "recon mismatch")
    assert seen and "recon mismatch" in seen[0]
    with pytest.raises(ValueError):
        a.emit("LOUD", "x", "y")


def test_dashboard_shows_pnl_and_ev_hides_mae_mfe():
    # AGENTS rule 8 (decision 0008, EV ban lifted 2026-11-09): P&L and EV shown; MAE/MFE and rule compliance removed.
    trades = [{"ret": 0.02, "pnl": 2000.0, "mae": 0.01, "mfe": 0.03},
              {"ret": -0.01, "pnl": -1000.0, "mae": 0.02, "mfe": 0.005}]
    payload = build_payload(trades, risk_used=0.3)
    assert payload["total_pnl"] == pytest.approx(1000.0)
    assert payload["profit_factor"] == pytest.approx(2.0)
    assert payload["ev_per_trade"] == pytest.approx(500.0)
    for key in ("avg_mae_pct", "avg_mfe_pct", "rule_breaches"):
        assert key not in payload


def test_accounting_needs_role_and_fifo_terms():
    with pytest.raises(PermissionError):
        require_role("trader")
    require_role("accountant")
    out = fifo_realised(
        buys=[(date(2025, 1, 1), 10, 100.0), (date(2025, 6, 1), 10, 120.0)],
        sells=[(date(2026, 3, 1), 15, 150.0)],
    )
    assert out[0]["term"] == "LTCG" and out[0]["qty"] == 10
    assert out[1]["term"] == "STCG" and out[1]["qty"] == 5
    with pytest.raises(ValueError):
        fifo_realised(buys=[(date(2025, 1, 1), 1, 1.0)], sells=[(date(2025, 2, 1), 5, 1.0)])


def test_historical_cvar_positive():
    assert historical_cvar(np.linspace(-0.1, 0.1, 100), 0.1) > 0.08


def test_only_broker_adapter_and_scripts_import_openalgo_client():
    """Only the order gate path and runners may reach the broker adapter."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    allowed = {root / "src/quant/execution/openalgo_client.py"}
    tests_dir = root / "src" / "quant" / "tests"
    for py in list((root / "src/quant").rglob("*.py")) + list((root / "scripts").glob("*.py")):
        if py in allowed or py.name == "run_paper.py" or py.parent.name == "scripts":
            continue
        if {"openalgo_platform", "vendor"} & set(py.parts):  # vendored upstream code (decisions 0013, 0014)
            continue
        # The quant tests now live inside the package (src/quant/tests/). They name
        # this module in order to ASSERT the rule, so scanning them would make the
        # guard fail on its own test. Production modules are still fully checked.
        if tests_dir in py.parents or py.name.startswith("test_"):
            continue
        # Match the IMPORT, not a free-text mention. ``live_approval.py`` refers to
        # this module in its docstring to say where the gate lives ("see
        # execution/openalgo_client.LiveApproval"); that is documentation, not a
        # call path, and the old substring check flagged it. A real reach-in is an
        # import or a direct attribute use, so match those forms only.
        import re
        text = py.read_text()
        assert not re.search(
            r"^\s*(from|import)\s+[\w.]*openalgo_client\b"
            r"|\bopenalgo_client\.(place|modify|cancel|order|\.)",
            text, re.M), py


def test_calendar_empty_is_unverified_and_holidays_block(tmp_path):
    from quant.core.calendar import TradingCalendar

    empty = tmp_path / "e.csv"
    empty.write_text("date,exchange,name\n")
    cal = TradingCalendar.from_csv(empty)
    assert not cal.verified and cal.is_trading_day(date(2026, 11, 3))
    full = tmp_path / "f.csv"
    full.write_text("date,exchange,name\n2026-11-04,NSE,Test holiday\n")
    cal2 = TradingCalendar.from_csv(full)
    assert cal2.verified and not cal2.is_trading_day(date(2026, 11, 4))
    assert not cal2.is_trading_day(date(2026, 11, 7))  # Saturday
