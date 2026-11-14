"""Bulk/block deals: source adapter fallback, storage, and screen lifetime.

The feature's contract is in the retention rule: a deal survives its symbol
leaving the screener. These tests pin that, plus the honest source reporting that
stops a result being attributed to a source that never answered.
"""
import json
from datetime import date

import pytest

from quant.data import nse_deals, nse_mcp_client


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload


BLOCK = {"data": [{"symbol": "SBIN", "clientName": "ABC FUND", "buySell": "BUY",
                   "quantity": "1,00,000", "price": "800.50"},
                  {"symbol": "NOTSCREENED", "clientName": "X", "buySell": "SELL",
                   "quantity": "500", "price": "10"}]}
BULK = {"data": [{"symbol": "TCS", "clientName": "DEF FUND", "buySell": "SELL",
                  "quantity": "25,000", "price": "3,900.00"}]}


@pytest.fixture()
def store(tmp_path):
    return nse_deals.DealsStore(tmp_path / "deals.sqlite")


# --------------------------------------------------------------------------- #
# adapter: source reporting and fallback
# --------------------------------------------------------------------------- #
def test_available_sources_always_includes_nse_json():
    assert "nse_json" in nse_mcp_client.available_sources()


def test_best_source_prefers_an_available_choice():
    src = nse_mcp_client.best_source("nse_mcp")
    assert src in nse_mcp_client.available_sources()


def test_block_deals_reports_which_source_answered():
    out = nse_mcp_client.fetch_block_deals(http_get=lambda *a, **k: _Resp(BLOCK))
    assert out["ok"] is True
    assert out["source"] in nse_mcp_client.available_sources()
    assert out["rows"][0]["symbol"] == "SBIN"


def test_block_deal_quantity_is_parsed_not_string():
    out = nse_mcp_client.fetch_block_deals(http_get=lambda *a, **k: _Resp(BLOCK))
    assert out["rows"][0]["quantity"] == pytest.approx(100000.0)


def test_deal_without_quantity_is_skipped():
    payload = {"data": [{"symbol": "X", "clientName": "A", "price": "1"}]}
    out = nse_mcp_client.fetch_block_deals(http_get=lambda *a, **k: _Resp(payload))
    assert out["rows"] == []


def test_deal_without_symbol_is_skipped():
    payload = {"data": [{"clientName": "A", "quantity": "5", "price": "1"}]}
    out = nse_mcp_client.fetch_block_deals(http_get=lambda *a, **k: _Resp(payload))
    assert out["rows"] == []


def test_forbidden_response_fails_with_a_reason():
    out = nse_mcp_client.fetch_block_deals(http_get=lambda *a, **k: _Resp({}, 403))
    assert out["ok"] is False
    assert "403" in out["detail"] or "failed" in out["detail"]


def test_network_error_never_raises():
    def boom(*a, **k):
        raise ConnectionError("down")
    out = nse_mcp_client.fetch_block_deals(http_get=boom)
    assert out["ok"] is False and "rows" in out


def test_bulk_deal_falls_back_to_json():
    out = nse_mcp_client.fetch_bulk_deals(http_get=lambda *a, **k: _Resp(BULK))
    assert out["rows"][0]["symbol"] == "TCS"
    assert out["rows"][0]["deal_type"] == "bulk"


def test_screened_symbols_empty_when_no_screen(tmp_path):
    assert nse_mcp_client._screened_symbols(tmp_path / "nope.json") == []


def test_screened_symbols_reads_the_writer_shape(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps({"as_of": "2026-11-14",
                             "symbols": [{"symbol": "sbin"}, {"symbol": "TCS"}]}))
    assert nse_mcp_client._screened_symbols(p) == ["SBIN", "TCS"]


def test_screened_symbols_accepts_a_bare_list(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(["sbin", "tcs"]))
    assert nse_mcp_client._screened_symbols(p) == ["SBIN", "TCS"]


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def test_save_is_idempotent(store):
    row = {"trade_date": "2026-11-14", "deal_type": "bulk", "symbol": "SBIN",
           "quantity": 1000, "price": 800.0}
    assert store.save([row], source="nse_json") == {"inserted": 1, "existing": 0}
    assert store.save([row], source="nse_json") == {"inserted": 0, "existing": 1}


def test_two_deals_for_one_client_are_both_kept(store):
    """Different size at a different price is a different deal."""
    base = {"trade_date": "2026-11-14", "deal_type": "bulk", "symbol": "SBIN",
            "client_name": "A FUND"}
    store.save([{**base, "quantity": 1000, "price": 800.0},
                {**base, "quantity": 2000, "price": 801.0}], source="nse_json")
    assert len(store.all_deals("SBIN")) == 2


