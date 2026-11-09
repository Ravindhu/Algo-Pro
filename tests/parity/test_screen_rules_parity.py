"""Parity: src/quant/data/screen_rules.py vs Blue_engine/screener/filters.py (real Blue leaf).

Runs in the sandbox through the isolated loader in conftest.py. Covers the rule outcomes,
the to_number cell grammar used by rules, the sector gate and the default two-rule screen.
Known, intentional difference: Blue accepts any ``on_missing`` string; the local rule
refuses anything other than fail/pass/unknown (stricter, fail-closed). Not in this grid.
"""
import pytest

from quant.data import screen_rules as local

CELLS = [None, "", "-", "N/A", True, 0, 0.0, 199.99, 200, 200.0, 200.01, "1,23,456 Cr.",
         "50 Lakh", "2.5 bn", "12%", "₹ 300", "abc", 1, "1.0", 0.99999, 1.0, "1.00x",
         "−5", "-5", ".5", "1,000.5 mn", float("nan"), "2 k"]


def _rule_grid():
    for metric in ("market_cap", "debt_equity"):
        for op in (">", ">=", "<", "<=", "==", "!="):
            for thr in (1.0, 200.0):
                for miss in ("fail", "pass", "unknown"):
                    yield metric, op, thr, miss


@pytest.mark.parametrize("cell", CELLS, ids=repr)
def test_to_number_matches_blue(blue, cell):
    numbers = blue("screener.client.numbers")
    want = numbers.to_number(cell)
    got = local.to_number(cell)
    if want is None or got is None:
        assert want is None and got is None
    elif want != want:  # NaN from Blue: local must also be NaN
        assert got != got
    else:
        assert got == pytest.approx(want, rel=1e-12)


@pytest.mark.parametrize("metric,op,thr,miss", list(_rule_grid()))
@pytest.mark.parametrize("cell", [None, "", "-", 0.0, 0.99, 1.0, 1.01, 200.0, 250.0, "1,23,456 Cr.", "abc"])
def test_rule_outcome_matches_blue(blue, metric, op, thr, miss, cell):
    filters = blue("screener.filters")
    b = filters.Rule(metric=metric, op=op, threshold=thr, on_missing=miss).evaluate({metric: cell})
    l = local.Rule(metric=metric, op=op, threshold=thr, on_missing=miss).evaluate({metric: cell})
    assert l.outcome == b.outcome
    if b.value is None:
        assert l.value is None
    else:
        assert l.value == pytest.approx(b.value, rel=1e-12)


def _candidates():
    return [
        {"symbol": "AAA", "sector": "Chemicals", "metrics": {"market_cap": "5,000 Cr.", "debt_equity": "0.4"}},
        {"symbol": "BBB", "sector": "Chemicals", "metrics": {"market_cap": "150 Cr", "debt_equity": "0.2"}},
        {"symbol": "CCC", "sector": "Chemicals", "metrics": {"market_cap": "300", "debt_equity": "-"}},
        {"symbol": "DDD", "sector": "Private Banks", "metrics": {"market_cap": "90000", "debt_equity": "9"}},
        {"symbol": "EEE", "sector": "Chemicals", "metrics": {"market_cap": "200", "debt_equity": "1"}},
        {"symbol": "FFF", "sector": "", "metrics": {"market_cap": "400", "debt_equity": "0.99"}},
        {"symbol": "", "metrics": {}},
    ]


@pytest.mark.parametrize("cfg", [
    {},                                           # default two-rule screen, default sector gate
    {"exclude_sectors": []},                      # gate off
    {"min_rules_passed": 1},                      # any one rule
    {"require_all": False},
    {"exclude_symbols": ["aaa"]},
])
def test_screen_verdicts_match_blue(blue, cfg):
    filters = blue("screener.filters")
    b = filters.Screener.from_config(cfg).evaluate(_candidates())
    l = local.Screener.from_config(cfg).evaluate(_candidates())
    assert [(r.symbol, r.selected, r.excluded_by) for r in l] == \
           [(r.symbol, r.selected, r.excluded_by) for r in b]
    assert [[(x.metric, x.outcome) for x in r.rules] for r in l] == \
           [[(x.metric, x.outcome) for x in r.rules] for r in b]
    assert [r.reason() for r in l] == [r.reason() for r in b]


def test_default_rules_match_blue(blue):
    filters = blue("screener.filters")
    b = [(r.metric, r.op, r.threshold, r.label) for r in filters.default_rules()]
    l = [(r.metric, r.op, r.threshold, r.label) for r in local.default_rules()]
    assert l == b
