"""Bot paths for the daily reading, holdings sync, MTF % and interest (owner 2026-11-25). Paper fakes only.

Flow stage: Risk gate, holdings sync, interest
Status: PHASE 3 IMPLEMENTED
"""
import sys
import pathlib
from datetime import date, datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from test_margin_bot import MARGIN, NOW, _bot, _intent, _pos  # noqa: E402
from quant.risk.margin_daily import DailyMarginStore  # noqa: E402


def _reading(margin_per_share_ratio):
    """Broker reply: margin = ratio x full value of the order (ratio 0.2 = 80% funded)."""
    return lambda i: {"status": "success", "data": {"total_margin_required": i.qty * i.ref_price * margin_per_share_ratio}}


def _holdings(rows):
    return lambda: {"status": "success", "data": rows}


def _row(symbol, qty, avg, product="MTF"):
    return {"symbol": symbol, "exchange": "NSE", "product": product, "netqty": qty, "average_price": avg}


# --- one reading per stock per day ----------------------------------------------

def test_reading_fetched_once_per_day_per_stock(tmp_path):
    calls = []

    def check(i):
        calls.append(i.symbol)
        return _reading(0.2)(i)
    bot = _bot(tmp_path, margin_check=check, margin_store=DailyMarginStore(tmp_path / "m.json"))
    for _ in range(3):
        assert bot._entry_product(_intent(symbol="INFY"))[0] == "MTF"
    assert calls == ["INFY"]


def test_reading_is_per_stock(tmp_path):
    calls = []

    def check(i):
        calls.append(i.symbol)
        return _reading(0.2)(i)
    bot = _bot(tmp_path, margin_check=check, margin_store=DailyMarginStore(None))
    bot._entry_product(_intent(symbol="INFY"))
    bot._entry_product(_intent(symbol="TCS"))
    assert sorted(calls) == ["INFY", "TCS"]


def test_stored_reading_is_used_after_restart_same_day(tmp_path):
    path = tmp_path / "m.json"
    _bot(tmp_path, margin_check=_reading(0.2), margin_store=DailyMarginStore(path))._entry_product(_intent())
    calls = []

    def check(i):
        calls.append(i)
        return _reading(0.2)(i)
    bot = _bot(tmp_path, margin_check=check, margin_store=DailyMarginStore(path))
    assert bot._entry_product(_intent())[0] == "MTF"
    assert calls == []


def test_unavailable_reading_keeps_cnc_for_the_day_without_refetch(tmp_path):
    calls = []

    def check(i):
        calls.append(i)
        return {"status": "error", "message": "no"}
    bot = _bot(tmp_path, margin_check=check, margin_store=DailyMarginStore(None))
    for _ in range(3):
        assert bot._entry_product(_intent())[0] == "CNC"
    assert len(calls) == 1


def test_funded_share_comes_from_the_reading(tmp_path):
    bot = _bot(tmp_path, margin_check=_reading(0.25), margin_store=DailyMarginStore(None))
    product, reason, funded = bot._entry_product(_intent())
    assert product == "MTF" and funded == pytest.approx(0.75)


# --- holdings sync ---------------------------------------------------------------

def test_no_sync_today_keeps_cnc(tmp_path):
    bot = _bot(tmp_path, mtf_holdings=None, margin_store=DailyMarginStore(None))
    product, reason, _ = bot._entry_product(_intent())
    assert product == "CNC" and "not synced" in reason


def test_sync_mismatch_blocks_mtf_but_not_cnc(tmp_path):
    bot = _bot(tmp_path, mtf_holdings=_holdings([_row("INFY", 50, 1000.0)]), margin_store=DailyMarginStore(None))
    product, reason, _ = bot._entry_product(_intent())
    assert product == "CNC" and "differ from the broker" in reason


def test_unreadable_broker_holdings_block_mtf(tmp_path):
    def broken():
        raise RuntimeError("secret detail")
    bot = _bot(tmp_path, mtf_holdings=broken, margin_store=DailyMarginStore(None))
    product, reason, _ = bot._entry_product(_intent())
    assert product == "CNC" and "unreadable" in reason and "secret detail" not in reason


