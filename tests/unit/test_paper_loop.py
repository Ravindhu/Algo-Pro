"""Paper loop control (decision 0029): session rules, start and stop refusals, heartbeat and stop flag."""
import pathlib
import sys
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.calendar import TradingCalendar  # noqa: E402
from quant.monitor.paper_loop import (IST, loop_status, session_reason, start_loop,  # noqa: E402
                                      stop_loop, write_state)

CAL = TradingCalendar.from_csv(ROOT / "config" / "calendar" / "holidays.csv")
INTERVAL = 60


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_session_open_during_cash_hours_on_a_trading_day():
    assert session_reason(at(2026, 11, 9, 10, 0), CAL) == (True, "NSE cash session")  # Monday
    assert session_reason(at(2026, 11, 9, 9, 15), CAL)[0] is True


def test_session_closed_before_open_and_after_close():
    assert session_reason(at(2026, 11, 9, 9, 14), CAL) == (False, "before 09:15 IST")
    assert session_reason(at(2026, 11, 9, 15, 30), CAL) == (False, "after 15:30 IST")


def test_session_closed_on_weekend_and_on_exchange_holiday():
    assert session_reason(at(2026, 11, 7, 10, 0), CAL) == (False, "weekend")  # Saturday
    assert session_reason(at(2026, 11, 10, 10, 0), CAL) == (False, "exchange holiday")  # Diwali-Balipratipada


def test_session_fails_closed_when_calendar_not_verified():
    unverified = TradingCalendar(set(), verified=False)
    assert session_reason(at(2026, 11, 9, 10, 0), unverified)[0] is False


def test_holiday_csv_has_the_equity_dates_from_the_nse_circular():
    assert date(2026, 1, 15) in CAL.holidays  # amendment, NSE circular 07/2026
    assert date(2026, 12, 25) in CAL.holidays
    assert date(2026, 2, 19) not in CAL.holidays  # currency-only, not an equity holiday


def test_start_refused_while_kill_switch_is_on(tmp_path):
    spawned = []
    ok, msg = start_loop(tmp_path, True, INTERVAL, lambda: spawned.append(1), now_ts=1000.0)
    assert ok is False and "Kill switch" in msg
    assert spawned == []


def test_start_spawns_once_and_second_start_is_refused(tmp_path):
    spawned = []
    ok, _ = start_loop(tmp_path, False, INTERVAL, lambda: spawned.append(1), now_ts=1000.0)
    assert ok is True and spawned == [1]
    assert loop_status(tmp_path, 1000.0, INTERVAL)["status"] == "running"  # "starting" counts as running
    ok2, msg2 = start_loop(tmp_path, False, INTERVAL, lambda: spawned.append(1), now_ts=1001.0)
    assert ok2 is False and "already running" in msg2
    assert spawned == [1]


def test_failed_spawn_leaves_loop_stopped(tmp_path):
    def boom():
        raise OSError("no python")
    ok, msg = start_loop(tmp_path, False, INTERVAL, boom, now_ts=1000.0)
    assert ok is False and "Could not start" in msg
    assert loop_status(tmp_path, 1000.0, INTERVAL)["status"] == "stopped"


def test_stale_heartbeat_means_stopped(tmp_path):
    write_state(tmp_path, status="running", heartbeat_ts=1000.0, pid=42)
    assert loop_status(tmp_path, 1000.0 + 2 * INTERVAL, INTERVAL)["status"] == "running"
    assert loop_status(tmp_path, 1000.0 + 4 * INTERVAL, INTERVAL)["status"] == "stopped"


def test_stop_writes_flag_only_when_running(tmp_path):
    ok, msg = stop_loop(tmp_path, INTERVAL, now_ts=1000.0)
    assert ok is True and "not running" in msg
    assert not (tmp_path / "stop.flag").exists()
    write_state(tmp_path, status="running", heartbeat_ts=1000.0)
    ok, msg = stop_loop(tmp_path, INTERVAL, now_ts=1001.0)
    assert ok is True and "Open positions are not closed here" in msg
    assert loop_status(tmp_path, 1001.0, INTERVAL)["stop_requested"] is True
