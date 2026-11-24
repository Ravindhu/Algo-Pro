"""Tests for the AlphaEngine retirement (owner decision 2026-11-24).

Strategy-only: POV takes its symbol/action from a supplied strategy intent and
never picks a symbol by itself. The macro pipeline has no ``alpha`` stage.
"""
from quant.engines.core.pipeline import MacroPipeline
from quant.engines.core.stage_gate import StageGate
from quant.engines.pov.pov_engine import PovEngine


def test_pipeline_has_no_alpha_stage():
    names = [e.name for e in MacroPipeline({"dry_run": True}).engines]
    assert "alpha" not in names
    assert "portfolio" in names and "pov" in names


def test_alpha_engine_is_parked_not_importable_from_package():
    import quant.engines.alpha as alpha_pkg
    assert not hasattr(alpha_pkg, "AlphaEngine")
    assert not hasattr(alpha_pkg, "signals")  # signals parked 2026-11-24 (Temp/Stag)


def test_pov_without_strategy_intent_fails_closed():
    res = PovEngine({"pov": {"quantity": 10}}).run({"dry_run": True})
    assert res.data["slice"]["status"] == "skipped"
    assert res.data["slice"]["reason"] == "no strategy intent"


def test_pov_uses_supplied_strategy_intent():
    ctx = {"dry_run": True,
           "strategy": {"intent": {"symbol": "INFY", "action": "BUY",
                                   "quantity": 5, "exchange": "NSE"}}}
    res = PovEngine({"pov": {"quantity": 10}}).run(ctx)
    cfg = res.data["slice"]["config"]
    assert cfg["symbol"] == "INFY" and cfg["action"] == "BUY"
    assert cfg["quantity"] == 5


def test_pov_refuses_when_action_missing_everywhere():
    ctx = {"dry_run": True, "strategy": {"intent": {"symbol": "INFY"}}}
    res = PovEngine({"pov": {"quantity": 10}}).run(ctx)
    assert res.data["slice"]["status"] == "skipped"
    assert res.data["slice"]["reason"] == "no action"


def test_portfolio_gate_needs_strategy_intent():
    gate = StageGate.from_config({"engines": {"stage_gate": {"enabled": True}}})
    assert gate.check("portfolio", {}).satisfied is False
    ok = gate.check("portfolio", {"strategy": {"intent": {"symbol": "INFY"}}})
    assert ok.satisfied is True
