"""
test_optimizer_live_data_fixes.py - regressions for the two live-data bugs
found on 2026-10-07 while wiring the optimizer to real OpenAlgo bars.

BUG 1 - interval spelling
-------------------------
The dashboard/optimizer speak "1d", "1h", "1m". OpenAlgo's history endpoint
accepts only: 1s 5s 10s 15s 30s 45s 1m 2m 3m 5m 10m 15m 20m 30m 1h 2h 3h 4h
D W M Q Y. Passing "1d" returned HTTP 400 and NO bars, so every daily study
silently fell back to the synthetic fixture. Fix:
``data_cache.to_openalgo_interval`` maps our spelling at the SDK boundary
while the cache keeps ours.

BUG 2 - SDK history is a DataFrame, and its index is a truncated epoch
---------------------------------------------------------------------
``client.history(...)`` returns a pandas DataFrame, NOT the REST dict. The
old code only handled the dict form, so every download failed with
"history returned non-success" even though the endpoint sent 248 bars.

Worse, the SDK's frame index holds ``epoch_seconds // 1000`` typed as
``datetime64`` (endpoint sent 1759795200 for 2025-10-07; the frame held
1759795), so pandas rendered every bar as a 1970 date. Fixes:
``data_cache._extract_history_rows`` reads the raw index integers, and
``price_cache._ts`` restores the x1000 factor for that magnitude band.

These tests are pure (no network, no broker) - they assert the translation
and the timestamp arithmetic directly.
"""
from __future__ import annotations

import datetime as dt

from blue_engine.optimizer.data_cache import (
    _extract_history_rows,
    to_openalgo_interval,
)
from blue_engine.portfolio.providers import price_cache as pc


# --------------------------------------------------------------------------- #
# BUG 1 - interval spelling
# --------------------------------------------------------------------------- #

def test_daily_maps_to_capital_D():
    """The live bug: '1d' -> 'D'. Without this the endpoint 400s."""
    assert to_openalgo_interval("1d") == "D"


def test_daily_aliases_all_map_to_D():
    for alias in ("1d", "1D", "1day", "day", "daily", " 1d "):
        assert to_openalgo_interval(alias) == "D", alias


def test_weekly_and_monthly_map():
    assert to_openalgo_interval("1w") == "W"
    assert to_openalgo_interval("week") == "W"
    assert to_openalgo_interval("1mo") == "M"
    assert to_openalgo_interval("month") == "M"


def test_minute_and_hour_tokens_pass_through_unchanged():
    """These are identical on both sides, so they must NOT be rewritten."""
    for token in ("1m", "5m", "15m", "1h", "2h"):
        assert to_openalgo_interval(token) == token, token


def test_unknown_token_is_passed_through_not_invented():
    assert to_openalgo_interval("3d") == "3d"


# --------------------------------------------------------------------------- #
# BUG 2a - the SDK DataFrame shape is accepted
# --------------------------------------------------------------------------- #

def test_extract_history_rows_accepts_the_rest_dict_envelope():
    resp = {"status": "success",
            "data": [{"timestamp": "2025-10-07 09:15:00", "open": 1,
                      "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]}
    rows = _extract_history_rows(resp)
    assert rows is not None and len(rows) == 1
    assert rows[0]["close"] == 1.5


def test_extract_history_rows_returns_none_on_error_envelope():
    """An error dict must return None so the caller can surface the message."""
    assert _extract_history_rows({"status": "error",
                                  "message": "Invalid openalgo apikey"}) is None


def test_extract_history_rows_accepts_a_dataframe():
    """The live bug: the SDK returns a DataFrame, not a dict."""
    pd = __import__("pytest").importorskip("pandas")
    df = pd.DataFrame(
        {"open": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9],
         "close": [1.05, 2.05], "volume": [10, 20]})
    rows = _extract_history_rows(df)
    assert rows is not None and len(rows) == 2
    assert rows[0]["close"] == 1.05


def test_extract_history_rows_reads_the_dataframe_index_as_epoch():
    """The SDK index is the bar epoch; it must reach the row dict."""
    pd = __import__("pytest").importorskip("pandas")
    idx = pd.to_datetime([1759795200, 1759881600], unit="s")
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    df.index.name = "timestamp"
    rows = _extract_history_rows(df)
    assert rows is not None and len(rows) == 2
    # The raw epoch must be preserved as an integer for _ts to decode.
    assert int(rows[0]["timestamp"]) == 1759795200


def test_extract_history_rows_handles_unusable_input():
    assert _extract_history_rows(None) is None
    assert _extract_history_rows(42) is None


# --------------------------------------------------------------------------- #
# BUG 2b - the truncated-epoch timestamp repair
# --------------------------------------------------------------------------- #

def test_truncated_epoch_is_restored_to_the_real_date():
    """The exact live value: the SDK held 1759795 for 2025-10-07.

    1759795200 // 1000 == 1759795, so the repair must multiply by 1000.
    """
    out = pc._ts(1759795)
    assert out is not None, "the truncated epoch must decode, not be dropped"
    assert out.startswith("2025-10-07"), out


def test_truncated_epoch_as_a_string_is_restored():
    assert pc._ts("1759795").startswith("2025-10-07")


def test_plain_epoch_seconds_still_decode_correctly():
    """A normal epoch (>= the repair band) must NOT be multiplied again."""
    assert pc._ts(1759795200).startswith("2025-10-07")


def test_epoch_microseconds_are_divided_down():
    """The SDK's other mangling: epoch_seconds * 1e6."""
    assert pc._ts(1759795200 * 1_000_000).startswith("2025-10-07")


def test_iso_string_passes_through_untouched():
    assert pc._ts("2025-10-07 09:15:00") == "2025-10-07 09:15:00"


def test_datetime_object_is_formatted():
    assert pc._ts(dt.datetime(2025, 10, 7, 9, 15)) == "2025-10-07 09:15:00"


def test_none_and_junk_return_none():
    for junk in (None, "", "None", "NaT", "nan"):
        assert pc._ts(junk) is None, junk


# --------------------------------------------------------------------------- #
# End-to-end: a DataFrame shaped exactly like the live SDK one
# --------------------------------------------------------------------------- #

def test_full_pipeline_dataframe_to_correct_iso_dates():
    """The whole path: SDK frame -> extract -> normalise -> real dates.

    This is the regression that would have caught both bugs together.
    """
    pd = __import__("pytest").importorskip("pandas")
    # Mimic the SDK: index = real epoch // 1000, typed as datetime64[s].
    raw_seconds = [1759795200, 1759881600]          # 2025-10-07, 2025-10-08
    truncated = pd.to_datetime([s // 1000 for s in raw_seconds], unit="s")
    df = pd.DataFrame({"open": [1.0, 2.0], "high": [1.1, 2.1],
                       "low": [0.9, 1.9], "close": [1.05, 2.05],
                       "volume": [10, 20]}, index=truncated)
    df.index.name = "timestamp"

    rows = _extract_history_rows(df)
    norm = pc.normalise_rows(rows)

    assert len(norm) == 2
    assert norm[0]["ts"].startswith("2025-10-07"), norm[0]["ts"]
    assert norm[1]["ts"].startswith("2025-10-08"), norm[1]["ts"]
