"""Phase 1 bridge tests (paper path only).

1. Conviction tiers in sizing: flat sizing is unchanged when tiers are off; when on, the tier
   multiplier can raise the size but every hard cap still wins; missing inputs fail closed.
2. Event blackout: the runner loads an event calendar, stamps trade_day on entries, blocks the
   blackout day, and records in the audit log when no calendar was loaded.
Hand-checked numbers are in the comments. Nothing here reaches the order API.
"""
import importlib.util
import json
import pathlib
from datetime import date

import pandas as pd
import pytest

from quant.data.market_inputs import blackout_map, read_event_calendar
from quant.models.conviction import CONDITIONS
from quant.portfolio.tiers import TierTable
from quant.signal.build_signals import build_signals, write_signals
from quant.strategies.base import Strategy

REPO = pathlib.Path(__file__).resolve().parents[3]
LIMITS = REPO / "config" / "risk_limits.yaml"
BASE = {"max_order_quantity": 10, "max_order_notional": 10000, "max_open_exposure": 20000}
ROOMY = {"max_order_quantity": 100, "max_order_notional": 1e9, "max_open_exposure": 1e9}
TIERS_ON = TierTable(enabled=True, bands=((0.50, 1.0), (0.70, 1.5), (0.85, 2.0)), max_multiplier=2.0)
TIERS_OFF = TierTable(enabled=False, bands=((0.50, 1.0),), max_multiplier=2.0)


def _all(value):
    return {name: value for name in CONDITIONS}


class OneSymbol(Strategy):
    """Full weight on symbol X, so the optimiser gives weight 1.0 by construction."""
    name = "one_symbol"

    def get_params(self):
        return {"symbol": "X"}

    def target_weights(self, close_history):
        w = pd.Series(0.0, index=close_history.columns)
        w["X"] = 1.0
        return w


def _bars(price):
    dates = pd.bdate_range("2026-01-01", periods=5)
    return pd.DataFrame({"date": dates, "symbol": "X", "open": price, "close": price,
                         "prev_close": price, "circuit_pct": 20.0, "adv_shares": 1e6, "asm_gsm": False})


def _sig(price, limits=BASE, **kw):
    return build_signals(_bars(price), strategy=OneSymbol(), optimiser_name="risk_parity",
                         strategy_id="t", limits=limits, capital=20000.0, **kw)


# ---- 1. conviction tiers in sizing

def test_tiers_off_is_flat_sizing_unchanged():
    for px in (7.7, 33.3, 500.0, 2000.0):
        flat = _sig(px)
        off = _sig(px, tier_table=TIERS_OFF, conviction={"X": _all(0.9)})
        assert flat["qty"].tolist() == off["qty"].tolist()


def test_low_conviction_blocks_entry():
    # All six conditions at 0.10 -> score 0.10, below the lowest band (0.50) -> multiplier 0 -> no trade.
    log = []
    s = _sig(500.0, limits=ROOMY, tier_table=TIERS_ON, conviction={"X": _all(0.10)}, sizing_log=log)
    assert s.empty
    assert log[0]["binding"] == "conviction_tier" and log[0]["qty"] == 0


def test_high_conviction_scales_up_within_caps():
    # Price 500, capital 20000 -> base 40 shares. Score 0.90 -> x2.0 -> 80. Caps: qty 100, notional 2e6/500,
    # exposure 1e9/500 -> all above 80, so 80 is allowed.
    s = _sig(500.0, limits=ROOMY, tier_table=TIERS_ON, conviction={"X": _all(0.90)})
    assert int(s.loc[0, "qty"]) == 80


def test_hard_cap_beats_conviction():
    # Same case, per-order quantity cap 50 -> the cap wins: 50, never 80.
    s = _sig(500.0, limits={**ROOMY, "max_order_quantity": 50}, tier_table=TIERS_ON,
             conviction={"X": _all(0.90)})
    assert int(s.loc[0, "qty"]) == 50


