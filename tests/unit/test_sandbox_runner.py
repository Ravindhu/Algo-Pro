"""Phase 1 of Full Auto (2026-11-27): the sandbox run, orders through OpenAlgo's analyzer.

Rules under test:
1. --sandbox builds the same bot with mode 'analyze' IN MEMORY; config/bot.yaml is not changed.
2. The sandbox start is refused unless OpenAlgo confirms the analyzer is ON (False or None refuse).
3. The sandbox bot sends through AnalyzeSender, and its client is in 'analyze' mode.
4. The live path is unchanged: a config of mode 'analyze' is still refused by the live runner.
5. The supervisor runs the analyze mode with --sandbox; engine start accepts 'analyze'.
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

from quant.execution.strategy_bot import AnalyzeSender, BotRefused  # noqa: E402


def _runner():
    spec = importlib.util.spec_from_file_location("run_live_sandbox_test",
                                                  ROOT / "scripts" / "run" / "run_strategy_bot_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Reader:
    def __init__(self, analyze):
        self.analyze = analyze

    def funds(self, api_key):
        return {"status": "success", "data": {"availablecash": "500000"}}

    def analyzer_mode(self, api_key):
        return self.analyze

    def depth(self, *a, **k):
        return {}

    def quotes(self, *a, **k):
        return {}

    def positionbook(self, *a, **k):
        return {"status": "success", "data": []}


def _files(tmp_path, mode="live", live_enabled=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = tmp_path / "settings.yaml"
    settings.write_text("live:\n  max_capital_inr: 500000\n"
                        "trade_setup:\n  timeframes:\n    htf: 1h\n    mtf: 1d\n    ltf: 5m\n", encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("max_order_quantity: 10\nmax_order_notional: 10000\nmax_open_exposure: 20000\n"
                      "max_daily_loss: 1000\nmax_strategy_loss: 500\nmax_adv_pct: 0.01\n"
                      "max_symbols_per_portfolio: 5\nblocked: false\n", encoding="utf-8")
    cfg = tmp_path / "bot.yaml"
    cfg.write_text(yaml.safe_dump({"mode": mode, "live_enabled": live_enabled}), encoding="utf-8")
    return cfg, limits, settings


class _Transport:
    def __call__(self, url, payload, **kw):
        raise AssertionError("building the bot must not call the broker")


SENDERS = []


def _build(tmp_path, analyze, sandbox=True, mode="live"):
    cfg, limits, settings = _files(tmp_path, mode=mode)
    before = cfg.read_text(encoding="utf-8")

    def startup(**kwargs):
        return {"ok": True, "reasons": [], "capital_cap": 500000}

    run = _runner()
    original = run._live_sender

    def recording(client, cfg_, sandbox=False):
        sender = original(client, cfg_, sandbox=sandbox)
        SENDERS.append(sender)
        return sender

    run._live_sender = recording
    bot, _ = run.build_live_bot(bot_cfg_path=cfg, limits_path=limits, state_dir=tmp_path / "s",
                                base_url="http://x", api_key="K", transport=_Transport(),
                                reader=_Reader(analyze), startup_fn=startup,
                                settings_path=settings, sandbox=sandbox)
    return bot, cfg, before


def test_sandbox_refused_unless_analyzer_confirmed_on(tmp_path):
    for analyze in (False, None):
        with pytest.raises(BotRefused, match="analyzer is not confirmed ON"):
            _build(tmp_path / str(analyze), analyze=analyze)


def run_live_sender_class():
    from quant.execution.strategy_bot import LiveSender
    return LiveSender


def test_sandbox_builds_analyze_bot_and_leaves_config_file_alone(tmp_path):
    bot, cfg, before = _build(tmp_path, analyze=True)
    assert bot.cfg.mode == "analyze" and bot.cfg.live_enabled is False
    assert isinstance(SENDERS[-1], AnalyzeSender)
    assert not isinstance(SENDERS[-1], run_live_sender_class())
    assert cfg.read_text(encoding="utf-8") == before          # bot.yaml still says live


def test_live_path_still_refuses_an_analyze_config(tmp_path):
    with pytest.raises(BotRefused, match="live runner needs"):
        _build(tmp_path / "x", analyze=True, sandbox=False, mode="analyze")


def test_supervisor_runs_analyze_with_sandbox_flag():
    import supervise_bot as sb
    cmd = sb.bot_command("analyze", "http://x")
    assert cmd[1].endswith("run_strategy_bot_live.py")
    assert cmd[-1] == "--sandbox"
    assert "--sandbox" not in sb.bot_command("live", "http://x")


def test_engine_start_accepts_analyze(tmp_path, monkeypatch):
    from quant.core import engine_control
    calls = {}
    monkeypatch.setattr(engine_control, "running_pid", lambda *a, **k: None)
    runner = tmp_path / "runner.py"
    runner.write_text("", encoding="utf-8")

    class _Proc:
        pid = 4242

    monkeypatch.setattr(engine_control.subprocess, "Popen",
                        lambda cmd, **kw: calls.setdefault("cmd", cmd) and _Proc())
    ok, msg, pid = engine_control.request_manual_start(kill_active=False, mode="analyze",
                                                       pid_path=tmp_path / "p.json",
                                                       log_path=tmp_path / "l.log", runner=runner)
    assert ok and "analyze" in msg and calls["cmd"][-2:] == ["--mode", "analyze"]
    ok, msg, _ = engine_control.request_manual_start(kill_active=False, mode="bogus",
                                                     pid_path=tmp_path / "p.json",
                                                     log_path=tmp_path / "l.log", runner=runner)
    assert not ok
