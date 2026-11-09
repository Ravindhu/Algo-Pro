"""Parity: quant/core/auction_windows.py vs Blue_engine/core/auction_windows.py (stage 6.5, decision 0024).

Blue imports its clock inside each predicate (blue_engine.core.time_utils). The fixture stubs that
module with an ist_now that fails if called. So any Blue clock read fails the test, and both sides
get the same explicit time. Blue's env override is cleared for each test.

Flow stage: Session guards
Status: TEST
"""
import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from quant.core import auction_windows as ours

BLUE_AW = pathlib.Path(__file__).resolve().parents[2] / "Blue_engine" / "core" / "auction_windows.py"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module")
def blue():
    def _no_clock():
        raise AssertionError("Blue read the system clock; the port must get the time from the caller")

    stub_pkg = types.ModuleType("blue_engine")
    stub_core = types.ModuleType("blue_engine.core")
    stub_time = types.ModuleType("blue_engine.core.time_utils")
    stub_time.ist_now = _no_clock
    saved = {k: sys.modules.get(k) for k in ("blue_engine", "blue_engine.core", "blue_engine.core.time_utils")}
    sys.modules.update({"blue_engine": stub_pkg, "blue_engine.core": stub_core,
                        "blue_engine.core.time_utils": stub_time})
    try:
        spec = importlib.util.spec_from_file_location("blue_auction_windows_ref", BLUE_AW)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("POV_SME_AUCTION_WINDOWS", raising=False)


SPECS = [
    None,
    "09:15-15:30",
    "09:15-10:00,11:00-11:30",
    [{"start": "09:15", "end": "10:00"}, ("11:00", "11:30")],
]

BAD = ["", [], "09:15", "10:00-09:00", "25:00-26:00", ["x"], 42, ["09:15-10:00", "bad"]]


def _outcome(fn, arg):
    try:
        return ("ok", fn(arg))
    except ValueError:
        return ("error", None)


@pytest.mark.parametrize("spec", SPECS + BAD)
def test_parse_matches_blue(blue, spec):
    a = _outcome(ours.parse_auction_windows, spec)
    b = _outcome(blue.parse_auction_windows, spec)
    assert a == b, spec


def _minutes_grid():
    return [datetime(2026, 11, 6, h, m) for h in range(9, 17) for m in range(60)]


@pytest.mark.parametrize("spec", SPECS)
def test_membership_grid_matches_blue(blue, spec):
    for now in _minutes_grid():
        assert ours.is_within_auction_window(now, spec) == blue.is_within_auction_window(now, spec), (now, spec)
        a = ours.current_auction_window(now, spec)
        b = blue.current_auction_window(now, spec)
        assert a == b, (now, spec)


def test_aware_times_match_blue(blue):
    cases = [
        datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc),   # 09:30 IST
        datetime(2026, 11, 6, 3, 44, tzinfo=timezone.utc),  # 09:14 IST
        datetime(2026, 11, 6, 10, 0, tzinfo=IST),
    ]
    for now in cases:
        assert ours.is_within_auction_window(now) == blue.is_within_auction_window(now), now
