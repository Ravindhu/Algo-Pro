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
    """Reset the latching kill-switch before and after every test."""
    from blue_engine.portfolio import optim_policy as op

    op._PROCESS_KILL_SWITCH = None          # fresh, untripped switch
    yield
    op._PROCESS_KILL_SWITCH = None          # never leak a trip between tests
