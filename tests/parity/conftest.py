"""Shared fixture: load REAL Blue_engine leaf modules without running Blue's package __init__.

Blue's package __init__ files import openalgo/httpx, which the sandbox cannot install. Each
Blue package directory is registered as a stub module whose __path__ points at the real
folder, so only the leaf modules we need execute. Relative imports (``from ...core.results``)
then resolve to the real files. Everything is restored after each test.
"""
import importlib
import pathlib
import sys
import types

import pytest

BLUE = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant" / "blue_engine"
PACKAGES = ["", "core", "portfolio", "portfolio.risk", "portfolio.optim", "portfolio.sizing",
            "backtest", "ml", "ml.evaluation", "screener", "screener.client"]


@pytest.fixture
def blue(monkeypatch):
    """Returns get(modname) -> real Blue module, e.g. get("portfolio.risk.tail").

    Teardown pops only the modules PARITY CREATED. Modules that other suites
    (collected earlier in the session, e.g. Blue_engine/tests) already imported
    must keep their identity: dropping them forces a re-import, and a second
    module object breaks isinstance() identity checks like the TokenGovernor's
    ChatMessage cache test (full-suite ordering bug, fixed 2026-11-11).
    """
    noop = lambda *a, **k: None  # noqa: E731
    logger = types.SimpleNamespace(info=noop, warning=noop, debug=noop)
    preexisting = {k for k in sys.modules if k == "blue_engine" or k.startswith("blue_engine.")}
    for pkg in PACKAGES:
        name = "blue_engine" + ("." + pkg if pkg else "")
        mod = types.ModuleType(name)
        mod.__path__ = [str(BLUE / pkg.replace(".", "/"))] if pkg else [str(BLUE)]
        mod.logger = logger
        monkeypatch.setitem(sys.modules, name, mod)

    def get(modname):
        return importlib.import_module("blue_engine." + modname)

    yield get
    for key in [k for k in sys.modules if k == "blue_engine" or k.startswith("blue_engine.")]:
        if key not in preexisting:
            sys.modules.pop(key, None)