def test_missing_conviction_inputs_fail_closed():
    log = []
    s = _sig(500.0, limits=ROOMY, tier_table=TIERS_ON, conviction={}, sizing_log=log)
    assert s.empty
    assert log[0]["conviction_inputs"] is False and log[0]["binding"] == "conviction_tier"


@pytest.mark.parametrize("px", [33.0, 250.0, 1200.0])
@pytest.mark.parametrize("score", [0.10, 0.60, 0.75, 0.95])
def test_tiered_size_never_exceeds_any_cap(px, score):
    limits = {"max_order_quantity": 25, "max_order_notional": 15000, "max_open_exposure": 9000}
    s = _sig(px, limits=limits, tier_table=TIERS_ON, conviction={"X": _all(score)})
    qty = int(s["qty"].sum()) if not s.empty else 0
    assert qty <= 25
    assert qty * px <= 15000 + 1e-6
    assert qty * px <= 9000 + 1e-6


# ---- 2. event calendar into the paper runner

def _load_run_paper():
    spec = importlib.util.spec_from_file_location("run_paper", REPO / "scripts" / "run_paper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _signals_csv(tmp_path):
    csv = tmp_path / "signals.csv"
    write_signals(_sig(500.0), csv)
    return csv


def _limits_file(tmp_path):
    # Test-only copy with a symbol limit set. The owner file is not changed.
    path = tmp_path / "limits.yaml"
    path.write_text(LIMITS.read_text(encoding="utf-8") + "\nmax_symbols_per_portfolio: 3\n", encoding="utf-8")
    return path


def _audit(out):
    return [json.loads(line) for line in (out / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


def test_blackout_day_blocks_entry_and_names_the_reason(tmp_path):
    rp, out = _load_run_paper(), tmp_path / "out"
    summary = rp.run(str(_signals_csv(tmp_path)), str(_limits_file(tmp_path)), str(out),
                     trade_day=date(2026, 1, 7), blackout_dates={"X": {date(2026, 1, 7)}})
    assert summary["blocked"] == 1 and summary["filled"] == 0
    reasons = [r for rec in _audit(out) if rec["kind"] == "decision" for r in rec["payload"]["reasons"]]
    assert any("event blackout on 2026-01-07 for X" in r for r in reasons)


def test_day_outside_blackout_fills(tmp_path):
    rp = _load_run_paper()
    summary = rp.run(str(_signals_csv(tmp_path)), str(_limits_file(tmp_path)), str(tmp_path / "out"),
                     trade_day=date(2026, 1, 8), blackout_dates={"X": {date(2026, 1, 7)}})
    assert summary["filled"] == 1


def test_blackout_without_trade_day_fails_closed(tmp_path):
    rp = _load_run_paper()
    summary = rp.run(str(_signals_csv(tmp_path)), str(_limits_file(tmp_path)), str(tmp_path / "out"),
                     trade_day=None, blackout_dates={"X": {date(2026, 1, 7)}})
    assert summary["blocked"] == 1 and summary["filled"] == 0


def test_run_without_calendar_is_recorded_in_audit(tmp_path):
    rp, out = _load_run_paper(), tmp_path / "out"
    rp.run(str(_signals_csv(tmp_path)), str(_limits_file(tmp_path)), str(out), trade_day=date(2026, 1, 8))
    cal = [rec for rec in _audit(out) if rec["kind"] == "event_calendar"]
    assert cal and cal[0]["payload"]["loaded"] is False


def test_event_csv_builds_blackout_window(tmp_path):
    f = tmp_path / "events.csv"
    f.write_text("symbol,date\nx,2026-01-07\n", encoding="utf-8")
    events = read_event_calendar(f)
    assert events == [{"symbol": "X", "date": date(2026, 1, 7)}]
    m = blackout_map(events, ["X", "Y"], 1, 1)
    assert m["X"] == {date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)}
    assert m["Y"] == set()


def test_event_csv_missing_columns_is_refused(tmp_path):
    f = tmp_path / "bad.csv"
    f.write_text("ticker,when\nX,2026-01-07\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_event_calendar(f)
