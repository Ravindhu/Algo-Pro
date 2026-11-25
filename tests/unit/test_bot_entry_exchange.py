"""Slice 2: the bot's entry exchange decision (StrategyBot._entry_exchange). Read only.

The full _gate_entry path needs the whole bot and is covered by the owner dry run.
This file checks the quote decision the gate relies on.
"""
from types import SimpleNamespace

from quant.execution.strategy_bot import StrategyBot


def _reply(bid, ask):
    return {"status": "success", "data": {"bid": bid, "ask": ask}}


def _bot_like(quotes):
    """A stand-in holding only the quote source the decision uses."""
    def provider(sym, exchange):
        value = quotes.get(exchange)
        if isinstance(value, Exception):
            raise value
        return value
    return SimpleNamespace(_quote_provider=provider)


def _choose(quotes):
    return StrategyBot._entry_exchange(_bot_like(quotes), "RELIANCE")


def test_nse_chosen_when_bse_not_0_05_percent_cheaper():
    c = _choose({"NSE": _reply(999.0, 1000.0), "BSE": _reply(998.0, 1000.0)})
    assert c.exchange == "NSE" and c.price == 1000.0


def test_bse_chosen_when_0_05_percent_cheaper():
    c = _choose({"NSE": _reply(999.0, 1000.0), "BSE": _reply(998.0, 1000.0 * (1 - 0.0005))})
    assert c.exchange == "BSE"


def test_bse_only_stock_is_bse():
    c = _choose({"NSE": _reply(None, None), "BSE": _reply(199.0, 200.0)})
    assert c.exchange == "BSE"


def test_no_usable_quote_is_no_trade():
    c = _choose({"NSE": {"status": "error"}, "BSE": None})
    assert c.exchange is None


def test_quote_source_error_is_treated_as_missing_not_raised():
    c = _choose({"NSE": ConnectionError("down"), "BSE": _reply(199.0, 200.0)})
    assert c.exchange == "BSE"  # NSE unusable; BSE used
    c2 = _choose({"NSE": ConnectionError("down"), "BSE": ConnectionError("down")})
    assert c2.exchange is None
