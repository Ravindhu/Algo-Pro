"""Slice 6: dry-run decision is pure, reads only, and cannot reach order code."""
import pathlib

from quant.execution.exchange_dry_run import decide, run
from quant.execution.exchange_router import Quote

ROOT = pathlib.Path(__file__).resolve().parents[2]


def ok(bid, ask):
    return {"status": "success", "data": {"bid": bid, "ask": ask}}


def test_nse_chosen_when_bse_not_cheaper_enough():
    row = decide("INFY", ok(99, 100), ok(99.99, 99.99 * 1.0))  # BSE 99.99 is only 0.01% cheaper: below the 0.05% edge
    assert row["entry_exchange"] == "NSE" and row["buy_price"] == 100.0
    assert row["listing"] == "NSE" and row["would_trade"] is True


def test_bse_chosen_when_at_least_five_bps_cheaper():
    row = decide("500325", ok(99, 100), ok(99.4, 99.9))  # 0.1% cheaper
    assert row["entry_exchange"] == "BSE" and row["buy_price"] == 99.9
    assert row["bse_entry_refused_by_gate"] is True  # gate still refuses BSE entries


def test_bse_only_stock_listed_on_bse():
    row = decide("500209", {"status": "error"}, ok(50, 50.5))
    assert row["listing"] == "BSE" and row["entry_exchange"] == "BSE"


def test_no_quote_means_no_trade():
    row = decide("XYZ", {"status": "error"}, None)
    assert row["would_trade"] is False and row["entry_exchange"] is None
    assert row["exit_exchange"] is None and row["listing"] is None


def test_exit_uses_best_bid_on_entry_exchange():
    row = decide("INFY", ok(99, 100), ok(98, 100.5))
    assert row["exit_exchange"] == "NSE" and row["exit_price"] == 99.0


def test_crossed_quote_is_unusable():
    row = decide("INFY", ok(101, 100), ok(99, 99.5))  # NSE bid > ask: ignored
    assert row["entry_exchange"] == "BSE" and row["listing"] == "BSE"


class _Reader:
    def __init__(self, data):
        self.data = data

    def quotes(self, api_key, symbol, exchange):
        value = self.data.get((symbol, exchange))
        if isinstance(value, Exception):
            raise value
        return value


def test_run_fails_closed_when_read_raises():
    reader = _Reader({("INFY", "NSE"): RuntimeError("down"), ("INFY", "BSE"): ok(99, 100)})
    rows = run(["INFY"], reader, "key")
    assert rows[0]["entry_exchange"] == "BSE"  # NSE failure treated as no quote
    assert rows[0]["listing"] == "BSE"


def test_run_with_no_reads_returns_no_trade():
    rows = run(["AAA"], _Reader({}), "key")
    assert rows[0]["would_trade"] is False


def test_run_reads_only_quotes_and_lot_lookup():
    calls = []

    class Spy(_Reader):
        def quotes(self, api_key, symbol, exchange):
            calls.append(("quotes", symbol, exchange))
            return ok(99, 100)

    run(["INFY"], Spy({}), "key", lot_reader=lambda k, s: calls.append(("lot", s)) or 25)
    assert {c[0] for c in calls} == {"quotes", "lot"}


def test_module_and_script_reference_no_order_code():
    forbidden = ("placeorder", "place_order", "place_child", "place_stop", "cancel", "modify", "close_position")
    for path in (ROOT / "src/quant/execution/exchange_dry_run.py", ROOT / "scripts/ops/dry_run_exchange.py"):
        text = path.read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text, f"{word} found in {path.name}"


def test_quote_type_is_frozen_value():
    assert Quote(1.0, 2.0) == Quote(1.0, 2.0)
