"""
tests/test_kill_switch_persistence.py — durable kill-switch across restarts.

Pins the contract wired in audit F4 / WIRE_PNL follow-up:

1. A trip is persisted exactly once, on the transition (evaluate()).
2. Simulated restart (fresh process state, same sqlite file): the switch
   loads TRIPPED and every recovery probe stays halted — no auto-resume.
3. Only a named operator reset clears it — and the clear is persisted too,
   so a subsequent restart comes up ARMED.
4. Fail-soft: a broken store degrades to in-memory-only, never raises, and
   the switch keeps working.

Run (no pytest required)::

    python tests\\test_kill_switch_persistence.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.core import kill_switch_store as ks_store  # noqa: E402
from blue_engine.portfolio import optim_policy as op  # noqa: E402

_INPUTS = {"universe": ["A", "B"], "n_obs": 300,
           "cov": [[0.02, 0.004], [0.004, 0.03]]}


class KillSwitchPersistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["BLUE_KILL_SWITCH_DB_PATH"] = os.path.join(
            self._tmpdir.name, "kill_switch.db")
        ks_store.close_store()
        ks_store._DEGRADED = False
        op._PROCESS_KILL_SWITCH = None

    def tearDown(self):
        ks_store.close_store()
        ks_store._DEGRADED = False
        op._PROCESS_KILL_SWITCH = None
        os.environ.pop("BLUE_KILL_SWITCH_DB_PATH", None)
        self._tmpdir.cleanup()

    # -- 1. trip is persisted ------------------------------------------- #
    def test_trip_persists_state(self):
        out = op.decide(_INPUTS, drawdown=0.20)
        self.assertFalse(out["trade"])
        state = ks_store.load_state()
        self.assertTrue(state["tripped"])
        self.assertIn("drawdown", state["reason"])

    # -- 2. trip survives a simulated restart --------------------------- #
    def test_trip_survives_restart_and_blocks_recovery_probe(self):
        op.decide(_INPUTS, drawdown=0.20)                 # trip + persist
        # "Crash": wipe all process state, keep the sqlite file.
        ks_store.close_store()
        op._PROCESS_KILL_SWITCH = None
        # Recovery probe: everything else looks fine.
        out = op.decide(_INPUTS, drawdown=0.01)
        self.assertFalse(out["trade"],
                         "a persisted trip MUST halt the fresh process")
        self.assertIn("persisted", out["reasons"][0])
        self.assertEqual(out["action"], "EXIT_TO_CASH")
        # Anonymous reset refused even after the restart.
        self.assertFalse(op.reset_kill_switch()["ok"])
        # Named operator reset re-arms.
        self.assertTrue(op.reset_kill_switch(confirm="ops-1")["ok"])
        out = op.decide(_INPUTS, drawdown=0.01)
        self.assertTrue(out["trade"])

    # -- 3. the clear is persisted too ---------------------------------- #
    def test_reset_clears_persisted_state(self):
        op.decide(_INPUTS, drawdown=0.20)                 # trip + persist
        op._PROCESS_KILL_SWITCH = None                    # "restart"
        op._process_kill_switch()                         # loads tripped
        op.reset_kill_switch(confirm="ops-2")
        # Another "restart": must NOT come back tripped.
        ks_store.close_store()
        op._PROCESS_KILL_SWITCH = None
        state = ks_store.load_state()
        self.assertFalse(state["tripped"])
        out = op.decide(_INPUTS, drawdown=0.01)
        self.assertTrue(out["trade"])

    # -- 4. fail-soft on a broken store --------------------------------- #
    def test_broken_store_degrades_never_raises(self):
        os.environ["BLUE_KILL_SWITCH_DB_PATH"] = "Z:\\no\\such\\dir\\x.db"
        ks_store.close_store()
        ks_store._DEGRADED = False
        op._PROCESS_KILL_SWITCH = None
        # The switch keeps working (in-memory-only) even though the store
        # cannot persist. In-memory latching still blocks this session.
        out = op.decide(_INPUTS, drawdown=0.20)
        self.assertFalse(out["trade"])
        self.assertFalse(ks_store.store_available())


def _run_all() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(KillSwitchPersistenceTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
