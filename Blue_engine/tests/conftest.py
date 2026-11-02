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
# ``Blue_engine/dashboard/web/run.py``.
# ---------------------------------------------------------------------------
if os.name == "nt":
    _ARCH = os.environ.get("PROCESSOR_ARCHITECTURE", "") or "AMD64"
    platform.machine = lambda: _ARCH                       # type: ignore[assignment]
    platform.win32_ver = lambda *a, **k: ("10", "", "", "")  # type: ignore[assignment]
    platform.uname = lambda *a, **k: platform.uname_result(  # type: ignore[assignment]
        "Windows", platform.node(), "10", "", "", _ARCH
    )

import pytest  # noqa: E402
# The checkout keeps source packages flat while the install maps them to the
# public ``blue_engine`` namespace. Make pytest against the source checkout use
# that exact package mapping without requiring an editable install in whichever
# interpreter launched pytest. This does not affect normal application imports.
import importlib.util  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if "blue_engine" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "blue_engine", _SOURCE_ROOT / "__init__.py",
        submodule_search_locations=[str(_SOURCE_ROOT)])
    if _spec is None or _spec.loader is None:
        raise RuntimeError("cannot initialize blue_engine from source checkout")
    _blue_engine = importlib.util.module_from_spec(_spec)
    sys.modules["blue_engine"] = _blue_engine
    _spec.loader.exec_module(_blue_engine)

@pytest.fixture(autouse=True)
def _clean_process_kill_switch():
    """Reset the latching kill-switch before and after every test.

    Also isolates the DURABLE kill-switch store per test (kill-switch
    persistence now survives restarts, so a test that trips the switch must
    not leak a persisted trip into the next test's process-level reload).
    """
    import os
    import tempfile
    try:
        from blue_engine.core import kill_switch_store as ks_store
        from blue_engine.portfolio import optim_policy as op
    except ModuleNotFoundError as exc:
        # Optional SDK missing: there is no switch to isolate, so let
        # pure tests run. Any other missing module still fails loudly.
        if exc.name not in ("openalgo", "rich"):
            raise
        yield
        return

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


# ---------------------------------------------------------------------------
# Optional-dependency collection guard.
#
# Some sandboxes cannot install every runtime dependency (for example
# ``openalgo`` or the Dash stack). A test module that fails to COLLECT only
# because one of these optional packages is missing is reported as SKIPPED
# with the reason, and listed in the terminal summary. Any other import error
# still fails loudly, so real breakage is never hidden.
# ---------------------------------------------------------------------------
_OPTIONAL_DEPS = ("openalgo", "rich")
_SKIPPED_COLLECTION: list = []


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    report = outcome.get_result()
    if report.outcome != "failed":
        return
    text = str(report.longrepr)
    for dep in _OPTIONAL_DEPS:
        if f"No module named '{dep}'" in text:
            path = str(getattr(collector, "path", collector))
            reason = f"optional dependency '{dep}' not installed"
            _SKIPPED_COLLECTION.append((path, reason))
            report.outcome = "skipped"
            report.longrepr = (path, 0, f"Skipped: {reason}")
            return


def pytest_terminal_summary(terminalreporter):
    if _SKIPPED_COLLECTION:
        terminalreporter.write_sep("-", "modules skipped at collection (optional deps)")
        for path, reason in _SKIPPED_COLLECTION:
            terminalreporter.write_line(f"  {path}: {reason}")
