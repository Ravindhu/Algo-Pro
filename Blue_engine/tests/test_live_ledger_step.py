"""Offline checks for the ledger step of the live path (POV slicer -> ledger).

The real ledger needs OpenAlgo, so these tests inject a fake ledger module and
drive the slicer's own recording methods. They verify what is written, and how
a ledger failure is reported (logged, not raised).
"""
import importlib
import sys

import pytest

# Skip cleanly when the live stack is not installed (same as other tests).
pytest.importorskip("openalgo")
pytest.importorskip("websocket")

from blue_engine.pov.slicers.pov import POVSlicer  # noqa: E402


class FakeLedger:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.created = []
        self.children = []

    def create_execution(self, **kw):
        if self.fail:
            raise RuntimeError("ledger down")
        self.created.append(kw)

    def record_child(self, **kw):
        if self.fail:
            raise RuntimeError("ledger down")
        self.children.append(kw)


@pytest.fixture
def ledger_stub(monkeypatch):
    def install(fake):
        core_pkg = importlib.import_module("blue_engine.core")
        monkeypatch.setattr(core_pkg, "ledger", fake, raising=False)
        monkeypatch.setitem(sys.modules, "blue_engine.core.ledger", fake)
        return fake
    return install


def _slicer(**config):
    s = POVSlicer.__new__(POVSlicer)
    s.client = None
    s.config = dict(config)
    s.strategy_label = "TEST"
    s.symbol, s.exchange = "INFY", "NSE"
    s.action, s.quantity = "BUY", 100
    s.logs = []
    s.log = lambda msg: s.logs.append(msg)
    return s


def test_successful_child_is_recorded_with_order_id(ledger_stub):
    fake = ledger_stub(FakeLedger())
    s = _slicer()
    s._record_child("exec-1", {"status": "success", "order_id": "O1",
                               "data": {"average_price": 101.5}}, 25, 101.0, "live")
    assert len(fake.children) == 1
    rec = fake.children[0]
    assert rec["execution_id"] == "exec-1"
    assert rec["order_id"] == "O1"
    assert rec["quantity"] == 25
    assert rec["status"] == "open"
    assert rec["avg_price"] == 101.5
    assert rec["assume_fill"] is False  # an ACK is not a fill by default


def test_rejected_child_is_recorded_as_rejected_without_order_id(ledger_stub):
    fake = ledger_stub(FakeLedger())
    s = _slicer()
    s._record_child("exec-2", {"status": "error", "message": "margin"}, 25, 101.0, "live")
    rec = fake.children[0]
    assert rec["status"] == "rejected"
    assert rec["order_id"] is None


def test_assume_fill_is_only_set_by_explicit_opt_in(ledger_stub):
    fake = ledger_stub(FakeLedger())
    s = _slicer(assume_fill_on_ack=True)
    s._record_child("exec-3", {"status": "success", "order_id": "O3"}, 10, 50.0, "live")
    assert fake.children[0]["assume_fill"] is True


def test_create_execution_writes_the_parent_row(ledger_stub):
    fake = ledger_stub(FakeLedger())
    s = _slicer()
    exec_id = s._create_execution_record("live")
    assert exec_id.startswith("TEST-")
    row = fake.created[0]
    assert row["execution_id"] == exec_id
    assert (row["symbol"], row["exchange"], row["action"]) == ("INFY", "NSE", "BUY")
    assert row["total_quantity"] == 100 and row["mode"] == "live"


def test_ledger_write_failure_is_logged_not_raised(ledger_stub):
    """Pins the audited gap: a failed child write is logged, trading continues.

    If the policy changes to fail closed, update this test with it.
    """
    ledger_stub(FakeLedger(fail=True))
    s = _slicer()
    s._record_child("exec-4", {"status": "success", "order_id": "O4"}, 10, 50.0, "live")
    assert any("ledger.record_child failed" in m for m in s.logs)


def test_create_failure_still_returns_an_id_not_in_the_ledger(ledger_stub):
    """Pins the audited gap: the parent row can fail to write, yet a usable id is returned."""
    ledger_stub(FakeLedger(fail=True))
    s = _slicer()
    exec_id = s._create_execution_record("live")
    assert exec_id.startswith("TEST-")
    assert any("ledger.create_execution failed" in m for m in s.logs)
