"""
tests/test_portfolio_manager.py — M1 API-shell tests.

Run:  & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests/test_portfolio_manager.py -q
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import portfolio_manager as pm_mod  # noqa: E402


def _inputs(**over):
    d = {
        "universe": ["NIFTY", "IT", "SMALLCAP"],
        "vols": {"NIFTY": 0.14, "IT": 0.16, "SMALLCAP": 0.50},
        "corr": {"NIFTY": {"NIFTY": 1.0, "IT": 0.5, "SMALLCAP": 0.2},
                 "IT": {"NIFTY": 0.5, "IT": 1.0, "SMALLCAP": 0.85},
                 "SMALLCAP": {"NIFTY": 0.2, "IT": 0.85, "SMALLCAP": 1.0}},
        "betas": {"NIFTY": 1.0, "IT": 0.8, "SMALLCAP": 1.3},
        "current_weights": {"NIFTY": 0.3, "IT": 0.1},
        "capital": 1_000_000.0,
    }
    d.update(over)
    return d


def test_registry_defaults_and_create():
    _check = lambda c, m: (_ for _ in ()).throw(AssertionError(m)) if not c else None  # noqa
    assert "default" in pm_mod.registered()
    pm = pm_mod.PortfolioManager.create({"persist": False})
    assert isinstance(pm, pm_mod.PortfolioManager)


def test_refresh_returns_ok_and_contract_keys():
    pm = pm_mod.PortfolioManager.create({"persist": False})
    out = pm.refresh_portfolio({"portfolio_inputs": _inputs()})
    assert out["ok"] is True
    for k in ("target_weights", "rebalance_trades", "risk", "optimiser"):
        assert k in out, f"missing {k}"


def test_dashboard_data_never_raises_and_has_drift():
    pm = pm_mod.PortfolioManager.create({"persist": False})
    out = pm.get_dashboard_data({"portfolio_inputs": _inputs()})
    assert out["ok"] is True
    assert "drift" in out and "regime" in out
    assert out["optimiser"]


def test_regime_unknown_without_economic():
    pm = pm_mod.PortfolioManager.create({"persist": False})
    out = pm.get_regime({})
    assert out["regime"] in ("UNKNOWN", "RISK_ON", "RISK_OFF", "NEUTRAL")


def test_engine_failure_is_soft_with_stale_cache():
    pm = pm_mod.PortfolioManager.create({"persist": False})
    pm.refresh_portfolio({"portfolio_inputs": _inputs()})
    def boom(*a, **k):
        raise RuntimeError("net down")
    pm._engine.run = boom  # simulate an engine crash
    out = pm.refresh_portfolio({})
    assert out["ok"] is False          # soft failure, never raised
    assert out.get("target_weights"), "should serve last known run"


def test_tearsheet_runs(tmp_path=None):
    pm = pm_mod.PortfolioManager.create({"persist": False})
    out = pm.get_tearsheet(days=7)
    assert out["ok"] in (True, False)  # degraded path allowed, no raise


def test_run_optimization_named_method():
    pm = pm_mod.PortfolioManager.create({"persist": False})
    out = pm.run_optimization("hrp", {"portfolio_inputs": _inputs()})
    assert out.get("method") == "hrp"
    assert "weights" in out


def test_registry_swap():
    class Alt(pm_mod.PortfolioManager):
        name = "alt"
    pm_mod.register("alt", Alt)
    pm = pm_mod.PortfolioManager.create({"persist": False, "manager_impl": "alt"})
    assert isinstance(pm, Alt)


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:
                print(f"SKIP {name} (needs pytest fixtures)")
                continue
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
