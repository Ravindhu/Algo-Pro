"""Hand-checked cases for core/auction_windows.py (stage 6.5, decision 0024).

Flow stage: Session guards
Status: TEST
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from quant.core.auction_windows import (
    DEFAULT_SME_AUCTION_WINDOWS, current_auction_window, is_within_auction_window, parse_auction_windows,
)

IST = ZoneInfo("Asia/Kolkata")


def at(hhmm):
    h, m = hhmm.split(":")
    return datetime(2026, 11, 6, int(h), int(m), tzinfo=IST)


def test_default_is_full_session():
    assert parse_auction_windows(None) == (("09:15", "15:30"),)
    assert DEFAULT_SME_AUCTION_WINDOWS == (("09:15", "15:30"),)


def test_parse_string_list_and_dicts():
    assert parse_auction_windows("09:15-10:00, 11:00-11:30") == (("09:15", "10:00"), ("11:00", "11:30"))
    assert parse_auction_windows(["09:15-10:00"]) == (("09:15", "10:00"),)
    assert parse_auction_windows([("9:5", "10:0")]) == (("09:05", "10:00"),)
    assert parse_auction_windows([{"start": "11:00", "end": "11:30"}]) == (("11:00", "11:30"),)
    assert parse_auction_windows([{"from": "11:00", "to": "11:30"}]) == (("11:00", "11:30"),)


@pytest.mark.parametrize("bad", [
    "", [], "09:15", "09:15-", "25:00-26:00", "10:00-09:00", "10:00-10:00",
    ["09:15-10:00", "bad"], {"start": "09:15"}, 42,
])
def test_bad_windows_raise(bad):
    with pytest.raises(ValueError):
        parse_auction_windows(bad)


@pytest.mark.parametrize("hhmm, inside", [
    ("09:14", False),
    ("09:15", True),    # start inclusive (minute level)
    ("12:00", True),
    ("15:30", True),    # end inclusive (minute level, as Blue)
    ("15:31", False),
])
def test_default_window_membership(hhmm, inside):
    assert is_within_auction_window(at(hhmm)) is inside


def test_two_windows_gap_is_outside():
    windows = "09:15-10:00,11:00-11:30"
    assert is_within_auction_window(at("10:30"), windows) is False
    assert is_within_auction_window(at("11:10"), windows) is True


def test_current_window_returns_the_matching_pair():
    windows = "09:15-10:00,11:00-11:30"
    assert current_auction_window(at("11:10"), windows) == ("11:00", "11:30")
    assert current_auction_window(at("10:30"), windows) is None


def test_aware_utc_time_is_converted_to_ist():
    # 04:00 UTC is 09:30 IST: inside the default window.
    assert is_within_auction_window(datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)) is True
    # 03:00 UTC is 08:30 IST: outside.
    assert is_within_auction_window(datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)) is False


def test_module_reads_no_clock_env_or_blue():
    """The port takes the time from the caller. It never reads the clock, the env, or Blue."""
    import ast
    import inspect
    import quant.core.auction_windows as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any(name.startswith("blue_engine") for name in imported), imported
    assert "os" not in imported

    used_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used_attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "ist_now" not in used_names
    assert "getenv" not in used_attrs and "environ" not in used_attrs
    assert "now" not in used_attrs  # datetime.now() and similar are never called


def test_env_variable_is_ignored(monkeypatch):
    """Windows come from config, not from the environment (decision 0024)."""
    monkeypatch.setenv("POV_SME_AUCTION_WINDOWS", "00:00-00:01")
    assert parse_auction_windows(None) == (("09:15", "15:30"),)