def test_all_deals_filters_by_symbol(store):
    store.save([{"trade_date": "2026-11-14", "deal_type": "bulk", "symbol": "SBIN",
                 "quantity": 1, "price": 1.0},
                {"trade_date": "2026-11-14", "deal_type": "bulk", "symbol": "TCS",
                 "quantity": 1, "price": 1.0}], source="nse_json")
    assert len(store.all_deals("SBIN")) == 1


# --------------------------------------------------------------------------- #
# screen lifetime: the core contract
# --------------------------------------------------------------------------- #
def test_membership_adds_new_symbols(store):
    assert store.sync_membership(["SBIN", "TCS"])["added"] == 2


def test_symbol_leaving_the_screen_marks_its_deals_removed(store):
    store.save([{"trade_date": "2026-11-14", "deal_type": "bulk", "symbol": "TCS",
                 "quantity": 10, "price": 5.0}], source="nse_json")
    store.sync_membership(["TCS", "SBIN"])
    store.sync_membership(["SBIN"])
    assert store.active_deals("TCS") == []
    # The history is retained, only marked.
    assert len(store.all_deals("TCS")) == 1
    assert store.all_deals("TCS")[0]["removed_at"] is not None


def test_removal_is_never_a_delete(store):
    store.save([{"trade_date": "2026-11-14", "deal_type": "block", "symbol": "TCS",
                 "quantity": 10, "price": 5.0}], source="nse_json")
    store.sync_membership(["TCS"])
    store.sync_membership([])
    assert len(store.all_deals()) == 1
    assert store.summary()["removed_deals"] == 1


def test_symbol_returning_to_the_screen_reopens_its_deals(store):
    store.save([{"trade_date": "2026-11-14", "deal_type": "bulk", "symbol": "TCS",
                 "quantity": 10, "price": 5.0}], source="nse_json")
    store.sync_membership(["TCS"])
    store.sync_membership(["SBIN"])          # TCS drops out
    counts = store.sync_membership(["TCS", "SBIN"])  # TCS comes back
    assert counts["reopened"] == 1
    assert len(store.active_deals("TCS")) == 1


def test_still_in_does_not_touch_removed_at(store):
    store.sync_membership(["SBIN"])
    assert store.sync_membership(["SBIN"])["still_in"] == 1
    assert store.membership()[0]["removed_at"] is None


def test_summary_distinguishes_active_from_retained(store):
    store.save([{"trade_date": "2026-11-14", "deal_type": "bulk", "symbol": "A",
                 "quantity": 1, "price": 1.0},
                {"trade_date": "2026-11-14", "deal_type": "block", "symbol": "B",
                 "quantity": 1, "price": 1.0}], source="nse_json")
    store.sync_membership(["A", "B"])
    store.sync_membership(["A"])
    s = store.summary()
    assert s["total_deals"] == 2
    assert s["active_deals"] == 1
    assert s["removed_deals"] == 1


# --------------------------------------------------------------------------- #
# fetch_and_store orchestration
# --------------------------------------------------------------------------- #
def test_empty_screen_fetches_nothing(store):
    report = nse_deals.fetch_and_store(store, symbols=[])
    assert report["ok"] is False
    assert report["symbols"] == 0
    assert "nothing fetched" in report["detail"]


def test_only_screened_symbols_are_stored(store):
    """A deal for a symbol we do not screen must not be stored."""
    def getter(url, *a, **k):
        return _Resp(BLOCK if "block" in url else BULK)

    report = nse_deals.fetch_and_store(store, symbols=["SBIN"], http_get=getter)
    assert report["block"]["found"] == 2
    assert report["block"]["stored"] == 1        # NOTSCREENED dropped
    assert [d["symbol"] for d in store.all_deals()] == ["SBIN"]


def test_report_names_the_source_per_deal_type(store):
    def getter(url, *a, **k):
        return _Resp(BLOCK if "block" in url else BULK)

    report = nse_deals.fetch_and_store(store, symbols=["SBIN", "TCS"], http_get=getter)
    assert report["block"]["source"] in nse_mcp_client.available_sources()
    assert report["bulk"]["source"] in nse_mcp_client.available_sources()


def test_both_sources_failing_is_reported(store):
    def boom(*a, **k):
        raise ConnectionError("down")
    report = nse_deals.fetch_and_store(store, symbols=["SBIN"], http_get=boom)
    assert report["ok"] is False
    assert store.all_deals() == []
