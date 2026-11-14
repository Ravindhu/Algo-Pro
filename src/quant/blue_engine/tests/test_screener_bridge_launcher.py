"""CSV symbol intake for the screener bridge launcher (offline test)."""
import csv
import sys
from pathlib import Path

# parents: tests -> blue_engine -> quant -> src -> repo root (4 levels up).
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts" / "run"))
from run_screener_bridge import read_symbols_from_csv  # noqa: E402


def _write(tmp_path, rows):
    p = tmp_path / "filtered_summary.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return p


def test_prefers_tradable_symbol_and_skips_unresolved(tmp_path):
    p = _write(tmp_path, [
        {"_symbol": "x1", "tradable_symbol": "KNRCON", "exchange": "NSE",
         "symbol_resolved": "True"},
        {"_symbol": "500319", "tradable_symbol": "500319", "exchange": "BSE",
         "symbol_resolved": "True"},
        {"_symbol": "544340", "tradable_symbol": "544340", "exchange": "BSE",
         "symbol_resolved": "False"},
    ])
    symbols, exchange = read_symbols_from_csv(p)
    assert symbols == [("KNRCON", "NSE"), ("500319", "BSE")]
    assert exchange == "NSE"          # majority exchange


def test_falls_back_to_symbol_column_without_duplicates(tmp_path):
    p = _write(tmp_path, [
        {"symbol": "INFY", "exchange": "NSE"},
        {"symbol": "infy", "exchange": "NSE"},
        {"symbol": "GOCLCORP", "exchange": "NSE"},
    ])
    symbols, exchange = read_symbols_from_csv(p)
    assert symbols == [("INFY", "NSE"), ("GOCLCORP", "NSE")]
    assert exchange == "NSE"
