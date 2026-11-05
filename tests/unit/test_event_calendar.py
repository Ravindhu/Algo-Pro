"""NSE event-calendar parser and blackout window (no network, uses a saved sample file)."""
import json
import pathlib
from datetime import date

import pytest

from quant.data.market_inputs import event_blackout_dates, parse_nse_event_calendar

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "nse_event_calendar_sample.json"


def _records():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parser_reads_dates_and_skips_bad_rows():
    events = parse_nse_event_calendar(_records())
    assert len(events) == 3
    assert {e["symbol"] for e in events} == {"AADHARHFC", "ABFRL", "ADFFOODS"}
    assert all(e["date"] == date(2026, 11, 5) for e in events)


def test_parser_keeps_purpose_text():
    events = parse_nse_event_calendar(_records())
    adf = next(e for e in events if e["symbol"] == "ADFFOODS")
    assert adf["purpose"] == "Financial Results/Dividend"


def test_parser_refuses_wrong_date_format_without_guessing():
    events = parse_nse_event_calendar([{"symbol": "X", "date": "2026-11-05"}])
    assert events == []


def test_parser_skips_missing_symbol():
    events = parse_nse_event_calendar([{"symbol": " ", "date": "05-Nov-2026"}])
    assert events == []


def test_blackout_window_covers_day_before_and_after():
    events = parse_nse_event_calendar(_records())
    blocked = event_blackout_dates(events, "abfrl")
    assert blocked == {date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)}


def test_blackout_is_empty_for_other_symbols():
    events = parse_nse_event_calendar(_records())
    assert event_blackout_dates(events, "RELIANCE") == set()


def test_blackout_window_size_is_configurable():
    events = parse_nse_event_calendar(_records())
    assert event_blackout_dates(events, "ABFRL", days_before=0, days_after=0) == {date(2026, 11, 5)}


def test_negative_window_is_refused():
    with pytest.raises(ValueError):
        event_blackout_dates([], "ABFRL", days_before=-1)
