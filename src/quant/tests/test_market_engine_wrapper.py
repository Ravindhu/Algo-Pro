"""Market engine wrapper: a missing market must never look like a neutral market.

Flow stage: Market engine
Status: TEST

Every section checks the pass path and a block path:
  * no data   - the state is UNKNOWN and risk appetite is None, never a neutral score
  * clamping  - risk appetite stays in [-1, 1]; a non-finite value is not used
  * never raises - junk payloads and a failing provider return a summary, not an error
Blue-dependent tests are skipped when the Blue market engine cannot load.
"""
from __future__ import annotations

import pytest

from quant.core.blue_bridge import BlueEngineUnavailable, load
from quant.data.market_engine import market_context, run_market


def _blue_ok() -> bool:
    try:
        load("market.market_engine")
        return True
    except BlueEngineUnavailable:
        return False


BLUE_MARKET = pytest.mark.skipif(not _blue_ok(), reason="Blue market engine unavailable")


@BLUE_MARKET
def test_no_market_data_is_unknown_not_neutral():
    result = run_market(market={})
    summary = market_context(result)
    assert summary["no_data"] is True
    assert summary["risk_appetite"] is None
    assert summary["volatility_state"] == "UNKNOWN"


@BLUE_MARKET
def test_sample_market_gives_a_bounded_score():
    sample = {
        "indices": {
            "NIFTY": {"close": 110.0, "prev_close": 108.0, "return_1m_pct": 3.0, "return_3m_pct": 5.0},
            "BANKNIFTY": {"close": 240.0, "prev_close": 245.0, "return_1m_pct": 1.0, "return_3m_pct": 2.0},
            "NIFTYIT": {"close": 90.0, "prev_close": 89.0, "return_1m_pct": -2.0, "return_3m_pct": -1.0},
        },
        "commodities": {},
        "fii": {"net_cash": 1500.0, "net_futures": 200.0},
    }
    summary = market_context(run_market(market=sample))
    assert summary["no_data"] is False
    assert summary["risk_appetite"] is not None
    assert -1.0 <= summary["risk_appetite"] <= 1.0


def test_out_of_range_risk_is_clamped():
    summary = market_context({"ok": True, "data": {"risk_appetite": 5.0}, "warnings": []})
    assert summary["risk_appetite"] == 1.0
    assert any("clamped" in r for r in summary["reasons"])
    low = market_context({"ok": True, "data": {"risk_appetite": -7.0}, "warnings": []})
    assert low["risk_appetite"] == -1.0


def test_non_finite_risk_is_not_used():
    summary = market_context({"ok": True, "data": {"risk_appetite": float("nan")}, "warnings": []})
    assert summary["risk_appetite"] is None


@pytest.mark.parametrize("junk", [None, 42, "x", {"data": "not a dict"}, {}])
def test_junk_payload_never_raises(junk):
    summary = market_context(junk)
    assert summary["no_data"] is True
    assert summary["risk_appetite"] is None
    assert summary["volatility_state"] == "UNKNOWN"


def test_provider_failure_returns_a_result_not_an_error():
    def broken():
        raise RuntimeError("feed down")
    result = run_market(provider=broken)
    assert isinstance(result, dict)
    assert "ok" in result
