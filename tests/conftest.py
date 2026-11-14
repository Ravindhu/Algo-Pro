"""Repo-level test conventions for everything under tests/.

1. ``src/`` on ``sys.path`` so ``import quant.*`` works from a plain checkout.
2. The blue_engine latching kill switch is isolated for EVERY test under
   ``tests/``, not just the ones in ``src/quant/blue_engine/tests/``.

WHY (2) IS HERE AND NOT ONLY UNDER src/quant/blue_engine/tests
--------------------------------------------------------------
``blue_engine.portfolio.optim_policy.decide()`` keeps ONE latching kill switch per
process and PERSISTS a trip to sqlite. ``src/quant/blue_engine/tests/conftest.py``
resets it around each of ITS tests, but that fixture only applies to files inside that
folder. A test under ``tests/`` that calls ``decide(drawdown=...)`` above the
limit therefore leaves the switch tripped for the rest of the session.

Measured 2026-11-08, after the quant suite moved into ``src/quant/tests`` and the
testpaths order changed: ``pytest`` produced 24 failures, all in
``src/quant/blue_engine/tests/test_optim_policy.py``, because ``decide()`` returned
``EXIT_TO_CASH`` before reaching the data-validity checks the tests were
asserting on. The order-only nature hid it from whichever suite ran last.

The same fixture is installed at the repo level so the guarantee does not depend
on folder placement or on pytest's collection order. It is a no-op when
``blue_engine`` or its optional SDK is absent.
"""
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_blue_kill_switch(request):
    """Fresh, unpersisted Blue kill switch around every test under tests/.

    Mirrors src/quant/blue_engine/tests/conftest.py::_clean_process_kill_switch. See the
    module docstring for the 24-failure ordering bug this prevents.

    NOT for tests/parity. Those tests deliberately register STUB ``blue_engine``
    packages in ``sys.modules`` so the real ``__init__`` (which needs openalgo)
    never runs. Importing the real package here - even inside an except guard -
    put real ``blue_engine.core`` next to the stub and produced 17
    ``ImportError: cannot import name 'kill_switch_store' from 'blue_engine.core'
    (unknown location)`` errors. Parity does not touch the kill switch.
    """
    import os
    import tempfile

    if "parity" in request.node.nodeid.split("/")[0]:
        yield
        return

    try:
        from blue_engine.core import kill_switch_store as ks_store
        from blue_engine.portfolio import optim_policy as op
    except ImportError:  # optional SDK absent, or a stubbed package: nothing to isolate
        yield
        return

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    os.environ["BLUE_KILL_SWITCH_DB_PATH"] = os.path.join(
        tmp.name, "kill_switch.db")
    ks_store.close_store()
    ks_store._DEGRADED = False
    op._PROCESS_KILL_SWITCH = None
    yield
    op._PROCESS_KILL_SWITCH = None
    ks_store.close_store()
    ks_store._DEGRADED = False
    os.environ.pop("BLUE_KILL_SWITCH_DB_PATH", None)
    tmp.cleanup()
