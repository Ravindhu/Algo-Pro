"""The five flow-node wraps: execution ledger, PIT joins, protection, session
windows, feed health.

Every one of these fails CLOSED or degrades visibly. These tests pin that, because
a wrapper that quietly returns False or [] when its engine module is missing is
indistinguishable from a healthy system with nothing to report.
"""
import os
import tempfile
from datetime import datetime

import pytest

# Point the engine's runtime stores at a temp dir before anything imports them.
os.environ.setdefault("BLUE_RUNTIME_DATA_ROOT", tempfile.mkdtemp(prefix="flow-node-"))

from quant.data import feature_store_pit as pit          # noqa: E402
from quant.execution import execution_ledger as el      # noqa: E402
from quant.execution import protection_wrap as pw       # noqa: E402
from quant.monitor.feed_health import FeedHealthBook    # noqa: E402
from quant.risk import session_windows as sw            # noqa: E402


# --------------------------------------------------------------------------- #
# Node 1: execution ledger
# --------------------------------------------------------------------------- #
def test_ledger_is_available_and_records_a_lifecycle():
    assert el.available() is True
    eid = el.new_execution_id("test")
    assert eid
    el.create_execution(eid, request_data={"symbol": "X", "slippage_window_fills": 5})
    el.update_vwap(eid, price=100.0, volume=10)
    assert el.get_vwap(eid) == pytest.approx(100.0)


def test_ledger_vwap_averages_two_fills():
    eid = el.new_execution_id("vwap")
    el.create_execution(eid, request_data={"symbol": "Y"})
    el.update_vwap(eid, price=100.0, volume=10)
    el.update_vwap(eid, price=102.0, volume=10)
    assert el.get_vwap(eid) == pytest.approx(101.0)


def test_ledger_report_has_a_stable_shape():
    eid = el.new_execution_id("rep")
    el.create_execution(eid, request_data={"symbol": "Z"})
    rep = el.slippage_report(eid)
    assert set(rep) == {"available", "execution_id", "vwap", "arrival_vwap",
                        "slippage_bps", "samples", "detail"}
    assert rep["available"] is True


def test_ledger_unknown_execution_does_not_raise():
    assert el.get_vwap("does-not-exist") is None
    assert el.get_execution("does-not-exist") in (None, {})


def test_recovered_quantity_is_zero_when_unknown():
    """Zero is the safe answer: a re-send is caught by the recon gate."""
    assert el.recovered_filled_quantity("nope") == 0


# --------------------------------------------------------------------------- #
# Node 2: point-in-time join
# --------------------------------------------------------------------------- #
PRICES = [{"as_of_ts": "2026-09-12T15:30:00+05:30", "close": 100.0}]
CPI = [{"as_of_ts": "2026-09-12T18:40:00+05:30", "value": 4.82},
       {"as_of_ts": "2026-08-12T18:40:00+05:30", "value": 4.75}]


def test_asof_join_never_uses_a_future_release():
    """The 18:40 release is after the 15:30 close, so it must not be used."""
    out = pit.asof_join(PRICES, CPI, columns=["value"], prefix="cpi_",
                        max_staleness_days=None)
    assert out[0]["cpi_value"] == 4.75


def test_asof_join_staleness_fence_falls_back_to_default():
    """A value older than the fence is unknown, not carried forward."""
    old = [{"as_of_ts": "2025-01-01T00:00:00+05:30", "value": 99.0}]
    out = pit.asof_join(PRICES, old, columns=["value"], prefix="cpi_",
                        max_staleness_days=30)
    assert out[0]["cpi_value"] is None


def test_asof_join_refuses_fabricated_zero_default():
    with pytest.raises(ValueError, match="fabricates"):
        pit.asof_join(PRICES, CPI, columns=["value"], default=0)


def test_join_report_shows_coverage():
    out = pit.asof_join(PRICES, [], columns=["value"], prefix="cpi_")
    rep = pit.join_report(out, ["cpi_value"])
    assert rep["rows"] == 1
    assert rep["columns"]["cpi_value"]["coverage"] == 0.0
    assert rep["all_empty"] is True


def test_join_report_flags_a_dead_join():
    """A join that fills nothing is a common, invisible failure. Name it."""
    out = pit.asof_join(PRICES, [], columns=["value"], prefix="cpi_")
    assert pit.join_report(out, ["cpi_value"])["all_empty"] is True


