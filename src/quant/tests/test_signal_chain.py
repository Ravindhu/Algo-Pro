"""Signal stage tests (paper path). Sizing checks are hand-computed; the end-to-end test runs the
existing paper runner (scripts/run_paper.py) on synthetic bars. Nothing reaches the order API."""
import importlib.util
import pathlib

import numpy as np
import pandas as pd
import pytest

from quant.signal.build_signals import build_signals, write_signals
from quant.strategies.base import Strategy
from quant.strategies.momentum import MomentumStrategy

REPO = pathlib.Path(__file__).resolve().parents[3]
LIMITS = REPO / "config" / "risk_limits.yaml"
LIMITS_DICT = {"max_order_quantity": 10, "max_order_notional": 10000, "max_open_exposure": 20000}


class OneSymbol(Strategy):
    """Selects a single symbol with full weight (so the optimiser gives weight 1.0 by construction)."""
    name = "one_symbol"

    def __init__(self, symbol):
        self.symbol = symbol

    def get_params(self):
        return {"symbol": self.symbol}

    def target_weights(self, close_history):
        w = pd.Series(0.0, index=close_history.columns)
        w[self.symbol] = 1.0
        return w


def _bars(price, days=5, sym="X"):
    dates = pd.bdate_range("2026-01-01", periods=days)
    return pd.DataFrame({"date": dates, "symbol": sym, "open": price, "close": price,
                         "prev_close": price, "circuit_pct": 20.0, "adv_shares": 1e6, "asm_gsm": False})


def _sig(bars, limits=LIMITS_DICT, capital=20000.0, held=None):
    return build_signals(bars, strategy=OneSymbol("X"), optimiser_name="risk_parity",
                         strategy_id="t", limits=limits, capital=capital, held=held)


def test_quantity_limit_binds_hand_checked():
    # capital 20000 / price 500 = 40 shares; per-order cap 10 shares; notional cap 10000/500 = 20.
    s = _sig(_bars(500.0))
    assert s.loc[0, "qty"] == 10 and s.loc[0, "side"] == "BUY" and s.loc[0, "kind"] == "entry"


def test_notional_limit_binds_hand_checked():
    # price 2000: 20000/2000 = 10 shares; notional cap 10000/2000 = 5 shares -> 5 (qty cap is 10).
    s = _sig(_bars(2000.0))
    assert s.loc[0, "qty"] == 5


def test_exposure_limit_binds_hand_checked():
    # price 500, open exposure cap 3000 -> 6 shares (3000 / 500).
    s = _sig(_bars(500.0), limits={**LIMITS_DICT, "max_open_exposure": 3000})
    assert s.loc[0, "qty"] == 6


def test_intent_ids_are_deterministic():
    a = _sig(_bars(500.0))
    b = _sig(_bars(500.0))
    assert a.loc[0, "intent_id"] == b.loc[0, "intent_id"] == "t-20260107-BUY-X"


def test_held_symbol_not_selected_gets_exit():
    bars = _bars(100.0, sym="X")
    bars = pd.concat([bars, _bars(100.0, sym="Y")], ignore_index=True)
    s = build_signals(bars, strategy=OneSymbol("X"), optimiser_name="risk_parity", strategy_id="t",
                      limits=LIMITS_DICT, capital=20000.0, held={"Y": 4})
    exits = s[s["side"] == "SELL"]
    assert len(exits) == 1 and exits.iloc[0]["symbol"] == "Y"
    assert exits.iloc[0]["qty"] == 4 and exits.iloc[0]["kind"] == "exit"


class TwoSymbols(Strategy):
    name = "two_symbols"

    def get_params(self):
        return {}

    def target_weights(self, close_history):
        return pd.Series({"X": 0.5, "Y": 0.5}).reindex(close_history.columns).fillna(0.0)


def test_optimiser_failure_raises_and_writes_nothing(tmp_path):
    # Two symbols are needed: with one symbol the optimiser is skipped (weight 1.0).
    bars = pd.concat([_bars(100.0, sym="X"), _bars(120.0, sym="Y")], ignore_index=True)
    with pytest.raises(RuntimeError):
        build_signals(bars, strategy=TwoSymbols(), optimiser_name="black_litterman",
                      strategy_id="t", limits=LIMITS_DICT, capital=20000.0)
    assert not (tmp_path / "signals.csv").exists()


