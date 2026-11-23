"""Phase 1 tests: the live sender and the live runner gates.

Flow stage: Algo router, Risk gate, Approved order queue, OpenAlgo
Status: TESTS for PHASE 1 (live sender). Uses the real OpenAlgoClient guard and the real
bot constructor. The HTTP transport, the reader and the startup gate are fakes.
No test here sends an order to a real broker.
"""
import importlib.util
from datetime import date
from pathlib import Path

import pytest
import yaml

from quant.audit.log import AuditLog
from quant.core.types import GateDecision, TradeIntent
from quant.execution.openalgo_client import LiveApproval, OpenAlgoClient, file_sha256
from quant.execution.strategy_bot import (AnalyzeSender, BotConfig, BotRefused, LiveSender,
                                          PaperSender, StrategyBot)
from quant.execution.openalgo_client import PaperBroker
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

ROOT = Path(__file__).resolve().parents[3]
LIMITS = ROOT / "config" / "risk_limits.yaml"
RUNNER = ROOT / "scripts" / "run" / "run_strategy_bot_live.py"


def _runner():
    spec = importlib.util.spec_from_file_location("run_strategy_bot_live_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Recorder:
    """Fake HTTP transport. Records every call so tests can prove no order was sent."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or {"status": "success", "orderid": "ORD-1"}

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        return dict(self.reply)


class FakeAudit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"


class FakeBars:
    def bars(self, sym, now):
        return None

    def daily_stats(self, sym, now):
        return None


class FakeReader:
    def __init__(self, funds=None, funds_error=False):
        self._funds = funds
        self._funds_error = funds_error

    def funds(self, api_key):
        if self._funds_error:
            raise RuntimeError("funds down")
        return self._funds

    def positionbook(self, api_key):
        return {"status": "success", "data": []}

    def history(self, *a, **k):
        raise AssertionError("no history call expected in these tests")


def _intent(qty=10):
    return TradeIntent(intent_id="INT-1", strategy_id="engulfing", symbol="INFY",
                       side="BUY", qty=qty, ref_price=100.0, kind="entry")


def _live_cfg(**over):
    base = dict(mode="live", live_enabled=True)
    base.update(over)
    return BotConfig(**base)


class _Stops:
    """Stub broker stops for live-mode construction (no network; never used in these tests)."""
    def place_stop(self, *a, **k): return {"status": "error", "reason": "stub"}
    def stop_status(self, *a, **k): return {"order_status": ""}
    def cancel_stop(self, *a, **k): return {"status": "error", "reason": "stub"}


def _bot(cfg, sender, live_gate=None, tmp_path=None):
    from quant.execution.position_store import PositionStore
    return StrategyBot(
        cfg=cfg, strategies=[], bars=FakeBars(), sender=sender, audit=FakeAudit(),
        api_key="KEY", read_funds=lambda: 100000.0, limits=_limits(), kill=KillSwitch(tmp_path / "k.json"),
        safe=SafeMode(), halt=HaltHandler(), state=RiskState(), live_gate=live_gate,
        stops=_Stops(), position_store=PositionStore(tmp_path / "positions.json"),
        entry_guard=lambda: (True, []),
    )


def _limits():
    from quant.core.config import RiskLimits
    return RiskLimits.from_yaml(LIMITS)


def _ok_gate():
    return True, []


def _live_client(transport=None, approval=None):
    return OpenAlgoClient("http://x", transport or Recorder(), mode="live",
                          live_approval=approval, limits_path=LIMITS)


# --- the sender ---

def test_live_sender_reports_live_mode():
    assert LiveSender.bot_mode == "live"


def test_live_client_without_approval_sends_no_order():
    """The real client refuses without a LiveApproval, so no HTTP call reaches the broker."""
    transport = Recorder()
    sender = LiveSender(_live_client(transport, approval=None))
    decision = GateDecision(intent_id="INT-1", approved=True)
    with pytest.raises(PermissionError):
        sender.place_child(_intent(), "KEY", decision)
    assert transport.calls == []


def test_live_client_with_mismatched_limits_hash_sends_no_order(tmp_path):
    """A stale approval (wrong limits hash) is void: no order is sent."""
    transport = Recorder()
    approval = LiveApproval(approver="Test Owner", approved_on=date.today(), limits_sha256="0" * 64)
    client = _live_client(transport, approval=approval)
    with pytest.raises(PermissionError):
        LiveSender(client).place_child(_intent(), "KEY", GateDecision(intent_id="INT-1", approved=True))
    assert transport.calls == []


# --- bot constructor guard ---

def test_live_mode_needs_live_enabled(tmp_path):
    with pytest.raises(BotRefused, match="live_enabled"):
        _bot(BotConfig(mode="live", live_enabled=False), LiveSender(_live_client()),
             live_gate=_ok_gate, tmp_path=tmp_path)


def test_live_mode_needs_a_gate_function(tmp_path):
    with pytest.raises(BotRefused, match="startup gate"):
        _bot(_live_cfg(), LiveSender(_live_client()), live_gate=None, tmp_path=tmp_path)


def test_failed_live_gate_reasons_are_reported(tmp_path):
    gate = lambda: (False, ["no live approval on file"])
    with pytest.raises(BotRefused, match="no live approval on file"):
        _bot(_live_cfg(), LiveSender(_live_client()), live_gate=gate, tmp_path=tmp_path)


def test_live_enabled_refused_outside_live_mode(tmp_path):
    with pytest.raises(BotRefused, match="live_enabled needs mode live"):
        _bot(BotConfig(mode="analyze", live_enabled=True), AnalyzeSender(object()), tmp_path=tmp_path)


def test_live_mode_with_paper_sender_refused(tmp_path):
    with pytest.raises(BotRefused, match="sender is for"):
        _bot(_live_cfg(), PaperSender(PaperBroker()), live_gate=_ok_gate, tmp_path=tmp_path)


def test_live_mode_accepted_when_enabled_gated_and_sender_matches(tmp_path):
    bot = _bot(_live_cfg(), LiveSender(_live_client()), live_gate=_ok_gate, tmp_path=tmp_path)
    assert bot.cfg.mode == "live"


# --- runner: cash reader ---

def test_available_cash_reads_the_funds_reply():
    run = _runner()
    assert run.available_cash(FakeReader({"status": "success", "data": {"availablecash": "250000.5"}}), "K") == 250000.5


@pytest.mark.parametrize("reader", [
    FakeReader(funds_error=True),
    FakeReader({"status": "success"}),
    FakeReader({"status": "success", "data": {"availablecash": "-5"}}),
    FakeReader({"status": "success", "data": {"availablecash": "nan"}}),
])
def test_available_cash_is_zero_when_unknown(reader):
    assert _runner().available_cash(reader, "K") == 0.0


# --- runner: build ---

def _write_live_cfg(tmp_path, **over):
    data = dict(mode="live", live_enabled=True)
    data.update(over)
    path = tmp_path / "bot.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_runner_refuses_when_config_is_not_live(tmp_path):
    run = _runner()
    cfg = _write_live_cfg(tmp_path, mode="analyze", live_enabled=False)
    with pytest.raises(BotRefused, match="live runner needs"):
        run.build_live_bot(bot_cfg_path=cfg, limits_path=LIMITS, state_dir=tmp_path / "s",
                           base_url="http://x", api_key="K", transport=Recorder(),
                           reader=FakeReader(), startup_fn=lambda **k: {"ok": True, "reasons": [], "approval": None})


def test_runner_refuses_when_start_gate_fails_and_sends_nothing(tmp_path):
    run = _runner()
    cfg = _write_live_cfg(tmp_path)
    transport = Recorder()
    failing = lambda **k: {"ok": False, "reasons": ["no live approval on file"], "approval": None}
    with pytest.raises(BotRefused, match="no live approval on file"):
        run.build_live_bot(bot_cfg_path=cfg, limits_path=LIMITS, state_dir=tmp_path / "s",
                           base_url="http://x", api_key="K", transport=transport,
                           reader=FakeReader(), startup_fn=failing)
    assert transport.calls == []


def test_runner_builds_live_bot_when_gate_passes_and_places_no_order(tmp_path):
    run = _runner()
    cfg = _write_live_cfg(tmp_path)
    transport = Recorder()
    approval = LiveApproval(approver="Test Owner", approved_on=date.today(),
                            limits_sha256=file_sha256(LIMITS))
    passing = lambda **k: {"ok": True, "reasons": [], "approval": approval, "capital_cap": 500000.0}
    settings = tmp_path / "settings.yaml"
    settings.write_text("live:\n  max_capital_inr: 500000\n"
                        "trade_setup:\n  timeframes:\n    htf: 1h\n    mtf: 1d\n    ltf: 5m\n", encoding="utf-8")
    bot, audit = run.build_live_bot(bot_cfg_path=cfg, limits_path=LIMITS, state_dir=tmp_path / "s",
                                    base_url="http://x", api_key="K", transport=transport,
                                    reader=FakeReader(), startup_fn=passing,
                                    settings_path=settings)
    assert bot.cfg.mode == "live"
    assert isinstance(bot._executor._sender, LiveSender)
    assert transport.calls == []  # building the bot must never send an order
    assert isinstance(audit, AuditLog)
