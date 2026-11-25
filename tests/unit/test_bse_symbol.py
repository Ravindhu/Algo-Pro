"""BSE code -> OpenAlgo symbol -> broker check (src/quant/data/bse_symbol.py). Read only."""
from quant.data import bse_symbol as bs


def _ok(symbol, exchange):
    return {"status": "success", "data": {"ltp": 100.0}}


def test_is_bse_code_accepts_only_six_digits():
    assert bs.is_bse_code("500325") and bs.is_bse_code(" 500325 ")
    for bad in ("RELIANCE", "50032", "5003250", "M&M", "", None, 500325):
        assert bs.is_bse_code(bad) is False


def test_resolved_and_matched_is_reported_but_not_tradable():
    rows = bs.bse_report([{"symbol": "500325"}], lambda c: "RELIANCE", _ok)
    assert rows == [{"code": "500325", "symbol": "RELIANCE", "broker_match": True,
                     "reason": "broker knows this BSE symbol; bot is NSE-only, not tradable",
                     "tradable": False}]


def test_code_missing_from_master_is_not_matched():
    rows = bs.bse_report([{"symbol": "999999"}], lambda c: None, _ok)
    assert rows[0]["symbol"] is None and rows[0]["broker_match"] is False
    assert "not in OpenAlgo symbol master" in rows[0]["reason"]


def test_lookup_error_fails_closed():
    def boom(_code):
        raise RuntimeError("master unavailable")
    rows = bs.bse_report([{"symbol": "500325"}], boom, _ok)
    assert rows[0]["broker_match"] is False and rows[0]["tradable"] is False


def test_failed_or_missing_broker_quote_is_not_matched():
    def fail(_s, _e):
        return {"status": "error", "message": "symbol not found"}
    def raises(_s, _e):
        raise ConnectionError("down")
    for reader in (fail, raises, None):
        rows = bs.bse_report([{"symbol": "500325"}], lambda c: "RELIANCE", reader)
        assert rows[0]["broker_match"] is False and rows[0]["tradable"] is False


def test_non_numeric_symbols_are_ignored():
    rows = bs.bse_report([{"symbol": "TCS"}, {"symbol": "M&M"}, {"nope": 1}], lambda c: "X", _ok)
    assert rows == []


def test_openalgo_lookup_is_none_outside_the_openalgo_runtime():
    # The OpenAlgo symbol master is not importable here, so the lookup must return None.
    assert bs.openalgo_bse_lookup("500325") is None
