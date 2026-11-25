"""Regression tests for the 2026-11-15 live-readiness audit fixes.

Flow stage: Risk gate, Strategy bot, Live execution
Status: TESTS. Real bot, real gate, real paper broker, no network, no orders sent.

Covers:
  - daily P&L: a closed trade is counted once (was counted twice)
  - strategy loss counter: each open position adds its change once (was once per position)
  - bot.yaml risk_per_trade equals settings.yaml sizing (was 0.25% vs 0.5%)
  - live sizing equity is capped at live.max_capital_inr
  - the live bot's kill switch file is the one the start gate reads
"""
import importlib.util
import json
import pathlib
import sys
from datetime import date, datetime

import pandas as pd
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.config import RiskLimits  # noqa: E402
from quant.execution.exit_manager import Position  # noqa: E402
from quant.execution.openalgo_client import LiveApproval, PaperBroker, file_sha256  # noqa: E402
from quant.execution.strategy_bot import BotConfig, PaperSender, StrategyBot  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.risk.controls import HaltHandler  # noqa: E402
from quant.risk.gate import RiskState  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402

NOW = datetime(2026, 11, 17, 10, 15)


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"

    def get(self, kind):
        return [p for k, p in self.records if k == kind]


class TimedBars:
    def __init__(self):
        self.cycle = 0
        self.last = dict(open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000.0)

    def step(self, **kw):
        self.cycle += 1
        self.last.update(kw)

    def bars(self, symbol, now):
        rows = [dict(open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000.0)] * 40
        rows[-2] = dict(self.last)
        index = pd.date_range("2026-11-02 09:15", periods=40, freq="5min") + pd.Timedelta(minutes=5 * self.cycle)
        return pd.DataFrame(rows, index=index)

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class Signal:
    name = "fixed"

    def signal(self, df):
        return {"side": "BUY", "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0, "time": "T1", "atr": 1.0}


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0, max_open_exposure=2_000_000.0,
                      max_daily_loss=50_000.0, max_strategy_loss=25_000.0, max_adv_pct=0.01,
                      max_symbols_per_portfolio=20)


def _bot(tmp_path, bars, *, funds=1_000_000.0):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources))
    audit = Audit()
    bot = StrategyBot(
        cfg=cfg, strategies=[Signal()], bars=bars, sender=PaperSender(PaperBroker()), audit=audit,
        api_key="KEY", read_funds=lambda: funds, limits=_limits(),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(), state=RiskState(),
    )
    bot.run_startup_recon(None)
    return bot, audit


def _df(close):
    rows = [dict(open=close, high=close, low=close, close=close, volume=1e6)] * 40
    index = pd.date_range("2026-11-02 09:15", periods=40, freq="5min")
    return pd.DataFrame(rows, index=index)


def _pos(key, strategy, entry=100.0, qty=10):
    return Position(key=key, symbol=key.split("|")[0], side="BUY", qty=qty, entry=entry,
                    stop=98.0, target=106.0, atr=1.0, adv_shares=5e6, prev_close=100.0,
                    circuit_pct=0.2, strategy=strategy)


# ---- daily P&L: a closed trade is counted once -----------------------------------------------

@pytest.fixture(autouse=True)
def _test_only_owner_caps(tmp_path, monkeypatch):
    """Owner caps in config/risk_limits.yaml would refuse these test
    entries. Point the gate at a test-only file with large caps. The real file is not changed."""
    from quant.core import owner_limits_store as ols
    p = tmp_path / "owner_caps_for_tests.yaml"
    p.write_text("max_order_quantity: 1000000\nmax_order_notional: 100000000\n"
                 "max_open_exposure: 100000000\nblocked: false\n", encoding="utf-8")
    monkeypatch.setattr(ols, "DEFAULT_PATH", p)
    monkeypatch.setattr(ols, "_loaded_from", None)
    monkeypatch.setattr(ols, "_cached", {})
    yield


def test_a_closed_trade_is_counted_once_in_daily_pnl(tmp_path):
    bars = TimedBars()
    bot, audit = _bot(tmp_path, bars)
    bot.cycle(NOW)                                          # entry fills near 100
    assert len(bot._positions) == 1
    # low stays above the breakeven stop (100.5) so only the target can exit
    bars.step(close=102.0, high=102.5, low=101.0)   # open profit: marked to market
    bot.cycle(NOW)
    assert bot.state.daily_pnl > 10.0                       # the open gain is in the counters
    bars.step(high=107.0, low=101.0)                        # target hit: exit
    bot.cycle(NOW)
    assert not bot._positions
    realized = sum(p["amount"] for p in audit.get("pnl_realized"))
    assert realized > 10.0
    # The bug: the open gain stayed in daily P&L and the realized gain was added on top.
    assert abs(bot.state.daily_pnl - realized) < 0.01


