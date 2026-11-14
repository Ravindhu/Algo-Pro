from __future__ import annotations

import json

from blue_engine.core import frontline_agent
from blue_engine.core.frontline_agent import FrontlineAgent
from blue_engine.core.pipeline import MacroPipeline


def _valid_advice(engine):
    if engine == "economic":
        return {
            "macro_regime": "Normal",
            "equity_regime_multiplier": 0.8,
            "interest_rate_bias": "Neutral",
            "rationale": "Cautious growth and inflation readings",
        }
    if engine == "market":
        return {
            "market_volatility_state": "High_Panic",
            "technical_lookback_days": 10,
            "short_term_bias": "Bearish",
            "rationale": "Text reports severe disruption",
        }
    if engine == "screener":
        return {
            "asset_blacklist": ["BAD"],
            "blacklist_reasons": [{"ticker": "BAD", "reason": "Unverified governance allegation"}],
        }
    if engine == "valuation":
        return {
            "target_ticker": "TCS",
            "growth_rate_ceiling": 0.05,
            "margin_safety_adjustment": 0.85,
            "rationale": "Use a conservative terminal growth assumption",
        }
    if engine == "backtest":
        return {
            "historical_start_date": "2020-01-01",
            "slippage_penalty_pct": 0.015,
            "flaw_detected": "Potential price mapping discrepancy",
        }
    if engine == "optimization":
        return {
            "min_exploration_rsi": 15,
            "max_exploration_rsi": 35,
            "optimization_method": "bounded_scipy",
            "rationale": "Restrict search after drawdown",
        }
    if engine == "portfolio":
        return {
            "max_tech_sector_exposure": 0.2,
            "min_cash_buffer_pct": 0.15,
            "rebalance_allowed": False,
            "rationale": "Concentration risk is elevated",
        }
    return {
        "risk_veto_active": True,
        "max_allowed_slippage_pct": 0.002,
        "force_trailing_stop_pct": 2.0,
        "veto_reason": "Conflicting flash reports need verification",
    }


def _agent(monkeypatch, advice):
    monkeypatch.setattr(frontline_agent, "get_secret", lambda name: "test-openrouter-key")

    def post(request, timeout):
        assert request.full_url == FrontlineAgent.endpoint
        assert request.get_header("Authorization") == "Bearer test-openrouter-key"
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["temperature"] == 0
        return {"choices": [{"message": {"content": json.dumps(advice)}}]}

    return FrontlineAgent({"enabled": True}, http_post=post)


def test_openrouter_advice_is_schema_validated_for_every_engine(monkeypatch):
    for engine in ("economic", "market", "screener", "valuation", "backtest",
                   "optimization", "portfolio", "risk"):
        result = _agent(monkeypatch, _valid_advice(engine)).advise(engine, "public report text")
        assert result["ok"], (engine, result)


def test_invalid_agent_output_is_rejected():
    assert frontline_agent._validate(
        "risk",
        {
            "risk_veto_active": "false",
            "max_allowed_slippage_pct": 0.01,
            "force_trailing_stop_pct": 2.5,
            "veto_reason": "No event identified",
        },
    ) is None
    assert frontline_agent._validate(
        "optimization",
        {
            "min_exploration_rsi": 40,
            "max_exploration_rsi": 15,
            "optimization_method": "bounded_scipy",
            "rationale": "Invalid interval",
        },
    ) is None


def test_agent_rejects_credential_text_before_network(monkeypatch):
    monkeypatch.setattr(frontline_agent, "get_secret", lambda name: "test-openrouter-key")
    agent = FrontlineAgent({"enabled": True}, http_post=lambda *_: None)

    result = agent.advise("economic", "report contains sk-or-v1-abcdefghijklmnopqrstuvwxyz")

    assert result["method"] == "rejected"
    assert "not sent" in result["warning"]


def test_openrouter_risk_advice_can_add_but_not_clear_a_veto(monkeypatch):
    agent = _agent(monkeypatch, {
        "risk_veto_active": False,
        "max_allowed_slippage_pct": 0.005,
        "force_trailing_stop_pct": 2.5,
        "veto_reason": "No additional warning",
    })
    pipeline = MacroPipeline({
        "dry_run": True,
        "gate_on_infrastructure": False,
        "engines": {"agent": {"enabled": True}},
    })
    pipeline.frontline_agent = agent

    report = pipeline.run({
        "risk_inputs": {"corrupted_data": True},
        "agent_inputs": {"risk": "Routine text"},
    })

    assert report["agent_advice"]["risk"]["ok"]
    assert report["stages"]["risk"]["data"]["risk_veto_active"] is True
    assert report["stages"]["pov"]["data"]["slice"]["status"] == "blocked"


def test_market_agent_can_only_escalate_volatility_state(monkeypatch):
    from blue_engine.market import MarketEngine

    advice = _valid_advice("market")
    result = MarketEngine().run({
        "market": {"annualized_volatility_pct": 10},
        "agent_advice": {"market": advice},
    })

    assert result.data["market_volatility_state"] == "High_Panic"

    advice["market_volatility_state"] = "Low"
    result = MarketEngine().run({
        "market": {"annualized_volatility_pct": 20},
        "agent_advice": {"market": advice},
    })
    assert result.data["market_volatility_state"] == "Elevated"
