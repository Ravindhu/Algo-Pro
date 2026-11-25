"""Exchange choice for entries and exit price (src/quant/execution/exchange_router.py)."""
import pytest

from quant.execution import exchange_router as er
from quant.execution.exchange_router import Quote


def test_bse_chosen_when_exactly_at_the_0_05_percent_edge():
    nse = Quote(bid=999.0, ask=1000.0)
    bse = Quote(bid=998.0, ask=1000.0 * (1 - 0.0005))  # 0.05% cheaper
    c = er.choose_entry(nse, bse)
    assert c.exchange == "BSE" and c.price == pytest.approx(999.5)


def test_nse_kept_when_bse_is_less_than_0_05_percent_cheaper():
    nse = Quote(bid=999.0, ask=1000.0)
    bse = Quote(bid=998.0, ask=1000.0 * (1 - 0.0004))
    c = er.choose_entry(nse, bse)
    assert c.exchange == "NSE" and c.price == 1000.0


def test_nse_kept_when_bse_is_dearer():
    c = er.choose_entry(Quote(999.0, 1000.0), Quote(999.0, 1001.0))
    assert c.exchange == "NSE" and c.price == 1000.0


def test_bse_only_stock_trades_on_bse():
    c = er.choose_entry(None, Quote(199.0, 200.0))
    assert c.exchange == "BSE" and c.price == 200.0


def test_missing_bse_quote_uses_nse():
    c = er.choose_entry(Quote(999.0, 1000.0), None)
    assert c.exchange == "NSE" and c.price == 1000.0


def test_no_quote_on_either_exchange_does_not_trade():
    c = er.choose_entry(None, None)
    assert c.exchange is None and c.price is None


@pytest.mark.parametrize("bad", [
    Quote(bid=None, ask=None),
    Quote(bid=0.0, ask=0.0),
    Quote(bid=-1.0, ask=10.0),
    Quote(bid=float("nan"), ask=10.0),
    Quote(bid=float("inf"), ask=10.0),
    Quote(bid=True, ask=10.0),
    Quote(bid=11.0, ask=10.0),  # crossed
])
def test_bad_quotes_are_never_used(bad):
    c = er.choose_entry(bad, Quote(bid=99.0, ask=100.0))
    assert c.exchange == "BSE" and c.price == 100.0  # NSE unusable, BSE used
    both_bad = er.choose_entry(bad, bad)
    assert both_bad.exchange is None


def test_exit_sells_at_best_bid_on_the_held_exchange():
    quotes = {"NSE": Quote(bid=999.0, ask=1000.0), "BSE": Quote(bid=995.0, ask=996.0)}
    c = er.exit_price("BSE", quotes)
    assert c.exchange == "BSE" and c.price == 995.0


def test_exit_never_switches_exchange_when_held_side_has_no_bid():
    quotes = {"NSE": Quote(bid=999.0, ask=1000.0), "BSE": None}
    c = er.exit_price("BSE", quotes)
    assert c.exchange is None and c.price is None


def test_exit_rejects_unknown_exchange():
    c = er.exit_price("MCX", {})
    assert c.exchange is None