def test_strategy_loss_counter_is_not_multiplied_by_open_positions(tmp_path):
    """Two open positions in one strategy: a 2-rupee move per share adds 2 x qty once each."""
    bars = TimedBars()
    bot, _ = _bot(tmp_path, bars)
    bot._positions = {"AAA|fixed|BUY|t1": _pos("AAA|fixed|BUY|t1", "fixed"),
                      "BBB|fixed|BUY|t1": _pos("BBB|fixed|BUY|t1", "fixed")}
    cache = {"AAA": _df(102.0), "BBB": _df(102.0)}
    bot._mark_positions_to_market(cache, NOW, {})
    assert abs(bot.state.daily_pnl - 40.0) < 1e-9                    # 2 positions x 10 shares x 2
    assert abs(bot.state.strategy_pnl["fixed"] - 40.0) < 1e-9        # bug: was 80 (once per position)


def test_marks_are_not_added_twice_when_nothing_moves(tmp_path):
    bars = TimedBars()
    bot, _ = _bot(tmp_path, bars)
    bot._positions = {"AAA|fixed|BUY|t1": _pos("AAA|fixed|BUY|t1", "fixed")}
    cache = {"AAA": _df(102.0)}
    bot._mark_positions_to_market(cache, NOW, {})
    bot._mark_positions_to_market(cache, NOW, {})                    # same bar again
    assert abs(bot.state.daily_pnl - 20.0) < 1e-9


def test_averaging_into_a_position_reverses_the_old_mark(tmp_path):
    """A second fill changes the average entry; the old mark must not stay in the counters."""
    bars = TimedBars()
    bot, _ = _bot(tmp_path, bars)
    key = "AAA|fixed|BUY|t1"
    bot._positions = {key: _pos(key, "fixed", entry=100.0, qty=10)}
    bot._mark_positions_to_market({"AAA": _df(102.0)}, NOW, {})      # +20
    assert abs(bot.state.daily_pnl - 20.0) < 1e-9
    bot._record_fill(bot._parents.get(key) or _Parent(key), 10, {"avg_price": 102.0})
    # Old mark reversed; the new average entry is 101 (10 @ 100 and 10 @ 102 at 102 close -> 0 gain on new half)
    bot._mark_positions_to_market({"AAA": _df(102.0)}, NOW, {})
    expected = (102.0 - 101.0) * 20                                  # 20 shares, average entry 101
    assert abs(bot.state.daily_pnl - expected) < 1e-9


class _Parent:
    def __init__(self, key):
        from quant.core.types import TradeIntent
        self.key = key
        self.intent = TradeIntent(intent_id=key, strategy_id="fixed", symbol="AAA", side="BUY", qty=10,
                                  ref_price=102.0, kind="entry", trade_day=NOW.date())
        self.stop, self.target, self.atr = 98.0, 106.0, 1.0
        self.strategy = "fixed"
        self.adv_shares, self.prev_close, self.circuit_pct = 5e6, 100.0, 0.2


# ---- bot.yaml and settings.yaml agree on the risk per trade ---------------------------------

def test_bot_yaml_does_not_set_risk_per_trade_and_the_bot_reads_settings():
    from quant.execution.strategy_bot import load_bot_config
    bot = yaml.safe_load((ROOT / "config" / "bot.yaml").read_text(encoding="utf-8"))
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert "risk_per_trade" not in bot
    from quant.core.config import RiskLimits
    assert RiskLimits.risk_per_trade(ROOT / "config" / "settings.yaml") == settings["sizing"]["risk_per_trade"]
    # product "auto" is resolved from the saved MTF at load time; the bot file itself only sets the mode
    from quant.execution.strategy_bot import BotConfig
    assert BotConfig.from_yaml(ROOT / "config" / "bot.yaml").product == "auto"


