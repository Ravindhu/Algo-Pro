"""Phase 1: data QA, corporate actions, point-in-time store, universe, end-to-end paper run."""
import numpy as np
import pandas as pd

from quant.data.corp_actions import back_adjust
from quant.data.feature_store import FeatureStore
from quant.data.qa import check_bars
from quant.data.universe import Universe
from quant.data.ingest import FeedHealth
from datetime import datetime, timedelta
import sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import run_paper  # noqa: E402


def _good(n=20):
    d = pd.bdate_range("2026-01-01", periods=n)
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame({"date": d, "symbol": "A", "open": close, "high": close + 1,
                         "low": close - 1, "close": close, "volume": 1000})


def test_clean_bars_pass():
    assert check_bars(_good()).passed


def test_nan_and_duplicates_fail():
    b = _good()
    b.loc[3, "close"] = np.nan
    assert not check_bars(b).passed
    d = pd.concat([_good(), _good().iloc[[0]]])
    assert not check_bars(d).passed


def test_unexplained_jump_fails_but_listed_action_passes():
    b = _good()
    # A permanent step (like an unadjusted split), not a one-bar spike.
    b.loc[b.index >= 10, ["open", "high", "low", "close"]] *= 2.0
    assert not check_bars(b).passed
    act = pd.DataFrame([{"symbol": "A", "date": b.date.iloc[10]}])
    assert check_bars(b, act).passed


def test_back_adjust_divides_prices_before_action():
    b = _good(5)
    act = pd.DataFrame([{"symbol": "A", "date": b.date.iloc[3], "factor": 2.0}])
    adj = back_adjust(b, act)
    assert adj.close.iloc[0] == b.close.iloc[0] / 2
    assert adj.close.iloc[3] == b.close.iloc[3]


def test_feature_store_is_point_in_time():
    fs = FeatureStore()
    t1, t2 = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")
    fs.put("pe", "A", t2, 20.0)
    fs.put("pe", "A", t1, 10.0)
    assert fs.get_asof("pe", "A", pd.Timestamp("2025-12-31")) is None
    assert fs.get_asof("pe", "A", t1) == 10.0
    assert fs.get_asof("pe", "A", pd.Timestamp("2026-01-05")) == 20.0


def test_universe_includes_delisted_by_date():
    u = Universe()
    u.add("OLD", pd.Timestamp("2020-01-01"), pd.Timestamp("2024-06-01"))
    assert u.is_member("OLD", pd.Timestamp("2023-01-01"))
    assert not u.is_member("OLD", pd.Timestamp("2025-01-01"))


def test_feed_health_detects_stale_feed():
    h = FeedHealth(max_gap_seconds=5)
    now = datetime(2026, 1, 1, 10, 0, 0)
    assert h.is_stale(now)
    h.heartbeat(now)
    assert not h.is_stale(now + timedelta(seconds=2))
    assert h.is_stale(now + timedelta(seconds=10))


def test_paper_run_end_to_end_and_restart_safe(tmp_path):
    limits = tmp_path / "limits.yaml"
    limits.write_text(
        "max_order_quantity: 1000\nmax_order_notional: 5000000\nmax_open_exposure: 20000000\n"
        "max_daily_loss: 100000\nmax_strategy_loss: 50000\nmax_adv_pct: 0.05\nmax_symbols_per_portfolio: 10\n"
    )
    sig = tmp_path / "signals.csv"
    pd.DataFrame([
        {"intent_id": "I1", "strategy_id": "S1", "symbol": "A", "side": "BUY", "qty": 10,
         "ref_price": 100.0, "kind": "entry", "lot_size": 1, "prev_close": 99.0,
         "circuit_pct": 0.2, "adv_shares": 1e6, "asm_gsm": False},
        {"intent_id": "I2", "strategy_id": "S1", "symbol": "A", "side": "BUY", "qty": 5000,
         "ref_price": 100.0, "kind": "entry", "lot_size": 1, "prev_close": 99.0,
         "circuit_pct": 0.2, "adv_shares": 1e6, "asm_gsm": False},
    ]).to_csv(sig, index=False)
    out = tmp_path / "paper"
    first = run_paper.run(sig, limits, out)
    assert first["approved"] == 1 and first["blocked"] == 1 and first["positions"] == {"A": 10}
    second = run_paper.run(sig, limits, out)
    assert second["approved"] == 0 and second["positions"] == {"A": 10}
    assert second["audit_verified"]
