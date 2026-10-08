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

import os
import platform

# ---------------------------------------------------------------------------
# Windows platform-probe guard (suite-wide).
#
# The autouse fixture below imports ``blue_engine`` -> openalgo -> pandas.
# pandas' import calls ``platform.machine()`` -> ``platform.win32_ver()`` ->
# ``subprocess.check_output("cmd /c ver")``, which NEVER RETURNS when stdio is
# redirected (pytest captures stdio). The result is a suite that hangs with no
# error. The OS arch of a running 64-bit interpreter is knowable without
# subprocessing, so answer it directly.
#
# This lives in conftest so it runs before ANY test module or fixture imports
# blue_engine -- a per-file guard cannot help, because pytest imports conftest
# (and its fixtures) first. Same reasoning as
# ``Blue_engine/dashboard/run_server.py``.
# ---------------------------------------------------------------------------
if os.name == "nt":
    _ARCH = os.environ.get("PROCESSOR_ARCHITECTURE", "") or "AMD64"
    platform.machine = lambda: _ARCH                       # type: ignore[assignment]
    platform.win32_ver = lambda *a, **k: ("10", "", "", "")  # type: ignore[assignment]
    platform.uname = lambda *a, **k: platform.uname_result(  # type: ignore[assignment]
        "Windows", platform.node(), "10", "", "", _ARCH
    )

import pytest  # noqa: E402


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
