"""Flow stage: Algo router, Risk gate, Approved order queue (paper mode of the strategy bot).
Status: TESTS for PHASE 3 IMPLEMENTED bot in paper mode. Real gate, real POV executor,
and the real PaperBroker (simulated fills, no network). Only the bar feed and audit are fakes.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest
import yaml

from quant.core.config import RiskLimits
from quant.execution.openalgo_client import PaperBroker
from quant.execution.strategy_bot import (
    AnalyzeSender,
    BotConfig,
    BotRefused,
    PaperSender,
    StrategyBot,
)
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

NOW = datetime(2026, 11, 10, 10, 15)


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"

    def kinds(self):
        return [k for k, _ in self.records]


class Bars:
    def __init__(self, volume=5000.0):
        self.volume = volume

    def bars(self, symbol, now):
        return pd.DataFrame({"open": [1.0] * 40, "high": [1.0] * 40, "low": [1.0] * 40,
                             "close": [1.0] * 40, "volume": [self.volume] * 40})

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class Signal:
    name = "fixed"

    def signal(self, df):
        return {"side": "BUY", "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0, "time": "T1"}


class RejectingBroker:
    """Paper broker that rejects every order."""

    def __init__(self):
        self.calls = 0

    def place(self, intent, decision=None):
        self.calls += 1
        return {"order_id": "", "status": "REJECTED", "filled_qty": 0, "avg_price": None}


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _files(tmp_path, approved=True):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": approved}}}),
                       encoding="utf-8")
    return screen, sources


def _bot(tmp_path, *, mode, sender, approved=True, broker_bars=None, **over):
    screen, sources = _files(tmp_path, approved)
    cfg = BotConfig(mode=mode, screen_path=str(screen), data_sources_path=str(sources), **over)
    audit = Audit()
    bot = StrategyBot(
        cfg=cfg, strategies=[Signal()], bars=broker_bars or Bars(), sender=sender, audit=audit,
        api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
        state=RiskState(),
    )
    return bot, audit


# --- mode and sender checks ---

def test_paper_mode_accepted_with_paper_sender(tmp_path):
    bot, _ = _bot(tmp_path, mode="paper", sender=PaperSender(PaperBroker()))
    assert bot.cfg.mode == "paper"


def test_paper_config_with_sandbox_sender_refused(tmp_path):
    with pytest.raises(BotRefused):
        _bot(tmp_path, mode="paper", sender=AnalyzeSender(object()))


def test_sandbox_config_with_paper_sender_refused(tmp_path):
    with pytest.raises(BotRefused):
        _bot(tmp_path, mode="analyze", sender=PaperSender(PaperBroker()))


def test_live_mode_still_refused_even_with_paper_sender(tmp_path):
    with pytest.raises(BotRefused):
        _bot(tmp_path, mode="live", sender=PaperSender(PaperBroker()))


def test_live_enabled_still_refused_in_paper(tmp_path):
    with pytest.raises(BotRefused):
        _bot(tmp_path, mode="paper", sender=PaperSender(PaperBroker()), live_enabled=True)


# --- paper flow ---

def test_paper_cycles_fill_children_and_sum_to_full_qty(tmp_path):
    # Full qty 1250. Bar volume 5000 * pov 0.1 = 500 per child -> 500, 500, 250.
    bot, audit = _bot(tmp_path, mode="paper", sender=PaperSender(PaperBroker()))
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    bot.cycle(NOW + timedelta(minutes=10))
    sent = [p for k, p in audit.records if k == "child_sent"]
    assert [p["qty"] for p in sent] == [500, 500, 250]
    assert sum(p["qty"] for p in sent) == 1250


def test_paper_run_respects_data_source_gate(tmp_path):
    broker = PaperBroker()
    bot, audit = _bot(tmp_path, mode="paper", sender=PaperSender(broker), approved=False)
    report = bot.cycle(NOW)
    assert broker.__dict__.get("_n", 0) == 0  # nothing simulated
    assert "cycle_skipped" in audit.kinds()
    assert report["parents_opened"] == 0


def test_paper_rejection_halts_parent(tmp_path):
    broker = RejectingBroker()
    bot, audit = _bot(tmp_path, mode="paper", sender=PaperSender(broker))
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    assert broker.calls == 1  # no resend after a rejected paper order
    assert "parent_closed" in audit.kinds()


def test_paper_sender_only_calls_broker_place():
    class OnlyPlace:
        def __init__(self):
            self.names = []

        def place(self, intent, decision=None):
            self.names.append("place")
            return {"order_id": "P1", "status": "FILLED", "filled_qty": intent.qty, "avg_price": 100.1}

    from quant.core.types import GateDecision, TradeIntent
    broker = OnlyPlace()
    sender = PaperSender(broker)
    intent = TradeIntent(intent_id="x", strategy_id="s", symbol="INFY", side="BUY", qty=10, ref_price=100.0)
    res = sender.place_child(intent, "KEY", GateDecision("x", True, ()))
    assert res == {"status": "success", "orderid": "P1", "avg_price": 100.1, "filled_qty": 10}
    assert broker.names == ["place"]


def test_paper_sender_maps_rejection_to_error():
    from quant.core.types import GateDecision, TradeIntent
    sender = PaperSender(RejectingBroker())
    intent = TradeIntent(intent_id="x", strategy_id="s", symbol="INFY", side="BUY", qty=10, ref_price=100.0)
    assert sender.place_child(intent, "KEY", GateDecision("x", True, ()))["status"] == "error"


def test_repo_config_mode_is_one_of_allowed(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    cfg = BotConfig.from_yaml(root / "config" / "bot.yaml")
    assert cfg.mode in ("paper", "analyze")
    assert cfg.live_enabled is False
