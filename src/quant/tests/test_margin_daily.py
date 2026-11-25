"""Once-a-day margin reading store (owner 2026-11-25). Files live under pytest tmp_path only.

Flow stage: Risk gate (margin funding), daily facility record
Status: PHASE 3 IMPLEMENTED
"""
import json
from datetime import date

from quant.risk.margin_daily import DailyMarginStore

D1 = date(2026, 11, 25)
D2 = date(2026, 11, 26)
REC = {"available": True, "funded_pct": 0.8, "reason": "ok"}


def test_reading_kept_for_the_same_day_only(tmp_path):
    store = DailyMarginStore(tmp_path / "m.json")
    store.put("INFY", D1, REC)
    assert store.get("INFY", D1) == REC
    assert store.get("INFY", D2) is None           # a new trade day has no reading yet


def test_new_day_clears_the_old_readings(tmp_path):
    store = DailyMarginStore(tmp_path / "m.json")
    store.put("INFY", D1, REC)
    store.put("TCS", D2, REC)
    assert store.get("INFY", D2) is None
    assert store.get("TCS", D2) == REC


def test_reading_survives_restart_on_the_same_day(tmp_path):
    path = tmp_path / "m.json"
    DailyMarginStore(path).put("INFY", D1, REC)
    assert DailyMarginStore(path).get("INFY", D1) == REC


def test_unreadable_file_is_empty_so_readings_are_fetched_again(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json", encoding="utf-8")
    store = DailyMarginStore(path)
    assert store.get("INFY", D1) is None


def test_wrong_shape_file_is_empty(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"day": "2026-11-25", "records": ["x"]}), encoding="utf-8")
    assert DailyMarginStore(path).get("INFY", D1) is None


def test_write_is_atomic_no_temp_file_left(tmp_path):
    path = tmp_path / "m.json"
    DailyMarginStore(path).put("INFY", D1, REC)
    assert path.exists() and not (tmp_path / "m.json.tmp").exists()


def test_unavailable_reading_is_stored_too(tmp_path):
    store = DailyMarginStore(tmp_path / "m.json")
    store.put("INFY", D1, {"available": False, "funded_pct": 0.0, "reason": "no funding"})
    assert store.get("INFY", D1)["available"] is False


def test_memory_only_store_without_path():
    store = DailyMarginStore(None)
    store.put("INFY", D1, REC)
    assert store.get("INFY", D1) == REC
