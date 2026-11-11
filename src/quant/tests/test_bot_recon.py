"""Flow stage: Ledger and recon (startup gate), Risk gate, Protection.
Status: TESTS for PHASE 3 IMPLEMENTED bot + startup reconciliation (monitor/broker_recon.py).
Real gate, real POV executor, real paper broker. Broker positions are faked.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest
import yaml

from quant.core.config import RiskLimits
from quant.execution.openalgo_client import PaperBroker
from quant.execution.strategy_bot import BotConfig, PaperSender, StrategyBot
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

NOW = datetime(2026, 11, 17, 10, 15)


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"

    def kinds(self):
        return [k for k, _ in self.records]


class Bars:
    def __init__(self):
        self.low = 100.0
        self.high = 100.0
        self.open = 100.0

    def bars(self, symbol, now):
        rows = [dict(open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000.0)] * 40
        rows[-2] = dict(open=self.open, high=self.high, low=self.low, close=100.0, volume=1_000_000.0)
        return pd.DataFrame(rows)

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class Signal:
    name = "fixed"

    def signal(self, df):
        return {"side": "BUY", "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0, "time": "T1", "atr": 1.0}


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _bot(tmp_path):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources))
    audit = Audit()
    bars = Bars()
    bot = StrategyBot(
        cfg=cfg, strategies=[Signal()], bars=bars, sender=PaperSender(PaperBroker()), audit=audit,
        api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(), state=RiskState(),
    )
    return bot, audit, bars


def test_entries_blocked_before_recon_runs(tmp_path):
    bot, audit, _ = _bot(tmp_path)
    bot.cycle(NOW)
    assert bot._positions == {}
    assert "entries_blocked" in audit.kinds()


def test_paper_startup_without_broker_allows_entries(tmp_path):
    bot, audit, _ = _bot(tmp_path)
    gate = bot.run_startup_recon(None)
    assert gate.status == "skipped" and gate.entries_allowed
    bot.cycle(NOW)
    assert len(bot._positions) == 1


def test_broker_match_allows_entries(tmp_path):
    bot, _, _ = _bot(tmp_path)
    gate = bot.run_startup_recon(lambda: {})  # broker flat, bot flat
    assert gate.status == "match"
    bot.cycle(NOW)
    assert len(bot._positions) == 1


def test_mismatch_blocks_new_entries(tmp_path):
    bot, audit, _ = _bot(tmp_path)
    gate = bot.run_startup_recon(lambda: {"INFY": 50})  # broker holds shares the bot does not know
    assert gate.status == "mismatch"
    bot.cycle(NOW)
    assert bot._positions == {}
    assert "entries_blocked" in audit.kinds()


def test_unavailable_broker_blocks_entries(tmp_path):
    bot, _, _ = _bot(tmp_path)

    def broken():
        raise OSError("positionbook down")

    gate = bot.run_startup_recon(broken)
    assert gate.status == "unavailable" and not gate.entries_allowed


def test_live_start_without_broker_reader_refuses_to_run(tmp_path):
    bot, _, _ = _bot(tmp_path)
    with pytest.raises(ValueError):
        bot.run_startup_recon(None, require_broker=True)


def test_mismatch_after_entry_still_allows_exits(tmp_path):
    bot, audit, bars = _bot(tmp_path)
    bot.run_startup_recon(None)
    bot.cycle(NOW)                       # open a position (paper)
    assert len(bot._positions) == 1
    bot.recon = bot.run_startup_recon(lambda: {})  # re-check: broker shows no position -> mismatch
    assert bot.recon.status == "mismatch"
    bars.low = 97.0                      # stop hit on the next closed bar
    bars.open = 99.0
    bot.cycle(NOW + timedelta(minutes=5))
    assert bot._positions == {}
    assert "exit_sent" in audit.kinds()


def test_recon_outcome_is_audited(tmp_path):
    bot, audit, _ = _bot(tmp_path)
    bot.run_startup_recon(lambda: {})
    assert "startup_recon" in audit.kinds()


def test_parse_positionbook_nets_nse_rows_and_skips_other_exchanges():
    from quant.monitor.broker_recon import parse_positionbook
    body = {"status": "success", "data": [
        {"symbol": "INFY", "exchange": "NSE", "netqty": "10"},
        {"symbol": "INFY", "exchange": "NSE", "netqty": "-3"},
        {"symbol": "TCS", "exchange": "BSE", "netqty": "99"},
    ]}
    assert parse_positionbook(body) == {"INFY": 7}


def test_parse_positionbook_fails_closed_on_bad_body():
    from quant.monitor.broker_recon import parse_positionbook
    with pytest.raises(ValueError):
        parse_positionbook({"status": "error"})
    with pytest.raises(ValueError):
        parse_positionbook({"status": "success", "data": [{"exchange": "NSE"}]})
