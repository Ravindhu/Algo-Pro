"""Flow stage: Algo router, Risk gate, Approved order queue (strategy bot tests).
Status: TESTS for PHASE 3 IMPLEMENTED bot. Uses the real risk gate and real POV executor.
Only the order sender, the bar feed and the audit sink are fakes.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest
import yaml

from quant.core.config import RiskLimits
from quant.execution.strategy_bot import (
    BotConfig,
    BotRefused,
    StrategyBot,
    live_feed_approved,
    read_screen,
    run_loop,
)
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

NOW = datetime(2026, 11, 10, 10, 15)


class FakeAudit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"

    def kinds(self):
        return [k for k, _ in self.records]


class FakeSender:
    bot_mode = "analyze"

    def __init__(self, status="success"):
        self.sent = []
        self.status = status

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        self.sent.append((intent.intent_id, intent.qty))
        return {"status": self.status, "orderid": f"O{len(self.sent)}"}


class FakeBars:
    def __init__(self, volume=5000.0, bars_ok=True, stats_ok=True):
        self.volume = volume
        self.bars_ok = bars_ok
        self.stats_ok = stats_ok

    def bars(self, symbol, now):
        if not self.bars_ok:
            return None
        return pd.DataFrame({"open": [1.0] * 40, "high": [1.0] * 40, "low": [1.0] * 40,
                             "close": [1.0] * 40, "volume": [self.volume] * 40})

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0} if self.stats_ok else None


class FixedSignal:
    """Strategy stub: returns one BUY signal on a fixed closed bar time."""
    name = "fixed"

    def __init__(self, time="T1", side="BUY", raise_error=False):
        self.time = time
        self.side = side
        self.raise_error = raise_error

    def signal(self, df):
        if self.raise_error:
            raise RuntimeError("boom")
        return {"side": self.side, "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0,
                "time": self.time, "atr": 1.0}


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _write_screen(tmp_path, as_of=NOW, results=None):
    if results is None:
        results = [{"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]
    path = tmp_path / "latest.json"
    path.write_text(json.dumps({"as_of": as_of.isoformat(), "results": results}), encoding="utf-8")
    return path


def _write_sources(tmp_path, allowed):
    path = tmp_path / "data_sources.yaml"
    path.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": allowed}}}), encoding="utf-8")
    return path


def _bot(tmp_path, *, strategies=None, bars=None, sender=None, audit=None, funds=1_000_000.0,
         allowed=True, kill_on=False, **cfg_over):
    screen = _write_screen(tmp_path)
    sources = _write_sources(tmp_path, allowed)
    cfg = BotConfig(screen_path=str(screen), data_sources_path=str(sources), **cfg_over)
    kill = KillSwitch(tmp_path / "kill.json")
    if kill_on:
        kill.trip("test")
    audit = audit or FakeAudit()
    return StrategyBot(
        cfg=cfg, strategies=strategies or [FixedSignal()], bars=bars or FakeBars(),
        sender=sender or FakeSender(), audit=audit, api_key="KEY",
        read_funds=lambda: funds, limits=_limits(), kill=kill, safe=SafeMode(),
        halt=HaltHandler(), state=RiskState(),
    ), audit


# --- configuration and refusals ---

def test_live_mode_refused(tmp_path):
    with pytest.raises(BotRefused):
        _bot(tmp_path, mode="live")


def test_live_enabled_refused(tmp_path):
    with pytest.raises(BotRefused):
        _bot(tmp_path, live_enabled=True)


def test_analyze_mode_accepted(tmp_path):
    bot, _ = _bot(tmp_path)
    assert bot.cfg.mode == "analyze"


def test_repo_bot_config_loads_and_is_paper_or_analyze():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    cfg = BotConfig.from_yaml(root / "config" / "bot.yaml")
    assert cfg.mode in ("paper", "analyze") and cfg.live_enabled is False and cfg.interval_minutes == 5


def test_repo_data_source_is_not_approved_for_live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    assert live_feed_approved(root / "config" / "data_sources.yaml") is False


# --- data-source gate (set live inputs) ---

def test_unapproved_data_source_skips_cycle_and_sends_nothing(tmp_path):
    sender = FakeSender()
    bot, audit = _bot(tmp_path, allowed=False, sender=sender)
    report = bot.cycle(NOW)
    assert sender.sent == []
    assert "cycle_skipped" in audit.kinds()
    assert report["parents_opened"] == 0


def test_live_feed_approved_fails_closed_on_missing_file(tmp_path):
    assert live_feed_approved(tmp_path / "nope.yaml") is False


# --- screen input ---

def test_stale_screen_gives_no_symbols(tmp_path):
    path = _write_screen(tmp_path, as_of=NOW - timedelta(minutes=30))
    assert read_screen(path, NOW, 10) == []


def test_screen_skips_asm_unselected_and_unknown_circuit(tmp_path):
    rows = [
        {"symbol": "A", "selected": True, "asm_gsm": False, "circuit_pct": 0.2},
        {"symbol": "B", "selected": True, "asm_gsm": True, "circuit_pct": 0.2},
        {"symbol": "C", "selected": False, "asm_gsm": False, "circuit_pct": 0.2},
        {"symbol": "D", "selected": True, "asm_gsm": False},
        {"symbol": "E", "selected": True, "asm_gsm": None, "circuit_pct": 0.2},
    ]
    path = _write_screen(tmp_path, results=rows)
    assert [s["symbol"] for s in read_screen(path, NOW, 10)] == ["A"]


def test_missing_screen_file_gives_no_symbols(tmp_path):
    assert read_screen(tmp_path / "missing.json", NOW, 10) == []


# --- entry and slicing ---

def test_parent_is_sliced_across_cycles_and_sums_to_full_qty(tmp_path):
    # Full qty 1250 (1,000,000 * 0.25% / 2.0). Bar volume 5000 * pov 0.1 = 500 per child.
    sender = FakeSender()
    bot, _ = _bot(tmp_path, sender=sender, bars=FakeBars(volume=5000.0))
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    bot.cycle(NOW + timedelta(minutes=10))
    qtys = [q for _, q in sender.sent]
    assert qtys == [500, 500, 250]
    assert sum(qtys) == 1250


def test_same_signal_on_later_cycles_opens_one_parent(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, sender=sender, bars=FakeBars(volume=1_000_000.0))
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    # One parent sized 1250 in one child on the first cycle; the signal is not reopened.
    assert [q for _, q in sender.sent] == [1250]


def test_zero_funds_refuses_entry(tmp_path):
    sender = FakeSender()
    bot, audit = _bot(tmp_path, sender=sender, funds=0.0)
    report = bot.cycle(NOW)
    assert sender.sent == []
    assert report["parents_opened"] == 0
    assert "parent_gate" in audit.kinds()


def test_failing_funds_read_is_zero_and_refuses_entry(tmp_path):
    def boom():
        raise OSError("read failed")
    screen = _write_screen(tmp_path)
    sources = _write_sources(tmp_path, True)
    cfg = BotConfig(screen_path=str(screen), data_sources_path=str(sources))
    sender = FakeSender()
    bot = StrategyBot(cfg=cfg, strategies=[FixedSignal()], bars=FakeBars(), sender=sender,
                      audit=FakeAudit(), api_key="KEY", read_funds=boom, limits=_limits(),
                      kill=KillSwitch(tmp_path / "k.json"), safe=SafeMode(), halt=HaltHandler(),
                      state=RiskState())
    bot.cycle(NOW)
    assert sender.sent == []


def test_kill_switch_blocks_children(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, sender=sender, kill_on=True)
    bot.cycle(NOW)
    assert sender.sent == []


def test_no_bars_skips_symbol(tmp_path):
    sender = FakeSender()
    bot, audit = _bot(tmp_path, sender=sender, bars=FakeBars(bars_ok=False))
    report = bot.cycle(NOW)
    assert sender.sent == []
    assert any("no fresh bars" in s for s in report["skipped"])


def test_no_daily_stats_skips_symbol(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, sender=sender, bars=FakeBars(stats_ok=False))
    report = bot.cycle(NOW)
    assert sender.sent == []
    assert any("no daily stats" in s for s in report["skipped"])


def test_strategy_error_is_logged_and_cycle_goes_on(tmp_path):
    sender = FakeSender()
    bot, audit = _bot(tmp_path, sender=sender, strategies=[FixedSignal(raise_error=True)])
    bot.cycle(NOW)
    assert "strategy_error" in audit.kinds()
    assert sender.sent == []


def test_non_success_reply_halts_parent(tmp_path):
    sender = FakeSender(status="error")
    bot, audit = _bot(tmp_path, sender=sender)
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    assert len(sender.sent) == 1  # no second child is sent after a failed reply
    assert "parent_closed" in audit.kinds()


def test_parent_dropped_after_max_cycles(tmp_path):
    sender = FakeSender()
    bot, audit = _bot(tmp_path, sender=sender, bars=FakeBars(volume=0.0), max_cycles=2)
    for i in range(4):
        bot.cycle(NOW + timedelta(minutes=5 * i))
    assert sender.sent == []
    assert any(k == "parent_closed" for k in audit.kinds())


def test_every_decision_is_audited(tmp_path):
    bot, audit = _bot(tmp_path)
    bot.cycle(NOW)
    kinds = set(audit.kinds())
    assert {"screen", "parent_gate", "gate", "child_sent"} <= kinds


def test_short_side_parent_is_supported(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, sender=sender, strategies=[FixedSignal(side="SELL", time="S1")])
    bot.cycle(NOW)
    assert [q for _, q in sender.sent] == [500]  # 10% of bar volume 5000, not the full 1250
    assert "SELL" in sender.sent[0][0]


# --- loop ---

def test_run_loop_runs_cycles_and_sleeps_between(tmp_path):
    bot, _ = _bot(tmp_path)
    sleeps = []
    times = iter([NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)])
    done = run_loop(bot, clock=lambda: next(times), sleep=sleeps.append, max_cycles=3)
    assert done == 3
    assert sleeps == [300, 300]


def test_run_loop_logs_cycle_error_and_continues(tmp_path):
    bot, audit = _bot(tmp_path)
    calls = {"n": 0}
    original = bot.cycle

    def flaky(now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("feed down")
        return original(now)

    bot.cycle = flaky
    run_loop(bot, clock=lambda: NOW, sleep=lambda s: None, max_cycles=2)
    assert "cycle_error" in audit.kinds()
    assert calls["n"] == 2


def test_run_loop_propagates_refusal(tmp_path):
    bot, _ = _bot(tmp_path)

    def refuse(now):
        raise BotRefused("mode changed")

    bot.cycle = refuse
    with pytest.raises(BotRefused):
        run_loop(bot, clock=lambda: NOW, sleep=lambda s: None, max_cycles=1)