def test_sync_sets_mtf_percentage_from_borrowed_and_held_value(tmp_path):
    bot = _bot(tmp_path, mtf_holdings=_holdings([_row("INFY", 100, 1000.0)]), margin_store=DailyMarginStore(None))
    bot._positions["INFY|s"] = _pos(qty=100, entry=1000.0, product="MTF")
    bot._positions["INFY|s"].funded_pct = 0.8                   # borrowed 80,000 of a 100,000 holding
    bot.sync_mtf_holdings(NOW.date())
    assert bot._mtf_issues == []
    assert bot._mtf_held_value == pytest.approx(100_000.0)
    assert bot._mtf_pct == pytest.approx(0.8)


def test_matching_sync_allows_mtf(tmp_path):
    bot = _bot(tmp_path, mtf_holdings=_holdings([]), margin_store=DailyMarginStore(None), margin_check=_reading(0.2))
    assert bot._entry_product(_intent())[0] == "MTF"


# --- interest at 12% a year ---------------------------------------------------------

def test_interest_is_borrowed_times_rate_per_day(tmp_path):
    bot = _bot(tmp_path)
    p = _pos(qty=100, entry=1000.0, product="MTF")
    p.funded_pct = 0.8                                         # borrowed 80,000
    bot._positions["INFY|s"] = p
    charged = bot._accrue_mtf_interest()
    assert charged == pytest.approx(80_000.0 * 0.12 / 365.0)
    assert bot.state.daily_pnl == pytest.approx(-charged)


def test_cnc_positions_pay_no_interest(tmp_path):
    bot = _bot(tmp_path)
    bot._positions["INFY|s"] = _pos(qty=100, entry=1000.0, product="CNC")
    assert bot._accrue_mtf_interest() == 0.0


def test_interest_off_when_margin_not_configured(tmp_path):
    bot = _bot(tmp_path, margin=None)
    p = _pos(qty=100, entry=1000.0, product="MTF")
    p.funded_pct = 0.8
    bot._positions["INFY|s"] = p
    assert bot._accrue_mtf_interest() == 0.0


# --- weekends: calendar days are charged, Friday to Monday is three days ---------------

def _mtf_book(bot, qty=100, entry=1000.0, funded=0.8):
    p = _pos(qty=qty, entry=entry, product="MTF")
    p.funded_pct = funded
    bot._positions["INFY|s"] = p
    return p


def test_friday_to_monday_roll_charges_three_days(tmp_path):
    from datetime import date as _d
    bot = _bot(tmp_path, margin_store=DailyMarginStore(None))
    _mtf_book(bot)
    bot._pnl_day = _d(2026, 11, 20)                       # Friday
    bot._roll_day(datetime(2026, 11, 23, 9, 0))            # Monday
    per_day = 80_000.0 * 0.12 / 365.0
    assert bot.state.daily_pnl == pytest.approx(-3 * per_day)


def test_restart_after_weekend_charges_days_owed_from_saved_book(tmp_path):
    from datetime import date as _d
    bot = _bot(tmp_path, margin_store=DailyMarginStore(None))
    _mtf_book(bot)
    bot._book_day = _d(2026, 11, 20)                       # saved book was last accounted on Friday
    bot._roll_day(datetime(2026, 11, 23, 9, 0))
    assert bot.state.daily_pnl == pytest.approx(-3 * 80_000.0 * 0.12 / 365.0)


def test_same_day_restart_does_not_charge_again(tmp_path):
    from datetime import date as _d
    bot = _bot(tmp_path, margin_store=DailyMarginStore(None))
    _mtf_book(bot)
    bot._restored_day = _d(2026, 11, 23)
    bot._book_day = _d(2026, 11, 23)
    bot._roll_day(datetime(2026, 11, 23, 9, 0))
    assert bot.state.daily_pnl == pytest.approx(0.0)


def test_accrual_with_zero_days_charges_nothing(tmp_path):
    bot = _bot(tmp_path)
    _mtf_book(bot)
    assert bot._accrue_mtf_interest(0) == 0.0
