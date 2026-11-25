"""Slice 5: lot size and circuit band per exchange; BSE codes in the screen input."""
from datetime import datetime

from quant.data.bse_symbol import split_for_screen
from quant.data.circuit_band import enrich_candidates
from quant.data.live_bars import LiveBars

NOW = datetime(2026, 11, 25, 10, 0)


def test_resolved_bse_code_becomes_a_bse_candidate_under_its_symbol():
    nse, bse, unresolved = split_for_screen(
        [{"symbol": "500325", "selected": True}, {"symbol": "INFY", "selected": True}],
        lambda code: "RELIANCE" if code == "500325" else None)
    assert nse == [{"symbol": "INFY", "selected": True}]
    assert bse == [{"symbol": "RELIANCE", "selected": True, "bse_code": "500325", "exchange": "BSE"}]
    assert unresolved == []


def test_unresolved_or_erroring_code_is_reported_not_traded():
    def lookup(code):
        if code == "999999":
            raise RuntimeError("master down")
        return None
    nse, bse, unresolved = split_for_screen([{"symbol": "999999"}, {"symbol": "500001"}], lookup)
    assert bse == [] and {u["code"] for u in unresolved} == {"999999", "500001"}


def test_same_openalgo_symbol_is_not_added_twice():
    _, bse, _ = split_for_screen([{"symbol": "500325"}, {"symbol": "500326"}], lambda c: "RELIANCE")
    assert [b["symbol"] for b in bse] == ["RELIANCE"]


def test_bse_band_is_read_on_bse():
    seen = []

    def reader(symbol, exchange):
        seen.append((symbol, exchange))
        return {"status": "success", "data": {"prev_close": 200.0,
                "upper_circuit_limit": 240.0, "lower_circuit_limit": 160.0}}
    out = enrich_candidates([{"symbol": "RELIANCE", "exchange": "BSE"}], reader, exchange="BSE")
    assert seen == [("RELIANCE", "BSE")]
    assert abs(out["candidates"][0]["circuit_pct"] - 0.20) < 1e-9


class _Reader:
    def __init__(self):
        self.calls = []

    def search_symbol(self, api_key, symbol, exchange="NSE"):
        self.calls.append((symbol, exchange))
        return {"status": "success", "data": [{"symbol": symbol, "lotsize": 5}]}


def test_lot_size_is_read_on_the_bars_own_exchange():
    reader = _Reader()
    assert LiveBars(reader, "key", exchange="BSE")._lot_size("RELIANCE") == 5
    assert LiveBars(reader, "key")._lot_size("RELIANCE") == 5
    assert reader.calls == [("RELIANCE", "BSE"), ("RELIANCE", "NSE")]
