"""
tests/test_ledger_store.py — durable sqlite ledger mirror (audit F3).

Pins the contract of blue_engine.core.ledger_store and its wiring into
blue_engine.core.ledger:

1. store/fetch round-trip preserves the snapshot exactly.
2. A row persisted BEFORE a simulated crash is restored AFTER the wipe:
   ``recover_filled_quantity`` returns the durable fill count, not 0.
3. Fills recorded after a restart via a late order-update event find the
   restored row and never double-count (idempotent max semantics).
4. Fail-soft: a broken DB path degrades to in-memory-only, never raises.
5. The store never invents state: an unknown execution id stays unknown.

Run (no pytest required)::

    python tests\\test_ledger_store.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.core import ledger, ledger_store  # noqa: E402


class LedgerStoreTest(unittest.TestCase):
    def setUp(self):
        # Isolated sqlite file per test.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "ledger_store.db"
        os.environ["BLUE_LEDGER_DB_PATH"] = str(self.db_path)
        ledger_store.close_store()
        ledger_store._DEGRADED = False  # reset degrade flag between tests
        ledger._IN_MEMORY_EXECUTIONS.clear()

    def tearDown(self):
        ledger_store.close_store()
        ledger._IN_MEMORY_EXECUTIONS.clear()
        os.environ.pop("BLUE_LEDGER_DB_PATH", None)
        self._tmpdir.cleanup()

    # -- 1. round-trip -------------------------------------------------- #
    def test_store_fetch_roundtrip(self):
        eid = "LS-RT-0001"
        ledger.create_execution(execution_id=eid, total_quantity=500)
        self.assertTrue(ledger_store.store_available())
        snap = ledger_store.fetch_row(eid)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["request_data"].get("total_quantity"), 500)

    # -- 2. crash recovery from durable state --------------------------- #
    def test_fill_survives_simulated_crash(self):
        eid = "LS-CRASH-0001"
        ledger.create_execution(execution_id=eid, total_quantity=300)
        ledger.claim_child(eid, 100, 1000.0, "accumulating")
        ledger.finish_child(eid, 1, success=True, order_id="LS-ORD1")
        ledger.record_fill("LS-ORD1", 100, "complete")
        self.assertEqual(ledger.recover_filled_quantity(eid), 100)

        # Simulated crash: wipe process memory, keep the sqlite file.
        ledger._IN_MEMORY_EXECUTIONS.clear()
        self.assertEqual(ledger.recover_filled_quantity(eid), 100,
                         "durable fill must survive the restart")
        row = ledger.get_execution(eid)
        self.assertIsNotNone(row)
        self.assertEqual(row.filled_quantity, 100)

    # -- 3. late fill after restart, idempotent ------------------------- #
    def test_late_fill_after_restart_never_double_counts(self):
        eid = "LS-LATE-0001"
        ledger.create_execution(execution_id=eid, total_quantity=300)
        ledger.claim_child(eid, 100, 1000.0, "accumulating")
        ledger.finish_child(eid, 1, success=True, order_id="LS-ORD2")
        ledger.record_fill("LS-ORD2", 60, "open")

        ledger._IN_MEMORY_EXECUTIONS.clear()  # crash
        # Late event re-reports the SAME quantity: must stay 60, not 120.
        ledger.record_fill("LS-ORD2", 60, "complete")
        row = ledger.get_execution(eid)
        self.assertEqual(row.filled_quantity, 60)
        # A genuine partial fill increase does update the durable count.
        ledger.record_fill("LS-ORD2", 100, "complete")
        self.assertEqual(ledger.get_execution(eid).filled_quantity, 100)

    # -- 4. fail-soft on a broken store --------------------------------- #
    def test_broken_store_degrades_never_raises(self):
        # Point the store at an impossible path, then force re-init.
        # A path whose parent is a regular FILE can never be created, on any
        # OS (a Windows drive path is just a relative file name on Linux).
        blocker = os.path.join(self._tmpdir.name, "not_a_dir")
        open(blocker, "w").close()
        os.environ["BLUE_LEDGER_DB_PATH"] = os.path.join(blocker, "x.db")
        ledger_store.close_store()
        ledger_store._DEGRADED = False
        ledger._IN_MEMORY_EXECUTIONS.clear()
        eid = "LS-BROKEN-0001"
        # The ledger keeps working in memory even though the store failed.
        ledger.create_execution(execution_id=eid, total_quantity=100)
        self.assertFalse(ledger_store.store_available())
        self.assertEqual(ledger.recover_filled_quantity(eid), 0)
        # And the store reports degraded rather than raising.

    # -- 5. no invented state ------------------------------------------- #
    def test_unknown_execution_stays_unknown(self):
        self.assertIsNone(ledger_store.fetch_row("LS-NONE-0001"))
        self.assertEqual(ledger.recover_filled_quantity("LS-NONE-0001"), 0)
        self.assertIsNone(ledger.get_execution("LS-NONE-0001"))


def _run_all() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(LedgerStoreTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
