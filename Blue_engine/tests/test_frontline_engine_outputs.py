from __future__ import annotations

from types import SimpleNamespace

from blue_engine.core.pipeline import MacroPipeline
from blue_engine.core.risk_engine import RiskEngine
from blue_engine.economics import EconomicEngine
from blue_engine.market import MarketEngine
from blue_engine.portfolio import PortfolioEngine
from blue_engine.screener import ScreenerEngine, ValuationEngine
from blue_engine.screener import valuation_engine


def test_economic_engine_exposes_bounded_macro_decision_fields():
    result = EconomicEngine({
        "macro": {
            "gdp_growth": {"value": 6.8, "previous": 6.2},
            "inflation": {"value": 5.1, "previous": 4.8},
        },
    }).run()

    assert result.ok
    assert result.data["macro_regime"] == "Expansionary"
    assert result.data["interest_rate_bias"] == "Hawkish"
    assert 0.25 <= result.data["equity_regime_multiplier"] <= 1.5
    assert len(result.data["rationale"].split()) <= 15


def test_market_engine_computes_volatility_and_bias_from_supplied_returns():
    result = MarketEngine().run({
        "market": {
            "daily_returns_pct": [1.0, -1.0, 1.0, -1.0],
            "indices": {
                "NIFTY": {
                    "close": 101, "prev_close": 100,
                    "return_1m_pct": 2.0, "return_3m_pct": 3.0,
                },
            },
        },
    })

    assert result.ok
    assert result.data["market_volatility_state"] == "Elevated"
    assert result.data["technical_lookback_days"] == 20
    assert result.data["short_term_bias"] == "Bullish"
    assert result.data["realized_volatility_pct"] > 0


def test_market_engine_marks_explicit_panic_as_high_panic():
    result = MarketEngine().run({
        "market": {"extreme_market_panic": True, "indices": {}},
    })

    assert result.data["market_volatility_state"] == "High_Panic"


def test_market_engine_does_not_treat_negative_volatility_as_low_risk():
    result = MarketEngine().run({
        "market": {"annualized_volatility_pct": -1.0},
    })

    assert result.data["market_volatility_state"] == "Elevated"
    assert any("invalid annualized volatility" in warning
               for warning in result.warnings)


def test_portfolio_enforces_cash_and_technology_limits_and_risk_veto():
    inputs = {
        "universe": ["IT_A", "IT_B", "BANK"],
        "vols": {"IT_A": 0.2, "IT_B": 0.2, "BANK": 0.2},
        "corr": {
            "IT_A": {"IT_B": 0.1, "BANK": 0.1},
            "IT_B": {"IT_A": 0.1, "BANK": 0.1},
            "BANK": {"IT_A": 0.1, "IT_B": 0.1},
        },
        "sector_by_symbol": {
            "IT_A": "Information Technology",
            "IT_B": "Software",
            "BANK": "Banking",
        },
        "capital": 100_000,
    }
    result = PortfolioEngine({
        "optimiser": "risk_parity",
        "max_weight": 1.0,
        "max_gross": 1.0,
    }).run({
        "portfolio_inputs": inputs,
        "risk": {"data": {"risk_veto_active": True}},
    })

    assert result.ok
    assert sum(abs(w) for w in result.data["target_weights"].values()) <= 0.85 + 1e-6
    tech_gross = abs(result.data["target_weights"].get("IT_A", 0)) + \
        abs(result.data["target_weights"].get("IT_B", 0))
    assert tech_gross <= 0.20 + 1e-6
    assert result.data["rebalance_allowed"] is False
    assert result.data["rebalance_trades"] == []


def test_screener_verified_legal_warning_overrides_fundamental_pass():
    result = ScreenerEngine({
        "enabled": True,
        "source": "symbols",
        "symbols": [],
    }).run({
        "screener_inputs": {
            "candidates": [{
                "symbol": "GOOD",
                "sector": "Industrials",
                "metrics": {"market_cap": "500 Cr.", "debt_equity": "0.1"},
            }],
            "legal_warnings": [{
                "ticker": "GOOD",
                "reason": "Exchange trading suspension pending",
                "source": "exchange bulletin reference",
                "verified": True,
            }],
        },
    })

    assert result.ok
    assert result.data["selected"] == []
    assert result.data["asset_blacklist"] == ["GOOD"]
    assert result.data["blacklist_reasons"][0]["ticker"] == "GOOD"


def test_valuation_engine_is_offline_and_returns_unfabricated_values(monkeypatch):
    fake_valuation = SimpleNamespace(
        as_dict=lambda: {
            "symbol": "TCS", "status": "ok",
            "classification": "strong", "classification_reason": "Base value exceeds price",
        },
    )

    def fake_value_company(company, **kwargs):
        assert company == {"financials": "provided"}
        assert kwargs["symbol"] == "TCS"
        return fake_valuation

    monkeypatch.setattr(valuation_engine, "value_company", fake_value_company)
    result = ValuationEngine().run({
        "valuation_inputs": {"companies": {"TCS": {"financials": "provided"}}},
    })

    assert result.ok
    assert result.data["target_ticker"] == "TCS"
    assert result.data["growth_rate_ceiling"] == 0.05
    assert result.data["margin_safety_adjustment"] == 0.85
    assert result.data["valuations"]["TCS"]["status"] == "ok"


def test_risk_engine_vetoes_corrupted_data_and_live_missing_inputs():
    incident = RiskEngine().run({
        "dry_run": True,
        "risk_inputs": {"corrupted_data": True},
    })
    missing_live = RiskEngine().run({"dry_run": False})

    assert incident.data["risk_veto_active"] is True
    assert "integrity" in incident.data["veto_reason"]
    assert missing_live.data["risk_veto_active"] is True


def test_macro_pipeline_blocks_pov_when_risk_veto_is_active():
    report = MacroPipeline({
        "dry_run": True,
        "gate_on_infrastructure": False,
    }).run({"risk_inputs": {"conflicting_reports": True}})

    assert report["stages"]["risk"]["data"]["risk_veto_active"] is True
    assert report["stages"]["pov"]["data"]["slice"]["status"] == "blocked"
