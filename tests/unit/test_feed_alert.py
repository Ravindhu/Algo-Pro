"""Audit item 6 (2026-11-28): a cycle skipped because the live data source is not approved must
reach the owner as a CRITICAL alert, once per condition, and clear when the source is approved.

Real StrategyBot cycle; only the bar feed, the broker sender and the alert sink are fakes.
"""
import json
from datetime import datetime

import pandas as pd
import yaml

from quant.core.config import RiskLimits
from quant.execution.openalgo_client import PaperBroker
from quant.execution.strategy_bot import BotConfig, PaperSender, StrategyBot, live_feed_approved
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

NOW = datetime(2026, 11, 17, 10, 15)


class _Audit:
    def append(self, kind, payload):
        return "h"


class _Alerts:
    def __init__(self):
        self.sent = []

    def emit(self, level, source, message):
        self.sent.append((level, message))


class _Bars:
    def bars(self, symbol, now):
        return pd.DataFrame({"open": [100.0] * 40, "high": [100.0] * 40, "low": [100.0] * 40,
                             "close": [100.0] * 40, "volume": [5000.0] * 40})

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class _NoStrategy:
    name = "none"
    is_cycle = False

    def signal(self, *a, **k):
        return None


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _bot(tmp_path, approved):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": approved}}}),
                       encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources))
    alerts = _Alerts()
    bot = StrategyBot(cfg=cfg, strategies=[_NoStrategy()], bars=_Bars(), sender=PaperSender(PaperBroker()),
                      audit=_Audit(), api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
                      kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
                      state=RiskState(), alerts=alerts)
    return bot, alerts, sources


def _feed_alerts(alerts):
    return [m for level, m in alerts.sent if "not approved" in m]


def test_skipped_cycle_alerts_critical(tmp_path):
    bot, alerts, _ = _bot(tmp_path, approved=False)
    report = bot.cycle(NOW)
    assert "data source not approved" in report["skipped"]
    assert [level for level, m in alerts.sent if "not approved" in m] == ["CRITICAL"]


def test_repeated_skipped_cycles_alert_only_once(tmp_path):
    bot, alerts, _ = _bot(tmp_path, approved=False)
    for _ in range(3):
        bot.cycle(NOW)
    assert len(_feed_alerts(alerts)) == 1


def test_approval_clears_the_alert_so_a_new_block_alerts_again(tmp_path):
    bot, alerts, sources = _bot(tmp_path, approved=False)
    bot.cycle(NOW)
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    bot.cycle(NOW)                       # approved: runs normally, alert cleared
    assert "feed" not in bot._alerted
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": False}}}), encoding="utf-8")
    bot.cycle(NOW)
    assert len(_feed_alerts(alerts)) == 2


def test_live_feed_approved_is_false_for_missing_or_broken_config(tmp_path):
    assert live_feed_approved(tmp_path / "missing.yaml") is False
    broken = tmp_path / "broken.yaml"
    broken.write_text("sources: [unclosed", encoding="utf-8")
    assert live_feed_approved(broken) is False
    assert live_feed_approved(tmp_path / "x.yaml") is False


def test_live_feed_approved_requires_true_not_a_truthy_string(tmp_path):
    p = tmp_path / "ds.yaml"
    p.write_text("sources:\n  live_feed:\n    live_allowed: 'yes'\n", encoding="utf-8")
    assert live_feed_approved(p) is False
