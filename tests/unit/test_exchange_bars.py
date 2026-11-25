"""Slice 4: bars routed to the exchange a stock is listed on (src/quant/data/exchange_bars.py)."""
from datetime import datetime
from types import SimpleNamespace

from quant.data.exchange_bars import ExchangeBars
from quant.execution.exchange_router import Quote, listing_exchange
from quant.execution.strategy_bot import StrategyBot

NOW = datetime(2026, 11, 25, 10, 0)


class Reader:
    def __init__(self, tag):
        self.tag, self.calls = tag, []

    def bars(self, sym, now):
        self.calls.append(("bars", sym))
        return f"{self.tag}-bars"

    def daily_stats(self, sym, now):
        self.calls.append(("stats", sym))
        return {"exchange": self.tag}


def _router(listing):
    nse, bse = Reader("NSE"), Reader("BSE")
    return ExchangeBars({"NSE": nse, "BSE": bse}, listing), nse, bse


def test_nse_listed_stock_signals_on_nse_bars():
    r, nse, bse = _router(lambda s: "NSE")
    assert r.bars("RELIANCE", NOW) == "NSE-bars"
    assert bse.calls == []


def test_bse_only_stock_signals_on_bse_bars():
    r, nse, bse = _router(lambda s: "BSE")
    assert r.bars("ABC", NOW) == "BSE-bars"
    assert nse.calls == []


def test_unknown_listing_gives_no_bars():
    r, nse, bse = _router(lambda s: None)
    assert r.bars("ZZZ", NOW) is None and r.daily_stats("ZZZ", NOW) is None
    assert nse.calls == [] and bse.calls == []


def test_explicit_exchange_reads_that_exchange_whatever_the_listing():
    r, nse, bse = _router(lambda s: "NSE")
    assert r.bars("RELIANCE", NOW, exchange="BSE") == "BSE-bars"  # a BSE position's exit checks
    assert nse.calls == []


def test_unknown_explicit_exchange_gives_no_bars():
    r, nse, bse = _router(lambda s: "NSE")
    assert r.bars("RELIANCE", NOW, exchange="MCX") is None


def test_listing_error_fails_closed():
    def boom(_s):
        raise ConnectionError("quote down")
    r, nse, bse = _router(boom)
    assert r.bars("RELIANCE", NOW) is None


def test_listing_is_checked_once_per_trading_day():
    seen = []

    def listing(s):
        seen.append(s)
        return "NSE"
    r, _, _ = _router(listing)
    r.bars("RELIANCE", NOW)
    r.daily_stats("RELIANCE", NOW)
    assert seen == ["RELIANCE"]
    r.bars("RELIANCE", datetime(2026, 11, 26, 10, 0))  # next day: checked again
    assert seen == ["RELIANCE", "RELIANCE"]


def test_daily_stats_routed_like_bars():
    r, nse, bse = _router(lambda s: "BSE")
    assert r.daily_stats("ABC", NOW) == {"exchange": "BSE"}


def test_listing_exchange_rule():
    assert listing_exchange(Quote(999.0, 1000.0), Quote(998.0, 999.0)) == "NSE"
    assert listing_exchange(Quote(None, None), Quote(198.0, 200.0)) == "BSE"
    assert listing_exchange(None, None) is None
    assert listing_exchange(Quote(0.0, 0.0), Quote(None, None)) is None


def test_bot_read_bars_keeps_the_nse_call_unchanged():
    calls = []

    class Bars:
        def bars(self, sym, now, **kw):
            calls.append((sym, kw))
            return "df"
    stub = SimpleNamespace(_bars=Bars())
    assert StrategyBot._read_bars(stub, "RELIANCE", NOW, "NSE") == "df"
    assert StrategyBot._read_bars(stub, "ABC", NOW, "BSE") == "df"
    assert calls == [("RELIANCE", {}), ("ABC", {"exchange": "BSE"})]
