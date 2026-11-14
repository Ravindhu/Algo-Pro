"""resolve_watchlist (moved from the removed console to blue_engine/screener/watchlist.py).

Pure logic: extras, dedupe, exchange filter. Preset lookup is not tested here: the presets module is not in the repo yet.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
# Load the file by path: the Blue_engine package name is not importable from tests/unit.
_spec = importlib.util.spec_from_file_location(
    "watchlist_under_test", ROOT / "src" / "quant" / "blue_engine" / "screener" / "watchlist.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
resolve_watchlist = _mod.resolve_watchlist


def test_extras_are_normalised_and_deduped():
    out = resolve_watchlist(extra_symbols=[
        {"symbol": " infy ", "exchange": "nse"},
        {"symbol": "INFY", "exchange": "NSE"},
        {"symbol": "TCS", "exchange": "BSE"},
    ])
    assert out == [{"symbol": "INFY", "exchange": "NSE"}, {"symbol": "TCS", "exchange": "BSE"}]


def test_exchange_filter_drops_other_exchanges():
    out = resolve_watchlist(extra_symbols=[
        {"symbol": "INFY", "exchange": "NSE"}, {"symbol": "TCS", "exchange": "BSE"},
    ], exchange_filter="nse")
    assert out == [{"symbol": "INFY", "exchange": "NSE"}]


def test_bad_rows_are_skipped():
    out = resolve_watchlist(extra_symbols=[{"symbol": "X"}, "junk", {"symbol": "", "exchange": "NSE"}])
    assert out == []


def test_unknown_preset_gives_no_rows_not_an_error():
    assert resolve_watchlist(preset="NIFTY50") == []