# --------------------------------------------------------------------------- #
# Node 3: protective orders
# --------------------------------------------------------------------------- #
def test_register_and_read_protection():
    assert pw.register_levels("E1", symbol="X", exchange="NSE", action="BUY",
                              stop_loss=100.0, target=120.0) is True
    assert pw.is_protected("E1", "X") is True
    assert pw.get_levels("E1", "X")["target"] == 120.0


def test_unregistered_position_is_unprotected():
    """No recorded stop is an ALERT, never an assumed default."""
    assert pw.is_protected("E9", "NOPE") is False


def test_stop_only_ratchets_towards_profit():
    pw.register_levels("E2", symbol="Y", exchange="NSE", action="BUY",
                       stop_loss=100.0, target=130.0)
    pw.ratchet_stop("E2", "Y", stop_loss=90.0)     # loosening must be ignored
    assert pw.get_levels("E2", "Y")["stop_loss"] == 100.0
    pw.ratchet_stop("E2", "Y", stop_loss=110.0)    # tightening must apply
    assert pw.get_levels("E2", "Y")["stop_loss"] == 110.0


def test_clear_drops_the_record():
    pw.register_levels("E3", symbol="Z", exchange="NSE", action="BUY",
                       stop_loss=50.0, target=60.0)
    pw.clear_levels("E3", "Z")
    assert pw.get_levels("E3", "Z") is None


def test_snapshot_is_a_list():
    assert isinstance(pw.levels_snapshot(), list)


# --------------------------------------------------------------------------- #
# Node 4: session windows
# --------------------------------------------------------------------------- #
def test_cash_default_does_not_block_everything():
    """The engine default is the SME all-day window; cash must not inherit it."""
    blocked, reason = sw.blocks_entry(datetime(2026, 11, 14, 11, 0))
    assert blocked is False
    assert reason == "cash_session_no_auction"


def test_sme_default_blocks_the_whole_session():
    blocked, reason = sw.blocks_entry(datetime(2026, 11, 14, 11, 0), is_sme=True)
    assert blocked is True
    assert reason == "in_auction"


def test_explicit_window_blocks_inside():
    blocked, reason = sw.blocks_entry(datetime(2026, 11, 14, 9, 20),
                                      windows="09:15-09:45")
    assert blocked is True and reason == "in_auction"


def test_entry_buffer_blocks_just_before_a_window():
    blocked, reason = sw.blocks_entry(datetime(2026, 11, 14, 9, 14),
                                      windows="09:15-09:45", buffer_min=2)
    assert blocked is True
    assert "approaching" in reason


def test_unparseable_schedule_fails_closed():
    blocked, reason = sw.blocks_entry(datetime(2026, 11, 14, 11, 0), windows="nonsense")
    assert blocked is True
    assert reason == "schedule_unknown"


def test_window_outside_session_is_not_blocked():
    blocked, _ = sw.blocks_entry(datetime(2026, 11, 14, 14, 0),
                                 windows="09:15-09:45")
    assert blocked is False


# --------------------------------------------------------------------------- #
# Node 5: feed health
# --------------------------------------------------------------------------- #
def test_feed_latches_paused_at_threshold():
    book = FeedHealthBook(threshold=3)
    assert book.record_failure("A", "stale") is False
    assert book.record_failure("A", "stale") is False
    assert book.record_failure("A", "stale") is True     # third crosses
    assert book.is_paused("A") is True


def test_success_clears_the_latch():
    book = FeedHealthBook(threshold=2)
    book.record_failure("B", "stale")
    book.record_failure("B", "stale")
    assert book.is_paused("B") is True
    book.record_success("B")
    assert book.is_paused("B") is False


def test_paused_feed_blocks_entries():
    book = FeedHealthBook(threshold=1)
    book.record_failure("C", "dead")
    guard = book.blocks_entries()
    assert guard["blocked"] is True
    assert "C" in guard["paused_symbols"]


def test_healthy_feed_does_not_block():
    book = FeedHealthBook(threshold=3)
    book.record_success("D")
    assert book.blocks_entries()["blocked"] is False


def test_snapshot_shape():
    book = FeedHealthBook(threshold=2)
    book.record_failure("E", "x")
    snap = book.snapshot()
    assert set(snap) >= {"available", "threshold", "tracked", "paused", "symbols", "detail"}
    assert snap["tracked"] == 1
