"""
conftest.py — pytest fixtures shared across the Blue_engine test suite.

Isolation for the process-level latching kill-switch
----------------------------------------------------
``optim_policy.decide()`` deliberately uses ONE latching KillSwitch per
process, so a tripped halt survives restarts (see tests/test_recovery.py).
That is correct in production and poisonous in a test suite: one test that
trips the switch would silently halt every later test that expects trading.

This autouse fixture re-arms the switch around every test, so each test
starts from a clean slate while the production behaviour is unchanged.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_process_kill_switch():
    """Reset the latching kill-switch before and after every test.

    Also isolates the DURABLE kill-switch store per test (kill-switch
    persistence now survives restarts, so a test that trips the switch must
    not leak a persisted trip into the next test's process-level reload).
    """
    import os
    import tempfile
    from blue_engine.core import kill_switch_store as ks_store
    from blue_engine.portfolio import optim_policy as op

    tmp = tempfile.TemporaryDirectory()
    os.environ["BLUE_KILL_SWITCH_DB_PATH"] = str(
        os.path.join(tmp.name, "kill_switch.db"))
    ks_store.close_store()
    ks_store._DEGRADED = False

    op._PROCESS_KILL_SWITCH = None          # fresh, untripped switch
    yield
    op._PROCESS_KILL_SWITCH = None          # never leak a trip between tests

    ks_store.close_store()
    ks_store._DEGRADED = False
    os.environ.pop("BLUE_KILL_SWITCH_DB_PATH", None)
    tmp.cleanup()

# ---------------------------------------------------------------------------
# Audit F2 (2026-09-29): legacy-layout orphan test exclusion.
#
# portfolio/tests/test_risk_models.py imports ``portfolio.risk_models``
# (a pre-rename top-level package layout that no longer exists anywhere in
# the repo: neither the module nor its classes — LedoitWolfShrinkage,
# RiskMetrics, RiskModelConfig — exist under blue_engine). Collecting it
# aborts the WHOLE suite with "attempted relative import beyond top-level
# package", hiding all real results. Excluded here with a named reason; the
# file must be either rewritten against blue_engine.portfolio.risk.* or
# deleted. Until then it stays out of regression runs.
# ---------------------------------------------------------------------------
collect_ignore_glob = ["../portfolio/tests/test_risk_models.py"]