def test_bot_yaml_refuses_its_own_risk_per_trade(tmp_path):
    import pytest
    from quant.execution.strategy_bot import BotConfig
    p = tmp_path / "bot.yaml"
    p.write_text("mode: paper\nrisk_per_trade: 0.01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="settings.yaml"):
        BotConfig.from_yaml(p)


# ---- live runner: capital cap and kill file ---------------------------------------------------

def _runner():
    spec = importlib.util.spec_from_file_location(
        "run_strategy_bot_live", ROOT / "scripts" / "run" / "run_strategy_bot_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Reader:
    def __init__(self, cash):
        self.cash = cash

    def funds(self, key):
        return {"status": "success", "data": {"availablecash": str(self.cash)}}

    def positionbook(self, key):
        return {"status": "success", "data": []}


def _live_build(tmp_path, cash, cap, seen):
    settings = tmp_path / "settings.yaml"
    settings.write_text("live:\n  max_capital_inr: 500000\n"
                        "trade_setup:\n  timeframes:\n    htf: 1h\n    mtf: 1d\n    ltf: 5m\n", encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("max_order_quantity: 10\nmax_order_notional: 10000\nmax_open_exposure: 20000\n"
                      "max_daily_loss: 1000\nmax_strategy_loss: 500\nmax_adv_pct: 0.01\n"
                      "max_symbols_per_portfolio: 5\nblocked: false\n", encoding="utf-8")
    cfg = tmp_path / "bot.yaml"
    cfg.write_text(yaml.safe_dump({"mode": "live", "live_enabled": True}), encoding="utf-8")
    approval = LiveApproval(approver="Test Owner", approved_on=date.today(),
                            limits_sha256=file_sha256(limits))

    def startup(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "reasons": [], "approval": approval, "capital_cap": cap}

    class _Transport:
        def __call__(self, url, payload, **kw):
            raise AssertionError("building the bot must not call the broker")

    run = _runner()
    bot, _ = run.build_live_bot(bot_cfg_path=cfg, limits_path=limits, state_dir=tmp_path / "s",
                                base_url="http://x", api_key="K", transport=_Transport(),
                                reader=_Reader(cash), startup_fn=startup,
                                settings_path=settings)
    return bot


def test_live_sizing_equity_is_capped_at_the_fixed_cap(tmp_path):
    seen = []
    bot = _live_build(tmp_path, cash=5_000_000.0, cap=500_000.0, seen=seen)
    assert bot._read_funds() == 500_000.0


def test_live_sizing_equity_uses_broker_cash_when_it_is_lower(tmp_path):
    seen = []
    bot = _live_build(tmp_path, cash=120_000.0, cap=500_000.0, seen=seen)
    assert bot._read_funds() == 120_000.0


def test_live_gate_is_given_the_bots_own_kill_file(tmp_path):
    seen = []
    _live_build(tmp_path, cash=1.0, cap=500_000.0, seen=seen)
    assert seen, "the start gate must run"
    assert all(k["kill_path"] == tmp_path / "s" / "kill.json" for k in seen)


def test_live_build_refuses_without_a_capital_cap(tmp_path):
    import pytest
    from quant.execution.strategy_bot import BotRefused
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("max_order_quantity: 10\n", encoding="utf-8")
    cfg = tmp_path / "bot.yaml"
    cfg.write_text(yaml.safe_dump({"mode": "live", "live_enabled": True}), encoding="utf-8")
    approval = LiveApproval(approver="O", approved_on=date.today(), limits_sha256=file_sha256(limits))
    run = _runner()
    with pytest.raises(BotRefused, match="capital cap"):
        run.build_live_bot(bot_cfg_path=cfg, limits_path=limits, state_dir=tmp_path / "s",
                           base_url="http://x", api_key="K", transport=lambda u, p: {},
                           reader=_Reader(1.0),
                           startup_fn=lambda **k: {"ok": True, "reasons": [], "approval": approval,
                                                   "capital_cap": None})


# ---- kill switch is file-backed: a trip from another process stops a running bot -------------

def test_kill_switch_reads_the_file_every_time(tmp_path):
    from quant.risk.kill_switch import KillSwitch
    running = KillSwitch(tmp_path / "kill.json")          # opened before any trip
    assert running.active is False
    KillSwitch(tmp_path / "kill.json").trip("dashboard STOP", source="owner")   # another process
    assert running.active is True
    assert "dashboard STOP" in running.reason
    running.reset("Owner", "checked")
    assert KillSwitch(tmp_path / "kill.json").active is False


def test_unreadable_kill_file_is_treated_as_on(tmp_path):
    from quant.risk.kill_switch import KillSwitch
    p = tmp_path / "kill.json"
    p.write_text("{broken", encoding="utf-8")
    assert KillSwitch(p).active is True


def test_a_trip_written_elsewhere_stops_a_running_bot_entries(tmp_path):
    bars = TimedBars()
    bot, audit = _bot(tmp_path, bars)                       # bot holds its own KillSwitch object
    KillSwitch(tmp_path / "kill.json").trip("dashboard STOP", source="owner")
    bot.cycle(NOW)
    assert not bot._positions, "no entry may open after a trip written by another process"
