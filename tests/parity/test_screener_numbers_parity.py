"""Parity: src/quant/data/screener_numbers.py vs Blue_engine/screener/client/numbers.py.

Blue's numbers.py uses only the standard library (re, typing), so it loads here with no stubs.
Every function is compared on a fixed grid. Outputs must match exactly, including None.

Flow stage: Screener rules (stage 6, sub-stage 2a)
Status: TEST
"""
import importlib.util
import itertools
import math
import pathlib
import sys

import pytest

from quant.data import screener_numbers as ours

BLUE_NUMBERS = (pathlib.Path(__file__).resolve().parents[2]
                / "src" / "quant" / "blue_engine" / "screener" / "client" / "numbers.py")


@pytest.fixture(scope="module")
def blue():
    spec = importlib.util.spec_from_file_location("blue_screener_numbers_ref", BLUE_NUMBERS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # register before exec (safe for dataclasses/typing)
    spec.loader.exec_module(mod)
    return mod


CELLS = [
    None, True, False, 0, 7, -3, 2.5, -35.09, float("nan"), float("inf"),
    "", " ", "-", "--", "—", "–", "N/A", "n/a", "NA", "null", "None", "nil", "Not available.",
    "Not applicable:", "1,23,456 Cr.", "1,23,456", "1,00,000", "1,234.56", "₹1,234", "$5",
    "₹1.2 Cr.", "50 Lakh", "2 lac", "1.2 bn", "3.5 mn", "3 k", "1 crores", "1 Cr", "1 crore",
    "12.3%", "-35.09%", "+4.5", ".5", "-.25", "abc", "Cr.", "1 2 3", "1,234 / 1,100",
    "18.5", "12,000,000", "0.0", "1e6", "nan", "1.2.3", "  50   Lakh  ", "  5 Cr  ",
]
SCALES = [None, "cr", "absolute", "absolute_cr", "zzz"]


def test_to_number_grid_matches_blue(blue):
    checked = 0
    diffs = []
    for cell, scale in itertools.product(CELLS, SCALES):
        a = ours.to_number(cell, unit_scale=scale)
        b = blue.to_number(cell, unit_scale=scale)
        checked += 1
        same = (a is None and b is None) or (
            a is not None and b is not None and (a == b or (math.isnan(a) and math.isnan(b))))
        if not same:
            diffs.append((cell, scale, a, b))
    assert checked == len(CELLS) * len(SCALES)
    assert not diffs, diffs[:5]


def test_clean_and_null_tokens_match_blue(blue):
    for cell in CELLS:
        assert ours.clean(cell) == blue.clean(cell), cell
        assert ours.is_null_token(cell) == blue.is_null_token(cell), cell


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return a == b or (math.isnan(a) and math.isnan(b))


def test_to_percent_matches_blue(blue):
    for cell in CELLS:
        assert _same(ours.to_percent(cell), blue.to_percent(cell)), cell


RATIO_FIXTURES = [
    [],
    [{"id": "market-cap", "name": "Market Cap", "value": "1,23,456 Cr."}],
    [{"id": "", "name": "Stock P/E", "value": "18.5"}],
    [{"id": "stock-pe", "name": "P/E", "value": "-"}, {"id": "", "name": "P/E", "value": "21.0"}],
    [{"id": "roce", "name": "ROCE", "value": "N/A"}, {"id": "", "name": "Return on capital", "value": "14%"}],
    [{"id": "debt-equity", "name": "Debt to equity", "value": ""}],
    [{"name": "Dividend Yield", "value": "1.2 %"}],
    [{"id": None, "name": None, "value": None}],
]


@pytest.mark.parametrize("metric", sorted(ours.RATIO_ALIASES) + ["unknown_metric"])
@pytest.mark.parametrize("rows", RATIO_FIXTURES)
def test_ratio_lookup_matches_blue(blue, metric, rows):
    assert ours.ratio_lookup(rows, metric) == blue.ratio_lookup(rows, metric)
    for scale in SCALES:
        a = ours.metric_number(rows, metric, unit_scale=scale)
        b = blue.metric_number(rows, metric, unit_scale=scale)
        assert a == b or (a is None and b is None), (metric, scale)


TABLE = {
    "headers": ["", "Mar 2024", "Mar 2025", "Mar 2026"],
    "rows": [
        {"": "Sales", "Mar 2024": "1,000", "Mar 2025": "1,200", "Mar 2026": "-"},
        {"": "Borrowings", "Mar 2024": "50 Cr.", "Mar 2025": "", "Mar 2026": "2,000"},
        {"": "Net Profit", "Mar 2024": "", "Mar 2025": "", "Mar 2026": ""},
    ],
}


@pytest.mark.parametrize("frags", [["sales"], ["borrowings"], ["total borrowings"], ["nothing"], []])
def test_table_row_lookup_matches_blue(blue, frags):
    assert ours.table_row_lookup(TABLE, frags) == blue.table_row_lookup(TABLE, frags)
    assert ours.table_row_lookup(None, frags) == blue.table_row_lookup(None, frags)
    assert ours.table_row_lookup({}, frags) == blue.table_row_lookup({}, frags)


@pytest.mark.parametrize("label", ["Sales", "Borrowings", "Net Profit", "missing"])
def test_latest_period_value_matches_blue(blue, label):
    row = ours.table_row_lookup(TABLE, [label]) or blue.table_row_lookup(TABLE, [label])
    headers = TABLE["headers"]
    for scale in SCALES:
        a = ours.latest_period_value(row, headers, unit_scale=scale)
        b = blue.latest_period_value(row, headers, unit_scale=scale)
        assert a == b or (a is None and b is None), (label, scale)
    assert ours.latest_period_value(None) is None and blue.latest_period_value(None) is None
