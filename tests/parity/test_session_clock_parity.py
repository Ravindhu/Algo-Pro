"""Parity: quant/core/session_clock.py vs Blue_engine/core/time_utils.py (stage 6.5, decision 0022).

Blue's holiday list is read from a file. The fixture points Blue at a missing file, so Blue sees
no holidays. Our side uses a verified, empty calendar. Both then judge weekdays as open.
Not compared: the reason text (wording differs) and the fail-closed cases (intentional differences).

Flow stage: Trading calendar, Session guards
Status: TEST
"""
import importlib.util
import itertools
import pathlib
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quant.core import session_clock as ours
from quant.core.calendar import TradingCalendar

BLUE_TIME = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant" / "blue_engine" / "core" / "time_utils.py"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module")
def blue(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("blue_time_utils_ref", BLUE_TIME)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod._HOLIDAY_PATH = tmp_path_factory.mktemp("blue") / "missing_holidays.json"  # no holiday file
    return mod


DAYS = [date(2026, 11, d) for d in range(2, 9)]          # Mon 2 .. Sun 8 Nov 2026
TIMES = ["09:14", "09:15", "09:30", "12:00", "14:44", "14:45", "15:00", "15:29", "15:30", "23:59"]
CUTOFFS = ["14:45", "15:00", "09:30"]                     # valid values only (bad ones are not compared)


def _dt(day, hhmm):
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


def test_session_state_grid_matches_blue(blue):
    cal = TradingCalendar(set(), verified=True)
    checked = 0
    for day, hhmm, cutoff in itertools.product(DAYS, TIMES, CUTOFFS):
        now = _dt(day, hhmm)
        a = ours.market_session_state(now, entry_cutoff=cutoff, calendar=cal)
        b = blue.market_session_state(now, entry_cutoff=cutoff)
        assert (a["open"], a["state"]) == (b["open"], b["state"]), (day, hhmm, cutoff, a, b)
        checked += 1
    assert checked == len(DAYS) * len(TIMES) * len(CUTOFFS)


def test_is_past_cutoff_grid_matches_blue(blue):
    for day, hhmm, value in itertools.product(DAYS[:2], TIMES, ["09:15", "14:45", "15:15"]):
        now = _dt(day, hhmm)
        assert ours.is_past_cutoff(value, now=now) == blue.is_past_cutoff(value, now=now), (hhmm, value)


@pytest.mark.parametrize("text", ["09:05", " 15:30 ", "00:00", "23:59"])
def test_parse_hhmm_matches_blue(blue, text):
    assert ours.parse_hhmm(text) == blue.parse_hhmm(text)


@pytest.mark.parametrize("text", ["abc", "9:5:1", ""])
def test_parse_hhmm_bad_text_raises_like_blue(blue, text):
    with pytest.raises(ValueError):
        ours.parse_hhmm(text)
    with pytest.raises(ValueError):
        blue.parse_hhmm(text)


def test_session_constants_match_blue(blue):
    assert ours.SESSION_OPEN == blue.SESSION_OPEN
    assert ours.SESSION_CLOSE == blue.SESSION_CLOSE
    assert ours.DEFAULT_ENTRY_CUTOFF == blue.DEFAULT_ENTRY_CUTOFF