def test_bad_bars_refused():
    with pytest.raises(ValueError):
        build_signals(pd.DataFrame({"date": [1], "symbol": ["X"]}), strategy=OneSymbol("X"),
                      optimiser_name="risk_parity", strategy_id="t", limits=LIMITS_DICT, capital=1.0)


def _load_run_paper():
    spec = importlib.util.spec_from_file_location("run_paper", REPO / "scripts" / "run_paper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _e2e_bars():
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2025-06-01", periods=120)
    rows = []
    for j, drift in enumerate([0.004, 0.002, 0.001, -0.001, 0.0, 0.003]):
        px = 300 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, len(dates))))
        prev = np.concatenate([[px[0]], px[:-1]])
        for d, c, p in zip(dates, px, prev):
            rows.append({"date": d, "symbol": f"S{j}", "open": c, "close": c, "prev_close": p,
                         "circuit_pct": 20.0, "adv_shares": 1e6, "asm_gsm": False})
    return pd.DataFrame(rows)


def _signals(tmp_path):
    sig = build_signals(_e2e_bars(), strategy=MomentumStrategy(lookback=20, top_n=3),
                        optimiser_name="risk_parity", strategy_id="mom", limits=LIMITS_DICT, capital=20000.0)
    assert len(sig) > 0 and (sig["qty"] > 0).all() and sig["qty"].le(10).all()
    assert (sig["qty"] * sig["ref_price"]).sum() <= 20000.0 + 1e-6
    csv = tmp_path / "signals.csv"
    write_signals(sig, csv)
    return csv, len(sig)


def _limits_with(tmp_path, **extra):
    """Copy of the real limits file with extra keys. Test-only values; the owner file is not changed."""
    text = LIMITS.read_text(encoding="utf-8")
    text += "\n" + "\n".join(f"{k}: {v}" for k, v in extra.items()) + "\n"
    path = tmp_path / "limits.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_end_to_end_blocked_while_symbol_limit_unset(tmp_path):
    # The owner file has no max_symbols_per_portfolio, so the gate fails closed: no fills.
    csv, n = _signals(tmp_path)
    run_paper = _load_run_paper()
    summary = run_paper.run(str(csv), str(LIMITS), str(tmp_path / "out"))
    assert summary["audit_verified"] is True
    assert summary["approved"] == 0 and summary["blocked"] == n and summary["filled"] == 0


def test_end_to_end_fills_through_paper_broker_when_limits_set(tmp_path):
    csv, n = _signals(tmp_path)
    limits = _limits_with(tmp_path, max_symbols_per_portfolio=3)
    run_paper = _load_run_paper()
    summary = run_paper.run(str(csv), str(limits), str(tmp_path / "out"))
    assert summary["audit_verified"] is True
    assert summary["approved"] == n and summary["blocked"] == 0
    assert summary["filled"] == n


def test_bridge_refuses_when_analyzer_is_off(tmp_path):
    # Analyze mode is checked before every send. Analyzer off -> refused; the order endpoint is never hit.
    from quant.execution.openalgo_client import OpenAlgoClient
    csv, n = _signals(tmp_path)
    limits = _limits_with(tmp_path, max_symbols_per_portfolio=3)
    calls = []

    def transport(path, payload):
        calls.append(path)
        if path.endswith("/analyzer"):
            return {"status": "success", "data": {"analyze_mode": False}}
        raise AssertionError("order endpoint must not be reached")

    client = OpenAlgoClient(base_url="http://openalgo.invalid", transport=transport, mode="analyze")
    run_paper = _load_run_paper()
    broker = run_paper.AnalyzeBroker(client=client, api_key="test-key")
    summary = run_paper.run(str(csv), str(limits), str(tmp_path / "out"), broker=broker)
    assert summary["filled"] == 0 and summary["rejected"] == n
    assert all(c.endswith("/analyzer") for c in calls)
