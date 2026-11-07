"""Dashboard data: FIFO P&L, positions, safety events, AI insight, and the P&L rule (AGENTS rule 8)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

from quant.reporting.dashboard_data import (  # noqa: E402
    ai_insight, build_overview, current_mode, equity_curve, fifo_pnl, safety_events, unrealized,
)


def fill(sym, q, px):
    return {"symbol": sym, "signed_qty": q, "price": px}


def test_fifo_long_round_trip_profit():
    r = fifo_pnl([fill("INFY", 10, 100.0), fill("INFY", -10, 110.0)])
    assert r["open"] == {}
    assert len(r["closed"]) == 1
    assert r["closed"][0]["pnl"] == pytest.approx(100.0)
    assert r["closed"][0]["ret"] == pytest.approx(0.1)


def test_fifo_matches_oldest_lot_first():
    r = fifo_pnl([fill("TCS", 5, 100.0), fill("TCS", 5, 200.0), fill("TCS", -6, 150.0)])
    # 5 from the 100 lot: +250. 1 from the 200 lot: -50. Total +200.
    assert sum(t["pnl"] for t in r["closed"]) == pytest.approx(200.0)
    assert r["open"]["TCS"] == {"qty": 4, "avg_price": pytest.approx(200.0)}


def test_fifo_short_round_trip():
    r = fifo_pnl([fill("SBIN", -10, 100.0), fill("SBIN", 10, 90.0)])
    assert r["closed"][0]["pnl"] == pytest.approx(100.0)


def test_unrealized_none_without_price_never_guessed():
    open_pos = {"INFY": {"qty": 10, "avg_price": 100.0}, "TCS": {"qty": 2, "avg_price": 50.0}}
    u = unrealized(open_pos, {"INFY": 105.0})
    assert u["INFY"] == pytest.approx(50.0)
    assert u["TCS"] is None


def test_safety_events_kill_switch_first_and_newest_first():
    audit = [
        {"kind": "safe_mode", "payload": {"to": "reduce_only", "reason": "feed stale"}, "ts": "t1"},
        {"kind": "override", "payload": {"reason": "manual"}, "ts": "t2"},
    ]
    ev = safety_events(audit, {"active": True, "reason": "automatic: loss"})
    assert ev[0]["severity"] == "CRITICAL" and ev[0]["kind"] == "kill_switch"
    assert [e["kind"] for e in ev[1:]] == ["override", "safe_mode"]


def test_mode_defaults_to_normal_and_uses_last_change():
    assert current_mode([]) == "normal"
    recs = [{"kind": "safe_mode", "payload": {"to": "halt_all"}}, {"kind": "safe_mode", "payload": {"to": "normal"}}]
    assert current_mode(recs) == "normal"


def test_empty_state_is_safe_and_has_no_forbidden_metrics():
    limits = {"blocked": False, "max_order_quantity": None, "max_daily_loss": 1000.0}
    o = build_overview([], [], {}, limits, {})
    assert o["mode"] == "normal"
    assert o["kill_switch"]["active"] is False
    assert o["unset_limits"] == ["max_order_quantity"]
    assert o["performance"]["trades"] == 0
    assert "No closed trades yet" in " ".join(o["insight"])
    text = json.dumps(o).lower()
    for banned in ("expected_value", '"ev"', "mae", "mfe", "compliance"):
        assert banned not in text, banned  # AGENTS rule 8: removed from the dashboard
    assert "realized_pnl" in o["performance"]  # P&L is shown


def test_insight_flags_kill_switch_and_unset_limits():
    lines = ai_insight({
        "kill_switch": {"active": True}, "unset_limits": ["max_daily_loss"],
        "performance": {"profit_factor": 0.8, "trades": 3},
    })
    joined = " ".join(lines)
    assert "kill switch" in joined and "max_daily_loss" in joined and "Profit factor" in joined


def test_load_state_reads_files_and_missing_files_are_safe(tmp_path):
    from app import load_state  # dashboard/app.py

    paths = {
        "ledger": tmp_path / "ledger.jsonl",
        "audit": tmp_path / "audit.jsonl",
        "kill": tmp_path / "kill.json",
        "limits": tmp_path / "missing_limits.yaml",
        "settings": tmp_path / "missing_settings.yaml",
        "page": tmp_path / "x.html",
    }
    empty = load_state(paths)
    assert empty["positions"] == [] and empty["settings"]["pnl_visible_default"] is True

    paths["ledger"].write_text(
        json.dumps({"event": "fill", "symbol": "INFY", "signed_qty": 10, "price": 100.0}) + "\n"
        + json.dumps({"event": "intent", "intent_id": "x"}) + "\n",
        encoding="utf-8",
    )
    s = load_state(paths, last_prices={"INFY": 103.0})
    assert s["positions"][0]["symbol"] == "INFY"
    assert s["positions"][0]["unrealized_pnl"] == pytest.approx(30.0)
    assert s["feed"]["status"] == "not connected"


def test_overview_with_closed_trade_needs_blue_profit_factor():
    # Profit factor is delegated to Blue_engine backtest.metrics, which needs openalgo.
    pytest.importorskip("openalgo")
    f = [fill("A", 10, 100.0), fill("A", -10, 110.0)]
    o = build_overview(f, [], {}, {"blocked": False, "max_daily_loss": 1000.0}, {})
    assert o["performance"]["trades"] == 1
    assert o["performance"]["realized_pnl"] == pytest.approx(100.0)
    assert o["performance"]["win_rate"] == pytest.approx(1.0)


def test_equity_curve_is_cumulative_realized_with_drawdown():
    e = equity_curve([{"pnl": 100.0}, {"pnl": -150.0}, {"pnl": 50.0}])
    assert e["points"] == [0.0, 100.0, -50.0, 0.0]
    assert e["max_drawdown"] == pytest.approx(150.0)


def test_equity_curve_empty_is_just_the_start():
    assert equity_curve([]) == {"points": [0.0], "max_drawdown": 0.0}
